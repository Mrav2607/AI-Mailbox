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
extract_action are monkeypatched per test, since they're covered in
isolation elsewhere in this file. Controlled call ordering stands in for
real cross-connection lock contention, which is exercised in live QA.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.db.models import MailMessage
from app.services.nlp import extraction_run, persistence
from app.services.nlp.extractor import ExtractedAction, NoAction
from app.workers import tasks_ingest, tasks_nlp


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

    def __init__(self, *, message=None, thread=None, classification=None, done_at_reads=None):
        self.message = message
        self.thread = thread
        self.classification = classification
        self._done_at_reads = list(done_at_reads or [])
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

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

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _enable_extraction(monkeypatch):
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")


# ---------------------------------------------------------------------------
# extraction_available()
# ---------------------------------------------------------------------------


def test_extraction_available_requires_flag_and_key(monkeypatch):
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    assert extraction_run.extraction_available() is False

    monkeypatch.setattr(settings, "action_extraction_enabled", False)
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    assert extraction_run.extraction_available() is False

    _enable_extraction(monkeypatch)
    assert extraction_run.extraction_available() is True


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
        extraction_run, "extract_action",
        lambda **k: pytest.fail("extract_action must not run without a claim"),
    )

    bucket, user_id = extraction_run._claim_extract_record(session, message.id)

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
        return NoAction()

    monkeypatch.setattr(extraction_run, "extract_action", fake_extract)

    extraction_run._claim_extract_record(session, message.id)

    assert commits_observed == [1]  # claim already committed before the call


def test_claim_extract_record_locks_classification_before_reading_label(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    classification = _make_classification(label="needs_reply")
    session = _FakeSession(
        message=message, thread=thread, classification=classification, done_at_reads=[None]
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "extract_action", lambda **k: NoAction())
    recorded = {}

    def fake_record(db, **kwargs):
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(extraction_run, "record_extraction", fake_record)

    bucket, _ = extraction_run._claim_extract_record(session, message.id)

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

    extraction_run._claim_extract_record(session, message.id, force=False)

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
    monkeypatch.setattr(extraction_run, "extract_action", lambda **k: NoAction())
    recorded = {}

    def fake_record(db, **kwargs):
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(extraction_run, "record_extraction", fake_record)

    extraction_run._claim_extract_record(session, message.id)

    assert recorded["thread_done"] is True


def test_claim_extract_record_stale_record_is_skipped(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(
        message=message, thread=thread,
        classification=_make_classification(), done_at_reads=[None],
    )
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: uuid4())
    monkeypatch.setattr(extraction_run, "extract_action", lambda **k: NoAction())
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: False)

    bucket, _ = extraction_run._claim_extract_record(session, message.id)

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
    monkeypatch.setattr(extraction_run, "extract_action", lambda **k: result)
    monkeypatch.setattr(extraction_run, "record_extraction", lambda *a, **k: True)

    bucket, user_id = extraction_run._claim_extract_record(session, message.id)

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

    extraction_run._claim_extract_record(session, message.id, force=True)

    assert captured["force"] is True


def test_claim_extract_record_exception_after_claim_fences_to_failed(monkeypatch):
    message = _make_message()
    thread = _make_thread(id=message.thread_id)
    session = _FakeSession(message=message, thread=thread)
    token = uuid4()
    monkeypatch.setattr(extraction_run, "claim_action_item", lambda *a, **k: token)

    def _boom(**kwargs):
        raise RuntimeError("gemini call blew up")

    monkeypatch.setattr(extraction_run, "extract_action", _boom)

    fresh_session = MagicMock()
    monkeypatch.setattr(extraction_run, "SessionLocal", lambda: nullcontext(fresh_session))

    bucket, user_id = extraction_run._claim_extract_record(session, message.id)

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

    monkeypatch.setattr(extraction_run, "extract_action", _boom)

    fresh_session = MagicMock()
    fresh_session.commit.side_effect = RuntimeError("fencing commit also failed")
    monkeypatch.setattr(extraction_run, "SessionLocal", lambda: nullcontext(fresh_session))

    with pytest.raises(RuntimeError, match="original failure"):
        extraction_run._claim_extract_record(session, message.id)


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
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    monkeypatch.setattr(
        extraction_run, "_recovery_candidates",
        lambda *a, **k: pytest.fail("message-driven sweep must not use row-driven selection"),
    )
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(MagicMock(), uuid4())


