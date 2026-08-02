"""Add the user_llm_credential table for BYOK per-user LLM credentials."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0017_user_llm_credentials"
down_revision = "0016_action_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_llm_credential",
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
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
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
        sa.UniqueConstraint("user_id", name="uq_llm_credential_user"),
        sa.CheckConstraint(
            "provider IN ('openai', 'gemini', 'openrouter', 'groq', 'mistral', 'custom')",
            name="ck_llm_credential_provider",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_llm_credential")
