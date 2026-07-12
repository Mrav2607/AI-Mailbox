"""Persist Gmail's incremental synchronization cursor."""

import sqlalchemy as sa
from alembic import op


revision = "0007_gmail_history_cursor"
down_revision = "0006_thread_done_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_account",
        sa.Column("gmail_history_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "provider_account",
        sa.Column(
            "gmail_backfill_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_account", "gmail_backfill_complete")
    op.drop_column("provider_account", "gmail_history_id")
