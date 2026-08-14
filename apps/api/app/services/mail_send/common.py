"""Reply-send building blocks shared by both providers.

Three concerns live here, deliberately kept together because every caller
(the send routes in a later wave, and this wave's reconciliation) needs all
three: server-side recipient computation (plan §3.3 -- the client sends only
``body_text``/``reply_all``, nothing that could target a wrong address), the
``reply_attempt`` compare-and-set lifecycle (§3.5/§3.6 -- every status write
in the whole feature funnels through the functions below), and the fence +
action-resolution helper shared by the completion transaction and every
reconciliation path (§3.5).

Nothing here commits. Every function operates inside whatever transaction
the caller already has open, mirroring ``nlp/persistence.py``'s convention
so the caller controls the boundary.
"""

from __future__ import annotations

import base64
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.db.models import ActionItem, MailThread, ReplyAttempt

# Domain for our own generated RFC 5322 Message-IDs (Gmail only -- Outlook
# correlates on the draft's provider-issued immutable id instead).
_MESSAGE_ID_DOMAIN = "cortexmail.app"

# Cap on address OBJECTS across To+Cc (plan §3.3) -- a distribution group can
# still expand at delivery, which draft/MIME inspection can't see; this cap
# plus the fail-closed target selection below are the actual controls.
MAX_RECIPIENTS = 50

# A `preparing` attempt older than this is presumed abandoned mid-flight
# (browser closed, tab crashed before the inflight CAS) and is fenced to
# `expired` by the NEXT request for the same thread, under the thread lock
# (plan §3.6 step 1) -- never a silent age-out.
PREPARING_TTL = timedelta(seconds=120)


