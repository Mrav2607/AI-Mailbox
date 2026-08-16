import time
from unittest.mock import MagicMock
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.ingest import gmail_ingest
from app.services.ingest.gmail_ingest import (
    _collect_history_thread_ids,
    _count_new_threads,
    _merge_sync_thread_ids,
    _should_reopen_thread,
    ingest_gmail_messages,
)
from app.services.nlp.classifier import ClassificationAttempt
from app.services.nlp.llm_client import LlmUsage
from app.services.nlp.providers import ClassificationRouting, LlmCredential

# A real UUID string, not the old opaque "u1" placeholder -- usage recording
# now does UsageAccumulator(uuid.UUID(user_id)) once at construction (every
# run, whether or not anything ever gets classified), so every test driving
# ingest_gmail_messages needs a user_id that actually parses as one.
_USER_ID = str(uuid4())


def test_history_sync_includes_known_threads_with_new_messages():
    client = MagicMock()
    client.list_history.side_effect = [
        {
            "historyId": "105",
            "nextPageToken": "page-2",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "m1", "threadId": "known-thread"}},
                        {"message": {"id": "m2", "threadId": "new-thread"}},
                    ]
                }
            ],
        },
        {
            "historyId": "110",
            "history": [
                {
                    "messagesAdded": [
                        # A second message in the same changed conversation
                        # must not make us fetch the thread twice.
                        {"message": {"id": "m3", "threadId": "known-thread"}}
                    ]
                }
            ],
        },
    ]

    changed, cursor = _collect_history_thread_ids(client, "100")

    assert changed == ["known-thread", "new-thread"]
    assert cursor == "110"
    assert client.list_history.call_args_list == [
        (("100",), {"page_token": None}),
        (("100",), {"page_token": "page-2"}),
    ]


def test_changed_threads_take_priority_and_are_deduplicated():
    assert _merge_sync_thread_ids(
        ["known-thread", "new-thread"],
        ["known-thread", "recent-thread"],
        ["new-thread", "old-thread"],
    ) == ["known-thread", "new-thread", "recent-thread", "old-thread"]


def test_new_thread_count_excludes_replies_to_known_threads():
    assert (
        _count_new_threads(
            ["known-thread", "new-thread"],
            ["new-thread", "old-backfill"],
            1,
            {"known-thread"},
        )
        == 1
    )


def test_new_only_selection_drops_the_backfill_tail():
    # listed_ids beyond head_new are older history the DB hasn't backfilled;
    # a new-only sync must never fetch them.
    listed = ["new-a", "new-b", "old-1", "old-2"]
    head_new = 2
    assert _merge_sync_thread_ids(["changed"], [], listed[:head_new]) == [
        "changed",
        "new-a",
        "new-b",
    ]


def test_disconnect_race_missing_account_raises_terminal_error(monkeypatch):
    # The account was deleted after the run got queued (a disconnect landing
    # mid-flight). Loading it by id has to fail closed -- not silently fall
    # back to some other account, and not retry forever against one that's
    # gone.
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = None

    with pytest.raises(ValueError, match="not connected"):
        ingest_gmail_messages(db, _USER_ID, provider_account_id=str(uuid4()))
    # Pins that the explicit account lookup ran exactly once -- with a
    # MagicMock db returning None everywhere, a buggy fallback to "oldest
    # connected account" would raise this same ValueError, so without this
    # count the test above can't tell the two apart.
    assert db.execute.call_count == 1


