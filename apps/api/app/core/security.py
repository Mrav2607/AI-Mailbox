"""Session-token helpers: issue and verify HS256 JWTs for authenticated users.

A token's ``sub`` claim holds the AppUser id, and every token carries our
``iss``/``aud`` claims so a token minted by (or for) some other service can't
be replayed against this API. Verification requires ``sub``/``exp``/``iat`` to
be present, so a stripped-down token can't dodge expiry checks. Tokens are
signed with ``settings.api_secret`` -- keep it secret in production, since
anyone holding it can mint valid tokens.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from app.core.config import settings

# Who mints the token and who it's for. Both are checked on decode, so tokens
# from another deployment (or another app sharing the secret) get rejected.
JWT_ISSUER = "ai-mailbox-api"
JWT_AUDIENCE = "ai-mailbox"

# These are argon2-cffi 23.1's PasswordHasher defaults, pinned so changing the
# library cannot quietly weaken newly-issued password hashes.
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    encoding="utf-8",
    type=Type.ID,
)
# Missing-password verification must cost a real Argon2 check too, otherwise
# login timing reveals whether an address has a password. The dummy plaintext
# is random per process: a fixed string here would be a skeleton key, since
# anyone reading this source could submit it and have the dummy verify pass.
_DUMMY_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def normalize_email(email: str) -> str:
    """Return the canonical address form used for every auth lookup.

    Whitespace around an address is accidental user input, while casefolding
    makes comparisons robust for the Unicode forms Python can represent.
    """
    return email.strip().casefold()


def hash_password(password: str) -> str:
    """Hash ``password`` with the pinned Argon2id parameters."""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password, using a dummy hash when no stored hash exists.

    Malformed or incompatible hashes are authentication failures, never errors
    exposed to a caller. The dummy path performs a real verify for timing
    parity but can never authenticate -- there is no stored password to match.
    """
    if password_hash is None:
        try:
            _PASSWORD_HASHER.verify(_DUMMY_HASH, password)
        except (InvalidHashError, VerificationError):
            pass
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Say whether a successfully verified hash needs the current parameters."""
    return _PASSWORD_HASHER.check_needs_rehash(password_hash)


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
