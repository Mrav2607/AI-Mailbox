"""Tests for the agenda routes: list/counts visibility + ordering, status
transitions, and the backfill queue endpoint.

No live DB is available to this suite (see conftest.py's module docstring).
Statement-shape cases inspect the actual SQLAlchemy statement handed to a
mocked ``db.execute`` -- same style as test_mailbox_pagination.py -- since a
stub DB can't tell us what WHERE/ORDER BY a route actually built. Business-
logic cases (list assembly, status transitions, the reclassify/thread-done
hooks) use small hand-built fakes answering the exact calls a route makes,
mirroring test_mailbox.py's approach.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import actions


def _compiled(statement):
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.fixture
def client():
    """Empty-result DB stub: enough for the list's `.all()` and the counts
    aggregate's `.one()`, same shared-mock convention as
    test_mailbox_pagination.py's fixture."""
    user = MagicMock(id=uuid4())
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    db.execute.return_value.one.return_value = (0, 0)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db, user
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET "" -- validation
# ---------------------------------------------------------------------------


def test_list_invalid_status_is_422(client):
    c, _, _ = client
    resp = c.get("/api/v1/mail/actions?status=bogus")
    assert resp.status_code == 422
    assert "Invalid status" in resp.json()["detail"]


@pytest.mark.parametrize("limit", [0, 501])
def test_list_out_of_range_limit_is_422(client, limit):
    c, _, _ = client
    assert c.get(f"/api/v1/mail/actions?limit={limit}").status_code == 422


def test_list_default_status_and_empty_result(client):
    c, _, _ = client
    resp = c.get("/api/v1/mail/actions")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "counts": {"open": 0, "overdue": 0}}


