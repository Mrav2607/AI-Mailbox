"""Tests for app/services/mail_send/*: recipient computation, the
reply_attempt CAS lifecycle, MIME assembly, and both providers' send
mechanics. Offline only -- httpx is replaced by MagicMock clients (mirroring
test_outlook_client.py's style) and the DB is a tiny FakeDB that records
statements and answers canned rowcounts (mirroring test_extraction_run.py's
_FakeDB/_FakeResult).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services.ingest import gmail_client, outlook_client
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.outlook_client import OutlookClient
from app.services.mail_send import common, gmail_send, outlook_send


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rowcount=0, scalar=None):
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def first(self):
        return self._scalar


class _FakeDB:
    """Records every statement handed to execute() and answers with the
    next canned _FakeResult."""

    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        if self.results:
            return self.results.pop(0)
        return _FakeResult()

    def add(self, obj):
        pass

    def flush(self):
        pass

    def commit(self):
        pass


def _compiled_sql(stmt, literal_binds=False):
    kwargs = {"compile_kwargs": {"literal_binds": True}} if literal_binds else {}
    return str(stmt.compile(dialect=postgresql.dialect(), **kwargs))


def _msg(
    *,
    sender="alice@example.com",
    headers=None,
    sent_at=None,
    created_at=None,
):
    return SimpleNamespace(
        sender=sender,
        headers=headers or {},
        sent_at=sent_at,
        created_at=created_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Recipients (plan §3.3)
# ---------------------------------------------------------------------------


def test_resolve_self_address_gmail_uses_external_user_id():
    account = SimpleNamespace(provider="gmail", external_user_id="me@gmail.com", display_email=None)
    assert common.resolve_self_address(account) == "me@gmail.com"


def test_resolve_self_address_outlook_uses_display_email():
    account = SimpleNamespace(provider="outlook", external_user_id="tid:oid", display_email="me@outlook.com")
    assert common.resolve_self_address(account) == "me@outlook.com"


def test_resolve_self_address_missing_fails_closed():
    account = SimpleNamespace(provider="outlook", external_user_id="tid:oid", display_email=None)
    with pytest.raises(common.SendError) as exc:
        common.resolve_self_address(account)
    assert exc.value.code == "account_identity_unavailable"


def test_resolve_self_address_invalid_fails_closed():
    account = SimpleNamespace(provider="gmail", external_user_id="not-an-email", display_email=None)
    with pytest.raises(common.SendError) as exc:
        common.resolve_self_address(account)
    assert exc.value.code == "account_identity_unavailable"


def test_select_reply_target_walks_newest_first_skipping_self():
    self_address = "me@example.com"
    old_from_other = _msg(sender="bob@example.com", sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newest_from_self = _msg(sender="Me <me@example.com>", sent_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    newest_from_other = _msg(sender="carol@example.com", sent_at=datetime(2026, 1, 2, tzinfo=timezone.utc))

    target = common.select_reply_target(
        [old_from_other, newest_from_self, newest_from_other], self_address
    )
    assert target is newest_from_other


def test_select_reply_target_all_self_thread_fails_closed():
    self_address = "me@example.com"
    only_self = _msg(sender="me@example.com")
    with pytest.raises(common.SendError) as exc:
        common.select_reply_target([only_self], self_address)
    assert exc.value.code == "no_reply_target"


def test_compute_recipients_honors_reply_to_over_sender():
    target = _msg(
        sender="noreply@example.com",
        headers={"Reply-To": "Support <support@example.com>"},
    )
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["support@example.com"]
    assert recipients.cc == []


def test_compute_recipients_falls_back_to_sender_without_reply_to():
    target = _msg(sender="bob@example.com")
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["bob@example.com"]


def test_compute_recipients_header_lookup_is_case_insensitive():
    target = _msg(sender="bob@example.com", headers={"reply-to": "carol@example.com"})
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["carol@example.com"]


def test_compute_recipients_reply_all_folds_in_original_to_and_cc_minus_self():
    target = _msg(
        sender="bob@example.com",
        headers={
            "To": "me@example.com, dave@example.com",
            "Cc": "Eve <eve@example.com>",
        },
    )
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=True
    )
    assert recipients.to == ["bob@example.com"]
    assert recipients.cc == ["dave@example.com", "eve@example.com"]


def test_compute_recipients_quoted_display_name_with_comma_is_parsed_as_one_address():
    # "Doe, Jane" <jane@example.com> must not split into two addresses on
    # the display name's internal comma.
    target = _msg(sender='"Doe, Jane" <jane@example.com>')
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["jane@example.com"]


def test_compute_recipients_multi_mailbox_reply_to_all_honored():
    target = _msg(
        sender="bob@example.com",
        headers={"Reply-To": "a@example.com, b@example.com"},
    )
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["a@example.com", "b@example.com"]


def test_compute_recipients_discards_invalid_addresses():
    target = _msg(
        sender="bob@example.com",
        headers={"Reply-To": "not-an-address, valid@example.com"},
    )
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["valid@example.com"]


def test_compute_recipients_crlf_in_header_value_is_dropped_not_injected():
    target = _msg(
        sender="bob@example.com",
        headers={"Reply-To": "attacker@example.com\r\nBcc: evil@example.com"},
    )
    # The whole Reply-To value is dropped for containing CR/LF, so the
    # fallback (sender) is used instead -- never a value with the injected
    # header surviving into getaddresses.
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=False
    )
    assert recipients.to == ["bob@example.com"]


def test_compute_recipients_bcc_never_copied():
    target = _msg(
        sender="bob@example.com",
        headers={"To": "me@example.com", "Cc": "", "Bcc": "secret@example.com"},
    )
    recipients = common.compute_recipients(
        target_message=target, self_address="me@example.com", reply_all=True
    )
    assert "secret@example.com" not in recipients.to
    assert "secret@example.com" not in recipients.cc


def test_compute_recipients_self_only_target_fails_closed():
    target = _msg(sender="me@example.com")
    with pytest.raises(common.SendError) as exc:
        common.compute_recipients(target_message=target, self_address="me@example.com", reply_all=False)
    assert exc.value.code == "no_reply_target"


def test_compute_recipients_cap_exceeded_raises_too_many_recipients():
    cc_addrs = ", ".join(f"user{i}@example.com" for i in range(60))
    target = _msg(sender="bob@example.com", headers={"To": "me@example.com", "Cc": cc_addrs})
    with pytest.raises(common.SendError) as exc:
        common.compute_recipients(target_message=target, self_address="me@example.com", reply_all=True)
    assert exc.value.code == "too_many_recipients"


def test_compute_recipients_exactly_at_cap_succeeds():
    # 1 in To + 49 in Cc == 50, the boundary itself must NOT raise.
    cc_addrs = ", ".join(f"user{i}@example.com" for i in range(48))
    target = _msg(
        sender="bob@example.com",
        headers={"To": "me@example.com, target2@example.com", "Cc": cc_addrs},
    )
    recipients = common.compute_recipients(target_message=target, self_address="me@example.com", reply_all=True)
    assert len(recipients.to) + len(recipients.cc) == 50


# ---------------------------------------------------------------------------
# Message-ID + Gmail MIME assembly (plan §3.1)
# ---------------------------------------------------------------------------


def test_generate_message_id_is_unique_and_well_formed():
    a = common.generate_message_id()
    b = common.generate_message_id()
    assert a != b
    assert a.startswith("<") and a.endswith(">")
    assert "@" in a


def test_threading_headers_derives_in_reply_to_and_appends_references():
    target = _msg(
        sender="bob@example.com",
        headers={"Message-ID": "<orig@example.com>", "References": "<root@example.com>"},
    )
    in_reply_to, references = common.threading_headers(target)
    assert in_reply_to == "<orig@example.com>"
    assert references == "<root@example.com> <orig@example.com>"


def test_threading_headers_no_prior_references_falls_back_to_just_in_reply_to():
    target = _msg(sender="bob@example.com", headers={"Message-ID": "<orig@example.com>"})
    in_reply_to, references = common.threading_headers(target)
    assert in_reply_to == "<orig@example.com>"
    assert references == "<orig@example.com>"


def test_build_reply_mime_sets_every_expected_header():
    recipients = common.RecipientSet(to=["bob@example.com"], cc=["carol@example.com"])
    raw = common.build_reply_mime(
        self_address="me@example.com",
        recipients=recipients,
        subject="Quarterly update",
        body_text="Sounds good.",
        in_reply_to="<orig@example.com>",
        references="<root@example.com> <orig@example.com>",
        message_id="<new@cortexmail.app>",
    )
    import base64
    from email import policy
    from email.parser import BytesParser

    decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    msg = BytesParser(policy=policy.default).parsebytes(decoded)
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "bob@example.com"
    assert msg["Cc"] == "carol@example.com"
    assert msg["Subject"] == "Re: Quarterly update"
    assert msg["Message-ID"] == "<new@cortexmail.app>"
    assert msg["In-Reply-To"] == "<orig@example.com>"
    assert msg["References"] == "<root@example.com> <orig@example.com>"
    assert msg.get_content().strip() == "Sounds good."


def test_build_reply_mime_subject_prefix_is_idempotent():
    recipients = common.RecipientSet(to=["bob@example.com"], cc=[])
    raw = common.build_reply_mime(
        self_address="me@example.com",
        recipients=recipients,
        subject="Re: Already prefixed",
        body_text="Hi",
        in_reply_to=None,
        references=None,
        message_id="<x@cortexmail.app>",
    )
    import base64
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw.encode("ascii")))
    assert msg["Subject"] == "Re: Already prefixed"


# ---------------------------------------------------------------------------
# reply_attempt lifecycle -- every CAS transition ordering the plan requires.
# ---------------------------------------------------------------------------


def test_transition_to_inflight_cas_succeeds_from_preparing():
    db = _FakeDB([_FakeResult(rowcount=1)])
    assert common.transition_to_inflight(db, attempt_id=uuid4()) is True
    sql = _compiled_sql(db.statements[0], literal_binds=True)
    assert "status='inflight'" in sql
    assert "status = 'preparing'" in sql


def test_transition_to_inflight_cas_aborts_when_already_expired():
    # The expiry-wins ordering: a prior expire already flipped the row, so
    # the CAS affects zero rows.
    db = _FakeDB([_FakeResult(rowcount=0)])
    assert common.transition_to_inflight(db, attempt_id=uuid4()) is False


def test_expire_stale_preparing_then_inflight_cas_aborts_expiry_wins():
    """Required ordering #1: expiry commits first, so the inflight CAS the
    original request tries next sees zero rows."""
    attempt_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1), _FakeResult(rowcount=0)])
    expired = common.expire_stale_preparing(db, thread_id=uuid4())
    assert expired == 1
    assert common.transition_to_inflight(db, attempt_id=attempt_id) is False


def test_inflight_cas_wins_then_expiry_finds_nothing_to_expire():
    """Required ordering #2: the original's inflight CAS committed first, so
    expire_stale_preparing (now re-run by the blocked new request per P6-2)
    affects zero rows -- the row is inflight, not preparing anymore."""
    attempt_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1), _FakeResult(rowcount=0)])
    assert common.transition_to_inflight(db, attempt_id=attempt_id) is True
    expired = common.expire_stale_preparing(db, thread_id=uuid4())
    assert expired == 0


def test_mark_sent_widened_cas_wins_over_abandoned():
    db = _FakeDB([_FakeResult(rowcount=1)])
    assert common.mark_sent(db, attempt_id=uuid4(), provider_message_id="m1") is True
    sql = _compiled_sql(db.statements[0], literal_binds=True)
    assert "'inflight'" in sql and "'abandoned'" in sql


def test_mark_unknown_does_not_widen_to_abandoned_required_ordering():
    """Required test: abandon-vs-unknown -- an attempt already abandoned by
    a user override must NOT be flipped back to 'unknown' by a concurrent
    ambiguous outcome."""
    attempt_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1), _FakeResult(rowcount=0)])
    assert common.abandon_attempt(db, attempt_id=attempt_id) is True
    assert common.mark_unknown(db, attempt_id=attempt_id) is False
    sql = _compiled_sql(db.statements[1], literal_binds=True).lower()
    assert "'abandoned'" not in sql.split("where", 1)[1]


def test_abandon_vs_success_required_ordering():
    """Required test: abandon-vs-success -- SUCCESS still lands via the
    widened CAS even though the row was abandoned first."""
    attempt_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1), _FakeResult(rowcount=1)])
    assert common.abandon_attempt(db, attempt_id=attempt_id) is True
    assert common.mark_sent(db, attempt_id=attempt_id, provider_message_id="m1") is True


def test_abandon_vs_failure_required_ordering():
    """Required test: abandon-vs-failure -- a definite rejection still lands
    via the widened CAS (P7-5): a provably-unsent attempt must not linger
    abandoned and reconciliation-eligible forever."""
    attempt_id = uuid4()
    db = _FakeDB([_FakeResult(rowcount=1), _FakeResult(rowcount=1)])
    assert common.abandon_attempt(db, attempt_id=attempt_id) is True
    assert common.mark_failed(db, attempt_id=attempt_id) is True


def test_mark_failed_widened_cas_shape():
    db = _FakeDB([_FakeResult(rowcount=1)])
    assert common.mark_failed(db, attempt_id=uuid4()) is True
    sql = _compiled_sql(db.statements[0], literal_binds=True)
    assert "'failed'" in sql
    assert "'inflight'" in sql and "'abandoned'" in sql


def test_abandon_attempt_cannot_touch_a_terminal_row():
    db = _FakeDB([_FakeResult(rowcount=0)])
    assert common.abandon_attempt(db, attempt_id=uuid4()) is False


def test_find_blocking_attempt_query_shape_excludes_terminal_statuses():
    db = _FakeDB([_FakeResult(scalar=None)])
    common.find_blocking_attempt(db, thread_id=uuid4())
    sql = _compiled_sql(db.statements[0], literal_binds=True)
    for status in ("'inflight'", "'unknown'", "'preparing'"):
        assert status in sql
    for terminal in ("'sent'", "'failed'", "'expired'"):
        # Terminal statuses never appear as a bare status match in the
        # blocker predicate.
        assert terminal not in sql


def test_start_attempt_inserts_preparing_row_with_message_id():
    db = _FakeDB()
    attempt = common.start_attempt(
        db,
        thread_id=uuid4(),
        user_id=uuid4(),
        provider="gmail",
        gmail_message_id_header="<x@cortexmail.app>",
    )
    assert attempt.status == "preparing"
    assert attempt.gmail_message_id_header == "<x@cortexmail.app>"


# ---------------------------------------------------------------------------
# Fence + action resolution (shared helper, plan §3.5)
# ---------------------------------------------------------------------------


def test_advance_fence_and_resolve_shape_and_rowcount():
    db = _FakeDB([_FakeResult(rowcount=0), _FakeResult(rowcount=2)])
    resolved = common.advance_fence_and_resolve(
        db, thread_id=uuid4(), attempt_created_at=datetime.now(timezone.utc)
    )
    assert resolved == 2
    thread_sql = _compiled_sql(db.statements[0], literal_binds=False).lower()
    assert "greatest" in thread_sql
    action_sql = _compiled_sql(db.statements[1], literal_binds=True).lower()
    assert "'done'" in action_sql
    assert "'open'" in action_sql
    assert "kind" in action_sql


# ---------------------------------------------------------------------------
# Gmail send (client + module)
# ---------------------------------------------------------------------------


def _resp(status_code=200, payload=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.side_effect = None
    return resp


def test_gmail_send_message_posts_raw_and_thread_id(monkeypatch):
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(200, {"id": "sent-1", "threadId": "t1"}))
    monkeypatch.setattr(gmail_client, "_client", lambda: fake_client)

    result = GmailClient("tok").send_message(raw="QkFTRTY0", thread_id="t1")

    assert result == {"id": "sent-1", "threadId": "t1"}
    call = fake_client.post.call_args
    assert call.args[0] == "/messages/send"
    assert call.kwargs["json"] == {"raw": "QkFTRTY0", "threadId": "t1"}
    assert call.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_gmail_send_message_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
    throttled = _resp(429)
    ok = _resp(200, {"id": "sent-1"})
    fake_client = MagicMock()
    fake_client.post = MagicMock(side_effect=[throttled, ok])
    monkeypatch.setattr(gmail_client, "_client", lambda: fake_client)

    result = GmailClient("tok").send_message(raw="x", thread_id="t1")

    assert result == {"id": "sent-1"}
    assert fake_client.post.call_count == 2


def test_gmail_send_message_never_retries_a_5xx():
    """No retry loop for sends except the bounded 429 backoff -- a 5xx must
    surface immediately as an ambiguous outcome, never a blind replay."""
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(503))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_client, "_client", lambda: fake_client)
        with pytest.raises(httpx.HTTPStatusError):
            GmailClient("tok").send_message(raw="x", thread_id="t1")
    assert fake_client.post.call_count == 1


def test_gmail_send_message_429_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(429))
    monkeypatch.setattr(gmail_client, "_client", lambda: fake_client)

    with pytest.raises(httpx.HTTPStatusError):
        GmailClient("tok").send_message(raw="x", thread_id="t1")

    assert fake_client.post.call_count == 3


def test_gmail_send_reply_wraps_client(monkeypatch):
    client = MagicMock()
    client.send_message.return_value = {"id": "sent-1"}
    result = gmail_send.send_reply(client, raw="x", provider_thread_id="t1")
    assert result == {"id": "sent-1"}
    client.send_message.assert_called_once_with(raw="x", thread_id="t1")


# ---------------------------------------------------------------------------
# Outlook send (client + module)
# ---------------------------------------------------------------------------


def test_outlook_create_reply_posts_comment_with_immutable_id_header(monkeypatch):
    fake_client = MagicMock()
    fake_client.post = MagicMock(
        return_value=_resp(200, {"id": "draft-1", "toRecipients": [], "ccRecipients": []})
    )
    monkeypatch.setattr(outlook_client, "_client", lambda: fake_client)

    result = OutlookClient("tok").create_reply("msg-1", reply_all=False, comment="hi")

    assert result["id"] == "draft-1"
    call = fake_client.post.call_args
    assert call.args[0].endswith("/me/messages/msg-1/createReply")
    assert call.kwargs["json"] == {"comment": "hi"}
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'


def test_outlook_create_reply_all_uses_createreplyall(monkeypatch):
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(200, {"id": "draft-1"}))
    monkeypatch.setattr(outlook_client, "_client", lambda: fake_client)

    OutlookClient("tok").create_reply("msg-1", reply_all=True, comment="hi")

    call = fake_client.post.call_args
    assert call.args[0].endswith("/me/messages/msg-1/createReplyAll")


def test_outlook_send_draft_posts_with_no_body(monkeypatch):
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(200))
    monkeypatch.setattr(outlook_client, "_client", lambda: fake_client)

    OutlookClient("tok").send_draft("draft-1")

    call = fake_client.post.call_args
    assert call.args[0].endswith("/me/messages/draft-1/send")
    assert call.kwargs["json"] is None
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'


def test_outlook_delete_draft_sends_delete_with_immutable_id_header(monkeypatch):
    fake_client = MagicMock()
    fake_client.request = MagicMock(return_value=_resp(200))
    monkeypatch.setattr(outlook_client, "_client", lambda: fake_client)

    OutlookClient("tok").delete_draft("draft-1")

    call = fake_client.request.call_args
    assert call.args[0] == "DELETE"
    assert call.args[1].endswith("/me/messages/draft-1")
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'


def test_outlook_post_never_retries():
    """Unlike `_get`, `_post` makes exactly one call -- retrying a send or a
    draft mutation risks a double-send/duplicate draft."""
    fake_client = MagicMock()
    fake_client.post = MagicMock(return_value=_resp(503))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(outlook_client, "_client", lambda: fake_client)
        resp = OutlookClient("tok")._post("https://x/y")
    assert fake_client.post.call_count == 1
    assert resp.status_code == 503


def test_outlook_create_reply_draft_under_cap_succeeds(monkeypatch):
    client = MagicMock()
    client.create_reply.return_value = {
        "id": "draft-1",
        "toRecipients": [{"emailAddress": {"address": "a@example.com"}}],
        "ccRecipients": [],
    }
    draft = outlook_send.create_reply_draft(
        client, message_id="msg-1", reply_all=False, comment="hi"
    )
    assert draft["id"] == "draft-1"
    client.delete_draft.assert_not_called()


def test_outlook_create_reply_draft_over_cap_deletes_draft_and_raises():
    client = MagicMock()
    client.create_reply.return_value = {
        "id": "draft-1",
        "toRecipients": [{"emailAddress": {"address": f"u{i}@example.com"}} for i in range(60)],
        "ccRecipients": [],
    }
    with pytest.raises(common.SendError) as exc:
        outlook_send.create_reply_draft(client, message_id="msg-1", reply_all=True, comment="hi")
    assert exc.value.code == "too_many_recipients"
    client.delete_draft.assert_called_once_with("draft-1")


def test_outlook_create_reply_draft_missing_id_raises_send_rejected():
    client = MagicMock()
    client.create_reply.return_value = {"toRecipients": [], "ccRecipients": []}
    with pytest.raises(common.SendError) as exc:
        outlook_send.create_reply_draft(client, message_id="msg-1", reply_all=False, comment="hi")
    assert exc.value.code == "send_rejected"


def test_outlook_build_ephemeral_message_uses_attempt_id_as_synthetic_id():
    attempt_id = uuid4()
    draft = {"bodyPreview": "hi there", "body": {"contentType": "text", "content": "hi there"}}
    message = outlook_send.build_ephemeral_message(
        draft,
        attempt_id=attempt_id,
        self_address="me@example.com",
        to=["bob@example.com"],
        cc=[],
        sent_at=datetime.now(timezone.utc),
    )
    assert message["id"] == str(attempt_id)
    assert message["body_text"] == "hi there"
    assert message["body_html"] is None