def test_run_extraction_sweep_recovery_uses_row_driven_candidates(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    monkeypatch.setattr(
        extraction_run, "_message_driven_candidates",
        lambda *a, **k: pytest.fail("recovery sweep must not use message-driven selection"),
    )
    monkeypatch.setattr(extraction_run, "_recovery_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(MagicMock(), uuid4(), recovery=True)


def test_run_extraction_sweep_counts_a_bucket_per_message(monkeypatch):
    _enable_extraction(monkeypatch)
    monkeypatch.setattr(extraction_run, "terminalize_expired_pending", lambda *a, **k: None)
    message_ids = [uuid4(), uuid4(), uuid4()]
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: message_ids)
    buckets = iter(["extracted", "no_action", "failed"])
    monkeypatch.setattr(
        extraction_run, "_claim_extract_record",
        lambda db, mid, force=False: (next(buckets), uuid4()),
    )

    result = extraction_run.run_extraction_sweep(MagicMock(), uuid4())

    assert result["processed"] == 3
    assert result["extracted"] == 1
    assert result["no_action"] == 1
    assert result["failed"] == 1


def test_run_extraction_sweep_end_to_end_terminalizes_expired_at_cap_row(monkeypatch):
    """Not helper-only: drives run_extraction_sweep itself (not just
    terminalize_expired_pending directly) through a user whose only row is a
    stuck pending-at-cap claim, and confirms the terminalization UPDATE the
    sweep issues actually targets it before candidate selection runs."""
    _enable_extraction(monkeypatch)
    user_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1)])
    monkeypatch.setattr(extraction_run, "_message_driven_candidates", lambda *a, **k: [])

    extraction_run.run_extraction_sweep(db, user_id)

    assert len(db.statements) == 1
    sql = _compiled_sql(db.statements[0])
    assert "outcome = 'pending'" in sql
    assert "attempts >= 3" in sql
    params = _compiled_params(db.statements[0])
    assert params["user_id_1"] == user_id


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


def test_message_driven_candidates_force_widens_existing_row_filter():
    db = _FakeDB([_FakeResult()])
    db.execute = lambda stmt: (db.statements.append(stmt), _FakeResult())[1]
    extraction_run._message_driven_candidates(
        db, uuid4(), since_days=30, limit=10, force=True
    )

    sql = _compiled_sql(db.statements[0])
    assert "action_item.outcome != 'pending'" in sql


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
    monkeypatch.setattr(tasks_nlp, "extraction_available", lambda: False)
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

    monkeypatch.setattr(tasks_nlp, "extraction_available", lambda: True)
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


# ---------------------------------------------------------------------------
# tasks_ingest.py enqueue hook
# ---------------------------------------------------------------------------


def test_enqueue_action_extraction_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda: False)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue when disabled"),
    )

    tasks_ingest._enqueue_action_extraction("user-1", {"messages_upserted": 5})


def test_enqueue_action_extraction_skips_when_nothing_upserted(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda: True)
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda *a, **k: pytest.fail("must not enqueue with zero upserts"),
    )

    tasks_ingest._enqueue_action_extraction("user-1", {"messages_upserted": 0})


def test_enqueue_action_extraction_fires_when_available_and_upserted(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda user_id, limit: calls.append((user_id, limit)),
    )

    tasks_ingest._enqueue_action_extraction("user-1", {"messages_upserted": 3})

    assert calls == [("user-1", 50)]


def test_enqueue_action_extraction_swallows_broker_failure(monkeypatch):
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("broker is down")

    monkeypatch.setattr(tasks_ingest.extract_actions_for_user, "delay", _boom)

    # Must not raise -- an enqueue failure can never fail an ingest that
    # already succeeded.
    tasks_ingest._enqueue_action_extraction("user-1", {"messages_upserted": 3})


def test_ingest_gmail_for_user_enqueues_extraction_after_success(monkeypatch):
    run = SimpleNamespace(
        status="queued", heartbeat_at=None, lease_expires_at=None,
        started_at=None, error=None, result=None, completed_at=None,
    )
    state_db = MagicMock()
    state_db.get.return_value = run
    ingest_db = MagicMock()
    sessions = iter([state_db, ingest_db, state_db])
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(next(sessions)))
    monkeypatch.setattr(
        tasks_ingest, "ingest_gmail_messages",
        lambda **kwargs: {"threads_upserted": 1, "messages_upserted": 1},
    )
    monkeypatch.setattr(tasks_ingest, "extraction_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        tasks_ingest.extract_actions_for_user, "delay",
        lambda user_id, limit: calls.append((user_id, limit)),
    )

    user_id = str(uuid4())
    result = tasks_ingest.ingest_gmail_for_user.run(run_id=str(uuid4()), user_id=user_id)

    assert result["status"] == "ok"
    assert calls == [(user_id, 50)]
