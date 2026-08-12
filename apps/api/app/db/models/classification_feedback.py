from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class ClassificationFeedback(Base):
    """
    Append-only log of operator corrections against the console's
    classification (see docs/plans/2026-08-11-feedback-capture-plan.md §3.1).

    Every row is a training example: the model's prior label/confidence (if
    any), the label the human chose, and a bounded snapshot of the text shown
    at correction time (`input_text`, truncated to `FEEDBACK_TEXT_MAX` chars
    -- see `services/nlp/persistence.py` for the policy). `prior_*` means
    "the committed classification state at correction time, read under the
    message lock" -- a model write racing that same instant may not be
    represented (the operator never saw it), so consumers must treat a NULL
    prior as "no prediction was visible", not "no prediction ever existed". Rows are never
    updated or deleted by application code; a second correction on the same
    message appends a new row instead, and `capture_seq` -- not `created_at`,
    which is transaction-start time and can mis-sort under concurrency --
    is the durable ordering key consumers use to resolve "latest event per
    message".
    """

    __tablename__ = "classification_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Identity, not a plain sequence default -- always=True means only the DB
    # ever assigns it, so concurrent inserts under the §3.2 per-message lock
    # get a strict monotone order with no risk of an ORM-supplied value
    # colliding or skipping ahead.
    capture_seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    # ON DELETE CASCADE per D4: a deleted thread's feedback goes with it,
    # honoring the product's deletion promise. Exports are the durable
    # artifact past that point (see RUNBOOK).
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mail_message.id", ondelete="CASCADE"),
        nullable=False,
    )
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    prior_label: Mapped[str | None] = mapped_column(Text)
    prior_confidence: Mapped[float | None] = mapped_column(Numeric(asdecimal=False))
    prior_model_version: Mapped[str | None] = mapped_column(Text)
    new_label: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="reclassify")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # After the columns, not before like every other model here -- the index
    # below needs to reference the already-built `capture_seq` column object
    # (for its DESC modifier), which doesn't exist yet at the point
    # __table_args__ normally sits.
    __table_args__ = (
        CheckConstraint(
            "new_label IN ('needs_reply', 'action_required', 'fyi', 'promotional', "
            "'security_alert', 'spam')",
            name="ck_classification_feedback_label",
        ),
        CheckConstraint(
            "source IN ('reclassify')",
            name="ck_classification_feedback_source",
        ),
        # Serves the export's `ORDER BY message_id, capture_seq DESC` --
        # DISTINCT ON (message_id) latest-per-message needs the mixed
        # ASC/DESC order, which a plain B-tree can't produce.
        Index(
            "ix_classification_feedback_msg_seq",
            "message_id",
            capture_seq.desc(),
        ),
    )
