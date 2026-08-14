from datetime import datetime
from typing import Any
from uuid import UUID

from .actions import ActionCounts
from .common import Response


class ClassificationOut(Response):
    label: str | None
    confidence: float | None
    model_version: str | None


class TriageItem(Response):
    thread_id: UUID
    subject: str | None
    last_message_at: datetime | None
    latest_message_snippet: str | None
    latest_message_sender: str | None
    classification: ClassificationOut
    # Which connected Gmail account this thread belongs to, so a multi-account
    # console can label rows instead of blending every mailbox into one list.
    account_email: str
    # The reply fence (docs/plans/2026-08-13-reply-plan.md §3.5/§3.9): null =
    # never replied from CortexMail, timestamp = when a reply attempt last
    # completed. Powers the "replied" indicator without a second call.
    replied_at: datetime | None


class Triage(Response):
    bucket: str
    items: list[TriageItem]


class Search(Response):
    query: str
    items: list[TriageItem]


class Counts(Response):
    # One entry per bucket, plus `all` and `unclassified`. Left open rather than
    # pinned to a literal set so adding a bucket doesn't silently drop it here.
    counts: dict[str, int]
    # Agenda counts, ALWAYS cross-account: unlike the bucket counts above,
    # there's no per-account filter here on purpose -- the agenda's whole
    # point is one unified board across every connected account, so a bucket
    # view scoped to one account must not narrow it. Same visibility rule as
    # GET /mail/actions (see routes/actions.py's module docstring).
    actions: ActionCounts


class ThreadSummary(Response):
    id: UUID
    subject: str | None
    provider: str
    # The provider's own id -- this is what powers the open-in-Gmail deep link,
    # so dropping it would quietly break that button and nothing else.
    provider_thread_id: str | None
    last_message_at: datetime | None
    done: bool
    # Which connected Gmail account this thread belongs to.
    account_email: str
    # See TriageItem.replied_at -- same fence, same composer-seeding use
    # (docs/plans/2026-08-13-reply-plan.md §3.9).
    replied_at: datetime | None


class ThreadMessage(Response):
    id: UUID
    sent_at: datetime | None
    sender: str | None
    snippet: str | None
    body_text: str | None
    body_html: str | None


class ThreadDetail(Response):
    thread: ThreadSummary
    messages: list[ThreadMessage]
    # The agenda opens threads that aren't in any loaded bucket list, so the
    # console can't fall back on a triage row's label the way the bucket view
    # does -- the detail response has to carry it directly. None when the
    # thread has no messages or its latest one hasn't been classified yet.
    classification: ClassificationOut | None = None


class Reclassified(Response):
    thread_id: UUID
    classification: ClassificationOut


class ThreadDone(Response):
    thread_id: UUID
    done: bool
    done_at: datetime | None


class SyncRun(Response):
    run_id: UUID
    task_id: str | None
    mode: str
    status: str
    ready: bool
    deduplicated: bool
    # Whatever the worker reported -- shape varies by task (upsert counts for
    # ingest, created/scanned for backfill), so it stays a free-form object.
    result: dict[str, Any] | None
    error: str | None
    # Which Gmail account this run is pulling from. Null on legacy rows a
    # migration couldn't attribute to a single account.
    provider_account_id: str | None


class SyncRunList(Response):
    """Multi-account fan-out: one entry per account a call touched."""

    runs: list[SyncRun]


class AccountSyncHealth(Response):
    provider_account_id: str
    email_address: str
    last_succeeded_at: datetime | None
    stale: bool
    sync_in_progress: bool
    # null | never_synced | reauth_required | <sync_pause_reason>
    reason: str | None


class SyncHealth(Response):
    """
    `stale` is about the DATA (has a sync succeeded recently), while
    `scheduler_alive` is about the MACHINERY (is the dispatcher still checking
    in). A dead scheduler with the browser fallback still working is stale=false,
    scheduler_alive=false.

    The top-level fields are the aggregate across every connected Gmail
    account (worst-of, so the console's existing pill logic still parses this
    unchanged); `accounts` breaks that aggregate down per account.
    """

    last_succeeded_at: datetime | None
    stale: bool
    sync_in_progress: bool
    scheduler_alive: bool
    threshold_seconds: int
    # null | never_synced | reauth_required | not_connected
    reason: str | None
    accounts: list[AccountSyncHealth]


class TaskStatus(Response):
    task_id: str
    state: str
    ready: bool
    result: dict[str, Any] | None = None
    # A failed task returns a generic message plus an id that matches a server
    # log line -- never the raw exception, which can carry SQL and mail content.
    error: str | None = None
    error_id: str | None = None


class BackfillDone(Response):
    """Small backfills run inline and report what they did."""

    status: str
    created: int
    scanned: int
    # How many candidate rows a force run left alone because a human already
    # overrode them -- see run_backfill's docstring. Defaults to 0 so an old
    # Celery result recorded before this field existed still parses.
    skipped_user_overrides: int = 0


class Queued(Response):
    """Anything over the inline cap gets handed to a worker instead."""

    status: str
    task_id: str
