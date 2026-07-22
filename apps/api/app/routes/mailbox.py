from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, desc, or_, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import user_rate_limit
from app.deps import get_db, get_current_user
from app.db.schemas.mailbox import (
    BackfillDone,
    Counts,
    Queued,
    Reclassified,
    Search,
    SyncHealth,
    SyncRun,
    SyncRunList,
    TaskStatus,
    ThreadDetail,
    ThreadDone,
    Triage,
)
from app.db.models import (
    AppUser,
    Classification,
    MailMessage,
    MailSyncRun,
    MailThread,
    ProviderAccount,
)
from app.workers.celery_app import celery_app
from app.workers.tasks_ingest import read_dispatcher_heartbeat
from app.workers.tasks_nlp import backfill_threads_for_user, classify_latest_threads
from app.services.nlp.backfill import (
    latest_label_subquery,
    latest_message_ordering,
    latest_messages_by_thread,
    run_backfill,
)
from app.services.nlp.classifier import LABELS
from app.services.nlp.persistence import upsert_classification
from app.services.sync_runs import (
    ACTIVE_SYNC_STATUSES,
    active_syncs,
    expire_stale_sync,
    now_utc,
    start_sync_run,
    sync_payload,
)

router = APIRouter(prefix="/mail")

# Upper bounds on caller-supplied counts so a single request can't ask the DB
# (or a Gmail pull) for an unbounded amount of work.
_MAX_PAGE_LIMIT = 200
_MAX_INGEST_RESULTS = 500
# Backfills up to this many threads run inline and return counts immediately;
# anything bigger goes to the Celery worker (a 500-thread Gemini run can hold
# a request open for minutes).
_MAX_INLINE_BACKFILL = 50
# Valid triage filters: every classifier label plus the synthetic buckets.
_TRIAGE_BUCKETS = frozenset(LABELS) | {"all", "unclassified", "done"}
# Valid triage orderings: recency (default) or grouped by connected account.
_TRIAGE_SORTS = frozenset({"recency", "account"})
# Backfill scopes by classification state, not done-ness, so it doesn't take
# the "done" bucket.
_BACKFILL_BUCKETS = _TRIAGE_BUCKETS - {"done"}
# Classifier backends a caller may request per run (see services.nlp.classify).
_CLASSIFIER_BACKENDS = frozenset({"local", "gemini", "heuristic", "auto"})
# Marks a label set by a human in the console rather than a model, so it's
# distinguishable from a real prediction and never overwritten by a backfill
# (which only touches unclassified messages unless forced).
_OPERATOR_MODEL_VERSION = "user-override"


class ReclassifyRequest(BaseModel):
    label: str


class DoneRequest(BaseModel):
    done: bool


def _recency_order() -> tuple:
    """Shared ORDER BY for triage and search: newest first, with an id
    tiebreak.

    created_at is transaction-stamped (server_default now()), so a bulk
    ingest can land a whole batch of threads on the same timestamp -- the id
    tiebreak keeps offset pagination deterministic across pages even then.
    """
    return (
        MailThread.last_message_at.desc().nullslast(),
        MailThread.created_at.desc(),
        MailThread.id.desc(),
    )


def _assemble_triage_items(db: Session, threads: list[MailThread]) -> list[dict]:
    """Build triage item dicts for an ordered list of threads: each thread's
    latest message plus that message's latest classification. Shared by the
    triage and search endpoints so both return the same shape."""
    thread_ids = [t.id for t in threads]
    latest_message_by_thread = latest_messages_by_thread(
        db,
        thread_ids,
        # The list only needs sender and snippet -- don't drag full bodies or
        # headers across the wire for a page that renders one line of each.
        columns=(
            MailMessage.id,
            MailMessage.thread_id,
            MailMessage.snippet,
            MailMessage.sender,
        ),
    )
    message_ids = [m.id for m in latest_message_by_thread.values()]
    latest_classifications = (
        db.execute(
            select(Classification)
            .where(Classification.message_id.in_(message_ids))
            .order_by(desc(Classification.created_at))
        )
        .scalars()
        .all()
        if message_ids
        else []
    )
    classifications_by_msg = {}
    for cls in latest_classifications:
        if cls.message_id not in classifications_by_msg:
            classifications_by_msg[cls.message_id] = cls

    # One lookup for every account these threads belong to, instead of a
    # query per thread -- a thread's account always exists (FK cascade).
    account_ids = {t.provider_account_id for t in threads}
    account_emails = (
        dict(
            db.execute(
                select(ProviderAccount.id, ProviderAccount.external_user_id).where(
                    ProviderAccount.id.in_(account_ids)
                )
            ).all()
        )
        if account_ids
        else {}
    )

    items = []
    for thread in threads:
        latest_message = latest_message_by_thread.get(thread.id)
        classification = classifications_by_msg.get(latest_message.id) if latest_message else None
        items.append(
            {
                "thread_id": str(thread.id),
                "subject": thread.subject,
                "last_message_at": thread.last_message_at,
                "latest_message_snippet": latest_message.snippet if latest_message else None,
                "latest_message_sender": latest_message.sender if latest_message else None,
                "classification": {
                    "label": classification.label if classification else None,
                    "confidence": float(classification.confidence) if classification and classification.confidence is not None else None,
                    "model_version": classification.model_version if classification else None,
                },
                "account_email": account_emails.get(thread.provider_account_id),
            }
        )
    return items


