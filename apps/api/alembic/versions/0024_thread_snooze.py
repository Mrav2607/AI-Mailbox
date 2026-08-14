"""Add mail_thread.snoozed_until/snoozed_at, the snooze-pair and done/snooze
exclusivity CHECKs, and a partial index for the snoozed-bucket query.

See docs/plans/2026-08-13-snooze-plan.md §3.1/§3.3. No scheduler backs this
feature -- a thread's snooze state is read at query time via a predicate in
app/routes/mailbox.py, never derived or written here beyond the two columns.

Downgrade REFUSES (0013/0023 precedent: RuntimeError + remediation SQL,
never silently erase user data) if any row still has snoozed_until set --
dropping it would erase an active snooze with no way to recover it.
"""

import sqlalchemy as sa
from alembic import op


revision = "0024_thread_snooze"
down_revision = "0023_reply_attempt_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mail_thread", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "mail_thread", sa.Column("snoozed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_mail_thread_snooze_pair",
        "mail_thread",
        "(snoozed_until IS NULL) = (snoozed_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_mail_thread_done_snooze_excl",
        "mail_thread",
        "NOT (done_at IS NOT NULL AND snoozed_until IS NOT NULL)",
    )
    # Partial: only rows with an active snooze ever hit this index (the
    # snoozed-bucket list and its per-user wake-time sort) -- migration-only
    # per house style (0009 precedent), see alembic/env.py's
    # MIGRATION_ONLY_INDEXES.
    op.create_index(
        "ix_mail_thread_snooze",
        "mail_thread",
        ["user_id", "snoozed_until"],
        postgresql_where=sa.text("snoozed_until IS NOT NULL"),
    )


def _require_empty(check_sql: str, remediation_sql: str, label: str) -> None:
    if op.get_bind().execute(sa.text(check_sql)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade past {label}; live snooze state would be destroyed. "
            f"Remediate before retrying:\n{remediation_sql}"
        )


def downgrade() -> None:
    _require_empty(
        "SELECT id FROM mail_thread WHERE snoozed_until IS NOT NULL LIMIT 1",
        "-- Export and clear active snoozes first, e.g.:\n"
        "-- COPY (SELECT * FROM mail_thread WHERE snoozed_until IS NOT NULL) "
        "TO '/tmp/mail_thread_snooze_backup.csv' CSV HEADER;\n"
        "UPDATE mail_thread SET snoozed_until = NULL, snoozed_at = NULL "
        "WHERE snoozed_until IS NOT NULL;",
        "0024_thread_snooze (mail_thread has active snoozes)",
    )

    op.drop_index("ix_mail_thread_snooze", table_name="mail_thread")
    op.drop_constraint("ck_mail_thread_done_snooze_excl", "mail_thread", type_="check")
    op.drop_constraint("ck_mail_thread_snooze_pair", "mail_thread", type_="check")
    op.drop_column("mail_thread", "snoozed_at")
    op.drop_column("mail_thread", "snoozed_until")
