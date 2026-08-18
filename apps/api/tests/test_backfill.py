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

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import MailMessage
from app.services.nlp import backfill
from app.services.nlp import classifier as classifier_module
from app.services.nlp.classifier import ClassificationAttempt
from app.services.nlp.llm_client import INLINE_RETRIES, LlmCallError, LlmCallResult, LlmUsage
from app.services.nlp.persistence import OPERATOR_MODEL_VERSION
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

    ``classification_rows`` stands in for the message_id/label/model_version
    read: pass SimpleNamespace(message_id=..., label=..., model_version=...)
    rows (defaults to none, i.e. nothing classified yet).
    """

    def __init__(
        self,
        *,
        threads,
        latest_rows,
        classification_rows=(),
        routing_row=None,
        events=None,
    ):
        self._threads = threads
        self._latest_rows = latest_rows
        self._classification_rows = classification_rows
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
        if cols == ["provider", "classification_byok", "classification_fallback_local"]:
            if self._routing_row is None:
                return _Result([])
            provider, byok = self._routing_row
            return _Result(
                [
                    SimpleNamespace(
                        provider=provider,
                        classification_byok=byok,
                        classification_fallback_local=False,
                    )
                ]
            )
        table = stmt.selected_columns[0].table.name
        if table == "mail_thread":
            return _Result(self._threads)
        if table == "mail_message":
            return _Result(self._latest_rows)
        if table == "classification":
            return _Result(self._classification_rows)
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

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        classify_routings.append(routing)
        return ClassificationAttempt(
            verdict=("fyi", 0.5, "no cues", "heuristic-v1"),
            provider_call_succeeded=False,
            usage=None,
        )

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

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
    to the heuristic, never touching an LLM. Asserting the seam (the BYOK
    wire call) is never built is the actual proof that nobody got billed,
    not just the returned label."""
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

    monkeypatch.setattr(classifier_module, "call_chat_completion", _explode)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    # backend="llm" skips the local-model branch so routing (not whatever
    # the local encoder happens to be doing in this test env) decides the
    # outcome.
    result = backfill.run_backfill(db, user_id, limit=10, backend="llm")

    assert result["created"] == 1


def test_run_backfill_threads_the_caller_supplied_policy_to_classify_with_usage(monkeypatch):
    """The `run_backfill` trap (plan: phase 3 of the LLM-failure work): it
    serves both an inline request and a queued worker task, so it must never
    guess a policy internally -- whatever the caller passes has to reach
    classify_with_usage() unchanged."""
    user_id = uuid4()
    db = _n_message_db(1)
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    captured = {}

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        captured["policy"] = policy
        return ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "m"), provider_call_succeeded=False, usage=None,
        )

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    backfill.run_backfill(db, user_id, limit=10, policy=INLINE_RETRIES)

    assert captured["policy"] is INLINE_RETRIES


# ---------------------------------------------------------------------------
# run_backfill: failure-visibility counters (plan: 2026-08-14-llm-failure-
# visibility) -- llm_attempted/llm_failed/fell_back/failure_categories, read
# straight off ClassificationAttempt's explicit facts. classify_with_usage is
# deliberately NOT mocked in these two tests -- driving the real classifier
# dispatch is the whole point: a fake attempt object could assert whatever
# the test wants regardless of whether the real code actually sets it right.
# ---------------------------------------------------------------------------


def test_run_backfill_default_backend_reports_zero_llm_attempted_and_fell_back(monkeypatch):
    """The predicate trap the plan calls out by name (first test to write):
    a CLASSIFIER_BACKEND=auto backfill (the global default; "local" is now a
    deprecated alias for the same value, plan §2/§3) must report
    fell_back=0 AND llm_attempted=0 -- a naive implementation gating on
    routing.mode=="user" would report this as 100% degraded even though no
    LLM was ever consulted."""
    from app.services.nlp import local_model

    monkeypatch.setattr(classifier_module.settings, "classifier_backend", "auto")
    monkeypatch.setattr(
        local_model, "try_predict",
        lambda text: ("fyi", 0.5, "local rationale", "local:test"),
    )

    user_id = uuid4()
    db = _single_message_db()
    # mode="server" (not "user") so the opt-in-goes-first-to-the-key branch
    # never triggers -- this pins the encoder-serves-directly path.
    routing = ClassificationRouting(mode="server", credential=None)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    result = backfill.run_backfill(db, user_id, limit=10)

    assert result["created"] == 1
    assert result["llm_attempted"] == 0
    assert result["llm_failed"] == 0
    assert result["fell_back"] == 0
    assert result["failure_categories"] == {}


