"""Add a per-account recency index for the account-filter path (WHERE
provider_account_id = ? AND done_at IS NULL ORDER BY recency), i.e. triage
and search with the provider_account_id param -- not sort=account, which
joins provider_account and orders by external_user_id first, so this index
can't serve it.

Not mirrored onto the MailThread model -- same precedent as 0009_recency_indexes:
partial indexes with expression columns (NULLS LAST, a WHERE clause) don't have
a clean SQLAlchemy Column-level spelling, so they live in the migration only.

No user_id prefix, unlike ix_mail_thread_open_user_recency from 0009: an account
belongs to exactly one user, so provider_account_id alone is already as selective
as (user_id, provider_account_id) would be.
"""

import sqlalchemy as sa
from alembic import op


revision = "0014_account_recency_index"
down_revision = "0013_multi_gmail_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_mail_thread_account_recency",
        "mail_thread",
        [
            "provider_account_id",
            sa.text("last_message_at DESC NULLS LAST"),
            sa.text("created_at DESC"),
        ],
        postgresql_where=sa.text("done_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mail_thread_account_recency", table_name="mail_thread")
