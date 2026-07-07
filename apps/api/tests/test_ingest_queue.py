"""Work-queueing routes: Gmail ingest always enqueues a Celery task and comes
back 202 with a task id, and classification backfills do the same once the
batch is too big to run inline.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.workers import tasks_ingest, tasks_nlp


USER_ID = uuid4()


@pytest.fixture
def client():
    user = MagicMock(id=USER_ID)
    # Same empty-result DB stub as test_validation: the inline backfill path
    # reads eligible threads, and against this stub it finds none.
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fake_delay(monkeypatch):
    # Stub out the broker call -- these tests run offline, so we only care
    # that the route hands the task the right kwargs.
    delay = MagicMock(return_value=MagicMock(id="task-123"))
    monkeypatch.setattr(tasks_ingest.ingest_gmail_for_user, "delay", delay)
    return delay


@pytest.fixture
def fake_backfill_delay(monkeypatch):
    delay = MagicMock(return_value=MagicMock(id="task-456"))
    monkeypatch.setattr(tasks_nlp.backfill_threads_for_user, "delay", delay)
    return delay


def test_ingest_requires_auth():
    resp = TestClient(app).post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 401


def test_ingest_queues_task_and_returns_202(client, fake_delay):
    resp = client.post(
        "/api/v1/mail/ingest/gmail?max_results=50&skip_existing=false&classify=false"
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_id": "task-123"}
    fake_delay.assert_called_once_with(
        user_id=str(USER_ID),
        max_results=50,
        skip_existing=False,
        classify_messages=False,
    )


def test_ingest_defaults_pass_through(client, fake_delay):
    resp = client.post("/api/v1/mail/ingest/gmail")
    assert resp.status_code == 202
    fake_delay.assert_called_once_with(
        user_id=str(USER_ID),
        max_results=25,
        skip_existing=True,
        classify_messages=True,
    )


def test_ingest_still_validates_max_results(client, fake_delay):
    assert client.post("/api/v1/mail/ingest/gmail?max_results=0").status_code == 422
    assert client.post("/api/v1/mail/ingest/gmail?max_results=10000").status_code == 422
    fake_delay.assert_not_called()


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
