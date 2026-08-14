"""Tests for app/services/mail_send/reconcile.py: the level-pass
reconciliation required by plan §3.5, offline only. `common.py`'s CAS
helpers (mark_sent/advance_fence_and_resolve/stamp_verified) and the
classification pipeline are monkeypatched at their reconcile.py call sites
so these tests exercise reconcile.py's OWN orchestration -- lock ordering,
per-attempt isolation, the classification claim -- without needing a real
database (mirrors test_extraction_run.py's decomposition: persistence.py's
SQL shape is tested separately from extraction_run.py's control flow).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.db.models import MailThread, ReplyAttempt
from app.services.mail_send import reconcile


def _compiled_sql(stmt, literal_binds=True):
    kwargs = {"compile_kwargs": {"literal_binds": True}} if literal_binds else {}
    return str(stmt.compile(dialect=postgresql.dialect(), **kwargs))


def _attempt(
    *,
    id=None,
    thread_id=None,
    provider="gmail",
    status="inflight",
    gmail_message_id_header=None,
    provider_message_id=None,
    verified_at=None,
    created_at=None,
):
    return SimpleNamespace(
        id=id or uuid4(),
        thread_id=thread_id or uuid4(),
        provider=provider,
        status=status,
        gmail_message_id_header=gmail_message_id_header,
        provider_message_id=provider_message_id,
        verified_at=verified_at,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _thread(*, id=None, user_id=None):
    return SimpleNamespace(id=id or uuid4(), user_id=user_id or uuid4())


def _message(*, id=None, thread_id=None, provider_message_id="m1", headers=None, snippet="hi", body_text="hi"):
    return SimpleNamespace(
        id=id or uuid4(),
        thread_id=thread_id or uuid4(),
        provider_message_id=provider_message_id,
        headers=headers or {},
        snippet=snippet,
        body_text=body_text,
    )


class _FakeDB:
    """Answers db.get() from in-memory dicts and db.execute() with enough
    to drive the `verified_at` re-read in `_classify_and_stamp` -- every
    other statement reconcile.py issues is either discarded (lock-acquire
    selects) or goes through a monkeypatched helper, so this fake never
    needs to understand their SQL.
    """

    def __init__(self, *, attempts=(), threads=(), verified_at_reads=None):
        self._attempts = {a.id: a for a in attempts}
        self._threads = {t.id: t for t in threads}
        self._verified_at_reads = list(verified_at_reads) if verified_at_reads is not None else None
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    def get(self, model, pk):
        if model is ReplyAttempt:
            return self._attempts.get(pk)
        if model is MailThread:
            return self._threads.get(pk)
        raise AssertionError(f"unexpected get({model!r}, {pk!r})")

    def execute(self, stmt):
        self.executed.append(stmt)
        sql = str(stmt).lower()
        result = MagicMock()
        if "reply_attempt.verified_at" in sql:
            if self._verified_at_reads is not None:
                result.scalar_one_or_none.return_value = self._verified_at_reads.pop(0)
            else:
                result.scalar_one_or_none.return_value = None
        return result

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# _eligible_attempt_ids -- account scoping + status predicate shape
# ---------------------------------------------------------------------------


def test_eligible_attempt_ids_scopes_by_account_and_provider():
    account_id = uuid4()

    class _EligibleDB:
        def execute(self, stmt):
            captured["stmt"] = stmt
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

    captured = {}
    reconcile._eligible_attempt_ids(_EligibleDB(), provider_account_id=account_id, provider="gmail")
    sql = _compiled_sql(captured["stmt"])
    assert f"'{account_id}'" in sql
    assert "'gmail'" in sql
    for status in ("'preparing'", "'inflight'", "'unknown'", "'abandoned'"):
        assert status in sql
    # Outlook's extra sent-but-unverified branch names 'outlook' and 'sent'
    # alongside the blocking-status list, not instead of it.
    assert "'outlook'" in sql
    assert "'sent'" in sql


def test_eligible_attempt_ids_returns_the_scalar_ids():
    ids = [uuid4(), uuid4()]

    class _EligibleDB:
        def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = ids
            return result

    result = reconcile._eligible_attempt_ids(_EligibleDB(), provider_account_id=uuid4(), provider="outlook")
    assert result == ids


# ---------------------------------------------------------------------------
# _match_message -- Gmail Message-ID header matching, Outlook provider id
# ---------------------------------------------------------------------------


def test_match_message_gmail_matches_case_insensitive_trimmed_header():
    thread_id = uuid4()
    attempt = _attempt(provider="gmail", gmail_message_id_header="<abc@cortexmail.app>")
    target = _message(thread_id=thread_id, headers={"message-id": "  <abc@cortexmail.app>  "})
    other = _message(thread_id=thread_id, headers={"Message-ID": "<other@x>"})

    class _MatchDB:
        def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [other, target]
            return result

    matched = reconcile._match_message(_MatchDB(), attempt=attempt, thread_id=thread_id)
    assert matched is target


def test_match_message_gmail_matches_on_provider_message_id_too():
    thread_id = uuid4()
    attempt = _attempt(provider="gmail", provider_message_id="resp-1", gmail_message_id_header=None)
    target = _message(thread_id=thread_id, provider_message_id="resp-1", headers={})

    class _MatchDB:
        def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [target]
            return result

    matched = reconcile._match_message(_MatchDB(), attempt=attempt, thread_id=thread_id)
    assert matched is target


def test_match_message_gmail_no_candidates_matches_returns_none():
    thread_id = uuid4()
    attempt = _attempt(provider="gmail", gmail_message_id_header="<abc@cortexmail.app>")
    unrelated = _message(thread_id=thread_id, headers={"Message-ID": "<different@x>"})

    class _MatchDB:
        def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [unrelated]
            return result

    assert reconcile._match_message(_MatchDB(), attempt=attempt, thread_id=thread_id) is None


def test_match_message_outlook_matches_provider_message_id():
    thread_id = uuid4()
    attempt = _attempt(provider="outlook", provider_message_id="draft-immutable-1")
    target = _message(thread_id=thread_id, provider_message_id="draft-immutable-1")

    class _MatchDB:
        def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.first.return_value = target
            return result

    matched = reconcile._match_message(_MatchDB(), attempt=attempt, thread_id=thread_id)
    assert matched is target


def test_match_message_outlook_without_provider_message_id_never_queries():
    attempt = _attempt(provider="outlook", provider_message_id=None)

    class _ExplodingDB:
        def execute(self, stmt):
            raise AssertionError("should never query without a provider_message_id")

    assert reconcile._match_message(_ExplodingDB(), attempt=attempt, thread_id=uuid4()) is None


# ---------------------------------------------------------------------------
# Orchestration: _reconcile_one_attempt / run_reconciliation_pass
# ---------------------------------------------------------------------------


def _patch_settle(monkeypatch, *, mark_sent_result=True, resolved=1):
    calls = {"mark_sent": [], "advance_fence": []}

    def fake_mark_sent(db, **kwargs):
        calls["mark_sent"].append(kwargs)
        return mark_sent_result

    def fake_advance(db, **kwargs):
        calls["advance_fence"].append(kwargs)
        return resolved

    monkeypatch.setattr(reconcile, "mark_sent", fake_mark_sent)
    monkeypatch.setattr(reconcile, "advance_fence_and_resolve", fake_advance)
    return calls


def _patch_classification(monkeypatch, *, raises=None):
    calls = {"classify": 0}

    def fake_classify(text, *, routing):
        calls["classify"] += 1
        if raises:
            raise raises
        return SimpleNamespace(
            verdict=("needs_reply", 0.9, "why", "heuristic-fallback"),
            provider_call_succeeded=False,
            usage=None,
        )

    class _FakeRouting:
        mode = "server"
        credential = None

    class _FakeRouter:
        def __init__(self, user_id):
            self.user_id = user_id

        def routing_for(self, db):
            return _FakeRouting()

    stamp_calls = []

    monkeypatch.setattr(reconcile, "build_classification_text", lambda *a: "text")
    monkeypatch.setattr(reconcile, "ClassificationRouter", _FakeRouter)
    monkeypatch.setattr(reconcile, "classify_with_usage", fake_classify)
    monkeypatch.setattr(reconcile, "upsert_classification", lambda db, **k: "written")
    monkeypatch.setattr(
        reconcile, "stamp_verified", lambda db, **kwargs: stamp_calls.append(kwargs)
    )
    return calls, stamp_calls


def test_settle_match_marks_sent_only_when_not_already_sent(monkeypatch):
    calls = _patch_settle(monkeypatch, resolved=2)
    attempt = _attempt(status="inflight")
    message = _message()
    db = _FakeDB()

    outcome = reconcile._settle_match(db, attempt=attempt, thread_id=attempt.thread_id, message=message)

    assert outcome == {"resolved_action_items": 2}
    assert len(calls["mark_sent"]) == 1
    assert db.commits == 1


def test_settle_match_skips_mark_sent_when_already_sent(monkeypatch):
    calls = _patch_settle(monkeypatch)
    attempt = _attempt(status="sent")
    message = _message()
    db = _FakeDB()

    reconcile._settle_match(db, attempt=attempt, thread_id=attempt.thread_id, message=message)

    assert calls["mark_sent"] == []


def test_classify_and_stamp_null_winner_classifies_and_stamps(monkeypatch):
    calls, stamp_calls = _patch_classification(monkeypatch)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won is True
    assert calls["classify"] == 1
    assert len(stamp_calls) == 1
    assert db.commits == 1
    assert db.rollbacks == 0


def test_classify_and_stamp_two_workers_racing_classify_exactly_once(monkeypatch):
    """Required interleaving test: two workers race the same attempt --
    the second sees a non-null verified_at under the shared lock and never
    calls the classifier."""
    calls, stamp_calls = _patch_classification(monkeypatch)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    now = datetime.now(timezone.utc)
    # First call: still NULL (this worker wins). Second call: already
    # stamped by the winner -- the loser must see that and skip.
    db = _FakeDB(verified_at_reads=[None, now])

    first = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )
    second = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert first is True
    assert second is False
    assert calls["classify"] == 1
    assert len(stamp_calls) == 1
    assert db.rollbacks == 1  # the loser's rollback


def test_classify_and_stamp_classification_failure_leaves_unverified(monkeypatch):
    """Required test: an Outlook run where correlated classification fails
    -- sent but unverified, and only THIS attempt's classification work
    rolls back."""
    calls, stamp_calls = _patch_classification(monkeypatch, raises=RuntimeError("provider down"))
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won is False
    assert calls["classify"] == 1
    assert stamp_calls == []
    assert db.rollbacks == 1
    assert db.commits == 0


def test_reconcile_one_attempt_settles_first_even_when_classification_fails(monkeypatch):
    """A provably delivered attempt must never stay blocking because
    classification later fails -- settle (mark_sent/fence) commits
    independently of the classify phase's outcome."""
    settle_calls = _patch_settle(monkeypatch)
    _patch_classification(monkeypatch, raises=RuntimeError("boom"))
    thread = _thread()
    attempt = _attempt(provider="outlook", thread_id=thread.id, status="inflight", verified_at=None)
    message = _message(thread_id=thread.id, provider_message_id="draft-1")
    attempt.provider_message_id = "draft-1"
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: message)
    db = _FakeDB(attempts=[attempt], threads=[thread], verified_at_reads=[None])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=True)

    assert outcome["resolved_action_items"] == 1
    assert outcome["classified"] is False
    assert len(settle_calls["mark_sent"]) == 1


