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
    db = MagicMock()
    monkeypatch.setattr(
        tasks_ingest, "SessionLocal", lambda: nullcontext(db)
    )
    monkeypatch.setattr(
        tasks_ingest, "_eligible_provider_rows", lambda _db: candidates
    )
    monkeypatch.setattr(tasks_ingest, "_log_stale_mailboxes", lambda _db, _ids: None)
    calls: list[dict] = []

    def default_start(_db, user_id, *, mode, options):
        calls.append({"user_id": user_id, "mode": mode, "options": options})
        return SimpleNamespace(id=uuid4()), False

    monkeypatch.setattr(tasks_ingest, "start_sync_run", start or default_start)
    result = tasks_ingest.dispatch_scheduled_syncs.run()
    return result, calls


def test_dispatch_creates_a_scheduled_new_only_run(monkeypatch, fake_redis):
    user_id = uuid4()
    result, calls = _dispatch_with(
        monkeypatch, fake_redis, candidates=[(user_id, uuid4())]
    )

    assert result == {"dispatched": 1, "skipped": 0, "failed": 0}
    assert calls[0]["user_id"] == user_id
    assert calls[0]["mode"] == "scheduled"
    # A scheduled tick must never balloon into a backfill.
    assert calls[0]["options"]["new_only"] is True
    assert calls[0]["options"]["skip_existing"] is True


def test_dispatch_defers_to_an_already_active_run(monkeypatch, fake_redis):
    # Cooperating with uq_mail_sync_run_active_user rather than fighting it:
    # a manual run (or the browser fallback) already holds the slot.
    def deduping_start(_db, _user_id, *, mode, options):
        return SimpleNamespace(id=uuid4()), True

    result, _ = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(uuid4(), uuid4())],
        start=deduping_start,
    )

    assert result == {"dispatched": 0, "skipped": 1, "failed": 0}


def test_one_users_failure_does_not_stop_the_rest(monkeypatch, fake_redis):
    good = uuid4()
    bad = uuid4()
    seen = []

    def flaky_start(_db, user_id, *, mode, options):
        if user_id == bad:
            raise RuntimeError("gmail exploded")
        seen.append(user_id)
        return SimpleNamespace(id=uuid4()), False

    result, _ = _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(bad, uuid4()), (good, uuid4())],
        start=flaky_start,
    )

    assert result == {"dispatched": 1, "skipped": 0, "failed": 1}
    assert seen == [good]


def test_dispatch_checks_in_before_doing_any_work(monkeypatch, fake_redis):
    # The heartbeat is the dead-man's switch. If it only landed after a
    # successful pass, one bad user would look like a dead scheduler and the
    # beat container would restart-loop for no reason.
    def exploding_start(_db, _user_id, *, mode, options):
        raise RuntimeError("boom")

    _dispatch_with(
        monkeypatch,
        fake_redis,
        candidates=[(uuid4(), uuid4())],
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
