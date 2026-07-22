"""Add Outlook delta-sync cursor storage and display_email to provider_account."""

import sqlalchemy as sa
from alembic import op


revision = "0015_outlook_columns"
down_revision = "0014_account_recency_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_account",
        sa.Column("outlook_delta_cursors", sa.Text(), nullable=True),
    )
    op.add_column(
        "provider_account",
        sa.Column(
            "outlook_backfill_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "provider_account",
        sa.Column("display_email", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_account", "display_email")
    op.drop_column("provider_account", "outlook_backfill_complete")
    op.drop_column("provider_account", "outlook_delta_cursors")