def test_ingest_with_an_explicit_account_scopes_existing_thread_ids_to_it(monkeypatch):
    # A second Gmail account on the same user must not look like it already
    # has every thread the first account pulled -- existing_thread_ids has to
    # filter on provider_account_id, not just user_id.
    account_id = uuid4()
    provider = MagicMock(
        id=account_id, access_token="tok", token_expiry=None, gmail_history_id="100"
    )
    captured_thread_query = {}

    def execute(stmt):
        sql = str(stmt).lower()
        result = MagicMock()
        if "from provider_account" in sql:
            result.scalars.return_value.first.return_value = provider
        elif "from mail_thread" in sql:
            captured_thread_query["sql"] = sql
            result.scalars.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.execute.side_effect = execute

    client = MagicMock()
    client.list_history.return_value = {"historyId": "110", "history": []}
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    ingest_gmail_messages(db, _USER_ID, provider_account_id=str(account_id), new_only=True)

    assert "provider_account_id" in captured_thread_query["sql"]
    assert "user_id" not in captured_thread_query["sql"]


def test_new_only_with_no_baseline_at_all_returns_without_pulling():
    # No known threads AND no cursor means no baseline to define "new" against —
    # the pull must bail out before touching Gmail instead of importing the
    # whole account.
    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id=None)
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert result["fetched"] == 0
    assert result["new_threads"] == 0
    assert result["threads_upserted"] == 0
    db.commit.assert_not_called()


def test_new_only_with_a_cursor_but_no_threads_still_syncs(monkeypatch):
    # An account whose first ingest found an empty mailbox has no threads but
    # DOES have a cursor. Treating "no threads" as "no baseline" would strand it
    # forever: no threads means no pull, and no pull means no first thread.
    provider = MagicMock(
        access_token="tok", token_expiry=None, gmail_history_id="4242"
    )
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = []

    client = MagicMock()
    client.list_history.return_value = {
        "historyId": "4300",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "first-ever"}}]}
        ],
    }
    client.get_thread.return_value = {"messages": []}
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    result = ingest_gmail_messages(db, _USER_ID, new_only=True)

    client.list_history.assert_called_once()
    assert result["fetched"] == 1
    assert provider.gmail_history_id == "4300"


def test_new_inbound_reply_reopens_done_thread_but_sent_or_existing_mail_does_not():
    done_at = datetime.now(timezone.utc)
    inbound = {
        "provider_message_id": "new-inbound",
        "sent_at": done_at + timedelta(seconds=1),
        "label_ids": ["INBOX"],
    }
    sent = {
        "provider_message_id": "new-sent",
        "sent_at": done_at + timedelta(seconds=2),
        "label_ids": ["SENT"],
    }
    assert _should_reopen_thread(done_at, {"old"}, [inbound]) is True
    assert _should_reopen_thread(done_at, {"old"}, [sent]) is False
    assert _should_reopen_thread(done_at, {"new-inbound"}, [inbound]) is False


def _deleted_thread_setup(monkeypatch, status_code):
    """A mailbox whose history names one thread that Gmail no longer has."""
    provider = MagicMock(
        access_token="tok", token_expiry=None, gmail_history_id="100"
    )
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = ["known-thread"]

    client = MagicMock()
    client.list_history.return_value = {
        "historyId": "110",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "gone-thread"}}]}
        ],
    }
    response = MagicMock(status_code=status_code)
    client.get_thread.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=response
    )
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)
    return db, provider


def test_thread_deleted_after_history_named_it_is_skipped(monkeypatch):
    db, provider = _deleted_thread_setup(monkeypatch, 404)

    result = ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert result["threads_missing"] == 1
    assert result["threads_upserted"] == 0
    assert result["new_threads"] == 0
    assert provider.gmail_history_id == "110"


def test_a_thread_fetch_failing_for_other_reasons_still_raises(monkeypatch):
    db, provider = _deleted_thread_setup(monkeypatch, 500)

    with pytest.raises(httpx.HTTPStatusError):
        ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert provider.gmail_history_id == "100"


@pytest.fixture
def google_creds(monkeypatch):
    """_refresh_access_token no-ops without client credentials.

    Without this the token tests pass locally (where deploy/.env supplies real
    ones) and vacuously "pass" anywhere else by never reaching the request.
    """
    monkeypatch.setattr(gmail_ingest.settings, "google_client_id", "cid")
    monkeypatch.setattr(gmail_ingest.settings, "google_client_secret", "secret")


