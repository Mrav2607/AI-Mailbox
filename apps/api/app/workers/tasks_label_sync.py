"""Label sync's Celery surface: `sync_thread_labels` runs the §3.8 ordering
guard end-to-end for one thread, and `label_sync_tick` is the beat sweep
that finds drifted threads and enqueues it (plan
docs/plans/2026-08-13-label-sync-plan.md §3.1/§3.8).

Every protocol step opens its OWN short-lived `SessionLocal()` -- there is
no single transaction spanning claim -> provider call -> record, by design
(the provider round trip is deliberately lockless, per §3.8). Token refresh
reuses ingest's own OAuth-exchange/invalid_grant-pause helpers verbatim
(never reimplemented here), the same way app/routes/reply.py does.
"""

from __future__ import annotations

from time import monotonic
from types import SimpleNamespace
from uuid import UUID

import httpx
from sqlalchemy import select, update

from .celery_app import celery_app
from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import MailThread, ProviderAccount
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.gmail_ingest import _refresh_access_token as _refresh_gmail_access_token
from app.services.ingest.outlook_client import OutlookClient
from app.services.ingest.outlook_ingest import (
    _refresh_and_persist_token as _refresh_outlook_access_token,
)
from app.services.label_sync.service import (
    AccountSnapshot,
    ClaimResult,
    apply_gmail_labels,
    apply_outlook_category,
    claim_thread,
    label_drift_clause,
    load_gmail_label_map,
    outlook_category_name,
    persist_gmail_label_map,
    record_sync_result,
    set_pending_target,
    strip_owned_outlook_categories,
    target_for_thread,
)

# Task deadline (§3.1) -- well below the 10-minute claim lease, so a live
# task can never keep writing after its lease is stolen out from under it.
_TASK_TIME_LIMIT = 480  # 8 minutes
_TASK_SOFT_TIME_LIMIT = 450

# Cumulative provider retry/backoff budget (§3.1), checked BETWEEN provider
# calls -- not preemptive mid-call. The shared Gmail/Outlook clients' own
# bounded 429/5xx retry loops (including Graph's Retry-After compliance,
# which they honor unbounded by design for ingest) aren't interruptible
# from out here; this stops the NEXT call from starting once the budget is
# spent, which is what actually matters for staying inside the task
# deadline and yielding the lease promptly.
_PROVIDER_RETRY_BUDGET_SECONDS = 60.0

# Bounded batch per account per tick (§3.4) -- Gmail thread.modify plus an
# occasional label-ensure keeps QPS trivial vs. quota even at this size.
_TICK_BATCH_PER_ACCOUNT = 200


class _BudgetExceeded(Exception):
    """Raised when this task's cumulative provider-retry budget is spent."""


class _Budget:
    def __init__(self, seconds: float = _PROVIDER_RETRY_BUDGET_SECONDS):
        self._deadline = monotonic() + seconds

    def check(self) -> None:
        if monotonic() > self._deadline:
            raise _BudgetExceeded()


def _make_client(provider: str, token: str) -> GmailClient | OutlookClient:
    return GmailClient(token) if provider == "gmail" else OutlookClient(token)


def _token_retry(account: AccountSnapshot):
    """Mirrors gmail_ingest.py's/outlook_ingest.py's with_token_retry +
    refresh_client shape, adapted for label sync's independent-session
    token refresh -- each protocol step opens/commits its own short
    session, so there's no shared page transaction to piggyback a token
    write onto the way ingest does.

    Returns (get_client, with_retry). `refresh()` propagates ValueError on
    a permanently dead grant -- the underlying helpers have ALREADY paused
    the account (same pause-write shape ingest uses) by the time that
    happens, so the caller only needs to stop, not pause again.
    """
    state = {"token": account.access_token, "refresh_token": account.refresh_token}
    holder = {"client": _make_client(account.provider, state["token"])}

    def refresh() -> bool:
        if account.provider == "gmail":
            fake_row = SimpleNamespace(id=account.id, refresh_token=state["refresh_token"])
            new_token, token_expiry = _refresh_gmail_access_token(fake_row)
            if not new_token:
                return False
            with SessionLocal() as db:
                db.execute(
                    update(ProviderAccount)
                    .where(ProviderAccount.id == account.id)
                    .values(access_token=new_token, token_expiry=token_expiry)
                )
                db.commit()
            state["token"] = new_token
        else:
            refreshed = _refresh_outlook_access_token(account.id, state["refresh_token"])
            if not refreshed:
                return False
            new_token, new_refresh_token, _expiry = refreshed
            state["token"] = new_token
            state["refresh_token"] = new_refresh_token
        holder["client"] = _make_client(account.provider, state["token"])
        return True

    def get_client() -> GmailClient | OutlookClient:
        return holder["client"]

    def with_retry(call):
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 401 and refresh():
                return call()
            raise

    return get_client, with_retry


