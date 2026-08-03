"""Add llm_usage_daily for per-user LLM usage visibility."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0019_llm_usage_daily"
down_revision = "0018_classification_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_daily",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "calls_with_total_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "usage_date", "stage", "provider", name="uq_llm_usage_daily_key"
        ),
        sa.CheckConstraint(
            "stage IN ('classification', 'extraction')",
            name="ck_llm_usage_daily_stage",
        ),
        sa.CheckConstraint(
            "calls >= 0 AND calls_with_total_tokens >= 0 AND prompt_tokens >= 0"
            " AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_llm_usage_daily_nonneg",
        ),
    )
    # The unique index above leads with user_id, so it can't serve the
    # retention tick's plain "usage_date < cutoff" range delete -- a
    # standalone index does.
    op.create_index(
        "ix_llm_usage_daily_usage_date", "llm_usage_daily", ["usage_date"]
    )


def downgrade() -> None:
    op.drop_table("llm_usage_daily")