class SendError(Exception):
    """Carries one of the frozen §3.8 error codes for the send/draft routes
    (a later wave) to map onto its HTTPException envelope. Raised by every
    fail-closed check in this module -- recipient resolution, self-address
    resolution, and the cap."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class RecipientSet:
    to: list[str]
    cc: list[str]


@dataclass(frozen=True)
class SendResult:
    """What a provider send module hands back to its caller (a later wave's
    route): the durable attempt id, the provider's own message/draft id, and
    whatever the caller needs to render or persist the "sent" representation.
    """

    attempt_id: UUID
    provider_message_id: str | None
    sent_at: datetime


# ---------------------------------------------------------------------------
# Recipients (plan §3.3) -- server-computed only, fail closed.
# ---------------------------------------------------------------------------


def _clean_header_value(value: str | None) -> str:
    """Drop a header value outright if it contains CR/LF. Feeding a value
    with embedded line breaks to ``getaddresses`` is a header-injection
    vector -- the safe response is to treat it as though the header weren't
    present at all, not to try to sanitize it in place."""
    if not value:
        return ""
    if "\r" in value or "\n" in value:
        return ""
    return value


def header_value(headers: dict[str, Any] | None, name: str) -> str | None:
    """Case-insensitive lookup into a stored ``headers`` map -- Gmail's raw
    header map preserves provider casing, so an exact-key lookup would miss
    e.g. a lowercase ``reply-to``."""
    target = name.lower()
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == target and isinstance(value, str):
            return value
    return None


def _parse_addresses(raw_value: str | None) -> list[str]:
    """``email.utils.getaddresses`` over a RAW header value, each candidate
    validated with ``email-validator``. Invalid addresses are discarded --
    one malformed Cc entry must not sink an otherwise-good reply -- and the
    normalized (lowercased domain, canonical) form is returned so later
    case-insensitive dedup/self-removal is just a lowercase compare.
    """
    cleaned = _clean_header_value(raw_value)
    if not cleaned:
        return []
    out: list[str] = []
    for _display_name, addr in getaddresses([cleaned]):
        if not addr:
            continue
        try:
            result = validate_email(addr, check_deliverability=False)
        except EmailNotValidError:
            continue
        out.append(result.normalized)
    return out


def resolve_self_address(account: Any) -> str:
    """The mailbox's own address for outgoing mail: Gmail's is
    ``external_user_id``, Outlook's is ``display_email`` -- populated by
    every supported OAuth path but NULLABLE in the DB (an application
    guarantee, not a DB invariant, per plan §2). Fails closed -- a wrong or
    missing self address can mistarget reply-all and mis-filter the target
    walk below.
    """
    raw = account.external_user_id if account.provider == "gmail" else account.display_email
    cleaned = _clean_header_value(raw)
    if not cleaned:
        raise SendError(
            "account_identity_unavailable", "no usable self address for this account"
        )
    try:
        result = validate_email(cleaned, check_deliverability=False)
    except EmailNotValidError as exc:
        raise SendError(
            "account_identity_unavailable", "self address failed validation"
        ) from exc
    return result.normalized


def select_reply_target(messages: list[Any], self_address: str) -> Any:
    """Walk ``messages`` newest-first and return the first whose parsed AND
    validated sender is not ``self_address`` (case-insensitive). Raises
    ``SendError("no_reply_target")`` when nothing qualifies -- an
    all-self-sent thread (e.g. notes to self) has nothing to reply to.
    """
    ordered = sorted(
        messages, key=lambda m: m.sent_at or m.created_at, reverse=True
    )
    self_lower = self_address.lower()
    for message in ordered:
        sender_addrs = _parse_addresses(message.sender)
        if any(addr.lower() != self_lower for addr in sender_addrs):
            return message
    raise SendError(
        "no_reply_target", "no message in this thread has a usable non-self sender"
    )


def _dedupe_drop(addrs: list[str], *, drop: str, exclude: list[str] = ()) -> list[str]:
    seen = {a.lower() for a in exclude}
    drop_lower = drop.lower()
    out: list[str] = []
    for addr in addrs:
        key = addr.lower()
        if key == drop_lower or key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def compute_recipients(
    *, target_message: Any, self_address: str, reply_all: bool
) -> RecipientSet:
    """Server-side recipient computation (plan §3.3): all ``Reply-To``
    mailboxes honored (falling back to the sender when absent); reply-all
    additionally folds in the target message's original To/Cc into a Cc
    bucket; self is removed case-insensitively from both buckets; Bcc is
    never read. Fails closed with ``SendError`` if the To slot would empty,
    or if the total exceeds ``MAX_RECIPIENTS``.
    """
    headers = target_message.headers or {}
    reply_to_addrs = _parse_addresses(header_value(headers, "Reply-To"))
    sender_addrs = _parse_addresses(target_message.sender)
    to_targets = reply_to_addrs or sender_addrs
    if not to_targets:
        raise SendError("no_reply_target", "reply target has no usable address")

    to_set = _dedupe_drop(to_targets, drop=self_address)
    if not to_set:
        raise SendError(
            "no_reply_target", "reply target resolved to only the self address"
        )

    cc_set: list[str] = []
    if reply_all:
        original_to = _parse_addresses(header_value(headers, "To"))
        original_cc = _parse_addresses(header_value(headers, "Cc"))
        cc_set = _dedupe_drop(original_to + original_cc, drop=self_address, exclude=to_set)

    total = len(to_set) + len(cc_set)
    if total > MAX_RECIPIENTS:
        raise SendError(
            "too_many_recipients", f"{total} addresses exceeds the {MAX_RECIPIENTS} cap"
        )

    return RecipientSet(to=to_set, cc=cc_set)


# ---------------------------------------------------------------------------
# Message-ID generation + Gmail MIME assembly (plan §3.1).
# ---------------------------------------------------------------------------


def generate_message_id() -> str:
    """Our own RFC 5322 Message-ID, generated and recorded on the attempt
    row BEFORE the Gmail send call (plan §3.1/§3.5) -- Gmail's send response
    doesn't let us choose the outgoing message's id, so this is what
    reconciliation actually matches on via the sent message's own
    Message-ID header. Not used for Outlook, which correlates on the
    provider-issued draft immutable id instead."""
    return f"<{uuid_lib.uuid4()}@{_MESSAGE_ID_DOMAIN}>"


def threading_headers(target_message: Any) -> tuple[str | None, str | None]:
    """`In-Reply-To`/`References` derived from the reply target's OWN stored
    headers -- `In-Reply-To` is its Message-ID, `References` appends that
    onto whatever References chain it already carried (RFC 5322 §5.1). Both
    are stored provider data, so they go through the same CRLF guard as the
    address headers (F4) -- a value with an embedded line break is dropped
    outright rather than risking an interior CRLF in the assembled MIME."""
    headers = target_message.headers or {}
    target_msg_id = _clean_header_value(header_value(headers, "Message-ID"))
    in_reply_to = target_msg_id.strip() or None
    existing_refs = _clean_header_value(header_value(headers, "References"))
    parts = []
    if existing_refs.strip():
        parts.append(existing_refs.strip())
    if in_reply_to:
        parts.append(in_reply_to)
    references = " ".join(parts) if parts else None
    return in_reply_to, references


def _reply_subject(subject: str | None) -> str:
    """`Re: <original>` -- idempotent: an already-`Re:`-prefixed subject is
    left alone rather than getting a second prefix. Subject is stored
    provider data too (F4): a value with an embedded CR/LF is dropped by the
    same guard as the address headers, which lands it on the same "Re:"
    fallback a genuinely blank subject already gets."""
    cleaned = _clean_header_value(subject).strip()
    if not cleaned:
        return "Re:"
    if cleaned.lower().startswith("re:"):
        return cleaned
    return f"Re: {cleaned}"


def build_reply_mime(
    *,
    self_address: str,
    recipients: RecipientSet,
    subject: str | None,
    body_text: str,
    in_reply_to: str | None,
    references: str | None,
    message_id: str,
) -> str:
    """Assemble the outgoing MIME message and return it base64url-encoded,
    ready for `GmailClient.send_message`'s `raw` field."""
    msg = EmailMessage()
    msg["From"] = self_address
    msg["To"] = ", ".join(recipients.to)
    if recipients.cc:
        msg["Cc"] = ", ".join(recipients.cc)
    msg["Subject"] = _reply_subject(subject)
    msg["Message-ID"] = message_id
    # `in_reply_to`/`references` are already cleaned by `threading_headers`
    # at the call site, but guarded again here too (F4) -- this is the
    # actual assembly boundary, and compat32's `as_bytes()` will happily
    # emit an interior CRLF from any caller that skips that step.
    cleaned_in_reply_to = _clean_header_value(in_reply_to)
    if cleaned_in_reply_to:
        msg["In-Reply-To"] = cleaned_in_reply_to
    cleaned_references = _clean_header_value(references)
    if cleaned_references:
        msg["References"] = cleaned_references
    msg.set_content(body_text)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# reply_attempt lifecycle -- every write to the table goes through here.
