"""Add classification_byok opt-in to user_llm_credential."""

import sqlalchemy as sa
from alembic import op


revision = "0018_classification_byok"
down_revision = "0017_user_llm_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_llm_credential",
        sa.Column(
            "classification_byok",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_llm_credential", "classification_byok")
