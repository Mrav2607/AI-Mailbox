"""Issue and atomically consume short-lived email-authentication tokens."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func

from app.core.security import normalize_email
from app.db.models import AuthToken

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session


VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(minutes=30)


def _token_hash(raw_token: str) -> str:
    """Hash the plaintext token before it can enter database state."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_token(
    db: "Session",
    *,
    purpose: str,
    email: str,
    user_id: "UUID | None" = None,
    pending_password_hash: str | None = None,
    display_name: str | None = None,
    ttl: timedelta,
) -> str:
    """Create a token and return its plaintext exactly once.

    Reissuing invalidates an earlier token for this email and purpose. This
    only flushes; the caller owns the surrounding transaction and commit.
    """
    normalized_email = normalize_email(email)
    raw_token = secrets.token_urlsafe(32)
    db.execute(
        delete(AuthToken).where(
            AuthToken.email == normalized_email,
            AuthToken.purpose == purpose,
        )
    )
    db.execute(delete(AuthToken).where(AuthToken.expires_at < func.now()))
    db.add(
        AuthToken(
            purpose=purpose,
            email=normalized_email,
            user_id=user_id,
            pending_password_hash=pending_password_hash,
            display_name=display_name,
            token_hash=_token_hash(raw_token),
            expires_at=func.now() + ttl,
        )
    )
    db.flush()
    return raw_token


def consume_token(
    db: "Session", *, purpose: str, raw_token: str
) -> AuthToken | None:
    """Atomically delete and return one live token, or ``None`` for any failure.

    Unknown, expired, wrong-purpose, and already-consumed tokens intentionally
    collapse to the same result. The caller owns committing the deletion.
    """
    statement = (
        delete(AuthToken)
        .where(
            AuthToken.token_hash == _token_hash(raw_token),
            AuthToken.purpose == purpose,
            AuthToken.expires_at > func.now(),
        )
        .returning(AuthToken)
    )
    return db.execute(statement).scalars().one_or_none()
