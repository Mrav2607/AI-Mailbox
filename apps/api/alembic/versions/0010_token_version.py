"""Add app_user.token_version -- the revocation generation counter.

Every session JWT carries the token_version it was minted against. Auth compares
the claim to this column and rejects a mismatch, so bumping the counter kills
every token that user holds. That's the whole kill switch: no denylist, no extra
lookup (get_current_user already loads this row), and it survives a restart
because it lives in Postgres rather than in Redis.

Existing rows get 0, and tokens minted before this migration carry no version
claim at all -- those are rejected outright, so everyone signs in once more.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_token_version"
down_revision = "0009_recency_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_user", "token_version")
