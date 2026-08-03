from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.db.models import MailMessage
from app.services.nlp import backfill, classifier as classifier_module
from app.workers import tasks_nlp


def _rendered_latest_messages_sql() -> str:
    """Run latest_messages_by_thread against a db that just captures the
    statement, then render it as Postgres would see it."""
    captured = {}

    class FakeResult:
        def all(self):
            return []

    class FakeDB:
        def execute(self, statement):
            captured["statement"] = statement
            return FakeResult()

    backfill.latest_messages_by_thread(
        FakeDB(),
        [uuid4()],
        columns=(MailMessage.id, MailMessage.thread_id),
    )
    return str(
        captured["statement"].compile(dialect=postgresql.dialect())
    ).lower()


def test_latest_message_is_picked_by_coalesced_recency():
    # The whole point of the shared helper: a message with no sent_at falls back
    # to created_at, and it's DISTINCT ON -- not Python -- that does the picking.
    # If either half drifts, a thread's bucket stops matching the message we
    # label, which is the bug this replaced.
    sql = _rendered_latest_messages_sql()

    assert "distinct on (mail_message.thread_id)" in sql
    assert "coalesce(mail_message.sent_at, mail_message.created_at) desc nulls last" in sql
    # thread_id has to lead the ORDER BY or Postgres rejects the DISTINCT ON.
    order_by = sql.split("order by", 1)[1]
    assert order_by.strip().startswith("mail_message.thread_id")


def test_latest_messages_by_thread_skips_the_query_when_there_are_no_threads():
    class ExplodingDB:
        def execute(self, statement):  # pragma: no cover - must never run
            raise AssertionError("no threads means no query")

    assert backfill.latest_messages_by_thread(
        ExplodingDB(), [], columns=(MailMessage.id, MailMessage.thread_id)
    ) == {}


def test_classify_latest_threads_delegates_to_the_shared_backfill(monkeypatch):
    user_id = uuid4()
    captured = {}

    def fake_run_backfill(db, uid, **kwargs):
        captured["user_id"] = uid
        captured.update(kwargs)
        return {
            "status": "ok",
            "created": 3,
            "scanned": 9,
            "task_created": 2,
            "task_processed": 3,
        }

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_nlp, "run_backfill", fake_run_backfill)

    result = tasks_nlp.classify_latest_threads.run(str(user_id), limit=25, force=True)

    assert captured["user_id"] == user_id
    assert captured["bucket"] == "all"
    assert captured["force"] is True
    assert captured["limit"] == 25
    # user_id rides along so the task-status endpoint can check ownership.
    assert result == {
        "status": "ok",
        "user_id": str(user_id),
        "created": 2,
        "processed": 3,
    }


# ---------------------------------------------------------------------------
# classify_message: single message, one-shot resolution (no router -- see
# providers.ClassificationRouter's docstring for why a memo only pays off
# across many messages in one run).
# ---------------------------------------------------------------------------


def test_classify_message_resolves_routing_once_after_loading_thread(monkeypatch):
    message_id = uuid4()
    thread_id = uuid4()
    user_id = uuid4()

    message = SimpleNamespace(id=message_id, thread_id=thread_id, snippet="hi", body_text="there")
    thread = SimpleNamespace(id=thread_id, user_id=user_id, subject="subj")

    db = MagicMock()
    db.get.side_effect = [message, thread]
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))

    resolve_calls = []
    sentinel_routing = object()

    def fake_resolve(db_arg, uid):
        resolve_calls.append((db_arg, uid))
        return sentinel_routing

    monkeypatch.setattr(tasks_nlp, "resolve_classification_routing", fake_resolve)

    classify_calls = []

    def fake_classify(text, backend=None, routing=None):
        classify_calls.append(routing)
        return ("fyi", 0.5, "no cues", "heuristic-v1")

    monkeypatch.setattr(tasks_nlp, "classify", fake_classify)
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: None)

    tasks_nlp.classify_message.run(str(message_id))

    # Resolved exactly once, after the thread (which carries user_id) loaded.
    assert resolve_calls == [(db, user_id)]
    assert classify_calls == [sentinel_routing]


def test_classify_message_missing_thread_routes_to_none_without_resolving(monkeypatch):
    # A message whose thread vanished underneath it: routing falls back to
    # None (today's server-key/heuristic behavior) rather than resolving off
    # a thread that isn't there.
    message_id = uuid4()
    message = SimpleNamespace(id=message_id, thread_id=uuid4(), snippet="hi", body_text="there")

    db = MagicMock()
    db.get.side_effect = [message, None]
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))

    def boom(*args, **kwargs):
        raise AssertionError("no thread means no user_id to resolve routing for")

    monkeypatch.setattr(tasks_nlp, "resolve_classification_routing", boom)

    classify_calls = []

    def fake_classify(text, backend=None, routing=None):
        classify_calls.append(routing)
        return ("fyi", 0.4, "no cues", "heuristic-v1")

    monkeypatch.setattr(tasks_nlp, "classify", fake_classify)
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: None)

    tasks_nlp.classify_message.run(str(message_id))

    assert classify_calls == [None]


