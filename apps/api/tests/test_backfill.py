"""Tests for backfill.py: the shared "latest message" queries, run_backfill's
router-per-run/routing-per-message contract, and its usage-recording flush
sequence (plan §5).

No live DB is available to this suite (see conftest.py's module docstring),
so run_backfill is driven with `_BackfillFakeDB` -- a dispatch-by-table fake
mirroring test_providers.py's `_FakeClassificationDB` idiom for the same
resolver. Usage-recording tests use `_FakeAcc` rather than the real
UsageAccumulator -- that class's own DB shape (the sorted multi-row UPSERT)
lives outside this module's boundary and is covered where it's defined;
these tests only assert the CALL-SITE contract (who gets recorded, and the
flush-before-commit ordering).
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import MailMessage
from app.services.nlp import backfill
from app.services.nlp import classifier as classifier_module
from app.services.nlp.classifier import ClassificationAttempt
from app.services.nlp.llm_client import LlmUsage
from app.services.nlp.providers import ClassificationRouting, LlmCredential


# ---------------------------------------------------------------------------
# Shared fakes
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
    """Answers just enough of a Session for run_backfill -- dispatched by
    which table each select's first column belongs to, mirroring
    test_providers.py's `_FakeClassificationDB` idiom for the same resolver.
    """

    def __init__(self, *, threads, latest_rows, classified_ids=(), routing_row=None, events=None):
        self._threads = threads
        self._latest_rows = latest_rows
        self._classified_ids = classified_ids
        # (provider, classification_byok) for the routing projection read, or
        # None for "no stored credential" -- there's never a second read to
        # answer here because every test below either opts out, is a
        # custom-provider row, or monkeypatches ClassificationRouter outright.
        self._routing_row = routing_row
        self.commits = 0
        # Optional shared list a usage-flush-ordering test appends to
        # alongside _FakeAcc's own events, so the two interleave into one
        # timeline without a real clock.
        self.events = events

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

    def flush(self):
        # Core DML (upsert_classification) never dirties ORM state, so this
        # is a no-op in practice -- it exists so flush_pending's
        # unconditional db.flush() has something to call.
        if self.events is not None:
            self.events.append("db.flush")

    def begin_nested(self):
        return nullcontext()

    def commit(self):
        self.commits += 1
        if self.events is not None:
            self.events.append("db.commit")


class _FakeAcc:
    """Stands in for UsageAccumulator at the call site -- see the module
    docstring for why the real class isn't used here."""

    def __init__(self, user_id, *, events=None, flush_raises=None):
        self.user_id = user_id
        self.records = []
        self.calls = []
        self.events = events
        self.flush_raises = flush_raises

    def record(self, stage, provider, usage, *, provider_call_succeeded):
        self.records.append((stage, provider, usage, provider_call_succeeded))
        self.calls.append("record")

    def flush(self, db):
        self.calls.append("flush")
        if self.events is not None:
            self.events.append("acc.flush")
        if self.flush_raises is not None:
            raise self.flush_raises

    def discard(self):
        self.calls.append("discard")

    def committed(self):
        self.calls.append("committed")
        if self.events is not None:
            self.events.append("acc.committed")


# ---------------------------------------------------------------------------
# latest_message_ordering / latest_messages_by_thread
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# run_backfill: one router (and one usage accumulator) per run,
# routing_for(db) per message.
# ---------------------------------------------------------------------------


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
    # mode="server" -- this test is about router/routing_for call counts, not
    # usage; a real ClassificationRouting keeps the usage-gating check inside
    # run_backfill from blowing up on a bare sentinel string.
    sentinel_routing = ClassificationRouting(mode="server", credential=None)

    class FakeRouter:
        def __init__(self, uid):
            construction_calls.append(uid)

        def routing_for(self, db_arg):
            routing_for_calls.append(db_arg)
            return sentinel_routing

    monkeypatch.setattr(backfill, "ClassificationRouter", FakeRouter)

    classify_routings = []

    def fake_classify_with_usage(text, backend=None, routing=None):
        classify_routings.append(routing)
        return ClassificationAttempt(
            verdict=("fyi", 0.5, "no cues", "heuristic-v1"),
            provider_call_succeeded=False,
            usage=None,
        )

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)

    result = backfill.run_backfill(db, user_id, limit=10)

    # Constructed exactly once for the whole run -- not once per message --
    # yet routing_for still ran once per message, which is what lets a
    # mid-run revocation settle within the memo's TTL instead of the run's end.
    assert construction_calls == [user_id]
    assert len(routing_for_calls) == 2
    assert classify_routings == [sentinel_routing, sentinel_routing]
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


# ---------------------------------------------------------------------------
# run_backfill: usage recording (plan §5's flush/commit contract)
# ---------------------------------------------------------------------------

_CRED = LlmCredential(
    provider="openai", base_url="https://api.openai.com/v1", api_key="key", model="gpt-4o-mini"
)


def _single_message_db(**kwargs):
    thread_id, msg_id = uuid4(), uuid4()
    threads = [SimpleNamespace(id=thread_id, subject="s")]
    latest_rows = [
        SimpleNamespace(id=msg_id, thread_id=thread_id, snippet="hi", body_text="there")
    ]
    return _BackfillFakeDB(threads=threads, latest_rows=latest_rows, **kwargs)


def _fake_router_returning(routing):
    class FakeRouter:
        def __init__(self, uid):
            pass

        def routing_for(self, db_arg):
            return routing

    return FakeRouter


def test_run_backfill_records_usage_only_for_user_mode_routing(monkeypatch):
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    usage = LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=usage,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)
    fake_acc = _FakeAcc(user_id)
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    backfill.run_backfill(db, user_id, limit=10)

    assert fake_acc.records == [("classification", "openai", usage, True)]


def test_run_backfill_server_mode_routing_records_nothing(monkeypatch):
    # The regression guard for the routing-mode gate: the operator's server
    # key must never get billed onto the user's readout.
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="server", credential=None)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "gemini-x"),
            provider_call_succeeded=True,
            usage=LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)
    fake_acc = _FakeAcc(user_id)
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    backfill.run_backfill(db, user_id, limit=10)

    assert fake_acc.records == []
    assert "flush" not in fake_acc.calls  # skipped cheaply -- nothing to flush
    assert "committed" in fake_acc.calls


def test_run_backfill_flush_happens_before_commit_and_committed_after(monkeypatch):
    user_id = uuid4()
    events = []
    db = _single_message_db(events=events)
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=None,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)
    fake_acc = _FakeAcc(user_id, events=events)
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    backfill.run_backfill(db, user_id, limit=10)

    # The leading "db.commit" is run_backfill's own read-transaction close,
    # issued before classification starts -- unrelated to usage. What matters
    # is the tail: flush lands before the batch's business commit, and
    # committed() only fires once that commit has actually returned.
    assert events == ["db.commit", "db.flush", "acc.flush", "db.commit", "acc.committed"]


def test_run_backfill_failing_usage_flush_does_not_block_business_commit(monkeypatch):
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=None,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: None)
    fake_acc = _FakeAcc(user_id, flush_raises=SQLAlchemyError("boom"))
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    result = backfill.run_backfill(db, user_id, limit=10)

    assert result["created"] == 1  # the business write still landed
    assert fake_acc.calls.index("discard") < fake_acc.calls.index("committed")
