from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.deps import get_db
from app.db.models import AppUser, ProviderAccount

router = APIRouter()


class DemoLoginRequest(BaseModel):
    email: EmailStr
    display_name: str | None = None


@router.get("/providers")
async def list_providers() -> dict:
    return {"providers": ["gmail", "outlook"]}


@router.post("/demo-login")
def demo_login(payload: DemoLoginRequest, db: Session = Depends(get_db)) -> dict:
    """
    Minimal user bootstrap for local dev. Creates the user record if missing.
    """
    email = payload.email.lower()
    user = db.query(AppUser).filter(AppUser.email == email).first()
    if not user:
        user = AppUser(email=email, display_name=payload.display_name)
        db.add(user)
        db.commit()
        db.refresh(user)
    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        }
    }


@router.get("/{user_id}/connections")
def list_connections(user_id: UUID, db: Session = Depends(get_db)) -> dict:
    connections = (
        db.query(ProviderAccount)
        .filter(ProviderAccount.user_id == user_id)
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
