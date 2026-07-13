from collections.abc import Generator
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .core.security import decode_access_token
from .db.base import SessionLocal
from .db.models import AppUser


def get_db() -> Generator:
    """
    FastAPI dependency that provides a SQLAlchemy session per request.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# auto_error=False so we can raise our own 401 (HTTPBearer defaults to 403).
_bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AppUser:
    """Resolve the authenticated AppUser from a ``Bearer`` session token.

    Raises 401 if the token is missing, invalid, expired, revoked, or no longer
    maps to a real user. Routes depend on this instead of trusting a ``user_id``
    param.
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = UUID(payload["sub"])
        token_version = int(payload["tv"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise _UNAUTHENTICATED from exc

    user = db.get(AppUser, user_id)
    if user is None:
        raise _UNAUTHENTICATED

    # The revocation check, and it's free: we already had to load the user. A
    # token minted against an older version was issued before someone hit
    # revoke-all, so it's dead no matter how valid its signature is.
    if token_version != user.token_version:
        raise _UNAUTHENTICATED

    return user
