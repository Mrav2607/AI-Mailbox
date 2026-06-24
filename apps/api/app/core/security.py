"""Session-token helpers: issue and verify HS256 JWTs for authenticated users.

A token's ``sub`` claim holds the AppUser id. Tokens are signed with
``settings.api_secret`` -- keep it secret in production, since anyone holding it
can mint valid tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    """Issue a signed token whose ``sub`` is ``subject`` (the AppUser id)."""
    now = datetime.now(timezone.utc)
    minutes = (
        expires_minutes
        if expires_minutes is not None
        else settings.access_token_expires_minutes
    )
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.api_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Verify a token and return its claims. Raises ``jwt.PyJWTError`` on an
    invalid signature or an expired/malformed token."""
    return jwt.decode(token, settings.api_secret, algorithms=[settings.jwt_algorithm])
