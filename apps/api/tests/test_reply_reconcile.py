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

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.db.models import MailThread, ReplyAttempt
from app.services.mail_send import reconcile
from app.workers import tasks_ingest


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

    def __init__(
        self,
        *,
        attempts=(),
        threads=(),
        verified_at_reads=None,
        status_reads=None,
        classification_reads=None,
    ):
        self._attempts = {a.id: a for a in attempts}
        self._threads = {t.id: t for t in threads}
        self._verified_at_reads = list(verified_at_reads) if verified_at_reads is not None else None
        self._status_reads = list(status_reads) if status_reads is not None else None
        self._classification_reads = (
            list(classification_reads) if classification_reads is not None else None
        )
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
        elif "reply_attempt.status" in sql:
            if self._status_reads is not None:
                result.scalar_one.return_value = self._status_reads.pop(0)
            else:
                result.scalar_one.return_value = "sent"
        elif "classification.message_id" in sql:
            if self._classification_reads is not None:
                result.scalar_one_or_none.return_value = self._classification_reads.pop(0)
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

    monkeypatch.setattr(reconcile, "mark_sent_reconciled", fake_mark_sent)
    monkeypatch.setattr(reconcile, "advance_fence_and_resolve", fake_advance)
    return calls


def _patch_classification(monkeypatch, *, raises=None, no_verdict=False):
    # calls["upsert"] tracks every upsert_classification invocation -- used
    # by the phase 2 no-verdict tests to assert directly that NO row is ever
    # written (not just inferred from outcome["classified"]).
    calls = {"classify": 0, "upsert": []}

    def fake_classify(text, *, routing):
        calls["classify"] += 1
        if raises:
            raise raises
        if no_verdict:
            # Phase 2 (D-C): a failed BYOK call with no local fallback
            # served -- see classifier.ClassificationAttempt's own docstring
            # for why this is the one case `verdict` is None.
            return SimpleNamespace(
                verdict=None,
                provider_call_succeeded=False,
                usage=None,
                llm_attempted=True,
                fallback_used=False,
                failure_category="connection_failed",
            )
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
    monkeypatch.setattr(
        reconcile, "upsert_classification",
        lambda db, **k: calls["upsert"].append(k) or "written",
    )
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


def test_settle_match_settles_an_unknown_attempt(monkeypatch):
    """R-1 (final review): an `unknown` attempt is exactly what
    reconciliation exists to settle -- it must go through the widened
    `mark_sent_reconciled` CAS and advance the fence, not stay blocking."""
    calls = _patch_settle(monkeypatch, resolved=1)
    attempt = _attempt(status="unknown")
    message = _message()
    db = _FakeDB()

    outcome = reconcile._settle_match(db, attempt=attempt, thread_id=attempt.thread_id, message=message)

    assert outcome == {"resolved_action_items": 1}
    assert len(calls["mark_sent"]) == 1
    assert db.commits == 1
    assert db.rollbacks == 0


def test_settle_match_cas_loss_rereads_and_proceeds_when_already_sent(monkeypatch):
    """A lost CAS race (another worker/route settled it first) is confirmed
    by a re-read under the same lock, not treated as a failure -- fence
    resolution still runs."""
    _patch_settle(monkeypatch, mark_sent_result=False, resolved=3)
    attempt = _attempt(status="unknown")
    message = _message()
    db = _FakeDB(status_reads=["sent"])

    outcome = reconcile._settle_match(db, attempt=attempt, thread_id=attempt.thread_id, message=message)

    assert outcome == {"resolved_action_items": 3}
    assert db.commits == 1


def test_settle_match_cas_loss_anomaly_does_not_advance_fence(monkeypatch):
    """R-1: a failed CAS whose re-read shows anything OTHER than `sent` is a
    genuine anomaly -- must roll back and return None rather than silently
    advancing the fence for an attempt that isn't actually settled."""
    calls = _patch_settle(monkeypatch, mark_sent_result=False)
    attempt = _attempt(status="unknown")
    message = _message()
    db = _FakeDB(status_reads=["failed"])

    outcome = reconcile._settle_match(db, attempt=attempt, thread_id=attempt.thread_id, message=message)

    assert outcome is None
    assert calls["advance_fence"] == []
    assert db.rollbacks == 1
    assert db.commits == 0


