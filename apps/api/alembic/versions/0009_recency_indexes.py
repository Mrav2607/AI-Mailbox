"""Match recency indexes to coalesced message ordering and open threads."""

import sqlalchemy as sa
from alembic import op


revision = "0009_recency_indexes"
down_revision = "0008_mail_sync_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_mail_message_thread_recency_coalesced",
        "mail_message",
        [
            "thread_id",
            sa.text("(COALESCE(sent_at, created_at)) DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
    )
    op.drop_index("ix_mail_message_thread_recency", table_name="mail_message")
    op.create_index(
        "ix_mail_thread_open_user_recency",
        "mail_thread",
        [
            "user_id",
            sa.text("last_message_at DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
        postgresql_where=sa.text("done_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mail_thread_open_user_recency", table_name="mail_thread")
    op.create_index(
        "ix_mail_message_thread_recency",
        "mail_message",
        [
            "thread_id",
            sa.text("sent_at DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
    )
    op.drop_index(
        "ix_mail_message_thread_recency_coalesced", table_name="mail_message"
    )
