"""Add trigram GIN indexes for the mail search query.

GET /mail/search matches ``'%q%'`` with ILIKE across ``mail_thread.subject``
and ``mail_message.sender/snippet/body_text``. Unanchored patterns can't use a
btree index, so every search was a sequential scan over both tables. pg_trgm
GIN indexes serve ILIKE ``'%...%'`` directly; we create one per column (rather
than one multicolumn index) because the search ORs across columns and the
planner combines single-column indexes with a BitmapOr.

The extension is enabled conditionally, same as pgvector in 0001: if the
server doesn't ship pg_trgm we RAISE NOTICE, skip the indexes, and the
migration still succeeds — search just stays slow there. That's also why the
ORM models deliberately don't declare these indexes: models are unconditional,
and mirroring them would break create_all/autogenerate on servers without the
extension.
"""

import logging

import sqlalchemy as sa
from alembic import op


revision = "0005_search_trgm_indexes"
down_revision = "0004_triage_indexes"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

TRGM_INDEXES = (
    ("ix_trgm_thread_subject", "mail_thread", "subject"),
    ("ix_trgm_message_sender", "mail_message", "sender"),
    ("ix_trgm_message_snippet", "mail_message", "snippet"),
    ("ix_trgm_message_body", "mail_message", "body_text"),
)


def upgrade() -> None:
    # Enable pg_trgm if the server ships it; skip gracefully if not
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
                CREATE EXTENSION IF NOT EXISTS pg_trgm;
            ELSE
                RAISE NOTICE 'pg_trgm extension not installed on this server; skipping';
            END IF;
        END
        $$;
        """
    )

    # Only build the indexes if the extension actually made it in
    has_trgm = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    ).scalar()
    if not has_trgm:
        logger.info("pg_trgm unavailable; skipping trigram search indexes")
        return

    for name, table, column in TRGM_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    for name, _table, _column in reversed(TRGM_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    # We leave pg_trgm installed on purpose: other objects may depend on it