def test_classify_and_stamp_null_winner_classifies_and_stamps(monkeypatch):
    calls, stamp_calls = _patch_classification(monkeypatch)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won == reconcile._OUTCOME_CLASSIFIED
    assert calls["classify"] == 1
    assert len(stamp_calls) == 1
    assert db.commits == 1
    assert db.rollbacks == 0


def test_classify_and_stamp_no_verdict_stamps_verified_without_classifying(monkeypatch):
    """Codex-caught bug fix (phase 2): `verdict is None` (a failed BYOK call
    with no local fallback served) must still stamp_verified and commit --
    the SEND itself is genuinely verified, that's a separate fact from
    classification succeeding (see _classify_and_stamp's own docstring).
    Skipping the stamp here left the attempt permanently eligible, which
    would re-issue the same already-known-to-fail BYOK call on every future
    sync forever. No Classification row is ever written either -- the
    message stays a genuine backfill candidate."""
    calls, stamp_calls = _patch_classification(monkeypatch, no_verdict=True)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won == reconcile._OUTCOME_NO_VERDICT  # not classified, but a KNOWN outcome
    assert calls["classify"] == 1
    assert calls["upsert"] == []  # never a null-label row
    assert len(stamp_calls) == 1  # but the send IS verified
    assert db.commits == 1
    assert db.rollbacks == 0


def test_classify_and_stamp_no_verdict_then_a_later_sync_never_reclassifies(monkeypatch):
    """The bounded-cost guarantee Codex asked us to document (phase 2): a
    no-verdict pass stamps verified_at (see the test above), so the NEXT
    reconciliation pass -- representing a later sync, whether that's the END
    pass right behind a page-loop's own already-failed classification, or a
    wholly separate later sync's START/END pass -- sees a non-null
    verified_at and skips straight to the loser-of-race early return without
    ever calling the classifier again. Total real-world cost for one message
    is bounded at "however many calls happened before the first stamp",
    never unbounded."""
    calls, stamp_calls = _patch_classification(monkeypatch, no_verdict=True)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()

    # Pass 1: verified_at is still NULL -- this is the first call reconcile.py
    # itself ever sees for this attempt (a page-loop classification that
    # already failed and left no Classification row looks IDENTICAL to this
    # state; reconcile.py can't and needn't distinguish the two).
    db_pass_1 = _FakeDB(verified_at_reads=[None])
    outcome_1 = reconcile._classify_and_stamp(
        db_pass_1, attempt_id=attempt.id, message=message,
        thread_id=attempt.thread_id, user_id=uuid4(),
    )
    assert outcome_1 == reconcile._OUTCOME_NO_VERDICT
    assert calls["classify"] == 1
    assert len(stamp_calls) == 1

    # Pass 2 (a later sync's own reconciliation pass): verified_at is now
    # non-null, stamped by pass 1 above.
    db_pass_2 = _FakeDB(verified_at_reads=[datetime.now(timezone.utc)])
    outcome_2 = reconcile._classify_and_stamp(
        db_pass_2, attempt_id=attempt.id, message=message,
        thread_id=attempt.thread_id, user_id=uuid4(),
    )
    assert outcome_2 == reconcile._OUTCOME_RACE_LOST
    assert calls["classify"] == 1  # no new call
    assert calls["upsert"] == []  # still no Classification row, ever
    assert len(stamp_calls) == 1  # no new stamp either
    assert db_pass_2.rollbacks == 1


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

    assert first == reconcile._OUTCOME_CLASSIFIED
    assert second == reconcile._OUTCOME_RACE_LOST
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

    assert won == reconcile._OUTCOME_FAILED  # unknown state, NOT the same as no_verdict
    assert calls["classify"] == 1
    assert stamp_calls == []
    assert db.rollbacks == 1
    assert db.commits == 0


