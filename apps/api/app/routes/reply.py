"""The reply-send and reply-draft HTTP surface (plan
docs/plans/2026-08-13-reply-plan.md §3.6/§3.7/§4.1).

Every DB write to ``reply_attempt`` or the fence/action-resolution pair
funnels through ``app/services/mail_send/common.py``'s CAS helpers -- this
module owns the choreography (which helper to call, in what order, under
which lock) and the HTTP mapping, never a bare status write of its own.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from typing import Any, NoReturn
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.core.ratelimit import user_rate_limit
from app.db.base import SessionLocal
from app.db.models import MailMessage, MailThread, ProviderAccount, ReplyAttempt
from app.db.schemas.reply import ReplyDraft, ReplySent
from app.deps import get_current_user, get_db
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.gmail_ingest import _refresh_access_token as _refresh_gmail_access_token
from app.services.ingest.outlook_client import OutlookClient
from app.services.ingest.outlook_ingest import (
    _refresh_and_persist_token as _refresh_outlook_access_token,
)
from app.services.mail_send.common import (
    RecipientSet,
    SendError,
    abandon_attempt,
    advance_fence_and_resolve,
    build_reply_mime,
    compute_recipients,
    expire_stale_preparing,
    find_blocking_attempt,
    generate_message_id,
    mark_failed,
    mark_sent,
    mark_unknown,
    record_provider_message_id,
    resolve_self_address,
    select_reply_target,
    start_attempt,
    threading_headers,
    transition_to_inflight,
)
from app.services.mail_send.gmail_send import send_reply as gmail_send_reply
from app.services.mail_send.outlook_send import (
    build_ephemeral_message,
    create_reply_draft,
    send_reply_draft,
)
from app.services.nlp.backfill import latest_message_ordering
from app.services.nlp.classifier import build_classification_text, classify_with_usage
from app.services.nlp.persistence import upsert_classification
from app.services.nlp.providers import ClassificationRouter
from app.services.nlp.reply_draft import generate_reply_draft, resolve_reply_draft_credential
from app.services.nlp.usage import UsageAccumulator
from app.workers.tasks_ingest import reconcile_reply_attempt

router = APIRouter(prefix="/mail")

# One end-to-end deadline across the whole provider sequence (plan §3.6 step
# 2, P3-5) -- Outlook's two-call choreography in particular must not rely on
# per-request timeouts alone.
_PROVIDER_SEQUENCE_DEADLINE_SECONDS = 45.0

# Timeliness-only recovery countdown (plan §3.6 step 4) -- fired on every
# ambiguous outcome, and on Outlook SUCCESS too (plan §3.4) so the
# authoritative Sent Items copy lands and gets classified promptly.
_RECONCILE_COUNTDOWN_SECONDS = 60

# The real, documented scope Google echoes back for a granted gmail.modify
# consent (see app/routes/auth_google.py's SCOPES) -- gated on the full URI,
# not the short name, since that's the actual stored value (plan §2).
_GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# Microsoft's granted-scope echo is stored short-form in this app (see
# app/routes/auth_microsoft.py's MS_SCOPES and test_auth_microsoft.py's
# stored-scope fixtures) -- both tested independently (P7-2), never one
# implying the other.
_OUTLOOK_REQUIRED_SCOPE_TOKENS = ("mail.readwrite", "mail.send")

# HTTP status for every SendError code that can reach this route from
# common.py/outlook_send.py -- the one place mapping the shared exception
# type onto the frozen §3.8 envelope.
_SEND_ERROR_STATUS: dict[str, int] = {
    "account_identity_unavailable": 409,
    "no_reply_target": 409,
    "too_many_recipients": 422,
    "send_rejected": 502,
}

# Snippet length for our own assembled sent-reply representation -- Gmail's
# send response carries no snippet field (unlike a messages.get/list read),
# so we derive one the same rough way the console displays one elsewhere.
_SNIPPET_MAX_LEN = 200

# Proactive-refresh buffer (plan §4.2, F8): a token that's merely close to
# expiry -- not yet past it -- must not be handed to the provider sequence,
# since Outlook's two-call choreography plus Gmail's own retries can eat
# more than a few seconds. Refresh a little early instead of racing a 401
# mid-sequence.
_TOKEN_EXPIRY_SKEW = timedelta(seconds=60)


class ReplyRequest(BaseModel):
    body_text: str
    reply_all: bool = False
    expected_replied_at: datetime | None = None
    override_attempt_id: UUID | None = None

    @field_validator("body_text")
    @classmethod
    def _validate_body_text(cls, value: str) -> str:
        stripped = value.strip()
        if not (1 <= len(stripped) <= 50_000):
            raise ValueError("body_text must be 1-50,000 characters after stripping")
        return stripped


def _http_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _send_error_response(exc: SendError) -> HTTPException:
    status_code = _SEND_ERROR_STATUS.get(exc.code, 409)
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)})


def _scope_tokens(raw: str | None) -> set[str]:
    return {tok.lower() for tok in (raw or "").split() if tok}


def _check_scope(account: ProviderAccount) -> None:
    """Exact tokenized, case-insensitive scope check (plan §3.2) -- Gmail
    needs `gmail.modify`; Outlook needs BOTH `Mail.ReadWrite` (createReply's
    permission) AND `Mail.Send` (the draft send's), each tested missing
    independently (P7-2)."""
    tokens = _scope_tokens(account.scope)
    if account.provider == "gmail":
        missing = _GMAIL_MODIFY_SCOPE.lower() not in tokens
    else:
        missing = any(token not in tokens for token in _OUTLOOK_REQUIRED_SCOPE_TOKENS)
    if missing:
        raise _http_error(
            "missing_scope",
            "This account needs to be reconnected to grant reply-send permission.",
            409,
        )


def _ensure_fresh_access_token(db: Session, account: ProviderAccount) -> str:
    """Proactive expiry refresh (plan §4.2), the same check ingest does
    before building a provider client: if `token_expiry` is missing or has
    passed, refresh BEFORE the provider sequence rather than relying on a
    reactive 401 to surface as a routine `send_rejected` once an hour. Reuses
    ingest's own refresh helpers verbatim (never reimplemented here) so the
    OAuth exchange, invalid_grant classification, and persistence stay in
    exactly one place per provider.

    `account.refresh_token` is already known non-empty by the time this
    runs (the missing_refresh_token gate above ran first) -- a `None`
    refreshed token here means OAuth isn't configured for this deployment,
    which is the same dead end as no refresh token at all.
    """
    now = datetime.now(timezone.utc)
    if account.token_expiry and account.token_expiry > now + _TOKEN_EXPIRY_SKEW:
        return account.access_token

    if account.provider == "gmail":
        try:
            refreshed_token, token_expiry = _refresh_gmail_access_token(account)
        except ValueError as exc:
            # invalid_grant -- _refresh_access_token already paused the
            # account (its own session), mirroring ingest's classification.
            raise _http_error("account_paused", str(exc), 409) from exc
        if not refreshed_token:
            raise _http_error(
                "missing_refresh_token",
                "This account needs to be reconnected before you can reply.",
                409,
            )
        # Gmail's helper does NOT persist -- the caller does, same as
        # ingest_gmail_messages does with its own session.
        account.access_token = refreshed_token
        account.token_expiry = token_expiry
        db.commit()
        return refreshed_token

    try:
        refreshed = _refresh_outlook_access_token(account.id, account.refresh_token)
    except ValueError as exc:
        raise _http_error("account_paused", str(exc), 409) from exc
    if refreshed is None:
        raise _http_error(
            "missing_refresh_token",
            "This account needs to be reconnected before you can reply.",
            409,
        )
    access_token, _refresh_token, _token_expiry = refreshed
    # Outlook's helper already persisted the new token through its OWN
    # independent session (see outlook_ingest.py's module docstring) -- our
    # session's `account` object is left alone so we never race a
    # redundant, possibly-stale re-write of what it just committed.
    return access_token


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    """Exact-equality stale-guard comparison (plan §3.6): both null (never
    replied, client agrees) counts as a match; anything else that can't be
    compared -- e.g. a naive datetime slipping past validation -- fails
    closed as a mismatch rather than risking a false pass."""
    if a is None or b is None:
        return a is b
    try:
        return a == b
    except TypeError:
        return False


def _snippet(body_text: str) -> str | None:
    cleaned = body_text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= _SNIPPET_MAX_LEN:
        return cleaned
    return cleaned[:_SNIPPET_MAX_LEN].rstrip() + "…"


def _headers_from_raw_mime(raw: str) -> dict[str, str]:
    """Decode the exact MIME we just handed Gmail back into a headers dict --
    guarantees the persisted `mail_message.headers` matches what actually
    went over the wire (Message-ID included) instead of re-deriving the
    same values a second, possibly-divergent way."""
    padded = raw + "=" * (-len(raw) % 4)
    parsed = message_from_bytes(base64.urlsafe_b64decode(padded))
    return {key: value for key, value in parsed.items()}


def _message_out_from_row(message: MailMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "sent_at": message.sent_at,
        "sender": message.sender,
        "recipient": message.recipient,
        "cc": message.cc,
        "snippet": message.snippet,
        "body_text": message.body_text,
        "body_html": message.body_html,
    }


def _fail_send(db: Session, *, attempt_id: UUID) -> NoReturn:
    """Definite rejection (plan §3.6 step 5): CAS the attempt to `failed`
    (never left dangling `inflight`/`abandoned`, which would wrongly keep
    the in-flight guard closed forever) and report a safe-to-retry 502."""
    mark_failed(db, attempt_id=attempt_id)
    db.commit()
    raise _http_error(
        "send_rejected",
        "The reply was rejected and was not sent. It's safe to try again.",
        502,
    )


def _unknown_send(db: Session, *, attempt_id: UUID, provider_account_id: UUID) -> NoReturn:
    """Ambiguous outcome (plan §3.6 step 4): CAS to `unknown`, enqueue the
    timeliness-only reconciliation task, and report the honest "may have
    sent" 502 -- the in-flight guard stays closed until reconciliation (or
    an explicit override) resolves it."""
    mark_unknown(db, attempt_id=attempt_id)
    db.commit()
    # A dead broker here must not turn an honest "may have sent" 502 into a
    # 500 -- the durable attempt row is the real guarantee (it keeps the
    # in-flight guard closed), this enqueue is only timeliness.
    try:
        reconcile_reply_attempt.apply_async(
            args=[str(provider_account_id)], countdown=_RECONCILE_COUNTDOWN_SECONDS
        )
    except Exception:
        logger.warning(
            "reconcile_reply_attempt enqueue failed for unknown attempt %s", attempt_id
        )
    raise _http_error(
        "send_outcome_unknown",
        "Your reply may have been sent. The thread will update once your "
        "mailbox syncs -- don't resend unless it's still missing after that.",
        502,
    )


def _fail_after_draft_created_db_error(
    db: Session, *, client: OutlookClient, draft_id: str | None, attempt_id: UUID
) -> NoReturn:
    """R-5(a) (final review): createReply already succeeded on the provider,
    but recording the draft id (or committing that write) failed -- left
    alone, the `inflight` attempt row survives with NO correlation id for a
    draft that was never sent, which reconciliation can never match
    (it keys on `provider_message_id`). Rolls back the broken transaction,
    best-effort deletes the orphan draft (nothing was ever SENT from it --
    only createReply ran), and settles the attempt `failed` in a fresh
    session, since `db`'s own transaction is the one that just failed and
    may not be safely reusable for further writes.
    """
    db.rollback()
    if draft_id:
        try:
            client.delete_draft(draft_id)
        except httpx.HTTPError:
            logger.warning(
                "failed to delete orphan Outlook reply draft %s for attempt %s",
                draft_id,
                attempt_id,
            )
    try:
        with SessionLocal() as fresh_db:
            mark_failed(fresh_db, attempt_id=attempt_id)
            fresh_db.commit()
    except Exception:
        logger.exception(
            "failed to settle attempt %s as failed after draft-record DB error", attempt_id
        )
    raise _http_error(
        "send_rejected",
        "The reply was rejected and was not sent. It's safe to try again.",
        502,
    )


def _unknown_after_completion_db_error(
    db: Session, *, attempt_id: UUID, provider_account_id: UUID
) -> NoReturn:
    """R-5(b) (final review): a completion-transaction failure AFTER a
    successful provider send must never escape unhandled -- the mail
    already left, so from here on this is exactly the ambiguous outcome,
    never a 500. Rolls back `db` (whose own commit is what failed) and CASes
    the attempt to `unknown` in a FRESH session, then enqueues the same
    timeliness-only reconciliation task `_unknown_send` does -- both
    best-effort, since the durable attempt row (still `inflight` after the
    rollback) is what actually keeps the in-flight guard closed either way.
    """
    db.rollback()
    try:
        with SessionLocal() as fresh_db:
            mark_unknown(fresh_db, attempt_id=attempt_id)
            fresh_db.commit()
    except Exception:
        logger.exception(
            "failed to CAS attempt %s to unknown after completion-txn DB error", attempt_id
        )
    try:
        reconcile_reply_attempt.apply_async(
            args=[str(provider_account_id)], countdown=_RECONCILE_COUNTDOWN_SECONDS
        )
    except Exception:
        logger.warning(
            "reconcile_reply_attempt enqueue failed after completion-txn DB error for "
            "attempt %s",
            attempt_id,
        )
    raise _http_error(
        "send_outcome_unknown",
        "Your reply may have been sent. The thread will update once your "
        "mailbox syncs -- don't resend unless it's still missing after that.",
        502,
    )


def _handle_provider_exception(
    exc: httpx.HTTPError, db: Session, *, attempt_id: UUID, provider_account_id: UUID
) -> NoReturn:
    """Classify a provider-call failure (plan §3.6 step 2): a definite 4xx
    response is a rejection (`_fail_send`); a timeout, connection failure, or
    5xx is ambiguous (`_unknown_send`) -- we genuinely don't know whether the
    provider accepted the send before the failure."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status_code = exc.response.status_code
        if 400 <= status_code < 500:
            _fail_send(db, attempt_id=attempt_id)
    _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)


