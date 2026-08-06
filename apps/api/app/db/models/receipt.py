from __future__ import annotations

import uuid

from sqlalchemy import Text, Numeric, TIMESTAMP, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class Receipt(Base):
    """
    Structured commerce receipts extracted from messages.
    """

    __tablename__ = "receipt"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_message.id", ondelete="CASCADE")
    )
    merchant: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Numeric | None] = mapped_column(Numeric)
    tax: Mapped[Numeric | None] = mapped_column(Numeric)
    total: Mapped[Numeric | None] = mapped_column(Numeric)
    purchased_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP(timezone=True))
    line_items: Mapped[dict | None] = mapped_column(JSONB)
    source_confidence: Mapped[Numeric | None] = mapped_column(Numeric)
