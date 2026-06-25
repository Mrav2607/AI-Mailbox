"""Custom SQLAlchemy column types."""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.crypto import decrypt, encrypt


class EncryptedText(TypeDecorator):
    """Text column whose value is encrypted at rest, transparently.

    Application code reads and writes plaintext; the ciphertext only exists in
    the database. Stored as Text so it needs no schema change beyond the column
    type. ``None`` passes through untouched (nullable columns stay nullable).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return decrypt(value)