def _token_response(status_code, payload=None, text="err"):
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = payload if payload is not None else {}
    if payload is None:
        resp.json.side_effect = ValueError("not json")
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        text, request=MagicMock(), response=resp
    )
    return resp


def test_revoked_refresh_token_pauses_the_account_and_does_not_retry(monkeypatch, google_creds):
    # invalid_grant is permanent: only the user reconnecting fixes it. Raising
    # ValueError puts it on the task's terminal branch, so we stop hammering
    # Google every cycle forever.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid, reason=reason)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx,
        "post",
        lambda *a, **k: _token_response(400, {"error": "invalid_grant"}),
    )

    with pytest.raises(ValueError, match="revoked"):
        gmail_ingest._refresh_access_token(provider)

    assert paused["id"] == provider.id
    assert paused["reason"] == "reauth_required"


def test_a_transient_token_failure_still_raises_for_retry(monkeypatch, google_creds):
    # 500 is Google having a bad day -- retryable. Pausing here would take a
    # working account out of the schedule until someone noticed.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx, "post", lambda *a, **k: _token_response(500, {})
    )

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}


def test_a_401_from_the_token_endpoint_is_our_problem_not_the_users(monkeypatch, google_creds):
    # 401 means invalid_client -- our credentials are misconfigured. Pausing the
    # user's account would blame them for an operator error.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx,
        "post",
        lambda *a, **k: _token_response(401, {"error": "invalid_client"}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}


def _make_pipeline_db(provider):
    """A db wired for a full gmail-ingest run: thread/message upserts each
    return a fresh id, and no message ever has a prior classification -- so
    every message reaches the classifier instead of being skipped as
    "already done". Shared across the router and usage-recording tests
    below, which all drive the real thread/message loop end to end.
    """
    def execute(stmt):
        sql = str(stmt).lower()
        result = MagicMock()
        if "from provider_account" in sql:
            result.scalars.return_value.first.return_value = provider
        elif "from mail_thread" in sql and "provider_thread_id" in sql:
            result.scalars.return_value.all.return_value = []
        elif "insert into mail_thread" in sql:
            result.scalar_one.return_value = uuid4()
        elif "insert into mail_message" in sql:
            result.scalar_one.return_value = uuid4()
        elif "from classification" in sql:
            result.scalars.return_value.first.return_value = None
        return result

    db = MagicMock()
    db.execute.side_effect = execute
    # Thread reopen bookkeeping (`db.get(MailThread, ...)`) is orthogonal to
    # every test below -- None keeps that branch a no-op.
    db.get.return_value = None
    return db


def _history_page(thread_ids):
    """One Gmail history page naming each thread exactly once, in order."""
    return {
        "historyId": "9999",
        "history": [
            {"messagesAdded": [{"message": {"id": f"m{i}", "threadId": tid}}]}
            for i, tid in enumerate(thread_ids)
        ],
    }


def _raw_thread(tid, mid):
    return {"messages": [{"id": mid, "threadId": tid, "payload": {"headers": []}, "snippet": "hi"}]}