def test_run_backfill_llm_failure_falls_back_to_encoder_with_provenance_marker(monkeypatch):
    """fell_back=1 in the aggregate AND the persisted row's model_version
    carries the +fallback marker -- the exact silent-degrade case from the
    plan's report (an LLM failure served by the encoder was byte-identical
    to a healthy local-backend run, with no trace anywhere). Requires
    fallback_local=True (phase 2, D-A/D-H) -- without the opt-in, this
    scenario now yields verdict=None instead (see the D-C stop tests below)."""
    from app.services.nlp import local_model

    monkeypatch.setattr(
        local_model, "try_predict",
        lambda text: ("fyi", 0.6, "local rationale", "local:test"),
    )

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier_module, "call_chat_completion", failing_call)

    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED, fallback_local=True)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    upserted_model_versions = []

    def fake_upsert(db_arg, *, message_id, label, confidence, rationale, model_version):
        upserted_model_versions.append(model_version)
        return "written"

    monkeypatch.setattr(backfill, "upsert_classification", fake_upsert)

    # backend="llm" skips the local-first dispatch order so the encoder is
    # reached only through the failure fallback, not the normal auto/local
    # precedence -- isolating the case under test.
    result = backfill.run_backfill(db, user_id, limit=10, backend="llm")

    assert result["llm_attempted"] == 1
    assert result["llm_failed"] == 1
    assert result["fell_back"] == 1
    assert result["failure_categories"] == {"connection_failed": 1}
    assert upserted_model_versions == ["local:test+fallback"]


def test_run_backfill_preflight_rejection_reports_fell_back_without_llm_attempted(monkeypatch):
    """Codex review finding: `llm_failed` must count only failures where a
    request was actually issued -- a destination-policy PREFLIGHT rejection
    (blocked_by_policy) never reaches the wire, so it must land in
    fell_back/failure_categories only. Without this, `llm_failed=1,
    llm_attempted=0` would be an incoherent shape (llm_failed <= llm_attempted
    is the invariant). Drives the real call_chat_completion via a fake
    pin_custom_destination, same pattern as the two tests above. Requires
    fallback_local=True (phase 2, D-A/D-H) so the encoder still serves this
    fallback the same way it did before that opt-in existed."""
    from app.services.nlp import llm_client as llm_client_module
    from app.services.nlp import local_model
    from app.services.nlp.providers import DestinationRejected

    monkeypatch.setattr(
        local_model, "try_predict",
        lambda text: ("fyi", 0.6, "local rationale", "local:test"),
    )

    def fake_pin(url):
        raise DestinationRejected("destination_rejected", "nope")

    monkeypatch.setattr(llm_client_module, "pin_custom_destination", fake_pin)

    user_id = uuid4()
    db = _single_message_db()
    credential = LlmCredential(
        provider="custom", base_url="https://ollama.example.com/v1", api_key="k", model="llama3"
    )
    routing = ClassificationRouting(mode="user", credential=credential, fallback_local=True)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    result = backfill.run_backfill(db, user_id, limit=10, backend="llm")

    assert result["llm_attempted"] == 0
    assert result["llm_failed"] == 0
    assert result["fell_back"] == 1
    assert result["failure_categories"] == {"blocked_by_policy": 1}


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


def _two_message_db(**kwargs):
    return _n_message_db(2, **kwargs)


def _n_message_db(n, **kwargs):
    threads = []
    latest_rows = []
    for _ in range(n):
        t, m = uuid4(), uuid4()
        threads.append(SimpleNamespace(id=t, subject="s"))
        latest_rows.append(SimpleNamespace(id=m, thread_id=t, snippet="hi", body_text="there"))
    return _BackfillFakeDB(threads=threads, latest_rows=latest_rows, **kwargs)


def _no_verdict_attempt(failure_category="connection_failed"):
    return ClassificationAttempt(
        verdict=None,
        provider_call_succeeded=False,
        usage=None,
        llm_attempted=True,
        fallback_used=False,
        failure_category=failure_category,
    )


def _verdict_attempt(label="fyi"):
    return ClassificationAttempt(
        verdict=(label, 0.5, "r", "test-model"),
        provider_call_succeeded=False,
        usage=None,
    )


# ---------------------------------------------------------------------------
# run_backfill: D-C's early stop after CONSECUTIVE no-verdict results, hit
# regardless of fallback_local (plan: 2026-08-14-llm-failure-visibility
# phase 2). A single odd message must never abort an otherwise-healthy run
# -- see backfill._CONSECUTIVE_NO_VERDICT_LIMIT's own comment for the "why
# not 1, why stop at all" reasoning these tests pin.
# ---------------------------------------------------------------------------


