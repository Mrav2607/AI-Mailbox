"""Enforce one classification per message.

Replaces the non-unique ``classification_message_idx`` with a unique
constraint on ``message_id`` so concurrent classifiers can't double-insert and
so writes can upsert. Existing duplicates (from before this invariant) are
collapsed to the most recent row per message first, otherwise the constraint
can't be created.
"""

from alembic import op


revision = "0002_classification_msg_unique"
down_revision = "0001_init_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Collapse any pre-existing duplicates, keeping the newest row per message
    # (ties broken by id) so the unique constraint can be applied.
    op.execute(
        """
        DELETE FROM classification c
        USING classification newer
        WHERE c.message_id = newer.message_id
          AND (
              c.created_at < newer.created_at
              OR (c.created_at = newer.created_at AND c.id < newer.id)
          );
        """
    )
    op.drop_index("classification_message_idx", table_name="classification")
    op.create_unique_constraint(
        "uq_classification_message", "classification", ["message_id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_classification_message", "classification", type_="unique"
    )
    op.create_index("classification_message_idx", "classification", ["message_id"])