def test_classification_router_is_built_once_per_run_not_once_per_message(monkeypatch):
    # ClassificationRouter's 60s memo only pays off if one instance lives for
    # the whole run -- rebuilding it per message would restart that memo (and
    # re-query routing) on every single message instead of once a minute. The
    # other half of the contract, routing_for called per message, is what
    # lets a mid-run consent revocation take effect within a minute.

    # A regression that keeps every router call intact but drops `routing=`
    # from the `classify_with_usage()` call would still pass every assertion
    # above -- so we also need to prove this exact routing object is what
    # `classify_with_usage` actually receives, not just that routing_for got
    # called.
    SENTINEL_ROUTING = ClassificationRouting(mode="off", credential=None)

    router_instances = []
    routing_calls = []
    classify_calls = []

    class CountingRouter:
        def __init__(self, user_id):
            router_instances.append(user_id)

        def routing_for(self, db):
            routing_calls.append(db)
            return SENTINEL_ROUTING

    def fake_classify_with_usage(text, *, routing, policy=None):
        classify_calls.append(routing)
        return ClassificationAttempt(
            verdict=("other", 0.5, "stub", "test-model"),
            provider_call_succeeded=False,
            usage=None,
        )

    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", CountingRouter)
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", fake_classify_with_usage)

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242")
    db = _make_pipeline_db(provider)

    client = MagicMock()
    client.list_history.return_value = {
        "historyId": "4300",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}
        ],
    }
    client.get_thread.return_value = {
        "messages": [
            {"id": "m1", "threadId": "t1", "payload": {"headers": []}, "snippet": "hi"},
            {"id": "m2", "threadId": "t1", "payload": {"headers": []}, "snippet": "there"},
        ]
    }
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    result = ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert result["messages_upserted"] == 2
    assert result["classified"] == 2
    assert router_instances == [_USER_ID]
    assert len(routing_calls) == 2
    # Both messages' classify calls got the router's actual routing object --
    # not a dropped kwarg silently falling back to DEFAULT SERVER ROUTING.
    assert len(classify_calls) == 2
    assert all(routing is SENTINEL_ROUTING for routing in classify_calls)


def test_more_than_30_rate_limited_messages_trips_the_breaker_not_a_retry_storm(monkeypatch):
    """Codex review regression (blocker, phase 3 of the LLM-failure work):
    before the per-run ClassificationBreaker, a sustained 429 storm across
    30+ messages in one ingest run would burn up to N * WORKER_RETRIES'
    ~60s wait budget -- comfortably past the ingest task's own 1740s soft
    time limit. The task would then hit its hard limit, get replayed by
    Celery's `autoretry_for`, and reprocess the same messages again (a
    no-verdict outcome never writes a Classification row) -- the exact
    retry storm this phase exists to prevent.

    The breaker caps the number of REAL classify_with_usage calls at 3
    regardless of how many messages need classifying, so even accounting
    for WORKER_RETRIES' own per-call bound (exhaustively tested in
    test_llm_client.py: max_attempts=4, total_budget_s=60.0), a run like
    this costs at most 3 * (60s waits + call time) -- a small fraction of
    the soft limit, not 35 times that.

    No real sleeping anywhere: classify_with_usage is mocked to return
    immediately (its own retry-budget behavior is proven elsewhere), so the
    wall-clock assertion below is really proving this TEST doesn't block --
    the production time-budget claim above follows from that plus the
    already-proven WORKER_RETRIES bound.
    """
    message_count = 35
    SENTINEL_ROUTING = ClassificationRouting(mode="off", credential=None)

    class FixedRouter:
        def __init__(self, user_id):
            pass

        def routing_for(self, db):
            return SENTINEL_ROUTING

    call_log = []

    def counting_classify(text, *, routing, policy=None):
        call_log.append(1)
        # Every call "fails" the same way a sustained 429 storm would --
        # this is precisely the scenario the breaker exists for.
        return ClassificationAttempt(verdict=None, provider_call_succeeded=False, usage=None)

    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", FixedRouter)
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", counting_classify)

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242")
    db = _make_pipeline_db(provider)

    client = MagicMock()
    client.list_history.return_value = _history_page(["t1"])
    client.get_thread.return_value = {
        "messages": [
            {"id": f"m{i}", "threadId": "t1", "payload": {"headers": []}, "snippet": f"hi {i}"}
            for i in range(message_count)
        ]
    }
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    start = time.monotonic()
    result = ingest_gmail_messages(db, _USER_ID, new_only=True)
    elapsed = time.monotonic() - start

    # The breaker: exactly 3 real classify calls, never message_count of them.
    assert len(call_log) == 3

    # Ingest itself never aborted -- every message still got upserted...
    assert result["messages_upserted"] == message_count
    # ...and the history cursor still advanced exactly as it would without
    # the breaker (_history_page's own historyId).
    assert provider.gmail_history_id == "9999"

    # Every message the breaker skipped (message_count - 3 never attempted,
    # plus the 3 real attempts that all came back no-verdict) is reported --
    # the user must be told, not silently shorted.
    assert result["left_unclassified"] == message_count

    # Reaching this line at all proves ingest_gmail_messages never raised --
    # which is what keeps Celery's autoretry_for from ever seeing a failure
    # and replaying the whole run (reprocessing these same messages again).
    assert elapsed < 1.0  # nothing in this test actually sleeps


