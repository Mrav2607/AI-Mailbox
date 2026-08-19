"""Agenda routes: list extracted action items, flip their resolution status,
and queue a manual extraction backfill sweep.

Visibility rule (frozen, shared by the list AND the counts aggregate below --
mailbox.py's ``GET /mail/counts`` imports ``compute_action_counts`` so the two
endpoints can never drift on it): an item is agenda-visible iff
``outcome="extracted"`` AND its SOURCE message's CURRENT classification label
is in ``ACTION_LABELS``, AND the thread join asserts
``MailThread.user_id == current_user.id``. Classification is upserted (one row
per ``message_id``), so a plain join on ``ActionItem.message_id`` always reads
the message's current label -- no "latest classification" ordering needed.
There is deliberately NO ``done_at`` filter here: thread-done resolves items by
write (see ``mailbox.set_thread_done``), not by hiding them from this query.

Reclassify semantics (frozen): ``mailbox.reclassify_thread`` relabels only a
thread's LATEST message, so it only hides/reveals the item sourced from THAT
message -- the common case, since extraction follows classification of new
mail. An item sourced from an older message in the same thread is unaffected
by a reclassify; it's only resolved item-level, via the status route below.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.ratelimit import user_rate_limit
from app.deps import get_current_user, get_db
from app.db.models import (
    ActionItem,
    AppUser,
    Classification,
    MailMessage,
    MailThread,
    ProviderAccount,
)
from app.db.schemas.actions import (
    ActionList,
    ActionStatusOut,
    ActionStatusRequest,
    BackfillQueued,
)
from app.services.nlp.extractor import ACTION_LABELS
from app.services.nlp.providers import extraction_available, extraction_feature_enabled
from app.workers.tasks_nlp import extract_actions_for_user

router = APIRouter(prefix="/mail/actions")

_VALID_STATUSES = ("open", "done", "dismissed")
_MAX_LIST_LIMIT = 500
_MAX_BACKFILL_LIMIT = 200
_MAX_SINCE_DAYS = 365


def _visibility_predicates(user_id: UUID) -> list:
    """Shared WHERE fragment for both the list query and the counts
    aggregate. Requires the caller to have already joined ``Classification``
    (on ``ActionItem.message_id``) and ``MailThread`` (on
    ``ActionItem.thread_id``).

    ``ActionItem.user_id == user_id`` lives here alongside the thread-owner
    check, not just bolted onto the list query -- both feed
    ``compute_action_counts`` too, so a hypothetical row whose ``user_id``
    disagrees with its thread's owner (an app-level convention, not a DB
    invariant -- see the agenda cursor pagination plan, D6) gets excluded
    from the list AND the badge count consistently, never one without the
    other. It's also what makes ``ix_action_item_user_agenda``
    (``ActionItem``-leading) able to serve this query at all: the list used
    to filter ownership only via ``MailThread.user_id``, which no
    ``ActionItem``-leading index can serve."""
    return [
        ActionItem.outcome == "extracted",
        Classification.label.in_(ACTION_LABELS),
        MailThread.user_id == user_id,
        ActionItem.user_id == user_id,
    ]


def compute_action_counts(db: Session, user_id: UUID) -> dict:
    """``{"open": ..., "overdue": ...}`` for a user's agenda, always
    cross-account -- one aggregate query shared by ``GET /mail/actions`` and
    ``GET /mail/counts`` (mailbox.py imports this) so the two can't disagree
    on the visibility rule. ``overdue`` is visible AND open AND
    ``due_at < now()``, independent of any status filter a caller applies to
    the list."""
    now = datetime.now(timezone.utc)
    open_case = case((ActionItem.status == "open", 1), else_=0)
    overdue_case = case(
        (and_(ActionItem.status == "open", ActionItem.due_at < now), 1),
        else_=0,
    )
    open_count, overdue_count = db.execute(
        select(
            func.coalesce(func.sum(open_case), 0),
            func.coalesce(func.sum(overdue_case), 0),
        )
        .select_from(ActionItem)
        .join(Classification, Classification.message_id == ActionItem.message_id)
        .join(MailThread, MailThread.id == ActionItem.thread_id)
        .where(*_visibility_predicates(user_id))
    ).one()
    return {"open": int(open_count), "overdue": int(overdue_count)}


@dataclass(frozen=True)
class AgendaCursor:
    """Decoded, validated agenda cursor -- the last row's sort key from the
    page that minted it. ``due_at`` being ``None`` is itself the signal that
    the cursor was minted in the null-due segment (D2); there's no separate
    phase flag."""

    due_at: datetime | None
    created_at: datetime
    id: UUID


def _invalid_cursor() -> HTTPException:
    # A fresh exception per call, not a shared module-level instance -- this
    # route runs in FastAPI's threadpool, and `raise ... from exc` mutates
    # `__cause__`/`__traceback__` on whatever it raises, so a shared instance
    # would let concurrent requests stomp each other's traceback.
    return HTTPException(status_code=422, detail="invalid cursor")


def _encode_agenda_cursor(status: str, item: ActionItem) -> str:
    """Opaque bookmark for the row after ``item`` in the agenda sort order.
    base64url(JSON) -- not signed or encrypted, since it carries nothing
    secret (timestamps + an id the caller already saw) and the query it
    drives is always re-scoped to the authenticated user (D3)."""
    payload = {
        "v": 1,
        "s": status,
        "d": item.due_at.isoformat() if item.due_at is not None else None,
        "c": item.created_at.isoformat(),
        "i": str(item.id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_agenda_cursor(token: str, status: str) -> AgendaCursor:
    """Strictly decode + validate a cursor token against the caller's current
    ``status`` filter. Any failure -- malformed base64/JSON, missing/unknown
    fields, wrong types, a naive (tz-less) timestamp, a non-UUID id, or a
    status that doesn't match ``status`` -- raises the same plain 422 that
    never echoes the token back (D3); a cursor minted under one status can't
    silently continue paging under another."""
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise _invalid_cursor() from exc

    if not isinstance(payload, dict):
        raise _invalid_cursor()
    if payload.get("v") != 1 or payload.get("s") != status:
        raise _invalid_cursor()

    created_raw, due_raw, id_raw = payload.get("c"), payload.get("d"), payload.get("i")
    if not isinstance(created_raw, str) or not isinstance(id_raw, str):
        raise _invalid_cursor()
    if due_raw is not None and not isinstance(due_raw, str):
        raise _invalid_cursor()

    try:
        created_at = datetime.fromisoformat(created_raw)
        due_at = datetime.fromisoformat(due_raw) if due_raw is not None else None
        row_id = UUID(id_raw)
    except (TypeError, ValueError) as exc:
        raise _invalid_cursor() from exc

    if created_at.tzinfo is None or (due_at is not None and due_at.tzinfo is None):
        raise _invalid_cursor()

    return AgendaCursor(due_at=due_at, created_at=created_at, id=row_id)


def _cursor_predicate(cur: AgendaCursor):
    """The D2 two-phase keyset predicate matching the route's ORDER BY
    (``due_at ASC NULLS LAST, created_at DESC, id DESC``). Which branch
    applies is picked by whether the cursor itself has a ``due_at`` --
    non-null means "resume the non-null segment, then fall into the null
    segment"; null means "resume within the null segment"."""
    tiebreak = or_(
        ActionItem.created_at < cur.created_at,
        and_(ActionItem.created_at == cur.created_at, ActionItem.id < cur.id),
    )
    if cur.due_at is not None:
        return or_(
            ActionItem.due_at > cur.due_at,
            and_(ActionItem.due_at == cur.due_at, tiebreak),
            ActionItem.due_at.is_(None),
        )
    return and_(ActionItem.due_at.is_(None), tiebreak)


