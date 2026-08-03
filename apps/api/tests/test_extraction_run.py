"""Tests for the action-extraction claim/record state machine: persistence
helpers (SQL shape + rowcount-driven branching), the extraction_run
orchestration (claim -> extract -> record, sweeps, recovery-tick user sets),
and the Celery task/hook wiring -- all offline, no real database or broker.

No live DB is available to this suite (see conftest.py's module docstring),
so persistence.py is exercised with a FakeDB that returns canned rowcounts
and lets us inspect the exact statement it built (mirroring
test_tasks_nlp.py's compiled-SQL-inspection style), and extraction_run.py's
orchestration is exercised with a FakeSession answering only the plain
SELECTs it issues directly -- claim_action_item/record_extraction and
extract_action_with_usage are monkeypatched per test, since they're covered
in isolation elsewhere in this file. Controlled call ordering stands in for
real cross-connection lock contention, which is exercised in live QA.

Usage-recording tests use `_FakeAcc` rather than the real UsageAccumulator --
that class's own DB shape (the sorted multi-row UPSERT) is out of this
module's boundary and covered where it's defined; these tests only assert
the CALL-SITE contract (who gets recorded, and the flush-before-commit
ordering from plan §5).
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.models import MailMessage
from app.services.nlp import extraction_run, persistence
from app.services.nlp.extractor import ExtractedAction, ExtractionAttempt, NoAction
from app.services.nlp.llm_client import LlmUsage
from app.services.nlp.providers import CallContext, LlmCredential, ResolvedExtraction
from app.workers import tasks_ingest, tasks_nlp

# A fake resolved credential for tests that drive _claim_extract_record
# directly and don't care about resolution itself -- passing it explicitly
# sidesteps the real DB-backed resolve_extraction_credential call, mirroring
# how these tests already monkeypatch claim_action_item/extract_action_with_usage.
_CRED = LlmCredential(provider="gemini", base_url="https://example.test", api_key="key", model="gemini-x")

# Default call context for tests that only care about credential threading,
# not usage -- "operator" so _claim_extract_record never records/flushes
# anything, and these tests don't need to know about UsageAccumulator at
# all. Tests that DO care about usage recording use _USER_CTX instead.
_CTX = CallContext(credential=_CRED, payer="operator")
_USER_CTX = CallContext(credential=_CRED, payer="user")


def _extraction_attempt(result, *, provider_call_succeeded=True, usage=None):
    """Build the ExtractionAttempt extract_action_with_usage's fakes return --
    a thin helper so every test doesn't hand-roll the same three fields."""
    return ExtractionAttempt(
        result=result, provider_call_succeeded=provider_call_succeeded, usage=usage
    )


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for the CursorResult a real db.execute() would return."""

    def __init__(self, rowcount=0, scalar=None, rows=()):
        self.rowcount = rowcount
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Records every statement persistence.py hands to execute() and answers
    each call with the next canned _FakeResult -- enough to drive
    claim_action_item/record_extraction through every branch without a real
    database."""

    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return self.results.pop(0)

    def commit(self):
        pass


def _compiled_params(stmt):
    return stmt.compile(dialect=postgresql.dialect()).params


def _compiled_sql(clause_or_stmt, literal_binds=True):
    kwargs = {"compile_kwargs": {"literal_binds": True}} if literal_binds else {}
    return str(clause_or_stmt.compile(dialect=postgresql.dialect(), **kwargs))


