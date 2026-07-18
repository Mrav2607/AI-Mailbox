from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import user_rate_limit
from app.core.security import create_access_token, normalize_email
from app.db.models import AppUser, ProviderAccount
from app.db.schemas.auth import AuthUrl, ConnectResult, TokenOut
from app.deps import get_current_user, get_db

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


def _store_state(state: str, payload: dict[str, str]) -> None:
    _state_store().set(
        f"{_STATE_KEY_PREFIX}{state}", json.dumps(payload), ex=_STATE_TTL_SECONDS
    )


def _consume_state(state: str) -> dict[str, str] | None:
    """One-time use: GETDEL so a replayed state can't pass twice."""
    value = _state_store().getdel(f"{_STATE_KEY_PREFIX}{state}")
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        payload = json.loads(value)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in payload.items()
    ):
        return None
    return payload


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_consent_url(state: str, pkce_verifier: str) -> str:
    if not settings.google_client_id or not settings.google_redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth config is missing.")
    query = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": _pkce_challenge(pkce_verifier),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"


def _token_expiry(expires_in: object) -> datetime | None:
    if expires_in:
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return None


def _exchange_code(
    code: str, pkce_verifier: str
) -> tuple[str, str, str | None, datetime | None, str | None]:
    """Exchange a Google code and return the Gmail identity plus token data."""
    if (
        not settings.google_client_id
        or not settings.google_client_secret
        or not settings.google_redirect_uri
    ):
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
                "code_verifier": pkce_verifier,
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
        if not isinstance(access_token, str) or not access_token:
            logger.warning("Google token exchange returned no access token")
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
        refresh_token = token_json.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None
        granted_scope = token_json.get("scope")
        if not isinstance(granted_scope, str):
            granted_scope = None

        profile_resp = client.get(
            GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_resp.status_code >= 400:
            logger.warning(
                "Gmail profile fetch failed (%s): %s", profile_resp.status_code, profile_resp.text
            )
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
        profile = profile_resp.json()
        profile_email = profile.get("emailAddress")
        if not isinstance(profile_email, str) or not profile_email.strip():
            logger.warning("Gmail profile fetch returned no email address")
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")

    return (
        normalize_email(profile_email),
        access_token,
        refresh_token,
        _token_expiry(token_json.get("expires_in")),
        granted_scope,
    )


def _upsert_gmail_account(
    db: Session,
    user: AppUser,
    external_user_id: str,
    access_token: str,
    refresh_token: str | None,
    token_expiry: datetime | None,
    granted_scope: str | None,
) -> ProviderAccount:
    """Update the linked Gmail account or stage its first insert for commit."""
    if not external_user_id:
        logger.warning("Refusing to store Gmail account without an external user id")
        raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
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
            # A paused account has a dead refresh token. Only a replacement can
            # prove it is safe to schedule this mailbox again.
            existing.sync_paused_at = None
            existing.sync_pause_reason = None
        existing.token_expiry = token_expiry
        if granted_scope:
            existing.scope = granted_scope
        return existing

    account = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id=external_user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        scope=granted_scope,
    )
    db.add(account)
    return account


def _invalid_state() -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid or expired OAuth state.")


def _find_user_by_email(db: Session, email: str) -> AppUser | None:
    """Use the database's lower(email) uniqueness rule for legacy rows too."""
    return db.query(AppUser).filter(func.lower(AppUser.email) == email).first()


@router.get("/start", response_model=AuthUrl)
# Sync on purpose. Redis and SQLAlchemy here are both blocking, so an async
# handler would run them straight on the event loop and stall every other
# request (and the health probes) for the duration. A plain def gets handed to
# FastAPI's threadpool instead.
def google_auth_start() -> dict:
    """Begin Google sign-in with a one-time, PKCE-bound state nonce."""
    state = secrets.token_urlsafe(16)
    pkce_verifier = secrets.token_urlsafe(64)
    auth_url = _build_consent_url(state, pkce_verifier)
    try:
        _store_state(state, {"mode": "login", "pkce_verifier": pkce_verifier})
    except redis.RedisError as exc:
        # Can't remember the nonce, so we can't verify the callback later.
        # Fail closed rather than hand out an unverifiable flow.
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    return {"auth_url": auth_url}


