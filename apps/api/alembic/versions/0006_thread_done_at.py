"""Add the triage done marker to threads.

``done_at`` is null while a thread is open; the console's "done" action stamps
it (and can clear it again). Done threads drop out of every open triage bucket
and the counts, gain their own ``done`` bucket, and remain searchable.
"""

import sqlalchemy as sa
from alembic import op


revision = "0006_thread_done_at"
down_revision = "0005_search_trgm_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mail_thread",
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mail_thread", "done_at")
