from contextlib import nullcontext
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.db.models import MailMessage
from app.services.nlp import backfill
from app.workers import tasks_nlp


def _rendered_latest_messages_sql() -> str:
    """Run latest_messages_by_thread against a db that just captures the
    statement, then render it as Postgres would see it."""
    captured = {}

    class FakeResult:
        def all(self):
            return []

    class FakeDB:
        def execute(self, statement):
            captured["statement"] = statement
            return FakeResult()

    backfill.latest_messages_by_thread(
        FakeDB(),
        [uuid4()],
        columns=(MailMessage.id, MailMessage.thread_id),
    )
    return str(
        captured["statement"].compile(dialect=postgresql.dialect())
    ).lower()


def test_latest_message_is_picked_by_coalesced_recency():
    # The whole point of the shared helper: a message with no sent_at falls back
    # to created_at, and it's DISTINCT ON -- not Python -- that does the picking.
    # If either half drifts, a thread's bucket stops matching the message we
    # label, which is the bug this replaced.
    sql = _rendered_latest_messages_sql()

    assert "distinct on (mail_message.thread_id)" in sql
    assert "coalesce(mail_message.sent_at, mail_message.created_at) desc nulls last" in sql
    # thread_id has to lead the ORDER BY or Postgres rejects the DISTINCT ON.
    order_by = sql.split("order by", 1)[1]
    assert order_by.strip().startswith("mail_message.thread_id")


def test_latest_messages_by_thread_skips_the_query_when_there_are_no_threads():
    class ExplodingDB:
        def execute(self, statement):  # pragma: no cover - must never run
            raise AssertionError("no threads means no query")

    assert backfill.latest_messages_by_thread(
        ExplodingDB(), [], columns=(MailMessage.id, MailMessage.thread_id)
    ) == {}


def test_classify_latest_threads_delegates_to_the_shared_backfill(monkeypatch):
    user_id = uuid4()
    captured = {}

    def fake_run_backfill(db, uid, **kwargs):
        captured["user_id"] = uid
        captured.update(kwargs)
        return {
            "status": "ok",
            "created": 3,
            "scanned": 9,
            "task_created": 2,
            "task_processed": 3,
        }

    monkeypatch.setattr(tasks_nlp, "SessionLocal", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks_nlp, "run_backfill", fake_run_backfill)

    result = tasks_nlp.classify_latest_threads.run(str(user_id), limit=25, force=True)

    assert captured["user_id"] == user_id
    assert captured["bucket"] == "all"
    assert captured["force"] is True
    assert captured["limit"] == 25
    # user_id rides along so the task-status endpoint can check ownership.
    assert result == {
        "status": "ok",
        "user_id": str(user_id),
        "created": 2,
        "processed": 3,
    }
