"""Label sync's API surface (docs/plans/2026-08-13-label-sync-plan.md
§3.1/§3.3/§4.2), offline only -- MagicMock db, no live database or network.

Covers Wave 2's surface: the connections PATCH's enable-time failure
contract, idempotent re-enable/disable semantics, the drift count exposed
on connections rows, non-owned 404, and the reclassify route's label-sync
post-commit enqueue (fires only when enabled, isolated both directions from
the extraction enqueue).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox as mailbox_routes

CONNECTIONS_URL = "/api/v1/auth/connections"


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


USER_ID = uuid4()


def _account(**overrides):
    defaults = dict(
        id=uuid4(),
        user_id=USER_ID,
        provider="gmail",
        created_at=datetime.now(timezone.utc),
        external_user_id="user@gmail.example",
        display_email=None,
        sync_paused_at=None,
        refresh_token="a-refresh-token",
        scope="https://www.googleapis.com/auth/gmail.modify",
        label_sync_enabled=False,
        label_sync_generation=0,
        gmail_label_map=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def auth_client():
    user = SimpleNamespace(id=USER_ID)
    db = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _patch(api_client, connection_id, enabled: bool):
    return api_client.patch(
        f"{CONNECTIONS_URL}/{connection_id}", json={"label_sync_enabled": enabled}
    )


# ---------------------------------------------------------------------------
# 404 / ownership
# ---------------------------------------------------------------------------


def test_patch_connection_404s_for_an_unknown_id(auth_client):
    api_client, db = auth_client
    db.get.return_value = None

    resp = _patch(api_client, uuid4(), True)

    assert resp.status_code == 404
    db.commit.assert_not_called()


def test_patch_connection_404s_for_another_users_account(auth_client):
    api_client, db = auth_client
    someone_elses = _account(user_id=uuid4())
    db.get.return_value = someone_elses

    resp = _patch(api_client, someone_elses.id, True)

    assert resp.status_code == 404
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotent PATCH semantics (plan §3.3): a repeat of the current state is a
# pure echo -- no lock, no generation bump, no marker changes.
# ---------------------------------------------------------------------------


def test_patch_connection_idempotent_reenable_is_a_pure_echo(auth_client):
    api_client, db = auth_client
    account = _account(label_sync_enabled=True, label_sync_generation=3)
    db.get.return_value = account
    drift_result = MagicMock()
    drift_result.scalar_one.return_value = 2
    db.execute.return_value = drift_result

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 200
    body = resp.json()
    assert body["label_sync_enabled"] is True
    assert body["label_sync_drift"] == 2
    # No generation bump, no lock, no marker writes -- a true no-op. The
    # single execute() call is the read-only drift count the echo still
    # needs (the account IS enabled), not a lock or bulk update.
    assert account.label_sync_generation == 3
    assert db.execute.call_count == 1
    db.commit.assert_not_called()


def test_patch_connection_idempotent_redisable_is_a_pure_echo(auth_client):
    api_client, db = auth_client
    account = _account(label_sync_enabled=False)
    db.get.return_value = account

    resp = _patch(api_client, account.id, False)

    assert resp.status_code == 200
    assert resp.json()["label_sync_enabled"] is False
    db.execute.assert_not_called()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# DISABLE (true -> false): just clears the flag, no marker/provider changes.
# ---------------------------------------------------------------------------


def test_patch_connection_disable_only_clears_the_flag(auth_client):
    api_client, db = auth_client
    account = _account(label_sync_enabled=True, label_sync_generation=2, gmail_label_map="{}")
    db.get.return_value = account

    resp = _patch(api_client, account.id, False)

    assert resp.status_code == 200
    body = resp.json()
    assert body["label_sync_enabled"] is False
    assert body["label_sync_drift"] is None
    assert account.label_sync_enabled is False
    # Untouched: disable never bumps generation or clears the map.
    assert account.label_sync_generation == 2
    assert account.gmail_label_map == "{}"
    db.execute.assert_not_called()  # no lock, no bulk update needed
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# ENABLE-time failure contract (plan §3.3) -- each check fails WITHOUT
# persisting the flag.
# ---------------------------------------------------------------------------


def test_patch_connection_enable_fails_on_missing_scope(auth_client):
    api_client, db = auth_client
    account = _account(scope="https://www.googleapis.com/auth/gmail.readonly")
    db.get.return_value = account
    db.execute.return_value = MagicMock()  # the advisory-lock call and reread share this
    db.execute.return_value.scalar_one_or_none.return_value = account

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"
    assert account.label_sync_enabled is False
    db.commit.assert_not_called()


def test_patch_connection_enable_fails_when_no_refresh_token(auth_client):
    api_client, db = auth_client
    account = _account(refresh_token=None)
    db.get.return_value = account
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = account

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reauth_required"
    assert account.label_sync_enabled is False
    db.commit.assert_not_called()


def test_patch_connection_enable_fails_when_paused(auth_client):
    api_client, db = auth_client
    account = _account(sync_paused_at=datetime.now(timezone.utc))
    db.get.return_value = account
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = account

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_paused"
    assert account.label_sync_enabled is False
    db.commit.assert_not_called()


def test_patch_connection_enable_fails_when_busy_with_an_unexpired_claim(auth_client):
    api_client, db = auth_client
    account = _account()
    db.get.return_value = account
    lock_result = MagicMock()
    reread_result = MagicMock()
    reread_result.scalar_one_or_none.return_value = account
    busy_result = MagicMock()
    busy_result.first.return_value = (uuid4(),)  # a live claim on some thread
    db.execute.side_effect = [lock_result, reread_result, busy_result]

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "label_sync_busy"
    assert body["detail"]["message"] == "a sync is finishing — try again in a few minutes"
    assert account.label_sync_enabled is False
    assert account.label_sync_generation == 0
    db.commit.assert_not_called()

    # The busy check's window matches Wave 1's CLAIM_LEASE exactly. It's
    # the 3rd execute() call now: lock, the post-lock reread, then busy.
    busy_stmt = db.execute.call_args_list[2].args[0]
    compiled = _compiled(busy_stmt)
    assert "label_sync_claim_token IS NOT NULL" in compiled
    assert "label_sync_claimed_at >" in compiled


def test_patch_connection_enable_never_persists_true_when_a_check_fails(auth_client):
    # Every failure path must leave the flag untouched -- assert it directly
    # against the mutable account object rather than trusting the response.
    api_client, db = auth_client
    account = _account(sync_paused_at=datetime.now(timezone.utc))
    db.get.return_value = account
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = account

    _patch(api_client, account.id, True)

    assert account.label_sync_enabled is False


# ---------------------------------------------------------------------------
# L-2: the account row read via db.get() (before the advisory lock) must
# never be what the eligibility checks run against -- a state change that
# commits WHILE this request is blocked on the lock (e.g. ingest pausing the
# account on an invalid_grant) has to be caught by a fresh, locked reread.
# ---------------------------------------------------------------------------


def test_patch_connection_enable_catches_a_pause_committed_while_waiting_on_the_lock(auth_client):
    api_client, db = auth_client
    # Two DISTINCT objects: what the pre-lock db.get() saw (unpaused), and
    # what the post-lock reread sees (paused) -- a fake-db double standing
    # in for "the row changed underneath us between those two reads".
    pre_lock_account = _account(sync_paused_at=None)
    paused_reread_account = _account(
        id=pre_lock_account.id, sync_paused_at=datetime.now(timezone.utc)
    )
    db.get.return_value = pre_lock_account
    lock_result = MagicMock()
    reread_result = MagicMock()
    reread_result.scalar_one_or_none.return_value = paused_reread_account
    db.execute.side_effect = [lock_result, reread_result]

    resp = _patch(api_client, pre_lock_account.id, True)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_paused"
    # Neither snapshot's flag was ever flipped true.
    assert pre_lock_account.label_sync_enabled is False
    assert paused_reread_account.label_sync_enabled is False
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# ENABLE success: generation bump, map clear, resync marking, pair
# retention, all under the shared advisory lock.
# ---------------------------------------------------------------------------


def test_patch_connection_enable_success_bumps_generation_and_clears_map(auth_client):
    api_client, db = auth_client
    account = _account(label_sync_generation=4, gmail_label_map='{"CortexMail": "Label_1"}')
    db.get.return_value = account
    lock_result = MagicMock()
    reread_result = MagicMock()
    reread_result.scalar_one_or_none.return_value = account
    busy_result = MagicMock()
    busy_result.first.return_value = None  # nothing busy
    update_result = MagicMock()
    drift_result = MagicMock()
    drift_result.scalar_one.return_value = 7
    db.execute.side_effect = [lock_result, reread_result, busy_result, update_result, drift_result]

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 200
    body = resp.json()
    assert body["label_sync_enabled"] is True
    assert body["label_sync_drift"] == 7
    assert account.label_sync_enabled is True
    assert account.label_sync_generation == 5
    assert account.gmail_label_map is None
    db.commit.assert_called_once()

    # 5 execute calls: advisory lock, the post-lock reread, busy check,
    # resync-marking bulk update, drift count -- in that order.
    assert db.execute.call_count == 5
    lock_call = db.execute.call_args_list[0]
    assert "pg_advisory_xact_lock" in str(lock_call.args[0])


def test_patch_connection_enable_marks_resync_and_retains_the_applied_pair(auth_client):
    # The resync-marking UPDATE must set label_resync_needed but must NEVER
    # touch synced_label/synced_message_id -- that pair is the Outlook
    # cleanup target and re-enable retains it (plan §3.3/P3-2).
    api_client, db = auth_client
    account = _account()
    db.get.return_value = account
    lock_result = MagicMock()
    reread_result = MagicMock()
    reread_result.scalar_one_or_none.return_value = account
    busy_result = MagicMock()
    busy_result.first.return_value = None
    update_result = MagicMock()
    drift_result = MagicMock()
    drift_result.scalar_one.return_value = 0
    db.execute.side_effect = [lock_result, reread_result, busy_result, update_result, drift_result]

    _patch(api_client, account.id, True)

    update_stmt = db.execute.call_args_list[3].args[0]
    compiled = _compiled(update_stmt)
    assert "mail_thread SET label_resync_needed=true" in compiled
    assert "synced_label" not in compiled.split("SET")[1].split("WHERE")[0]
    assert "synced_message_id" not in compiled.split("SET")[1].split("WHERE")[0]
    # The WHERE clause covers a live classification OR a populated applied
    # pair (either half) -- exactly the plan's re-enable predicate.
    where_clause = compiled.split("WHERE", 1)[1]
    assert "synced_label IS NOT NULL" in where_clause
    assert "synced_message_id IS NOT NULL" in where_clause


def test_patch_connection_enable_success_for_outlook_uses_outlook_scope_token(auth_client):
    api_client, db = auth_client
    account = _account(
        provider="outlook",
        scope="offline_access Mail.ReadWrite",
        external_user_id="tid:oid",
        display_email="user@outlook.example",
    )
    db.get.return_value = account
    lock_result = MagicMock()
    reread_result = MagicMock()
    reread_result.scalar_one_or_none.return_value = account
    busy_result = MagicMock()
    busy_result.first.return_value = None
    update_result = MagicMock()
    drift_result = MagicMock()
    drift_result.scalar_one.return_value = 0
    db.execute.side_effect = [lock_result, reread_result, busy_result, update_result, drift_result]

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 200
    assert account.label_sync_enabled is True


def test_patch_connection_enable_fails_for_outlook_missing_mail_readwrite(auth_client):
    api_client, db = auth_client
    account = _account(provider="outlook", scope="offline_access Mail.Send")
    db.get.return_value = account
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = account

    resp = _patch(api_client, account.id, True)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"


# ---------------------------------------------------------------------------
# Drift count exposure on connections rows (plan §3.4/§3.6).
# ---------------------------------------------------------------------------


def test_connections_list_carries_drift_count_for_enabled_accounts(auth_client):
    api_client, db = auth_client
    enabled = _account(label_sync_enabled=True)
    disabled = _account(label_sync_enabled=False)
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        enabled,
        disabled,
    ]
    drift_result = MagicMock()
    drift_result.scalar_one.return_value = 3
    db.execute.return_value = drift_result

    body = api_client.get(CONNECTIONS_URL).json()

    rows = {row["id"]: row for row in body["connections"]}
    assert rows[str(enabled.id)]["label_sync_drift"] == 3
    assert rows[str(disabled.id)]["label_sync_drift"] is None
    # Only the enabled account's drift count was ever queried.
    assert db.execute.call_count == 1


# ---------------------------------------------------------------------------
# Reclassify route's label-sync post-commit enqueue (plan §3.1/§4.2) --
# fires only when enabled, isolated both directions from the extraction
# enqueue. Self-contained fake session, deliberately not sharing
# test_mailbox.py's _ReclassifyDB (different file's test double, not a
# shared fixture module).
# ---------------------------------------------------------------------------


class _ReclassifyIsolationDB:
    def __init__(self, thread, message, *, label_sync_enabled, label_sync_lookup_should_raise=False):
        self.thread = thread
        self.message = message
        self.label_sync_enabled = label_sync_enabled
        self.label_sync_lookup_should_raise = label_sync_lookup_should_raise

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        compiled = str(statement)
        result = MagicMock()
        if "FROM mail_message" in compiled:
            result.scalars.return_value.first.return_value = self.message
        elif "FROM provider_account" in compiled:
            # Post-commit eligibility lookup (L1 regression coverage): the
            # session's `thread` row is already expired by the commit above,
            # so this is a fresh SELECT, not a lazy-load off the stale ORM
            # object -- simulate it raising (e.g. a raced account deletion)
            # to prove that no longer 500s a committed reclassify.
            if self.label_sync_lookup_should_raise:
                raise RuntimeError("connection dropped")
            result.scalar_one_or_none.return_value = self.label_sync_enabled
        elif "FROM classification" in compiled:
            result.scalars.return_value.first.return_value = None
        elif "INSERT INTO classification " in compiled:
            result.rowcount = 1
        return result

    def commit(self):
        pass


class _FakeTask:
    def __init__(self, *, should_raise=False):
        self.should_raise = should_raise
        self.calls: list[str] = []

    def delay(self, arg):
        self.calls.append(arg)
        if self.should_raise:
            raise RuntimeError("broker down")


def _reclassify_isolation_setup(monkeypatch, *, label_sync_enabled, extraction_should_raise=False,
                                 label_sync_should_raise=False, label_sync_lookup_should_raise=False):
    user = SimpleNamespace(id=uuid4())
    thread_id = uuid4()
    message_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id, user_id=user.id, subject="Renewal notice", provider_account_id=uuid4()
    )
    message = SimpleNamespace(id=message_id, snippet="snip", body_text="body")
    db = _ReclassifyIsolationDB(
        thread,
        message,
        label_sync_enabled=label_sync_enabled,
        label_sync_lookup_should_raise=label_sync_lookup_should_raise,
    )
    extraction_task = _FakeTask(should_raise=extraction_should_raise)
    label_sync_task = _FakeTask(should_raise=label_sync_should_raise)

    monkeypatch.setattr(mailbox_routes, "extraction_available", lambda db, user_id: True)
    monkeypatch.setattr(mailbox_routes, "extract_action_for_message", extraction_task)
    monkeypatch.setattr(mailbox_routes, "sync_thread_labels", label_sync_task)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), extraction_task, label_sync_task, thread_id, message_id


def test_reclassify_enqueues_label_sync_only_when_account_is_enabled(monkeypatch):
    client, _extraction, label_sync_task, thread_id, _message_id = _reclassify_isolation_setup(
        monkeypatch, label_sync_enabled=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert label_sync_task.calls == [str(thread_id)]


def test_reclassify_skips_label_sync_enqueue_when_account_is_disabled(monkeypatch):
    client, _extraction, label_sync_task, thread_id, _message_id = _reclassify_isolation_setup(
        monkeypatch, label_sync_enabled=False
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert label_sync_task.calls == []


def test_reclassify_label_sync_enqueue_survives_an_extraction_broker_failure(monkeypatch):
    client, extraction_task, label_sync_task, thread_id, message_id = _reclassify_isolation_setup(
        monkeypatch, label_sync_enabled=True, extraction_should_raise=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    # The extraction enqueue's own failure is swallowed (existing
    # behavior) -- it must never suppress the label-sync enqueue below it.
    assert resp.status_code == 200
    assert extraction_task.calls == [str(message_id)]
    assert label_sync_task.calls == [str(thread_id)]


def test_reclassify_extraction_enqueue_survives_a_label_sync_broker_failure(monkeypatch):
    client, extraction_task, label_sync_task, thread_id, message_id = _reclassify_isolation_setup(
        monkeypatch, label_sync_enabled=True, label_sync_should_raise=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    # A dead broker on the label-sync side must never turn a successful
    # reclassify into a 500, and the extraction enqueue that already ran
    # must not be undone by it.
    assert resp.status_code == 200
    assert extraction_task.calls == [str(message_id)]
    assert label_sync_task.calls == [str(thread_id)]


def test_reclassify_survives_a_raising_post_commit_eligibility_lookup(monkeypatch):
    # L1 regression: the eligibility SELECT and the .delay() call both live
    # inside the same try/except now, so a raise from the lookup itself
    # (not just the enqueue) must still return 200, not 500.
    client, extraction_task, label_sync_task, thread_id, message_id = _reclassify_isolation_setup(
        monkeypatch, label_sync_enabled=True, label_sync_lookup_should_raise=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert extraction_task.calls == [str(message_id)]
    assert label_sync_task.calls == []
