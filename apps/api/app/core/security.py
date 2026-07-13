"""Session-token helpers: issue and verify HS256 JWTs for authenticated users.

A token's ``sub`` claim holds the AppUser id, and every token carries our
``iss``/``aud`` claims so a token minted by (or for) some other service can't
be replayed against this API. Verification requires ``sub``/``exp``/``iat`` to
be present, so a stripped-down token can't dodge expiry checks. Tokens are
signed with ``settings.api_secret`` -- keep it secret in production, since
anyone holding it can mint valid tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings

# Who mints the token and who it's for. Both are checked on decode, so tokens
# from another deployment (or another app sharing the secret) get rejected.
JWT_ISSUER = "ai-mailbox-api"
JWT_AUDIENCE = "ai-mailbox"


def create_access_token(
    subject: str, token_version: int, expires_minutes: int | None = None
) -> str:
    """Issue a signed token whose ``sub`` is ``subject`` (the AppUser id).

    ``token_version`` is required on purpose -- pass the user's current
    ``AppUser.token_version``. If it had a default, a new issuer could quietly
    mint version-0 tokens that survive every revocation.
    """
    now = datetime.now(timezone.utc)
    minutes = (
        expires_minutes
        if expires_minutes is not None
        else settings.access_token_expires_minutes
    )
    payload = {
        "sub": subject,
        "tv": token_version,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.api_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Verify a token and return its claims. Raises ``jwt.PyJWTError`` on an
    invalid signature, a wrong issuer/audience, missing required claims, or an
    expired/malformed token.

    ``tv`` is required, so a token minted before revocation existed is rejected
    rather than treated as version 0 -- everyone re-signs-in once. Callers still
    have to compare it against the user's current version; this only guarantees
    the claim is present and signed.
    """
    return jwt.decode(
        token,
        settings.api_secret,
        algorithms=[settings.jwt_algorithm],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        options={"require": ["sub", "exp", "iat", "tv"]},
    )
