from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Text,
    ForeignKey,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MailThread(Base):
    """
    Normalized thread record per provider + user.
    """

    __tablename__ = "mail_thread"
    __table_args__ = (
        # provider_account_id (not user_id) is the identity boundary now: a
        # user can have multiple Gmail accounts, and each account's threads
        # are keyed independently.
        UniqueConstraint(
            "provider_account_id",
            "provider_thread_id",
            name="uq_thread_provider_account",
        ),
        # Serves the triage list: WHERE user_id = ? ORDER BY recency.
        Index(
            "ix_mail_thread_user_recency",
            "user_id",
            text("last_message_at DESC NULLS LAST"),
            text("created_at DESC"),
        ),
        # Snoozed_until and snoozed_at are set/cleared together (see the
        # snoozed_until column comment below) -- one is never null while the
        # other is set.
        CheckConstraint(
            "(snoozed_until IS NULL) = (snoozed_at IS NULL)",
            name="ck_mail_thread_snooze_pair",
        ),
        # Belt-and-braces (docs/plans/2026-08-13-snooze-plan.md §3.3): done
        # and active-snooze are mutually exclusive at the application layer
        # (set_thread_done/snooze_thread in app/routes/mailbox.py both clear
        # the other lifecycle state on write) -- this CHECK makes a future
        # code path that reintroduces that race fail loudly instead of
        # corrupting state.
        CheckConstraint(
            "NOT (done_at IS NOT NULL AND snoozed_until IS NOT NULL)",
            name="ck_mail_thread_done_snooze_excl",
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
    provider_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Triage "done" marker: null = open, timestamp = when the operator cleared
    # it. Done threads leave every open bucket but stay searchable.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The reply fence: null = never replied from CortexMail, timestamp = the
    # moment a reply attempt completed (console success or reconciliation),
    # on OUR clock -- never inferred from provider/ingest chronology (see
    # docs/plans/2026-08-13-reply-plan.md §3.5). Only advances (greatest()),
    # only through app/services/mail_send/common.py's fence/resolve helper.
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Snooze wake time: null = not snoozed, timestamp = when the thread
    # reappears in triage -- or sooner, if new mail lands first (see the
    # active-snooze predicate in app/routes/mailbox.py). Always set/cleared
    # together with snoozed_at (CHECK ck_mail_thread_snooze_pair) and never
    # alongside a non-null done_at (CHECK ck_mail_thread_done_snooze_excl).
    # docs/plans/2026-08-13-snooze-plan.md §3.1/§3.3.
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the current snooze was set, on OUR clock -- the active-snooze
    # predicate compares this against last_message_at to tell "time passed"
    # from "new mail woke it early".
    snoozed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
