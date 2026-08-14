from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import rate_limit, user_rate_limit
from app.core.security import create_access_token
from app.deps import get_db, get_current_user
from app.db.models import AppUser, MailThread, ProviderAccount
from app.db.schemas.auth import (
    Connections,
    ConnectionOut,
    Providers,
    RevokeOut,
    TokenOut,
    UpdateConnectionRequest,
    UserOut,
)
from app.scripts.seed_demo import seed_demo_data
from app.services.label_sync import (
    CLAIM_LEASE,
    GMAIL_MODIFY_SCOPE,
    OUTLOOK_REQUIRED_SCOPE_TOKEN,
    account_drift_count,
    acquire_account_lock,
)
from app.services.nlp.backfill import latest_label_subquery

router = APIRouter()

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_REVOKE_TIMEOUT_SECONDS = 5

# ENABLE-time failure copy (plan §3.3) -- the label_sync_busy text is the
# frozen exact wording; the others are ours, same style as reply.py's
# _http_error messages.
_ENABLE_FAILURE_MESSAGES: dict[str, str] = {
    "missing_scope": "This account needs to be reconnected to grant label-sync permission.",
    "reauth_required": "This account needs to be reconnected before label sync can turn on.",
    "account_paused": (
        "This account is paused and needs to be reconnected before label sync can turn on."
    ),
    "label_sync_busy": "a sync is finishing — try again in a few minutes",
}


class DemoLoginRequest(BaseModel):
    email: EmailStr
    display_name: str | None = None


@router.get("/providers", response_model=Providers)
def list_providers() -> dict:
    # Only what actually works. Outlook only shows up once its OAuth app
    # credentials are configured -- advertising it otherwise just sets
    # callers up to fail against a route that 503s.
    providers = ["gmail"]
    if settings.microsoft_oauth_enabled:
        providers.append("outlook")
    return {"providers": providers, "demo_login": not settings.is_production}


@router.get("/me", response_model=UserOut)
def get_me(current_user: AppUser = Depends(get_current_user)) -> dict:
    """Return the authenticated user. Lets a client validate its stored token
    and restore the session on reload (401 if the token is missing/invalid)."""
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
    }


