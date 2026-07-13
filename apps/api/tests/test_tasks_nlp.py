from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.nlp import backfill
from app.workers import tasks_nlp


def test_classify_latest_threads_uses_created_at_when_sent_at_is_null(monkeypatch):
    user_id = uuid4()
    thread_id = uuid4()
    older_message = SimpleNamespace(
        id=uuid4(),
        thread_id=thread_id,
        sent_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        snippet="older",
        body_text="older body",
    )
    newer_message = SimpleNamespace(
        id=uuid4(),
        thread_id=thread_id,
        sent_at=None,
        created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        snippet="newer",
        body_text="newer body",
    )
    thread = SimpleNamespace(id=thread_id, subject="Subject")

    db = MagicMock()
    thread_result = MagicMock()
    thread_result.scalars.return_value.all.return_value = [thread]
    message_result = MagicMock()
    message_result.scalars.return_value.all.return_value = [older_message, newer_message]
    classification_result = MagicMock()
    classification_result.scalars.return_value = []
    db.execute.side_effect = [thread_result, message_result, classification_result]

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(db))
    classify = MagicMock(return_value=("personal", 0.9, "reason", "test-model"))
    monkeypatch.setattr(backfill, "classify", classify)
    upsert = MagicMock()
    monkeypatch.setattr(backfill, "upsert_classification", upsert)

    result = tasks_nlp.classify_latest_threads.run(str(user_id), limit=25, force=False)

    assert result == {
        "status": "ok",
        "user_id": str(user_id),
        "created": 1,
        "processed": 1,
    }
    classify.assert_called_once()
    assert "newer body" in classify.call_args.args[0]
    upsert.assert_called_once()
    assert upsert.call_args.kwargs["message_id"] == newer_message.id