def _deadline_exceeded(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _classify_gmail_message_best_effort(
    db: Session, *, message: MailMessage, subject: str | None, user_id: UUID
) -> None:
    """Gmail-only, post-completion, its own transaction (plan §3.4): our
    assembled MIME IS what sync later normalizes, so this is the one and
    only classification pass a Gmail reply ever needs. Never inside a lock,
    and a failure here must never mask the send this request already
    completed -- it's logged and left for a later reclassify/backfill."""
    try:
        text_for_classification = build_classification_text(
            subject, message.snippet, message.body_text
        )
        router = ClassificationRouter(user_id)
        routing = router.routing_for(db)
        attempt_result = classify_with_usage(text_for_classification, routing=routing)
        label, confidence, rationale, model_version = attempt_result.verdict
        if routing.mode == "user" and routing.credential is not None:
            acc = UsageAccumulator(user_id)
            acc.record(
                "classification",
                routing.credential.provider,
                attempt_result.usage,
                provider_call_succeeded=attempt_result.provider_call_succeeded,
            )
            acc.flush(db)
        upsert_classification(
            db,
            message_id=message.id,
            label=label,
            confidence=confidence,
            rationale=rationale,
            model_version=model_version,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "best-effort classification failed for sent reply message %s", message.id
        )


@router.post(
    "/thread/{thread_id}/reply",
    response_model=ReplySent,
    dependencies=[Depends(user_rate_limit("reply_send", 10, 300, fail_closed=True))],
)
def send_reply(
    thread_id: UUID,
    payload: ReplyRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    # A thread's account always exists (FK cascade) -- defensive 404 only.
    account = db.get(ProviderAccount, thread.provider_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    if account.sync_paused_at is not None:
        raise _http_error(
            "account_paused",
            "This account is paused and needs to be reconnected before you can reply.",
            409,
        )
    if not account.refresh_token:
        raise _http_error(
            "missing_refresh_token",
            "This account is missing a refresh token and needs to be reconnected.",
            409,
        )

    _check_scope(account)

    try:
        self_address = resolve_self_address(account)
    except SendError as exc:
        raise _send_error_response(exc) from exc

    provider = account.provider
    provider_account_id = account.id
    # Capture thread attributes BEFORE the refresh below: a Gmail refresh
    # commits, which expires every loaded object (expire_on_commit), and a
    # post-commit lazy reload of `thread` would 500 with ObjectDeletedError
    # if the row vanished meanwhile (concurrent disconnect cascade) -- the
    # same class of bug the reclassify enqueue had.
    provider_thread_id = thread.provider_thread_id
    subject = thread.subject
    gmail_message_id_header = generate_message_id() if provider == "gmail" else None

    # Proactive refresh (plan §4.2), before any lock and before the provider
    # sequence -- an access token aging past its ~1h TTL must not become a
    # routine send_rejected.
    access_token = _ensure_fresh_access_token(db, account)

    # Txn A (plan §3.6 step 1): validate + claim, under a short lock_timeout
    # so an ingest run legitimately holding this thread's lock for minutes
    # (plan §2) surfaces as a clean, retryable "busy" instead of hanging the
    # request.
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    try:
        # Column-only select, bypassing the identity map (mailbox.py's
        # set_thread_done idiom) -- `thread` above came from a plain
        # db.get() and is already sitting in the session identity map. A
        # follow-up select(MailThread)...with_for_update() would return
        # that SAME cached object without refreshing its attributes (the
        # ORM only repopulates an already-loaded object with
        # populate_existing()), so a concurrent reply that committed
        # between the two reads would go undetected -- the fence check
        # would compare the pre-lock snapshot against itself. Selecting
        # just the column skips the identity map entirely and always
        # reflects the row as it exists once the lock is actually held.
        locked_replied_at = db.execute(
            select(MailThread.replied_at).where(MailThread.id == thread_id).with_for_update()
        ).scalar_one()
    except OperationalError as exc:
        db.rollback()
        raise _http_error(
            "reply_thread_busy",
            "The mailbox is busy syncing -- try again in a moment.",
            409,
        ) from exc

    if not _same_instant(payload.expected_replied_at, locked_replied_at):
        db.rollback()
        raise _http_error(
            "reply_state_stale",
            "This thread changed since you loaded it. Refresh and try again.",
            409,
        )

    # R-2 (final review) -- deliberate deviation from the frozen §3.6 step-1
    # ordering, documented here per the review's instruction: the message
    # set and target/recipient computation used to run BEFORE this lock was
    # acquired. A concurrently ingested newer inbound message (a different
    # sender/Reply-To) doesn't change `replied_at`, so the stale guard above
    # would pass while the reply still targeted the STALE correspondent --
    # and the fence (`replied_at = attempt.created_at`) would then resolve
    # the newer message's obligation as answered too. Reading messages and
    # computing target/recipients/threading HERE, under the thread lock (which
    # ingest also holds while inserting new messages -- see gmail_ingest.py/
    # outlook_ingest.py's own MailThread lock around message upserts), means
    # any concurrent ingest has either fully committed before we got the
    # lock (so we see its newest message) or is blocked behind us (so it
    # can't land a newer message mid-computation). No caller above this
    # point queries `MailMessage` for this thread, so there's no stale
    # identity-map snapshot to worry about here the way there was for
    # `replied_at` above.
    messages = (
        db.execute(select(MailMessage).where(MailMessage.thread_id == thread_id))
        .scalars()
        .all()
    )
    if not messages:
        db.rollback()
        raise _http_error("no_reply_target", "This thread has no messages to reply to.", 409)

    try:
        target = select_reply_target(messages, self_address)
        recipients = compute_recipients(
            target_message=target, self_address=self_address, reply_all=payload.reply_all
        )
    except SendError as exc:
        db.rollback()
        raise _send_error_response(exc) from exc

    target_provider_message_id = target.provider_message_id

    # Stale-preparing expiry runs under the lock, before this request admits
    # itself (P5-1), so the in-flight guard below never blocks on a row that
    # should already be terminal. One shared `now` for expiry and both
    # blocker lookups (F7) -- the two predicates are exact complements, and
    # each deriving its own clock read could let a row fall between them.
    lock_time = datetime.now(timezone.utc)
    expire_stale_preparing(db, thread_id=thread_id, now=lock_time)
    blocker = find_blocking_attempt(db, thread_id=thread_id, now=lock_time)

    if (
        blocker is not None
        and payload.override_attempt_id is not None
        and blocker.id == payload.override_attempt_id
    ):
        # P5-2: the override id is re-verified as the CURRENT blocker under
        # this same lock, so a stale id from a second tab can't bypass a
        # newer blocker.
        abandon_attempt(db, attempt_id=payload.override_attempt_id)
        blocker = find_blocking_attempt(db, thread_id=thread_id, now=lock_time)

    if blocker is not None:
        # Commit here, not rollback -- the expiry/abandon writes above are
        # real, independent state transitions that must persist even though
        # THIS request is being rejected.
        blocker_id = blocker.id
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reply_in_flight",
                "message": "A reply to this thread is already in progress.",
                "attempt_id": str(blocker_id),
            },
        )

    attempt = start_attempt(
        db,
        thread_id=thread_id,
        user_id=current_user.id,
        provider=provider,
        gmail_message_id_header=gmail_message_id_header,
    )
    attempt_id = attempt.id
    attempt_created_at = attempt.created_at
    db.commit()

    # Provider sequence (plan §3.6 step 2): no DB locks held. The conditional
    # preparing -> inflight flip is its own tiny txn, immediately before the
    # first provider call.
    flipped = transition_to_inflight(db, attempt_id=attempt_id)
    db.commit()
    if not flipped:
        raise _http_error(
            "reply_superseded",
            "This reply attempt was superseded by a newer one. Nothing was sent.",
            409,
        )

    deadline = time.monotonic() + _PROVIDER_SEQUENCE_DEADLINE_SECONDS

    if provider == "gmail":
        in_reply_to, references = threading_headers(target)
        raw = build_reply_mime(
            self_address=self_address,
            recipients=recipients,
            subject=subject,
            body_text=payload.body_text,
            in_reply_to=in_reply_to,
            references=references,
            message_id=gmail_message_id_header,
        )
        client = GmailClient(access_token)
        if _deadline_exceeded(deadline):
            _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)
        try:
            # R-8 (final review): thread the REMAINING slice of the 45s
            # budget into the send call itself, not just the between-calls
            # check above -- send_message's own 429 retry loop can
            # otherwise burn 3x its 20s per-request timeout plus backoff
            # well past this request's deadline.
            response = gmail_send_reply(
                client,
                raw=raw,
                provider_thread_id=provider_thread_id,
                remaining_seconds=max(0.0, deadline - time.monotonic()),
            )
        except httpx.HTTPError as exc:
            _handle_provider_exception(
                exc, db, attempt_id=attempt_id, provider_account_id=provider_account_id
            )
        except Exception:
            logger.exception("unexpected error sending Gmail reply for attempt %s", attempt_id)
            _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)

        provider_message_id = response.get("id")
        if not provider_message_id:
            # A 2xx with no id is malformed beyond anything Gmail is known to
            # send -- we genuinely can't confirm what happened, so treat it
            # the same as any other ambiguous outcome rather than guess.
            _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)
        sent_headers = _headers_from_raw_mime(raw)
        try:
            return _complete_gmail_send(
                db,
                thread_id=thread_id,
                attempt_id=attempt_id,
                attempt_created_at=attempt_created_at,
                self_address=self_address,
                recipients=recipients,
                body_text=payload.body_text,
                headers=sent_headers,
                provider_message_id=provider_message_id,
                current_user_id=current_user.id,
            )
        except SQLAlchemyError:
            # R-5(b): the Gmail send already succeeded -- this can only be a
            # completion-transaction DB failure, never a reason to 500.
            _unknown_after_completion_db_error(
                db, attempt_id=attempt_id, provider_account_id=provider_account_id
            )

    # Outlook: createReply -> record draft id -> send (plan §3.1/§3.6 step 2).
    client = OutlookClient(access_token)
    if _deadline_exceeded(deadline):
        _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)
    try:
        # R-8 (final review): remaining-seconds pre-flight guard plus the
        # actual per-request HTTP timeout, capped by OutlookClient itself.
        draft = create_reply_draft(
            client,
            message_id=target_provider_message_id,
            reply_all=payload.reply_all,
            comment=payload.body_text,
            remaining_seconds=max(0.0, deadline - time.monotonic()),
        )
    except SendError as exc:
        # A locally-decided rejection (missing draft id, or the §3.3 cap
        # violated on Graph's own computed recipients) -- the attempt is
        # definitely not going anywhere, so it's settled the same way a
        # provider 4xx is, but keeps its own status code (422 for the cap).
        mark_failed(db, attempt_id=attempt_id)
        db.commit()
        raise _send_error_response(exc) from exc
    except httpx.HTTPError as exc:
        _handle_provider_exception(
            exc, db, attempt_id=attempt_id, provider_account_id=provider_account_id
        )
    except Exception:
        logger.exception("unexpected error creating Outlook reply draft for attempt %s", attempt_id)
        _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)

    try:
        record_provider_message_id(
            db, attempt_id=attempt_id, provider_message_id=draft.get("id")
        )
        db.commit()
    except SQLAlchemyError:
        # R-5(a) (final review): createReply already succeeded -- the draft
        # exists on the provider -- but recording its id / committing that
        # failed. Left alone, the `inflight` attempt survives with NO
        # correlation id for an unsent draft: reconciliation matches on
        # `provider_message_id`, so this would be unreconcilable forever.
        # Nothing was ever sent from this draft (only createReply ran), so
        # this settles `failed`, not `unknown`.
        _fail_after_draft_created_db_error(
            db, client=client, draft_id=draft.get("id"), attempt_id=attempt_id
        )

    if _deadline_exceeded(deadline):
        _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)
    try:
        # R-8: same remaining-seconds pre-flight guard as createReply above.
        send_reply_draft(
            client,
            draft_id=draft["id"],
            remaining_seconds=max(0.0, deadline - time.monotonic()),
        )
    except SendError as exc:
        # F6: a Graph 403 on the send step too (permission revoked between
        # the up-front scope check and now) -- draft-stage failure, nothing
        # was sent, settled the same way create_reply_draft's SendError is.
        mark_failed(db, attempt_id=attempt_id)
        db.commit()
        raise _send_error_response(exc) from exc
    except httpx.HTTPError as exc:
        _handle_provider_exception(
            exc, db, attempt_id=attempt_id, provider_account_id=provider_account_id
        )
    except Exception:
        logger.exception("unexpected error sending Outlook reply draft for attempt %s", attempt_id)
        _unknown_send(db, attempt_id=attempt_id, provider_account_id=provider_account_id)

    sent_at = datetime.now(timezone.utc)
    try:
        return _complete_outlook_send(
            db,
            thread_id=thread_id,
            account_id=provider_account_id,
            attempt_id=attempt_id,
            attempt_created_at=attempt_created_at,
            draft=draft,
            self_address=self_address,
            recipients=recipients,
            sent_at=sent_at,
        )
    except SQLAlchemyError:
        # R-5(b): the Outlook send already succeeded -- this can only be a
        # completion-transaction DB failure, never a reason to 500.
        _unknown_after_completion_db_error(
            db, attempt_id=attempt_id, provider_account_id=provider_account_id
        )


