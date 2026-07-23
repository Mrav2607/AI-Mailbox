"""The scheduler: who gets synced, who doesn't, and how we know it's alive."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.workers import tasks_ingest


@pytest.fixture
def fake_redis(monkeypatch):
    """Stand-in for the heartbeat store, so tests don't need a live Redis."""
    store: dict[str, str] = {}
    client = MagicMock()
    client.set.side_effect = lambda key, value, ex=None: store.__setitem__(key, value)
    client.get.side_effect = lambda key: store.get(key)
    monkeypatch.setattr(tasks_ingest.redis, "from_url", lambda _url: client)
    # The client is cached per process, so without clearing it a test would
    # reuse whichever mock got there first -- and monkeypatch restores the
    # attribute, not the cache.
    monkeypatch.setattr(tasks_ingest, "_redis_client", None)
    return store


def _dispatch_with(monkeypatch, fake_redis, *, candidates, start=None):
    """`candidates` is a list of (user_id, account_id, provider) triples --
    _eligible_provider_rows' real shape now that it covers both providers."""
    db = MagicMock()
    monkeypatch.setattr(
        tasks_ingest, "SessionLocal", lambda: nullcontext(db)
    )
    monkeypatch.setattr(
        tasks_ingest, "_eligible_provider_rows", lambda _db: candidates
    )
    monkeypatch.setattr(tasks_ingest, "_log_stale_mailboxes", lambda _db, _ids: None)
    calls: list[dict] = []

    def default_start(_db, user_id, account_id, *, mode, options, provider="gmail"):
        calls.append(
            {
                "user_id": user_id,
                "account_id": account_id,
                "mode": mode,
                "options": options,
                "provider": provider,
            }
        )
        return SimpleNamespace(id=uuid4()), False

    monkeypatch.setattr(tasks_ingest, "start_sync_run", start or default_start)
    result = tasks_ingest.dispatch_scheduled_syncs.run()
    return result, calls


def test_dispatch_creates_a_scheduled_new_only_run(monkeypatch, fake_redis):
    user_id = uuid4()
    result, calls = _dispatch_with(
        monkeypatch, fake_redis, candidates=[(user_id, uuid4(), "gmail")]
    )

    assert result == {"dispatched": 1, "skipped": 0, "failed": 0}
    assert calls[0]["user_id"] == user_id
    assert calls[0]["mode"] == "scheduled"
    # A scheduled tick must never balloon into a backfill.
    assert calls[0]["options"]["new_only"] is True
    assert calls[0]["options"]["skip_existing"] is True


def test_dispatch_enqueues_one_run_per_eligible_account(monkeypatch, fake_redis):
    # Multi-account support means one candidate row per account, not per user
    # -- both of this user's accounts must get their own run.
    user_id = uuid4()
    account_a = uuid4()
    account_b = uuid4()

    result, calls = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(user_id, account_a, "gmail"), (user_id, account_b, "gmail")],
    )

    assert result == {"dispatched": 2, "skipped": 0, "failed": 0}
    assert {call["account_id"] for call in calls} == {account_a, account_b}
    assert all(call["user_id"] == user_id for call in calls)


def test_dispatch_enqueues_the_matching_provider_for_each_account(monkeypatch, fake_redis):
    # A user with both a gmail and an outlook account must have each one
    # dispatched through its own provider, not both defaulting to gmail.
    user_id = uuid4()
    gmail_account = uuid4()
    outlook_account = uuid4()

    result, calls = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[
            (user_id, gmail_account, "gmail"),
            (user_id, outlook_account, "outlook"),
        ],
    )

    assert result == {"dispatched": 2, "skipped": 0, "failed": 0}
    providers_by_account = {call["account_id"]: call["provider"] for call in calls}
    assert providers_by_account == {gmail_account: "gmail", outlook_account: "outlook"}


def test_dispatch_defers_to_an_already_active_run(monkeypatch, fake_redis):
    # Cooperating with uq_mail_sync_run_active_account rather than fighting
    # it: a manual run (or the browser fallback) already holds this account's
    # slot.
    def deduping_start(_db, _user_id, _account_id, *, mode, options, provider="gmail"):
        return SimpleNamespace(id=uuid4()), True

    result, _ = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(uuid4(), uuid4(), "gmail")],
        start=deduping_start,
    )

    assert result == {"dispatched": 0, "skipped": 1, "failed": 0}


def test_a_paused_account_being_skipped_does_not_block_its_sibling(monkeypatch, fake_redis):
    # _eligible_provider_rows is what actually filters out paused accounts;
    # here we simulate its output (the paused account already excluded) and
    # confirm dispatch still processes the healthy sibling on the same user.
    user_id = uuid4()
    healthy_account = uuid4()

    result, calls = _dispatch_with(
        monkeypatch, fake_redis, candidates=[(user_id, healthy_account, "gmail")]
    )

    assert result == {"dispatched": 1, "skipped": 0, "failed": 0}
    assert calls[0]["account_id"] == healthy_account


