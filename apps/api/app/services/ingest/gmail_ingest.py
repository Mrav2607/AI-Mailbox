from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import MailThread, MailMessage, ProviderAccount, Classification
from app.core.config import settings
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.normalizer import normalize_message
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.persistence import upsert_classification


def _collect_history_thread_ids(
    client: GmailClient, start_history_id: str
) -> tuple[list[str], str]:
    """Return each thread changed since a Gmail history cursor exactly once."""
    changed: list[str] = []
    seen: set[str] = set()
    page_token: str | None = None
    latest_history_id = start_history_id
    while True:
        page = client.list_history(start_history_id, page_token=page_token)
        latest_history_id = str(page.get("historyId") or latest_history_id)
        for record in page.get("history", []):
            for addition in record.get("messagesAdded", []):
                tid = addition.get("message", {}).get("threadId")
                if tid and tid not in seen:
                    seen.add(tid)
                    changed.append(tid)
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return changed, latest_history_id


def _merge_sync_thread_ids(
    history_ids: list[str], recent_ids: list[str], listed_ids: list[str]
) -> list[str]:
    """Prioritize changed threads, then reconciliation and backfill work."""
    return list(dict.fromkeys([*history_ids, *recent_ids, *listed_ids]))


def _count_new_threads(
    history_ids: list[str], listed_ids: list[str], head_new: int, existing_ids: set[str]
) -> int:
    genuinely_new = {tid for tid in history_ids if tid not in existing_ids}
    genuinely_new.update(listed_ids[:head_new])
    return len(genuinely_new)


def _should_reopen_thread(
    done_at: datetime | None,
    existing_message_ids: set[str],
    messages: list[dict[str, Any]],
) -> bool:
    if done_at is None:
        return False
    return any(
        message.get("provider_message_id") not in existing_message_ids
        and message.get("sent_at") is not None
        and message["sent_at"] > done_at
        and "SENT" not in message.get("label_ids", [])
        for message in messages
    )


def _refresh_access_token(provider: ProviderAccount) -> tuple[str | None, datetime | None]:
    if not provider.refresh_token:
        return None, None
    if not settings.google_client_id or not settings.google_client_secret:
        return None, None

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": provider.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    access_token = data.get("access_token")
    expires_in = data.get("expires_in")
    token_expiry = None
    if expires_in:
        token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return access_token, token_expiry