@pytest.mark.parametrize("status", ["open", "done", "dismissed"])
def test_list_valid_status_values_pass_validation(client, status):
    c, _, _ = client
    resp = c.get(f"/api/v1/mail/actions?status={status}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET "" -- statement shape (visibility rule + ordering)
# ---------------------------------------------------------------------------


def test_list_statement_carries_visibility_predicates(client):
    c, db, user = client
    c.get("/api/v1/mail/actions?status=done")
    list_statement = db.execute.call_args_list[0].args[0]
    compiled = _compiled(list_statement)
    assert "action_item.outcome = 'extracted'" in compiled
    assert "classification.label IN ('needs_reply', 'action_required')" in compiled
    assert f"mail_thread.user_id = '{user.id.hex}'" in compiled
    assert "action_item.status = 'done'" in compiled


def test_list_statement_joins_classification_on_the_items_own_message(client):
    # The visibility join keys on the ITEM's source message, not the thread's
    # latest message -- an older item stays governed by its own message's
    # classification even after the thread's latest message is reclassified.
    c, db, _ = client
    c.get("/api/v1/mail/actions")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    assert "classification.message_id = action_item.message_id" in compiled


def test_list_statement_has_no_thread_done_filter(client):
    # Thread-done resolves items by write (mailbox.set_thread_done), never by
    # hiding them from this query.
    c, db, _ = client
    c.get("/api/v1/mail/actions")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    assert "done_at" not in compiled


def test_list_statement_orders_due_at_asc_nulls_last_then_created_desc_then_id_desc(client):
    c, db, _ = client
    c.get("/api/v1/mail/actions")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "action_item.due_at ASC NULLS LAST" in order_by_clause
    assert "action_item.created_at DESC" in order_by_clause
    assert "action_item.id DESC" in order_by_clause


def test_counts_statement_is_cross_account_and_status_independent(client):
    # counts must reflect the FULL visible set regardless of the status query
    # filter -- the second execute() call is the counts aggregate, and it must
    # not carry an ActionItem.status predicate the way the list query does.
    c, db, _ = client
    c.get("/api/v1/mail/actions?status=dismissed")
    counts_statement = db.execute.call_args_list[1].args[0]
    compiled = _compiled(counts_statement)
    assert "action_item.outcome = 'extracted'" in compiled
    assert "'dismissed'" not in compiled


def test_counts_overdue_requires_open_status_and_past_due_at(client):
    c, db, _ = client
    c.get("/api/v1/mail/actions")
    compiled = _compiled(db.execute.call_args_list[1].args[0])
    assert "action_item.status = 'open'" in compiled
    assert "action_item.due_at <" in compiled


# ---------------------------------------------------------------------------
# GET "" -- list assembly (account_email fallback, item shape)
# ---------------------------------------------------------------------------


def _make_action_item(**overrides):
    defaults = dict(
        id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        kind="payment",
        title="Pay invoice #429",
        due_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        due_precision="date",
        due_raw="by end of week",
        amount=42.5,
        currency="USD",
        source_confidence=0.9,
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _Row:
    """Stand-in for the Row a real `select(ActionItem, ...)` would return --
    attribute access by label, plus `.ActionItem` for the entity column."""

    def __init__(self, action_item, **labels):
        self.ActionItem = action_item
        for key, value in labels.items():
            setattr(self, key, value)


class _ListThenCountsDB:
    """Answers the list query's `.all()` first, then the counts aggregate's
    `.one()` -- the two execute() calls list_actions makes, in that order."""

    def __init__(self, rows, counts_row):
        self._rows = rows
        self._counts_row = counts_row
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        result = MagicMock()
        result.all.return_value = self._rows
        result.one.return_value = self._counts_row
        return result


def test_list_assembles_items_with_account_email_fallback(client, monkeypatch):
    c, _, user = client
    item_with_display_email = _make_action_item()
    item_with_fallback_only = _make_action_item(status="open")
    rows = [
        _Row(
            item_with_display_email,
            thread_subject="Invoice due",
            provider="gmail",
            sender="billing@example.com",
            display_email="owner@gmail.example",
            external_user_id="owner-external-id",
            label="action_required",
        ),
        _Row(
            item_with_fallback_only,
            thread_subject="RSVP",
            provider="outlook",
            sender="events@example.com",
            display_email=None,
            external_user_id="tid:oid",
            label="needs_reply",
        ),
    ]
    db = _ListThenCountsDB(rows, (2, 1))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get("/api/v1/mail/actions")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"] == {"open": 2, "overdue": 1}
    assert [item["account_email"] for item in body["items"]] == [
        "owner@gmail.example",
        "tid:oid",
    ]
    assert body["items"][0]["title"] == "Pay invoice #429"
    assert body["items"][0]["label"] == "action_required"


# ---------------------------------------------------------------------------
# POST "/{action_id}/status"
# ---------------------------------------------------------------------------


class _MutableItem:
    def __init__(self, status="open", status_at=None):
        self.status = status
        self.status_at = status_at


class _StatusDB:
    def __init__(self, item):
        self._item = item
        self.commits = 0

    def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._item
        return result

    def commit(self):
        self.commits += 1


@pytest.fixture
def status_client():
    user = MagicMock(id=uuid4())
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.clear()
    app.dependency_overrides.pop(get_db, None)


def test_status_invalid_status_is_422_before_any_db_access(status_client):
    db = _StatusDB(None)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post(
        f"/api/v1/mail/actions/{uuid4()}/status", json={"status": "not_a_status"}
    )
    assert resp.status_code == 422
    assert db.commits == 0


@pytest.mark.parametrize("reason", ["missing", "non_owner", "non_extracted"])
def test_status_404_when_row_not_visible_to_caller(status_client, reason):
    # All three collapse to the same query returning no row -- another
    # user's item, an unsettled (non-"extracted") item, and a missing id are
    # all indistinguishable to the caller, and 404 doesn't leak which.
    db = _StatusDB(None)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post(
        f"/api/v1/mail/actions/{uuid4()}/status", json={"status": "done"}
    )
    assert resp.status_code == 404


def test_status_marks_done_and_stamps_status_at(status_client):
    action_id = uuid4()
    item = _MutableItem(status="open", status_at=None)
    db = _StatusDB(item)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post(
        f"/api/v1/mail/actions/{action_id}/status", json={"status": "done"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["status_at"] is not None
    assert db.commits == 1


def test_status_reopen_clears_status_at(status_client):
    action_id = uuid4()
    item = _MutableItem(status="dismissed", status_at=datetime.now(timezone.utc))
    db = _StatusDB(item)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post(
        f"/api/v1/mail/actions/{action_id}/status", json={"status": "open"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "open"
    assert body["status_at"] is None


# ---------------------------------------------------------------------------
# POST "/backfill"
# ---------------------------------------------------------------------------


@pytest.fixture
def backfill_client():
    user = MagicMock(id=uuid4())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield user
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "params",
    ["limit=0", "limit=201", "since_days=0", "since_days=366"],
)
def test_backfill_out_of_range_params_are_422(backfill_client, params):
    resp = TestClient(app).post(f"/api/v1/mail/actions/backfill?{params}")
    assert resp.status_code == 422


def test_backfill_returns_409_when_extraction_disabled(backfill_client, monkeypatch):
    monkeypatch.setattr(actions, "extraction_available", lambda: False)
    resp = TestClient(app).post("/api/v1/mail/actions/backfill")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "action extraction disabled"


def test_backfill_queues_and_returns_202(backfill_client, monkeypatch):
    monkeypatch.setattr(actions, "extraction_available", lambda: True)
    fake_task = SimpleNamespace(id="task-123")
    captured = {}

    def fake_delay(**kwargs):
        captured.update(kwargs)
        return fake_task

    monkeypatch.setattr(actions.extract_actions_for_user, "delay", fake_delay)
    resp = TestClient(app).post(
        "/api/v1/mail/actions/backfill?limit=50&force=true&since_days=10"
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_id": "task-123"}
    assert captured["user_id"] == str(backfill_client.id)
    assert captured["limit"] == 50
    assert captured["force"] is True
    assert captured["since_days"] == 10


def test_backfill_is_rate_limited_at_three_per_window(backfill_client, monkeypatch):
    monkeypatch.setattr(actions, "extraction_available", lambda: True)
    monkeypatch.setattr(
        actions.extract_actions_for_user, "delay", lambda **kw: SimpleNamespace(id="t")
    )
    client_ = TestClient(app)
    for _ in range(3):
        assert client_.post("/api/v1/mail/actions/backfill").status_code == 202
    resp = client_.post("/api/v1/mail/actions/backfill")
    assert resp.status_code == 429
