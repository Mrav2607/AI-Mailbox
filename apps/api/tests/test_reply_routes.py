"""Tests for app/routes/reply.py: the HTTP surface over W1's mail_send
services (docs/plans/2026-08-13-reply-plan.md §3.6/§3.7/§3.8/§4.1).

Offline only. The `reply_attempt` CAS lifecycle, MIME assembly, and the
provider send mechanics are all unit-tested directly in test_mail_send.py --
here they're monkeypatched at their app.routes.reply call sites (mirroring
test_reply_reconcile.py's decomposition), so these tests exercise the
ROUTE's own choreography: which helper gets called in what order, and which
of the frozen §3.8 codes comes back for which condition. `resolve_self_
address`/`select_reply_target`/`compute_recipients` are the one exception --
they're pure, DB-free functions, so the REAL implementations run against
fixture messages, which also proves the route wires them correctly.

The DB is a tiny statement-dispatching fake (the `_ReclassifyDB`/`_FakeDB`
precedent in test_mailbox.py / test_mail_send.py): every raw SQL statement
this route issues itself gets a hand-built response; every write that goes
through a CAS helper is monkeypatched instead, so the fake never needs to
understand that SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.core import ratelimit
from app.db.models import MailMessage, MailThread, ProviderAccount
from app.deps import get_current_user, get_db
from app.main import app
from app.routes import reply as reply_routes
from app.services.mail_send.common import SendError
from app.services.nlp.reply_draft import ReplyDraftAttempt
from app.services.nlp.providers import LlmCredential, ResolvedExtraction

_SELF = "me@example.com"
_OTHER = "alice@example.com"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _account(
    *,
    provider="gmail",
    scope="https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify",
    paused=False,
    has_refresh_token=True,
    external_user_id=_SELF,
    display_email=None,
    # Fresh by default (an hour out) so most tests never touch the refresh
    # path at all -- the expiry/refresh-specific tests override this.
    token_expiry=None,
):
    return SimpleNamespace(
        id=uuid4(),
        provider=provider,
        scope=scope,
        sync_paused_at=datetime.now(timezone.utc) if paused else None,
        refresh_token="rt" if has_refresh_token else None,
        external_user_id=external_user_id,
        display_email=display_email,
        access_token="access-token",
        token_expiry=token_expiry or (datetime.now(timezone.utc) + timedelta(hours=1)),
    )


def _thread(*, user_id, account_id, provider="gmail", replied_at=None, subject="Hello"):
    return SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        provider_account_id=account_id,
        provider=provider,
        provider_thread_id="provider-thread-1",
        subject=subject,
        replied_at=replied_at,
    )


def _message(*, sender=_OTHER, headers=None, provider_message_id="m-1", minutes_ago=5):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        sender=sender,
        headers=headers or {},
        sent_at=now - timedelta(minutes=minutes_ago),
        created_at=now - timedelta(minutes=minutes_ago),
        provider_message_id=provider_message_id,
    )


class _FakeResult:
    def __init__(self, value=None, rows=None, rowcount=0):
        self._value = value
        self._rows = list(rows) if rows is not None else ([value] if value is not None else [])
        self.rowcount = rowcount

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _ReplyDB:
    """Answers every raw statement app/routes/reply.py itself issues.
    Every `reply_attempt`/provider write goes through a monkeypatched
    helper instead, so this never needs to understand that SQL (mirrors
    test_reply_reconcile.py's `_FakeDB` docstring).
    """

    # Sentinel distinguishing "no override given" from "override to None" --
    # the F1 regression test needs to set the locked read to a value
    # different from `thread.replied_at` (including None).
    _NO_OVERRIDE = object()

    def __init__(
        self,
        *,
        thread,
        account,
        messages,
        attempt_status="inflight",
        already_sent_message=None,
        fail_thread_lock=False,
        locked_replied_at=_NO_OVERRIDE,
    ):
        self.thread = thread
        self.account = account
        self.messages = list(messages)
        self.attempt_status = attempt_status
        self.already_sent_message = already_sent_message
        self.fail_thread_lock = fail_thread_lock
        # F1: the value Txn A's column-only, identity-map-bypassing fence
        # read returns -- defaults to `thread.replied_at` (the common case
        # where nothing raced it), but a test can point it somewhere else
        # to simulate a fence that committed between the route's earlier
        # `db.get(MailThread, ...)` and this locked re-read.
        self.locked_replied_at = locked_replied_at
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self._message_rows: dict[UUID, SimpleNamespace] = {}
        self.subject_update = None
        self.last_message_at_update = None

    def get(self, model, pk):
        if model is MailThread and pk == self.thread.id:
            return self.thread
        if model is ProviderAccount and pk == self.account.id:
            return self.account
        if model is MailMessage:
            return self._message_rows.get(pk)
        return None

    def execute(self, stmt):
        self.statements.append(stmt)
        compiled_obj = stmt.compile(dialect=postgresql.dialect())
        compiled = str(compiled_obj)

        if compiled.strip().upper().startswith("SET LOCAL"):
            return _FakeResult()
        if "INSERT INTO mail_message" in compiled:
            params = compiled_obj.params
            new_id = uuid4()
            row = SimpleNamespace(
                id=new_id,
                thread_id=params.get("thread_id"),
                provider_message_id=params.get("provider_message_id"),
                sender=params.get("sender"),
                recipient=params.get("recipient"),
                cc=params.get("cc"),
                bcc=params.get("bcc"),
                sent_at=params.get("sent_at"),
                snippet=params.get("snippet"),
                body_text=params.get("body_text"),
                body_html=params.get("body_html"),
                headers=params.get("headers"),
            )
            self._message_rows[new_id] = row
            return _FakeResult(value=new_id)
        if "UPDATE mail_thread" in compiled:
            params = compiled_obj.params
            self.subject_update = params.get("subject")
            self.last_message_at_update = params.get("last_message_at")
            return _FakeResult(rowcount=1)
        if "reply_attempt.status" in compiled:
            return _FakeResult(value=self.attempt_status)
        if "AND mail_message.provider_message_id" in compiled:
            rows = [self.already_sent_message] if self.already_sent_message else []
            return _FakeResult(value=self.already_sent_message, rows=rows)
        # Txn A's fence read (F1): a column-only SELECT ... FOR UPDATE,
        # distinct from the plain full-row lock below -- checked first
        # since it also contains "FOR UPDATE"/"FROM mail_thread".
        if compiled.startswith("SELECT mail_thread.replied_at") and "FOR UPDATE" in compiled:
            if self.fail_thread_lock:
                raise OperationalError("SELECT ... FOR UPDATE", {}, Exception("lock timeout"))
            value = (
                self.thread.replied_at
                if self.locked_replied_at is self._NO_OVERRIDE
                else self.locked_replied_at
            )
            return _FakeResult(value=value)
        if "FOR UPDATE" in compiled and "FROM mail_thread" in compiled:
            if self.fail_thread_lock:
                raise OperationalError("SELECT ... FOR UPDATE", {}, Exception("lock timeout"))
            return _FakeResult(value=self.thread)
        if compiled.startswith("SELECT mail_thread.replied_at"):
            return _FakeResult(value=self.thread.replied_at)
        if "FROM mail_message" in compiled:
            return _FakeResult(rows=self.messages)
        raise AssertionError(f"_ReplyDB: unhandled statement: {compiled}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _client_for(db, user_id):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=user_id)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _teardown():
    app.dependency_overrides.clear()


def _no_op_classify(monkeypatch):
    monkeypatch.setattr(reply_routes, "_classify_gmail_message_best_effort", lambda *a, **k: None)


def _no_op_reconcile_task(monkeypatch):
    monkeypatch.setattr(reply_routes.reconcile_reply_attempt, "apply_async", MagicMock())


def _basic_send_stubs(monkeypatch, db, *, attempt_id=None, created_at=None):
    """Stub out every reply_attempt CAS write with a happy-path default --
    individual tests override whichever one they care about. Mirrors the
    real fence helper's effect on `mail_thread.replied_at` (GREATEST()) so
    the completion txn's re-read sees an advanced fence, same as Postgres
    would after a real `advance_fence_and_resolve` call."""
    attempt_id = attempt_id or uuid4()
    created_at = created_at or datetime.now(timezone.utc)
    attempt = SimpleNamespace(id=attempt_id, created_at=created_at)

    def _advance_fence(fake_db, *, thread_id, attempt_created_at):
        current = db.thread.replied_at
        db.thread.replied_at = (
            attempt_created_at if current is None else max(current, attempt_created_at)
        )
        return 1

    monkeypatch.setattr(reply_routes, "expire_stale_preparing", lambda *a, **k: 0)
    monkeypatch.setattr(reply_routes, "find_blocking_attempt", lambda *a, **k: None)
    monkeypatch.setattr(reply_routes, "abandon_attempt", lambda *a, **k: True)
    monkeypatch.setattr(reply_routes, "start_attempt", lambda *a, **k: attempt)
    monkeypatch.setattr(reply_routes, "transition_to_inflight", lambda *a, **k: True)
    monkeypatch.setattr(reply_routes, "mark_sent", lambda *a, **k: True)
    monkeypatch.setattr(reply_routes, "mark_failed", lambda *a, **k: True)
    monkeypatch.setattr(reply_routes, "mark_unknown", lambda *a, **k: True)
    monkeypatch.setattr(reply_routes, "advance_fence_and_resolve", _advance_fence)
    monkeypatch.setattr(reply_routes, "record_provider_message_id", lambda *a, **k: None)
    _no_op_reconcile_task(monkeypatch)
    _no_op_classify(monkeypatch)
    return attempt


# ---------------------------------------------------------------------------
# 404 / precondition gating
# ---------------------------------------------------------------------------


def test_reply_404s_for_another_users_thread(monkeypatch):
    account = _account()
    thread = _thread(user_id=uuid4(), account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, uuid4())
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 404


def test_reply_account_paused(monkeypatch):
    user_id = uuid4()
    account = _account(paused=True)
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_paused"


def test_reply_missing_refresh_token(monkeypatch):
    user_id = uuid4()
    account = _account(has_refresh_token=False)
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_refresh_token"


def test_reply_missing_scope_gmail():
    user_id = uuid4()
    account = _account(scope="https://www.googleapis.com/auth/gmail.readonly")
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"


def test_reply_missing_scope_outlook_requires_both_independently():
    user_id = uuid4()
    # Has Mail.Send but not Mail.ReadWrite -- each scope tested missing
    # independently (P7-2), not just "at least one present".
    account = _account(provider="outlook", scope="openid Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"


def test_reply_no_messages_in_thread():
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_reply_target"


def test_reply_no_reply_target_when_thread_is_all_self():
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message(sender=_SELF)]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_reply_target"


def test_reply_account_identity_unavailable():
    user_id = uuid4()
    account = _account(external_user_id=None)
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_identity_unavailable"


def test_reply_too_many_recipients_from_compute_recipients():
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    # 60 CC-worthy addresses via reply_all -- over the 50-object cap.
    cc_header = ", ".join(f"person{i}@example.com" for i in range(60))
    messages = [_message(headers={"To": cc_header})]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply",
            json={"body_text": "hi", "reply_all": True},
        )
    finally:
        _teardown()
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "too_many_recipients"


def test_reply_body_text_validation_rejects_blank():
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "   "}
        )
    finally:
        _teardown()
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Proactive access-token refresh (plan §4.2) -- before the provider
# sequence, mirroring ingest's own expiry check. Ingest's refresh helpers
# are monkeypatched at their app.routes.reply import aliases; the OAuth
# exchange itself is exercised by ingest's own tests, not here.
# ---------------------------------------------------------------------------


def test_reply_gmail_refreshes_expired_token_before_sending(monkeypatch):
    user_id = uuid4()
    account = _account(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5))
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    refresh_calls = []
    new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def fake_refresh(provider):
        refresh_calls.append(provider.id)
        return "fresh-access-token", new_expiry

    monkeypatch.setattr(reply_routes, "_refresh_gmail_access_token", fake_refresh)

    used_tokens = []

    def fake_send(client, *, raw, provider_thread_id):
        used_tokens.append(client.token)
        return {"id": "gmail-sent-1"}

    monkeypatch.setattr(reply_routes, "gmail_send_reply", fake_send)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    assert refresh_calls == [account.id]
    assert used_tokens == ["fresh-access-token"]
    # Persisted the same way ingest_gmail_messages does with its own session.
    assert account.access_token == "fresh-access-token"
    assert account.token_expiry == new_expiry


def test_reply_gmail_refresh_invalid_grant_maps_to_account_paused(monkeypatch):
    user_id = uuid4()
    account = _account(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5))
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    def fake_refresh(provider):
        # Mirrors _refresh_access_token's own invalid_grant branch, which
        # already pauses the account via its own session before raising.
        raise ValueError("Gmail authorization was revoked. Reconnect the account.")

    monkeypatch.setattr(reply_routes, "_refresh_gmail_access_token", fake_refresh)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "account_paused"


def test_reply_gmail_refresh_with_nothing_to_refresh_maps_to_missing_refresh_token(monkeypatch):
    user_id = uuid4()
    account = _account(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5))
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    # (None, None) is _refresh_access_token's own "nothing to refresh with"
    # result (OAuth not configured on this deployment) -- same dead end as
    # no refresh token at all.
    monkeypatch.setattr(reply_routes, "_refresh_gmail_access_token", lambda provider: (None, None))

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_refresh_token"


def test_reply_outlook_refreshes_expired_token_before_sending(monkeypatch):
    user_id = uuid4()
    account = _account(
        provider="outlook",
        scope="Mail.ReadWrite Mail.Send",
        display_email=_SELF,
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    refresh_calls = []

    def fake_refresh(provider_id, refresh_token):
        refresh_calls.append((provider_id, refresh_token))
        return (
            "fresh-outlook-token",
            "new-refresh-token",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

    monkeypatch.setattr(reply_routes, "_refresh_outlook_access_token", fake_refresh)

    used_tokens = []

    def fake_create_draft(client, *, message_id, reply_all, comment):
        used_tokens.append(client.token)
        return {
            "id": "draft-1",
            "body": {"contentType": "text", "content": comment},
            "bodyPreview": comment[:50],
        }

    monkeypatch.setattr(reply_routes, "create_reply_draft", fake_create_draft)
    monkeypatch.setattr(reply_routes, "send_reply_draft", lambda client, *, draft_id: None)
    monkeypatch.setattr(reply_routes.reconcile_reply_attempt, "apply_async", MagicMock())

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    assert refresh_calls == [(account.id, "rt")]
    assert used_tokens == ["fresh-outlook-token"]


def test_reply_gmail_refreshes_token_within_expiry_skew_buffer(monkeypatch):
    """F8: a token expiring a few seconds from now -- inside the 60s skew
    buffer, so `token_expiry > now` is still technically true -- must still
    trigger a refresh. The provider sequence must not start with a token
    that can die mid-request."""
    user_id = uuid4()
    account = _account(token_expiry=datetime.now(timezone.utc) + timedelta(seconds=30))
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    refresh_calls = []
    new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def fake_refresh(provider):
        refresh_calls.append(provider.id)
        return "fresh-access-token", new_expiry

    monkeypatch.setattr(reply_routes, "_refresh_gmail_access_token", fake_refresh)
    monkeypatch.setattr(
        reply_routes,
        "gmail_send_reply",
        lambda client, *, raw, provider_thread_id: {"id": "gmail-sent-1"},
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    assert refresh_calls == [account.id]


def test_reply_gmail_does_not_refresh_token_comfortably_outside_skew_buffer(monkeypatch):
    """The complement: a token expiring well past the 60s buffer is left
    alone -- refresh must not fire on every request."""
    user_id = uuid4()
    account = _account(token_expiry=datetime.now(timezone.utc) + timedelta(minutes=10))
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    monkeypatch.setattr(
        reply_routes,
        "_refresh_gmail_access_token",
        lambda provider: pytest.fail("must not refresh a comfortably fresh token"),
    )
    monkeypatch.setattr(
        reply_routes,
        "gmail_send_reply",
        lambda client, *, raw, provider_thread_id: {"id": "gmail-sent-1"},
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Lock / stale / in-flight / override / superseded (§3.6 step 1)
# ---------------------------------------------------------------------------


def test_reply_thread_busy_on_lock_timeout():
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages, fail_thread_lock=True)
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_thread_busy"
    assert db.rollbacks == 1


def test_reply_state_stale_when_expected_replied_at_mismatches():
    user_id = uuid4()
    account = _account()
    thread = _thread(
        user_id=user_id, account_id=account.id, replied_at=datetime.now(timezone.utc)
    )
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply",
            json={"body_text": "hi", "expected_replied_at": None},
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_state_stale"


def test_reply_state_stale_detected_via_locked_column_read_bypassing_identity_map():
    """F1: the fence check must read `replied_at` via a column-only, FOR
    UPDATE select -- not the identity-mapped object from the route's
    earlier `db.get(MailThread, ...)`. Simulated by a fake DB that answers
    the two reads differently: the thread snapshot still shows
    `replied_at=None` (what the client's `expected_replied_at` agrees
    with), but the locked column read reflects a reply that committed in
    between -- this must still surface as `reply_state_stale`, not a
    silent pass-through that a stale identity-mapped object would produce.
    """
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id, replied_at=None)
    messages = [_message()]
    concurrent_fence = datetime.now(timezone.utc)
    db = _ReplyDB(
        thread=thread,
        account=account,
        messages=messages,
        locked_replied_at=concurrent_fence,
    )
    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply",
            json={"body_text": "hi", "expected_replied_at": None},
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_state_stale"


def test_reply_in_flight_blocks_and_reports_blocker_id(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    blocker_id = uuid4()
    monkeypatch.setattr(reply_routes, "expire_stale_preparing", lambda *a, **k: 0)
    monkeypatch.setattr(
        reply_routes,
        "find_blocking_attempt",
        lambda *a, **k: SimpleNamespace(id=blocker_id),
    )

    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "reply_in_flight"
    assert detail["attempt_id"] == str(blocker_id)
    assert db.commits == 1  # housekeeping (expiry) still persists on reject


def test_reply_override_abandons_current_blocker_and_proceeds(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    blocker_id = uuid4()
    calls = {"find_blocking_attempt": 0}

    def find_blocking(*a, **k):
        calls["find_blocking_attempt"] += 1
        # First call: the current blocker. Second call (after abandon):
        # cleared.
        if calls["find_blocking_attempt"] == 1:
            return SimpleNamespace(id=blocker_id)
        return None

    _basic_send_stubs(monkeypatch, db)
    # Applied AFTER the happy-path stubs above, so these test-specific
    # overrides win (monkeypatch.setattr is last-write-wins).
    monkeypatch.setattr(reply_routes, "find_blocking_attempt", find_blocking)
    abandoned = {}
    monkeypatch.setattr(
        reply_routes,
        "abandon_attempt",
        lambda db, *, attempt_id: abandoned.setdefault("id", attempt_id) or True,
    )
    monkeypatch.setattr(
        reply_routes,
        "gmail_send_reply",
        lambda client, *, raw, provider_thread_id: {"id": "sent-1"},
    )

    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply",
            json={"body_text": "hi", "override_attempt_id": str(blocker_id)},
        )
    finally:
        _teardown()
    assert resp.status_code == 200
    assert abandoned["id"] == blocker_id


def test_reply_stale_override_id_is_rejected_when_a_newer_blocker_exists(monkeypatch):
    """P5-2: an override id from a second tab must not bypass a NEWER
    blocker -- the server recomputes the current blocker under the lock and
    only accepts the override if it's still that one."""
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    stale_override_id = uuid4()
    current_blocker_id = uuid4()

    monkeypatch.setattr(reply_routes, "expire_stale_preparing", lambda *a, **k: 0)
    monkeypatch.setattr(
        reply_routes,
        "find_blocking_attempt",
        lambda *a, **k: SimpleNamespace(id=current_blocker_id),
    )
    abandon_mock = MagicMock()
    monkeypatch.setattr(reply_routes, "abandon_attempt", abandon_mock)

    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply",
            json={"body_text": "hi", "override_attempt_id": str(stale_override_id)},
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "reply_in_flight"
    assert detail["attempt_id"] == str(current_blocker_id)
    abandon_mock.assert_not_called()


def test_reply_superseded_when_inflight_flip_loses(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)

    _basic_send_stubs(monkeypatch, db)
    monkeypatch.setattr(reply_routes, "transition_to_inflight", lambda *a, **k: False)

    client = _client_for(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/reply", json={"body_text": "hi"}
        )
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_superseded"


# ---------------------------------------------------------------------------
# Provider outcome classification (§3.6 step 2/4/5)
# ---------------------------------------------------------------------------


def _send_request(client, thread_id, **json_extra):
    return client.post(
        f"/api/v1/mail/thread/{thread_id}/reply",
        json={"body_text": "hi", **json_extra},
    )


def test_reply_send_rejected_on_definite_4xx(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    def raise_400(client, *, raw, provider_thread_id):
        request = httpx.Request("POST", "https://gmail.googleapis.com/x")
        response = httpx.Response(400, request=request, json={"error": "bad"})
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(reply_routes, "gmail_send_reply", raise_400)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "send_rejected"


def test_reply_send_outcome_unknown_on_timeout(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    def raise_timeout(client, *, raw, provider_thread_id):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(reply_routes, "gmail_send_reply", raise_timeout)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "send_outcome_unknown"


def test_reply_send_outcome_unknown_on_5xx(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    def raise_502(client, *, raw, provider_thread_id):
        request = httpx.Request("POST", "https://gmail.googleapis.com/x")
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

    monkeypatch.setattr(reply_routes, "gmail_send_reply", raise_502)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "send_outcome_unknown"


def test_reply_unknown_outcome_enqueue_failure_does_not_mask_502(monkeypatch):
    """F3: a dead broker on the ambiguous-outcome reconcile enqueue must not
    turn the honest 502 send_outcome_unknown into an unrelated 500 -- the
    durable attempt row (already marked `unknown`) is the real guarantee,
    this enqueue is only timeliness."""
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    def raise_timeout(client, *, raw, provider_thread_id):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(reply_routes, "gmail_send_reply", raise_timeout)
    monkeypatch.setattr(
        reply_routes.reconcile_reply_attempt,
        "apply_async",
        MagicMock(side_effect=RuntimeError("broker is down")),
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "send_outcome_unknown"


def test_reply_outlook_403_on_create_reply_maps_to_missing_scope(monkeypatch):
    """F6: a Graph 403 (permission revoked between the up-front scope check
    and now) surfaces as the same missing_scope/409 the up-front gate
    returns, not a generic send_rejected -- and the attempt is marked
    failed, not left dangling inflight."""
    user_id = uuid4()
    account = _account(provider="outlook", scope="Mail.ReadWrite Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    mark_failed_calls = []
    monkeypatch.setattr(
        reply_routes, "mark_failed", lambda db, *, attempt_id: mark_failed_calls.append(attempt_id)
    )

    def raise_403(client, *, message_id, reply_all, comment):
        raise SendError(
            "missing_scope",
            "This account needs to be reconnected to grant reply-send permission.",
        )

    monkeypatch.setattr(reply_routes, "create_reply_draft", raise_403)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"
    assert len(mark_failed_calls) == 1


def test_reply_outlook_403_on_send_draft_maps_to_missing_scope(monkeypatch):
    """F6: the same 403-during-the-send-step case -- the draft was created
    but sending it was rejected, nothing went out."""
    user_id = uuid4()
    account = _account(provider="outlook", scope="Mail.ReadWrite Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    monkeypatch.setattr(
        reply_routes,
        "create_reply_draft",
        lambda client, *, message_id, reply_all, comment: {
            "id": "draft-1",
            "body": {"contentType": "text", "content": comment},
            "bodyPreview": comment[:50],
        },
    )
    mark_failed_calls = []
    monkeypatch.setattr(
        reply_routes, "mark_failed", lambda db, *, attempt_id: mark_failed_calls.append(attempt_id)
    )

    def raise_403(client, *, draft_id):
        raise SendError(
            "missing_scope",
            "This account needs to be reconnected to grant reply-send permission.",
        )

    monkeypatch.setattr(reply_routes, "send_reply_draft", raise_403)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "missing_scope"
    assert len(mark_failed_calls) == 1


def test_reply_outlook_cap_violation_marks_failed_and_returns_422(monkeypatch):
    user_id = uuid4()
    account = _account(provider="outlook", scope="Mail.ReadWrite Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    mark_failed_calls = []
    monkeypatch.setattr(
        reply_routes, "mark_failed", lambda db, *, attempt_id: mark_failed_calls.append(attempt_id)
    )

    def raise_cap(client, *, message_id, reply_all, comment):
        raise SendError("too_many_recipients", "61 addresses exceeds the 50 cap")

    monkeypatch.setattr(reply_routes, "create_reply_draft", raise_cap)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "too_many_recipients"
    assert len(mark_failed_calls) == 1


# ---------------------------------------------------------------------------
# Success paths (§3.6 step 3 / §3.4)
# ---------------------------------------------------------------------------


def test_reply_gmail_success_persists_message_and_returns_replied_at(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id, subject="Renewal")
    messages = [_message(headers={"Message-ID": "<orig@example.com>"})]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    attempt = _basic_send_stubs(monkeypatch, db)

    monkeypatch.setattr(
        reply_routes,
        "gmail_send_reply",
        lambda client, *, raw, provider_thread_id: {"id": "gmail-sent-1"},
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == str(thread.id)
    assert body["resolved_action_items"] == 1
    assert body["message"]["sender"] == _SELF
    assert body["message"]["recipient"] == [_OTHER]
    assert body["message"]["body_text"] == "hi"
    assert db.subject_update == "Re: Renewal"
    assert attempt.id is not None


def test_reply_gmail_completion_returns_early_when_reconciliation_already_won(monkeypatch):
    """Race ordering (plan §3.6 step 3): a scheduled ingest can deliver the
    sent copy and reconcile it before this request re-acquires the lock --
    the completion txn must return success from the already-persisted row,
    not re-insert."""
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id, replied_at=datetime.now(timezone.utc))
    messages = [_message(headers={"Message-ID": "<orig@example.com>"})]
    already_sent = SimpleNamespace(
        id=uuid4(),
        sent_at=datetime.now(timezone.utc),
        sender=_SELF,
        recipient=[_OTHER],
        cc=None,
        snippet="hi",
        body_text="hi",
        body_html=None,
    )
    db = _ReplyDB(
        thread=thread,
        account=account,
        messages=messages,
        attempt_status="sent",
        already_sent_message=already_sent,
    )
    _basic_send_stubs(monkeypatch, db)
    monkeypatch.setattr(
        reply_routes,
        "gmail_send_reply",
        lambda client, *, raw, provider_thread_id: {"id": "gmail-sent-1"},
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id, expected_replied_at=thread.replied_at.isoformat())
    finally:
        _teardown()

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"]["id"] == str(already_sent.id)
    assert body["resolved_action_items"] == 0


def test_reply_outlook_success_builds_ephemeral_message_and_enqueues_reconcile(monkeypatch):
    user_id = uuid4()
    account = _account(provider="outlook", scope="Mail.ReadWrite Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    monkeypatch.setattr(
        reply_routes,
        "create_reply_draft",
        lambda client, *, message_id, reply_all, comment: {
            "id": "draft-1",
            "body": {"contentType": "text", "content": comment},
            "bodyPreview": comment[:50],
        },
    )
    send_calls = []
    monkeypatch.setattr(
        reply_routes,
        "send_reply_draft",
        lambda client, *, draft_id: send_calls.append(draft_id),
    )
    reconcile_mock = MagicMock()
    monkeypatch.setattr(reply_routes.reconcile_reply_attempt, "apply_async", reconcile_mock)

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    body = resp.json()
    assert send_calls == ["draft-1"]
    assert body["message"]["body_text"] == "hi"
    reconcile_mock.assert_called_once()
    assert reconcile_mock.call_args.kwargs["args"] == [str(account.id)]


def test_reply_outlook_success_enqueue_failure_does_not_mask_200(monkeypatch):
    """F3: the same guard on the success-path enqueue -- the reply already
    went out and the completion txn already committed, so a dead broker
    here must not turn that into a 500."""
    user_id = uuid4()
    account = _account(provider="outlook", scope="Mail.ReadWrite Mail.Send", display_email=_SELF)
    thread = _thread(user_id=user_id, account_id=account.id, provider="outlook")
    messages = [_message()]
    db = _ReplyDB(thread=thread, account=account, messages=messages)
    _basic_send_stubs(monkeypatch, db)

    monkeypatch.setattr(
        reply_routes,
        "create_reply_draft",
        lambda client, *, message_id, reply_all, comment: {
            "id": "draft-1",
            "body": {"contentType": "text", "content": comment},
            "bodyPreview": comment[:50],
        },
    )
    monkeypatch.setattr(reply_routes, "send_reply_draft", lambda client, *, draft_id: None)
    monkeypatch.setattr(
        reply_routes.reconcile_reply_attempt,
        "apply_async",
        MagicMock(side_effect=RuntimeError("broker is down")),
    )

    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()

    assert resp.status_code == 200
    assert resp.json()["message"]["body_text"] == "hi"


# ---------------------------------------------------------------------------
# Rate limiting (§3.6 -- fail_closed on send)
# ---------------------------------------------------------------------------


class _BrokenRedis:
    def incr(self, key):
        raise redis.ConnectionError("redis is down")


class _OverLimitRedis:
    def __init__(self, limit):
        self._limit = limit
        self._count = 0

    def incr(self, key):
        self._count += 1
        return self._count + self._limit

    def expire(self, key, seconds):
        pass

    def ttl(self, key):
        return 42


def test_reply_send_unavailable_when_rate_limiter_fails_closed(monkeypatch):
    monkeypatch.setattr(ratelimit, "_client", lambda: _BrokenRedis())
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "send_unavailable"


def test_reply_rate_limited_over_the_send_cap(monkeypatch):
    monkeypatch.setattr(ratelimit, "_client", lambda: _OverLimitRedis(10))
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, user_id)
    try:
        resp = _send_request(client, thread.id)
    finally:
        _teardown()
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# reply-draft
# ---------------------------------------------------------------------------


def test_reply_draft_404s_for_another_users_thread():
    account = _account()
    thread = _thread(user_id=uuid4(), account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    client = _client_for(db, uuid4())
    try:
        resp = client.post(f"/api/v1/mail/thread/{thread.id}/reply-draft")
    finally:
        _teardown()
    assert resp.status_code == 404


def test_reply_draft_credential_missing(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    monkeypatch.setattr(
        reply_routes,
        "resolve_reply_draft_credential",
        lambda db, user_id: ResolvedExtraction(
            credential=None,
            source=None,
            stored=False,
            blocked_reason=None,
            revision=None,
            credential_id=None,
        ),
    )
    client = _client_for(db, user_id)
    try:
        resp = client.post(f"/api/v1/mail/thread/{thread.id}/reply-draft")
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_draft_credential_missing"


def test_reply_draft_credential_blocked(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    monkeypatch.setattr(
        reply_routes,
        "resolve_reply_draft_credential",
        lambda db, user_id: ResolvedExtraction(
            credential=None,
            source=None,
            stored=True,
            blocked_reason="destination_rejected",
            revision=1,
            credential_id=uuid4(),
        ),
    )
    client = _client_for(db, user_id)
    try:
        resp = client.post(f"/api/v1/mail/thread/{thread.id}/reply-draft")
    finally:
        _teardown()
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "reply_draft_credential_blocked"


def test_reply_draft_failed_maps_to_502(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    credential = LlmCredential(provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt")
    monkeypatch.setattr(
        reply_routes,
        "resolve_reply_draft_credential",
        lambda db, user_id: ResolvedExtraction(
            credential=credential,
            source="user",
            stored=True,
            blocked_reason=None,
            revision=1,
            credential_id=uuid4(),
        ),
    )
    monkeypatch.setattr(
        reply_routes,
        "generate_reply_draft",
        lambda *, credential, subject, messages: ReplyDraftAttempt(
            draft_text=None, provider_call_succeeded=False, usage=None
        ),
    )
    client = _client_for(db, user_id)
    try:
        resp = client.post(f"/api/v1/mail/thread/{thread.id}/reply-draft")
    finally:
        _teardown()
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "reply_draft_failed"


def test_reply_draft_success_returns_text_provider_and_model(monkeypatch):
    user_id = uuid4()
    account = _account()
    thread = _thread(user_id=user_id, account_id=account.id)
    db = _ReplyDB(thread=thread, account=account, messages=[])
    credential = LlmCredential(provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-x")
    monkeypatch.setattr(
        reply_routes,
        "resolve_reply_draft_credential",
        lambda db, user_id: ResolvedExtraction(
            credential=credential,
            source="fallback",
            stored=False,
            blocked_reason=None,
            revision=None,
            credential_id=None,
        ),
    )
    monkeypatch.setattr(
        reply_routes,
        "generate_reply_draft",
        lambda *, credential, subject, messages: ReplyDraftAttempt(
            draft_text="Sounds good, thanks!", provider_call_succeeded=True, usage=None
        ),
    )
    client = _client_for(db, user_id)
    try:
        resp = client.post(f"/api/v1/mail/thread/{thread.id}/reply-draft")
    finally:
        _teardown()
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"draft_text": "Sounds good, thanks!", "provider": "openai", "model": "gpt-x"}
