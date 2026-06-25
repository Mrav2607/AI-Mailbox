from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class Classification(Base):
    """
    LLM or heuristic classification results per message.
    """

    __tablename__ = "classification"
    # One classification per message: the unique constraint makes that an
    # invariant the DB enforces (so concurrent writers can't double-insert) and
    # serves as the conflict target for upserts. It also backs message_id
    # lookups, replacing the old non-unique index.
    __table_args__ = (UniqueConstraint("message_id", name="uq_classification_message"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_message.id", ondelete="CASCADE")
    )
    label: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