@router.get("/triage", response_model=Triage)
def get_triage(
    bucket: str = "needs_reply",
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    provider_account_id: UUID | None = Query(default=None),
    sort: str = Query(default="recency"),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Fetch recent threads for the authenticated user with latest classification label.
    """
    if bucket not in _TRIAGE_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid bucket '{bucket}'. Valid: {sorted(_TRIAGE_BUCKETS)}",
        )
    if sort not in _TRIAGE_SORTS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sort '{sort}'. Valid: {sorted(_TRIAGE_SORTS)}",
        )
    query = select(MailThread).where(MailThread.user_id == current_user.id)
    # Filter by bucket in SQL, before the limit, so a specific-label view returns
    # up to `limit` matching threads instead of whatever matches happen to fall
    # inside the `limit` most-recent threads overall. Done threads live only in
    # the `done` bucket; every open bucket (including `all`) excludes them.
    if bucket == "done":
        query = query.where(MailThread.done_at.is_not(None))
    else:
        query = query.where(MailThread.done_at.is_(None))
        if bucket == "unclassified":
            query = query.where(latest_label_subquery().is_(None))
        elif bucket != "all":
            query = query.where(latest_label_subquery() == bucket)

    if provider_account_id is not None:
        # Pure self-scoping predicate, same style as the done_at filter above --
        # a non-owned or unknown id just yields an empty page, never a 404.
        query = query.where(MailThread.provider_account_id == provider_account_id)

    recency_order = _recency_order()
    if sort == "account":
        query = query.join(
            ProviderAccount, ProviderAccount.id == MailThread.provider_account_id
        ).order_by(ProviderAccount.external_user_id.asc(), *recency_order)
    else:
        query = query.order_by(*recency_order)

    threads = list(
        db.execute(query.offset(offset).limit(limit))
        .scalars()
        .all()
    )
    return {"bucket": bucket, "items": _assemble_triage_items(db, threads)}


@router.get("/counts", response_model=Counts)
def get_counts(
    provider_account_id: UUID | None = Query(default=None),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Total thread count per bucket for the sidebar.

    Grouped in SQL so the counts reflect the whole mailbox rather than a single
    truncated page. Keys cover every triage bucket, including `all` (the total)
    and `unclassified`. When `provider_account_id` is given, both the open-bucket
    grouping and the done count are scoped to that account, so `all` still equals
    the filtered open total.
    """
    bucket_label = latest_label_subquery().label("bucket_label")
    open_predicates = [
        MailThread.user_id == current_user.id,
        # Open buckets only; done threads are counted separately below.
        MailThread.done_at.is_(None),
    ]
    done_predicates = [
        MailThread.user_id == current_user.id,
        MailThread.done_at.is_not(None),
    ]
    if provider_account_id is not None:
        open_predicates.append(MailThread.provider_account_id == provider_account_id)
        done_predicates.append(MailThread.provider_account_id == provider_account_id)

    grouped = (
        select(bucket_label)
        .select_from(MailThread)
        .where(*open_predicates)
        .subquery()
    )
    rows = db.execute(
        select(grouped.c.bucket_label, func.count())
        .group_by(grouped.c.bucket_label)
    ).all()

    counts = {bucket: 0 for bucket in _TRIAGE_BUCKETS}
    total = 0
    for label, count in rows:
        total += count
        if label is None:
            counts["unclassified"] += count
        elif label in counts:
            counts[label] += count
    counts["all"] = total
    counts["done"] = db.execute(
        select(func.count())
        .select_from(MailThread)
        .where(*done_predicates)
    ).scalar_one()
    return {"counts": counts}


@router.get("/thread/{thread_id}", response_model=ThreadDetail)
def get_thread(
    thread_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    # A thread's account always exists (FK cascade guarantees it).
    account_email = db.execute(
        select(ProviderAccount.external_user_id).where(
            ProviderAccount.id == thread.provider_account_id
        )
    ).scalar_one()
    messages = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            .order_by(*latest_message_ordering())
        )
        .scalars()
        .all()
    )
    return {
        "thread": {
            "id": str(thread.id),
            "subject": thread.subject,
            "provider": thread.provider,
            # The provider's own thread id, so the console can deep-link back
            # to the source mailbox (Gmail's #all/<id> URL).
            "provider_thread_id": thread.provider_thread_id,
            "last_message_at": thread.last_message_at,
            "done": thread.done_at is not None,
            "account_email": account_email,
        },
        "messages": [
            {
                "id": str(m.id),
                "sent_at": m.sent_at,
                "sender": m.sender,
                "snippet": m.snippet,
                "body_text": m.body_text,
                "body_html": m.body_html,
            }
            for m in messages
        ],
    }


