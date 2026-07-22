"""Work-queueing routes: Gmail ingest fans out to one sync run per connected
account and comes back 202 with the whole batch, and classification backfills
enqueue a Celery task once the batch is too big to run inline.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox as mailbox_routes
from app.workers import tasks_nlp


USER_ID = uuid4()


def _account(*, paused=False):
    return SimpleNamespace(
        id=uuid4(),
        sync_paused_at=datetime.now(timezone.utc) if paused else None,
    )


def _run(account_id, mode):
    return SimpleNamespace(id=uuid4(), mode=mode, status="queued", provider_account_id=account_id)


@pytest.fixture
def client():
    user = MagicMock(id=USER_ID)
    # Same empty-result DB stub as test_validation: the inline backfill path
    # reads eligible threads, and against this stub it finds none.
    db = MagicMock()
    db.scalar.return_value = None
    db.execute.return_value.scalars.return_value.all.return_value = []
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fanout(monkeypatch):
    """Same dependency wiring as `client`, but exposes the db double so tests
    can seed the caller's provider accounts, and stubs start_sync_run so the
    fan-out logic is tested without going through Celery or the sibling
    services.sync_runs implementation."""
    user = MagicMock(id=USER_ID)
    db = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    # sync_payload's exact shape belongs to services.sync_runs; stub it so
    # these tests only assert on the route's own fan-out behavior.
    monkeypatch.setattr(
        mailbox_routes,
        "sync_payload",
        lambda run, deduplicated=False: {
            "run_id": str(run.id),
            "task_id": None,
            "mode": run.mode,
            "status": run.status,
            "ready": False,
            "deduplicated": deduplicated,
            "result": None,
            "error": None,
            "provider_account_id": str(run.provider_account_id),
        },
    )
    yield TestClient(app), db
    app.dependency_overrides.clear()


@pytest.fixture
def fake_backfill_delay(monkeypatch):
    delay = MagicMock(return_value=MagicMock(id="task-456"))
    monkeypatch.setattr(tasks_nlp.backfill_threads_for_user, "delay", delay)
    return delay


def test_ingest_requires_auth():
    resp = TestClient(app).post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 401


def test_ingest_still_validates_max_results(client):
    assert client.post("/api/v1/mail/ingest/gmail?max_results=0").status_code == 422
    assert client.post("/api/v1/mail/ingest/gmail?max_results=10000").status_code == 422


def test_ingest_with_zero_eligible_accounts_returns_empty_runs_not_an_error(fanout):
    client, db = fanout
    db.execute.return_value.scalars.return_value.all.return_value = []
    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    assert resp.json() == {"runs": []}


def test_ingest_query_excludes_paused_accounts_in_sql(fanout):
    # The route filters paused accounts out in the WHERE clause itself, not
    # with a Python-side loop -- this pins that instead of just trusting
    # whatever list the (mocked) query happens to hand back.
    client, db = fanout
    db.execute.return_value.scalars.return_value.all.return_value = []
    client.post("/api/v1/mail/ingest/gmail")
    statement = db.execute.call_args[0][0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "sync_paused_at IS NULL" in compiled


def test_ingest_fans_out_to_every_eligible_account(fanout, monkeypatch):
    client, db = fanout
    account_a = _account()
    account_b = _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account_a, account_b]
    calls = []

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        calls.append(account_id)
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    body = resp.json()
    assert calls == [account_a.id, account_b.id]
    assert [r["provider_account_id"] for r in body["runs"]] == [
        str(account_a.id),
        str(account_b.id),
    ]
    assert {r["deduplicated"] for r in body["runs"]} == {False}


def test_ingest_reports_a_deduplicated_run(fanout, monkeypatch):
    # One account already has a sync in flight -- start_sync_run hands back
    # the existing run instead of starting a second one for that account.
    client, db = fanout
    account = _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account]

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        return _run(account_id, mode), True

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    assert resp.json()["runs"][0]["deduplicated"] is True


def test_ingest_skips_paused_accounts(fanout, monkeypatch):
    client, db = fanout
    healthy = _account()
    # A real paused account would never come back from the SQL query above
    # (see test_ingest_query_excludes_paused_accounts_in_sql); this pins that
    # only what the query returns gets synced.
    db.execute.return_value.scalars.return_value.all.return_value = [healthy]
    calls = []

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        calls.append(account_id)
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    assert calls == [healthy.id]
    assert len(resp.json()["runs"]) == 1


def test_ingest_defaults_map_to_manual_mode(fanout, monkeypatch):
    client, db = fanout
    account = _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account]
    captured = {}

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        captured["user_id"] = user_id
        captured["account_id"] = account_id
        captured["mode"] = mode
        captured["options"] = options
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    assert captured["user_id"] == USER_ID
    assert captured["account_id"] == account.id
    assert captured["mode"] == "manual"
    assert captured["options"] == {
        "max_results": 25,
        "skip_existing": True,
        "classify_messages": True,
        "new_only": False,
    }


def test_ingest_custom_params_map_to_refresh_mode(fanout, monkeypatch):
    client, db = fanout
    account = _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account]
    captured = {}

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        captured["mode"] = mode
        captured["options"] = options
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post(
        "/api/v1/mail/ingest/gmail?max_results=50&skip_existing=false&classify=false"
    )
    assert resp.status_code == 202
    assert captured["mode"] == "refresh"
    assert captured["options"] == {
        "max_results": 50,
        "skip_existing": False,
        "classify_messages": False,
        "new_only": False,
    }


def test_ingest_new_only_maps_to_auto_mode(fanout, monkeypatch):
    client, db = fanout
    account = _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account]
    captured = {}

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        captured["mode"] = mode
        captured["options"] = options
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post("/api/v1/mail/ingest/gmail?new_only=true")
    assert resp.status_code == 202
    assert captured["mode"] == "auto"
    assert captured["options"]["new_only"] is True


def test_ingest_provider_account_ids_targets_only_listed_accounts(fanout, monkeypatch):
    # Three connected accounts, but the caller only wants a1/a2 refreshed --
    # the route's user/gmail/not-paused predicates already scope the SQL, so
    # a3 simply never comes back from the (mocked) query.
    client, db = fanout
    account_a1, account_a2 = _account(), _account()
    db.execute.return_value.scalars.return_value.all.return_value = [account_a1, account_a2]
    calls = []

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        calls.append(account_id)
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post(
        f"/api/v1/mail/ingest/gmail?provider_account_ids={account_a1.id}&provider_account_ids={account_a2.id}"
    )
    assert resp.status_code == 202
    assert calls == [account_a1.id, account_a2.id]

    statement = db.execute.call_args[0][0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    # literal_binds renders each UUID bind value without hyphens.
    assert (
        f"provider_account.id IN ('{account_a1.id.hex}', '{account_a2.id.hex}')"
        in compiled
    )


def test_ingest_provider_account_ids_still_skips_paused_accounts(fanout, monkeypatch):
    # A paused account named explicitly in provider_account_ids stays out of
    # the eligible set -- the SQL predicate excludes it regardless of what the
    # caller asked for.
    client, db = fanout
    paused = _account(paused=True)
    # The stub DB can't actually filter, so mirror what the real WHERE clause
    # would return: nothing, since sync_paused_at IS NULL excludes `paused`.
    db.execute.return_value.scalars.return_value.all.return_value = []
    calls = []

    def fake_start_sync_run(db_arg, user_id, account_id, *, mode, options):
        calls.append(account_id)
        return _run(account_id, mode), False

    monkeypatch.setattr(mailbox_routes, "start_sync_run", fake_start_sync_run)

    resp = client.post(f"/api/v1/mail/ingest/gmail?provider_account_ids={paused.id}")
    assert resp.status_code == 202
    assert resp.json() == {"runs": []}
    assert calls == []


def test_ingest_unknown_provider_account_id_returns_empty_runs(fanout):
    # An id that isn't owned by (or doesn't belong to) this user never
    # matches the user-scoped select, so it's silently dropped rather than
    # erroring.
    client, db = fanout
    db.execute.return_value.scalars.return_value.all.return_value = []
    resp = client.post(f"/api/v1/mail/ingest/gmail?provider_account_ids={uuid4()}")
    assert resp.status_code == 202
    assert resp.json() == {"runs": []}


def test_large_backfill_queues_task_and_returns_202(client, fake_backfill_delay):
    resp = client.post(
        "/api/v1/mail/classify/backfill"
        "?limit=51&force=true&bucket=all&backend=heuristic"
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_id": "task-456"}
    fake_backfill_delay.assert_called_once_with(
        user_id=str(USER_ID),
        limit=51,
        force=True,
        bucket="all",
        backend="heuristic",
    )


def test_small_backfill_runs_inline(client, fake_backfill_delay):
    # At or under the inline cap nothing is enqueued; the route classifies in
    # the request and reports counts (zero against the empty-result DB stub).
    resp = client.post("/api/v1/mail/classify/backfill?limit=50")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "created": 0, "scanned": 0}
    fake_backfill_delay.assert_not_called()


def test_large_backfill_still_validates_before_queueing(client, fake_backfill_delay):
    # Bad params are rejected up front even when the size would queue, so the
    # worker never sees an invalid bucket/backend.
    assert (
        client.post(
            "/api/v1/mail/classify/backfill?limit=400&bucket=not_a_bucket"
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/mail/classify/backfill?limit=400&backend=not_a_model"
        ).status_code
        == 422
    )
    fake_backfill_delay.assert_not_called()
