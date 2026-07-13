"""Backfill: encrypt existing provider OAuth tokens at rest.

The access_token / refresh_token columns became EncryptedText (transparent at
the ORM layer), but rows written before that stay plaintext until next rewritten.
This migration encrypts any plaintext values in place so existing records are
protected too, not just new writes.

Uses the same key resolution as the app (TOKEN_ENCRYPTION_KEY, else derived from
API_SECRET), so it must run in the same environment as the API.

The Fernet logic below is deliberately duplicated from app.core.crypto rather
than imported. A migration is frozen history: it has to keep doing what it did
the day it was written, and importing live application code means a later
refactor of crypto.py silently changes what this migration does on a fresh
database.
"""

import base64
import binascii
import hashlib

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet

from app.core.config import settings

# Fernet's version byte, and the minimum decoded length of a real token:
# version(1) + timestamp(8) + IV(16) + one cipher block(16) + HMAC(32).
_FERNET_VERSION = 0x80
_FERNET_MIN_BYTES = 1 + 8 + 16 + 16 + 32


def _fernet() -> Fernet:
    key = settings.token_encryption_key
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(settings.api_secret.encode()).digest()
    )
    return Fernet(derived)


def _looks_like_fernet(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
    except (binascii.Error, ValueError, TypeError):
        return False
    return len(decoded) >= _FERNET_MIN_BYTES and decoded[0] == _FERNET_VERSION


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    if not _looks_like_fernet(value):
        return value
    return _fernet().decrypt(value.encode()).decode()


revision = "0003_encrypt_provider_tokens"
down_revision = "0002_classification_msg_unique"
branch_labels = None
depends_on = None

_SELECT = sa.text("SELECT id, access_token, refresh_token FROM provider_account")
# Compare-and-swap on the original token values, so a token the app refreshed
# concurrently (between our SELECT and UPDATE) isn't clobbered with stale data.
# IS NOT DISTINCT FROM matches NULLs (refresh_token is nullable).
_UPDATE = sa.text(
    "UPDATE provider_account SET access_token = :access, refresh_token = :refresh "
    "WHERE id = :id "
    "AND access_token IS NOT DISTINCT FROM :old_access "
    "AND refresh_token IS NOT DISTINCT FROM :old_refresh"
)


def _rewrite(transform) -> None:
    """Apply ``transform`` to each token and persist rows that actually change."""
    conn = op.get_bind()
    for row in conn.execute(_SELECT).fetchall():
        new_access = transform(row.access_token)
        new_refresh = transform(row.refresh_token)
        if new_access != row.access_token or new_refresh != row.refresh_token:
            conn.execute(
                _UPDATE,
                {
                    "id": row.id,
                    "access": new_access,
                    "refresh": new_refresh,
                    "old_access": row.access_token,
                    "old_refresh": row.refresh_token,
                },
            )


def upgrade() -> None:
    # Encrypt only plaintext values; already-encrypted rows are left as-is.
    def to_ciphertext(value: str | None) -> str | None:
        if value is None:
            return value
        if _looks_like_fernet(value):
            # Already ciphertext: validate this environment holds the right key
            # rather than silently leaving an undecryptable value behind.
            decrypt(value)
            return value
        return encrypt(value)

    _rewrite(to_ciphertext)


def downgrade() -> None:
    # Decrypt back to plaintext so the column can revert to bare Text.
    def to_plaintext(value: str | None) -> str | None:
        if value is None or not _looks_like_fernet(value):
            return value
        return decrypt(value)

    _rewrite(to_plaintext)
