"""Label sync's non-worker surface: name maps, the frozen drift predicate,
execution-time eligibility, the advisory-lock helper, and the DB/provider
mechanics behind each numbered step of the ordering guard (plan
docs/plans/2026-08-13-label-sync-plan.md §3.1/§3.2/§3.8). Orchestration --
which step runs when, session/transaction boundaries, token refresh --
belongs to `app/workers/tasks_label_sync.py`; everything here is a building
block that module composes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.db.models import Classification, MailMessage, MailThread, ProviderAccount
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.outlook_client import OutlookClient
from app.services.nlp.backfill import latest_label_subquery, latest_message_ordering
from app.services.nlp.classifier import LABELS

# ---------------------------------------------------------------------------
# Names: internal label -> provider-facing display name (plan §3.2).
# ---------------------------------------------------------------------------

DISPLAY_NAMES: dict[str, str] = {
    "needs_reply": "Needs reply",
    "action_required": "Action required",
    "fyi": "FYI",
    "promotional": "Promotional",
    "security_alert": "Security alert",
    "spam": "Spam",
}
assert set(DISPLAY_NAMES) == set(LABELS)  # keep in lockstep with the classifier

GMAIL_PARENT_LABEL = "CortexMail"
OUTLOOK_CATEGORY_PREFIX = "CortexMail: "


def gmail_label_full_name(label: str) -> str:
    return f"{GMAIL_PARENT_LABEL}/{DISPLAY_NAMES[label]}"


def outlook_category_name(label: str) -> str:
    return f"{OUTLOOK_CATEGORY_PREFIX}{DISPLAY_NAMES[label]}"


def _label_for_gmail_full_name(name: str) -> str:
    for label in LABELS:
        if gmail_label_full_name(label) == name:
            return label
    raise ValueError(f"not a CortexMail-owned label name: {name!r}")


GMAIL_OWNED_LABEL_NAMES: frozenset[str] = frozenset(
    {GMAIL_PARENT_LABEL, *(gmail_label_full_name(lbl) for lbl in LABELS)}
)

OUTLOOK_OWNED_CATEGORY_NAMES: frozenset[str] = frozenset(
    outlook_category_name(label) for label in LABELS
)

# Gmail's documented allowed label-color hex palette (users.labels#color) --
# a subset used here, one background/text pair per internal label, all
# members of this list. https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels
ALLOWED_GMAIL_LABEL_COLORS: frozenset[str] = frozenset(
    {
        "#000000", "#434343", "#666666", "#999999", "#cccccc", "#efefef",
        "#f3f3f3", "#ffffff", "#fb4c2f", "#ffad47", "#fad165", "#16a765",
        "#43d692", "#4a86e8", "#a479e2", "#f691b3", "#cc3a21", "#eaa041",
        "#f2c960", "#149e60",
    }
)

# House colors, one pair per label -- both members drawn from the allowed
# palette above (asserted below so a future edit can't silently pick an
# invalid combination).
GMAIL_LABEL_COLORS: dict[str, dict[str, str]] = {
    "needs_reply": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"},
    "action_required": {"backgroundColor": "#ffad47", "textColor": "#ffffff"},
    "fyi": {"backgroundColor": "#4a86e8", "textColor": "#ffffff"},
    "promotional": {"backgroundColor": "#a479e2", "textColor": "#ffffff"},
    "security_alert": {"backgroundColor": "#cc3a21", "textColor": "#ffffff"},
    "spam": {"backgroundColor": "#999999", "textColor": "#ffffff"},
}
assert set(GMAIL_LABEL_COLORS) == set(LABELS)
assert all(
    pair["backgroundColor"] in ALLOWED_GMAIL_LABEL_COLORS
    and pair["textColor"] in ALLOWED_GMAIL_LABEL_COLORS
    for pair in GMAIL_LABEL_COLORS.values()
)

# ---------------------------------------------------------------------------
# Execution-time eligibility (plan §3.1/P1-2) and the shared advisory lock
# (§3.3/P7-1) -- imported by the enable route (Wave 2) and every claiming
# task so both sides agree on the same key derivation and scope check.
# ---------------------------------------------------------------------------

# The real, documented scope Google echoes back for a granted gmail.modify
# consent (see app/routes/auth_google.py's SCOPES) -- gated on the full URI,
# not the short name, since that's the actual stored value. Mirrors
# app/routes/reply.py's _check_scope precedent.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# Outlook only needs the read/write grant here (unlike reply, label sync
# never sends mail) -- stored short-form (app/routes/auth_microsoft.py's
# MS_SCOPES), tested independently per this repo's precedent.
OUTLOOK_REQUIRED_SCOPE_TOKEN = "mail.readwrite"

# Per-thread claim lease (sync-run lease precedent, plan §3.1/§3.8 step 1).
CLAIM_LEASE = timedelta(minutes=10)


def _scope_tokens(raw: str | None) -> set[str]:
    return {tok.lower() for tok in (raw or "").split() if tok}


def label_sync_execution_eligible(account: ProviderAccount) -> tuple[bool, str | None]:
    """Re-checked inside every task before any provider call (plan §3.1) --
    a queued task whose account was disabled, paused, or reauth'd meanwhile
    must no-op, never assume the state that made it eligible for enqueue
    still holds. Returns (eligible, reason) where reason is one of
    "disabled" / "paused" / "no_refresh_token" / "missing_scope" when not.
    """
    if not account.label_sync_enabled:
        return False, "disabled"
    if account.sync_paused_at is not None:
        return False, "paused"
    if not account.refresh_token:
        return False, "no_refresh_token"
    tokens = _scope_tokens(account.scope)
    if account.provider == "gmail":
        if GMAIL_MODIFY_SCOPE.lower() not in tokens:
            return False, "missing_scope"
    else:
        if OUTLOOK_REQUIRED_SCOPE_TOKEN not in tokens:
            return False, "missing_scope"
    return True, None


def advisory_lock_key(account_id: UUID) -> int:
    """Stable, always-positive 63-bit key derived from a provider_account
    UUID -- fits Postgres' signed-bigint advisory lock functions."""
    return account_id.int & 0x7FFFFFFFFFFFFFFF


