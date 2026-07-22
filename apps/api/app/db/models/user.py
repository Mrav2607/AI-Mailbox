from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class AppUser(Base):
    """
    End users of the mailbox app. Matches alembic table app_user.
    """

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Bump this to invalidate every token this user holds. Each JWT carries the
    # version it was minted against, and auth rejects any token whose version
    # doesn't match -- so a stolen token dies the moment the counter moves.
    #
    # An int, not a timestamp: iat is truncated to whole seconds, so a time-based
    # cutoff either kills a token minted right after the revoke or spares one
    # minted right before it. Integer equality has no such window, and no clock
    # to disagree about.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