@celery_app.task(bind=True, time_limit=_TASK_TIME_LIMIT, soft_time_limit=_TASK_SOFT_TIME_LIMIT)
def sync_thread_labels(self, thread_id: str) -> dict:
    """Run the §3.8 ordering guard for one thread, start to finish."""
    tid = UUID(thread_id)

    with SessionLocal() as db:
        claim: ClaimResult | None = claim_thread(db, tid)
    if claim is None:
        return {"status": "skipped", "thread_id": thread_id}

    account = claim.account
    budget = _Budget()
    get_client, with_retry = _token_retry(account)

    new_target = target_for_thread(
        desired_label=claim.desired_label,
        latest_message_id=claim.latest_message_id,
        synced_message_id=claim.synced_message_id,
        inherited_pending_target=claim.inherited_pending_target,
    )

    try:
        # §3.8 step 2 (Outlook, lockless): clean up an inherited pending
        # target from a dead task BEFORE this task overwrites it.
        if account.provider == "outlook" and claim.inherited_pending_target:
            budget.check()
            with_retry(
                lambda: strip_owned_outlook_categories(
                    get_client(), claim.inherited_pending_target
                )
            )
            if (
                claim.synced_message_id
                and claim.synced_message_id != claim.inherited_pending_target
            ):
                budget.check()
                with_retry(
                    lambda: strip_owned_outlook_categories(
                        get_client(), claim.synced_message_id
                    )
                )

        # §3.8 step 1b (txn1b): declare intent before touching the provider.
        with SessionLocal() as db:
            if not set_pending_target(db, tid, claim.claim_token, new_target):
                return {"status": "lease_lost", "thread_id": thread_id}

        applied_message_id: str | None = None
        learned_gmail_map: dict[str, str] | None = None

        # §3.8 step 4: provider apply (lockless).
        if account.provider == "gmail":
            cached_map = load_gmail_label_map(account.gmail_label_map)
            budget.check()
            result = with_retry(
                lambda: apply_gmail_labels(
                    get_client(),
                    provider_thread_id=claim.provider_thread_id,
                    cached_map=cached_map,
                    desired_label=claim.desired_label,
                )
            )
            learned_gmail_map = result.working_map
            if claim.desired_label is not None:
                applied_message_id = claim.latest_message_id
        else:
            desired_name = (
                outlook_category_name(claim.desired_label) if claim.desired_label else None
            )
            if new_target is not None:
                budget.check()
                with_retry(
                    lambda: apply_outlook_category(
                        get_client(),
                        new_target=new_target,
                        previous_target=claim.synced_message_id,
                        desired_name=desired_name,
                    )
                )
            if claim.desired_label is not None:
                applied_message_id = claim.latest_message_id

        # §3.8 step 5 (Gmail only): persist any newly learned label ids,
        # generation-fenced, own transaction, no MailThread lock held.
        if account.provider == "gmail" and learned_gmail_map is not None:
            with SessionLocal() as db:
                persist_gmail_label_map(
                    db, account.id, generation=claim.generation, learned=learned_gmail_map
                )

        # §3.8 step 6 (txn2): record what was actually applied.
        with SessionLocal() as db:
            outcome = record_sync_result(
                db,
                tid,
                claim.claim_token,
                account_id=account.id,
                generation=claim.generation,
                applied_label=claim.desired_label,
                applied_message_id=applied_message_id,
            )
        return {"status": outcome, "thread_id": thread_id}

    except _BudgetExceeded:
        logger.warning(
            "label sync retry budget exhausted for thread %s (account %s)",
            thread_id,
            account.id,
        )
        return {"status": "budget_exceeded", "thread_id": thread_id}
    except ValueError:
        # invalid_grant -- the refresh helper already paused the account
        # (see _token_retry's docstring); nothing left to do here.
        return {"status": "paused", "thread_id": thread_id}
    except Exception:
        logger.exception("label sync failed for thread %s", thread_id)
        return {"status": "error", "thread_id": thread_id}


@celery_app.task(ignore_result=True, time_limit=120)
def label_sync_tick() -> dict:
    """Beat sweep (§3.1/§3.8): for every label-sync-enabled, non-paused
    account, enqueue `sync_thread_labels` for its drifted threads (bounded
    batch, oldest thread activity first -- there's no dedicated "went
    stale at" column, so thread recency is the best available proxy for
    "been drifted the longest"). Per-item rollback + log + continue
    (REVIEW.md's fan-out isolation, mirrors tasks_ingest.dispatch_scheduled_syncs)
    -- one account's bad state must never stop the sweep for the rest.
    """
    accounts_swept = 0
    enqueued = 0
    failed = 0
    with SessionLocal() as db:
        account_ids = db.execute(
            select(ProviderAccount.id).where(
                ProviderAccount.label_sync_enabled.is_(True),
                ProviderAccount.sync_paused_at.is_(None),
            )
        ).scalars().all()

        for account_id in account_ids:
            accounts_swept += 1
            try:
                thread_ids = db.execute(
                    select(MailThread.id)
                    .where(
                        MailThread.provider_account_id == account_id,
                        label_drift_clause(),
                    )
                    .order_by(
                        MailThread.last_message_at.asc().nullsfirst(),
                        MailThread.created_at.asc(),
                    )
                    .limit(_TICK_BATCH_PER_ACCOUNT)
                ).scalars().all()
                for tid in thread_ids:
                    sync_thread_labels.delay(str(tid))
                    enqueued += 1
            except Exception:
                db.rollback()
                failed += 1
                logger.exception("label sync tick failed for account %s", account_id)

    return {"accounts_swept": accounts_swept, "enqueued": enqueued, "failed": failed}