@router.get("", response_model=ActionList)
def list_actions(
    status: str = Query(default="open"),
    limit: int = Query(default=200, ge=1, le=_MAX_LIST_LIMIT),
    cursor: str | None = Query(default=None),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The agenda board: every visible action item for the caller in
    ``status`` (default ``"open"``), ordered ``due_at ASC NULLS LAST,
    created_at DESC, id DESC`` for a deterministic deadline-first order with a
    tiebreak. ``cursor`` is an opaque bookmark from a previous page's
    ``next_cursor`` -- treat it as a token, don't parse it; pass it back
    unmodified to keep paging. ``counts`` is always the full open/overdue
    tally regardless of ``status``, so switching status filters in the
    console doesn't make the badge counts flicker.
    """
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{status}'. Valid: {', '.join(_VALID_STATUSES)}",
        )

    predicates = [*_visibility_predicates(current_user.id), ActionItem.status == status]
    if cursor is not None:
        predicates.append(_cursor_predicate(_decode_agenda_cursor(cursor, status)))

    query = (
        select(
            ActionItem,
            MailThread.subject.label("thread_subject"),
            MailThread.provider.label("provider"),
            MailThread.last_message_at.label("last_message_at"),
            MailMessage.sender.label("sender"),
            ProviderAccount.display_email.label("display_email"),
            ProviderAccount.external_user_id.label("external_user_id"),
            Classification.label.label("label"),
        )
        .join(Classification, Classification.message_id == ActionItem.message_id)
        .join(MailThread, MailThread.id == ActionItem.thread_id)
        .join(MailMessage, MailMessage.id == ActionItem.message_id)
        .join(ProviderAccount, ProviderAccount.id == MailThread.provider_account_id)
        .where(*predicates)
        .order_by(
            ActionItem.due_at.asc().nullslast(),
            ActionItem.created_at.desc(),
            ActionItem.id.desc(),
        )
        .limit(limit)
    )
    rows = db.execute(query).all()
    items = [
        {
            "id": row.ActionItem.id,
            "thread_id": row.ActionItem.thread_id,
            "message_id": row.ActionItem.message_id,
            "kind": row.ActionItem.kind,
            "title": row.ActionItem.title,
            "due_at": row.ActionItem.due_at,
            "due_precision": row.ActionItem.due_precision,
            "due_raw": row.ActionItem.due_raw,
            "amount": row.ActionItem.amount,
            "currency": row.ActionItem.currency,
            "source_confidence": row.ActionItem.source_confidence,
            "status": row.ActionItem.status,
            "created_at": row.ActionItem.created_at,
            "thread_subject": row.thread_subject,
            "sender": row.sender,
            "provider": row.provider,
            "last_message_at": row.last_message_at,
            # display_email fallback identity rule, same as the mailbox list
            # routes: external_user_id (Outlook's tid:oid) is only a fallback.
            "account_email": row.display_email or row.external_user_id,
            "label": row.label,
        }
        for row in rows
    ]
    next_cursor = (
        _encode_agenda_cursor(status, rows[-1].ActionItem) if len(rows) == limit else None
    )
    return {
        "items": items,
        "counts": compute_action_counts(db, current_user.id),
        "next_cursor": next_cursor,
    }


@router.post("/{action_id}/status", response_model=ActionStatusOut)
def set_action_status(
    action_id: UUID,
    payload: ActionStatusRequest,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Mark an action item done, dismissed, or reopen it.

    404 (not 403) covers a missing row, another user's row, and a row that
    hasn't settled as ``extracted`` -- none of those are the caller's to
    resolve, and 404 doesn't leak which case it was. ``status_at`` is stamped
    ``now()`` on any non-open status and cleared back to ``None`` on reopen.
    """
    item = db.execute(
        select(ActionItem)
        .join(MailThread, MailThread.id == ActionItem.thread_id)
        .where(
            ActionItem.id == action_id,
            ActionItem.outcome == "extracted",
            MailThread.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Action item not found")

    item.status = payload.status
    item.status_at = datetime.now(timezone.utc) if payload.status != "open" else None
    db.commit()

    return {"action_id": action_id, "status": item.status, "status_at": item.status_at}


@router.post(
    "/backfill",
    response_model=BackfillQueued,
    status_code=202,
    # Worst-case forced amplification is 3 x 200/min -- judged acceptable for
    # a single-operator deployment; per-user daily quotas are deferred.
    dependencies=[Depends(user_rate_limit("actions-backfill", 3, 60))],
)
def backfill_actions(
    limit: int = Query(default=100, ge=1, le=_MAX_BACKFILL_LIMIT),
    force: bool = False,
    since_days: int = Query(default=30, ge=1, le=_MAX_SINCE_DAYS),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Queue an action-extraction sweep for the caller off the request path.

    Always enqueued, never run inline: every candidate needs its own LLM
    call, so unlike the classification backfill there's no small-batch inline
    path here. 409s before enqueuing anything -- distinguishing the operator
    flag being off from the flag being on but this user having no usable
    credential (no BYOK row, no server fallback) -- config absence must
    never burn a queue slot either way.
    """
    if not extraction_feature_enabled():
        raise HTTPException(status_code=409, detail="action extraction disabled")
    if not extraction_available(db, current_user.id):
        raise HTTPException(status_code=409, detail="no LLM credential configured")

    task = cast(Any, extract_actions_for_user).delay(
        user_id=str(current_user.id),
        limit=limit,
        force=force,
        since_days=since_days,
    )
    return {"status": "queued", "task_id": task.id}