def test_classify_and_stamp_existing_classification_stamps_without_classifying(monkeypatch):
    """R-3 (final review): a classification-enabled sync already classified
    this message in its own page loop -- the END pass's claim must find the
    existing Classification row and just stamp verified_at, never call the
    classifier again (double BYOK charge / verdict-overwrite risk)."""
    calls, stamp_calls = _patch_classification(monkeypatch)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None], classification_reads=[uuid4()])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won == reconcile._OUTCOME_ALREADY_CLASSIFIED
    assert calls["classify"] == 0
    assert len(stamp_calls) == 1
    assert db.commits == 1
    assert db.rollbacks == 0


def test_classify_and_stamp_user_override_stamps_without_overwriting(monkeypatch):
    """R-3: a user-override Classification row is treated the same way --
    stamped, never reclassified or overwritten."""
    calls, stamp_calls = _patch_classification(monkeypatch)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message()
    db = _FakeDB(verified_at_reads=[None], classification_reads=[uuid4()])

    won = reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert won == reconcile._OUTCOME_ALREADY_CLASSIFIED
    assert calls["classify"] == 0


def test_classify_and_stamp_passes_subject_from_stored_headers(monkeypatch):
    """R-7 (final review): the Subject header must be read case-insensitively
    from the stored `headers` JSONB and passed through, the same as ingest
    does -- a None subject would classify this message differently than an
    identical one classified via normal ingest."""
    captured = {}

    def fake_build_text(subject, snippet, body_text):
        captured["subject"] = subject
        return "text"

    # _patch_classification stubs build_classification_text too -- override
    # it again afterward so this capturing fake wins.
    _patch_classification(monkeypatch)
    monkeypatch.setattr(reconcile, "build_classification_text", fake_build_text)
    attempt = _attempt(provider="outlook", verified_at=None)
    message = _message(headers={"sUbJeCt": "Re: quarterly numbers"})
    db = _FakeDB(verified_at_reads=[None], classification_reads=[None])

    reconcile._classify_and_stamp(
        db, attempt_id=attempt.id, message=message, thread_id=attempt.thread_id, user_id=uuid4()
    )

    assert captured["subject"] == "Re: quarterly numbers"


def test_reconcile_one_attempt_settles_first_even_when_classification_fails(monkeypatch):
    """A provably delivered attempt must never stay blocking because
    classification later fails -- settle (mark_sent/fence) commits
    independently of the classify phase's outcome. Codex finding (D-C/D-I):
    a genuine exception is an UNKNOWN state, not a known "left unclassified"
    outcome, so it must NOT count toward `left_unclassified` either -- only
    `test_reconcile_one_attempt_no_verdict_reports_left_unclassified` below
    (a clean no-verdict) should."""
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
    assert outcome["left_unclassified"] is False  # unknown state, not a known miss
    assert len(settle_calls["mark_sent"]) == 1


def test_reconcile_one_attempt_no_verdict_reports_left_unclassified(monkeypatch):
    """Codex finding (D-C/D-I): a clean no-verdict outcome (a failed BYOK
    call with no local fallback served) must surface as
    `outcome["left_unclassified"] is True` -- this is what
    `run_reconciliation_pass` aggregates and both `outlook_ingest.py` call
    sites fold into `stats["left_unclassified"]`, the ONE thing the ingest
    toast/auto-sync warning actually reads. Before this fix, a message left
    unclassified via reconciliation (as opposed to the page loop) was
    invisible end to end."""
    settle_calls = _patch_settle(monkeypatch)
    _patch_classification(monkeypatch, no_verdict=True)
    thread = _thread()
    attempt = _attempt(provider="outlook", thread_id=thread.id, status="inflight", verified_at=None)
    message = _message(thread_id=thread.id, provider_message_id="draft-1")
    attempt.provider_message_id = "draft-1"
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: message)
    db = _FakeDB(attempts=[attempt], threads=[thread], verified_at_reads=[None])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=True)

    assert outcome["classified"] is False
    assert outcome["left_unclassified"] is True
    assert len(settle_calls["mark_sent"]) == 1  # the send is still settled/verified


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