# ---------------------------------------------------------------------------


def start_attempt(
    db: Session,
    *,
    thread_id: UUID,
    user_id: UUID,
    provider: str,
    gmail_message_id_header: str | None = None,
) -> ReplyAttempt:
    """Insert the `preparing` row (plan §3.6 step 1). Does not commit -- the
    caller's Txn A commits it, releasing the thread lock before any provider
    call."""
    attempt = ReplyAttempt(
        thread_id=thread_id,
        user_id=user_id,
        provider=provider,
        status="preparing",
        gmail_message_id_header=gmail_message_id_header,
    )
    db.add(attempt)
    db.flush()
    return attempt


def expire_stale_preparing(db: Session, *, thread_id: UUID, now: datetime | None = None) -> int:
    """`preparing` -> `expired` for rows older than `PREPARING_TTL` on this
    thread -- an explicit fencing transition (never a silent age-out), run
    under the thread lock before a new request admits itself. Returns the
    number of rows expired.

    `now` lets the caller pin ONE instant for both this call and the
    matching `find_blocking_attempt` call right after it -- the two
    predicates are meant to be exact complements, and each deriving its own
    `datetime.now()` would let a row fall in the gap between them (neither
    expired nor blocking) if the clock ticked past the TTL boundary between
    the two calls."""
    cutoff = (now or datetime.now(timezone.utc)) - PREPARING_TTL
    result = db.execute(
        update(ReplyAttempt)
        .where(
            ReplyAttempt.thread_id == thread_id,
            ReplyAttempt.status == "preparing",
            ReplyAttempt.created_at < cutoff,
        )
        .values(status="expired", updated_at=text("now()"))
    )
    return result.rowcount or 0


def find_blocking_attempt(
    db: Session, *, thread_id: UUID, now: datetime | None = None
) -> ReplyAttempt | None:
    """The current in-flight guard's blocker, if any: a `preparing` row
    younger than `PREPARING_TTL`, or ANY `inflight`/`unknown` row regardless
    of age (time never unblocks an attempt that may have reached the
    provider). Call `expire_stale_preparing` first under the same lock so a
    stale `preparing` row never blocks. Newest first -- there should only
    ever be at most one live blocker, but this is defensive.

    `now` should be the SAME instant passed to the `expire_stale_preparing`
    call immediately before this one -- see that function's docstring for
    why the two must share a cutoff.
    """
    cutoff = (now or datetime.now(timezone.utc)) - PREPARING_TTL
    return (
        db.execute(
            select(ReplyAttempt)
            .where(
                ReplyAttempt.thread_id == thread_id,
                or_(
                    and_(ReplyAttempt.status == "preparing", ReplyAttempt.created_at >= cutoff),
                    ReplyAttempt.status.in_(("inflight", "unknown")),
                ),
            )
            .order_by(ReplyAttempt.created_at.desc())
        )
        .scalars()
        .first()
    )