def _make_message(*, thread_id=None, sender="a@b.com", snippet="hi", body_text="body",
                   sent_at=None, created_at=None):
    return SimpleNamespace(
        id=uuid4(),
        thread_id=thread_id or uuid4(),
        sender=sender,
        snippet=snippet,
        body_text=body_text,
        sent_at=sent_at,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_thread(*, id=None, user_id=None, subject="Subject", done_at=None):
    return SimpleNamespace(
        id=id or uuid4(), user_id=user_id or uuid4(), subject=subject, done_at=done_at
    )


def _make_classification(*, label="needs_reply"):
    return SimpleNamespace(label=label)


class _FakeSession:
    """Drives _claim_extract_record's real control flow without a database.

    Answers the three plain queries extraction_run.py itself issues (thread
    FOR UPDATE, classification FOR UPDATE, a later done_at re-read) by
    inspecting each select's column_descriptions -- claim_action_item and
    record_extraction are monkeypatched by the test, so this fake never has
    to understand their SQL, only extraction_run.py's own.
    """

    def __init__(
        self, *, message=None, thread=None, classification=None, done_at_reads=None, events=None
    ):
        self.message = message
        self.thread = thread
        self.classification = classification
        self._done_at_reads = list(done_at_reads or [])
        self.commits = 0
        self.rollbacks = 0
        self.executed = []
        # Optional shared list a usage-flush-ordering test appends to
        # alongside _FakeAcc's own events, so the two can be interleaved
        # into one timeline without a real clock.
        self.events = events

    def get(self, model, pk):
        assert model is MailMessage
        return self.message

    def execute(self, stmt):
        self.executed.append(stmt)
        name = stmt.column_descriptions[0]["name"]
        if name == "done_at":
            return _FakeResult(scalar=self._done_at_reads.pop(0))
        if name == "MailThread":
            return _FakeResult(scalar=self.thread)
        if name == "Classification":
            return _FakeResult(scalar=self.classification)
        raise AssertionError(f"unexpected statement in fake session: {stmt}")

    def flush(self):
        # Core DML (claim_action_item/record_extraction) never dirties ORM
        # state, so this is a no-op in practice -- it exists so
        # _claim_extract_record's unconditional db.flush() has something to
        # call, matching persistence.py's real Core-DML shape.
        if self.events is not None:
            self.events.append("db.flush")

    def begin_nested(self):
        return nullcontext()

    def commit(self):
        self.commits += 1
        if self.events is not None:
            self.events.append("db.commit")

    def rollback(self):
        self.rollbacks += 1


class _FakeAcc:
    """Stands in for UsageAccumulator at the call site so these tests assert
    who gets recorded and the flush/commit/discard ordering (plan §5)
    without depending on UsageAccumulator's own DB-shape internals -- those
    live outside this module's boundary and are covered where the class is
    defined.
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


def _enable_extraction(monkeypatch):
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")


# ---------------------------------------------------------------------------
# extraction_available()
# ---------------------------------------------------------------------------


# The old no-arg extraction_available lived here and is gone: coverage is now
# per-user (flag AND a resolvable credential), so providers.extraction_available
# owns it and tests/test_providers.py covers every branch.


# ---------------------------------------------------------------------------
# persistence.claimable_predicate
# ---------------------------------------------------------------------------


def test_claimable_predicate_sql_shape():
    default_sql = _compiled_sql(persistence.claimable_predicate())
    assert "outcome = 'ineligible'" in default_sql
    assert "outcome = 'failed' AND action_item.attempts < 3" in default_sql
    assert "outcome = 'pending' AND action_item.last_attempted_at <" in default_sql
    assert "!=" not in default_sql

    forced_sql = _compiled_sql(persistence.claimable_predicate(force=True))
    assert "outcome != 'pending'" in forced_sql


# ---------------------------------------------------------------------------
# persistence.claim_action_item
# ---------------------------------------------------------------------------


def test_claim_action_item_insert_stamps_attempts_and_last_attempted_at():
    mid, tid, uid = uuid4(), uuid4(), uuid4()
    db = _FakeDB([_FakeResult(rowcount=1)])

    token = persistence.claim_action_item(
        db, message_id=mid, thread_id=tid, user_id=uid, thread_done=False
    )

    assert token is not None
    params = _compiled_params(db.statements[0])
    assert params["message_id"] == mid
    assert params["thread_id"] == tid
    assert params["user_id"] == uid
    assert params["outcome"] == "pending"
    assert params["claim_token"] == token
    assert params["attempts"] == 1
    assert isinstance(params["last_attempted_at"], datetime)
    assert "status" not in params  # thread not done -- no stamp on a fresh insert


def test_claim_action_item_insert_on_done_thread_stamps_status_done():
    db = _FakeDB([_FakeResult(rowcount=1)])

    persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(), thread_done=True
    )

    params = _compiled_params(db.statements[0])
    assert params["status"] == "done"
    assert isinstance(params["status_at"], datetime)


def test_claim_action_item_reclaims_existing_row_and_increments_attempts():
    # Insert affects zero rows (a row already exists) -- falls through to the
    # conditional UPDATE, which succeeds.
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=1)])

    token = persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(), thread_done=False
    )

    assert token is not None
    update_params = _compiled_params(db.statements[1])
    assert update_params["claim_token"] == token
    assert update_params["attempts_1"] == 1  # attempts = attempts + 1
    assert isinstance(update_params["last_attempted_at"], datetime)


def test_claim_action_item_ineligible_claimable_regardless_of_attempts():
    # Real rowcount semantics aren't exercised without a database, but the
    # WHERE clause is: ineligible rows are unconditionally in the claimable
    # OR, with no attempts check -- verify that shape directly.
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=1)])
    persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(), thread_done=False
    )
    where_sql = _compiled_sql(db.statements[1])
    assert "outcome = 'ineligible'" in where_sql
    # No "attempts" condition directly adjacent to the ineligible clause.
    assert "outcome = 'ineligible' AND" not in where_sql


def test_claim_action_item_force_never_steals_live_pending():
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=1)])
    persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(),
        thread_done=False, force=True,
    )
    where_sql = _compiled_sql(db.statements[1])
    # force widens to "any non-pending row" -- a live (unexpired) pending row
    # is deliberately absent from every disjunct, forced or not.
    assert "outcome != 'pending'" in where_sql
    assert "outcome = 'pending'" in where_sql  # still present: the expired-lease branch


def test_claim_action_item_reclaim_on_done_thread_preserves_dismissed():
    # The done-stamp on the reclaim path is a CASE keyed off the row's OWN
    # pre-write status ('open' -> 'done'), never an unconditional overwrite --
    # that CASE is exactly what keeps a forced reclaim of a dismissed row
    # dismissed. Assert the CASE shape, not a literal 'done'.
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=1)])
    persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(),
        thread_done=True, force=True,
    )
    sql = _compiled_sql(db.statements[1])
    assert "CASE WHEN (action_item.status = 'open') THEN 'done' ELSE action_item.status END" in sql
    assert "status = 'done'" not in sql.replace("ELSE action_item.status", "")


def test_claim_action_item_not_claimable_terminalizes_expired_at_cap_row():
    # Insert misses, claim-update misses (someone else's live/expired-but-
    # not-yet-terminal claim) -- the third statement must be the
    # terminalization UPDATE, and the function returns None either way.
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=0), _FakeResult(rowcount=1)])

    token = persistence.claim_action_item(
        db, message_id=uuid4(), thread_id=uuid4(), user_id=uuid4(), thread_done=False
    )

    assert token is None
    assert len(db.statements) == 3
    terminalize_sql = _compiled_sql(db.statements[2])
    assert "outcome = 'pending'" in terminalize_sql
    assert "attempts >= 3" in terminalize_sql
    assert "outcome='failed'" in terminalize_sql.replace(" ", "") or "outcome = 'failed'" in terminalize_sql


# ---------------------------------------------------------------------------
# persistence.record_extraction
# ---------------------------------------------------------------------------


def _extracted_action(**overrides):
    fields = dict(
        kind="payment", title="Pay invoice", due_at=None, due_precision=None,
        due_raw=None, amount=None, currency=None, confidence=0.9, model_version="gemini-x",
    )
    fields.update(overrides)
    return ExtractedAction(**fields)


def test_record_extraction_extracted_sets_all_fields():
    db = _FakeDB([_FakeResult(rowcount=1)])
    token = uuid4()

    ok = persistence.record_extraction(
        db, message_id=uuid4(), claim_token=token,
        result=_extracted_action(), thread_done=False, label_still_actionable=True,
    )

    assert ok is True
    params = _compiled_params(db.statements[0])
    assert params["outcome"] == "extracted"
    assert params["claim_token"] is None
    assert params["kind"] == "payment"
    assert params["title"] == "Pay invoice"
    assert params["model_version"] == "gemini-x"
    assert "status" not in params  # thread not done -- status never touched


def test_record_extraction_no_action_clears_stale_fields():
    db = _FakeDB([_FakeResult(rowcount=1)])

    persistence.record_extraction(
        db, message_id=uuid4(), claim_token=uuid4(),
        result=NoAction(), thread_done=False, label_still_actionable=True,
    )

    params = _compiled_params(db.statements[0])
    assert params["outcome"] == "no_action"
    for field in ("kind", "title", "due_at", "due_raw", "amount", "currency",
                  "source_confidence", "model_version"):
        assert params[field] is None


def test_record_extraction_failed_leaves_fields_untouched():
    db = _FakeDB([_FakeResult(rowcount=1)])

    persistence.record_extraction(
        db, message_id=uuid4(), claim_token=uuid4(),
        result=None, thread_done=False, label_still_actionable=True,
    )

    params = _compiled_params(db.statements[0])
    assert params["outcome"] == "failed"
    # No extraction-field keys at all -- a transient failure must not touch them.
    assert "kind" not in params
    assert "title" not in params


def test_record_extraction_label_mismatch_wins_as_ineligible():
    # Even a successful ExtractedAction result is discarded as ineligible if
    # the label left ACTION_LABELS while the call was in flight -- claimable,
    # not a terminal no_action, so a reclassify-back can re-extract.
    db = _FakeDB([_FakeResult(rowcount=1)])

    persistence.record_extraction(
        db, message_id=uuid4(), claim_token=uuid4(),
        result=_extracted_action(), thread_done=False, label_still_actionable=False,
    )

    params = _compiled_params(db.statements[0])
    assert params["outcome"] == "ineligible"
    assert "kind" not in params


def test_record_extraction_stale_token_affects_zero_rows():
    db = _FakeDB([_FakeResult(rowcount=0)])

    ok = persistence.record_extraction(
        db, message_id=uuid4(), claim_token=uuid4(),
        result=None, thread_done=False, label_still_actionable=True,
    )

    assert ok is False


@pytest.mark.parametrize(
    "result, label_still_actionable",
    [
        (_extracted_action(), True),
        (NoAction(), True),
        (None, True),
        (_extracted_action(), False),
    ],
    ids=["extracted", "no_action", "failed", "ineligible"],
)
def test_record_extraction_thread_done_stamps_status_on_every_branch(
    result, label_still_actionable
):
    db = _FakeDB([_FakeResult(rowcount=1)])

    persistence.record_extraction(
        db, message_id=uuid4(), claim_token=uuid4(),
        result=result, thread_done=True, label_still_actionable=label_still_actionable,
    )

    sql = _compiled_sql(db.statements[0])
    assert "CASE WHEN (action_item.status = 'open') THEN 'done' ELSE action_item.status END" in sql


# ---------------------------------------------------------------------------
# extraction_run._claim_extract_record
# ---------------------------------------------------------------------------


def test_claim_extract_record_missing_message_is_skipped():
    session = _FakeSession(message=None)

    bucket, user_id = extraction_run._claim_extract_record(session, uuid4())

    assert (bucket, user_id) == ("skipped", None)
    assert session.executed == []


def test_claim_extract_record_missing_thread_is_skipped():
    message = _make_message()
    session = _FakeSession(message=message, thread=None)

    bucket, user_id = extraction_run._claim_extract_record(session, message.id)

    assert (bucket, user_id) == ("skipped", None)


def test_claim_extract_record_not_claimable_skips_before_extraction(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: None)
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage",
        lambda **k: pytest.fail("extraction must not run without a claim"),
    )

    bucket, user_id = extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert bucket == "skipped"
    assert user_id == thread.user_id
    assert session.commits == 1  # released even though nothing was claimed


def test_claim_extract_record_releases_transaction_before_llm_call(monkeypatch):
    """Snapshot-then-release: no open transaction while extract_action runs."""
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)

    commits_observed = []

    def fake_extract(**kwargs):
        commits_observed.append(session.commits)
        return _extraction_attempt(NoAction())

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", fake_extract)

    extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert commits_observed == [1]  # claim already committed before the call


def test_claim_extract_record_locks_classification_before_reading_label(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    classification = _make_classification(label="needs_reply")
    session = _FakeSession(
        message=message, thread=thread, classification=classification, done_at_reads=[None]
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage", lambda **k: _extraction_attempt(NoAction())
    )
    recorded = {}

    def fake_record(db, **kwargs):
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(extraction_run, "record_extraction", fake_record)

    bucket, _ = extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    classification_stmt = next(
        s for s in session.executed if s.column_descriptions[0]["name"] == "Classification"
    )
    assert "FOR UPDATE" in _compiled_sql(classification_stmt, literal_binds=False).upper()
    assert recorded["label_still_actionable"] is True
    assert bucket == "no_action"


def test_claim_extract_record_thread_row_locked_for_update(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: None)

    extraction_run._claim_extract_record(session, message.id, force=False, call_context=_CTX)

    thread_stmt = next(
        s for s in session.executed if s.column_descriptions[0]["name"] == "MailThread"
    )
    assert "FOR UPDATE" in _compiled_sql(thread_stmt, literal_binds=False).upper()


def test_claim_extract_record_rereads_done_at_at_record_time(monkeypatch):
    # Thread wasn't done when claimed, but a concurrent set_thread_done
    # commits while the Gemini call is "in flight" -- the record must react
    # to the freshly re-read done_at, not the stale claim-time flag.
    message = _make_message()
    thread = _make_thread(id=message.thread_id, done_at=None)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[datetime.now(timezone.utc)],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage", lambda **k: _extraction_attempt(NoAction())
    )
    recorded = {}

    def fake_record(db, **kwargs):
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(extraction_run, "record_extraction", fake_record)

    extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert recorded["thread_done"] is True


def test_claim_extract_record_stale_record_is_skipped(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage", lambda **k: _extraction_attempt(NoAction())
    )
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: False)

    bucket, _ = extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert bucket == "skipped"


@pytest.mark.parametrize(
    "label, result, expected",
    [
        ("needs_reply", _extracted_action(), "extracted"),
        ("needs_reply", NoAction(), "no_action"),
        ("needs_reply", None, "failed"),
        ("other", _extracted_action(), "ineligible"),
    ],
    ids=["extracted", "no_action", "failed", "ineligible"],
)
def test_claim_extract_record_outcome_buckets(monkeypatch, label, result, expected):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(label=label), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage", lambda **k: _extraction_attempt(result)
    )
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)

    bucket, user_id = extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert bucket == expected
    assert user_id == thread.user_id


def test_claim_extract_record_force_forwarded_to_claim(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    captured = {}

    def fake_claim(db, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(extraction_run, "claim_action_item", fake_claim)

    extraction_run._claim_extract_record(session, message.id, force=True, call_context=_CTX)

    assert captured["force"] is True


def test_claim_extract_record_exception_after_claim_fences_to_failed(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    token = uuid4()
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: token)

    def _boom(**kwargs):
        raise RuntimeError("gemini call blew up")

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", _boom)

    fresh_session = MagicMock()
    monkeypatch.setattr(extraction_run, "SessionLocal", lambda: nullcontext(fresh_session))

    bucket, user_id = extraction_run._claim_extract_record(session, message.id, call_context=_CTX)

    assert bucket == "failed"
    assert user_id == thread.user_id
    assert session.rollbacks == 1
    fresh_session.execute.assert_called_once()
    fence_stmt = fresh_session.execute.call_args[0][0]
    fence_params = _compiled_params(fence_stmt)
    assert fence_params["outcome"] == "failed"
    assert fence_params["claim_token"] is None
    assert fence_params["claim_token_1"] == token
    fresh_session.commit.assert_called_once()


def test_claim_extract_record_containment_commit_failure_reraises_original(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())

    def _boom(**kwargs):
        raise RuntimeError("original failure")

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", _boom)

    fresh_session = MagicMock()
    fresh_session.commit.side_effect = RuntimeError("fencing commit also failed")
    monkeypatch.setattr(extraction_run, "SessionLocal", lambda: nullcontext(fresh_session))

    with pytest.raises(RuntimeError, match="original failure"):
        extraction_run._claim_extract_record(session, message.id, call_context=_CTX)


# ---------------------------------------------------------------------------
# _claim_extract_record: usage recording (plan §5's flush/commit contract)
# ---------------------------------------------------------------------------


def test_claim_extract_record_records_usage_only_for_user_payer(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage",
        lambda **k: _extraction_attempt(NoAction(), usage=usage),
    )

    fake_acc = _FakeAcc(thread.user_id)
    extraction_run._claim_extract_record(
        session, message.id, call_context=_USER_CTX, acc=fake_acc
    )

    assert fake_acc.records == [("extraction", _CRED.provider, usage, True)]


def test_claim_extract_record_fallback_payer_records_nothing(monkeypatch):
    # The regression guard for the source plumbing: without it, the
    # operator's key would silently get billed onto the user's readout.
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage",
        lambda **k: _extraction_attempt(NoAction(), usage=usage),
    )

    fake_acc = _FakeAcc(thread.user_id)
    extraction_run._claim_extract_record(
        session, message.id, call_context=_CTX, acc=fake_acc
    )

    assert fake_acc.records == []
    # Nothing recorded -- the flush block must skip cheaply too, not just
    # the record() call.
    assert "flush" not in fake_acc.calls
    assert "committed" in fake_acc.calls


def test_claim_extract_record_flush_happens_before_commit_and_committed_after(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    events = []
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
        events=events,
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage",
        lambda **k: _extraction_attempt(NoAction()),
    )

    fake_acc = _FakeAcc(thread.user_id, events=events)

    extraction_run._claim_extract_record(
        session, message.id, call_context=_USER_CTX, acc=fake_acc
    )

    # The leading "db.commit" is the claim-release commit (snapshot-then-
    # release, unrelated to usage) -- what matters is the tail: flush lands
    # before the business commit, and committed() only fires once that
    # commit has actually returned.
    assert events == ["db.commit", "db.flush", "acc.flush", "db.commit", "acc.committed"]


def test_claim_extract_record_failing_usage_flush_does_not_block_business_commit(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)
    monkeypatch.setattr(
        extraction_run, "extract_action_with_usage",
        lambda **k: _extraction_attempt(NoAction()),
    )

    fake_acc = _FakeAcc(thread.user_id, flush_raises=SQLAlchemyError("boom"))

    bucket, _ = extraction_run._claim_extract_record(
        session, message.id, call_context=_USER_CTX, acc=fake_acc
    )

    assert bucket == "no_action"  # the business write still landed
    assert session.commits == 2  # claim commit + the business commit, both intact
    assert fake_acc.calls.index("discard") < fake_acc.calls.index("committed")


def test_claim_extract_record_rollback_path_discards_the_batch(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())

    def _boom(**kwargs):
        raise RuntimeError("gemini call blew up")

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", _boom)
    fresh_session = MagicMock()
    monkeypatch.setattr(extraction_run, "SessionLocal", lambda: nullcontext(fresh_session))

    fake_acc = _FakeAcc(thread.user_id)
    bucket, _ = extraction_run._claim_extract_record(
        session, message.id, call_context=_USER_CTX, acc=fake_acc
    )

    assert bucket == "failed"
    # Nothing was ever recorded here (the call blew up before record()), but
    # the rollback path must discard unconditionally -- this is the exact
    # spot plan §5 calls out, so a batch a LATER exception rolled back can
    # never survive into the next flush.
    assert fake_acc.calls == ["discard"]


# ---------------------------------------------------------------------------
# run_extraction_for_message
# ---------------------------------------------------------------------------


def test_run_extraction_for_message_disabled_never_claims(monkeypatch):
    monkeypatch.setattr(settings, "action_extraction_enabled", False)
    monkeypatch.setattr(
        extraction_run, "_claim_extract_record",
        lambda *a, **k: pytest.fail("must not claim when disabled"),
    )

    result = extraction_run.run_extraction_for_message(MagicMock(), uuid4())

    assert result["status"] == "disabled"
    assert result["processed"] == 0


def test_run_extraction_for_message_delegates_and_includes_user_id(monkeypatch):
    _enable_extraction(monkeypatch)
    user_id = uuid4()
    monkeypatch.setattr(
        extraction_run, "_claim_extract_record", lambda db, mid, force=False: ("extracted", user_id)
    )

    result = extraction_run.run_extraction_for_message(MagicMock(), uuid4())

    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["extracted"] == 1
    assert result["user_id"] == str(user_id)


def test_run_extraction_for_message_no_credential_is_disabled_with_zero_attempts(monkeypatch):
    # The user id is only known once the thread loads inside
    # _claim_extract_record -- this drives the real function (not a
    # monkeypatched stand-in) to prove resolution happens before any claim.
    _enable_extraction(monkeypatch)
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=None),
    )
    monkeypatch.setattr(
        extraction_run, "claim_action_item",
        lambda *a, **k: pytest.fail("must not claim without a resolved credential"),
    )

    result = extraction_run.run_extraction_for_message(session, message.id)

    assert result == {"status": "disabled", **extraction_run._empty_counts()}
    assert session.commits == 1  # thread lock released, nothing claimed


def test_run_extraction_for_message_blocked_custom_row_is_disabled_not_fallback(monkeypatch):
    # Frozen policy: a stored-but-policy-blocked custom row (e.g. the
    # custom-endpoints flag turned off after the row was saved) must NOT
    # fall through to a claim -- it's simply unavailable, same as no row at
    # all with fallback off.
    _enable_extraction(monkeypatch)
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    blocked = ResolvedExtraction(
        credential=None, source=None, stored=True,
        blocked_reason="custom_disabled", revision=3, credential_id=uuid4(),
    )
    monkeypatch.setattr(extraction_run, "resolve_extraction_credential", lambda db, uid: blocked)
    monkeypatch.setattr(
        extraction_run, "claim_action_item",
        lambda *a, **k: pytest.fail("a policy-blocked stored row must not be claimed"),
    )

    result = extraction_run.run_extraction_for_message(session, message.id)

    assert result["status"] == "disabled"
    assert result["processed"] == 0


def test_run_extraction_for_message_threads_resolved_credential_to_extract_action(monkeypatch):
    _enable_extraction(monkeypatch)
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)
    captured = {}

    def fake_extract(**kwargs):
        captured.update(kwargs)
        return _extraction_attempt(NoAction())

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", fake_extract)

    extraction_run.run_extraction_for_message(session, message.id)

    assert captured["credential"] is _CRED


# ---------------------------------------------------------------------------
# run_extraction_sweep
# ---------------------------------------------------------------------------


def test_run_extraction_sweep_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "action_extraction_enabled", False)
    monkeypatch.setattr(
        extraction_run, "terminalize_expired_pending",
        lambda *a, **k: pytest.fail("must not run when disabled"),
    )

    result = extraction_run.run_extraction_sweep(MagicMock(), uuid4())

    assert result == {"status": "disabled", **extraction_run._empty_counts()}


def test_run_extraction_sweep_terminalizes_before_selecting_candidates(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    order = []
    monkeypatch.setattr(
        extraction_run, "terminalize_expired_pending",
        lambda db, user_id=None: order.append("terminalize"),
    )
    monkeypatch.setattr(
        extraction_run, "_message_driven_candidates",
        lambda *a, **k: (order.append("select"), [])[1],
    )

    extraction_run.run_extraction_sweep(MagicMock(), uuid4())

    assert order == ["terminalize", "select"]


def test_run_extraction_sweep_message_driven_by_default(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    monkeypatch.setattr(
        extraction_run, "_recovery_candidates",
        lambda *a, **k: pytest.fail("message-driven sweep must not use row-driven selection"),
    )
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(MagicMock(), uuid4())


def test_run_extraction_sweep_recovery_uses_row_driven_candidates(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    monkeypatch.setattr(
        extraction_run, "_message_driven_candidates",
        lambda *a, **k: pytest.fail("recovery sweep must not use message-driven selection"),
    )
    monkeypatch.setattr(extraction_run, "_recovery_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(MagicMock(), uuid4(), recovery=True)


def test_run_extraction_sweep_counts_a_bucket_per_message(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    message_ids = [uuid4(), uuid4(), uuid4()]
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: message_ids)
    buckets = iter(["extracted", "no_action", "failed"])
    monkeypatch.setattr(
        extraction_run, "_claim_extract_record",
        lambda db, mid, force=False, call_context=None, acc=None: (next(buckets), uuid4()),
    )

    result = extraction_run.run_extraction_sweep(MagicMock(), uuid4())

    assert result["processed"] == 3
    assert result["extracted"] == 1
    assert result["no_action"] == 1
    assert result["failed"] == 1


def test_run_extraction_sweep_isolates_one_bad_message_and_continues(monkeypatch):
    # Same fan-out isolation as dispatch_scheduled_syncs: a message that
    # blows up _claim_extract_record (an exception that even its own
    # containment couldn't fence) must not abort the rest of the sweep.
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    bad_message, good_message = uuid4(), uuid4()
    monkeypatch.setattr(
        extraction_run, "_message_driven_candidates",
        lambda *a, **k: [bad_message, good_message],
    )

    def fake_claim(db, mid, force=False, call_context=None, acc=None):
        if mid == bad_message:
            raise RuntimeError("boom")
        return "extracted", uuid4()

    monkeypatch.setattr(extraction_run, "_claim_extract_record", fake_claim)
    db = MagicMock()

    result = extraction_run.run_extraction_sweep(db, uuid4())

    assert result["processed"] == 2
    assert result["failed"] == 1
    assert result["extracted"] == 1
    db.rollback.assert_called_once()


def test_run_extraction_sweep_end_to_end_terminalizes_expired_at_cap_row(monkeypatch):
    """Not helper-only: drives run_extraction_sweep itself (not just
    terminalize_expired_pending directly) through a user whose only row is a
    stuck pending-at-cap claim, and confirms the terminalization UPDATE the
    sweep issues actually targets it before candidate selection runs."""
    _enable_extraction(monkeypatch)
    user_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1)])
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=_CRED, source="fallback"),
    )
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(db, user_id)

    assert len(db.statements) == 1
    sql = _compiled_sql(db.statements[0])
    assert "outcome = 'pending'" in sql
    assert "attempts >= 3" in sql
    params = _compiled_params(db.statements[0])
    assert params["user_id_1"] == user_id


def test_run_extraction_sweep_no_credential_is_disabled_with_zero_attempts(monkeypatch):
    # Covers both "no stored row, fallback off" (BYOK-only mode) and "stored
    # custom row currently policy-blocked" -- from the sweep's perspective
    # they're identical: resolve_extraction_credential returned no usable
    # credential, so the sweep must behave exactly like the flag being off.
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: SimpleNamespace(credential=None),
    )
    monkeypatch.setattr(
        extraction_run, "terminalize_expired_pending",
        lambda *a, **k: pytest.fail("must not terminalize without a resolved credential"),
    )

    result = extraction_run.run_extraction_sweep(MagicMock(), uuid4())

    assert result == {"status": "disabled", **extraction_run._empty_counts()}


def test_run_extraction_sweep_resolves_credential_once_and_threads_it(monkeypatch):
    # The fallback (synthetic gemini) credential resolves once for the whole
    # run and is passed unchanged to every _claim_extract_record call --
    # never re-resolved per message. Its "fallback" source must map to an
    # "operator" payer, and one accumulator must be shared across every call
    # in the run, not rebuilt per message.
    _enable_extraction(monkeypatch)
    fallback_credential = LlmCredential(
        provider="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="server-key", model="gemini-2.0-flash",
    )
    resolved = ResolvedExtraction(
        credential=fallback_credential, source="fallback", stored=False,
        blocked_reason=None, revision=None, credential_id=None,
    )
    resolve_calls = []
    monkeypatch.setattr(
        extraction_run, "resolve_extraction_credential",
        lambda db, uid: (resolve_calls.append(uid), resolved)[1],
    )
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    message_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: message_ids)
    captured_call_contexts = []
    captured_accs = []

    def fake_claim(db, mid, force=False, call_context=None, acc=None):
        captured_call_contexts.append(call_context)
        captured_accs.append(acc)
        return "extracted", uuid4()

    monkeypatch.setattr(extraction_run, "_claim_extract_record", fake_claim)

    user_id = uuid4()
    extraction_run.run_extraction_sweep(MagicMock(), user_id)

    assert resolve_calls == [user_id]  # exactly one resolution for the whole run
    assert [ctx.credential for ctx in captured_call_contexts] == [
        fallback_credential, fallback_credential
    ]
    assert [ctx.payer for ctx in captured_call_contexts] == ["operator", "operator"]
    assert captured_accs[0] is captured_accs[1]  # one shared accumulator, not one per message


# ---------------------------------------------------------------------------
# Recovery-tick user-set derivation (structural: no live DB to run these
# joins against, so we assert the compiled SQL captures the frozen rules).
# ---------------------------------------------------------------------------


def test_recovery_candidates_ignore_since_days_and_skip_done_threads():
    db = _FakeDB([_FakeResult()])
    db.execute = lambda stmt: (db.statements.append(stmt), _FakeResult())[1]
    extraction_run._recovery_candidates(db, uuid4(), limit=25)

    sql = _compiled_sql(db.statements[0])
    assert "sent_at" not in sql  # row-driven: no message-age filter at all
    assert "mail_thread.done_at IS NULL" in sql
    assert "action_item.outcome = 'ineligible'" in sql
    assert "classification.label IN" in sql


def test_users_with_unclaimed_actionable_messages_requires_no_existing_row():
    db = _FakeDB([_FakeResult()])
    db.execute = lambda stmt: (db.statements.append(stmt), _FakeResult())[1]
    extraction_run.users_with_unclaimed_actionable_messages(db)

    sql = _compiled_sql(db.statements[0])
    assert "action_item.id IS NULL" in sql
    assert "classification.label IN" in sql
    assert "mail_thread.done_at IS NULL" in sql
    # A NULL sent_at (possible by design) must not make the message
    # permanently invisible to the recovery pass -- coalesce to created_at.
    assert "coalesce(mail_message.sent_at, mail_message.created_at) >=" in sql


def test_message_driven_candidates_force_widens_existing_row_filter():
    db = _FakeDB([_FakeResult()])
    db.execute = lambda stmt: (db.statements.append(stmt), _FakeResult())[1]
    extraction_run._message_driven_candidates(
        db, uuid4(), since_days=30, limit=10, force=True
    )

    sql = _compiled_sql(db.statements[0])
    assert "action_item.outcome != 'pending'" in sql


def test_message_driven_candidates_coalesces_null_sent_at_for_cutoff_and_order():
    db = _FakeDB([_FakeResult()])
    db.execute = lambda stmt: (db.statements.append(stmt), _FakeResult())[1]
    extraction_run._message_driven_candidates(
        db, uuid4(), since_days=30, limit=10, force=False
    )

    sql = _compiled_sql(db.statements[0])
    assert "coalesce(mail_message.sent_at, mail_message.created_at) >=" in sql
    assert (
        "ORDER BY coalesce(mail_message.sent_at, mail_message.created_at) "
        "DESC NULLS LAST" in sql
    )


# ---------------------------------------------------------------------------
# tasks_nlp.py Celery tasks
# ---------------------------------------------------------------------------


def test_extract_action_for_message_autoretry_shape_matches_backfill():
    for attr, expected in (
        ("autoretry_for", (Exception,)),
        ("max_retries", 3),
        ("retry_backoff", True),
        ("time_limit", 1800),
        ("soft_time_limit", 1740),
    ):
        assert getattr(tasks_nlp.extract_action_for_message, attr) == expected
        assert getattr(tasks_nlp.extract_actions_for_user, attr) == expected


def test_extract_action_for_message_delegates(monkeypatch):
    message_id = uuid4()
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        tasks_nlp, "run_extraction_for_message",
        lambda db, mid: {"status": "ok", "processed": 1, "extracted": 1},
    )

    result = tasks_nlp.extract_action_for_message.run(str(message_id))

    assert result == {
        "message_id": str(message_id), "status": "ok", "processed": 1, "extracted": 1
    }


def test_extract_actions_for_user_delegates_and_includes_user_id(monkeypatch):
    user_id = uuid4()
    captured = {}

    def fake_sweep(db, uid, **kwargs):
        captured["user_id"] = uid
        captured.update(kwargs)
        return {"status": "ok", "processed": 0}

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_nlp, "run_extraction_sweep", fake_sweep)

    result = tasks_nlp.extract_actions_for_user.run(str(user_id), limit=50, force=True)

    assert captured["user_id"] == user_id
    assert captured["limit"] == 50
    assert captured["force"] is True
    assert result["user_id"] == str(user_id)


def test_extraction_recovery_tick_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(tasks_nlp, "extraction_feature_enabled", lambda: False)
    monkeypatch.setattr(
        tasks_nlp, "SessionLocal", lambda: pytest.fail("must not open a session when disabled")
    )

    result = tasks_nlp.extraction_recovery_tick.run()

    assert result == {"status": "disabled"}


def test_extraction_recovery_tick_sweeps_two_independent_user_sets(monkeypatch):
    # message_user owns ZERO action_item rows -- the pass-6 regression this
    # tick exists to close: a rows-only user set can never see it.
    row_user = uuid4()
    message_user = uuid4()
    calls = []

    monkeypatch.setattr(tasks_nlp, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        tasks_nlp, "terminalize_expired_pending", lambda db: calls.append(("terminalize",))
    )
    monkeypatch.setattr(
        tasks_nlp, "users_with_claimable_action_items", lambda db: [row_user]
    )
    monkeypatch.setattr(
        tasks_nlp, "users_with_unclaimed_actionable_messages", lambda db: [message_user]
    )

    def fake_sweep(db, user_id, *, limit, recovery=False, **kwargs):
        calls.append(("sweep", user_id, limit, recovery))
        return {"status": "ok"}

    monkeypatch.setattr(tasks_nlp, "run_extraction_sweep", fake_sweep)

    result = tasks_nlp.extraction_recovery_tick.run()

    assert calls[0] == ("terminalize",)  # global, before either sweep
    assert ("sweep", row_user, 25, True) in calls
    assert ("sweep", message_user, 25, False) in calls
    assert result == {"status": "ok", "row_driven_users": 1, "message_driven_users": 1}


def test_extraction_recovery_tick_skips_credential_less_users_via_sweep_preflight(monkeypatch):
    # The tick's user-set queries are deliberately credential-agnostic (a
    # credential filter there is an optimization, not correctness) --
    # run_extraction_sweep's OWN preflight is what skips a credential-less
    # user, burning nothing but a resolve call. Uses the REAL
    # run_extraction_sweep (not mocked) to prove that.
    _enable_extraction(monkeypatch)
    credentialed_user, credential_less_user = uuid4(), uuid4()

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        tasks_nlp, "users_with_claimable_action_items",
        lambda db: [credentialed_user, credential_less_user],
    )
    monkeypatch.setattr(tasks_nlp, "users_with_unclaimed_actionable_messages", lambda db: [])

    def fake_resolve(db, user_id):
        credential = _CRED if user_id == credentialed_user else None
        return SimpleNamespace(credential=credential, source="fallback" if credential else None)

    monkeypatch.setattr(extraction_run, "resolve_extraction_credential", fake_resolve)
    candidate_queries = []
    monkeypatch.setattr(
        extraction_run, "_recovery_candidates",
        lambda db, user_id, *, limit: (candidate_queries.append(user_id), [])[1],
    )

    result = tasks_nlp.extraction_recovery_tick.run()

    # Only the credentialed user's sweep got far enough to select
    # candidates -- the other bailed at its own preflight, zero attempts.
    assert candidate_queries == [credentialed_user]
    assert result == {"status": "ok", "row_driven_users": 2, "message_driven_users": 0}


def test_extraction_recovery_tick_isolates_one_bad_user_and_continues(monkeypatch):
    # One user's sweep blowing up must not abort the tick for the rest --
    # same fan-out isolation as dispatch_scheduled_syncs.
    bad_user, good_user = uuid4(), uuid4()
    swept = []
    db = MagicMock()

    monkeypatch.setattr(tasks_nlp, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_nlp, "terminalize_expired_pending", lambda db: None)
    monkeypatch.setattr(
        tasks_nlp, "users_with_claimable_action_items", lambda db: [bad_user, good_user]
    )
    monkeypatch.setattr(tasks_nlp, "users_with_unclaimed_actionable_messages", lambda db: [])

    def fake_sweep(db, user_id, *, limit, recovery=False, **kwargs):
        swept.append(user_id)
        if user_id == bad_user:
            raise RuntimeError("boom")
        return {"status": "ok"}

    monkeypatch.setattr(tasks_nlp, "run_extraction_sweep", fake_sweep)

    result = tasks_nlp.extraction_recovery_tick.run()

    assert swept == [bad_user, good_user]  # bad user didn't stop the good one
    db.rollback.assert_called_once()
    assert result == {"status": "ok", "row_driven_users": 1, "message_driven_users": 0}


def test_extraction_recovery_tick_returns_partial_counts_on_soft_time_limit(monkeypatch):
    user_a, user_b = uuid4(), uuid4()

    monkeypatch.setattr(tasks_nlp, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_nlp, "terminalize_expired_pending", lambda db: None)
    monkeypatch.setattr(
        tasks_nlp, "users_with_claimable_action_items", lambda db: [user_a, user_b]
    )
    monkeypatch.setattr(tasks_nlp, "users_with_unclaimed_actionable_messages", lambda db: [])

    def fake_sweep(db, user_id, *, limit, recovery=False, **kwargs):
        if user_id == user_b:
            raise tasks_nlp.SoftTimeLimitExceeded()
        return {"status": "ok"}

    monkeypatch.setattr(tasks_nlp, "run_extraction_sweep", fake_sweep)

    # A hard kill (time_limit=300) has no chance to fence a claim mid-flight
    # -- the soft limit must stop the tick cleanly instead of propagating
    # and burning the whole tick's work with an unhandled SIGKILL.
    result = tasks_nlp.extraction_recovery_tick.run()

    assert result == {"status": "timed_out", "row_driven_users": 1, "message_driven_users": 0}


# ---------------------------------------------------------------------------
# tasks_ingest.py enqueue hook
# ---------------------------------------------------------------------------


def test_enqueue_action_extraction_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda db, uid: False)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue when disabled"),
    )

    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 5})


def test_enqueue_action_extraction_skips_without_opening_a_session_when_feature_flag_off(
    monkeypatch,
):
    # ACTION_EXTRACTION_ENABLED defaults to false -- the cheap flag check
    # must short-circuit before the per-user availability lookup opens any
    # session at all, not just before enqueueing.
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: False)
    monkeypatch.setattr(
        tasks_ingest, "SessionLocal",
        lambda: pytest.fail("must not open a session when the feature flag is off"),
    )
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue when the feature flag is off"),
    )

    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 5})


def test_enqueue_action_extraction_skips_when_nothing_upserted(monkeypatch):
    # Checked before the availability lookup opens any session -- a quiet
    # ingest must not cost a DB round trip.
    monkeypatch.setattr(
        tasks_ingest, "SessionLocal",
        lambda: pytest.fail("must not open a session with zero upserts"),
    )
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue with zero upserts"),
    )

    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 0})


def test_enqueue_action_extraction_fires_when_available_and_upserted(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda db, uid: True)
    calls = []
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda user_id, limit: calls.append((user_id, limit)),
    )

    user_id = str(uuid4())
    tasks_ingest._enqueue_action_extraction(user_id, {"messages_upserted": 3})

    assert calls == [(user_id, 50)]


def test_enqueue_action_extraction_opens_its_own_session_for_the_lookup(monkeypatch):
    # The ingest task's own `with SessionLocal()` block has already exited
    # by the time this hook runs -- it must open a fresh one, not reuse a
    # closed session.
    opened = []

    def fake_session_local():
        opened.append(True)
        return nullcontext(MagicMock())

    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "SessionLocal", fake_session_local)
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda db, uid: True)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay", lambda user_id, limit: None
    )

    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 3})

    assert opened == [True]


def test_enqueue_action_extraction_swallows_broker_failure(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda db, uid: True)

    def _boom(*a, **k):
        raise RuntimeError("broker is down")

    monkeypatch.setattr(tasks_ingest.extract_actions_for_user, "delay", _boom)

    # Must not raise -- an enqueue failure can never fail an ingest that
    # already succeeded.
    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 3})


def test_enqueue_action_extraction_availability_lookup_exception_does_not_fail_ingest(
    monkeypatch,
):
    def _boom_session_local():
        raise RuntimeError("db is down")

    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "SessionLocal", _boom_session_local)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue if the availability lookup blew up"),
    )

    # Must not raise -- a DB hiccup in the availability lookup can never
    # fail (or retry) an ingest that already succeeded.
    tasks_ingest._enqueue_action_extraction(str(uuid4()), {"messages_upserted": 3})


def test_ingest_gmail_for_user_enqueues_extraction_after_success(monkeypatch):
    run = SimpleNamespace(
        status="queued", heartbeat_at=None, lease_expires_at=None,
        started_at=None, error=None, result=None, completed_at=None,
    )
    state_db = MagicMock()
    state_db.get.return_value = run
    ingest_db = MagicMock()
    enqueue_db = MagicMock()
    sessions = iter([state_db, ingest_db, enqueue_db, state_db])
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(next(sessions)))
    monkeypatch.setattr(
        tasks_ingest, "ingest_gmail_messages",
        lambda **kwargs: {"threads_upserted": 1, "messages_upserted": 1},
    )
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda db, uid: True)
    calls = []
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda user_id, limit: calls.append((user_id, limit)),
    )

    user_id = str(uuid4())
    result = tasks_ingest.ingest_gmail_for_user.run(run_id=str(uuid4()), user_id=user_id)

    assert result["status"] == "ok"
    assert calls == [(user_id, 50)]


def test_ingest_gmail_for_user_succeeds_even_if_enqueue_availability_lookup_blows_up(
    monkeypatch,
):
    # The whole point of the hook opening its own session inside its own
    # try/except: an ingest that already succeeded must report success even
    # when the post-ingest enqueue's DB lookup fails.
    run = SimpleNamespace(
        status="queued", heartbeat_at=None, lease_expires_at=None,
        started_at=None, error=None, result=None, completed_at=None,
    )
    state_db = MagicMock()
    state_db.get.return_value = run
    ingest_db = MagicMock()
    # Calls in order: set_state("running"), the ingest itself, the enqueue
    # hook's own availability-lookup session (blows up), set_state
    # ("succeeded") -- only the third call fails.
    call_count = [0]

    def fake_session_local():
        call_count[0] += 1
        if call_count[0] == 1:
            return nullcontext(state_db)
        if call_count[0] == 2:
            return nullcontext(ingest_db)
        if call_count[0] == 3:
            raise RuntimeError("enqueue's availability lookup can't get a session")
        return nullcontext(MagicMock())

    monkeypatch.setattr(tasks_ingest, "SessionLocal", fake_session_local)
    monkeypatch.setattr(
        tasks_ingest, "ingest_gmail_messages",
        lambda **kwargs: {"threads_upserted": 1, "messages_upserted": 1},
    )
    monkeypatch.setattr(tasks_ingest, "extraction_feature_enabled", lambda: True)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue if the availability lookup blew up"),
    )

    user_id = str(uuid4())
    result = tasks_ingest.ingest_gmail_for_user.run(run_id=str(uuid4()), user_id=user_id)

    assert result["status"] == "ok"