@router.post("/thread/{thread_id}/classification", response_model=Reclassified)
def reclassify_thread(
    thread_id: UUID,
    payload: ReclassifyRequest,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Apply an operator's manual label to a thread.

    The console lets the user override the model when QA-ing predictions. The
    label is stored against the thread's latest message (the same message whose
    classification drives the triage view), with full confidence and an
    ``user-override`` model_version.
    """
    if payload.label not in LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid label '{payload.label}'. Valid: {sorted(LABELS)}",
        )

    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    latest_message = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            # Coalesced like latest_label_subquery, so the override lands on
            # the exact message whose classification drives the triage bucket.
            .order_by(*latest_message_ordering())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if latest_message is None:
        raise HTTPException(status_code=409, detail="Thread has no messages to label")

    upsert_classification(
        db,
        message_id=latest_message.id,
        label=payload.label,
        confidence=1.0,
        rationale="Operator override from the console.",
        model_version=_OPERATOR_MODEL_VERSION,
    )
    db.commit()

    return {
        "thread_id": str(thread_id),
        "classification": {
            "label": payload.label,
            "confidence": 1.0,
            "model_version": _OPERATOR_MODEL_VERSION,
        },
    }


@router.post("/thread/{thread_id}/done", response_model=ThreadDone)
def set_thread_done(
    thread_id: UUID,
    payload: DoneRequest,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Mark a thread done (cleared from triage) or restore it.

    Done is the non-destructive exit from the triage buckets: the thread moves
    to the ``done`` bucket and stays searchable, unlike delete. Idempotent —
    re-marking a done thread keeps its original ``done_at``.
    """
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    if payload.done and thread.done_at is None:
        thread.done_at = datetime.now(timezone.utc)
    elif not payload.done:
        thread.done_at = None
    db.commit()

    return {
        "thread_id": str(thread_id),
        "done": thread.done_at is not None,
        "done_at": thread.done_at,
    }


