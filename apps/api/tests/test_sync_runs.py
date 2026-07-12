from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from app.db.models import MailSyncRun
from app.services.sync_runs import renew_sync, sync_payload


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
