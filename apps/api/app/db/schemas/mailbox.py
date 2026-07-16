from datetime import datetime
from typing import Any
from uuid import UUID

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


class ThreadSummary(Response):
    id: UUID
    subject: str | None
    provider: str
    # The provider's own id -- this is what powers the open-in-Gmail deep link,
    # so dropping it would quietly break that button and nothing else.
    provider_thread_id: str | None
    last_message_at: datetime | None
    done: bool


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


class Queued(Response):
    """Anything over the inline cap gets handed to a worker instead."""

    status: str
    task_id: str