# Per-IP limit: this route creates user rows on demand, so don't let one
# caller mint them in bulk on dev/staging instances.
@router.post(
    "/demo-login",
    response_model=TokenOut,
    dependencies=[Depends(rate_limit("demo-login", 10, 60))],
)
def demo_login(payload: DemoLoginRequest, db: Session = Depends(get_db)) -> dict:
    """
    Minimal user bootstrap for local dev. Creates the user record if missing
    and returns a session token for subsequent authenticated requests.

    DEV convenience only -- it verifies no credential, so it must not be
    exposed in production. Real sign-in goes through Google OAuth.
    """
    # Anyone reaching this route in production could mint a session for any
    # email, so refuse outright. 404 rather than 403: don't advertise that a
    # passwordless login route exists at all.
    if settings.is_production:
        raise HTTPException(status_code=404, detail="Not Found")

    email = payload.email.lower()
    user = db.query(AppUser).filter(AppUser.email == email).first()
    if not user:
        user = AppUser(email=email, display_name=payload.display_name)
        db.add(user)
        db.commit()
        db.refresh(user)
        # A brand-new demo user has no mail, so the console would open empty
        # and there'd be nothing to try. Seeding is best-effort: if it fails,
        # the operator still gets a usable session rather than a 500.
        try:
            seed_demo_data(db, user)
        except Exception:
            logger.exception("demo seed failed for %s", email)
            db.rollback()
    return {
        "access_token": create_access_token(str(user.id), user.token_version),
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


@router.post("/revoke-all", response_model=RevokeOut)
def revoke_all_tokens(
    current_user: AppUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    """Kill every session token this user holds, including the one presenting
    this request. Use it when a token leaks -- signing out only clears the
    browser's copy, which does nothing about a token someone else already has.

    The increment is a SQL expression rather than a read-then-write so two
    concurrent calls can't both read version 3 and both write 4.
    """
    db.execute(
        update(AppUser)
        .where(AppUser.id == current_user.id)
        .values(token_version=AppUser.token_version + 1)
    )
    db.commit()  # get_db never commits for us, and an uncommitted revoke is no revoke
    return {"status": "revoked"}


def _connection_row(conn: ProviderAccount, db: Session) -> dict:
    """Shared row shape for the connections list AND the PATCH echo (plan
    §3.3 -- "response mirrors the connections list row"). The drift count
    is only computed for enabled accounts (§3.4/§3.6) -- a disabled account
    has nothing converging, so there's no cheap-count query to run for it.
    """
    return {
        "id": str(conn.id),
        "provider": conn.provider,
        "created_at": conn.created_at,
        "email_address": conn.display_email or conn.external_user_id,
        "reauth_required": conn.sync_paused_at is not None,
        "label_sync_enabled": conn.label_sync_enabled,
        "label_sync_drift": (
            account_drift_count(db, conn.id) if conn.label_sync_enabled else None
        ),
    }


@router.get("/connections", response_model=Connections)
def list_connections(
    current_user: AppUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    connections = (
        db.query(ProviderAccount)
        .filter(ProviderAccount.user_id == current_user.id)
        .order_by(ProviderAccount.created_at.desc())
        .all()
    )
    return {"connections": [_connection_row(conn, db) for conn in connections]}


def _scope_tokens(raw: str | None) -> set[str]:
    return {tok.lower() for tok in (raw or "").split() if tok}


def _enable_failure_code(account: ProviderAccount) -> str | None:
    """The ENABLE-time failure contract's scope/refresh/pause checks (plan
    §3.3), in the plan's listed order. Deliberately NOT
    `label_sync_execution_eligible` (app/services/label_sync/service.py) --
    that helper also gates on `label_sync_enabled` itself, which is still
    false here (we haven't flipped it yet), so it would always report
    "disabled" instead of checking what we actually care about.
    """
    tokens = _scope_tokens(account.scope)
    if account.provider == "gmail":
        has_scope = GMAIL_MODIFY_SCOPE.lower() in tokens
    else:
        has_scope = OUTLOOK_REQUIRED_SCOPE_TOKEN in tokens
    if not has_scope:
        return "missing_scope"
    if not account.refresh_token:
        return "reauth_required"
    if account.sync_paused_at is not None:
        return "account_paused"
    return None


def _label_sync_error(code: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"code": code, "message": _ENABLE_FAILURE_MESSAGES[code]}
    )


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: UUID,
    payload: UpdateConnectionRequest,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Flip an account's label-sync opt-in (plan §3.3).

    DISABLE (true -> false) just clears the flag -- labels already pushed
    stay on the provider as a snapshot, nothing else changes. ENABLE (false
    -> true) forces reconvergence: under the SAME per-account advisory lock
    the task claim path uses, it re-checks scope/refresh-token/pause, then
    checks for any unexpired label-sync claim on the account's threads
    (409 label_sync_busy -- a task holding an uncommitted claim would
    otherwise be invisible to this check and race the generation bump), and
    only then bumps `label_sync_generation`, clears the Gmail label-id
    cache, and marks every thread with a classification or a populated
    applied pair for resync (the applied pair itself is RETAINED -- it's
    the Outlook cleanup target). `label_sync_enabled` is never persisted
    true unless every one of those checks passed.

    A repeat of the account's CURRENT state (either direction) is a pure
    echo: no lock, no generation bump, no marker changes.
    """
    account = db.get(ProviderAccount, connection_id)
    # 404 (not 403) for another user's connection so we don't leak that it exists.
    if not account or account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")

    if payload.label_sync_enabled == account.label_sync_enabled:
        return _connection_row(account, db)

    if not payload.label_sync_enabled:
        account.label_sync_enabled = False
        db.commit()
        return _connection_row(account, db)

    # ENABLE (false -> true): acquire the shared advisory lock BEFORE any
    # check below (P7-1) -- the enable route and every claiming task agree
    # on the same key, so a task's uncommitted claim can't slip past the
    # busy check while we hold it.
    acquire_account_lock(db, account.id)

    # `account` entered the identity map on the plain db.get() above, i.e.
    # BEFORE the lock -- if ingest commits a pause (or anything else) while
    # this request was blocked on it, that plain object would keep echoing
    # the pre-lock snapshot forever. Re-read FOR UPDATE with
    # populate_existing so every check below runs against whatever's
    # actually committed now, then redo them all -- including the
    # current-state echo, since the fresh read may have changed it too (L-2).
    account = db.execute(
        select(ProviderAccount)
        .where(ProviderAccount.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if payload.label_sync_enabled == account.label_sync_enabled:
        return _connection_row(account, db)

    failure_code = _enable_failure_code(account)
    if failure_code:
        raise _label_sync_error(failure_code)

    now = datetime.now(timezone.utc)
    busy = db.execute(
        select(MailThread.id)
        .where(
            MailThread.provider_account_id == account.id,
            MailThread.label_sync_claim_token.is_not(None),
            MailThread.label_sync_claimed_at > now - CLAIM_LEASE,
        )
        .limit(1)
    ).first()
    if busy is not None:
        raise _label_sync_error("label_sync_busy")

    account.label_sync_enabled = True
    account.label_sync_generation = account.label_sync_generation + 1
    account.gmail_label_map = None

    current_label = latest_label_subquery()
    db.execute(
        update(MailThread)
        .where(
            MailThread.provider_account_id == account.id,
            or_(
                current_label.is_not(None),
                MailThread.synced_label.is_not(None),
                MailThread.synced_message_id.is_not(None),
            ),
        )
        .values(label_resync_needed=True)
    )
    db.commit()
    return _connection_row(account, db)


def _revoke_google_token(refresh_token: str) -> None:
    """Best-effort: tell Google we're done with this token. A failure here
    never blocks the delete -- the row (and the local ability to use the
    token) is gone either way, so the worst case is a token that lingers on
    Google's side until it expires on its own."""
    try:
        response = httpx.post(
            GOOGLE_REVOKE_URL,
            data={"token": refresh_token},
            timeout=_REVOKE_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.warning("Google token revocation failed (%s)", response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Google token revocation errored: %s", type(exc).__name__)


# response_model=None: a 204 carries no body, so there is nothing to validate
# and declaring a model here would be a lie in the OpenAPI schema.
@router.delete(
    "/connections/{connection_id}",
    status_code=204,
    response_model=None,
    dependencies=[Depends(user_rate_limit("disconnect", 10, 600))],
)
def delete_connection(
    connection_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Disconnect a provider account and everything hanging off it.

    The mail_thread -> provider_account and mail_sync_run -> provider_account
    foreign keys are ON DELETE CASCADE, so dropping this row takes the
    account's threads, messages, classifications, and sync runs with it.
    """
    account = db.get(ProviderAccount, connection_id)
    # 404 (not 403) for another user's connection so we don't leak that it exists.
    if not account or account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    # Outlook has no remote-revocation call wired up yet (documented
    # limitation) -- deleting the row is the only cleanup for those rows.
    if account.provider == "gmail" and account.refresh_token:
        _revoke_google_token(account.refresh_token)
    db.delete(account)
    db.commit()
    return Response(status_code=204)