def test_reconcile_one_attempt_settle_anomaly_returns_none_and_skips_classification(monkeypatch):
    """R-1: when `_settle_match` reports a genuine anomaly (None), there's
    nothing settled to classify -- the classification branch must not run."""
    monkeypatch.setattr(reconcile, "_settle_match", lambda db, **k: None)
    calls, stamp_calls = _patch_classification(monkeypatch)
    thread = _thread()
    attempt = _attempt(provider="outlook", thread_id=thread.id, status="unknown", verified_at=None)
    message = _message(thread_id=thread.id)
    monkeypatch.setattr(reconcile, "_match_message", lambda db, **k: message)
    db = _FakeDB(attempts=[attempt], threads=[thread])

    outcome = reconcile._reconcile_one_attempt(db, attempt_id=attempt.id, classify_messages=True)

    assert outcome is None
    assert calls["classify"] == 0


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
    assert start_result == {
        "attempts_checked": 0, "completed": 0, "classified": 0, "left_unclassified": 0,
    }

    attempt_id = uuid4()
    reconciled = []
    monkeypatch.setattr(
        reconcile,
        "_reconcile_one_attempt",
        lambda db, *, attempt_id, classify_messages: reconciled.append(attempt_id)
        or {"resolved_action_items": 1, "classified": False, "left_unclassified": False},
    )

    class _EndDB:
        pending_ids = [attempt_id]

    end_result = reconcile.run_reconciliation_pass(
        _EndDB(), provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )
    assert end_result == {
        "attempts_checked": 1, "completed": 1, "classified": 0, "left_unclassified": 0,
    }
    assert reconciled == [attempt_id]


def test_run_reconciliation_pass_isolates_a_failing_attempt(monkeypatch):
    """Per-item isolation (REVIEW.md): one attempt raising must not abort
    the pass for the rest."""
    ids = [uuid4(), uuid4(), uuid4()]
    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", lambda db, **k: ids)

    def fake_reconcile(db, *, attempt_id, classify_messages):
        if attempt_id == ids[1]:
            raise RuntimeError("boom")
        return {
            "resolved_action_items": 1,
            "classified": attempt_id == ids[2],
            "left_unclassified": False,
        }

    monkeypatch.setattr(reconcile, "_reconcile_one_attempt", fake_reconcile)

    class _DB:
        def rollback(self):
            self.rolled_back = True

    db = _DB()
    result = reconcile.run_reconciliation_pass(
        db, provider_account_id=uuid4(), provider="gmail", classify_messages=True
    )

    assert result == {
        "attempts_checked": 3, "completed": 2, "classified": 1, "left_unclassified": 0,
    }
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

    assert result == {
        "attempts_checked": 0, "completed": 0, "classified": 0, "left_unclassified": 0,
    }
    assert db.rolled_back is True


def test_run_reconciliation_pass_counts_matches_with_nothing_yet_as_incomplete(monkeypatch):
    ids = [uuid4(), uuid4()]
    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", lambda db, **k: ids)
    monkeypatch.setattr(reconcile, "_reconcile_one_attempt", lambda db, **k: None)

    result = reconcile.run_reconciliation_pass(
        MagicMock(), provider_account_id=uuid4(), provider="outlook", classify_messages=True
    )

    assert result == {
        "attempts_checked": 2, "completed": 0, "classified": 0, "left_unclassified": 0,
    }


def test_run_reconciliation_pass_aggregates_left_unclassified_and_never_counts_failures(
    monkeypatch,
):
    """Codex finding (D-C/D-I): a no-verdict outcome must reach the pass's
    own return dict as `left_unclassified`, and a genuinely failed/unknown
    outcome (or a race-lost no-op) must NOT be counted there -- only
    `_OUTCOME_NO_VERDICT` is a KNOWN "left unclassified" fact."""
    ids = [uuid4(), uuid4(), uuid4()]

    def fake_reconcile(db, *, attempt_id, classify_messages):
        if attempt_id == ids[0]:
            return {"resolved_action_items": 1, "classified": False, "left_unclassified": True}
        if attempt_id == ids[1]:
            return {"resolved_action_items": 1, "classified": True, "left_unclassified": False}
        # ids[2]: a race-lost/gmail-branch outcome -- neither classified nor
        # left_unclassified, same shape _reconcile_one_attempt always emits.
        return {"resolved_action_items": 1, "classified": False, "left_unclassified": False}

    monkeypatch.setattr(reconcile, "_eligible_attempt_ids", lambda db, **k: ids)
    monkeypatch.setattr(reconcile, "_reconcile_one_attempt", fake_reconcile)

    result = reconcile.run_reconciliation_pass(
        MagicMock(), provider_account_id=uuid4(), provider="outlook", classify_messages=True
    )

    assert result == {
        "attempts_checked": 3, "completed": 3, "classified": 1, "left_unclassified": 1,
    }