# ---------------------------------------------------------------------------
# Per-user usage recording and its flush sites (docs/plans/2026-08-02-llm-
# usage-visibility-plan.md §5) -- classify_with_usage(), UsageAccumulator,
# and the three commit points that flush it.
# ---------------------------------------------------------------------------


def _fixed_router(routing):
    """A ClassificationRouter stand-in that always hands back the same
    routing decision, for tests that only care what the recorder does with
    a given mode -- not the router's own memo/refresh behavior."""

    class FixedRouter:
        def __init__(self, user_id):
            pass

        def routing_for(self, db):
            return routing

    return FixedRouter


def _fake_classify_with_usage(usage):
    def fake(text, *, routing, policy=None):
        return ClassificationAttempt(
            verdict=("fyi", 0.5, "stub", "test-model"),
            provider_call_succeeded=True,
            usage=usage,
        )

    return fake


def _tracing_accumulator_class(trace, records, *, fail_flush_times=0):
    """Stands in for UsageAccumulator. Records calls into `records` and
    appends every flush/discard/committed call onto the shared `trace` list
    so a test can assert ORDER against db.flush/db.commit, not just that each
    method got called somewhere.
    """
    state = {"fail_flush_times": fail_flush_times}

    class TracingAccumulator:
        def __init__(self, user_id):
            self.user_id = user_id

        def record(self, stage, provider, usage, *, provider_call_succeeded):
            records.append((stage, provider, usage, provider_call_succeeded))

        def flush(self, db):
            trace.append("acc.flush")
            if state["fail_flush_times"] > 0:
                state["fail_flush_times"] -= 1
                raise SQLAlchemyError("usage flush boom")

        def discard(self):
            trace.append("acc.discard")

        def committed(self):
            trace.append("acc.committed")

    return TracingAccumulator


def _wire_tracing_db(db, trace):
    """Makes db.flush/db.commit append to `trace` too, and makes
    `with db.begin_nested():` actually propagate exceptions raised inside it
    (a bare MagicMock's __exit__ returns a truthy value by default, which
    would silently swallow the SQLAlchemyError the failing-flush test raises).
    """
    db.flush = MagicMock(side_effect=lambda: trace.append("db.flush"))
    db.commit = MagicMock(side_effect=lambda: trace.append("db.commit"))
    db.begin_nested.return_value.__exit__.return_value = False


def test_user_paid_classification_records_usage(monkeypatch):
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
        ),
    )
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class([], records)
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1"])
    client.get_thread.side_effect = [_raw_thread("t1", "m1")]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert records == [("classification", "openai", usage, True)]


def test_server_paid_classification_records_nothing(monkeypatch):
    routing = ClassificationRouting(mode="server", credential=None)
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class([], records)
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1"])
    client.get_thread.side_effect = [_raw_thread("t1", "m1")]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    ingest_gmail_messages(db, _USER_ID, new_only=True)

    # The operator's own key paid for this call -- it must never show up in
    # anyone's usage panel.
    assert records == []


