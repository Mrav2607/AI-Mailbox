from __future__ import annotations

import uuid

from sqlalchemy import TIMESTAMP, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, ARRAY, FLOAT
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MessageEmbedding(Base):
    """
    Vector representation stored alongside the message (pgvector friendly).
    """

    __tablename__ = "message_embedding"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mail_message.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(FLOAT()))
    created_at: Mapped[TIMESTAMP] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
