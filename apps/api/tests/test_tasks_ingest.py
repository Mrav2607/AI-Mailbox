from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.workers import tasks_ingest


def test_ingest_heartbeat_is_throttled(monkeypatch):
    run = SimpleNamespace(
        status="queued",
        heartbeat_at=None,
        lease_expires_at=None,
        started_at=None,
        error=None,
        result=None,
        completed_at=None,
    )
    state_db = MagicMock()
    state_db.get.return_value = run
    ingest_db = MagicMock()
    sessions = iter([state_db, ingest_db, state_db, state_db])
    monkeypatch.setattr(
        tasks_ingest, "SessionLocal", lambda: nullcontext(next(sessions))
    )
    monkeypatch.setattr(
        tasks_ingest, "monotonic", MagicMock(side_effect=[0, 10, 59, 60, 61])
    )

    def fake_ingest(**kwargs):
        for _ in range(4):
            kwargs["progress"]()
        return {"threads_upserted": 4}

    monkeypatch.setattr(tasks_ingest, "ingest_gmail_messages", fake_ingest)

    result = tasks_ingest.ingest_gmail_for_user.run(
        run_id=str(uuid4()), user_id=str(uuid4())
    )

    assert result["status"] == "ok"
    assert result["threads_upserted"] == 4
    assert state_db.commit.call_count == 3
