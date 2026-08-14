from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import MailMessage
from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox
from app.services.nlp.classifier import build_classification_text
from app.services.nlp.persistence import FEEDBACK_TEXT_MAX


def test_triage_items_include_latest_message_sender_and_account_email(monkeypatch):
    thread_id = uuid4()
    message_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
        provider_account_id=account_id,
        replied_at=None,
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
        replied_at=None,
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
    """Fake session backing reclassify_thread. Dispatches db.execute() by
    statement shape instead of returning one canned result for every call --
    the route issues up to four statements per request now (locked
    latest-message select, prior-classification select, feedback insert,
    classification upsert), plus an optional fifth (extracted->ineligible
    invalidation UPDATE) and a sixth post-commit (the label-sync-enabled
    lookup, plan §3.1), and a single fake result would silently make every
    read return whatever this stub was last told to hand back. Every
    statement is recorded in `statements` (in call order) regardless, and
    commit() is timestamped into `events` so tests can assert the extraction
    enqueue happens strictly after it.
    """

    def __init__(
        self, thread, message, events, *, prior_classification=None, label_sync_enabled=False
    ):
        self.thread = thread
        self.message = message
        self.events = events
        self.prior_classification = prior_classification
        self.label_sync_enabled = label_sync_enabled
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        compiled = str(statement)
        result = MagicMock()
        if "FROM mail_message" in compiled:
            result.scalars.return_value.first.return_value = self.message
        elif "FROM provider_account" in compiled:
            result.scalar_one_or_none.return_value = self.label_sync_enabled
        elif "FROM classification" in compiled:
            result.scalars.return_value.first.return_value = self.prior_classification
        elif "INSERT INTO classification " in compiled:
            # The upsert -- rowcount 1 reads as "written", not "protected".
            result.rowcount = 1
        return result

    def commit(self):
        self.events.append("commit")


def _statement_matching(db, substring):
    """The one statement in db.statements whose SQL contains `substring` --
    fails loudly on zero or multiple matches rather than silently grabbing
    the wrong index, since the exact statement count/order here is precisely
    what several of these tests exist to pin down."""
    matches = [s for s in db.statements if substring in str(s)]
    assert len(matches) == 1, f"expected exactly one match for {substring!r}, got {len(matches)}"
    return matches[0]


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


