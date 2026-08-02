from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import MailMessage
from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox


def test_triage_items_include_latest_message_sender_and_account_email(monkeypatch):
    thread_id = uuid4()
    message_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
        provider_account_id=account_id,
    )
    message = SimpleNamespace(
        id=message_id,
        thread_id=thread_id,
        snippet="The latest details",
        sender='"Ada Lovelace" <ada@example.com>',
    )
    captured = {}

    def latest_messages(db, thread_ids, columns):
        captured["thread_ids"] = thread_ids
        captured["columns"] = columns
        return {thread_id: message}

    monkeypatch.setattr(
        mailbox,
        "latest_messages_by_thread",
        latest_messages,
    )

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class DB:
        def execute(self, statement):
            # The classification lookup queries with no rows to return; the
            # account-email lookup is the one that matters here. Row shape is
            # (id, display_email, external_user_id) -- display_email wins
            # when set.
            if "provider_account" in str(statement):
                return Result([(account_id, None, "owner@gmail.example")])
            return Result([])

    [item] = mailbox._assemble_triage_items(DB(), [thread])

    assert captured["thread_ids"] == [thread_id]
    assert MailMessage.sender in captured["columns"]
    assert item["latest_message_sender"] == '"Ada Lovelace" <ada@example.com>'
    assert item["account_email"] == "owner@gmail.example"


def test_triage_account_email_prefers_display_email_over_external_user_id(monkeypatch):
    # Outlook's external_user_id is a stable tid:oid, not an email -- when a
    # display_email is on file it must win over the identity fallback.
    thread_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
        provider_account_id=account_id,
    )
    monkeypatch.setattr(
        mailbox, "latest_messages_by_thread", lambda db, thread_ids, columns: {}
    )

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class DB:
        def execute(self, statement):
            if "provider_account" in str(statement):
                return Result([(account_id, "user@outlook.example", "tid:oid")])
            return Result([])

    [item] = mailbox._assemble_triage_items(DB(), [thread])

    assert item["account_email"] == "user@outlook.example"


# ---------------------------------------------------------------------------
# reclassify_thread's action-extraction enqueue hook
# ---------------------------------------------------------------------------


class _ReclassifyDB:
    """Fake session backing reclassify_thread: db.get resolves the owned
    thread, db.execute resolves the latest-message lookup (and swallows
    upsert_classification's own insert plus the extracted->ineligible
    invalidation UPDATE, both recorded in `statements`), and commit() is
    timestamped into `events` so tests can assert the enqueue happens
    strictly after it."""

    def __init__(self, thread, message, events):
        self.thread = thread
        self.message = message
        self.events = events
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        result.scalars.return_value.first.return_value = self.message
        return result

    def commit(self):
        self.events.append("commit")


class _FakeExtractionTask:
    """Stand-in for extract_action_for_message: records every enqueue (and
    the shared `events` list records where it landed relative to commit())."""

    def __init__(self, events, *, should_raise=False):
        self.events = events
        self.should_raise = should_raise
        self.calls = []

    def delay(self, message_id):
        self.calls.append(message_id)
        self.events.append("delay")
        if self.should_raise:
            raise RuntimeError("broker down")


def _reclassify_setup(monkeypatch, *, extraction_available, should_raise=False):
    user = MagicMock(id=uuid4())
    thread_id = uuid4()
    message_id = uuid4()
    thread = SimpleNamespace(id=thread_id, user_id=user.id)
    message = SimpleNamespace(id=message_id)
    events: list[str] = []
    db = _ReclassifyDB(thread, message, events)
    fake_task = _FakeExtractionTask(events, should_raise=should_raise)

    monkeypatch.setattr(
        mailbox, "extraction_available", lambda db, user_id: extraction_available
    )
    monkeypatch.setattr(mailbox, "extract_action_for_message", fake_task)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), fake_task, events, thread_id, message_id, db