def abandon_attempt(db: Session, *, attempt_id: UUID) -> bool:
    """CAS the explicit user-confirmed override (plan §3.6's "Abandon and
    send anyway"): only a still-blocking attempt (`preparing`, `inflight`,
    `unknown`) can be abandoned. The caller must already have verified,
    under the thread lock, that `attempt_id` is the CURRENT blocker for this
    user/thread via `find_blocking_attempt` -- a stale id from a second tab
    must never bypass a newer blocker (P5-2). Abandoned attempts remain
    matchable by reconciliation (fence monotonic, resolution idempotent), so
    an overridden-but-delivered send still records truthfully.
    """
    result = db.execute(
        update(ReplyAttempt)
        .where(
            ReplyAttempt.id == attempt_id,
            ReplyAttempt.status.in_(("preparing", "inflight", "unknown")),
        )
        .values(status="abandoned", updated_at=text("now()"))
    )
    return bool(result.rowcount)


def transition_to_inflight(db: Session, *, attempt_id: UUID) -> bool:
    """`preparing` -> `inflight`, immediately before the first provider
    call. Zero rowcount means the attempt was expired or superseded while
    this process was suspended -- the caller must abort with
    `reply_superseded` and send nothing."""
    result = db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id, ReplyAttempt.status == "preparing")
        .values(status="inflight", updated_at=text("now()"))
    )
    return bool(result.rowcount)


def record_provider_message_id(db: Session, *, attempt_id: UUID, provider_message_id: str) -> None:
    """Write the provider's own id as soon as it's known -- Gmail's send
    response id, or (BETWEEN Outlook's createReply and send calls) the
    draft's immutable id. Unconditional by id: this is informational, not a
    status transition, and the attempt is guaranteed to still exist (we hold
    its id from the CAS that put us on the provider-call path)."""
    db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id)
        .values(provider_message_id=provider_message_id, updated_at=text("now()"))
    )


def mark_sent(
    db: Session,
    *,
    attempt_id: UUID,
    provider_message_id: str | None = None,
    verified_at: datetime | None = None,
) -> bool:
    """`inflight`/`abandoned` -> `sent` (widened per plan §3.5: mail
    provably left, `sent` is terminal truth, and reconciliation may win this
    race before the completion transaction gets here -- losing to an
    already-`sent` row is handled by the caller treating rowcount 0 as
    "already done", not a failure).

    This is the SEND ROUTE's completion-transaction CAS specifically -- it
    never needs to settle an `unknown` attempt (an `unknown` row only exists
    once the route has already returned an ambiguous 502; the completion
    transaction that calls this ran and returned before that). Reconciliation
    settling an `unknown` attempt from the provider's own sent copy uses
    `mark_sent_reconciled` below instead (R-1, final review)."""
    values: dict[str, Any] = {"status": "sent", "updated_at": text("now()")}
    if provider_message_id is not None:
        values["provider_message_id"] = provider_message_id
    if verified_at is not None:
        values["verified_at"] = verified_at
    result = db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id, ReplyAttempt.status.in_(("inflight", "abandoned")))
        .values(**values)
    )
    return bool(result.rowcount)


