"""Require provider_account.user_id (orphaned NULL rows are unrecoverable)."""

from alembic import op
from sqlalchemy.dialects import postgresql


# Kept short: alembic_version.version_num is varchar(32).
revision = "0020_provider_owner_required"
down_revision = "0019_llm_usage_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A NULL user_id here is a login-callback bug artifact, not a real
    # account: no user can ever see or reconnect it, and it blocks every
    # future login for that provider identity via the cross-user unique
    # constraint. There's nothing to preserve, so delete before tightening
    # the column.
    op.execute("DELETE FROM provider_account WHERE user_id IS NULL")
    op.alter_column(
        "provider_account",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )


def downgrade() -> None:
    # Deleted orphans are intentionally not restored -- this only reopens
    # the column to NULL.
    op.alter_column(
        "provider_account",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
