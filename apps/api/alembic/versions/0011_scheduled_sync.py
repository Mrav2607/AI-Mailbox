"""Groundwork for server-side scheduled sync.

Three things, all in service of a scheduler that pulls Gmail on a fixed cadence
instead of waiting for a browser tab to be open:

1. provider_account.sync_paused_at / sync_pause_reason. When Google tells us a
   refresh token is dead (invalid_grant), retrying is pointless -- only the user
   reconnecting fixes it. Pausing takes that account out of the schedule so a
   revoked token costs zero Gmail calls forever after, and lets the console show
   "reconnect" instead of pretending the mailbox is merely stale.

2. A unique constraint on (user_id, provider, external_user_id). The OAuth
   callback does query-then-insert with no integrity backstop, so two concurrent
   authorizations could leave duplicate rows for the same account -- and ingest
   picks the first match with no ordering. Note the external_user_id in the key:
   connecting a *second* Google account is a supported flow, so the constraint
   matches the callback's real lookup key rather than banning it.

3. A partial index for the sync-health query, which asks for the newest
   successful run per user. The existing (user_id, requested_at) index doesn't
   serve it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_scheduled_sync"
down_revision = "0010_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_account",
        sa.Column("sync_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_account",
        sa.Column("sync_pause_reason", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_provider_account_user_provider_external",
        "provider_account",
        ["user_id", "provider", "external_user_id"],
    )
    op.create_index(
        "ix_mail_sync_run_user_succeeded",
        "mail_sync_run",
        ["user_id", sa.text("completed_at DESC")],
        postgresql_where=sa.text("status = 'succeeded'"),
    )


def downgrade() -> None:
    op.drop_index("ix_mail_sync_run_user_succeeded", table_name="mail_sync_run")
    op.drop_constraint(
        "uq_provider_account_user_provider_external",
        "provider_account",
        type_="unique",
    )
    op.drop_column("provider_account", "sync_pause_reason")
    op.drop_column("provider_account", "sync_paused_at")
