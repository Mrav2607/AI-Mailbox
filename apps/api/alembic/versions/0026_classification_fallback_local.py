"""Add classification_fallback_local opt-in to user_llm_credential.

Phase 2 of the LLM-failure-visibility work (plan: docs/plans/2026-08-14-
llm-failure-visibility-plan.md): today, a failed BYOK classification call
silently falls back to the local encoder, then keyword rules. This column
makes the local-encoder fallback an explicit per-user opt-in -- default
**false**, so every existing BYOK user loses automatic fallback on deploy
until they turn it back on (called out in the PR's deploy notes). Without
it, a failed call now leaves the message unclassified instead.

Downgrade PREFLIGHT (0018/0025 precedent): refuses with RuntimeError +
remediation SQL if any row has this opt-in set to true -- dropping it out
from under a user who turned it on would silently revert their choice with
no record they ever made it.
"""

import sqlalchemy as sa
from alembic import op


revision = "0026_classification_fallback"
down_revision = "0025_label_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_llm_credential",
        sa.Column(
            "classification_fallback_local",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    if op.get_bind().execute(
        sa.text(
            "SELECT id FROM user_llm_credential "
            "WHERE classification_fallback_local = true LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade past 0026_classification_fallback; a user has "
            "classification_fallback_local = true. Remediate before "
            "retrying:\n"
            "-- Turn the opt-in back off first, e.g.:\n"
            "UPDATE user_llm_credential SET classification_fallback_local = false "
            "WHERE classification_fallback_local = true;"
        )
    op.drop_column("user_llm_credential", "classification_fallback_local")
