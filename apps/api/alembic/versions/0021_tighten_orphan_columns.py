"""Tighten nullable columns where the DB is looser than the models.

`alembic check` flagged a batch of columns that should have been NOT NULL
from the start: server-defaulted timestamps that autogenerate always fills,
and FK columns pointing at ON DELETE CASCADE relationships (a NULL there can
only be a pre-cascade orphan artifact, never a legitimate row -- deleting
its parent removes it, it never gets re-parented to NULL).

Also drops app_user_email_key, the plain unique constraint SQLAlchemy used
to generate from `email` column's `unique=True`. It's redundant now:
ux_app_user_email_lower already enforces uniqueness on lower(email), which
is strictly stronger (catches case-variant duplicates too).
"""

from alembic import op
from sqlalchemy.dialects import postgresql

# Kept short: alembic_version.version_num is varchar(32).
revision = "0021_tighten_orphan_cols"
down_revision = "0020_provider_owner_required"
branch_labels = None
depends_on = None


# (table, column) pairs backfilled with now() before SET NOT NULL. Prod may
# carry NULLs here even though local dev doesn't -- these are all
# server_default now() columns, so a NULL only happens if a row predates the
# default or was written around it.
TIMESTAMP_COLUMNS = [
    ("action_item", "created_at"),
    ("action_log", "created_at"),
    ("app_user", "created_at"),
    ("classification", "created_at"),
    ("llm_usage_daily", "created_at"),
    ("llm_usage_daily", "updated_at"),
    ("mail_message", "created_at"),
    ("mail_thread", "created_at"),
    ("message_embedding", "created_at"),
    ("provider_account", "created_at"),
    ("user_llm_credential", "created_at"),
    ("user_llm_credential", "updated_at"),
]

# (table, column) pairs for FK columns backed by ON DELETE CASCADE. Rows
# with a NULL here are orphans left over from before the FK's default
# behavior was relied on -- there's nothing to preserve.
FK_COLUMNS = [
    ("action_log", "user_id"),
    ("action_log", "message_id"),
    ("calendar_event", "message_id"),
    ("classification", "message_id"),
    ("mail_message", "thread_id"),
    ("mail_thread", "user_id"),
    ("receipt", "message_id"),
]


def upgrade() -> None:
    for table, column in TIMESTAMP_COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = now() WHERE {column} IS NULL")
        op.alter_column(
            table,
            column,
            existing_type=postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        )

    for table, column in FK_COLUMNS:
        op.execute(f"DELETE FROM {table} WHERE {column} IS NULL")
        op.alter_column(
            table,
            column,
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )

    op.drop_constraint("app_user_email_key", "app_user", type_="unique")


def downgrade() -> None:
    # Reopens every column back to nullable. Deleted orphans and backfilled
    # timestamps are intentionally not restored.
    op.create_unique_constraint("app_user_email_key", "app_user", ["email"])

    for table, column in FK_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )

    for table, column in TIMESTAMP_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        )