def _complete_gmail_send(
    db: Session,
    *,
    thread_id: UUID,
    attempt_id: UUID,
    attempt_created_at: datetime,
    self_address: str,
    recipients: RecipientSet,
    body_text: str,
    headers: dict[str, str],
    provider_message_id: str,
    current_user_id: UUID,
) -> dict[str, Any]:
    """Completion txn for a Gmail send (plan §3.6 step 3): re-lock, re-read
    the attempt under the lock -- if reconciliation already completed it
    during the unlocked provider-call window (a scheduled ingest can deliver
    the sent copy first), skip straight to success. Otherwise upsert the
    sent message the way ingest does, CAS to `sent` + stamp `verified_at`
    (our own MIME IS what sync later normalizes -- no separate verification
    pass is needed), fence, resolve items, single commit, then best-effort
    classification in its own transaction.
    """
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    current_status = db.execute(
        select(ReplyAttempt.status).where(ReplyAttempt.id == attempt_id).with_for_update()
    ).scalar_one()

    if current_status == "sent":
        message = (
            db.execute(
                select(MailMessage).where(
                    MailMessage.thread_id == thread_id,
                    MailMessage.provider_message_id == provider_message_id,
                )
            )
            .scalars()
            .first()
        )
        replied_at = db.execute(
            select(MailThread.replied_at).where(MailThread.id == thread_id)
        ).scalar_one()
        db.commit()
        if message is not None:
            return {
                "thread_id": thread_id,
                "message": _message_out_from_row(message),
                "replied_at": replied_at,
                "resolved_action_items": 0,
            }
        # Extremely unlikely: reconciliation matched via the Message-ID
        # header rather than provider id. Fall back to our own known-good
        # representation rather than surfacing a 500 for a send that truly
        # did succeed.
        return {
            "thread_id": thread_id,
            "message": {
                "id": attempt_id,
                "sent_at": datetime.now(timezone.utc),
                "sender": self_address,
                "recipient": recipients.to,
                "cc": recipients.cc or None,
                "snippet": _snippet(body_text),
                "body_text": body_text,
                "body_html": None,
            },
            "replied_at": replied_at,
            "resolved_action_items": 0,
        }

    now = datetime.now(timezone.utc)
    message_values = {
        "sender": self_address,
        "recipient": recipients.to,
        "cc": recipients.cc or None,
        "bcc": None,
        "sent_at": now,
        "snippet": _snippet(body_text),
        "body_text": body_text,
        "body_html": None,
        "headers": headers,
    }
    insert_stmt = (
        pg_insert(MailMessage)
        .values(thread_id=thread_id, provider_message_id=provider_message_id, **message_values)
        # uq_msg_provider rejects duplicates, it doesn't dedupe them -- an
        # upsert (never a bare insert) is what makes this idempotent against
        # ingest having already landed the same provider_message_id.
        .on_conflict_do_update(
            index_elements=["thread_id", "provider_message_id"], set_=message_values
        )
        .returning(MailMessage.id)
    )
    message_pk = db.execute(insert_stmt).scalar_one()

    # Thread fields advance the way ingest does for a new latest message.
    db.execute(
        update(MailThread)
        .where(MailThread.id == thread_id)
        .values(subject=headers.get("Subject"), last_message_at=now)
    )
    mark_sent(db, attempt_id=attempt_id, provider_message_id=provider_message_id, verified_at=now)
    resolved = advance_fence_and_resolve(
        db, thread_id=thread_id, attempt_created_at=attempt_created_at
    )
    db.commit()

    message_row = db.get(MailMessage, message_pk)
    replied_at = db.execute(
        select(MailThread.replied_at).where(MailThread.id == thread_id)
    ).scalar_one()

    _classify_gmail_message_best_effort(
        db, message=message_row, subject=headers.get("Subject"), user_id=current_user_id
    )

    return {
        "thread_id": thread_id,
        "message": _message_out_from_row(message_row),
        "replied_at": replied_at,
        "resolved_action_items": resolved,
    }