@router.get("/callback", response_model=TokenOut)
# Sync for the same reason as /start: this one also commits to Postgres.
def google_auth_callback(
    code: str | None = None, state: str | None = None, db: Session = Depends(get_db)
) -> dict:
    # Verify state before anything else -- a callback we can't tie to a /start
    # we issued is a forgery until proven otherwise.
    if not state:
        raise _invalid_state()
    try:
        state_payload = _consume_state(state)
    except redis.RedisError as exc:
        # Redis down means we can't verify the state, so fail closed.
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    if not state_payload or state_payload.get("mode") != "login":
        raise _invalid_state()
    pkce_verifier = state_payload.get("pkce_verifier")
    if not pkce_verifier:
        raise _invalid_state()
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    external_user_id, access_token, refresh_token, token_expiry, granted_scope = _exchange_code(
        code, pkce_verifier
    )
    email = normalize_email(external_user_id)

    # The user and provider link deliberately commit together: a failed provider
    # insert must not leave an otherwise unusable passwordless account behind.
    for attempt in range(2):
        user = _find_user_by_email(db, email)
        created_user = user is None
        if user is None:
            user = AppUser(email=email)
            db.add(user)
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(timezone.utc)
        try:
            _upsert_gmail_account(
                db,
                user,
                external_user_id,
                access_token,
                refresh_token,
                token_expiry,
                granted_scope,
            )
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            logger.warning("Google account upsert failed: %s", type(exc).__name__)
            # A case-insensitive AppUser insert can race another login. After a
            # rollback, requery once and use that winner; provider races remain
            # deliberately generic to avoid exposing linkage details.
            if created_user and attempt == 0:
                raced_user = _find_user_by_email(db, email)
                if raced_user is not None:
                    continue
            raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")
    else:  # pragma: no cover - the loop either commits or raises above.
        raise HTTPException(status_code=400, detail="Google sign-in failed; try again.")

    # The SPA only needs the session token and who it belongs to; provider
    # linkage details stay server-side.
    return {
        "access_token": create_access_token(str(user.id), user.token_version),
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email},
    }


def _gmail_belongs_to_other_user(db: Session, user: AppUser, email: str) -> bool:
    owner = _find_user_by_email(db, email)
    return owner is not None and owner.id != user.id


def _has_different_gmail_account(db: Session, user: AppUser, external_user_id: str) -> bool:
    account = (
        db.query(ProviderAccount)
        .filter(ProviderAccount.user_id == user.id, ProviderAccount.provider == "gmail")
        .first()
    )
    return account is not None and account.external_user_id != external_user_id


def _gmail_account_belongs_to_other_user(
    db: Session, user: AppUser, external_user_id: str
) -> bool:
    account = (
        db.query(ProviderAccount)
        .filter(
            ProviderAccount.provider == "gmail",
            ProviderAccount.external_user_id == external_user_id,
        )
        .first()
    )
    return account is not None and account.user_id != user.id


def _connect_conflict(db: Session, user: AppUser, email: str) -> HTTPException | None:
    if _gmail_belongs_to_other_user(
        db, user, email
    ) or _gmail_account_belongs_to_other_user(db, user, email):
        return HTTPException(
            status_code=409,
            detail=(
                "That Gmail account belongs to a different account — sign in with Google instead."
            ),
        )
    if _has_different_gmail_account(db, user, email):
        return HTTPException(
            status_code=409, detail="A different Gmail account is already connected."
        )
    return None


@router.get(
    "/connect/start",
    response_model=AuthUrl,
    dependencies=[Depends(user_rate_limit("gmail-connect", 10, 600))],
)
def gmail_connect_start(current_user: AppUser = Depends(get_current_user)) -> dict:
    """Begin an authenticated Gmail connection with a user-bound state nonce."""
    state = secrets.token_urlsafe(16)
    pkce_verifier = secrets.token_urlsafe(64)
    auth_url = _build_consent_url(state, pkce_verifier)
    try:
        _store_state(
            state,
            {
                "mode": "connect",
                "user_id": str(current_user.id),
                "pkce_verifier": pkce_verifier,
            },
        )
    except redis.RedisError as exc:
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    return {"auth_url": auth_url}


@router.get("/connect/callback", response_model=ConnectResult)
def gmail_connect_callback(
    code: str | None = None,
    state: str | None = None,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not state:
        raise _invalid_state()
    try:
        state_payload = _consume_state(state)
    except redis.RedisError as exc:
        logger.warning("OAuth state store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")
    if (
        not state_payload
        or state_payload.get("mode") != "connect"
        or state_payload.get("user_id") != str(current_user.id)
    ):
        raise _invalid_state()
    pkce_verifier = state_payload.get("pkce_verifier")
    if not pkce_verifier:
        raise _invalid_state()
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    external_user_id, access_token, refresh_token, token_expiry, granted_scope = _exchange_code(
        code, pkce_verifier
    )
    conflict = _connect_conflict(db, current_user, external_user_id)
    if conflict:
        raise conflict
    _upsert_gmail_account(
        db,
        current_user,
        external_user_id,
        access_token,
        refresh_token,
        token_expiry,
        granted_scope,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("Gmail connect upsert failed: %s", type(exc).__name__)
        conflict = _connect_conflict(db, current_user, external_user_id)
        if conflict:
            raise conflict
        # A concurrent insert can be invisible to a lightweight test double;
        # it is still one of the two provider uniqueness constraints in the DB.
        raise HTTPException(
            status_code=409, detail="A different Gmail account is already connected."
        )

    return {"status": "connected", "provider_email": external_user_id}