def test_verdict_none_leaves_message_unclassified_and_reports_left_unclassified(monkeypatch):
    """Phase 2 (D-C, plan: 2026-08-14-llm-failure-visibility): a failed BYOK
    call the user hasn't opted into local fallback for produces verdict=None.
    Ingest must never write a null-label row (it would strand the message,
    neither classified nor a backfill candidate again) and must report it via
    left_unclassified instead of classified -- but usage still gets recorded,
    since the call may have genuinely billed the user even though it
    produced nothing to classify with (D3). The run otherwise completes
    successfully -- unlike run_backfill, ingest has no BYOK-spend reason to
    stop early on a single message."""
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
        ),
    )  # fallback_local=False
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))

    def fake_classify_with_usage(text, *, routing, policy=None):
        return ClassificationAttempt(
            verdict=None,
            provider_call_succeeded=False,
            usage=usage,
            llm_attempted=True,
            fallback_used=False,
            failure_category="connection_failed",
        )

    monkeypatch.setattr(gmail_ingest, "classify_with_usage", fake_classify_with_usage)
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class([], records)
    )
    upsert_calls = []
    monkeypatch.setattr(
        gmail_ingest, "upsert_classification",
        lambda *a, **k: (upsert_calls.append(k), "written")[1],
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1"])
    client.get_thread.side_effect = [_raw_thread("t1", "m1")]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    result = ingest_gmail_messages(db, _USER_ID, new_only=True)

    assert upsert_calls == []  # never wrote a null-label row
    assert result["classified"] == 0
    assert result["left_unclassified"] == 1
    assert records == [("classification", "openai", usage, False)]


def test_periodic_commit_flushes_usage_before_it_in_order_and_committed_runs_last(monkeypatch):
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="gemini", base_url="https://x", api_key="k", model="m"
        ),
    )
    usage = LlmUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    trace = []
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class(trace, records)
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    _wire_tracing_db(db, trace)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1", "t2"])
    client.get_thread.side_effect = [_raw_thread("t1", "m1"), _raw_thread("t2", "m2")]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    # commit_every=1 fires the periodic commit after EVERY thread, so both
    # threads' usage gets flushed there and the trailing final commit (below)
    # has nothing left to flush.
    ingest_gmail_messages(db, _USER_ID, new_only=True, commit_every=1)

    # Two periodic cycles, each: business flush, then usage flush inside the
    # SAVEPOINT, then the real commit, then committed() -- only once the
    # commit has actually returned.
    assert trace[:4] == ["db.flush", "acc.flush", "db.commit", "acc.committed"]
    assert trace[4:8] == ["db.flush", "acc.flush", "db.commit", "acc.committed"]
    # The final cursor commit still runs, but with usage already flushed
    # twice above there's nothing pending -- it must skip the usage block
    # entirely rather than re-flush an empty batch.
    assert trace[8:] == ["db.flush", "db.commit"]
    assert records == [
        ("classification", "gemini", usage, True),
        ("classification", "gemini", usage, True),
    ]


def test_final_cursor_commit_flushes_the_tail_thread_the_periodic_commit_missed(monkeypatch):
    # commit_every=10 with 11 threads: the periodic commit fires once, after
    # thread 10 (index 9). Thread 11 (index 10) never hits another periodic
    # commit -- its usage only becomes durable at the final cursor commit.
    # Losing this would silently drop the tail thread's usage on a crash.
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="groq", base_url="https://x", api_key="k", model="m"
        ),
    )
    usage = LlmUsage(prompt_tokens=1, completion_tokens=None, total_tokens=None)
    trace = []
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class(trace, records)
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    _wire_tracing_db(db, trace)
    thread_ids = [f"t{i}" for i in range(11)]
    client = MagicMock()
    client.list_history.return_value = _history_page(thread_ids)
    client.get_thread.side_effect = [_raw_thread(tid, f"m-{tid}") for tid in thread_ids]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    ingest_gmail_messages(db, _USER_ID, new_only=True, commit_every=10)

    # Exactly two flush cycles: the periodic one at thread 10, the final one
    # for thread 11's tail.
    assert trace.count("acc.flush") == 2
    assert trace.count("acc.committed") == 2
    # The final cycle is the LAST thing in the trace, and keeps the same
    # flush-before-commit-before-committed shape as the periodic one.
    assert trace[-4:] == ["db.flush", "acc.flush", "db.commit", "acc.committed"]
    assert len(records) == 11