def mark_sent_reconciled(
    db: Session,
    *,
    attempt_id: UUID,
    provider_message_id: str | None = None,
    verified_at: datetime | None = None,
) -> bool:
    """`inflight`/`abandoned`/`unknown` -> `sent` -- RECONCILIATION ONLY
    (R-1, final review). `mark_sent`'s CAS was `WHERE status IN
    ('inflight','abandoned')`, but reconciliation's whole purpose is
    settling `unknown` attempts from the provider's authoritative sent
    copy -- an `unknown` row could never win that CAS, so it stayed
    reconciliation-eligible (and kept the in-flight guard closed) forever
    even after a matching message was found. The completion-transaction
    route path keeps `mark_sent`'s narrower semantics unchanged -- it never
    observes an `unknown` attempt itself.

    Zero rowcount here is NOT automatically "someone else already did it"
    the way it is for `mark_sent` -- the caller must re-read the row under
    the same lock to confirm it's actually `sent` before treating settlement
    as complete; any other status is a genuine anomaly.
    """
    values: dict[str, Any] = {"status": "sent", "updated_at": text("now()")}
    if provider_message_id is not None:
        values["provider_message_id"] = provider_message_id
    if verified_at is not None:
        values["verified_at"] = verified_at
    result = db.execute(
        update(ReplyAttempt)
        .where(
            ReplyAttempt.id == attempt_id,
            ReplyAttempt.status.in_(("inflight", "abandoned", "unknown")),
        )
        .values(**values)
    )
    return bool(result.rowcount)


def mark_failed(db: Session, *, attempt_id: UUID) -> bool:
    """`inflight`/`abandoned` -> `failed` (definite provider rejection,
    widened the same way as `mark_sent` per P7-5 -- a provably-unsent
    attempt must not linger `abandoned` and reconciliation-eligible
    forever)."""
    result = db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id, ReplyAttempt.status.in_(("inflight", "abandoned")))
        .values(status="failed", updated_at=text("now()"))
    )
    return bool(result.rowcount)


def mark_unknown(db: Session, *, attempt_id: UUID) -> bool:
    """`inflight` -> `unknown` ONLY -- deliberately NOT widened to include
    `abandoned`: the ambiguous outcome must preserve a concurrent abandon
    rather than restore the guard, per plan §3.5. Regardless of whether this
    CAS applies, the caller still enqueues reconciliation -- an abandoned
    row stays matchable on its own."""
    result = db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id, ReplyAttempt.status == "inflight")
        .values(status="unknown", updated_at=text("now()"))
    )
    return bool(result.rowcount)


def stamp_verified(db: Session, *, attempt_id: UUID) -> None:
    """Stamp `verified_at` without a status change -- Gmail's completion
    path (our assembled MIME IS what sync later normalizes, so it's
    verified the moment the sent message lands) and reconciliation's
    Gmail-recovery branch (a crashed completion transaction whose provider
    call actually succeeded)."""
    db.execute(
        update(ReplyAttempt)
        .where(ReplyAttempt.id == attempt_id)
        .values(verified_at=text("now()"), updated_at=text("now()"))
    )


# ---------------------------------------------------------------------------
# Shared fence + action-resolution helper (plan §3.5) -- used by BOTH the
# send completion transaction (a later wave) and every reconciliation path
# in this wave.
# ---------------------------------------------------------------------------


def advance_fence_and_resolve(
    db: Session, *, thread_id: UUID, attempt_created_at: datetime
) -> int:
    """Advance `mail_thread.replied_at` to `greatest(replied_at,
    attempt_created_at)` -- the moment the user hit send, on OUR clock, per
    plan §3.5 -- and resolve every OPEN `kind='reply'` OR `kind IS NULL`
    action item on the thread to `done`. Returns the number of items
    resolved.

    The caller MUST already hold the `MailThread` row lock (frozen
    repo-wide lock order: `MailThread` -> `ActionItem`, same as
    `set_thread_done` / `extraction_run.py`) before calling this -- it
    issues the `ActionItem` UPDATE unconditionally on that assumption.
    Idempotent: re-running against an already-fenced thread with the same
    or an earlier `attempt_created_at` is a no-op (`greatest()` never moves
    backward, and a re-resolved item has no open rows left to touch).
    """
    now = datetime.now(timezone.utc)
    db.execute(
        update(MailThread)
        .where(MailThread.id == thread_id)
        .values(replied_at=_greatest_replied_at(attempt_created_at))
    )
    result = db.execute(
        update(ActionItem)
        .where(
            ActionItem.thread_id == thread_id,
            ActionItem.status == "open",
            or_(ActionItem.kind == "reply", ActionItem.kind.is_(None)),
        )
        .values(status="done", status_at=now)
    )
    return result.rowcount or 0


def _greatest_replied_at(attempt_created_at: datetime):
    """`GREATEST(replied_at, :attempt_created_at)` -- Postgres' GREATEST
    ignores NULL arguments (returns the other one), unlike SQL MAX, so this
    is correct even on a thread's first-ever reply where `replied_at` is
    still NULL."""
    return func.greatest(MailThread.replied_at, attempt_created_at)
