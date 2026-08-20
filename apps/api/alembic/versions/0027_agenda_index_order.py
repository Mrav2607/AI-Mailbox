"""Replace ix_action_item_user_agenda with an ordered index.

Part of the agenda cursor pagination work (plan: docs/plans/2026-08-19-
agenda-cursor-pagination-plan.md, D6.2). The old index
(`user_id, outcome, status, due_at`) has an unordered `due_at` column, which
can't serve the route's mixed-direction sort (`due_at ASC NULLS LAST,
created_at DESC, id DESC`) -- Postgres falls back to an explicit sort after
the index scan. Same name, same leading columns, dropped and recreated with
the ordering expressions appended so it matches the ORDER BY exactly.
"""

import sqlalchemy as sa
from alembic import op


revision = "0027_agenda_index_order"
down_revision = "0026_classification_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_action_item_user_agenda", table_name="action_item")
    op.create_index(
        "ix_action_item_user_agenda",
        "action_item",
        [
            "user_id",
            "outcome",
            "status",
            sa.text("due_at ASC NULLS LAST"),
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_action_item_user_agenda", table_name="action_item")
    op.create_index(
        "ix_action_item_user_agenda",
        "action_item",
        ["user_id", "outcome", "status", "due_at"],
    )
