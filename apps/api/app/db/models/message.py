from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MailMessage(Base):
    """
    Individual message within a thread.
    """

    __tablename__ = "mail_message"
    __table_args__ = (
        UniqueConstraint("thread_id", "provider_message_id", name="uq_msg_provider"),
        # Serves the latest-message-per-thread lookup used by triage.
        Index(
            "ix_mail_message_thread_recency",
            "thread_id",
            text("sent_at DESC NULLS LAST"),
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_thread.id", ondelete="CASCADE")
    )
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    sender: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    cc: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    bcc: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snippet: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    headers: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