def test_failing_usage_flush_does_not_block_the_business_commit(monkeypatch):
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="openai", base_url="https://x", api_key="k", model="m"
        ),
    )
    usage = LlmUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    trace = []
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest,
        "UsageAccumulator",
        _tracing_accumulator_class(trace, records, fail_flush_times=1),
    )

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    _wire_tracing_db(db, trace)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1"])
    client.get_thread.side_effect = [_raw_thread("t1", "m1")]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    # commit_every=10 with a single thread means the only commit is the final
    # cursor commit -- keeps the trace to one cycle so the assertions below
    # are unambiguous about which flush failed.
    result = ingest_gmail_messages(db, _USER_ID, new_only=True, commit_every=10)

    assert result["threads_upserted"] == 1
    # The SAVEPOINT rolled back the usage batch, but the outer commit for the
    # mail work itself still ran -- a usage-layer failure must never take
    # real business writes down with it.
    assert "db.commit" in trace
    assert "acc.discard" in trace
    assert trace.index("acc.flush") < trace.index("acc.discard") < trace.index("db.commit")


def test_token_refresh_mid_run_flushes_pending_usage_before_its_own_commit(monkeypatch):
    routing = ClassificationRouting(
        mode="user",
        credential=LlmCredential(
            provider="openai", base_url="https://x", api_key="k", model="m"
        ),
    )
    usage = LlmUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    trace = []
    records = []
    monkeypatch.setattr(gmail_ingest, "ClassificationRouter", _fixed_router(routing))
    monkeypatch.setattr(gmail_ingest, "classify_with_usage", _fake_classify_with_usage(usage))
    monkeypatch.setattr(
        gmail_ingest, "UsageAccumulator", _tracing_accumulator_class(trace, records)
    )
    # Bypass the real OAuth exchange -- this test is about the flush ordering
    # around refresh_client()'s commit, not the token endpoint itself.
    monkeypatch.setattr(gmail_ingest, "_refresh_access_token", lambda provider: ("new-tok", None))

    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id="4242", id=uuid4())
    db = _make_pipeline_db(provider)
    _wire_tracing_db(db, trace)
    client = MagicMock()
    client.list_history.return_value = _history_page(["t1", "t2"])
    unauthorized = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=MagicMock(status_code=401)
    )
    # t1 fetches cleanly; t2's first attempt hits a 401 (triggering the
    # mid-run refresh and its commit), then succeeds on retry.
    client.get_thread.side_effect = [
        _raw_thread("t1", "m1"),
        unauthorized,
        _raw_thread("t2", "m2"),
    ]
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    # commit_every large enough that the periodic commit never fires on its
    # own -- the only two commits in this run are the token-refresh one and
    # the final cursor commit.
    ingest_gmail_messages(db, _USER_ID, new_only=True, commit_every=10)

    assert trace.count("acc.flush") == 2
    # First cycle is the mid-run refresh commit, flushing t1's usage before
    # its own commit lands -- exactly the ordering the plan calls out.
    assert trace[:4] == ["db.flush", "acc.flush", "db.commit", "acc.committed"]
    # Second cycle is the final cursor commit, flushing t2's usage.
    assert trace[4:] == ["db.flush", "acc.flush", "db.commit", "acc.committed"]
    assert len(records) == 2


def test_a_non_json_400_is_not_mistaken_for_a_revoked_token(monkeypatch, google_creds):
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(gmail_ingest.httpx, "post", lambda *a, **k: _token_response(400))

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}
