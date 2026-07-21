from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.db.models import MailSyncRun
from app.services.sync_runs import expire_stale_sync, renew_sync, sync_payload


_OPTIONS = {
    "max_results": 100,
    "skip_existing": True,
    "classify_messages": True,
    "new_only": True,
}


def test_sync_payload_is_terminal_only_after_success_or_failure():
    run = MailSyncRun(
        id=uuid4(),
        user_id=uuid4(),
        mode="auto",
        status="running",
        options={},
        lease_expires_at=datetime.now(timezone.utc),
    )
    assert sync_payload(run)["ready"] is False
    run.status = "succeeded"
    run.result = {"threads_upserted": 2}
    assert sync_payload(run)["ready"] is True
    assert sync_payload(run)["result"] == {"threads_upserted": 2}


def test_renew_sync_updates_heartbeat_and_lease():
    run = MagicMock(status="queued")
    db = MagicMock()
    before = datetime.now(timezone.utc)
    renew_sync(db, run, "running")
    assert run.status == "running"
    assert run.heartbeat_at >= before
    assert run.lease_expires_at > run.heartbeat_at


def test_expire_stale_sync_expires_every_stale_run_not_just_one():
    # A user with two Gmail accounts can have two stale active runs at once --
    # stopping at the first would leave the second wedged until its own lease
    # expired on its own.
    stale_a = MagicMock(status="running")
    stale_b = MagicMock(status="queued")
    db = MagicMock()
    db.scalars.return_value.all.return_value = [stale_a, stale_b]

    expire_stale_sync(db, uuid4())

    for run in (stale_a, stale_b):
        assert run.status == "failed"
        assert run.error == "sync lease expired"
        assert run.completed_at is not None
    db.commit.assert_called_once()


def _start_run(monkeypatch, *, delay, commit_fails_after):
    """Drive start_sync_run with a stubbed broker and a commit that can fail.

    commit_fails_after: how many commits succeed before one raises. The row
    INSERT is commit #1, recording task_id is commit #2.
    """
    from app.services import sync_runs

    monkeypatch.setattr(sync_runs, "active_sync", lambda _db, _uid, _account_id: None)
    task_module = MagicMock()
    task_module.ingest_gmail_for_user.delay = delay
    monkeypatch.setitem(
        __import__("sys").modules, "app.workers.tasks_ingest", task_module
    )

    run = MagicMock(id=uuid4(), status="queued", error=None, completed_at=None)
    db = MagicMock()
    calls = {"n": 0}

    def commit():
        calls["n"] += 1
        if commit_fails_after is not None and calls["n"] > commit_fails_after:
            raise RuntimeError("db went away")

    db.commit.side_effect = commit
    monkeypatch.setattr(sync_runs, "MailSyncRun", lambda **kwargs: run)
    return sync_runs, db, run


def test_a_failed_enqueue_releases_the_users_sync_slot(monkeypatch):
    # Nothing is running, so the committed row would otherwise hold the single
    # per-user slot until the 2h queued lease expired.
    def exploding_delay(**_kwargs):
        raise RuntimeError("broker down")

    sync_runs, db, run = _start_run(
        monkeypatch, delay=exploding_delay, commit_fails_after=None
    )

    with pytest.raises(RuntimeError, match="broker down"):
        sync_runs.start_sync_run(db, uuid4(), uuid4(), mode="scheduled", options=_OPTIONS)

    assert run.status == "failed"
    assert run.error == "failed to enqueue sync"
    assert run.completed_at is not None


def test_a_queued_task_is_never_marked_failed_just_because_its_id_didnt_save(
    monkeypatch,
):
    # The task is already running. Marking the run failed would release the slot
    # mid-ingest (letting the scheduler start a second concurrent run) and make
    # the worker abort on set_state's terminal guard.
    sync_runs, db, run = _start_run(
        monkeypatch,
        delay=MagicMock(return_value=MagicMock(id="task-1")),
        commit_fails_after=1,
    )

    with pytest.raises(RuntimeError, match="db went away"):
        sync_runs.start_sync_run(db, uuid4(), uuid4(), mode="scheduled", options=_OPTIONS)

    assert run.status == "queued"
    assert run.error is None
    assert run.completed_at is None
    db.rollback.assert_called()


def test_an_active_run_on_one_account_does_not_block_a_sibling_account(monkeypatch):
    # active_sync is scoped by provider_account_id now -- account B must get
    # its own run even while account A is mid-sync for the same user.
    from app.services import sync_runs

    account_a = uuid4()
    account_b = uuid4()

    def scoped_active_sync(_db, _uid, account_id):
        return MagicMock() if account_id == account_a else None

    monkeypatch.setattr(sync_runs, "active_sync", scoped_active_sync)
    task_module = MagicMock()
    task_module.ingest_gmail_for_user.delay = MagicMock(
        return_value=MagicMock(id="task-1")
    )
    monkeypatch.setitem(
        __import__("sys").modules, "app.workers.tasks_ingest", task_module
    )

    run = MagicMock(id=uuid4(), status="queued", error=None, completed_at=None)
    db = MagicMock()
    monkeypatch.setattr(sync_runs, "MailSyncRun", lambda **kwargs: run)

    result_run, deduplicated = sync_runs.start_sync_run(
        db, uuid4(), account_b, mode="scheduled", options=_OPTIONS
    )

    assert deduplicated is False
    assert result_run is run
