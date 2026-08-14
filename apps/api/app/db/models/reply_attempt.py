from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class ReplyAttempt(Base):
    """
    One row per "send reply" click: the durable audit trail AND the
    duplicate-send guard (see docs/plans/2026-08-13-reply-plan.md §3.5/§3.6).

    Status ladder: ``preparing`` -> ``inflight`` -> one of ``sent`` /
    ``failed`` / ``unknown``, or ``preparing`` -> ``expired`` (a stale claim
    superseded by a later request), or user-overridden -> ``abandoned``.
    Every transition is a compare-and-set -- ``app/services/mail_send/
    common.py`` owns every write to this table, never a bare UPDATE
    elsewhere.

    ``verified_at`` is stamped once the authoritative provider copy is
    confirmed: at completion for Gmail (our assembled MIME IS what sync
    later normalizes), or at correlated sentitems reconciliation for
    Outlook (Graph's createReply response is a provisional draft
    representation, never classified, never persisted as its own row).
    """

    __tablename__ = "reply_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('preparing','inflight','sent','unknown','failed','abandoned','expired')",
            name="ck_reply_attempt_status",
        ),
        CheckConstraint(
            "provider IN ('gmail','outlook')", name="ck_reply_attempt_provider"
        ),
        # ix_reply_attempt_reconcile (the reconciliation-eligibility partial
        # index) lives in the migration ONLY -- partial indexes are never
        # mirrored to models here (0009 precedent, REVIEW.md); it's exempted
        # from the drift check via MIGRATION_ONLY_INDEXES in alembic/env.py.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_thread.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="preparing")
    # Our own generated RFC Message-ID, written before the Gmail send. Gmail
    # only -- Outlook correlates on provider_message_id instead.
    gmail_message_id_header: Mapped[str | None] = mapped_column(Text)
    # Gmail: the send response's message id. Outlook: the draft's immutable
    # id, which survives the draft -> Sent Items move.
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