def ingest_gmail_messages(
    db: Session,
    user_id: str,
    max_results: int = 50,
    skip_existing: bool = True,
    max_pages: int = 20,
    classify_messages: bool = True,
    # Threads per transaction. Each one costs a Gmail round-trip plus a classify
    # call per message, so a big batch means a connection sitting
    # idle-in-transaction across minutes of network and inference -- which blocks
    # vacuum and holds locks. Commit often; the upserts make a replay harmless.
    commit_every: int = 5,
    new_only: bool = False,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    provider = (
        db.execute(
            select(ProviderAccount).where(
                ProviderAccount.user_id == user_id, ProviderAccount.provider == "gmail"
            )
        )
        .scalars()
        .first()
    )
    if not provider or not provider.access_token:
        raise ValueError("Gmail provider account not connected.")

    access_token = provider.access_token
    if provider.token_expiry and provider.token_expiry <= datetime.now(timezone.utc):
        refreshed_token, token_expiry = _refresh_access_token(provider)
        if refreshed_token:
            provider.access_token = refreshed_token
            provider.token_expiry = token_expiry
            db.commit()
            access_token = refreshed_token

    client = GmailClient(access_token)

    # Existing IDs serve two purposes: historical backfill skips them, while
    # incremental history sync deliberately re-fetches them when Gmail reports
    # a new message in an existing conversation.
    existing_thread_ids: set[str] = set()
    if skip_existing:
        existing_thread_ids = set(
            db.execute(
                select(MailThread.provider_thread_id).where(MailThread.user_id == user_id)
            )
            .scalars()
            .all()
        )

    # New-only mode has nothing to anchor "new" against on an empty mailbox —
    # pulling would mean importing the whole account. Auto-sync waits for a
    # manual ingest to establish the baseline (and the history cursor).
    if new_only and skip_existing and not existing_thread_ids:
        return {
            "threads_upserted": 0,
            "messages_upserted": 0,
            "classified": 0,
            "fetched": 0,
            "skipped_existing": 0,
            "new_threads": 0,
        }

    # Threads this run walked past because we already had them. Not the same as
    # len(existing_thread_ids), which is every thread we've ever ingested -- that
    # number told the operator nothing about what just happened.
    skipped_existing = 0

    def collect_listed_thread_ids(
        *, include_recent: bool = False
    ) -> tuple[list[str], int, list[str], bool]:
        """Walk Gmail's newest-first listing collecting unknown threads.

        Also counts how many of them sit BEFORE the first already-known
        thread: those actually arrived since the last pull, as opposed to
        older history the DB just hasn't backfilled yet. ``include_recent`` is
        used when establishing/recovering a history cursor so recent known
        threads are reconciled once as well.
        """
        nonlocal skipped_existing
        new_ids: list[str] = []
        recent_ids: list[str] = []
        head_new = 0
        seen_known = False
        seen: set[str] = set()
        page_token: str | None = None
        pages = 0
        # New-only pulls take every head thread even past max_results: capping
        # a burst would strand the remainder below a now-known thread, where
        # no later head scan could ever see it.
        while (
            len(new_ids) < max_results or (new_only and not seen_known)
        ) and pages < max_pages:
            batch = client.list_threads(max_results=500, page_token=page_token)
            for thread in batch.get("threads", []):
                tid = thread["id"]
                if tid in seen:
                    continue
                seen.add(tid)
                if include_recent and len(recent_ids) < max_results:
                    recent_ids.append(tid)
                if skip_existing and tid in existing_thread_ids:
                    seen_known = True
                    skipped_existing += 1
                    continue
                if not seen_known:
                    head_new += 1
                new_ids.append(tid)
                if len(new_ids) >= max_results and not (new_only and not seen_known):
                    break
            pages += 1
            page_token = batch.get("nextPageToken")
            if not page_token:
                break
        # Reaching the requested unknown-thread limit may have stopped midway
        # through the final Gmail page even when no nextPageToken exists.
        # Treat that as incomplete; one harmless follow-up pass will prove the
        # mailbox is exhausted and then disable future listing scans.
        exhausted = not page_token and len(new_ids) < max_results
        # Head threads always survive the cap (see the loop condition), so a
        # new-only caller gets the complete burst.
        kept = max(max_results, head_new) if new_only else max_results
        return new_ids[:kept], head_new, recent_ids, exhausted

    def refresh_client() -> bool:
        """Refresh the access token and rebuild the client. Returns success."""
        nonlocal client
        refreshed_token, token_expiry = _refresh_access_token(provider)
        if not refreshed_token:
            return False
        provider.access_token = refreshed_token
        provider.token_expiry = token_expiry
        db.commit()
        client = GmailClient(refreshed_token)
        return True

    def with_token_retry(call):
        """Run a Gmail call, refreshing the token once on a 401 and retrying.
        The access token expires ~1h in, so a large pull can outlive it."""
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 401 and refresh_client():
                return call()
            raise

    # History is the source of truth for replies in already-known Gmail
    # threads. The listing remains in the loop solely to keep importing older
    # unknown history up to max_results per run.
    history_ids: list[str] = []
    next_history_id = provider.gmail_history_id
    reconcile_recent = not skip_existing or not next_history_id
    if skip_existing and next_history_id:
        try:
            history_ids, next_history_id = with_token_retry(
                lambda: _collect_history_thread_ids(client, next_history_id or "")
            )
        except httpx.HTTPStatusError as exc:
            # Gmail returns 404 when a history cursor has aged out. Reconcile
            # recent threads and establish a fresh cursor instead of failing
            # every future sync with the same unusable value.
            if exc.response is None or exc.response.status_code != 404:
                raise
            reconcile_recent = True
            next_history_id = None

    # Capture the replacement cursor before listing/fetching. Mail arriving
    # afterward is safely replayed by the next history call; advancing to a
    # cursor captured after the fetch could skip that race window.
    if not next_history_id:
        profile = with_token_retry(client.get_profile)
        next_history_id = str(profile["historyId"])

    # New-only pulls never walk the listing for backfill: with a live cursor,
    # history alone covers every arrival, so listing only runs to establish or
    # recover that cursor (and reconcile recents while at it).
    should_list = (
        not skip_existing
        or reconcile_recent
        or (not new_only and not provider.gmail_backfill_complete)
    )
    if should_list:
        listed_ids, head_new, recent_ids, listing_exhausted = with_token_retry(
            lambda: collect_listed_thread_ids(include_recent=reconcile_recent)
        )
    else:
        listed_ids, head_new, recent_ids, listing_exhausted = [], 0, [], True
    if not skip_existing:
        # Explicit full refresh keeps its existing meaning: re-fetch the first
        # max_results threads from Gmail's newest-first listing.
        thread_ids = recent_ids
    else:
        # New-only keeps just the head of the listing — threads above the
        # first already-known one — and drops the older backfill tail.
        thread_ids = _merge_sync_thread_ids(
            history_ids,
            recent_ids,
            listed_ids[:head_new] if new_only else listed_ids,
        )

    new_threads = (
        _count_new_threads(history_ids, listed_ids, head_new, existing_thread_ids)
        if skip_existing
        else 0
    )

    threads_upserted = 0
    messages_upserted = 0
    classified = 0
    threads_reopened = 0
    threads_missing = 0

    for index, tid in enumerate(thread_ids):
        try:
            raw_thread = with_token_retry(lambda tid=tid: client.get_thread(tid))
        except httpx.HTTPStatusError as exc:
            # Skip deleted threads
            if exc.response is None or exc.response.status_code != 404:
                raise
            threads_missing += 1
            if tid not in existing_thread_ids:
                new_threads = max(0, new_threads - 1)
            continue

        normalized_msgs = [normalize_message(m) for m in raw_thread.get("messages", [])]
        normalized_msgs = [
            n
            for n in normalized_msgs
            if n.get("provider_message_id") and n.get("provider_thread_id")
        ]
        if not normalized_msgs:
            continue

        # Thread-level fields come from the newest message in the thread.
        _epoch = datetime.min.replace(tzinfo=timezone.utc)
        latest = max(normalized_msgs, key=lambda n: n.get("sent_at") or _epoch)

        # On re-fetch, only move last_message_at when we actually know the
        # send time — stamping now() would fabricate "new mail" for every
        # watermark/recency consumer downstream.
        update_cols: dict[str, Any] = {"subject": latest.get("subject")}
        if latest.get("sent_at"):
            update_cols["last_message_at"] = latest["sent_at"]
        thread_stmt = (
            insert(MailThread)
            .values(
                user_id=user_id,
                provider="gmail",
                provider_thread_id=tid,
                subject=latest.get("subject"),
                last_message_at=latest.get("sent_at") or datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "provider", "provider_thread_id"],
                set_=update_cols,
            )
            .returning(MailThread.id)
        )
        thread_id = db.execute(thread_stmt).scalar_one()
        threads_upserted += 1

        thread_state = db.get(MailThread, thread_id)
        done_at = thread_state.done_at if thread_state else None
        existing_message_ids: set[str] = set()
        # Existing IDs only matter when checking whether a done thread reopened.
        if done_at is not None:
            existing_message_ids = set(
                db.execute(
                    select(MailMessage.provider_message_id).where(
                        MailMessage.thread_id == thread_id
                    )
                ).scalars()
            )
        should_reopen = _should_reopen_thread(
            done_at, existing_message_ids, normalized_msgs
        )

        for normalized in normalized_msgs:
            message_stmt = (
                insert(MailMessage)
                .values(
                    thread_id=thread_id,
                    provider_message_id=normalized["provider_message_id"],
                    sender=normalized.get("sender"),
                    recipient=normalized.get("recipient"),
                    cc=normalized.get("cc"),
                    bcc=normalized.get("bcc"),
                    sent_at=normalized.get("sent_at"),
                    snippet=normalized.get("snippet"),
                    body_text=normalized.get("body_text"),
                    body_html=normalized.get("body_html"),
                    headers=normalized.get("headers"),
                )
                .on_conflict_do_update(
                    index_elements=["thread_id", "provider_message_id"],
                    set_={
                        "sender": normalized.get("sender"),
                        "recipient": normalized.get("recipient"),
                        "cc": normalized.get("cc"),
                        "bcc": normalized.get("bcc"),
                        "sent_at": normalized.get("sent_at"),
                        "snippet": normalized.get("snippet"),
                        "body_text": normalized.get("body_text"),
                        "body_html": normalized.get("body_html"),
                        "headers": normalized.get("headers"),
                    },
                )
                .returning(MailMessage.id)
            )
            new_message_id = db.execute(message_stmt).scalar_one()
            messages_upserted += 1

            # Inline classification runs the classifier once PER message -- fine
            # for interactive pulls, but skipped when classify_messages is False
            # (the data-gathering path re-labels separately).
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
            db.execute(
                update(MailThread)
                .where(MailThread.id == thread_id)
                .values(done_at=None)
            )
            threads_reopened += 1

        # Commit per thread so a late failure (e.g. token death mid-pull) keeps
        # every thread fetched so far instead of rolling the whole run back.
        if commit_every and (index + 1) % commit_every == 0:
            db.commit()
        if progress:
            progress()

    # The cursor advances only after all affected threads are durable. A worker
    # failure before this point replays the same history safely via upserts.
    provider.gmail_history_id = next_history_id
    # A head-only walk proves nothing about the deep listing, so new-only
    # runs never flip the backfill flag.
    if skip_existing and should_list and listing_exhausted and not new_only:
        provider.gmail_backfill_complete = True
    db.commit()
    return {
        "threads_upserted": threads_upserted,
        "messages_upserted": messages_upserted,
        "classified": classified,
        "threads_reopened": threads_reopened,
        "fetched": len(thread_ids),
        "skipped_existing": skipped_existing,
        "threads_missing": threads_missing,
        # Genuinely new arrivals, not backfilled history — threads_upserted
        # counts both, so it can't distinguish "you have mail" from "the DB
        # is still catching up". Meaningful only with skip_existing.
        "new_threads": new_threads,
    }
