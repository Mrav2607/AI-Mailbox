from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.ratelimit import rate_limit
from app.core.security import create_access_token
from app.deps import get_db, get_current_user
from app.db.models import AppUser, ProviderAccount

router = APIRouter()


class DemoLoginRequest(BaseModel):
    email: EmailStr
    display_name: str | None = None


@router.get("/providers")
async def list_providers() -> dict:
    return {"providers": ["gmail", "outlook"]}


@router.get("/me")
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
@router.post("/demo-login", dependencies=[Depends(rate_limit("demo-login", 10, 60))])
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
        "access_token": create_access_token(str(user.id)),
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


@router.get("/connections")
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
            }
            for conn in connections
        ]
    }