def _escape_like(q: str) -> str:
    """Escape LIKE metacharacters so user input matches literally -- searching
    for "100%" should find "100%", not "100" followed by anything. Backslash
    goes first so we don't double-escape our own escapes."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Generous per-user limit -- the console fires a search per keystroke, and each
# one is an unanchored ILIKE over body_text, so cap runaway callers while still
# keeping pace with typing.
@router.get(
    "/search",
    response_model=Search,
    dependencies=[Depends(user_rate_limit("search", 120, 60))],
)
def search_threads(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    provider_account_id: UUID | None = Query(default=None),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Search the user's threads across every bucket.

    Case-insensitive substring match on the thread subject and on any of its
    messages' sender / snippet / body. Returns the same item shape as triage so
    the console can render results in the same list.
    """
    pattern = f"%{_escape_like(q)}%"
    message_match = (
        select(MailMessage.id)
        .where(
            MailMessage.thread_id == MailThread.id,
            or_(
                MailMessage.sender.ilike(pattern, escape="\\"),
                MailMessage.snippet.ilike(pattern, escape="\\"),
                MailMessage.body_text.ilike(pattern, escape="\\"),
            ),
        )
        .exists()
    )
    query = select(MailThread).where(
        MailThread.user_id == current_user.id,
        or_(MailThread.subject.ilike(pattern, escape="\\"), message_match),
    )
    if provider_account_id is not None:
        # Same self-scoping predicate as triage -- a non-owned or unknown id
        # just yields an empty page, never a 404.
        query = query.where(MailThread.provider_account_id == provider_account_id)
    threads = list(
        db.execute(
            query.order_by(*_recency_order())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return {"query": q, "items": _assemble_triage_items(db, threads)}


# response_model=None: a 204 carries no body, so there is nothing to validate
# and declaring a model here would be a lie in the OpenAPI schema.
@router.delete("/thread/{thread_id}", status_code=204, response_model=None)
def delete_thread(
    thread_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Permanently delete a thread and everything hanging off it.

    The mail_message -> mail_thread and classification -> mail_message foreign
    keys are ON DELETE CASCADE, so dropping the thread row takes its messages
    and their classifications with it.
    """
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    db.delete(thread)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/ingest/gmail",
    response_model=SyncRunList,
    status_code=202,
    # Each call queues up to 500 Gmail fetches of Celery work per account; keep
    # it rare.
    dependencies=[Depends(user_rate_limit("ingest-gmail", 5, 60))],
)
def ingest_gmail(
    max_results: int = Query(default=25, ge=1, le=_MAX_INGEST_RESULTS),
    skip_existing: bool = True,
    classify: bool = True,
    # Pull only mail newer than the newest known thread (what auto-sync
    # wants), instead of also backfilling older history up to max_results.
    new_only: bool = False,
    # Scope the fan-out to specific accounts instead of every connected one.
    provider_account_ids: list[UUID] | None = Query(default=None),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Queue a Gmail pull per connected account instead of running it inline.

    A full ingest is up to 500 serial Gmail fetches plus classification --
    way too slow to hold a request open for, so we hand each account to
    Celery and return 202 with one run per account. Paused accounts (a dead
    refresh token) are skipped -- nothing but a reconnect fixes those, so
    queuing them just burns quota. Zero connected/eligible accounts isn't an
    error; it just means nothing to sync yet.

    ``provider_account_ids``, when given, narrows the fan-out to just those
    accounts -- still filtered through the same user-owned, gmail, not-paused
    predicates, so an unknown or another user's id is silently dropped rather
    than erroring, and a paused account in the list stays skipped. Omit it to
    get today's all-accounts behavior.
    """
    predicates = [
        ProviderAccount.user_id == current_user.id,
        ProviderAccount.provider == "gmail",
        ProviderAccount.sync_paused_at.is_(None),
    ]
    if provider_account_ids is not None:
        predicates.append(ProviderAccount.id.in_(provider_account_ids))
    accounts = (
        db.execute(select(ProviderAccount).where(*predicates))
        .scalars()
        .all()
    )
    mode = "auto" if new_only else ("refresh" if not skip_existing else "manual")
    options = {
        "max_results": max_results,
        "skip_existing": skip_existing,
        "classify_messages": classify,
        "new_only": new_only,
    }
    runs = []
    for account in accounts:
        run, deduplicated = start_sync_run(
            db, current_user.id, account.id, mode=mode, options=options
        )
        runs.append(sync_payload(run, deduplicated=deduplicated))
    return {"runs": runs}


@router.get("/sync/active", response_model=SyncRunList)
def get_active_sync(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return {"runs": [sync_payload(r) for r in active_syncs(db, current_user.id)]}


# Must precede /sync/{run_id}: that route types run_id as a UUID, so a literal
# "health" reaching it first would 422 instead of landing here.
@router.get("/sync/health", response_model=SyncHealth)
def get_sync_health(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Whether this mailbox is actually current, and why not if it isn't.
    Deliberately its own endpoint. /sync/active returns no runs when idle.

    Multi-account aware: the top-level fields are a worst-of aggregate across
    every connected Gmail account (so the console's existing pill logic keeps
    working unchanged), and `accounts` breaks that down per account.
    """
    expire_stale_sync(db, current_user.id)

    accounts = (
        db.execute(
            select(ProviderAccount).where(
                ProviderAccount.user_id == current_user.id,
                ProviderAccount.provider == "gmail",
            )
        )
        .scalars()
        .all()
    )
    last_success_by_account = dict(
        db.execute(
            select(MailSyncRun.provider_account_id, func.max(MailSyncRun.completed_at))
            .where(
                MailSyncRun.user_id == current_user.id,
                MailSyncRun.status == "succeeded",
            )
            .group_by(MailSyncRun.provider_account_id)
        ).all()
    )
    active_account_ids = set(
        db.execute(
            select(MailSyncRun.provider_account_id)
            .where(
                MailSyncRun.user_id == current_user.id,
                MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
            )
            .group_by(MailSyncRun.provider_account_id)
        )
        .scalars()
        .all()
    )

    # When scheduling is switched off (interval 0), no heartbeat is ever
    # written and the browser fallback carries sync by design -- so "no
    # heartbeat" is the expected state, not a dead scheduler. Report alive so
    # the console doesn't cry "scheduler down" at everyone forever.
    scheduling_enabled = settings.scheduled_sync_interval_seconds > 0
    heartbeat = read_dispatcher_heartbeat() if scheduling_enabled else None
    scheduler_alive = not scheduling_enabled or heartbeat is not None
    threshold = settings.sync_stale_after_seconds
    now = now_utc()

    account_entries: list[dict] = []
    for account in accounts:
        last_succeeded_at = last_success_by_account.get(account.id)
        reason: str | None = None
        stale = False
        if account.sync_paused_at is not None or not account.refresh_token:
            # Nothing but a reconnect fixes this, so say so instead of
            # reporting a staleness the user can't act on. A missing refresh
            # token (Google sometimes omits it on a login-path insert) is the
            # same dead end as an explicit pause -- the dispatcher can't sync
            # without one either way, and only a re-consent mints a new one.
            reason = account.sync_pause_reason or "reauth_required"
        elif last_succeeded_at is None:
            reason = "never_synced"
        else:
            # Purely a fact about the data: an in-flight run does NOT make old
            # mail fresh. Letting it suppress this would hide the exact
            # failure this endpoint exists for -- a run that keeps retrying,
            # or sits queued against a dead worker for its 2h lease, is "in
            # progress" the whole time the mailbox rots. Callers that want to
            # soften the wording have sync_in_progress right there.
            stale = last_succeeded_at < now - timedelta(seconds=threshold)
        account_entries.append(
            {
                "provider_account_id": str(account.id),
                "email_address": account.external_user_id,
                "last_succeeded_at": last_succeeded_at,
                "stale": stale,
                "sync_in_progress": account.id in active_account_ids,
                "reason": reason,
            }
        )

    # Worst-of across accounts: a caller reconnecting one broken mailbox
    # shouldn't have another account's staleness go quiet, and vice versa.
    if not accounts:
        top_reason = "not_connected"
    elif any(
        account.sync_paused_at is not None or not account.refresh_token
        for account in accounts
    ):
        top_reason = "reauth_required"
    elif any(entry["reason"] == "never_synced" for entry in account_entries):
        top_reason = "never_synced"
    else:
        top_reason = None

    successes = [
        entry["last_succeeded_at"]
        for entry in account_entries
        if entry["last_succeeded_at"] is not None
    ]
    # The oldest success across accounts -- an honest "mailbox behind since"
    # rather than letting one healthy account hide another's staleness. Null
    # only when NO account has ever succeeded.
    top_last_succeeded_at = min(successes) if successes else None

    return {
        "last_succeeded_at": top_last_succeeded_at,
        "stale": any(entry["stale"] for entry in account_entries),
        "sync_in_progress": bool(active_account_ids),
        "scheduler_alive": scheduler_alive,
        "threshold_seconds": threshold,
        "reason": top_reason,
        "accounts": account_entries,
    }


@router.get("/sync/{run_id}", response_model=SyncRun)
def get_sync_run(
    run_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(MailSyncRun, run_id)
    if not run or run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    if run.status in ("queued", "running", "retrying") and run.lease_expires_at < now_utc():
        run.status = "failed"
        run.error = "sync lease expired"
        run.completed_at = now_utc()
        db.commit()
    return sync_payload(run)


@router.get("/tasks/{task_id}", response_model=TaskStatus)
def get_task_status(
    task_id: str,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Report a queued job's state so the UI can wait for it to finish before
    refreshing -- otherwise it refetches against a DB the worker hasn't written
    to yet, and nothing appears until the operator navigates or reloads.
    """
    result = celery_app.AsyncResult(task_id)
    sync_run = db.scalar(select(MailSyncRun).where(MailSyncRun.task_id == task_id))
    if sync_run and sync_run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")

    ready = result.ready()
    payload: dict[str, Any] = {"task_id": task_id, "state": result.state, "ready": ready}
    if ready:
        if result.successful():
            data = result.result
            # Backfill/classify tasks don't have a sync-run row, so their result
            # payload is the only ownership record we can check.
            if isinstance(data, dict) and data.get("user_id") not in (None, str(current_user.id)):
                raise HTTPException(status_code=404, detail="Not Found")
            payload["result"] = data
        else:
            error_id = uuid4().hex
            logger.error("Worker task failed [%s] task_id=%s", error_id, task_id)
            if result.result:
                logger.debug(
                    "Worker task failed [%s] detail: %r", error_id, result.result
                )
            payload["error"] = "task failed"
            payload["error_id"] = error_id
    return payload


# Two shapes on purpose: inline runs report counts, queued ones report a task
# id. The union keeps both honest in the schema instead of widening to Any.
@router.post(
    "/classify/backfill",
    response_model=BackfillDone | Queued,
    # Small backfills run the classifier inline; big ones enqueue worker jobs.
    # Either way each call is expensive, so keep the per-user cadence low.
    dependencies=[Depends(user_rate_limit("classify-backfill", 5, 60))],
)
def backfill_classifications(
    response: Response,
    limit: int = Query(default=100, ge=1, le=_MAX_INGEST_RESULTS),
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Classify (or, with ``force``, re-classify) a batch of threads.

    ``bucket`` scopes which threads are eligible, filtered in SQL *before* the
    limit so the batch targets the intended threads instead of whatever falls in
    the most-recent ``limit`` rows:
      - "unclassified" (default): threads whose latest message has no label.
      - a specific label: threads currently in that bucket (needs ``force`` to
        actually re-run, since they're already classified).
      - "all": every thread; unlabeled ones get classified, and with ``force``
        everything is re-classified.
    ``backend`` overrides the configured classifier for this run.

    Up to ``_MAX_INLINE_BACKFILL`` threads this runs inline and returns counts;
    anything bigger is handed to the worker and answered with 202 + task id,
    since a large Gemini batch can hold the request open for minutes.
    """
    if bucket not in _BACKFILL_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid bucket '{bucket}'. Valid: {sorted(_BACKFILL_BUCKETS)}",
        )
    # classify() lowercases its backend anyway, but normalize here so one
    # canonical value flows into task kwargs and logs.
    normalized_backend = backend.lower() if backend is not None else None
    if normalized_backend is not None and normalized_backend not in _CLASSIFIER_BACKENDS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid backend '{backend}'. Valid: {sorted(_CLASSIFIER_BACKENDS)}",
        )

    if limit > _MAX_INLINE_BACKFILL:
        task = cast(Any, backfill_threads_for_user).delay(
            user_id=str(current_user.id),
            limit=limit,
            force=force,
            bucket=bucket,
            backend=normalized_backend,
        )
        response.status_code = 202
        return {"status": "queued", "task_id": task.id}

    return run_backfill(
        db,
        current_user.id,
        limit=limit,
        force=force,
        bucket=bucket,
        backend=normalized_backend,
    )


@router.post(
    "/classify/queue",
    response_model=Queued,
    # Same story as ingest: each call piles classification work on the worker.
    dependencies=[Depends(user_rate_limit("classify-queue", 5, 60))],
)
def queue_classification(
    limit: int = Query(default=25, ge=1, le=_MAX_INGEST_RESULTS),
    force: bool = False,
    current_user: AppUser = Depends(get_current_user),
) -> dict:
    task = cast(Any, classify_latest_threads).delay(
        user_id=str(current_user.id),
        limit=limit,
        force=force,
    )
    return {"status": "queued", "task_id": task.id}
