"""Multiple named BYOK LLM credentials per user, with at most one active.

Plan: docs/plans/2026-08-19-multi-credential-llm-profiles-plan.md (D7).
Precedent for the shape of this migration: 0013_multi_gmail_accounts.py
relaxed a per-user-scoped uniqueness the same way -- add the new columns
nullable, backfill, tighten, then swap the constraint -- with preflights
that raise carrying remediation SQL rather than silently mutating (same
guard style as 0026's downgrade).

Steps:
1. Add `name` nullable, `is_active` default false.
2. Backfill `name = 'default'`, `is_active = true` -- the OLD constraint
   (`uq_llm_credential_user`, one row per user) guarantees there's no
   ambiguity in either backfilled value.
3. Alter `name` to NOT NULL now that every row has one.
4. Drop `uq_llm_credential_user`; create `uq_llm_credential_user_name`
   (user_id, name) and the partial unique index
   `uq_llm_credential_user_active` (D2's <=1-active-per-user half).

Downgrade PREFLIGHT (0013/0026 precedent): refuses with RuntimeError +
remediation SQL if any user has more than one row, OR if any surviving row
was renamed away from 'default' -- either would mean dropping the columns
silently discards data (a second profile, or a rename) with no record it
ever existed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_multi_llm_credentials"
down_revision = "0028_anthropic_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_llm_credential", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "user_llm_credential",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE user_llm_credential SET name = 'default', is_active = true")
    op.alter_column("user_llm_credential", "name", nullable=False)

    op.drop_constraint("uq_llm_credential_user", "user_llm_credential", type_="unique")
    op.create_unique_constraint(
        "uq_llm_credential_user_name", "user_llm_credential", ["user_id", "name"]
    )
    op.create_index(
        "uq_llm_credential_user_active",
        "user_llm_credential",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    if op.get_bind().execute(
        sa.text(
            "SELECT user_id FROM user_llm_credential "
            "GROUP BY user_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade past 0029_multi_llm_credentials; some users have more "
            "than one credential row. Remediate before retrying (keep exactly one row "
            "per user, e.g. the active one):\n"
            "SELECT user_id, array_agg(id) FROM user_llm_credential "
            "GROUP BY user_id HAVING count(*) > 1;"
        )
    if op.get_bind().execute(
        sa.text("SELECT id FROM user_llm_credential WHERE name != 'default' LIMIT 1")
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade past 0029_multi_llm_credentials; a row has been renamed "
            "away from 'default'. Dropping the name column would silently discard that "
            "rename. Remediate before retrying:\n"
            "UPDATE user_llm_credential SET name = 'default' WHERE name != 'default';"
        )

    op.drop_index("uq_llm_credential_user_active", table_name="user_llm_credential")
    op.drop_constraint("uq_llm_credential_user_name", "user_llm_credential", type_="unique")
    op.create_unique_constraint(
        "uq_llm_credential_user", "user_llm_credential", ["user_id"]
    )
    op.drop_column("user_llm_credential", "is_active")
    op.drop_column("user_llm_credential", "name")
