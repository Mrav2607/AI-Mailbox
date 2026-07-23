"""Outlook mail ingest: Graph delta walk over inbox + sentitems.

Unlike Gmail's history-cursor-plus-listing model, Graph's delta API is a
self-contained resumable walk per folder: a cursor url is either an interim
`@odata.nextLink` (more pages waiting) or a settled `@odata.deltaLink` (caught
up). A folder with no cursor yet starts a bounded "baseline" generation --
Graph's filtered baseline walk truncates silently past `OUTLOOK_DELTA_CAP`
messages, so we count what we saw and re-baseline with a narrower lookback
window when we suspect that happened (see the cap-detection block below).

Commits are page-granular: a page's additions, its verified removals, and its
cursor advance land in one transaction, so a crash mid-page replays that page
from the old cursor (upserts are idempotent, removals get re-verified) and a
cursor never advances past removals that weren't actually applied.

Token refresh persists through its OWN session (`SessionLocal`), never the
ingest session passed in as `db`. A mid-page 401 that triggers a refresh must
not make that page's partial work durable before its cursor advances -- which
is exactly what would happen if refreshing committed the caller's session (as
Gmail's `with_token_retry` does; see gmail_ingest.py:283). So the refreshed
access/refresh tokens live only in local closures here until the next full
page commit, and are persisted to the DB independently of it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import Classification, MailMessage, MailThread, ProviderAccount
from app.services.ingest.gmail_ingest import _pause_provider, _should_reopen_thread
from app.services.ingest.normalizer import normalize_outlook_message
from app.services.ingest.outlook_client import (
    OUTLOOK_DELTA_CAP,
    DeltaExpiredError,
    OutlookClient,
)
from app.services.nlp.classifier import build_classification_text, classify
from app.services.nlp.persistence import upsert_classification

# Inbox first: it's the folder users actually triage, so it gets first claim
# on a bounded run's message/page budget.
_FOLDERS = ("inbox", "sentitems")
# Verification GETs are the expensive part of a removal (a live Graph round
# trip per candidate) -- capped per folder per run, enforced at page
# boundaries so a run always makes forward progress (see module docstring).
_REMOVAL_VERIFY_BUDGET = 200


def _ms_token_url() -> str:
    return f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0/token"


def _load_cursors(account: Any) -> dict[str, Any]:
    """Parse ``account.outlook_delta_cursors``.

    ``None`` or malformed JSON both recover to ``{}`` (a warning is logged for
    the malformed case) -- a corrupt cursor blob must never abort ingest, it
    should just look like a fresh account and re-baseline both folders.
    """
    raw = getattr(account, "outlook_delta_cursors", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "outlook_delta_cursors for provider %s was malformed JSON; resetting",
            getattr(account, "id", None),
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "outlook_delta_cursors for provider %s was not a JSON object; resetting",
            getattr(account, "id", None),
        )
        return {}
    return parsed


def _save_cursor(
    db: Session,
    account: Any,
    folder_key: str,
    *,
    url: str | None,
    baseline_complete: bool,
    baseline_count: int,
    baseline_days: int,
) -> dict[str, Any]:
    """Read-modify-write ``folder_key``'s entry, preserving the sibling folder's.

    Writes the whole cursors blob onto ``account`` in the caller's transaction
    (no commit here -- the caller's page transaction owns that). Returns the
    full merged map so the caller can check both folders' completion state
    without a second parse.
    """
    cursors = _load_cursors(account)
    cursors[folder_key] = {
        "url": url,
        "baseline_complete": baseline_complete,
        "baseline_count": baseline_count,
        "baseline_days": baseline_days,
    }
    account.outlook_delta_cursors = json.dumps(cursors)
    return cursors


def _is_permanent_auth_error(resp: httpx.Response) -> bool:
    # invalid_grant covers both a revoked refresh token and Azure's own
    # consent-revoked variant (AADSTS65001 rides in error_description, not a
    # dedicated error code) -- either way, only reconnecting fixes it.
    if resp.status_code != 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    if body.get("error") == "invalid_grant":
        return True
    description = str(body.get("error_description") or "")
    return "aadsts65001" in description.lower()


def _refresh_and_persist_token(
    provider_id: Any, refresh_token: str | None
) -> tuple[str, str, datetime | None] | None:
    """Exchange ``refresh_token`` at the MS token endpoint and persist the
    result through an independent session.

    Returns ``(access_token, refresh_token, token_expiry)`` on success, or
    ``None`` when there's nothing to refresh with (no refresh token, or
    Microsoft OAuth isn't configured). Raises ``ValueError`` -- the caller's
    non-retryable terminal branch, mirroring `_pause_provider` -- when
    Microsoft reports the grant is permanently dead.
    """
    if not refresh_token or not settings.microsoft_client_id or not settings.microsoft_client_secret:
        return None

    resp = httpx.post(
        _ms_token_url(),
        data={
            "client_id": settings.microsoft_client_id,
            "client_secret": settings.microsoft_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    if _is_permanent_auth_error(resp):
        _pause_provider(provider_id, "reauth_required")
        raise ValueError("Outlook authorization was revoked. Reconnect the account.")
    resp.raise_for_status()
    data = resp.json()
    access_token = data.get("access_token")
    # Microsoft may or may not rotate the refresh token on a given exchange;
    # keep the old one when it doesn't so the next refresh still has one.
    new_refresh_token = data.get("refresh_token") or refresh_token
    expires_in = data.get("expires_in")
    token_expiry = None
    if expires_in:
        token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    # Independent session and commit -- see module docstring. This must never
    # be the `db` the caller passed in, or a mid-page refresh would make that
    # page's not-yet-finished work durable before its cursor advances.
    with SessionLocal() as tok_db:
        tok_db.execute(
            update(ProviderAccount)
            .where(ProviderAccount.id == provider_id)
            .values(
                access_token=access_token,
                refresh_token=new_refresh_token,
                token_expiry=token_expiry,
            )
        )
        tok_db.commit()

    return access_token, new_refresh_token, token_expiry


def _locally_stored_removal_rows(
    db: Session, provider_account_id: Any, candidate_ids: list[str]
) -> list[tuple[Any, str, Any]]:
    """(message id, provider_message_id, thread id) for the candidates that
    are actually stored for this account -- ids Graph reports removed but we
    never had (or belong to a different account) need no verification at all.
    """
    if not candidate_ids:
        return []
    return (
        db.execute(
            select(MailMessage.id, MailMessage.provider_message_id, MailMessage.thread_id)
            .join(MailThread, MailThread.id == MailMessage.thread_id)
            .where(
                MailThread.provider_account_id == provider_account_id,
                MailMessage.provider_message_id.in_(candidate_ids),
            )
        )
        .all()
    )


def _finalize_thread_after_removal(db: Session, thread_id: Any) -> None:
    """Recompute recency for a thread that just lost a message, or drop it if
    that was the last one. Subject is deliberately left untouched -- messages
    don't store a subject of their own, so refetching one just to keep a
    thread's subject "fresh" after a deletion isn't worth another call.
    """
    remaining_sent_ats = (
        db.execute(
            select(MailMessage.sent_at)
            .where(MailMessage.thread_id == thread_id)
            .order_by(MailMessage.sent_at.desc().nullslast())
        )
        .scalars()
        .all()
    )
    if not remaining_sent_ats:
        db.execute(delete(MailThread).where(MailThread.id == thread_id))
        return
    newest = next((sent_at for sent_at in remaining_sent_ats if sent_at is not None), None)
    if newest is not None:
        db.execute(
            update(MailThread).where(MailThread.id == thread_id).values(last_message_at=newest)
        )


def _apply_removals(
    db: Session, client: OutlookClient, provider_account_id: Any, removed_ids: list[str]
) -> dict[str, int]:
    """Verify a page's Graph-reported removals before trusting them.

    Graph reports `@removed` for a message moved out of the watched folder
    too, not just real deletions -- only a live 404 on the message id means
    it's actually gone. Dedupes first since the same id can legitimately
    repeat across a delta page's own change records.
    """
    deduped = list(dict.fromkeys(removed_ids))
    rows = _locally_stored_removal_rows(db, provider_account_id, deduped)

    verified = 0
    deleted = 0
    kept = 0
    affected_threads: set[Any] = set()
    for message_id, provider_message_id, thread_id in rows:
        exists = client.get_message(provider_message_id)
        verified += 1
        if exists is None:
            db.execute(delete(MailMessage).where(MailMessage.id == message_id))
            deleted += 1
            affected_threads.add(thread_id)
        else:
            # Still exists elsewhere in the mailbox (moved) -- matches Gmail's
            # keep-archived behavior, we don't chase folder membership.
            kept += 1

    for thread_id in affected_threads:
        _finalize_thread_after_removal(db, thread_id)

    return {"verified": verified, "deleted": deleted, "kept": kept}


_THREAD_INDEX_ELEMENTS = ["provider_account_id", "provider_thread_id"]
_MESSAGE_INDEX_ELEMENTS = ["thread_id", "provider_message_id"]
_MESSAGE_COLUMNS = (
    "sender",
    "recipient",
    "cc",
    "bcc",
    "sent_at",
    "snippet",
    "body_text",
    "body_html",
    "headers",
)


def _upsert_page_messages(
    db: Session,
    user_id: str,
    provider: ProviderAccount,
    folder_key: str,
    messages: list[dict[str, Any]],
    classify_messages: bool,
) -> dict[str, int]:
    """Upsert one delta page's messages (and their threads) for one folder."""
    threads_upserted = 0
    messages_upserted = 0
    classified = 0

    for raw in messages:
        normalized = normalize_outlook_message(raw, folder_key)
        provider_message_id = normalized.get("provider_message_id")
        provider_thread_id = normalized.get("provider_thread_id")
        if not provider_message_id or not provider_thread_id:
            continue

        sent_at = normalized.get("sent_at") or datetime.now(timezone.utc)
        thread_stmt = (
            insert(MailThread)
            .values(
                user_id=user_id,
                provider_account_id=provider.id,
                provider="outlook",
                provider_thread_id=provider_thread_id,
                subject=normalized.get("subject"),
                last_message_at=sent_at,
            )
            .on_conflict_do_update(
                index_elements=_THREAD_INDEX_ELEMENTS,
                # Subject is set only at creation (see _finalize_thread_after_removal);
                # recency only ever moves forward, since delta pages aren't
                # guaranteed to arrive in chronological order.
                set_={"last_message_at": func.greatest(MailThread.last_message_at, sent_at)},
            )
            .returning(MailThread.id)
        )
        thread_id = db.execute(thread_stmt).scalar_one()
        threads_upserted += 1

        should_reopen = False
        thread_state = db.get(MailThread, thread_id)
        done_at = thread_state.done_at if thread_state else None
        if done_at is not None:
            existing_message_ids = set(
                db.execute(
                    select(MailMessage.provider_message_id).where(
                        MailMessage.thread_id == thread_id
                    )
                ).scalars()
            )
            should_reopen = _should_reopen_thread(done_at, existing_message_ids, [normalized])

        message_values = {column: normalized.get(column) for column in _MESSAGE_COLUMNS}
        message_stmt = (
            insert(MailMessage)
            .values(
                thread_id=thread_id,
                provider_message_id=provider_message_id,
                **message_values,
            )
            .on_conflict_do_update(
                index_elements=_MESSAGE_INDEX_ELEMENTS,
                set_=message_values,
            )
            .returning(MailMessage.id)
        )
        new_message_id = db.execute(message_stmt).scalar_one()
        messages_upserted += 1

        if classify_messages:
            existing = (
                db.execute(
                    select(Classification).where(Classification.message_id == new_message_id)
                )
                .scalars()
                .first()
            )
            if not existing:
                text_for_classification = build_classification_text(
                    normalized.get("subject"),
                    normalized.get("snippet"),
                    normalized.get("body_text"),
                )
                label, confidence, rationale, model_version = classify(text_for_classification)
                upsert_classification(
                    db,
                    message_id=new_message_id,
                    label=label,
                    confidence=confidence,
                    rationale=rationale,
                    model_version=model_version,
                )
                classified += 1

        if should_reopen:
            db.execute(update(MailThread).where(MailThread.id == thread_id).values(done_at=None))

    return {
        "threads_upserted": threads_upserted,
        "messages_upserted": messages_upserted,
        "classified": classified,
    }


def ingest_outlook_messages(
    db: Session,
    user_id: str,
    provider_account_id: str | None = None,
    max_results: int = 50,
    max_pages: int = 20,
    classify_messages: bool = True,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Pull Outlook mail via Graph's delta API for inbox + sentitems.

    ``max_results`` is the total message cap across both folders (inbox
    walked first); ``max_pages`` caps delta pages fetched across both folders
    in this run. Each page's additions, verified removals, and cursor advance
    commit together -- a failure mid-page never advances that folder's cursor
    (see module docstring for the full contract).
    """
    if provider_account_id is not None:
        provider = (
            db.execute(
                select(ProviderAccount).where(
                    ProviderAccount.id == provider_account_id,
                    ProviderAccount.user_id == user_id,
                )
            )
            .scalars()
            .first()
        )
    else:
        provider = (
            db.execute(
                select(ProviderAccount)
                .where(
                    ProviderAccount.user_id == user_id, ProviderAccount.provider == "outlook"
                )
                .order_by(ProviderAccount.created_at)
            )
            .scalars()
            .first()
        )
    if not provider or not provider.access_token:
        raise ValueError("Outlook provider account not connected.")

    access_token = provider.access_token
    current_refresh_token = provider.refresh_token
    client = OutlookClient(access_token)

    def refresh_client() -> bool:
        """Refresh lazily -- only ever called after a 401. Persists through
        its own session; never touches or commits `db`."""
        nonlocal client, current_refresh_token
        refreshed = _refresh_and_persist_token(provider.id, current_refresh_token)
        if not refreshed:
            return False
        new_access_token, new_refresh_token, _ = refreshed
        current_refresh_token = new_refresh_token
        client = OutlookClient(new_access_token)
        return True

    def with_token_retry(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 401 and refresh_client():
                return call()
            raise

    remaining = max_results
    pages_done = 0
    stats = {
        "threads_upserted": 0,
        "messages_upserted": 0,
        "classified": 0,
        "messages_removed": 0,
        "fetched": 0,
    }

    for folder_key in _FOLDERS:
        if remaining <= 0 or pages_done >= max_pages:
            break

        cursors = _load_cursors(provider)
        entry = cursors.get(folder_key) or {}
        cursor_url = entry.get("url")
        baseline_complete = bool(entry.get("baseline_complete", False))
        baseline_count = int(entry.get("baseline_count", 0))
        baseline_days = int(entry.get("baseline_days") or settings.outlook_backfill_days)
        removal_budget_used = 0

        while pages_done < max_pages and remaining > 0:
            page_size = min(50, remaining)
            received_after = None
            if not cursor_url:
                received_after = datetime.now(timezone.utc) - timedelta(days=baseline_days)

            try:
                page = with_token_retry(
                    lambda folder_key=folder_key,
                    cursor_url=cursor_url,
                    received_after=received_after,
                    page_size=page_size: client.delta_page(
                        folder_key=folder_key if not cursor_url else None,
                        cursor_url=cursor_url,
                        received_after=received_after,
                        page_size=page_size,
                    )
                )
            except DeltaExpiredError:
                # New baseline generation for this folder -- the aggregate
                # outlook_backfill_complete column is never touched here (it's
                # ever-completed, not current-generation).
                _save_cursor(
                    db,
                    provider,
                    folder_key,
                    url=None,
                    baseline_complete=False,
                    baseline_count=0,
                    baseline_days=settings.outlook_backfill_days,
                )
                db.commit()
                logger.warning(
                    "outlook delta expired for folder=%s provider=%s; re-baselining",
                    folder_key,
                    provider.id,
                )
                break

            messages = page["messages"]
            removed_ids = page["removed_ids"]
            next_url = page["next_url"]
            delta_url = page["delta_url"]

            deduped_removed = list(dict.fromkeys(removed_ids))
            local_removal_rows = _locally_stored_removal_rows(
                db, provider.id, deduped_removed
            )
            if len(local_removal_rows) > (_REMOVAL_VERIFY_BUDGET - removal_budget_used):
                # This page's removals alone would blow the per-folder budget --
                # defer the WHOLE page (additions included) so the cursor never
                # advances past unverified removals. Next run gets a fresh
                # budget, so this can't stall forever on its own -- and any
                # other folder in this same run still makes progress.
                break

            upsert_stats = _upsert_page_messages(
                db, user_id, provider, folder_key, messages, classify_messages
            )
            removal_stats = with_token_retry(
                lambda deduped_removed=deduped_removed: _apply_removals(
                    db, client, provider.id, deduped_removed
                )
            )
            removal_budget_used += removal_stats["verified"]

            if not baseline_complete:
                baseline_count += len(messages)

            if delta_url:
                if not baseline_complete and baseline_count >= OUTLOOK_DELTA_CAP and baseline_days > 7:
                    new_days = max(7, baseline_days // 2)
                    logger.warning(
                        "outlook baseline for folder=%s provider=%s hit the delta cap at a "
                        "%sd window; narrowing to %sd",
                        folder_key,
                        provider.id,
                        baseline_days,
                        new_days,
                    )
                    merged = _save_cursor(
                        db,
                        provider,
                        folder_key,
                        url=None,
                        baseline_complete=False,
                        baseline_count=0,
                        baseline_days=new_days,
                    )
                else:
                    if not baseline_complete and baseline_count >= OUTLOOK_DELTA_CAP:
                        logger.warning(
                            "outlook baseline for folder=%s provider=%s hit the delta cap at "
                            "the %sd floor window; accepting possible gap",
                            folder_key,
                            provider.id,
                            baseline_days,
                        )
                    merged = _save_cursor(
                        db,
                        provider,
                        folder_key,
                        url=delta_url,
                        baseline_complete=True,
                        baseline_count=baseline_count,
                        baseline_days=baseline_days,
                    )
            else:
                merged = _save_cursor(
                    db,
                    provider,
                    folder_key,
                    url=next_url,
                    baseline_complete=baseline_complete,
                    baseline_count=baseline_count,
                    baseline_days=baseline_days,
                )

            # Ever-completed aggregate: only ever set True, never unset --
            # a later re-baseline of one folder must not flip this back off.
            if not provider.outlook_backfill_complete:
                inbox_done = bool((merged.get("inbox") or {}).get("baseline_complete"))
                sent_done = bool((merged.get("sentitems") or {}).get("baseline_complete"))
                if inbox_done and sent_done:
                    provider.outlook_backfill_complete = True

            db.commit()

            stats["threads_upserted"] += upsert_stats["threads_upserted"]
            stats["messages_upserted"] += upsert_stats["messages_upserted"]
            stats["classified"] += upsert_stats["classified"]
            stats["messages_removed"] += removal_stats["deleted"]
            stats["fetched"] += len(messages)

            remaining -= len(messages)
            pages_done += 1
            if progress:
                progress()

            if delta_url:
                # Caught up (or just re-baselined into a fresh generation) --
                # leave the rest of this generation's walk for a later run.
                break
            cursor_url = next_url

    return stats
