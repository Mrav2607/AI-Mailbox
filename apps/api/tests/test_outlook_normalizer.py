from datetime import datetime, timezone

from app.services.ingest.normalizer import normalize_message, normalize_outlook_message


def _msg(**overrides):
    base = {
        "id": "msg-1",
        "conversationId": "conv-1",
        "subject": "Hello",
        "from": {"emailAddress": {"name": "Jane Doe", "address": "jane@example.com"}},
        "toRecipients": [{"emailAddress": {"name": "Bob", "address": "bob@example.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "cc@example.com"}}],
        "receivedDateTime": "2026-07-20T10:00:00Z",
        "sentDateTime": "2026-07-20T09:59:00Z",
        "bodyPreview": "snip",
        "body": {"contentType": "html", "content": "<p>hi</p>"},
        "internetMessageId": "<abc@mail>",
    }
    base.update(overrides)
    return base


def test_inbox_folder_gets_inbox_label():
    n = normalize_outlook_message(_msg(), "inbox")
    assert n["label_ids"] == ["INBOX"]


def test_sentitems_folder_gets_sent_label():
    n = normalize_outlook_message(_msg(), "sentitems")
    assert n["label_ids"] == ["SENT"]


def test_thread_key_is_conversation_id():
    n = normalize_outlook_message(_msg(), "inbox")
    assert n["provider_thread_id"] == "conv-1"


def test_sent_at_prefers_sent_date_time():
    n = normalize_outlook_message(_msg(), "inbox")
    assert n["sent_at"] == datetime(2026, 7, 20, 9, 59, tzinfo=timezone.utc)


def test_sent_at_falls_back_to_received_date_time_when_sent_missing():
    n = normalize_outlook_message(_msg(sentDateTime=None), "inbox")
    assert n["sent_at"] == datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def test_sent_at_is_none_when_both_timestamps_missing():
    n = normalize_outlook_message(_msg(sentDateTime=None, receivedDateTime=None), "inbox")
    assert n["sent_at"] is None


def test_basic_fields_and_recipients():
    n = normalize_outlook_message(_msg(), "inbox")
    assert n["provider_message_id"] == "msg-1"
    assert n["subject"] == "Hello"
    assert n["snippet"] == "snip"
    assert n["sender"] == "Jane Doe <jane@example.com>"
    assert n["recipient"] == ["Bob <bob@example.com>"]
    assert n["cc"] == ["cc@example.com"]
    assert n["bcc"] is None


def test_html_body_yields_plain_text_fallback():
    n = normalize_outlook_message(_msg(), "inbox")
    assert n["body_html"] == "<p>hi</p>"
    assert n["body_text"] == "hi"


def test_plain_text_body_has_no_html():
    n = normalize_outlook_message(
        _msg(body={"contentType": "text", "content": "plain body"}), "inbox"
    )
    assert n["body_text"] == "plain body"
    assert n["body_html"] is None


def test_missing_sender_and_recipients_are_none():
    n = normalize_outlook_message(
        _msg(**{"from": None, "toRecipients": [], "ccRecipients": None}), "inbox"
    )
    assert n["sender"] is None
    assert n["recipient"] is None
    assert n["cc"] is None


def test_shape_parity_with_gmail_normalizer_keys():
    gmail_raw = {
        "id": "g1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "snippet": "s",
        "internalDate": "1751900000000",
        "payload": {"headers": []},
    }
    gmail_shape = set(normalize_message(gmail_raw).keys())
    outlook_shape = set(normalize_outlook_message(_msg(), "inbox").keys())
    assert gmail_shape == outlook_shape
