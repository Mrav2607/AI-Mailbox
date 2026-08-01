from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class ActionItem(Base):
    """
    One row per extraction attempt target (unique per message): tracks the
    claim/attempt lifecycle for the second-stage LLM action extraction and,
    once settled, the extracted obligation itself.
    """

    __tablename__ = "action_item"
    __table_args__ = (
        # The claim/upsert conflict target -- one attempt record per message.
        UniqueConstraint("message_id", name="uq_action_item_message"),
        CheckConstraint(
            "outcome IN ('pending', 'extracted', 'no_action', 'failed', 'ineligible')",
            name="ck_action_item_outcome",
        ),
        CheckConstraint(
            "kind IS NULL OR kind IN "
            "('reply', 'payment', 'signature', 'form', 'rsvp', 'deadline', 'other')",
            name="ck_action_item_kind",
        ),
        CheckConstraint(
            "due_precision IS NULL OR due_precision IN ('date', 'datetime')",
            name="ck_action_item_due_precision",
        ),
        CheckConstraint(
            "source_confidence >= 0 AND source_confidence <= 1",
            name="ck_action_item_confidence",
        ),
        CheckConstraint(
            "status IN ('open', 'done', 'dismissed')",
            name="ck_action_item_status",
        ),
        # Serves the agenda list/counts: WHERE user_id = ? AND outcome/status
        # filters, ordered by due_at.
        Index("ix_action_item_user_agenda", "user_id", "outcome", "status", "due_at"),
        # Serves the thread-done resolution write (FK columns aren't auto-indexed).
        Index("ix_action_item_thread", "thread_id", "outcome", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_message.id", ondelete="CASCADE")
    )
    # Denormalized for the agenda index/query, but always derived server-side
    # from message -> thread in extraction_run -- never accepted from a caller.
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_thread.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE")
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # Fencing token: set on every claim, cleared on record -- guards against a
    # stale worker's late write landing after the lease expired.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Incremented at claim time (an abandoned in-flight call counts).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Stamped at claim time (including the initial insert), so a crashed
    # fresh claim is reclaimable after the lease and never NULL while pending.
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str | None] = mapped_column(Text)
    # Set iff outcome="extracted" (code-enforced).
    title: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_precision: Mapped[str | None] = mapped_column(Text)
    # Deadline phrase as written ("by end of week").
    due_raw: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Numeric(asdecimal=False))
    currency: Mapped[str | None] = mapped_column(Text)
    source_confidence: Mapped[float | None] = mapped_column(Numeric(asdecimal=False))
    # Operator/system resolution state -- user-meaningful only for extracted
    # rows, but writable on any row (thread-done resolves pending rows too).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    # When status last left open (None while open).
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
