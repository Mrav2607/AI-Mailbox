"""Tests for tasks_nlp.py's Celery tasks. run_backfill's own contract (router-
per-run, routing-per-message, usage recording) lives in test_backfill.py --
these tests only cover the task wiring around it.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import LlmUsageDaily
from app.services.nlp.classifier import ClassificationAttempt
from app.services.nlp.llm_client import LlmUsage
from app.services.nlp.providers import ClassificationRouting, LlmCredential
from app.workers import tasks_nlp


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
            "skipped_user_overrides": 1,
            "task_created": 2,
            "task_processed": 3,
            "llm_attempted": 0,
            "llm_failed": 0,
            "fell_back": 0,
            "failure_categories": {},
            "left_unclassified": 0,
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
        "skipped_user_overrides": 1,
        "llm_attempted": 0,
        "llm_failed": 0,
        "fell_back": 0,
        "failure_categories": {},
        "left_unclassified": 0,
    }


def test_classify_latest_threads_forwards_failure_visibility_counters(monkeypatch):
    """Codex finding: this route rebuilds its result dict from a legacy key
    subset, which used to silently drop run_backfill's failure-visibility
    counters -- a queued LLM failure here would look like plain success on
    the task-status endpoint. Non-zero values must survive the reshape."""
    user_id = uuid4()

    def fake_run_backfill(db, uid, **kwargs):
        return {
            "status": "ok",
            "created": 1,
            "scanned": 5,
            "skipped_user_overrides": 0,
            "task_created": 1,
            "task_processed": 5,
            "llm_attempted": 4,
            "llm_failed": 2,
            "fell_back": 2,
            "failure_categories": {"http_429": 2},
            "left_unclassified": 1,
        }

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_nlp, "run_backfill", fake_run_backfill)

    result = tasks_nlp.classify_latest_threads.run(str(user_id), limit=25, force=True)

    assert result["llm_attempted"] == 4
    assert result["llm_failed"] == 2
    assert result["fell_back"] == 2
    assert result["failure_categories"] == {"http_429": 2}
    assert result["left_unclassified"] == 1


# ---------------------------------------------------------------------------
# classify_message: single message, one-shot resolution (no router -- see
# providers.ClassificationRouter's docstring for why a memo only pays off
# across many messages in one run).
# ---------------------------------------------------------------------------


def _classification_attempt(verdict, *, provider_call_succeeded=False, usage=None):
    return ClassificationAttempt(
        verdict=verdict, provider_call_succeeded=provider_call_succeeded, usage=usage
    )


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
    # mode="server" -- this test is about routing resolution timing, not
    # usage; a real ClassificationRouting keeps the usage-gating check
    # inside classify_message from blowing up on a bare sentinel object.
    sentinel_routing = ClassificationRouting(mode="server", credential=None)

    def fake_resolve(db_arg, uid):
        resolve_calls.append((db_arg, uid))
        return sentinel_routing

    monkeypatch.setattr(tasks_nlp, "resolve_classification_routing", fake_resolve)

    classify_calls = []

    def fake_classify_with_usage(text, backend=None, routing=None):
        classify_calls.append(routing)
        return _classification_attempt(("fyi", 0.5, "no cues", "heuristic-v1"))

    monkeypatch.setattr(tasks_nlp, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")

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

    def fake_classify_with_usage(text, backend=None, routing=None):
        classify_calls.append(routing)
        return _classification_attempt(("fyi", 0.4, "no cues", "heuristic-v1"))

    monkeypatch.setattr(tasks_nlp, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")

    tasks_nlp.classify_message.run(str(message_id))

    assert classify_calls == [None]


def test_classify_message_reports_skipped_marker_when_upsert_protects_a_user_override(
    monkeypatch,
):
    # upsert_classification returns "protected" when a user override landed
    # on this message before this write reached it -- classify_message must
    # not claim the label it computed but never actually persisted.
    message_id = uuid4()
    message = SimpleNamespace(
        id=message_id, thread_id=uuid4(), snippet="hi", body_text="there"
    )

    db = MagicMock()
    db.get.side_effect = [message, None]
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        tasks_nlp, "classify_with_usage",
        lambda text, backend=None, routing=None: _classification_attempt(
            ("fyi", 0.4, "no cues", "heuristic-v1")
        ),
    )
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "protected")

    result = tasks_nlp.classify_message.run(str(message_id))

    assert result == {"message_id": str(message_id), "status": "skipped_user_override"}


# ---------------------------------------------------------------------------
# classify_message: usage recording (plan §5's flush/commit contract)
# ---------------------------------------------------------------------------


class _FakeAcc:
    """Stands in for UsageAccumulator at the call site -- its own DB shape
    lives outside this module's boundary and is covered where it's defined.
    """

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


def _message_and_thread():
    message_id, thread_id, user_id = uuid4(), uuid4(), uuid4()
    message = SimpleNamespace(id=message_id, thread_id=thread_id, snippet="hi", body_text="there")
    thread = SimpleNamespace(id=thread_id, user_id=user_id, subject="subj")
    return message, thread


_CRED = LlmCredential(
    provider="openai", base_url="https://api.openai.com/v1", api_key="key", model="gpt-4o-mini"
)


def test_classify_message_records_usage_only_for_user_mode_routing(monkeypatch):
    message, thread = _message_and_thread()
    db = MagicMock()
    db.get.side_effect = [message, thread]
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        tasks_nlp, "resolve_classification_routing",
        lambda db_arg, uid: ClassificationRouting(mode="user", credential=_CRED),
    )
    usage = LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    monkeypatch.setattr(
        tasks_nlp, "classify_with_usage",
        lambda text, backend=None, routing=None: _classification_attempt(
            ("fyi", 0.5, "r", "openai:gpt-4o-mini"), provider_call_succeeded=True, usage=usage
        ),
    )
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")
    fake_acc = _FakeAcc(thread.user_id)
    monkeypatch.setattr(tasks_nlp, "UsageAccumulator", lambda uid: fake_acc)

    tasks_nlp.classify_message.run(str(message.id))

    assert fake_acc.records == [("classification", "openai", usage, True)]


def test_classify_message_server_mode_routing_records_nothing(monkeypatch):
    # The regression guard for the routing-mode gate: the operator's server
    # key must never get billed onto the user's readout.
    message, thread = _message_and_thread()
    db = MagicMock()
    db.get.side_effect = [message, thread]
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        tasks_nlp, "resolve_classification_routing",
        lambda db_arg, uid: ClassificationRouting(mode="server", credential=None),
    )
    monkeypatch.setattr(
        tasks_nlp, "classify_with_usage",
        lambda text, backend=None, routing=None: _classification_attempt(
            ("fyi", 0.5, "r", "gemini-x"),
            provider_call_succeeded=True,
            usage=LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        ),
    )
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")

    def _fail_if_constructed(uid):
        raise AssertionError("UsageAccumulator must not be constructed when nothing is recorded")

    monkeypatch.setattr(tasks_nlp, "UsageAccumulator", _fail_if_constructed)

    tasks_nlp.classify_message.run(str(message.id))


def test_classify_message_flush_happens_before_commit_and_committed_after(monkeypatch):
    message, thread = _message_and_thread()
    events = []
    db = MagicMock()
    db.get.side_effect = [message, thread]
    db.flush.side_effect = lambda: events.append("db.flush")
    db.commit.side_effect = lambda: events.append("db.commit")
    db.begin_nested.return_value = nullcontext()
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        tasks_nlp, "resolve_classification_routing",
        lambda db_arg, uid: ClassificationRouting(mode="user", credential=_CRED),
    )
    monkeypatch.setattr(
        tasks_nlp, "classify_with_usage",
        lambda text, backend=None, routing=None: _classification_attempt(
            ("fyi", 0.5, "r", "openai:gpt-4o-mini"), provider_call_succeeded=True
        ),
    )
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")
    fake_acc = _FakeAcc(thread.user_id, events=events)
    monkeypatch.setattr(tasks_nlp, "UsageAccumulator", lambda uid: fake_acc)

    tasks_nlp.classify_message.run(str(message.id))

    assert events == ["db.flush", "acc.flush", "db.commit", "acc.committed"]


def test_classify_message_failing_usage_flush_does_not_block_business_commit(monkeypatch):
    message, thread = _message_and_thread()
    db = MagicMock()
    db.get.side_effect = [message, thread]
    db.begin_nested.return_value = nullcontext()
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        tasks_nlp, "resolve_classification_routing",
        lambda db_arg, uid: ClassificationRouting(mode="user", credential=_CRED),
    )
    monkeypatch.setattr(
        tasks_nlp, "classify_with_usage",
        lambda text, backend=None, routing=None: _classification_attempt(
            ("fyi", 0.5, "r", "openai:gpt-4o-mini"), provider_call_succeeded=True
        ),
    )
    monkeypatch.setattr(tasks_nlp, "upsert_classification", lambda *a, **k: "written")
    fake_acc = _FakeAcc(thread.user_id, flush_raises=SQLAlchemyError("boom"))
    monkeypatch.setattr(tasks_nlp, "UsageAccumulator", lambda uid: fake_acc)

    result = tasks_nlp.classify_message.run(str(message.id))

    assert result["label"] == "fyi"  # the business write still landed
    db.commit.assert_called_once()  # never skipped despite the flush failure
    assert fake_acc.calls.index("discard") < fake_acc.calls.index("committed")


# ---------------------------------------------------------------------------
# prune_llm_usage_daily: the retention sweep behind llm_usage_daily's
# standalone usage_date index (plan §8).
# ---------------------------------------------------------------------------


class _FixedNow:
    """Stands in for tasks_nlp's `datetime` import so the cutoff is
    deterministic -- no freezegun in this repo, matching test_usage.py's
    fake-datetime-class pattern.
    """

    _FIXED = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, _tz):
        return cls._FIXED


def _compiled(stmt):
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_prune_llm_usage_daily_cutoff_is_400_days_not_some_other_number(monkeypatch):
    db = MagicMock()
    db.execute.return_value = MagicMock(rowcount=0)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_nlp, "datetime", _FixedNow)

    tasks_nlp.prune_llm_usage_daily.run()

    stmt = db.execute.call_args[0][0]
    expected_cutoff = _FixedNow._FIXED.date() - timedelta(days=400)
    assert str(expected_cutoff) in _compiled(stmt)
    # 90 would be the wrong number to "tidy" this down to -- guard against it
    # explicitly rather than only asserting the right value.
    assert str(_FixedNow._FIXED.date() - timedelta(days=90)) not in _compiled(stmt)


def test_prune_llm_usage_daily_targets_only_that_table(monkeypatch):
    # The failure mode worth guarding against: a retention sweep that
    # quietly deletes from the wrong table.
    db = MagicMock()
    db.execute.return_value = MagicMock(rowcount=0)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_nlp, "datetime", _FixedNow)

    tasks_nlp.prune_llm_usage_daily.run()

    stmt = db.execute.call_args[0][0]
    assert stmt.table.name == LlmUsageDaily.__tablename__
    assert "llm_usage_daily" in _compiled(stmt)


def test_prune_llm_usage_daily_commits_and_reports_the_deleted_count(monkeypatch):
    db = MagicMock()
    db.execute.return_value = MagicMock(rowcount=7)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_nlp, "datetime", _FixedNow)

    result = tasks_nlp.prune_llm_usage_daily.run()

    db.commit.assert_called_once()
    assert result == {"status": "ok", "deleted": 7}


def test_prune_llm_usage_daily_swallows_db_error_instead_of_taking_down_beat(monkeypatch):
    # The whole point of the try/except here: a bad sweep must report
    # "error" and let tomorrow's tick retry, not blow up the beat worker.
    db = MagicMock()
    db.execute.side_effect = SQLAlchemyError("boom")
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_nlp, "datetime", _FixedNow)

    result = tasks_nlp.prune_llm_usage_daily.run()

    assert result == {"status": "error"}
    db.commit.assert_not_called()