def _reclassify_setup(
    monkeypatch,
    *,
    extraction_available,
    should_raise=False,
    prior_classification=None,
    subject="Renewal notice",
    snippet="Your plan renews soon",
    body_text="Full message body.",
):
    user = MagicMock(id=uuid4())
    thread_id = uuid4()
    message_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id, user_id=user.id, subject=subject, provider_account_id=uuid4()
    )
    message = SimpleNamespace(id=message_id, snippet=snippet, body_text=body_text)
    events: list[str] = []
    db = _ReclassifyDB(thread, message, events, prior_classification=prior_classification)
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
    # statements: [0] locked latest-message select, [1] prior-classification
    # select, [2] feedback insert, [3] classification upsert, [4] the
    # extracted->ineligible invalidation UPDATE, [5] the post-commit
    # label-sync-enabled lookup (plan §3.1, always runs regardless of label).
    assert len(db.statements) == 6
    compiled = str(
        db.statements[4].compile(compile_kwargs={"literal_binds": True})
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
    # Locked latest-message select, prior-classification select, feedback
    # insert, classification upsert, then the post-commit label-sync-enabled
    # lookup -- no invalidation statement (this label stays an action label).
    assert len(db.statements) == 5
    assert fake_task.calls == [str(message_id)]


def test_reclassify_locks_the_latest_message_row_for_update(monkeypatch):
    # The row lock is what makes the prior-read -> insert -> upsert sequence
    # race-safe against a second concurrent reclassify on the same thread.
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lock_statement = db.statements[0]
    compiled = str(lock_statement)
    assert "FROM mail_message" in compiled
    assert "FOR UPDATE" in compiled


def test_reclassify_feedback_row_carries_prior_snapshot(monkeypatch):
    prior = SimpleNamespace(label="fyi", confidence=0.62, model_version="local-v3")
    client, _fake_task, _events, thread_id, message_id, db = _reclassify_setup(
        monkeypatch,
        extraction_available=False,
        prior_classification=prior,
        subject="Renewal notice",
        snippet="Your plan renews soon",
        body_text="Full message body.",
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "action_required"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    params = _statement_matching(
        db, "INSERT INTO classification_feedback"
    ).compile().params
    assert params["message_id"] == message_id
    assert params["user_id"] == db.thread.user_id
    assert params["prior_label"] == "fyi"
    assert params["prior_confidence"] == 0.62
    assert params["prior_model_version"] == "local-v3"
    assert params["new_label"] == "action_required"
    assert params["input_text"] == build_classification_text(
        "Renewal notice", "Your plan renews soon", "Full message body."
    )


def test_reclassify_feedback_prior_is_none_for_unclassified_message(monkeypatch):
    client, _fake_task, _events, thread_id, message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False, prior_classification=None
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    params = _statement_matching(
        db, "INSERT INTO classification_feedback"
    ).compile().params
    assert params["message_id"] == message_id
    assert params["prior_label"] is None
    assert params["prior_confidence"] is None
    assert params["prior_model_version"] is None
    assert params["new_label"] == "fyi"


def test_reclassify_override_of_override_appends_with_previous_override_as_prior(
    monkeypatch,
):
    # The operator corrects their own earlier override to a different label --
    # this must be recorded (not suppressed), with prior_* reflecting that
    # earlier override, not a model prediction.
    prior_override = SimpleNamespace(label="fyi", confidence=1.0, model_version="user-override")
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False, prior_classification=prior_override
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "spam"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    params = _statement_matching(
        db, "INSERT INTO classification_feedback"
    ).compile().params
    assert params["prior_label"] == "fyi"
    assert params["prior_confidence"] == 1.0
    assert params["prior_model_version"] == "user-override"
    assert params["new_label"] == "spam"


def test_reclassify_exact_repeat_of_operator_override_is_suppressed(monkeypatch):
    # Same label as the operator's own last override -- a double-click/stuck-
    # key repeat, not a new correction. No feedback row, but the upsert still
    # runs (harmless no-op write against the classification table).
    prior_override = SimpleNamespace(label="fyi", confidence=1.0, model_version="user-override")
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False, prior_classification=prior_override
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert not any(
        "INSERT INTO classification_feedback" in str(s) for s in db.statements
    )
    # The classification upsert still ran.
    assert any(
        "INSERT INTO classification " in str(s) for s in db.statements
    )


def test_reclassify_input_text_truncated_at_feedback_text_max(monkeypatch):
    long_body = "x" * (FEEDBACK_TEXT_MAX + 500)
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch,
        extraction_available=False,
        subject="Subject",
        snippet="Snippet",
        body_text=long_body,
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    params = _statement_matching(
        db, "INSERT INTO classification_feedback"
    ).compile().params
    assert len(params["input_text"]) == FEEDBACK_TEXT_MAX
    full_text = build_classification_text("Subject", "Snippet", long_body)
    assert params["input_text"] == full_text[:FEEDBACK_TEXT_MAX]


def test_reclassify_invalid_label_writes_nothing(monkeypatch):
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "not_a_real_label"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert db.statements == []


def test_reclassify_unknown_thread_writes_nothing(monkeypatch):
    client, _fake_task, _events, _thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False
    )
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{uuid4()}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert db.statements == []


def test_reclassify_thread_with_no_messages_writes_nothing(monkeypatch):
    client, _fake_task, _events, thread_id, _message_id, db = _reclassify_setup(
        monkeypatch, extraction_available=False
    )
    db.message = None
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/classification",
            json={"label": "fyi"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409
    # The latest-message select itself still ran (that's how we learned
    # there's nothing to lock) -- nothing past it.
    assert len(db.statements) == 1


# ---------------------------------------------------------------------------
# set_thread_done's thread-lock + action-item resolution
# ---------------------------------------------------------------------------


class _ThreadDoneDB:
    """Fake session backing set_thread_done: db.get resolves the owned
    thread (the pre-lock snapshot, used only for the 404 check), and every
    db.execute() call (the full-row FOR UPDATE lock, then an optional bulk
    ActionItem update) is recorded in call order. `locked_thread` is what
    the FOR UPDATE select returns -- it defaults to `thread` itself, but a
    test can pass a distinct object to simulate a race where a concurrent
    request already committed different lifecycle fields by the time this
    request's lock lands."""

    def __init__(self, thread, *, locked_thread=None):
        self.thread = thread
        self.locked_thread = thread if locked_thread is None else locked_thread
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        if "FOR UPDATE" in str(statement):
            result.scalar_one.return_value = self.locked_thread
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


def test_thread_done_locked_reread_skips_mutation_when_already_done_under_lock():
    # Two concurrent done=true requests both see done_at=None pre-lock. This
    # request loses the race for the FOR UPDATE lock -- by the time it
    # acquires the full row, the winner already committed a done_at. The
    # locked re-read must see that and skip mutating anything (including the
    # bulk ActionItem resolve, which the winner's own commit already ran) --
    # not just skip overwriting done_at with this request's own `now`.
    user_id = uuid4()
    thread_id = uuid4()
    winners_done_at = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    thread = SimpleNamespace(id=thread_id, user_id=user_id, done_at=None)
    locked_thread = SimpleNamespace(
        id=thread_id,
        user_id=user_id,
        done_at=winners_done_at,
        snoozed_until=None,
        snoozed_at=None,
    )
    db = _ThreadDoneDB(thread, locked_thread=locked_thread)
    client = _thread_done_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    # Only the full-row lock select runs -- no bulk ActionItem resolve, since
    # the locked read already shows this thread done.
    assert len(db.statements) == 1
    lock_statement = db.statements[0]
    assert "FOR UPDATE" in _compiled(lock_statement)
    assert f"mail_thread.id = '{thread_id.hex}'" in _compiled(lock_statement)
    assert resp.json()["done_at"] is not None


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
    # The full-row lock always runs, but the bulk ActionItem resolve is
    # skipped -- the locked read shows this thread is already done.
    assert len(db.statements) == 1
    assert "FOR UPDATE" in _compiled(db.statements[0])
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
    # The full-row lock always runs, but done=false never touches ActionItem.
    assert len(db.statements) == 1
    assert "FOR UPDATE" in _compiled(db.statements[0])


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


# ---------------------------------------------------------------------------
# GET /mail/thread/{id} -- classification carried on the detail response
# ---------------------------------------------------------------------------


class _ThreadDetailDB:
    """Fake session backing GET /mail/thread/{id}: db.get resolves the owned
    thread, and db.execute dispatches on statement shape between the
    projected active-snoozed_until scalar, the account lookup, the ordered
    message list, and the latest message's newest classification lookup.
    Every statement is recorded so a test can also assert the classification
    query's own ordering/limit, not just trust whatever this stub was told
    to hand back."""

    def __init__(
        self, thread, account_row, messages, classification, *, snoozed_until=None
    ):
        self.thread = thread
        self.account_row = account_row
        self.messages = messages
        self.classification = classification
        self.snoozed_until = snoozed_until
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        compiled = str(statement)
        if "provider_account" in compiled:
            result.one.return_value = self.account_row
        elif "FROM classification" in compiled:
            result.scalars.return_value.first.return_value = self.classification
        elif "FROM mail_message" in compiled:
            result.scalars.return_value.all.return_value = self.messages
        else:
            # The projected active_snoozed_until scalar -- the one query
            # left over once the other three shapes are ruled out.
            result.scalar_one.return_value = self.snoozed_until
        return result


def _thread_detail_client(db, user_id):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=user_id)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _thread_detail_setup(*, classification, snoozed_until=None):
    user_id = uuid4()
    thread_id = uuid4()
    message_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        user_id=user_id,
        subject="Renewal notice",
        provider="google",
        provider_thread_id="thread-abc",
        last_message_at=None,
        done_at=None,
        provider_account_id=account_id,
        replied_at=None,
    )
    account_row = SimpleNamespace(display_email="owner@gmail.example", external_user_id="owner-ext")
    message = SimpleNamespace(
        id=message_id,
        sent_at=None,
        sender="billing@vendor.example",
        snippet="Your plan renews soon",
        body_text="body",
        body_html="<p>body</p>",
    )
    db = _ThreadDetailDB(
        thread, account_row, [message], classification, snoozed_until=snoozed_until
    )
    return _thread_detail_client(db, user_id), db, thread_id, message_id


