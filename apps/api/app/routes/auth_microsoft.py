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
from app.services.ingest.outlook_client import OutlookClient

router = APIRouter()

MS_AUTH_BASE = f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MS_SCOPES = "openid profile email offline_access User.Read Mail.ReadWrite"

# Own Redis key prefix (not shared with auth_google's) so a state token minted
# by one provider flow can never be mistaken for the other's, however
# vanishingly unlikely a collision between two token_urlsafe(16) values is.
_STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "oauth:ms:state:"
_REDIS_TIMEOUT_SECONDS = 5

_redis_client: redis.Redis | None = None


def _state_store() -> redis.Redis:
    """Build the Redis client on first use so importing this module never
    needs a live Redis (mirrors auth_google's lazy pattern)."""
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


def _require_configured() -> None:
    """Outlook OAuth is optional (deploy-gate decision, config.py) -- every
    route fails closed with a clear 503 rather than a confusing downstream
    error against an unconfigured Microsoft app."""
    if not settings.microsoft_oauth_enabled:
        raise HTTPException(status_code=503, detail="microsoft oauth not configured")


def _build_consent_url(state: str, pkce_verifier: str) -> str:
    if not settings.microsoft_client_id or not settings.microsoft_redirect_uri:
        raise HTTPException(status_code=500, detail="Microsoft OAuth config is missing.")
    query = {
        "client_id": settings.microsoft_client_id,
        "redirect_uri": settings.microsoft_redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": MS_SCOPES,
        "state": state,
        "code_challenge": _pkce_challenge(pkce_verifier),
        "code_challenge_method": "S256",
    }
    return f"{MS_AUTH_BASE}/authorize?{urlencode(query)}"


def _token_expiry(expires_in: object) -> datetime | None:
    if expires_in:
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return None


