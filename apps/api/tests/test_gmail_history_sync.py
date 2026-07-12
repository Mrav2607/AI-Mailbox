from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

from app.services.ingest.gmail_ingest import (
    _collect_history_thread_ids,
    _count_new_threads,
    _merge_sync_thread_ids,
    _should_reopen_thread,
    ingest_gmail_messages,
)


def test_history_sync_includes_known_threads_with_new_messages():
    client = MagicMock()
    client.list_history.side_effect = [
        {
            "historyId": "105",
            "nextPageToken": "page-2",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "m1", "threadId": "known-thread"}},
                        {"message": {"id": "m2", "threadId": "new-thread"}},
                    ]
                }
            ],
        },
        {
            "historyId": "110",
            "history": [
                {
                    "messagesAdded": [
                        # A second message in the same changed conversation
                        # must not make us fetch the thread twice.
                        {"message": {"id": "m3", "threadId": "known-thread"}}
                    ]
                }
            ],
        },
    ]

    changed, cursor = _collect_history_thread_ids(client, "100")

    assert changed == ["known-thread", "new-thread"]
    assert cursor == "110"
    assert client.list_history.call_args_list == [
        (("100",), {"page_token": None}),
        (("100",), {"page_token": "page-2"}),
    ]


def test_changed_threads_take_priority_and_are_deduplicated():
    assert _merge_sync_thread_ids(
        ["known-thread", "new-thread"],
        ["known-thread", "recent-thread"],
        ["new-thread", "old-thread"],
    ) == ["known-thread", "new-thread", "recent-thread", "old-thread"]


def test_new_thread_count_excludes_replies_to_known_threads():
    assert (
        _count_new_threads(
            ["known-thread", "new-thread"],
            ["new-thread", "old-backfill"],
            1,
            {"known-thread"},
        )
        == 1
    )


def test_new_only_selection_drops_the_backfill_tail():
    # listed_ids beyond head_new are older history the DB hasn't backfilled;
    # a new-only sync must never fetch them.
    listed = ["new-a", "new-b", "old-1", "old-2"]
    head_new = 2
    assert _merge_sync_thread_ids(["changed"], [], listed[:head_new]) == [
        "changed",
        "new-a",
        "new-b",
    ]


def test_new_only_on_empty_mailbox_returns_without_pulling():
    # No known threads means no baseline to define "new" against — the pull
    # must bail out before touching Gmail instead of importing the account.
    provider = MagicMock(access_token="tok", token_expiry=None)
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = ingest_gmail_messages(db, "u1", new_only=True)

    assert result["fetched"] == 0
    assert result["new_threads"] == 0
    assert result["threads_upserted"] == 0
    db.commit.assert_not_called()


def test_new_inbound_reply_reopens_done_thread_but_sent_or_existing_mail_does_not():
    done_at = datetime.now(timezone.utc)
    inbound = {
        "provider_message_id": "new-inbound",
        "sent_at": done_at + timedelta(seconds=1),
        "label_ids": ["INBOX"],
    }
    sent = {
        "provider_message_id": "new-sent",
        "sent_at": done_at + timedelta(seconds=2),
        "label_ids": ["SENT"],
    }
    assert _should_reopen_thread(done_at, {"old"}, [inbound]) is True
    assert _should_reopen_thread(done_at, {"old"}, [sent]) is False
    assert _should_reopen_thread(done_at, {"new-inbound"}, [inbound]) is False
