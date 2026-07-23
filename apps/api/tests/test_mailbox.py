from types import SimpleNamespace
from uuid import uuid4

from app.db.models import MailMessage
from app.routes import mailbox


def test_triage_items_include_latest_message_sender_and_account_email(monkeypatch):
    thread_id = uuid4()
    message_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
        provider_account_id=account_id,
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
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class DB:
        def execute(self, statement):
            # The classification lookup queries with no rows to return; the
            # account-email lookup is the one that matters here. Row shape is
            # (id, display_email, external_user_id) -- display_email wins
            # when set.
            if "provider_account" in str(statement):
                return Result([(account_id, None, "owner@gmail.example")])
            return Result([])

    [item] = mailbox._assemble_triage_items(DB(), [thread])

    assert captured["thread_ids"] == [thread_id]
    assert MailMessage.sender in captured["columns"]
    assert item["latest_message_sender"] == '"Ada Lovelace" <ada@example.com>'
    assert item["account_email"] == "owner@gmail.example"


def test_triage_account_email_prefers_display_email_over_external_user_id(monkeypatch):
    # Outlook's external_user_id is a stable tid:oid, not an email -- when a
    # display_email is on file it must win over the identity fallback.
    thread_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Status update",
        last_message_at=None,
        provider_account_id=account_id,
    )
    monkeypatch.setattr(
        mailbox, "latest_messages_by_thread", lambda db, thread_ids, columns: {}
    )

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class DB:
        def execute(self, statement):
            if "provider_account" in str(statement):
                return Result([(account_id, "user@outlook.example", "tid:oid")])
            return Result([])

    [item] = mailbox._assemble_triage_items(DB(), [thread])

    assert item["account_email"] == "user@outlook.example"