def test_eligible_provider_rows_has_no_per_user_collapse():
    # The old query collapsed to one (oldest) account per user via a
    # DISTINCT ON subquery -- multi-account support means every eligible
    # account has to survive that query untouched. A MagicMock db can't
    # evaluate a WHERE clause, so check the query shape instead.
    from sqlalchemy.dialects import postgresql

    captured = {"sql": []}
    db = MagicMock()

    def execute(stmt):
        captured["sql"].append(str(stmt.compile(dialect=postgresql.dialect())).upper())
        return MagicMock(all=lambda: [])

    db.execute.side_effect = execute
    rows = tasks_ingest._eligible_provider_rows(db)

    assert rows == []
    # One query per provider (gmail, then outlook); both share the
    # refresh-token/not-paused gate.
    assert len(captured["sql"]) == 2
    for sql in captured["sql"]:
        assert "DISTINCT ON" not in sql
        assert "SYNC_PAUSED_AT IS NULL" in sql
        assert "REFRESH_TOKEN IS NOT NULL" in sql


def test_one_users_failure_does_not_stop_the_rest(monkeypatch, fake_redis):
    good = uuid4()
    bad = uuid4()
    seen = []

    def flaky_start(_db, user_id, _account_id, *, mode, options, provider="gmail"):
        if user_id == bad:
            raise RuntimeError("gmail exploded")
        seen.append(user_id)
        return SimpleNamespace(id=uuid4()), False

    result, _ = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(bad, uuid4(), "gmail"), (good, uuid4(), "gmail")],
        start=flaky_start,
    )

    assert result == {"dispatched": 1, "skipped": 0, "failed": 1}
    assert seen == [good]


def test_dispatch_checks_in_before_doing_any_work(monkeypatch, fake_redis):
    # The heartbeat is the dead-man's switch. If it only landed after a
    # successful pass, one bad user would look like a dead scheduler and the
    # beat container would restart-loop for no reason.
    def exploding_start(_db, _user_id, _account_id, *, mode, options, provider="gmail"):
        raise RuntimeError("boom")

    _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(uuid4(), uuid4(), "gmail")],
        start=exploding_start,
    )

    assert tasks_ingest.DISPATCHER_HEARTBEAT_KEY in fake_redis


def test_the_redis_client_is_built_once_not_per_read(monkeypatch):
    # Every health poll from every open tab reads the heartbeat; building a
    # client per read means a TCP connection set up and torn down each time.
    monkeypatch.setattr(tasks_ingest, "_redis_client", None)
    built = []
    monkeypatch.setattr(
        tasks_ingest.redis, "from_url", lambda _url: built.append(1) or MagicMock()
    )

    tasks_ingest.read_dispatcher_heartbeat()
    tasks_ingest.read_dispatcher_heartbeat()
    tasks_ingest.write_dispatcher_heartbeat()

    assert len(built) == 1


def test_heartbeat_round_trips(monkeypatch, fake_redis):
    tasks_ingest.write_dispatcher_heartbeat()
    beat = tasks_ingest.read_dispatcher_heartbeat()
    assert beat is not None
    assert (datetime.now(timezone.utc) - beat) < timedelta(seconds=30)


def test_missing_heartbeat_reads_as_no_scheduler(fake_redis, monkeypatch):
    # An expired/absent key is the whole signal -- it must not raise.
    assert tasks_ingest.read_dispatcher_heartbeat() is None


def test_garbage_heartbeat_reads_as_no_scheduler(fake_redis, monkeypatch):
    fake_redis[tasks_ingest.DISPATCHER_HEARTBEAT_KEY] = "not-a-timestamp"
    assert tasks_ingest.read_dispatcher_heartbeat() is None


def test_beat_schedule_is_registered_when_enabled():
    from app.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "dispatch-scheduled-syncs" in schedule
    assert (
        schedule["dispatch-scheduled-syncs"]["task"]
        == "app.workers.tasks_ingest.dispatch_scheduled_syncs"
    )


def test_a_redis_outage_degrades_the_read_instead_of_raising(monkeypatch):
    # The health endpoint calls this, and Redis being down is exactly when
    # someone's looking at health -- it must return "can't tell" (None), not
    # 500. It should also drop the cached client so the dead socket isn't reused.
    monkeypatch.setattr(tasks_ingest, "_redis_client", object())
    dead = MagicMock()
    dead.get.side_effect = tasks_ingest.redis.ConnectionError("redis is down")
    monkeypatch.setattr(tasks_ingest.redis, "from_url", lambda _url: dead)
    monkeypatch.setattr(tasks_ingest, "_redis_client", dead)

    assert tasks_ingest.read_dispatcher_heartbeat() is None
    assert tasks_ingest._redis_client is None


def test_prune_deletes_old_terminal_runs_while_shielding_the_latest_success(
    monkeypatch,
):
    # A mock DB can't run the SQL, but it CAN capture the exact statement the
    # real function builds. Compiling that catches a regression in prune itself
    # -- someone dropping the anchor guard, the nullslast, or the terminal
    # filter -- which a hand-mirrored copy of the query wouldn't.
    from sqlalchemy.dialects import postgresql

    captured = {}
    db = MagicMock()

    def execute(stmt):
        captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
        return MagicMock(rowcount=3)

    db.execute.side_effect = execute
    monkeypatch.setattr(tasks_ingest, "SessionLocal", lambda: nullcontext(db))

    result = tasks_ingest.prune_sync_runs.run()

    sql = captured["sql"].upper()
    assert result == {"deleted": 3}
    # Keep-latest-success anchor, null-safe, and only finished runs are touched.
    # (status values render as a bound POSTCOMPILE placeholder, not literals.)
    assert "DISTINCT ON" in sql
    assert "NULLS LAST" in sql
    assert "NOT IN" in sql
    assert "STATUS IN" in sql
    db.commit.assert_called_once()