# ---------------------------------------------------------------------------
# app.workers.tasks_ingest.reconcile_reply_attempt -- R-4 (final review): the
# level pass must run directly, independent of the sync claim outcome.
# ---------------------------------------------------------------------------


def _account(*, account_id=None, user_id=None, provider="gmail"):
    return SimpleNamespace(
        id=account_id or uuid4(),
        user_id=user_id or uuid4(),
        refresh_token="tok",
        sync_paused_at=None,
        provider=provider,
    )


def test_reconcile_reply_attempt_runs_level_pass_on_deduplicated_claim(monkeypatch):
    """R-4: dedup + an already-persisted matching message must settle the
    attempt NOW, not wait for the stale sync lease or the one-shot
    re-enqueue -- the level pass runs regardless of the claim outcome."""
    account = _account()
    state_db = MagicMock()
    state_db.get.return_value = account
    recon_db = MagicMock()
    sessions = iter([state_db, recon_db])
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(next(sessions)))
    monkeypatch.setattr(
        tasks_ingest, "start_sync_run", lambda *a, **k: (SimpleNamespace(id=uuid4()), True)
    )
    pass_calls = []
    monkeypatch.setattr(
        tasks_ingest,
        "run_reconciliation_pass",
        lambda db, **kwargs: pass_calls.append(kwargs) or {"completed": 1},
    )
    monkeypatch.setattr(tasks_ingest.reconcile_reply_attempt, "apply_async", MagicMock())

    result = tasks_ingest.reconcile_reply_attempt.run(str(account.id), retry_count=0)

    assert len(pass_calls) == 1
    assert pass_calls[0] == {
        "provider_account_id": account.id,
        "provider": "gmail",
        "classify_messages": True,
    }
    # Dedup + first attempt still defers the one-shot re-enqueue -- the
    # level pass above is what actually closes the gap, this is only
    # secondary timeliness.
    assert result["status"] == "deferred"


def test_reconcile_reply_attempt_runs_level_pass_on_won_claim_too(monkeypatch):
    """A won claim's new ingest run does its own START/END passes -- this is
    redundant-but-harmless there, never skipped."""
    account = _account(provider="outlook")
    state_db = MagicMock()
    state_db.get.return_value = account
    recon_db = MagicMock()
    sessions = iter([state_db, recon_db])
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(next(sessions)))
    monkeypatch.setattr(
        tasks_ingest, "start_sync_run", lambda *a, **k: (SimpleNamespace(id=uuid4()), False)
    )
    pass_calls = []
    monkeypatch.setattr(
        tasks_ingest,
        "run_reconciliation_pass",
        lambda db, **kwargs: pass_calls.append(kwargs) or {"completed": 0},
    )

    result = tasks_ingest.reconcile_reply_attempt.run(str(account.id), retry_count=0)

    assert len(pass_calls) == 1
    assert pass_calls[0]["provider"] == "outlook"
    assert result["status"] == "enqueued"


def test_reconcile_reply_attempt_skipped_account_never_runs_level_pass(monkeypatch):
    """A paused/missing account skips everything, including the level pass
    -- there's nothing this account's attempts can be reconciled against."""
    state_db = MagicMock()
    state_db.get.return_value = None
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(state_db))
    pass_calls = []
    monkeypatch.setattr(
        tasks_ingest,
        "run_reconciliation_pass",
        lambda db, **kwargs: pass_calls.append(kwargs),
    )

    result = tasks_ingest.reconcile_reply_attempt.run(str(uuid4()), retry_count=0)

    assert pass_calls == []
    assert result["status"] == "skipped"