def test_reconcile_one_attempt_classify_false_skips_classification_entirely(monkeypatch):
    """Required interleaving: classify_messages=False reconciles fence/
    action state but leaves verified_at untouched -- a later
    classification-enabled pass finishes the job (cursor-advanced case)."""
    _patch_settle(monkeypatch)
    calls, stamp_calls = _patch_classification(monkeypatch)
    thread = _thread()
    attempt = _attempt(provider="outlook", thread_id=thread.id, status="sent", verified_at=None)
    message = _message(thread_id=thread.id)
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: message)
    db = _FakeDB(attempts=[attempt], threads=[thread])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=False)

    assert outcome["classified"] is False
    assert calls["classify"] == 0
    assert stamp_calls == []

    # A later pass with classify_messages=True (the "cursor advanced" run)
    # picks up where this one left off.
    db2 = _FakeDB(attempts=[attempt], threads=[thread], verified_at_reads=[None])
    outcome2 = reconcile._reconcile_one_attempt(db2, attempt_id=attempt.id, classify_messages=True)
    assert outcome2["classified"] is True
    assert calls["classify"] == 1


def test_reconcile_one_attempt_no_match_returns_none_and_rolls_back(monkeypatch):
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: None)
    thread = _thread()
    attempt = _attempt(thread_id=thread.id)
    db = _FakeDB(attempts=[attempt], threads=[thread])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=True)

    assert outcome is None
    assert db.rollbacks == 1