def test_thread_detail_returns_latest_message_classification():
    classification = SimpleNamespace(label="needs_reply", confidence=0.87, model_version="local-v3")
    client, _db, thread_id, _message_id = _thread_detail_setup(classification=classification)
    try:
        resp = client.get(f"/api/v1/mail/thread/{thread_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["classification"] == {
        "label": "needs_reply",
        "confidence": 0.87,
        "model_version": "local-v3",
    }


def test_thread_detail_carries_the_projected_active_snoozed_until():
    wake = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
    client, _db, thread_id, _message_id = _thread_detail_setup(
        classification=None, snoozed_until=wake
    )
    try:
        resp = client.get(f"/api/v1/mail/thread/{thread_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["thread"]["snoozed_until"]) == wake


def test_thread_detail_classification_is_null_when_unclassified():
    client, _db, thread_id, _message_id = _thread_detail_setup(classification=None)
    try:
        resp = client.get(f"/api/v1/mail/thread/{thread_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["classification"] is None


def test_thread_detail_reclassified_thread_returns_newest_classification_row():
    # A thread whose latest message was reclassified must surface the newest
    # classification row, not whichever one was written first -- this is the
    # ordering guarantee the whole feature rests on.
    newest = SimpleNamespace(label="fyi", confidence=1.0, model_version="user-override")
    client, db, thread_id, message_id = _thread_detail_setup(classification=newest)
    try:
        resp = client.get(f"/api/v1/mail/thread/{thread_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["classification"] == {
        "label": "fyi",
        "confidence": 1.0,
        "model_version": "user-override",
    }

    classification_statements = [
        statement for statement in db.statements if "FROM classification" in str(statement)
    ]
    assert len(classification_statements) == 1
    compiled = _compiled(classification_statements[0])
    # Scoped to the latest message (messages[0]) with no second message
    # query, ordered newest-first, and capped to one row -- the mechanism
    # that guarantees a reclassification wins over the original label.
    assert f"classification.message_id = '{message_id.hex}'" in compiled
    assert "ORDER BY classification.created_at DESC" in compiled
    assert "LIMIT 1" in compiled
