"""Backfill: encrypt existing provider OAuth tokens at rest.

The access_token / refresh_token columns became EncryptedText (transparent at
the ORM layer), but rows written before that stay plaintext until next rewritten.
This migration encrypts any plaintext values in place so existing records are
protected too, not just new writes.

Uses the same key resolution as the app (TOKEN_ENCRYPTION_KEY, else derived from
API_SECRET), so it must run in the same environment as the API.
"""

import sqlalchemy as sa
from alembic import op

from app.core.crypto import decrypt, encrypt, _looks_like_fernet


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