def test_reconcile_one_attempt_missing_attempt_or_thread_returns_none():
    db = _FakeDB()
    assert reconcile._reconcile_one_attempt(db, attempt_id=uuid4(), classify_messages=True) is None


def test_reconcile_one_attempt_gmail_stamps_verified_when_missing(monkeypatch):
    """Recovery branch: a Gmail completion transaction that crashed after
    the provider call succeeded but before its own commit -- this pass
    closes the verified_at gap without touching classification (Gmail is
    never classified here, only at completion time or via this recovery)."""
    _patch_settle(monkeypatch)
    stamp_calls = []
    monkeypatch.setattr(reconcile, "stamp_verified", lambda db, **kwargs: stamp_calls.append(kwargs))
    thread = _thread()
    attempt = _attempt(provider="gmail", thread_id=thread.id, status="sent", verified_at=None)
    message = _message(thread_id=thread.id)
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: message)
    db = _FakeDB(attempts=[attempt], threads=[thread])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=True)

    assert outcome["classified"] is False
    assert len(stamp_calls) == 1


# ---------------------------------------------------------------------------
# run_reconciliation_pass -- fan-out, per-item isolation, and the
# snapshot-before-attempt-created / END-pass-completes-it scenario.
# ---------------------------------------------------------------------------


def test_run_reconciliation_pass_snapshot_before_attempt_created_end_pass_completes_it(monkeypatch):
    """A START pass runs before the attempt (and its matching message) ever
    existed -- it sees nothing. The END pass re-fetches AFTER the run's
    final commit and completes it."""
    calls = []

    def fake_eligible(db, **kwargs):
        calls.append(kwargs)
        return db.pending_ids

    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", fake_eligible)

    class _StartDB:
        pending_ids = []

    start_result = reconcile.run_reconciliation_pass(
        _StartDB(), provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )
    assert start_result == {"attempts_checked": 0, "completed": 0, "classified": 0}

    attempt_id = uuid4()
    reconciled = []
    monkeypatch.setattr(
        reconcile,
        "_reconcile_one_attempt",
        lambda db, *, attempt_id, classify_messages: reconciled.append(attempt_id)
        or {"resolved_action_items": 1, "classified": False},
    )

    class _EndDB:
        pending_ids = [attempt_id]

    end_result = reconcile.run_reconciliation_pass(
        _EndDB(), provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )
    assert end_result == {"attempts_checked": 1, "completed": 1, "classified": 0}
    assert reconciled == [attempt_id]


