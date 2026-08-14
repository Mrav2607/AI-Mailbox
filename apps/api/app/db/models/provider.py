from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
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
        # account" without banning it. Users can also connect more than one
        # Gmail account -- there's no cap on (user_id, provider) rows anymore.
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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
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
    # Cursor for Outlook's incremental delta walk, keyed by folder ("inbox",
    # "sentitems"): {folder_key: {"url", "baseline_complete", "baseline_count",
    # "baseline_days"}}. Stored as JSON text since each folder tracks its own
    # generation independently (unlike Gmail's single history_id).
    outlook_delta_cursors: Mapped[str | None] = mapped_column(Text, nullable=True)
    outlook_backfill_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Human-readable account email for display -- external_user_id is the
    # stable tid:oid identity, not necessarily an email. Null for existing
    # gmail rows.
    display_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-account opt-in for pushing CortexMail's classification back to the
    # provider as a Gmail label / Outlook category (docs/plans/2026-08-13-
    # label-sync-plan.md §3.1/§3.3). Off by default -- this is a write path,
    # so it never turns on without the user asking for it.
    label_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Cached Gmail label name -> id map (JSON text, outlook_delta_cursors
    # precedent above): full slash-delimited names ("CortexMail",
    # "CortexMail/Needs reply", ...) to their Gmail label ids, so a sync task
    # doesn't re-list/re-create labels on every run. Cleared on re-enable
    # (label-sync-plan §3.3) since a label renamed while disabled would
    # otherwise keep a stale-but-valid cached id.
    gmail_label_map: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bumped on every enable (false->true). Tasks capture this at claim time
    # and fence their writes against it (label-sync-plan §3.1/§3.8 P4-1) so
    # an older-generation task can never repopulate a cache -- or clear a
    # resync flag -- a re-enable just reset.
    label_sync_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
