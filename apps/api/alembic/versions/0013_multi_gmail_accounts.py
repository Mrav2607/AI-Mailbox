"""Support multiple connected Gmail accounts per user."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_multi_gmail_accounts"
down_revision = "0012_email_password_auth"
branch_labels = None
depends_on = None


def _require_no_rows(check_sql: str, remediation_sql: str, label: str) -> None:
    """Stop before a new uniqueness rule hides an existing data conflict."""
    if op.get_bind().execute(sa.text(check_sql)).first() is not None:
        raise RuntimeError(
            f"Cannot create {label}; existing rows violate its uniqueness rule. "
            f"Remediate before retrying:\n{remediation_sql}"
        )


def upgrade() -> None:
    op.drop_constraint(
        "uq_provider_account_user_provider", "provider_account", type_="unique"
    )

    # Preflight: every mail_thread row must resolve to exactly one
    # provider_account via (user_id, provider), or the backfill join below
    # would silently leave it NULL and fail the NOT NULL alter.
    orphan_threads = op.get_bind().execute(
        sa.text(
            "SELECT id FROM mail_thread mt WHERE NOT EXISTS ("
            "SELECT 1 FROM provider_account pa "
            "WHERE pa.user_id = mt.user_id AND pa.provider = mt.provider"
            ") LIMIT 1"
        )
    ).first()
    if orphan_threads is not None:
        raise RuntimeError(
            "Cannot backfill mail_thread.provider_account_id; some threads have "
            "no matching provider_account for their (user_id, provider). "
            "Remediate before retrying:\n"
            "DELETE FROM mail_thread mt WHERE NOT EXISTS ("
            "SELECT 1 FROM provider_account pa "
            "WHERE pa.user_id = mt.user_id AND pa.provider = mt.provider);"
        )

    op.add_column(
        "mail_thread",
        sa.Column(
            "provider_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_account.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE mail_thread SET provider_account_id = pa.id "
        "FROM provider_account pa "
        "WHERE pa.user_id = mail_thread.user_id AND pa.provider = mail_thread.provider"
    )
    op.alter_column("mail_thread", "provider_account_id", nullable=False)

    op.drop_constraint("uq_thread_provider", "mail_thread", type_="unique")
    _require_no_rows(
        "SELECT provider_account_id, provider_thread_id FROM mail_thread "
        "GROUP BY provider_account_id, provider_thread_id HAVING count(*) > 1",
        "SELECT provider_account_id, provider_thread_id, array_agg(id) FROM mail_thread "
        "GROUP BY provider_account_id, provider_thread_id HAVING count(*) > 1;",
        "uq_thread_provider_account",
    )
    op.create_unique_constraint(
        "uq_thread_provider_account",
        "mail_thread",
        ["provider_account_id", "provider_thread_id"],
    )

    op.add_column(
        "mail_sync_run",
        sa.Column(
            "provider_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_account.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE mail_sync_run SET provider_account_id = pa.id "
        "FROM provider_account pa "
        "WHERE pa.user_id = mail_sync_run.user_id AND pa.provider = 'gmail'"
    )

    op.drop_index("uq_mail_sync_run_active_user", table_name="mail_sync_run")
    op.create_index(
        "uq_mail_sync_run_active_account",
        "mail_sync_run",
        ["provider_account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retrying')"),
    )


def downgrade() -> None:
    active_per_user = op.get_bind().execute(
        sa.text(
            "SELECT user_id FROM mail_sync_run "
            "WHERE status IN ('queued', 'running', 'retrying') "
            "GROUP BY user_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if active_per_user is not None:
        raise RuntimeError(
            "Cannot recreate uq_mail_sync_run_active_user; some users have more "
            "than one active sync run across their provider accounts. "
            "Wait for or expire the extra runs before retrying. Find them with:\n"
            "SELECT user_id, array_agg(id) FROM mail_sync_run "
            "WHERE status IN ('queued', 'running', 'retrying') "
            "GROUP BY user_id HAVING count(*) > 1;"
        )
    op.drop_index("uq_mail_sync_run_active_account", table_name="mail_sync_run")
    op.create_index(
        "uq_mail_sync_run_active_user",
        "mail_sync_run",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retrying')"),
    )
    op.drop_column("mail_sync_run", "provider_account_id")

    _require_no_rows(
        "SELECT user_id, provider, provider_thread_id FROM mail_thread "
        "GROUP BY user_id, provider, provider_thread_id HAVING count(*) > 1",
        "SELECT user_id, provider, provider_thread_id, array_agg(id) FROM mail_thread "
        "GROUP BY user_id, provider, provider_thread_id HAVING count(*) > 1;",
        "uq_thread_provider",
    )
    op.drop_constraint("uq_thread_provider_account", "mail_thread", type_="unique")
    op.create_unique_constraint(
        "uq_thread_provider", "mail_thread", ["user_id", "provider", "provider_thread_id"]
    )
    op.drop_column("mail_thread", "provider_account_id")

    _require_no_rows(
        "SELECT user_id, provider FROM provider_account "
        "GROUP BY user_id, provider HAVING count(*) > 1",
        "SELECT user_id, provider, array_agg(id) FROM provider_account "
        "GROUP BY user_id, provider HAVING count(*) > 1;",
        "uq_provider_account_user_provider",
    )
    op.create_unique_constraint(
        "uq_provider_account_user_provider", "provider_account", ["user_id", "provider"]
    )