def _complete_outlook_send(
    db: Session,
    *,
    thread_id: UUID,
    account_id: UUID,
    attempt_id: UUID,
    attempt_created_at: datetime,
    draft: dict[str, Any],
    self_address: str,
    recipients: RecipientSet,
    sent_at: datetime,
) -> dict[str, Any]:
    """Completion txn for an Outlook send (plan §3.6 step 3/§3.4): NO
    message row is written -- fence + resolve items, CAS to `sent`
    (`verified_at` stays NULL until correlated sentitems reconciliation),
    and the response is built from the normalized createReply draft,
    rendered ephemerally client-side.
    """
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    current_status = db.execute(
        select(ReplyAttempt.status).where(ReplyAttempt.id == attempt_id).with_for_update()
    ).scalar_one()

    draft_id = draft.get("id")

    if current_status == "sent":
        message = (
            db.execute(
                select(MailMessage).where(
                    MailMessage.thread_id == thread_id,
                    MailMessage.provider_message_id == draft_id,
                )
            )
            .scalars()
            .first()
        )
        replied_at = db.execute(
            select(MailThread.replied_at).where(MailThread.id == thread_id)
        ).scalar_one()
        db.commit()
        if message is not None:
            return {
                "thread_id": thread_id,
                "message": _message_out_from_row(message),
                "replied_at": replied_at,
                "resolved_action_items": 0,
            }
        ephemeral = build_ephemeral_message(
            draft,
            attempt_id=attempt_id,
            self_address=self_address,
            to=recipients.to,
            cc=recipients.cc,
            sent_at=sent_at,
        )
        return {
            "thread_id": thread_id,
            "message": ephemeral,
            "replied_at": replied_at,
            "resolved_action_items": 0,
        }

    mark_sent(db, attempt_id=attempt_id)
    resolved = advance_fence_and_resolve(
        db, thread_id=thread_id, attempt_created_at=attempt_created_at
    )
    db.commit()

    replied_at = db.execute(
        select(MailThread.replied_at).where(MailThread.id == thread_id)
    ).scalar_one()
    # Timeliness (plan §3.4): enqueued on SUCCESS too, not just `unknown` --
    # the authoritative Sent Items copy still needs correlating and
    # classifying, and nothing else proactively chases that down. A dead
    # broker here must not turn an already-sent reply into a 500 -- the
    # reply went out and the completion txn above already committed; this
    # enqueue only affects how quickly the real message shows up.
    try:
        reconcile_reply_attempt.apply_async(
            args=[str(account_id)], countdown=_RECONCILE_COUNTDOWN_SECONDS
        )
    except Exception:
        logger.warning(
            "reconcile_reply_attempt enqueue failed for sent attempt %s", attempt_id
        )
    ephemeral = build_ephemeral_message(
        draft,
        attempt_id=attempt_id,
        self_address=self_address,
        to=recipients.to,
        cc=recipients.cc,
        sent_at=sent_at,
    )
    return {
        "thread_id": thread_id,
        "message": ephemeral,
        "replied_at": replied_at,
        "resolved_action_items": resolved,
    }