def acquire_account_lock(db: Session, account_id: UUID) -> None:
    """Transaction-scoped advisory lock (P7-1): the enable route and every
    claiming task acquire the SAME lock, keyed on the account id, before
    touching claim/generation state -- released automatically at
    COMMIT/ROLLBACK, never unlocked explicitly.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_lock_key(account_id)})


# ---------------------------------------------------------------------------
# The frozen drift predicate (plan §3.1, P3-1) -- ONE shared expression, used
# identically by the tick's selection query and any drift-count helper.
# Never hand-roll a second copy of this logic; import this.
# ---------------------------------------------------------------------------


def _latest_message_provider_id_subquery():
    """Correlated scalar subquery: the thread's latest message's
    provider_message_id. Uses the SAME ordering as latest_label_subquery
    (via latest_message_ordering) so both agree on which message is
    "latest"."""
    return (
        select(MailMessage.provider_message_id)
        .where(MailMessage.thread_id == MailThread.id)
        .order_by(*latest_message_ordering())
        .limit(1)
        .correlate(MailThread)
        .scalar_subquery()
    )


def label_drift_clause():
    """The frozen drift predicate:

        current_label IS DISTINCT FROM synced_label
        OR (
          current_label IS NOT NULL
          AND latest_message_id IS DISTINCT FROM synced_message_id
        )
        OR label_resync_needed
        OR label_sync_pending_target IS NOT NULL

    The message-id term applies ONLY when a label is desired (P3-1) -- else
    never-classified and removal-completed threads would be perpetually
    selected. The pending-target term keeps crash-recovery work selectable
    even when classification has reverted so desired == recorded again.
    """
    current_label = latest_label_subquery()
    latest_message_id = _latest_message_provider_id_subquery()
    return or_(
        current_label.is_distinct_from(MailThread.synced_label),
        and_(
            current_label.is_not(None),
            latest_message_id.is_distinct_from(MailThread.synced_message_id),
        ),
        MailThread.label_resync_needed.is_(True),
        MailThread.label_sync_pending_target.is_not(None),
    )


def _is_drifted(
    *,
    desired_label: str | None,
    latest_message_id: str | None,
    synced_label: str | None,
    synced_message_id: str | None,
    label_resync_needed: bool,
    pending_target: str | None,
) -> bool:
    """Python-side mirror of `label_drift_clause()` (same docstring text
    applies) -- used at claim time against a single already-locked row,
    where a second SQL round trip to re-derive the same answer would be
    redundant. Must stay logically identical to the SQL clause above.
    """
    if desired_label != synced_label:
        return True
    if desired_label is not None and latest_message_id != synced_message_id:
        return True
    if label_resync_needed:
        return True
    if pending_target is not None:
        return True
    return False


def account_drift_count(db: Session, provider_account_id: UUID) -> int:
    """Count of an account's threads currently drifted -- same predicate the
    tick uses, exposed for the connections response's "syncing... N
    remaining" line (plan §3.4/§3.6)."""
    return db.execute(
        select(func.count())
        .select_from(MailThread)
        .where(
            MailThread.provider_account_id == provider_account_id,
            label_drift_clause(),
        )
    ).scalar_one()


