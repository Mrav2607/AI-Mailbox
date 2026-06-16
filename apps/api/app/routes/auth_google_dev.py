from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
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
async def google_dev_auth_start(user_id: UUID) -> dict:
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
        "state": str(user_id),
    }
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{urlencode(query)}"}


@router.get("/callback")
async def google_dev_auth_callback(
    code: str | None = None, state: str | None = None, db: Session = Depends(get_db)
) -> dict:
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")
    if not settings.google_client_id or not settings.google_client_secret or not settings.google_redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth config is missing.")

    try:
        user_id = UUID(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid state.") from exc

    user = db.get(AppUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

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

    token_expiry = None
    if expires_in:
        token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    existing = (
        db.query(ProviderAccount)
        .filter(
            ProviderAccount.user_id == user_id,
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
            user_id=user_id,
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
        "provider_account": {
            "id": str(provider.id),
            "user_id": str(provider.user_id),
            "provider": provider.provider,
            "external_user_id": provider.external_user_id,
            "token_expiry": provider.token_expiry,
        }
    }