@router.post(
    "/thread/{thread_id}/reply-draft",
    response_model=ReplyDraft,
    dependencies=[Depends(user_rate_limit("reply_draft", 20, 300))],
)
def draft_reply(
    thread_id: UUID,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Stateless AI-drafted reply text (plan §3.7). Credential resolution
    reuses `resolve_extraction_credential`'s BYOK semantics wholesale (via
    `resolve_reply_draft_credential`) -- same missing/blocked 409s, same
    server-fallback eligibility as the real extraction pipeline (unlike
    `/settings/llm/test`'s stricter fallback-rejecting rule, which exists
    only to stop the operator's own key from being probed through that
    route).
    """
    thread = db.get(MailThread, thread_id)
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            .order_by(*latest_message_ordering())
        )
        .scalars()
        .all()
    )

    resolved = resolve_reply_draft_credential(db, current_user.id)
    if resolved.credential is None:
        if resolved.blocked_reason is not None:
            raise _http_error(
                "reply_draft_credential_blocked",
                "Your custom LLM endpoint is currently blocked by policy. "
                "Fix or remove it to draft with AI.",
                409,
            )
        raise _http_error(
            "reply_draft_credential_missing",
            "No LLM credential is configured for drafting replies.",
            409,
        )

    attempt = generate_reply_draft(
        credential=resolved.credential, subject=thread.subject, messages=messages
    )

    if resolved.source == "user":
        acc = UsageAccumulator(current_user.id)
        acc.record(
            "reply_draft",
            resolved.credential.provider,
            attempt.usage,
            provider_call_succeeded=attempt.provider_call_succeeded,
        )
        try:
            acc.flush(db)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.warning("reply-draft usage flush failed for user %s", current_user.id)

    if attempt.draft_text is None:
        raise _http_error(
            "reply_draft_failed",
            "Couldn't generate a draft reply. You can still write one yourself.",
            502,
        )

    return {
        "draft_text": attempt.draft_text,
        "provider": resolved.credential.provider,
        "model": resolved.credential.model,
    }