def desired_state_for_thread(db: Session, thread_id: UUID) -> tuple[str | None, str | None]:
    """(desired_label, latest_message_provider_id) for one thread -- what
    `label_drift_clause()` compares against, computed directly against an
    already-identified thread rather than via the correlated subqueries."""
    row = db.execute(
        select(MailMessage.provider_message_id, Classification.label)
        .select_from(MailMessage)
        .outerjoin(Classification, Classification.message_id == MailMessage.id)
        .where(MailMessage.thread_id == thread_id)
        .order_by(*latest_message_ordering())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return row.label, row.provider_message_id


# ---------------------------------------------------------------------------
# §3.8 step 1 (txn1: claim)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSnapshot:
    id: UUID
    provider: str
    access_token: str
    refresh_token: str | None
    scope: str | None
    gmail_label_map: str | None


@dataclass(frozen=True)
class ClaimResult:
    thread_id: UUID
    provider_thread_id: str
    claim_token: str
    generation: int
    account: AccountSnapshot
    desired_label: str | None
    latest_message_id: str | None
    synced_label: str | None
    synced_message_id: str | None
    inherited_pending_target: str | None


def claim_thread(db: Session, thread_id: UUID) -> ClaimResult | None:
    """§3.8 step 1 (txn1): claim `thread_id` for one sync attempt.

    Runs entirely in the caller's already-open session/transaction and
    commits on success; every early-return path leaves nothing written (the
    caller doesn't need to roll back -- no write happens before the final
    commit). Returns None when the thread isn't eligible right now:
    missing, wrong/paused/scopeless account, already converged, or a live
    lease held by someone else.

    Lock order: the account's advisory lock, then the MailThread row FOR
    UPDATE (never a MailThread lock and a ProviderAccount row lock held at
    the same time -- see §3.8's map-persistence step, which locks only the
    account row and never touches this one).
    """
    thread_account = db.execute(
        select(MailThread.provider_account_id).where(MailThread.id == thread_id)
    ).first()
    if thread_account is None:
        return None

    # The lock has to be acquired BEFORE reading the account row, not after
    # -- reading first would let a concurrent enable route's generation
    # bump land between our read and our lock acquisition, so we'd
    # serialize on nothing and still capture a stale generation (P7-1).
    acquire_account_lock(db, thread_account.provider_account_id)

    account_row = db.get(ProviderAccount, thread_account.provider_account_id)
    if account_row is None:
        return None

    eligible, _reason = label_sync_execution_eligible(account_row)
    if not eligible:
        return None

    locked = db.execute(
        select(MailThread).where(MailThread.id == thread_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        return None

    desired_label, latest_message_id = desired_state_for_thread(db, thread_id)

    if not _is_drifted(
        desired_label=desired_label,
        latest_message_id=latest_message_id,
        synced_label=locked.synced_label,
        synced_message_id=locked.synced_message_id,
        label_resync_needed=locked.label_resync_needed,
        pending_target=locked.label_sync_pending_target,
    ):
        return None

    now = datetime.now(timezone.utc)
    if (
        locked.label_sync_claim_token
        and locked.label_sync_claimed_at
        and locked.label_sync_claimed_at > now - CLAIM_LEASE
    ):
        return None  # a live lease, held by another in-flight task

    inherited_pending_target = locked.label_sync_pending_target
    token = str(uuid4())
    # Do NOT touch label_sync_pending_target here even if it's populated --
    # it may be the only record of a possibly-applied orphan left by a dead
    # task (§3.8 step 1's explicit instruction).
    locked.label_sync_claim_token = token
    locked.label_sync_claimed_at = now
    db.commit()

    return ClaimResult(
        thread_id=thread_id,
        provider_thread_id=locked.provider_thread_id,
        claim_token=token,
        generation=account_row.label_sync_generation,
        account=AccountSnapshot(
            id=account_row.id,
            provider=account_row.provider,
            access_token=account_row.access_token,
            refresh_token=account_row.refresh_token,
            scope=account_row.scope,
            gmail_label_map=account_row.gmail_label_map,
        ),
        desired_label=desired_label,
        latest_message_id=latest_message_id,
        synced_label=locked.synced_label,
        synced_message_id=locked.synced_message_id,
        inherited_pending_target=inherited_pending_target,
    )


# ---------------------------------------------------------------------------
# §3.8 step 1b (txn1b: intent)
# ---------------------------------------------------------------------------


def set_pending_target(
    db: Session, thread_id: UUID, claim_token: str, target: str | None
) -> bool:
    """§3.8 step 1b: token-fenced write of this task's intended target
    message id. Commits. Zero rowcount means the lease was stolen since the
    claim -- the caller aborts silently (returns False), never overwriting
    whatever the new claimant is doing.
    """
    result = db.execute(
        update(MailThread)
        .where(
            MailThread.id == thread_id,
            MailThread.label_sync_claim_token == claim_token,
        )
        .values(label_sync_pending_target=target)
    )
    db.commit()
    return bool(result.rowcount)


def target_for_thread(
    *,
    desired_label: str | None,
    latest_message_id: str | None,
    synced_message_id: str | None,
    inherited_pending_target: str | None,
) -> str | None:
    """The message id this attempt's write cycle is centered on (plan
    §3.1/§3.5): the current latest message when a label is desired, else
    the PREVIOUS applied target (falling back to whatever a dead task's
    pending target already named) when converging to "no label" -- there's
    no new message to categorize in that case, only an old one to clean.
    """
    if desired_label is not None:
        return latest_message_id
    return synced_message_id or inherited_pending_target


# ---------------------------------------------------------------------------
# §3.2 Gmail mechanics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GmailApplyResult:
    working_map: dict[str, str]
    add_ids: list[str]
    remove_ids: list[str]


def load_gmail_label_map(raw: str | None) -> dict[str, str]:
    """Malformed/non-object cache JSON is treated as empty (P1-5) -- a
    corrupt cache must never abort a sync, only cost a re-list."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}


def _rebuild_gmail_map_from_listing(client: GmailClient, working: dict[str, str]) -> None:
    """Re-`GET /labels` and adopt ids by exact full-name match (P1-5) --
    called at most once per task run, from either a create conflict or an
    invalid-cached-id error at modify time."""
    listing = client.list_labels()
    by_name = {
        label["name"]: label["id"]
        for label in listing.get("labels", [])
        if label.get("name") and label.get("id")
    }
    for name in GMAIL_OWNED_LABEL_NAMES:
        if name in by_name:
            working[name] = by_name[name]


def _is_auth_error(exc: httpx.HTTPStatusError) -> bool:
    """A 401 is the caller's cue to refresh the access token (ingest's
    with_token_retry idiom), never a label-conflict/invalid-id signal --
    must propagate immediately, not consume the once-per-run rebuild."""
    return exc.response is not None and exc.response.status_code == 401


def _create_gmail_label(
    client: GmailClient, working: dict[str, str], name: str, rebuilt: dict[str, bool]
) -> None:
    payload: dict[str, Any] = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    if name != GMAIL_PARENT_LABEL:
        payload["color"] = GMAIL_LABEL_COLORS[_label_for_gmail_full_name(name)]
    try:
        resp = client.create_label(payload)
        working[name] = resp["id"]
    except httpx.HTTPStatusError as exc:
        if _is_auth_error(exc):
            raise
        # Gmail doesn't document exact conflict statuses -- don't
        # pattern-match, just fall back to re-list-and-adopt, once.
        if rebuilt["done"]:
            raise
        rebuilt["done"] = True
        _rebuild_gmail_map_from_listing(client, working)
        if name not in working:
            raise


def _ensure_gmail_label_ids(
    client: GmailClient, working: dict[str, str], rebuilt: dict[str, bool]
) -> None:
    """Ensure the parent AND all six children have ids in `working` (§3.2
    step 1) -- parent-first, since a child's full name embeds it but there
    is no separate parent-id field. All six are needed even when only one
    is desired: step 2's removal intersects the thread's live labelIds
    against every CortexMail-owned id, not just the desired one.
    """
    for name in (GMAIL_PARENT_LABEL, *(gmail_label_full_name(lbl) for lbl in LABELS)):
        if name not in working:
            _create_gmail_label(client, working, name, rebuilt)


def apply_gmail_labels(
    client: GmailClient,
    *,
    provider_thread_id: str,
    cached_map: dict[str, str],
    desired_label: str | None,
) -> GmailApplyResult:
    """§3.2 Gmail apply: ensure label ids, then union the thread's messages'
    labelIds (a Gmail Thread has no top-level labelIds, P2-4), intersect
    with the six owned ids, and modify only that actually-present
    intersection plus the desired id (self-healing: always reads live
    provider state, never trusts what we last recorded).
    """
    working = dict(cached_map)
    rebuilt = {"done": False}

    _ensure_gmail_label_ids(client, working, rebuilt)
    owned_full_names = [gmail_label_full_name(lbl) for lbl in LABELS]

    def compute_and_modify() -> GmailApplyResult:
        owned_ids = {working[n] for n in owned_full_names if n in working}
        desired_id = (
            working.get(gmail_label_full_name(desired_label)) if desired_label else None
        )
        thread = client.get_thread_minimal(provider_thread_id)
        present_ids: set[str] = set()
        for message in thread.get("messages", []) or []:
            present_ids.update(message.get("labelIds", []) or [])
        current_owned = present_ids & owned_ids
        add_ids = [desired_id] if desired_id and desired_id not in current_owned else []
        remove_ids = sorted(current_owned - ({desired_id} if desired_id else set()))
        if add_ids or remove_ids:
            client.modify_thread(provider_thread_id, add_ids=add_ids, remove_ids=remove_ids)
        return GmailApplyResult(working_map=working, add_ids=add_ids, remove_ids=remove_ids)

    try:
        return compute_and_modify()
    except httpx.HTTPStatusError as exc:
        if _is_auth_error(exc) or rebuilt["done"]:
            raise
        rebuilt["done"] = True
        # A cached id can point at a label that's since been DELETED --
        # `_rebuild_gmail_map_from_listing`'s adopt-by-name only overwrites
        # names it actually finds, so a dead id for a name that's now
        # missing from the listing would otherwise survive the rebuild
        # untouched and fail every retry forever (L-1). Drop every
        # CortexMail-owned entry first so the listing can only repopulate
        # what's genuinely still there, then re-ensure so anything that's
        # gone gets recreated before we retry the modify.
        for name in GMAIL_OWNED_LABEL_NAMES:
            working.pop(name, None)
        _rebuild_gmail_map_from_listing(client, working)
        _ensure_gmail_label_ids(client, working, rebuilt)
        return compute_and_modify()


# ---------------------------------------------------------------------------
# §3.2 Outlook mechanics
# ---------------------------------------------------------------------------


class OutlookItemNotFound(Exception):
    """The CURRENT desired target 404'd -- an item failure (retryable),
    distinct from a 404 on a PREVIOUS cleanup target (which is satisfied,
    not an error -- see `_strip_owned_categories`)."""

    def __init__(self, message_id: str):
        super().__init__(f"Outlook message {message_id} not found")
        self.message_id = message_id


def _strip_owned_categories(client: OutlookClient, message_id: str) -> str:
    """Remove the six CortexMail-owned category names from `message_id`,
    exact-name match only. Returns "cleaned" (done, or nothing owned was
    present) or "absent" (404 -- the message is gone, cleanup is
    satisfied, §3.2/P3-4). One re-merge on 412.
    """
    current = client.get_message_categories(message_id)
    if current is None:
        return "absent"
    kept = [c for c in current["categories"] if c not in OUTLOOK_OWNED_CATEGORY_NAMES]
    if kept == current["categories"]:
        return "cleaned"  # nothing owned present; no write needed
    resp = client.set_message_categories(message_id, kept, etag=current["etag"])
    if resp.status_code == 412:
        current = client.get_message_categories(message_id)
        if current is None:
            return "absent"
        kept = [c for c in current["categories"] if c not in OUTLOOK_OWNED_CATEGORY_NAMES]
        resp = client.set_message_categories(message_id, kept, etag=current["etag"])
    if resp.status_code == 404:
        return "absent"
    resp.raise_for_status()
    return "cleaned"


def _merge_desired_category(
    client: OutlookClient, message_id: str, desired_name: str | None
) -> None:
    """Merge `desired_name` onto message_id's categories, stripping any
    other CortexMail-owned name first (exact match only). A 404 here is the
    CURRENT target -- an item failure, raised as OutlookItemNotFound (§3.2/
    P3-4), never treated as satisfied. One re-merge on 412.
    """
    current = client.get_message_categories(message_id)
    if current is None:
        raise OutlookItemNotFound(message_id)
    merged = [c for c in current["categories"] if c not in OUTLOOK_OWNED_CATEGORY_NAMES]
    if desired_name:
        merged.append(desired_name)
    resp = client.set_message_categories(message_id, merged, etag=current["etag"])
    if resp.status_code == 412:
        current = client.get_message_categories(message_id)
        if current is None:
            raise OutlookItemNotFound(message_id)
        merged = [c for c in current["categories"] if c not in OUTLOOK_OWNED_CATEGORY_NAMES]
        if desired_name:
            merged.append(desired_name)
        resp = client.set_message_categories(message_id, merged, etag=current["etag"])
    if resp.status_code == 404:
        raise OutlookItemNotFound(message_id)
    resp.raise_for_status()


def strip_owned_outlook_categories(client: OutlookClient, message_id: str) -> str:
    """Public wrapper over `_strip_owned_categories` for §3.8 step 2's
    inherited-cleanup call sites."""
    return _strip_owned_categories(client, message_id)


def apply_outlook_category(
    client: OutlookClient,
    *,
    new_target: str,
    previous_target: str | None,
    desired_name: str | None,
) -> None:
    """§3.2 Outlook apply: if the desired target differs from the
    previously recorded one, FIRST strip the six owned names from the
    PREVIOUS target (404 = satisfied), THEN merge the desired category (or
    nothing, for a removal) onto the new target (404 = item failure,
    retryable). Both idempotent on retry; the new pair is only recorded by
    the caller after this whole call succeeds.
    """
    if previous_target and previous_target != new_target:
        _strip_owned_categories(client, previous_target)
    _merge_desired_category(client, new_target, desired_name)


# ---------------------------------------------------------------------------
# §3.8 step 5 (Gmail map persistence) and step 6 (record)
# ---------------------------------------------------------------------------


def persist_gmail_label_map(
    db: Session, account_id: UUID, *, generation: int, learned: dict[str, str]
) -> None:
    """§3.8 step 5: lock ONLY the ProviderAccount row (never together with a
    MailThread lock), re-read the generation -- if it moved past
    `generation`, drop the merge silently (an older generation must never
    repopulate a cache a re-enable just cleared); else read-merge-write
    `gmail_label_map`. Commits either way.
    """
    account = db.execute(
        select(ProviderAccount).where(ProviderAccount.id == account_id).with_for_update()
    ).scalar_one_or_none()
    if account is None:
        db.rollback()
        return
    if account.label_sync_generation != generation:
        db.rollback()
        return
    current = load_gmail_label_map(account.gmail_label_map)
    current.update(learned)
    account.gmail_label_map = json.dumps(current)
    db.commit()


def record_sync_result(
    db: Session,
    thread_id: UUID,
    claim_token: str,
    *,
    account_id: UUID,
    generation: int,
    applied_label: str | None,
    applied_message_id: str | None,
) -> str:
    """§3.8 step 6 (txn2): lock the MailThread row FOR UPDATE fenced on
    `claim_token`. Zero rowcount ("lease_lost") means the lease was stolen
    since the claim -- the winner records instead; this task's write is
    dropped, and any category it applied is recoverable via the pending
    target it wrote in step 1b. Otherwise re-checks the account generation:
    if it moved past `generation` ("generation_lost"), only the claim
    fields are cleared -- NOT label_resync_needed, NOT the applied pair --
    so the newer generation's tick reconverges from scratch and the
    pending target stays discoverable. Otherwise ("recorded") the applied
    pair, label_resync_needed, and the pending target are all cleared
    together with the claim.
    """
    locked = db.execute(
        select(MailThread)
        .where(MailThread.id == thread_id, MailThread.label_sync_claim_token == claim_token)
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        db.rollback()
        return "lease_lost"

    current_generation = db.execute(
        select(ProviderAccount.label_sync_generation).where(ProviderAccount.id == account_id)
    ).scalar_one_or_none()

    if current_generation is None or current_generation != generation:
        locked.label_sync_claim_token = None
        locked.label_sync_claimed_at = None
        db.commit()
        return "generation_lost"

    locked.synced_label = applied_label
    locked.synced_message_id = applied_message_id
    locked.label_resync_needed = False
    locked.label_sync_pending_target = None
    locked.label_sync_claim_token = None
    locked.label_sync_claimed_at = None
    db.commit()
    return "recorded"
