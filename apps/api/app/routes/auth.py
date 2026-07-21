from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import rate_limit, user_rate_limit
from app.core.security import create_access_token
from app.deps import get_db, get_current_user
from app.db.models import AppUser, ProviderAccount
from app.db.schemas.auth import Connections, Providers, RevokeOut, TokenOut, UserOut

router = APIRouter()

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_REVOKE_TIMEOUT_SECONDS = 5


class DemoLoginRequest(BaseModel):
    email: EmailStr
    display_name: str | None = None


@router.get("/providers", response_model=Providers)
def list_providers() -> dict:
    # Only what actually works. Outlook is in the DB check constraint and
    # nowhere else -- advertising it here just sets callers up to fail.
    return {"providers": ["gmail"]}


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
    return {
        "connections": [
            {
                "id": str(conn.id),
                "provider": conn.provider,
                "created_at": conn.created_at,
                "email_address": conn.external_user_id,
                "reauth_required": conn.sync_paused_at is not None,
            }
            for conn in connections
        ]
    }


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
    if account.refresh_token:
        _revoke_google_token(account.refresh_token)
    db.delete(account)
    db.commit()
    return Response(status_code=204)
