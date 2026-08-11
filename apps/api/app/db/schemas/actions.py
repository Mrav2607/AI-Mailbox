from datetime import datetime
from typing import Literal
from uuid import UUID

from .common import Response

# Frozen in the extractor contract (app/services/nlp/extractor.py) -- kept in
# sync by hand rather than imported, since schemas/models/extractor are
# separate owned files that all pin the same literal set independently.
ActionKind = Literal["reply", "payment", "signature", "form", "rsvp", "deadline", "other"]
ActionStatus = Literal["open", "done", "dismissed"]
DuePrecision = Literal["date", "datetime"]


class ActionOut(Response):
    """One agenda row: an extracted action item joined against its source
    thread/message for display.
    """

    id: UUID
    thread_id: UUID
    message_id: UUID
    kind: ActionKind | None
    title: str | None
    due_at: datetime | None
    due_precision: DuePrecision | None
    due_raw: str | None
    amount: float | None
    currency: str | None
    source_confidence: float | None
    status: ActionStatus
    created_at: datetime
    # Joined display fields -- mirrors TriageItem/ThreadSummary's join pattern.
    thread_subject: str | None
    sender: str | None
    provider: str
    account_email: str
    # The source thread's latest-message timestamp -- the console's unread
    # tracking compares a thread's stored "seen" timestamp against this to
    # decide whether it's still bold, so an agenda row needs it too.
    last_message_at: datetime | None
    # The source message's current classification label -- what the
    # visibility join is keyed on, surfaced so the console can explain why an
    # item is showing.
    label: str | None


class ActionCounts(Response):
    open: int
    overdue: int


class ActionList(Response):
    """Agenda counts are always cross-account: unlike bucket counts, there is
    no per-account filter here -- the agenda's whole point is one unified
    board across every connected account. `counts` is computed regardless of
    the `status` query filter.
    """

    items: list[ActionOut]
    counts: ActionCounts


class ActionStatusRequest(Response):
    status: ActionStatus


class ActionStatusOut(Response):
    action_id: UUID
    status: ActionStatus
    status_at: datetime | None


class BackfillQueued(Response):
    """Response for the actions backfill route -- same shape as
    `mailbox.Queued`, kept as its own model so this module doesn't depend on
    `mailbox.py`.
    """

    status: str
    task_id: str
