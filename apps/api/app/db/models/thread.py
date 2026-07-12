from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MailThread(Base):
    """
    Normalized thread record per provider + user.
    """

    __tablename__ = "mail_thread"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "provider_thread_id", name="uq_thread_provider"
        ),
        # Serves the triage list: WHERE user_id = ? ORDER BY recency.
        Index(
            "ix_mail_thread_user_recency",
            "user_id",
            text("last_message_at DESC NULLS LAST"),
            text("created_at DESC"),
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
    provider_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Triage "done" marker: null = open, timestamp = when the operator cleared
    # it. Done threads leave every open bucket but stay searchable.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
