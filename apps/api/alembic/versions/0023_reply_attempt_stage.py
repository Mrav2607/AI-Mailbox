"""Add reply_attempt, mail_thread.replied_at, and widen the usage stage CHECK.

Reply-from-CortexMail's durable attempt record + fence (see
docs/plans/2026-08-13-reply-plan.md §3.5). ``reply_attempt`` is the
duplicate-send guard and audit trail; ``mail_thread.replied_at`` is the fence
extraction resolves reply-kind outcomes against; the usage CHECK widens for
the new ``reply_draft`` stage (§3.7).

Downgrade REFUSES (0013 precedent: RuntimeError + remediation SQL, never
delete user data) if any state from this migration is live: a reply_attempt
row, a non-null replied_at, or a reply_draft usage row. Dropping an
unresolved `unknown` attempt would erase the only duplicate-send protection
this feature has.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0023_reply_attempt_stage"
down_revision = "0022_classification_feedback"
branch_labels = None
depends_on = None

_RECONCILE_INDEX_WHERE = (
    "status IN ('preparing','inflight','unknown','abandoned') "
    "OR (provider = 'outlook' AND status = 'sent' AND verified_at IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "reply_attempt",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="preparing"
        ),
        sa.Column("gmail_message_id_header", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('preparing','inflight','sent','unknown','failed','abandoned','expired')",
            name="ck_reply_attempt_status",
        ),
        sa.CheckConstraint(
            "provider IN ('gmail','outlook')", name="ck_reply_attempt_provider"
        ),
    )
    op.create_index(
        "ix_reply_attempt_reconcile",
        "reply_attempt",
        ["thread_id"],
        postgresql_where=sa.text(_RECONCILE_INDEX_WHERE),
    )

    op.add_column(
        "mail_thread", sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.drop_constraint(
        "ck_llm_usage_daily_stage", "llm_usage_daily", type_="check"
    )
    op.create_check_constraint(
        "ck_llm_usage_daily_stage",
        "llm_usage_daily",
        "stage IN ('classification', 'extraction', 'reply_draft')",
    )


def _require_empty(check_sql: str, remediation_sql: str, label: str) -> None:
    if op.get_bind().execute(sa.text(check_sql)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade past {label}; live reply state would be destroyed. "
            f"Remediate before retrying:\n{remediation_sql}"
        )


def downgrade() -> None:
    _require_empty(
        "SELECT id FROM reply_attempt LIMIT 1",
        "-- Export and clear reply_attempt rows first, e.g.:\n"
        "-- COPY reply_attempt TO '/tmp/reply_attempt_backup.csv' CSV HEADER;\n"
        "DELETE FROM reply_attempt;",
        "0023_reply_attempt_stage (reply_attempt has rows)",
    )
    _require_empty(
        "SELECT id FROM mail_thread WHERE replied_at IS NOT NULL LIMIT 1",
        "-- A non-null replied_at means a reply was sent from CortexMail; "
        "clearing it loses that fence.\n"
        "UPDATE mail_thread SET replied_at = NULL WHERE replied_at IS NOT NULL;",
        "0023_reply_attempt_stage (mail_thread.replied_at is set)",
    )
    _require_empty(
        "SELECT id FROM llm_usage_daily WHERE stage = 'reply_draft' LIMIT 1",
        "-- Export and clear reply_draft usage rows first, e.g.:\n"
        "-- COPY (SELECT * FROM llm_usage_daily WHERE stage = 'reply_draft') "
        "TO '/tmp/reply_draft_usage_backup.csv' CSV HEADER;\n"
        "DELETE FROM llm_usage_daily WHERE stage = 'reply_draft';",
        "0023_reply_attempt_stage (reply_draft usage rows exist)",
    )

    op.drop_constraint(
        "ck_llm_usage_daily_stage", "llm_usage_daily", type_="check"
    )
    op.create_check_constraint(
        "ck_llm_usage_daily_stage",
        "llm_usage_daily",
        "stage IN ('classification', 'extraction')",
    )

    op.drop_column("mail_thread", "replied_at")

    op.drop_index("ix_reply_attempt_reconcile", table_name="reply_attempt")
    op.drop_table("reply_attempt")
