"""Add the database foundation for verified email/password authentication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_email_password_auth"
down_revision = "0011_scheduled_sync"
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
    op.add_column("app_user", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "app_user", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )

    _require_no_rows(
        "SELECT lower(email) FROM app_user GROUP BY lower(email) HAVING count(*) > 1",
        "SELECT lower(email), array_agg(id) FROM app_user "
        "GROUP BY lower(email) HAVING count(*) > 1;",
        "ux_app_user_email_lower",
    )
    op.create_index(
        "ux_app_user_email_lower",
        "app_user",
        [sa.text("lower(email)")],
        unique=True,
    )

    _require_no_rows(
        "SELECT provider, external_user_id FROM provider_account "
        "GROUP BY provider, external_user_id HAVING count(*) > 1",
        "SELECT provider, external_user_id, array_agg(id) FROM provider_account "
        "GROUP BY provider, external_user_id HAVING count(*) > 1;",
        "uq_provider_account_provider_external_user",
    )
    op.create_unique_constraint(
        "uq_provider_account_provider_external_user",
        "provider_account",
        ["provider", "external_user_id"],
    )

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

    op.create_table(
        "auth_token",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_password_hash", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'password_reset')", name="auth_token_purpose_check"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
    )
    op.create_index("ux_auth_token_token_hash", "auth_token", ["token_hash"], unique=True)
    op.create_index("ix_auth_token_expires_at", "auth_token", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_token_expires_at", table_name="auth_token")
    op.drop_index("ux_auth_token_token_hash", table_name="auth_token")
    op.drop_table("auth_token")
    op.drop_constraint(
        "uq_provider_account_user_provider", "provider_account", type_="unique"
    )
    op.drop_constraint(
        "uq_provider_account_provider_external_user",
        "provider_account",
        type_="unique",
    )
    op.drop_index("ux_app_user_email_lower", table_name="app_user")
    op.drop_column("app_user", "email_verified_at")
    op.drop_column("app_user", "password_hash")