def test_run_backfill_stops_after_three_consecutive_no_verdicts_with_partial_counts(
    monkeypatch,
):
    """Not opted into fallback_local -- three CONSECUTIVE verdict=None
    results stop the run rather than continuing to spend the user's BYOK
    budget on calls that will keep failing the same way. status becomes
    "llm_unavailable", counts are partial, and the untouched fourth
    candidate is rolled into left_unclassified too."""
    user_id = uuid4()
    db = _n_message_db(4)
    routing = ClassificationRouting(mode="user", credential=_CRED)  # fallback_local=False
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    calls = []

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        calls.append(text)
        return _no_verdict_attempt()

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    upserts = []
    monkeypatch.setattr(
        backfill, "upsert_classification",
        lambda *a, **k: (upserts.append(k), "written")[1],
    )

    result = backfill.run_backfill(db, user_id, limit=10)

    assert len(calls) == 3  # the fourth candidate was never even attempted
    assert upserts == []  # never a null-label row
    assert result["status"] == "llm_unavailable"
    assert result["created"] == 0
    assert result["scanned"] == 4
    assert result["left_unclassified"] == 4  # the 3 attempted + the never-attempted one
    assert result["failure_categories"] == {"connection_failed": 3}


def test_run_backfill_opted_in_encoder_down_stops_after_threshold_not_immediately(
    monkeypatch,
):
    """The contract bug this coordinator correction fixes: an opted-in user
    whose encoder happens to be unavailable burns BYOK money identically to
    an opted-out one, so the stop must NOT be gated on fallback_local -- it
    stops after the same 3-consecutive threshold, not on the first failure
    and not never."""
    user_id = uuid4()
    db = _n_message_db(5)
    routing = ClassificationRouting(mode="user", credential=_CRED, fallback_local=True)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    calls = []

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        calls.append(text)
        return _no_verdict_attempt()

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    result = backfill.run_backfill(db, user_id, limit=10)

    assert len(calls) == 3  # stopped after the threshold, not ground through all 5
    assert result["status"] == "llm_unavailable"
    assert result["left_unclassified"] == 5


def test_run_backfill_isolated_no_verdicts_among_successes_never_stop_and_counter_resets(
    monkeypatch,
):
    """An isolated no-verdict (or even two, separated by a success) must
    never trip the stop -- the streak counter has to actually reset on a
    verdict, not just accumulate a running total. Pattern: no-verdict,
    success, no-verdict, no-verdict, success -- 3 no-verdicts total but the
    longest CONSECUTIVE run is 2, under the threshold, so all 5 candidates
    get attempted and the run finishes "ok"."""
    user_id = uuid4()
    db = _n_message_db(5)
    routing = ClassificationRouting(mode="user", credential=_CRED)  # fallback_local=False
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    outcomes = [
        _no_verdict_attempt(),
        _verdict_attempt(),
        _no_verdict_attempt(),
        _no_verdict_attempt(),
        _verdict_attempt(),
    ]
    calls = []

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        calls.append(text)
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    result = backfill.run_backfill(db, user_id, limit=10)

    assert len(calls) == 5  # nothing stopped the run early
    assert result["status"] == "ok"
    assert result["created"] == 2  # the two verdicts that landed
    assert result["left_unclassified"] == 3  # the three no-verdict messages


