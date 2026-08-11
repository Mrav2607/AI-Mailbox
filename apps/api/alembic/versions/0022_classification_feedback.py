"""Add classification_feedback, the append-only operator-correction log.

Every manual relabel in the console now writes a row here alongside the
existing classification upsert: the model's prior label/confidence/version
(if any), the operator's new label, and a bounded snapshot of the text shown
at correction time. See docs/plans/2026-08-11-feedback-capture-plan.md §3.1.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0022_classification_feedback"
down_revision = "0021_tighten_orphan_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classification_feedback",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "capture_seq",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("prior_label", sa.Text(), nullable=True),
        sa.Column("prior_confidence", sa.Numeric(), nullable=True),
        sa.Column("prior_model_version", sa.Text(), nullable=True),
        sa.Column("new_label", sa.Text(), nullable=False),
        sa.Column(
            "source", sa.Text(), nullable=False, server_default="reclassify"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "new_label IN ('needs_reply', 'action_required', 'fyi', 'promotional', "
            "'security_alert', 'spam')",
            name="ck_classification_feedback_label",
        ),
        sa.CheckConstraint(
            "source IN ('reclassify')",
            name="ck_classification_feedback_source",
        ),
    )
    # DESC on capture_seq so the export's `ORDER BY message_id, capture_seq
    # DESC` (DISTINCT ON latest-per-message) scans this index directly
    # instead of sorting -- a plain ASC/ASC B-tree can't produce that order.
    op.create_index(
        "ix_classification_feedback_msg_seq",
        "classification_feedback",
        ["message_id", sa.text("capture_seq DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_feedback_msg_seq", table_name="classification_feedback"
    )
    op.drop_table("classification_feedback")