def _decode_id_token_claims(id_token: str) -> dict:
    """Decode a JWT's payload segment without verifying its signature.

    Safe here because the id_token arrived directly from the Microsoft token
    endpoint over TLS -- the same trust boundary as the access_token sitting
    right next to it in the same response.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed id_token")
    segment = parts[1]
    padded = segment + "=" * (-len(segment) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("id_token payload is not an object")
    return claims


def _exchange_code(
    code: str, pkce_verifier: str
) -> tuple[str, str, str | None, datetime | None, str | None, str]:
    """Exchange a Microsoft code for tokens plus the stable outlook identity.

    Returns (external_user_id, access_token, refresh_token, token_expiry,
    granted_scope, display_email) -- external_user_id is `f"{tid}:{oid}"`
    from the ID token's claims (Graph's `mail` is mutable and must never be
    the identity); display_email is Graph `/me`'s `mail or
    userPrincipalName`, normalized.
    """
    if (
        not settings.microsoft_client_id
        or not settings.microsoft_client_secret
        or not settings.microsoft_redirect_uri
    ):
        raise HTTPException(status_code=500, detail="Microsoft OAuth config is missing.")

    with httpx.Client(timeout=20.0) as client:
        token_resp = client.post(
            f"{MS_AUTH_BASE}/token",
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "redirect_uri": settings.microsoft_redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": pkce_verifier,
                "scope": MS_SCOPES,
            },
        )
        if token_resp.status_code >= 400:
            # Microsoft's error bodies can echo our client_id and other config
            # detail -- keep them in the server log, not the response.
            logger.warning(
                "Microsoft token exchange failed (%s): %s",
                token_resp.status_code,
                token_resp.text,
            )
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        token_json = token_resp.json()

        access_token = token_json.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.warning("Microsoft token exchange returned no access token")
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        refresh_token = token_json.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None
        granted_scope = token_json.get("scope")
        if not isinstance(granted_scope, str):
            granted_scope = None

        id_token = token_json.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            logger.warning("Microsoft token exchange returned no id_token")
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        try:
            claims = _decode_id_token_claims(id_token)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Microsoft id_token decode failed: %s", type(exc).__name__)
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        tid = claims.get("tid")
        oid = claims.get("oid")
        if not tid or not oid:
            logger.warning("Microsoft id_token is missing tid/oid claims")
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        external_user_id = f"{tid}:{oid}"

        try:
            profile = OutlookClient(access_token).get_me()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Microsoft profile fetch failed (%s): %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        raw_email = profile.get("mail") or profile.get("userPrincipalName")
        if not isinstance(raw_email, str) or not raw_email.strip():
            logger.warning("Microsoft profile fetch returned no email address")
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")

    return (
        external_user_id,
        access_token,
        refresh_token,
        _token_expiry(token_json.get("expires_in")),
        granted_scope,
        normalize_email(raw_email),
    )


def _upsert_outlook_account(
    db: Session,
    user: AppUser,
    external_user_id: str,
    access_token: str,
    refresh_token: str | None,
    token_expiry: datetime | None,
    granted_scope: str | None,
    display_email: str,
) -> ProviderAccount:
    """Update the linked Outlook account or stage its first insert for commit.

    Mirrors `_upsert_gmail_account`. Deliberately does not seed
    `outlook_delta_cursors` -- the first ingest run establishes the baseline
    generation for each folder.
    """
    if not external_user_id:
        logger.warning("Refusing to store Outlook account without an external user id")
        raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
    existing = (
        db.query(ProviderAccount)
        .filter(
            ProviderAccount.user_id == user.id,
            ProviderAccount.provider == "outlook",
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
        existing.display_email = display_email
        return existing

    account = ProviderAccount(
        user_id=user.id,
        provider="outlook",
        external_user_id=external_user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        scope=granted_scope,
        display_email=display_email,
    )
    db.add(account)
    return account


def _invalid_state() -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid or expired OAuth state.")


def _find_user_by_email(db: Session, email: str) -> AppUser | None:
    """Use the database's lower(email) uniqueness rule for legacy rows too."""
    return db.query(AppUser).filter(func.lower(AppUser.email) == email).first()


def _find_outlook_account(db: Session, external_user_id: str) -> ProviderAccount | None:
    return (
        db.query(ProviderAccount)
        .filter(
            ProviderAccount.provider == "outlook",
            ProviderAccount.external_user_id == external_user_id,
        )
        .first()
    )


@router.get("/start", response_model=AuthUrl)
# Sync on purpose, mirroring auth_google: Redis here is blocking, so an async
# handler would stall every other request on the event loop for its duration.
def microsoft_auth_start() -> dict:
    """Begin Microsoft sign-in with a one-time, PKCE-bound state nonce."""
    _require_configured()
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
def microsoft_auth_callback(
    code: str | None = None, state: str | None = None, db: Session = Depends(get_db)
) -> dict:
    _require_configured()
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

    (
        external_user_id,
        access_token,
        refresh_token,
        token_expiry,
        granted_scope,
        display_email,
    ) = _exchange_code(code, pkce_verifier)

    # The user and provider link deliberately commit together: a failed provider
    # insert must not leave an otherwise unusable passwordless account behind.
    for attempt in range(2):
        # A tid:oid match to an already-connected account is authoritative --
        # its owner logs in even if their outlook mail differs from the
        # account's original login email; the email is never consulted once
        # the identity is linked.
        existing_account = _find_outlook_account(db, external_user_id)
        if existing_account is not None:
            user = db.query(AppUser).filter(AppUser.id == existing_account.user_id).first()
            if user is None:  # pragma: no cover - FK guarantees this can't happen
                logger.warning("Outlook account referenced a missing user")
                raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
        else:
            # Graph's mail/userPrincipalName claim is tenant-admin-controlled
            # and unverified -- Microsoft documents it as mutable, and an
            # attacker who controls their own Entra tenant can set it to any
            # address (the "nOAuth" account-takeover class). An unlinked
            # identity must never log into -- or silently link -- an existing
            # account on the strength of that claim; the authenticated
            # /connect flow is the only way to attach Outlook to one.
            if _find_user_by_email(db, display_email) is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "An account with that email already exists. Sign in with your "
                        "existing method, then connect Outlook from the accounts menu."
                    ),
                )
            user = AppUser(email=display_email)
            db.add(user)
            # Deliberately left unverified: unlike Google's email claim,
            # Microsoft's mail/UPN claim isn't authoritative, so a fresh
            # outlook-login account starts exactly as unverified as any other
            # signup without a proven address.
        try:
            _upsert_outlook_account(
                db,
                user,
                external_user_id,
                access_token,
                refresh_token,
                token_expiry,
                granted_scope,
                display_email,
            )
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            logger.warning("Microsoft account upsert failed: %s", type(exc).__name__)
            # Retry once, but only if the identity itself won a genuine
            # same-login race (a concurrent request for this exact tid:oid
            # already committed its own user+account pair) -- never fall
            # back to linking an existing user by email here, which is
            # exactly the hole this flow guards against.
            if attempt == 0 and _find_outlook_account(db, external_user_id) is not None:
                continue
            raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")
    else:  # pragma: no cover - the loop either commits or raises above.
        raise HTTPException(status_code=400, detail="Microsoft sign-in failed; try again.")

    # The SPA only needs the session token and who it belongs to; provider
    # linkage details stay server-side.
    return {
        "access_token": create_access_token(str(user.id), user.token_version),
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email},
    }


def _outlook_belongs_to_other_user(db: Session, user: AppUser, display_email: str) -> bool:
    owner = _find_user_by_email(db, display_email)
    return owner is not None and owner.id != user.id


def _outlook_account_belongs_to_other_user(
    db: Session, user: AppUser, external_user_id: str
) -> bool:
    account = _find_outlook_account(db, external_user_id)
    return account is not None and account.user_id != user.id


def _connect_conflict(
    db: Session, user: AppUser, display_email: str, external_user_id: str
) -> HTTPException | None:
    if _outlook_belongs_to_other_user(
        db, user, display_email
    ) or _outlook_account_belongs_to_other_user(db, user, external_user_id):
        return HTTPException(
            status_code=409,
            detail=(
                "That Outlook account belongs to a different account — "
                "sign in with Microsoft instead."
            ),
        )
    return None


@router.get(
    "/connect/start",
    response_model=AuthUrl,
    dependencies=[Depends(user_rate_limit("outlook-connect", 10, 600))],
)
def outlook_connect_start(current_user: AppUser = Depends(get_current_user)) -> dict:
    """Begin an authenticated Outlook connection with a user-bound state nonce."""
    _require_configured()
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
def outlook_connect_callback(
    code: str | None = None,
    state: str | None = None,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_configured()
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

    (
        external_user_id,
        access_token,
        refresh_token,
        token_expiry,
        granted_scope,
        display_email,
    ) = _exchange_code(code, pkce_verifier)
    conflict = _connect_conflict(db, current_user, display_email, external_user_id)
    if conflict:
        raise conflict
    _upsert_outlook_account(
        db,
        current_user,
        external_user_id,
        access_token,
        refresh_token,
        token_expiry,
        granted_scope,
        display_email,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("Outlook connect upsert failed: %s", type(exc).__name__)
        conflict = _connect_conflict(db, current_user, display_email, external_user_id)
        if conflict:
            raise conflict
        # The only uniqueness constraint an insert can still lose the race on
        # is cross-user (uq_provider_account_provider_external_user). A
        # concurrent winner can be invisible to a lightweight test double, so
        # report the cross-user conflict even if the recheck above came up
        # empty.
        raise HTTPException(
            status_code=409,
            detail=(
                "That Outlook account belongs to a different account — "
                "sign in with Microsoft instead."
            ),
        )

    return {"status": "connected", "provider_email": display_email}
