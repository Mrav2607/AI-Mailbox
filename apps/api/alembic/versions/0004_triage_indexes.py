"""Add composite indexes for the triage recency queries.

The triage list and counts endpoints resolve "latest message per thread ->
its latest classification" with correlated subqueries, but neither table had
an index matching those access paths: ``mail_thread`` is scanned by
``user_id`` ordered by recency, and ``mail_message`` by ``thread_id`` ordered
the same way. The existing unique constraints share leading columns but can't
serve the ``DESC NULLS LAST`` sort, so both queries fall back to sorting.
These two indexes match the exact filter + ordering so Postgres can walk them
directly.
"""

import sqlalchemy as sa
from alembic import op


revision = "0004_triage_indexes"
down_revision = "0003_encrypt_provider_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_mail_thread_user_recency",
        "mail_thread",
        [
            "user_id",
            sa.text("last_message_at DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
    )
    op.create_index(
        "ix_mail_message_thread_recency",
        "mail_message",
        [
            "thread_id",
            sa.text("sent_at DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_mail_message_thread_recency", table_name="mail_message")
    op.drop_index("ix_mail_thread_user_recency", table_name="mail_thread")
