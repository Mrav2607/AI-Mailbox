from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import EncryptedText


class ProviderAccount(Base):
    """
    Connected provider account (e.g., Gmail, Outlook) with OAuth tokens.
    """

    __tablename__ = "provider_account"
    __table_args__ = (
        CheckConstraint("provider IN ('gmail','outlook')", name="provider_check"),
        # external_user_id is in the key on purpose: connecting a second Google
        # account is a supported flow, so this pins "one row per connected
        # account" without banning it.
        UniqueConstraint(
            "user_id",
            "provider",
            "external_user_id",
            name="uq_provider_account_user_provider_external",
        ),
        UniqueConstraint(
            "provider",
            "external_user_id",
            name="uq_provider_account_provider_external_user",
        ),
        UniqueConstraint(
            "user_id", "provider", name="uq_provider_account_user_provider"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Encrypted at rest (transparent to application code). Stored as Text, so
    # no column-type migration is needed -- existing plaintext is read back via
    # the legacy passthrough in app.core.crypto and re-encrypted on next write.
    access_token: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cursor for Gmail's incremental History API. Stored as text because Gmail
    # history IDs are opaque, monotonically increasing decimal strings that can
    # exceed JavaScript's safe integer range.
    gmail_history_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gmail_backfill_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Set when Google rejects our refresh token for good (invalid_grant). The
    # scheduler skips paused accounts -- nothing but a reconnect will fix them,
    # so retrying just burns quota and buries the real signal.
    sync_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
