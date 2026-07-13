"""Symmetric encryption for secrets stored at rest (provider OAuth tokens).

Uses Fernet (AES-128-CBC + HMAC). The key comes from TOKEN_ENCRYPTION_KEY when
set, otherwise it's derived from API_SECRET so dev works with zero extra config
(API_SECRET is already validated as strong in production).

Decryption tolerates legacy *plaintext* values written before encryption was
introduced: a stored value that isn't a Fernet token is returned as-is and gets
re-encrypted on its next write (access tokens rotate on refresh; refresh tokens
on the next sign-in). A value that *is* a Fernet token but fails to decrypt is a
real error (wrong key) and is allowed to raise.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import logger

# Fernet's token version byte; a base64url-decoded ciphertext always starts here.
_FERNET_VERSION = 0x80
# Minimum decoded length of a Fernet token: version(1) + timestamp(8) + IV(16) +
# one cipher block(16) + HMAC(32). Used to avoid misreading a short legacy
# plaintext that happens to decode with a 0x80 first byte as ciphertext.
_FERNET_MIN_BYTES = 1 + 8 + 16 + 16 + 32


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Build the Fernet instance once.

    A configured TOKEN_ENCRYPTION_KEY must be a valid 32-byte urlsafe-base64
    Fernet key. With none set, derive one deterministically from API_SECRET.
    """
    key = settings.token_encryption_key
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(settings.api_secret.encode()).digest()
    )
    return Fernet(derived)


def _looks_like_fernet(value: str) -> bool:
    """True if ``value`` has the shape of a Fernet token (vs. legacy plaintext)."""
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
    except (binascii.Error, ValueError, TypeError):
        return False
    return len(decoded) >= _FERNET_MIN_BYTES and decoded[0] == _FERNET_VERSION


def encrypt(plaintext: str) -> str:
    """Encrypt a string for storage."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a stored value, passing through legacy plaintext unchanged."""
    if not _looks_like_fernet(value):
        # Migration 0003 already re-encrypted every row that existed, so nothing
        # should reach this path any more. If something does, a write bypassed
        # the EncryptedText type and we're storing a secret in the clear -- say
        # so loudly rather than hand the plaintext back as if all were well.
        logger.warning(
            "Read a provider secret that isn't encrypted at rest; a write path "
            "is bypassing EncryptedText"
        )
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Looks like ciphertext but won't decrypt -- almost certainly the wrong
        # key (e.g. rotated without re-encrypting). Surface it rather than hand
        # back garbage that would fail confusingly downstream.
        logger.error("Failed to decrypt a stored secret -- TOKEN_ENCRYPTION_KEY mismatch?")
        raise
