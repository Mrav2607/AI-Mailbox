"""Add 'anthropic' to user_llm_credential.provider's CHECK constraint.

Native Anthropic BYOK support (plan: docs/plans/2026-08-19-native-anthropic-
byok-plan.md, D7): Anthropic now has its own preset in `providers.py` and its
own wire shape in `llm_client.py`, so a row can be saved with
`provider='anthropic'` -- the DB-level allowlist has to widen to match, or
that save 500s on the CHECK constraint before the app-level validation ever
gets a say.

Downgrade PREFLIGHT (0024/0026 precedent): refuses with RuntimeError +
remediation SQL if any row already has `provider='anthropic'` -- narrowing
the constraint back out from under a live row would either orphan it or
force a silent delete, neither of which this migration gets to decide on
its own.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_anthropic_provider"
down_revision = "0027_agenda_index_order"
branch_labels = None
depends_on = None

_OLD_PROVIDERS = "'openai', 'gemini', 'openrouter', 'groq', 'mistral', 'custom'"
_NEW_PROVIDERS = "'openai', 'gemini', 'openrouter', 'groq', 'mistral', 'anthropic', 'custom'"


def upgrade() -> None:
    op.drop_constraint("ck_llm_credential_provider", "user_llm_credential", type_="check")
    op.create_check_constraint(
        "ck_llm_credential_provider",
        "user_llm_credential",
        f"provider IN ({_NEW_PROVIDERS})",
    )


def downgrade() -> None:
    if op.get_bind().execute(
        sa.text("SELECT id FROM user_llm_credential WHERE provider = 'anthropic' LIMIT 1")
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade past 0028_anthropic_provider; a user has "
            "provider='anthropic'. Remediate before retrying:\n"
            "-- Move the credential to another provider or delete it, e.g.:\n"
            "DELETE FROM user_llm_credential WHERE provider = 'anthropic';"
        )

    op.drop_constraint("ck_llm_credential_provider", "user_llm_credential", type_="check")
    op.create_check_constraint(
        "ck_llm_credential_provider",
        "user_llm_credential",
        f"provider IN ({_OLD_PROVIDERS})",
    )