def test_reclassify_enqueues_extraction_strictly_after_commit(monkeypatch):
    client, fake_task, events, thread_id, message_id, _db = _reclassify_setup(
        monkeypatch, extraction_available=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert fake_task.calls == [str(message_id)]
    assert events == ["commit", "delay"]


def test_reclassify_skips_enqueue_for_a_non_action_label(monkeypatch):
    client, fake_task, _events, thread_id, _message_id, _db = _reclassify_setup(
        monkeypatch, extraction_available=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert fake_task.calls == []


def test_reclassify_skips_enqueue_when_extraction_unavailable(monkeypatch):
    client, fake_task, _events, thread_id, _message_id, _db = _reclassify_setup(
        monkeypatch, extraction_available=False
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert fake_task.calls == []


def test_reclassify_broker_failure_is_swallowed_and_response_still_succeeds(monkeypatch):
    client, fake_task, events, thread_id, _message_id, _db = _reclassify_setup(
        monkeypatch, extraction_available=True, should_raise=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "needs_reply"},
        )
    finally:
        app.dependency_overrides.clear()

    # The override already committed -- a dead broker must never turn a
    # successful reclassify into a 500.
    assert resp.status_code == 200
    assert resp.json()["classification"]["label"] == "needs_reply"
    assert events == ["commit", "delay"]


def test_reclassify_away_invalidates_a_settled_extracted_row(monkeypatch):
    # Reclassifying AWAY from an action label must make a previously
    # `extracted` row claimable again -- otherwise a later back-transition's
    # enqueued task finds the terminal row and skips it, silently re-showing
    # the stale extraction instead of re-extracting.
    client, fake_task, _events, thread_id, message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    # statements: [0] latest-message select, [1] classification upsert,
    # [2] the extracted->ineligible invalidation UPDATE.
    assert len(db.statements) == 3
    compiled = str(
        db.statements[2].compile(compile_kwargs={"literal_binds": True})
    )
    assert "UPDATE action_item" in compiled
    assert f"action_item.message_id = '{message_id.hex}'" in compiled
    assert "action_item.outcome = 'extracted'" in compiled
    assert "outcome='ineligible'" in compiled.replace(" ", "")
    # Never touches status/status_at -- an operator's done/dismissed must
    # survive the round-trip.
    assert "status" not in compiled
    assert fake_task.calls == []


def test_reclassify_to_action_label_issues_no_invalidation_update(monkeypatch):
    client, fake_task, _events, thread_id, message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=True
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    # Only the latest-message select and the classification upsert -- no
    # third (invalidation) statement.
    assert len(db.statements) == 2
    assert fake_task.calls == [str(message_id)]


# ---------------------------------------------------------------------------
# set_thread_done's thread-lock + action-item resolution
# ---------------------------------------------------------------------------


class _ThreadDoneDB:
    """Fake session backing set_thread_done: db.get resolves the owned
    thread, and every db.execute() call (the locked done_at re-read, then
    the bulk ActionItem update) is recorded in call order. The locked
    re-read defaults to None -- "no one beat us to it", the plain
    single-request case."""

    def __init__(self, thread, *, locked_done_at=None):
        self.thread = thread
        self.locked_done_at = locked_done_at
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        result.scalar_one.return_value = self.locked_done_at
        return result

    def commit(self):
        pass


def _thread_done_client(db, user_id):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=user_id)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _compiled(statement):
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_thread_done_locks_thread_before_bulk_resolving_action_items():
    user_id = uuid4()
    thread_id = uuid4()
    thread = SimpleNamespace(id=thread_id, user_id=user_id, done_at=None)
    db = _ThreadDoneDB(thread)
    client = _thread_done_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(db.statements) == 2
    lock_statement, update_statement = db.statements
    assert "FOR UPDATE" in _compiled(lock_statement)
    assert f"mail_thread.id = '{thread_id.hex}'" in _compiled(lock_statement)

    compiled_update = _compiled(update_statement)
    assert "UPDATE action_item" in compiled_update
    assert f"action_item.thread_id = '{thread_id.hex}'" in compiled_update
    # Every OPEN item resolves regardless of outcome (pending included) --
    # the WHERE clause must not mention outcome at all.
    assert "action_item.status = 'open'" in compiled_update
    assert "outcome" not in compiled_update
    assert "status='done'" in compiled_update.replace(" ", "")


def test_thread_done_locked_reread_keeps_the_winners_done_at():
    # Two concurrent done=true requests both read the thread pre-lock with
    # done_at=None. This request loses the race for the FOR UPDATE lock --
    # by the time it acquires it, the winner already committed a done_at.
    # The locked re-read must see that and must NOT overwrite it with this
    # request's own `now`.
    user_id = uuid4()
    thread_id = uuid4()
    winners_done_at = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    thread = SimpleNamespace(id=thread_id, user_id=user_id, done_at=None)
    db = _ThreadDoneDB(thread, locked_done_at=winners_done_at)
    client = _thread_done_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lock_statement, update_statement = db.statements
    assert "FOR UPDATE" in _compiled(lock_statement)
    assert "mail_thread.done_at" in _compiled(lock_statement)
    # The bulk resolve still runs (idempotent no-op against already-resolved
    # rows), but done_at itself keeps the winner's original timestamp.
    assert "UPDATE action_item" in _compiled(update_statement)
    assert thread.done_at == winners_done_at


def test_thread_done_is_idempotent_and_skips_the_bulk_update_when_already_done():
    user_id = uuid4()
    thread_id = uuid4()
    already_done_at = datetime.now(timezone.utc)
    thread = SimpleNamespace(id=thread_id, user_id=user_id, done_at=already_done_at)
    db = _ThreadDoneDB(thread)
    client = _thread_done_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert db.statements == []
    assert resp.json()["done_at"] is not None


def test_thread_undone_does_not_touch_action_items():
    user_id = uuid4()
    thread_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id, user_id=user_id, done_at=datetime.now(timezone.utc)
    )
    db = _ThreadDoneDB(thread)
    client = _thread_done_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": False}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["done"] is False
    assert db.statements == []


def test_thread_done_404s_for_another_users_thread():
    thread = SimpleNamespace(id=uuid4(), user_id=uuid4(), done_at=None)
    db = _ThreadDoneDB(thread)
    client = _thread_done_client(db, uuid4())
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /mail/counts -- actions extension
# ---------------------------------------------------------------------------


def test_counts_includes_actions_and_ignores_the_account_filter(monkeypatch):
    user = MagicMock(id=uuid4())
    captured = {}

    def fake_compute_action_counts(db, user_id):
        captured["user_id"] = user_id
        return {"open": 4, "overdue": 1}

    monkeypatch.setattr(mailbox, "compute_action_counts", fake_compute_action_counts)

    db = MagicMock()
    db.execute.return_value.all.return_value = []
    db.execute.return_value.scalar_one.return_value = 0
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get(
            f"/api/v1/mail/counts?provider_account_id={uuid4()}"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["actions"] == {"open": 4, "overdue": 1}
    # Always cross-account: compute_action_counts gets the user id only, no
    # account scoping, even though the request set provider_account_id.
    assert captured["user_id"] == user.id