def test_run_backfill_two_consecutive_no_verdicts_stay_under_the_stop_threshold(monkeypatch):
    """Two consecutive no-verdicts (opted-in, encoder unavailable both
    times) stay under the 3-consecutive threshold -- every candidate still
    gets attempted and the run reports "ok", not "llm_unavailable"."""
    user_id = uuid4()
    db = _two_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED, fallback_local=True)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    calls = []

    def fake_classify_with_usage(text, backend=None, routing=None, policy=None):
        calls.append(text)
        return _no_verdict_attempt()

    monkeypatch.setattr(backfill, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")

    result = backfill.run_backfill(db, user_id, limit=10)

    assert len(calls) == 2  # both candidates attempted
    assert result["status"] == "ok"
    assert result["left_unclassified"] == 2


def test_run_backfill_records_usage_only_for_user_mode_routing(monkeypatch):
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    usage = LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=usage,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")
    fake_acc = _FakeAcc(user_id)
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    backfill.run_backfill(db, user_id, limit=10)

    assert fake_acc.records == [("classification", "openai", usage, True)]


def test_run_backfill_flushes_usage_even_when_every_message_is_no_verdict(monkeypatch):
    """CodeRabbit finding (billing data loss): `usage_pending` is set for
    every recorded user-mode attempt REGARDLESS of verdict -- a failed call
    can still have reached and billed the provider (D3) -- but `pending`
    (the classification-write batch) only grows when a verdict exists. The
    OLD trailing flush was gated on `if pending:` alone, so an all-no-verdict
    run recorded usage into the accumulator and then returned without ever
    flushing it -- silently dropping billed usage exactly when the user's
    provider is failing and their usage numbers matter most."""
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="user", credential=_CRED)  # fallback_local=False
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    usage = LlmUsage(prompt_tokens=1, completion_tokens=0, total_tokens=1)
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=None,
            provider_call_succeeded=False,
            usage=usage,
            llm_attempted=True,
            fallback_used=False,
            failure_category="connection_failed",
        ),
    )
    upserts = []
    monkeypatch.setattr(
        backfill, "upsert_classification",
        lambda *a, **k: (upserts.append(k), "written")[1],
    )
    fake_acc = _FakeAcc(user_id)
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    result = backfill.run_backfill(db, user_id, limit=10)

    assert upserts == []  # never a null-label row
    assert fake_acc.records == [("classification", "openai", usage, False)]
    # The bug: without the fix, flush_pending() (and therefore acc.flush()/
    # acc.committed()) never runs at all on an all-no-verdict run.
    assert "flush" in fake_acc.calls
    assert "committed" in fake_acc.calls
    assert result["left_unclassified"] == 1


def test_run_backfill_server_mode_routing_records_nothing(monkeypatch):
    # The regression guard for the routing-mode gate: only "user" mode spends
    # a key anyone is billed for, so nothing lands on the user's readout here.
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="server", credential=None)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "gemini-x"),
            provider_call_succeeded=True,
            usage=LlmUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")
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
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=None,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")
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
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "openai:gpt-4o-mini"),
            provider_call_succeeded=True,
            usage=None,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "written")
    fake_acc = _FakeAcc(user_id, flush_raises=SQLAlchemyError("boom"))
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: fake_acc)

    result = backfill.run_backfill(db, user_id, limit=10)

    assert result["created"] == 1  # the business write still landed
    assert fake_acc.calls.index("discard") < fake_acc.calls.index("committed")


# ---------------------------------------------------------------------------
# run_backfill: protecting user overrides (plan §3.3) -- a force run must
# neither re-classify an override at read time nor claim credit for one that
# survives the write-time guard.
# ---------------------------------------------------------------------------


def test_run_backfill_force_skips_user_override_rows_at_candidate_selection(monkeypatch):
    user_id = uuid4()
    thread_override, thread_model = uuid4(), uuid4()
    msg_override, msg_model = uuid4(), uuid4()

    threads = [
        SimpleNamespace(id=thread_override, subject="a"),
        SimpleNamespace(id=thread_model, subject="b"),
    ]
    latest_rows = [
        SimpleNamespace(id=msg_override, thread_id=thread_override, snippet="s-a", body_text="b-a"),
        SimpleNamespace(id=msg_model, thread_id=thread_model, snippet="s-b", body_text="b-b"),
    ]
    classification_rows = [
        SimpleNamespace(message_id=msg_override, label="fyi", model_version=OPERATOR_MODEL_VERSION),
        SimpleNamespace(message_id=msg_model, label="fyi", model_version="heuristic-v1"),
    ]
    db = _BackfillFakeDB(
        threads=threads, latest_rows=latest_rows, classification_rows=classification_rows
    )
    routing = ClassificationRouting(mode="server", credential=None)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "no cues", "heuristic-v1"),
            provider_call_succeeded=False,
            usage=None,
        ),
    )
    upserted_ids = []

    def fake_upsert(db_arg, *, message_id, **kwargs):
        upserted_ids.append(message_id)
        return "written"

    monkeypatch.setattr(backfill, "upsert_classification", fake_upsert)

    result = backfill.run_backfill(db, user_id, limit=10, force=True)

    # Only the model-labeled message gets re-classified; the override never
    # even reaches classify_with_usage/upsert_classification.
    assert upserted_ids == [msg_model]
    assert result["created"] == 1
    assert result["skipped_user_overrides"] == 1