# ---------------------------------------------------------------------------
# run_backfill: one router per run, routing_for(db) per message.
#
# _Result/_BackfillFakeDB answer just enough of a Session for run_backfill --
# dispatched by which table each select's first column belongs to, mirroring
# test_providers.py's _FakeClassificationDB idiom for the same resolver.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


class _BackfillFakeDB:
    def __init__(self, *, threads, latest_rows, classified_ids=(), routing_row=None):
        self._threads = threads
        self._latest_rows = latest_rows
        self._classified_ids = classified_ids
        # (provider, classification_byok) for the routing projection read, or
        # None for "no stored credential" -- there's never a second read to
        # answer here because every test below either opts out or is a
        # custom-provider row, both of which short-circuit before it.
        self._routing_row = routing_row
        self.commits = 0

    def execute(self, stmt):
        cols = [c.key for c in stmt.selected_columns]
        if cols == ["provider", "classification_byok"]:
            if self._routing_row is None:
                return _Result([])
            provider, byok = self._routing_row
            return _Result([SimpleNamespace(provider=provider, classification_byok=byok)])
        table = stmt.selected_columns[0].table.name
        if table == "mail_thread":
            return _Result(self._threads)
        if table == "mail_message":
            return _Result(self._latest_rows)
        if table == "classification":
            return _Result(self._classified_ids)
        raise AssertionError(f"unexpected table in test query: {table}")

    def commit(self):
        self.commits += 1


def test_run_backfill_builds_one_router_per_run_and_calls_routing_for_per_message(monkeypatch):
    user_id = uuid4()
    thread_a, thread_b = uuid4(), uuid4()
    msg_a, msg_b = uuid4(), uuid4()

    threads = [
        SimpleNamespace(id=thread_a, subject="a"),
        SimpleNamespace(id=thread_b, subject="b"),
    ]
    latest_rows = [
        SimpleNamespace(id=msg_a, thread_id=thread_a, snippet="s-a", body_text="b-a"),
        SimpleNamespace(id=msg_b, thread_id=thread_b, snippet="s-b", body_text="b-b"),
    ]
    db = _BackfillFakeDB(threads=threads, latest_rows=latest_rows)

    construction_calls = []
    routing_for_calls = []

    class FakeRouter:
        def __init__(self, uid):
            construction_calls.append(uid)

        def routing_for(self, db_arg):
            routing_for_calls.append(db_arg)
            return "sentinel-routing"

    monkeypatch.setattr(backfill, "ClassificationRouter", FakeRouter)

    classify_routings = []

    def fake_classify(text, backend=None, routing=None):
        classify_routings.append(routing)
        return ("fyi", 0.5, "no cues", "heuristic-v1")

    monkeypatch.setattr(backfill, "classify", fake_classify)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)

    result = backfill.run_backfill(db, user_id, limit=10)

    # Constructed exactly once for the whole run -- not once per message --
    # yet routing_for still ran once per message, which is what lets a
    # mid-run revocation settle within the memo's TTL instead of the run's end.
    assert construction_calls == [user_id]
    assert len(routing_for_calls) == 2
    assert classify_routings == ["sentinel-routing", "sentinel-routing"]
    assert result["created"] == 2


def test_run_backfill_custom_opt_in_credential_routes_off_with_no_server_call(monkeypatch):
    """A pre-existing custom-provider row with classification_byok=True is
    presets-only in v1 -- it must resolve to mode="off" and classify straight
    to the heuristic, never touching the server-key path. Asserting the seam
    (genai client, the BYOK wire call) is never built is the actual proof
    that nobody got billed, not just the returned label."""
    user_id = uuid4()
    thread_id = uuid4()
    msg_id = uuid4()

    threads = [SimpleNamespace(id=thread_id, subject="s")]
    latest_rows = [
        SimpleNamespace(id=msg_id, thread_id=thread_id, snippet="hi", body_text="there")
    ]
    db = _BackfillFakeDB(threads=threads, latest_rows=latest_rows, routing_row=("custom", True))

    def _explode(*args, **kwargs):
        raise AssertionError("server key must never be used for an off-routed message")

    monkeypatch.setattr(classifier_module, "_genai_client", _explode)
    monkeypatch.setattr(classifier_module, "call_chat_completion", _explode)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)

    # backend="gemini" skips the local-model branch so routing (not whatever
    # the local encoder happens to be doing in this test env) decides the
    # outcome.
    result = backfill.run_backfill(db, user_id, limit=10, backend="gemini")

    assert result["created"] == 1
