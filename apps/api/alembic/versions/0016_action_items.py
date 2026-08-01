"""Add the action_item table for second-stage LLM action extraction."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0016_action_items"
down_revision = "0015_outlook_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_item",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("kind", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("due_precision", sa.Text()),
        sa.Column("due_raw", sa.Text()),
        sa.Column("amount", sa.Numeric()),
        sa.Column("currency", sa.Text()),
        sa.Column("source_confidence", sa.Numeric()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("status_at", sa.DateTime(timezone=True)),
        sa.Column("model_version", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("message_id", name="uq_action_item_message"),
        sa.CheckConstraint(
            "outcome IN ('pending', 'extracted', 'no_action', 'failed', 'ineligible')",
            name="ck_action_item_outcome",
        ),
        sa.CheckConstraint(
            "kind IS NULL OR kind IN "
            "('reply', 'payment', 'signature', 'form', 'rsvp', 'deadline', 'other')",
            name="ck_action_item_kind",
        ),
        sa.CheckConstraint(
            "due_precision IS NULL OR due_precision IN ('date', 'datetime')",
            name="ck_action_item_due_precision",
        ),
        sa.CheckConstraint(
            "source_confidence >= 0 AND source_confidence <= 1",
            name="ck_action_item_confidence",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'dismissed')",
            name="ck_action_item_status",
        ),
    )
    op.create_index(
        "ix_action_item_user_agenda",
        "action_item",
        ["user_id", "outcome", "status", "due_at"],
    )
    op.create_index(
        "ix_action_item_thread",
        "action_item",
        ["thread_id", "outcome", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_item_thread", table_name="action_item")
    op.drop_index("ix_action_item_user_agenda", table_name="action_item")
    op.drop_table("action_item")
