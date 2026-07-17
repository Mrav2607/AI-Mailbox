from unittest.mock import MagicMock
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.ingest import gmail_ingest
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


def test_new_only_with_no_baseline_at_all_returns_without_pulling():
    # No known threads AND no cursor means no baseline to define "new" against —
    # the pull must bail out before touching Gmail instead of importing the
    # whole account.
    provider = MagicMock(access_token="tok", token_expiry=None, gmail_history_id=None)
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = []

    result = ingest_gmail_messages(db, "u1", new_only=True)

    assert result["fetched"] == 0
    assert result["new_threads"] == 0
    assert result["threads_upserted"] == 0
    db.commit.assert_not_called()


def test_new_only_with_a_cursor_but_no_threads_still_syncs(monkeypatch):
    # An account whose first ingest found an empty mailbox has no threads but
    # DOES have a cursor. Treating "no threads" as "no baseline" would strand it
    # forever: no threads means no pull, and no pull means no first thread.
    provider = MagicMock(
        access_token="tok", token_expiry=None, gmail_history_id="4242"
    )
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = []

    client = MagicMock()
    client.list_history.return_value = {
        "historyId": "4300",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "first-ever"}}]}
        ],
    }
    client.get_thread.return_value = {"messages": []}
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)

    result = ingest_gmail_messages(db, "u1", new_only=True)

    client.list_history.assert_called_once()
    assert result["fetched"] == 1
    assert provider.gmail_history_id == "4300"


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


def _deleted_thread_setup(monkeypatch, status_code):
    """A mailbox whose history names one thread that Gmail no longer has."""
    provider = MagicMock(
        access_token="tok", token_expiry=None, gmail_history_id="100"
    )
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    db.execute.return_value.scalars.return_value.all.return_value = ["known-thread"]

    client = MagicMock()
    client.list_history.return_value = {
        "historyId": "110",
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "gone-thread"}}]}
        ],
    }
    response = MagicMock(status_code=status_code)
    client.get_thread.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=response
    )
    monkeypatch.setattr(gmail_ingest, "GmailClient", lambda _token: client)
    return db, provider


def test_thread_deleted_after_history_named_it_is_skipped(monkeypatch):
    db, provider = _deleted_thread_setup(monkeypatch, 404)

    result = ingest_gmail_messages(db, "u1", new_only=True)

    assert result["threads_missing"] == 1
    assert result["threads_upserted"] == 0
    assert result["new_threads"] == 0
    assert provider.gmail_history_id == "110"


def test_a_thread_fetch_failing_for_other_reasons_still_raises(monkeypatch):
    db, provider = _deleted_thread_setup(monkeypatch, 500)

    with pytest.raises(httpx.HTTPStatusError):
        ingest_gmail_messages(db, "u1", new_only=True)

    assert provider.gmail_history_id == "100"


def _token_response(status_code, payload=None, text="err"):
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = payload if payload is not None else {}
    if payload is None:
        resp.json.side_effect = ValueError("not json")
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        text, request=MagicMock(), response=resp
    )
    return resp


def test_revoked_refresh_token_pauses_the_account_and_does_not_retry(monkeypatch):
    # invalid_grant is permanent: only the user reconnecting fixes it. Raising
    # ValueError puts it on the task's terminal branch, so we stop hammering
    # Google every cycle forever.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid, reason=reason)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx,
        "post",
        lambda *a, **k: _token_response(400, {"error": "invalid_grant"}),
    )

    with pytest.raises(ValueError, match="revoked"):
        gmail_ingest._refresh_access_token(provider)

    assert paused["id"] == provider.id
    assert paused["reason"] == "reauth_required"


def test_a_transient_token_failure_still_raises_for_retry(monkeypatch):
    # 500 is Google having a bad day -- retryable. Pausing here would take a
    # working account out of the schedule until someone noticed.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx, "post", lambda *a, **k: _token_response(500, {})
    )

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}


def test_a_401_from_the_token_endpoint_is_our_problem_not_the_users(monkeypatch):
    # 401 means invalid_client -- our credentials are misconfigured. Pausing the
    # user's account would blame them for an operator error.
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(
        gmail_ingest.httpx,
        "post",
        lambda *a, **k: _token_response(401, {"error": "invalid_client"}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}


def test_a_non_json_400_is_not_mistaken_for_a_revoked_token(monkeypatch):
    provider = MagicMock(id=uuid4(), refresh_token="rt")
    paused = {}
    monkeypatch.setattr(
        gmail_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )
    monkeypatch.setattr(gmail_ingest.httpx, "post", lambda *a, **k: _token_response(400))

    with pytest.raises(httpx.HTTPStatusError):
        gmail_ingest._refresh_access_token(provider)

    assert paused == {}