def test_run_backfill_protected_upsert_outcome_counts_as_skipped_not_created(monkeypatch):
    # Simulates a user override landing mid-run: the message clears the
    # read-time candidate check (it wasn't an override yet when the run
    # started), but the write-time guard in upsert_classification catches it.
    user_id = uuid4()
    db = _single_message_db()
    routing = ClassificationRouting(mode="server", credential=None)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    monkeypatch.setattr(
        backfill, "classify_with_usage",
        lambda text, backend=None, routing=None, policy=None: ClassificationAttempt(
            verdict=("fyi", 0.5, "r", "heuristic-v1"),
            provider_call_succeeded=False,
            usage=None,
        ),
    )
    monkeypatch.setattr(backfill, "upsert_classification", lambda *a, **k: "protected")

    result = backfill.run_backfill(db, user_id, limit=10, include_task_counts=True)

    assert result["created"] == 0
    assert result["skipped_user_overrides"] == 1
    # task_created must come from actual upsert outcomes, not the candidate
    # list -- this message was unclassified at selection, but nothing was
    # persisted, so reporting it as created would be a lie the task result
    # (classify_latest_threads) passes straight to the client.
    assert result["task_created"] == 0
    assert result["task_processed"] == 1


# ---------------------------------------------------------------------------
# §1's required "runs through an ingester or backfill" test (plan:
# 2026-08-16-classifier-default-honesty): a failed BYOK call with no
# fallback_local opt-in must leave NO classification row written, not just
# `classify()` returning `None` in isolation -- the write guard lives at the
# call site (gmail_ingest.py:606's ingest twin), and run_backfill's own copy
# is what's exercised here. classify_with_usage is deliberately NOT mocked:
# it's the real dispatch (call_chat_completion IS mocked, at the wire) that
# has to produce verdict=None for this to mean anything.
# ---------------------------------------------------------------------------


def test_run_backfill_byok_failure_with_no_fallback_writes_no_classification_row(monkeypatch):
    from app.services.nlp import classifier as classifier_module, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier_module, "call_chat_completion", failing_call)

    user_id = uuid4()
    db = _single_message_db()
    # fallback_local defaults False -- no encoder fallback, no heuristic
    # fallback (phase 2, D-C/D-H left the heuristic out of this chain
    # entirely), so a failed call must leave the message unclassified.
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))

    upserts = []
    monkeypatch.setattr(
        backfill, "upsert_classification",
        lambda *a, **k: (upserts.append(k), "written")[1],
    )

    result = backfill.run_backfill(db, user_id, limit=10, backend="llm")

    assert upserts == []  # the write guard: no classification row for a None verdict
    assert result["created"] == 0
    assert result["left_unclassified"] == 1


def test_run_backfill_byok_opt_in_dormant_under_heuristic_then_writes_a_row_under_auto(
    monkeypatch,
):
    """Companion to test_classifier.py's direct-dispatch version of this same
    §1 scenario, but through run_backfill so the write side is proven too:
    under a "heuristic" global backend the opted-in credential is never
    reached (no wire call, a heuristic row still gets written since the
    heuristic always produces a verdict); flipping to "auto" reaches the
    same stored credential and its row carries the BYOK model_version."""
    from app.services.nlp import classifier as classifier_module, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    wire_calls = []

    def spy_call_chat_completion(*args, **kwargs):
        wire_calls.append(1)
        content = json.dumps({"label": "fyi", "confidence": 0.5, "rationale": "r"})
        return LlmCallResult(content=content, usage=None)

    monkeypatch.setattr(classifier_module, "call_chat_completion", spy_call_chat_completion)

    user_id = uuid4()
    routing = ClassificationRouting(mode="user", credential=_CRED)
    monkeypatch.setattr(backfill, "ClassificationRouter", _fake_router_returning(routing))
    # The successful BYOK call below has provider_call_succeeded=True, so
    # run_backfill's real UsageAccumulator would try a genuine DB upsert on
    # flush -- not what this test is about (usage recording has its own
    # coverage above), so swap in the same fake the usage tests use.
    monkeypatch.setattr(backfill, "UsageAccumulator", lambda uid: _FakeAcc(uid))

    upserted_model_versions = []
    monkeypatch.setattr(
        backfill, "upsert_classification",
        lambda db_arg, *, message_id, label, confidence, rationale, model_version: (
            upserted_model_versions.append(model_version), "written"
        )[1],
    )

    monkeypatch.setattr(classifier_module.settings, "classifier_backend", "heuristic")
    dormant_result = backfill.run_backfill(db=_single_message_db(), user_id=user_id, limit=10)
    assert wire_calls == []
    assert upserted_model_versions == ["heuristic-v1"]

    monkeypatch.setattr(classifier_module.settings, "classifier_backend", "auto")
    active_result = backfill.run_backfill(db=_single_message_db(), user_id=user_id, limit=10)
    assert wire_calls == [1]
    assert upserted_model_versions == ["heuristic-v1", f"{_CRED.provider}:{_CRED.model}"]
    assert dormant_result["created"] == 1
    assert active_result["created"] == 1
