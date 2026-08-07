import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure app package is importable when running alembic from the apps/api directory
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
# Import models so metadata is populated for autogenerate
import app.db.models  # noqa: F401,E402

config = context.config
section = config.get_section(config.config_ini_section) or {}
# Prefer an explicit shell DATABASE_URL; otherwise fall back to the value in
# .env (loaded by Settings). Run locally with a host of "localhost" -- the
# "db" hostname only resolves inside the Docker Compose network.
section["sqlalchemy.url"] = os.getenv("DATABASE_URL", settings.database_url)

# Use ORM metadata for autogenerate (kept in sync with alembic revision 0001)
target_metadata = Base.metadata

# Indexes created directly in a migration (expression/partial/trigram indexes)
# have no ORM-metadata equivalent, so autogenerate/check would flag them as
# "missing from models" forever. Policy: an index that only exists because a
# migration created it by hand goes on this allowlist; anything else belongs
# in the models, full stop.
MIGRATION_ONLY_INDEXES = {
    "ux_app_user_email_lower",
    "ix_trgm_message_body",
    "ix_trgm_message_sender",
    "ix_trgm_message_snippet",
    "ix_trgm_thread_subject",
    "ix_mail_message_thread_recency_coalesced",
    "ix_mail_sync_run_user_requested",
    "ix_mail_sync_run_user_succeeded",
    "uq_mail_sync_run_active_account",
    "ix_mail_thread_account_recency",
    "ix_mail_thread_open_user_recency",
}


def include_name(name, type_, parent_names):
    if type_ == "index" and name in MIGRATION_ONLY_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=section["sqlalchemy.url"],
        target_metadata=target_metadata,
        include_name=include_name,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
