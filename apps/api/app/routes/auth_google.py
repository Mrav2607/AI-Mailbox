from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.security import create_access_token
from app.db.models import AppUser, ProviderAccount
from app.deps import get_db

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# OAuth state nonces live in Redis so the callback can prove the flow started
# here. Ten minutes is plenty for a consent screen; anything older is stale.
_STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "oauth:state:"
_REDIS_TIMEOUT_SECONDS = 5

_redis_client: redis.Redis | None = None


def _state_store() -> redis.Redis:
    """Build the Redis client on first use so importing this module never
    needs a live Redis (the offline test suite imports it freely)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )
    return _redis_client


def _store_state(state: str) -> None:
    _state_store().set(f"{_STATE_KEY_PREFIX}{state}", "1", ex=_STATE_TTL_SECONDS)


def _consume_state(state: str) -> bool:
    """One-time use: GETDEL so a replayed state can't pass twice."""
    return _state_store().getdel(f"{_STATE_KEY_PREFIX}{state}") is not None


@router.get("/start")
# Sync on purpose. Redis and SQLAlchemy here are both blocking, so an async
# handler would run them straight on the event loop and stall every other
# request (and the health probes) for the duration. A plain def gets handed to
# FastAPI's threadpool instead.
def google_auth_start() -> dict:
    """Begin Google sign-in. No user is required up front -- the user is
    identified (and created on first sign-in) from their Google email in the
    callback. ``state`` is a random nonce stashed in Redis; the callback
    refuses any state it didn't hand out (CSRF protection)."""
    if not settings.google_client_id or not settings.google_redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth config is missing.")
    state = secrets.token_urlsafe(16)
    try:
        _store_state(state)
    except redis.RedisError as exc:
        # Can't remember the nonce, so we can't verify the callback later.
        # Fail closed rather than hand out an unverifiable flow.
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    query = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{urlencode(query)}"}


@router.get("/callback")
# Sync for the same reason as /start: this one also commits to Postgres.
def google_auth_callback(
    code: str | None = None, state: str | None = None, db: Session = Depends(get_db)
) -> dict:
    # Verify state before anything else -- a callback we can't tie to a /start
    # we issued is a forgery until proven otherwise.
    if not state:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
    try:
        state_ok = _consume_state(state)
    except redis.RedisError as exc:
        # Redis down means we can't verify the state, so fail closed.
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    if not state_ok:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    if not settings.google_client_id or not settings.google_client_secret or not settings.google_redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth config is missing.")

    with httpx.Client(timeout=20.0) as client:
        token_resp = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            # Google's error bodies can include our client_id and other config
            # detail -- keep them in the server log, not the response.
            logger.warning(
                "Google token exchange failed (%s): %s", token_resp.status_code, token_resp.text
            )
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
        token_json = token_resp.json()

        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")
        expires_in = token_json.get("expires_in")

        profile_resp = client.get(
            GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_resp.status_code >= 400:
            logger.warning(
                "Gmail profile fetch failed (%s): %s", profile_resp.status_code, profile_resp.text
            )
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
        profile = profile_resp.json()
        external_user_id = profile.get("emailAddress") or profile.get("email") or "unknown"

    # Identity comes from the Google account: find the AppUser by that email, or
    # create one on first sign-in. This is the real (passwordless) login path.
    email = external_user_id.lower()
    user = db.query(AppUser).filter(AppUser.email == email).first()
    if not user:
        user = AppUser(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)

    token_expiry = None
    if expires_in:
        token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    existing = (
        db.query(ProviderAccount)
        .filter(
            ProviderAccount.user_id == user.id,
            ProviderAccount.provider == "gmail",
            ProviderAccount.external_user_id == external_user_id,
        )
        .first()
    )
    if existing:
        existing.access_token = access_token
        if refresh_token:
            existing.refresh_token = refresh_token
        existing.token_expiry = token_expiry
        existing.scope = " ".join(SCOPES)
        db.commit()
    else:
        db.add(
            ProviderAccount(
                user_id=user.id,
                provider="gmail",
                external_user_id=external_user_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=token_expiry,
                scope=" ".join(SCOPES),
            )
        )
        db.commit()

    # The SPA only needs the session token and who it belongs to; provider
    # linkage details stay server-side.
    return {
        "access_token": create_access_token(str(user.id), user.token_version),
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email},
    }
