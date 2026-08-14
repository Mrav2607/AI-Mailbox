"""Add label-sync control state to provider_account and mail_thread.

Label sync's reconciliation-first design (docs/plans/2026-08-13-label-sync-plan.md
§3.1/§4.1): ``provider_account.label_sync_enabled`` is the per-account opt-in,
``gmail_label_map`` caches Gmail's name->id map (JSON text, same precedent as
``outlook_delta_cursors``), and ``label_sync_generation`` fences a re-enable's
reconvergence against in-flight tasks from the old generation. On
``mail_thread``: ``synced_label``/``synced_message_id`` record what was
actually pushed (the applied pair), ``label_resync_needed`` forces
reconvergence on re-enable without erasing that pair, and the claim/lease
trio (``label_sync_claim_token``/``label_sync_claimed_at``) plus
``label_sync_pending_target`` implement the ordering guard in plan §3.8.

NO new index (Codex disposition 3 -- existing account-leading and
latest-message indexes suffice at O(10^3); don't add one absent a real
contrary EXPLAIN).

Downgrade PREFLIGHT (P1-6/P4-3, 0013/0023/0024 precedent): refuses with
RuntimeError + remediation SQL if ANY control state from this migration is
live, checked INDEPENDENTLY per guarded state (an account's enabled flag, a
non-null gmail_label_map, a non-zero generation, a populated applied pair on
any thread, a true label_resync_needed, a populated claim field, or a
populated pending target) -- destroying any one of these silently would lose
either the user's opt-in, the Outlook cleanup target, or the only record of
a possibly-applied-but-unconfirmed provider write.
"""

import sqlalchemy as sa
from alembic import op


revision = "0025_label_sync"
down_revision = "0024_thread_snooze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_account",
        sa.Column(
            "label_sync_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "provider_account", sa.Column("gmail_label_map", sa.Text(), nullable=True)
    )
    op.add_column(
        "provider_account",
        sa.Column(
            "label_sync_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column("mail_thread", sa.Column("synced_label", sa.Text(), nullable=True))
    op.add_column(
        "mail_thread", sa.Column("synced_message_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "mail_thread",
        sa.Column(
            "label_resync_needed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "mail_thread", sa.Column("label_sync_claim_token", sa.Text(), nullable=True)
    )
    op.add_column(
        "mail_thread",
        sa.Column(
            "label_sync_claimed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "mail_thread",
        sa.Column("label_sync_pending_target", sa.Text(), nullable=True),
    )


def _require_empty(check_sql: str, remediation_sql: str, label: str) -> None:
    if op.get_bind().execute(sa.text(check_sql)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade past {label}; live label-sync state would be "
            f"destroyed. Remediate before retrying:\n{remediation_sql}"
        )


def downgrade() -> None:
    _require_empty(
        "SELECT id FROM provider_account WHERE label_sync_enabled = true LIMIT 1",
        "-- Disable label sync on every account first, e.g.:\n"
        "UPDATE provider_account SET label_sync_enabled = false "
        "WHERE label_sync_enabled = true;",
        "0025_label_sync (an account has label_sync_enabled = true)",
    )
    _require_empty(
        "SELECT id FROM provider_account WHERE gmail_label_map IS NOT NULL LIMIT 1",
        "-- Export and clear the cached Gmail label map first, e.g.:\n"
        "-- COPY (SELECT id, gmail_label_map FROM provider_account "
        "WHERE gmail_label_map IS NOT NULL) TO '/tmp/gmail_label_map_backup.csv' "
        "CSV HEADER;\n"
        "UPDATE provider_account SET gmail_label_map = NULL "
        "WHERE gmail_label_map IS NOT NULL;",
        "0025_label_sync (an account has a cached gmail_label_map)",
    )
    _require_empty(
        "SELECT id FROM provider_account WHERE label_sync_generation != 0 LIMIT 1",
        "-- Reset the sync generation counter first, e.g.:\n"
        "UPDATE provider_account SET label_sync_generation = 0 "
        "WHERE label_sync_generation != 0;",
        "0025_label_sync (an account has a non-zero label_sync_generation)",
    )
    _require_empty(
        "SELECT id FROM mail_thread "
        "WHERE synced_label IS NOT NULL OR synced_message_id IS NOT NULL LIMIT 1",
        "-- A non-null applied pair means a label/category was actually "
        "pushed to the provider; clearing it loses the Outlook cleanup "
        "target and re-introduces drift with no memory of it.\n"
        "-- Export first, e.g.:\n"
        "-- COPY (SELECT id, synced_label, synced_message_id FROM mail_thread "
        "WHERE synced_label IS NOT NULL OR synced_message_id IS NOT NULL) "
        "TO '/tmp/mail_thread_synced_pair_backup.csv' CSV HEADER;\n"
        "UPDATE mail_thread SET synced_label = NULL, synced_message_id = NULL "
        "WHERE synced_label IS NOT NULL OR synced_message_id IS NOT NULL;",
        "0025_label_sync (mail_thread has a populated applied label pair)",
    )
    _require_empty(
        "SELECT id FROM mail_thread WHERE label_resync_needed = true LIMIT 1",
        "-- Clear pending resync flags first, e.g.:\n"
        "UPDATE mail_thread SET label_resync_needed = false "
        "WHERE label_resync_needed = true;",
        "0025_label_sync (mail_thread has label_resync_needed = true)",
    )
    _require_empty(
        "SELECT id FROM mail_thread "
        "WHERE label_sync_claim_token IS NOT NULL "
        "OR label_sync_claimed_at IS NOT NULL LIMIT 1",
        "-- A populated claim means a sync task may be mid-flight; wait for "
        "it to finish or clear it manually first, e.g.:\n"
        "UPDATE mail_thread SET label_sync_claim_token = NULL, "
        "label_sync_claimed_at = NULL "
        "WHERE label_sync_claim_token IS NOT NULL "
        "OR label_sync_claimed_at IS NOT NULL;",
        "0025_label_sync (mail_thread has a populated label-sync claim)",
    )
    _require_empty(
        "SELECT id FROM mail_thread "
        "WHERE label_sync_pending_target IS NOT NULL LIMIT 1",
        "-- A populated pending target is the only record of a possibly-"
        "applied-but-unconfirmed provider write; clearing it can orphan a "
        "category on the provider forever. Export first, e.g.:\n"
        "-- COPY (SELECT id, label_sync_pending_target FROM mail_thread "
        "WHERE label_sync_pending_target IS NOT NULL) "
        "TO '/tmp/mail_thread_pending_target_backup.csv' CSV HEADER;\n"
        "UPDATE mail_thread SET label_sync_pending_target = NULL "
        "WHERE label_sync_pending_target IS NOT NULL;",
        "0025_label_sync (mail_thread has a populated label_sync_pending_target)",
    )

    op.drop_column("mail_thread", "label_sync_pending_target")
    op.drop_column("mail_thread", "label_sync_claimed_at")
    op.drop_column("mail_thread", "label_sync_claim_token")
    op.drop_column("mail_thread", "label_resync_needed")
    op.drop_column("mail_thread", "synced_message_id")
    op.drop_column("mail_thread", "synced_label")

    op.drop_column("provider_account", "label_sync_generation")
    op.drop_column("provider_account", "gmail_label_map")
    op.drop_column("provider_account", "label_sync_enabled")
