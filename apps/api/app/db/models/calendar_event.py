from __future__ import annotations

import uuid

from sqlalchemy import Text, TIMESTAMP, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class CalendarEvent(Base):
    """
    Events parsed from messages (invites, reminders).
    """

    __tablename__ = "calendar_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mail_message.id", ondelete="CASCADE")
    )
    title: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP(timezone=True))
    ends_at: Mapped[TIMESTAMP | None] = mapped_column(TIMESTAMP(timezone=True))
    location: Mapped[str | None] = mapped_column(Text)
    organizer: Mapped[str | None] = mapped_column(Text)
    rsvp_link: Mapped[str | None] = mapped_column(Text)