def test_run_reconciliation_pass_isolates_a_failing_attempt(monkeypatch):
    """Per-item isolation (REVIEW.md): one attempt raising must not abort
    the pass for the rest."""
    ids = [uuid4(), uuid4(), uuid4()]
    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", lambda db, **k: ids)

    def fake_reconcile(db, *, attempt_id, classify_messages):
        if attempt_id == ids[1]:
            raise RuntimeError("boom")
        return {"resolved_action_items": 1, "classified": attempt_id == ids[2]}

    monkeypatch.setattr(reconcile, "_reconcile_one_attempt", fake_reconcile)

    class _DB:
        def rollback(self):
            self.rolled_back = True

    db = _DB()
    result = reconcile.run_reconciliation_pass(
        db, provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )

    assert result == {"attempts_checked": 3, "completed": 2, "classified": 1}
    assert db.rolled_back is True


def test_run_reconciliation_pass_eligible_query_failure_never_raises(monkeypatch):
    """F2: reconciliation is timeliness-only and must never gate ingest -- a
    failure OUTSIDE the per-attempt loop (the eligible-attempts query
    itself) must roll back, log, and return cleanly instead of propagating
    to the four ingest call sites (which would abort a sync before fetch,
    or fail an already-committed run)."""

    def raise_boom(db, **kwargs):
        raise RuntimeError("eligible-attempts query blew up")

    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", raise_boom)

    class _DB:
        def rollback(self):
            self.rolled_back = True

    db = _DB()
    result = reconcile.run_reconciliation_pass(
        db, provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )

    assert result == {"attempts_checked": 0, "completed": 0, "classified": 0}
    assert db.rolled_back is True


def test_run_reconciliation_pass_counts_matches_with_nothing_yet_as_incomplete(monkeypatch):
    ids = [uuid4(), uuid4()]
    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", lambda db, **k: ids)
    monkeypatch.setattr(reconcile, "_reconcile_one_attempt", lambda db, **k: None)

    result = reconcile.run_reconciliation_pass(
        MagicMock(), provider_account_id=uuid4(), provider="outlook", classify_messages=True
    )

    assert result == {"attempts_checked": 2, "completed": 0, "classified": 0}
