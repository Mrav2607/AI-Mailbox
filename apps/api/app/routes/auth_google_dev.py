from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
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


@router.get("/start")
async def google_dev_auth_start() -> dict:
    """Begin Google sign-in. No user is required up front -- the user is
    identified (and created on first sign-in) from their Google email in the
    callback. ``state`` is a random nonce; full CSRF verification needs a
    server-side store, which is a follow-up once sessions exist."""
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
        "state": secrets.token_urlsafe(16),
    }
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{urlencode(query)}"}


@router.get("/callback")
async def google_dev_auth_callback(
    code: str | None = None, state: str | None = None, db: Session = Depends(get_db)
) -> dict:
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    if not settings.google_client_id or not settings.google_client_secret or not settings.google_redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth config is missing.")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
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
            raise HTTPException(status_code=400, detail=token_resp.text)
        token_json = token_resp.json()

        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")
        expires_in = token_json.get("expires_in")

        profile_resp = await client.get(
            GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_resp.status_code >= 400:
            raise HTTPException(status_code=400, detail=profile_resp.text)
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
        db.refresh(existing)
        provider = existing
    else:
        provider = ProviderAccount(
            user_id=user.id,
            provider="gmail",
            external_user_id=external_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            scope=" ".join(SCOPES),
        )
        db.add(provider)
        db.commit()
        db.refresh(provider)

    return {
        "access_token": create_access_token(str(user.id)),
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email},
        "provider_account": {
            "id": str(provider.id),
            "user_id": str(provider.user_id),
            "provider": provider.provider,
            "external_user_id": provider.external_user_id,
            "token_expiry": provider.token_expiry,
        },
    }
