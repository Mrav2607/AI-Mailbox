from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routes import mailbox


@pytest.fixture
def task_status_context(monkeypatch):
    user = MagicMock(id=uuid4())
    db = MagicMock()

    result = MagicMock()
    monkeypatch.setattr(mailbox.celery_app, "AsyncResult", lambda _task_id: result)
    return user, db, result


def test_failed_task_hides_worker_exception(task_status_context):
    user, db, result = task_status_context
    db.scalar.return_value = MagicMock(user_id=user.id)
    result.ready.return_value = True
    result.successful.return_value = False
    result.state = "FAILURE"
    result.result = RuntimeError("mail body leaked through a bound SQL parameter")

    payload = mailbox.get_task_status("task-failed", user, db)

    assert payload["error"] == "task failed"
    assert len(payload["error_id"]) == 32
    assert "mail body" not in repr(payload)


def test_failed_sync_task_is_hidden_from_another_user(task_status_context):
    user, db, result = task_status_context
    db.scalar.return_value = MagicMock(user_id=uuid4())
    result.ready.return_value = True
    result.successful.return_value = False
    result.state = "FAILURE"
    result.result = RuntimeError("private worker detail")

    with pytest.raises(HTTPException) as exc_info:
        mailbox.get_task_status("someone-elses-task", user, db)

    assert exc_info.value.status_code == 404
    assert "private worker detail" not in str(exc_info.value.detail)
