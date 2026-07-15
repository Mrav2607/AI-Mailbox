from types import SimpleNamespace
from uuid import uuid4

from app.db.models import MailMessage
from app.routes import mailbox


def test_triage_items_include_latest_message_sender(monkeypatch):
    thread_id = uuid4()
    message_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
    )
    message = SimpleNamespace(
        id=message_id,
        thread_id=thread_id,
        snippet="The latest details",
        sender='"Ada Lovelace" <ada@example.com>',
    )
    captured = {}

    def latest_messages(db, thread_ids, columns):
        captured["thread_ids"] = thread_ids
        captured["columns"] = columns
        return {thread_id: message}

    monkeypatch.setattr(
        mailbox,
        "latest_messages_by_thread",
        latest_messages,
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class DB:
        def execute(self, statement):
            return Result()

    [item] = mailbox._assemble_triage_items(DB(), [thread])

    assert captured["thread_ids"] == [thread_id]
    assert MailMessage.sender in captured["columns"]
    assert item["latest_message_sender"] == '"Ada Lovelace" <ada@example.com>'
