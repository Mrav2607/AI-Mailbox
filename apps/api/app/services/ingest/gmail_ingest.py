from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import MailThread, MailMessage, ProviderAccount, Classification
from app.core.config import settings
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.normalizer import normalize_message
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.persistence import upsert_classification


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
    commit_every: int = 50,
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

    # Threads already ingested for this user, so repeat pulls resume at the next
    # NEW thread instead of re-fetching the same newest page every time. Keying
    # on threads (not messages) is what makes `max_results` mean "threads": the
    # unit a triage operator actually thinks in.
    existing_thread_ids: set[str] = set()
    if skip_existing:
        existing_thread_ids = set(
            db.execute(
                select(MailThread.provider_thread_id).where(MailThread.user_id == user_id)
            )
            .scalars()
            .all()
        )

    def collect_new_thread_ids() -> list[str]:
        new_ids: list[str] = []
        seen: set[str] = set()
        page_token: str | None = None
        pages = 0
        while len(new_ids) < max_results and pages < max_pages:
            batch = client.list_threads(max_results=500, page_token=page_token)
            for thread in batch.get("threads", []):
                tid = thread["id"]
                if tid in seen:
                    continue
                seen.add(tid)
                if skip_existing and tid in existing_thread_ids:
                    continue
                new_ids.append(tid)
                if len(new_ids) >= max_results:
                    break
            pages += 1
            page_token = batch.get("nextPageToken")
            if not page_token:
                break
        return new_ids[:max_results]

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

    thread_ids = with_token_retry(collect_new_thread_ids)

    threads_upserted = 0
    messages_upserted = 0
    classified = 0

    for index, tid in enumerate(thread_ids):
        raw_thread = with_token_retry(lambda: client.get_thread(tid))

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
                set_={
                    "subject": latest.get("subject"),
                    "last_message_at": latest.get("sent_at") or datetime.now(timezone.utc),
                },
            )
            .returning(MailThread.id)
        )
        thread_id = db.execute(thread_stmt).scalar_one()
        threads_upserted += 1

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

        # Commit per thread so a late failure (e.g. token death mid-pull) keeps
        # every thread fetched so far instead of rolling the whole run back.
        if commit_every and (index + 1) % commit_every == 0:
            db.commit()

    db.commit()
    return {
        "threads_upserted": threads_upserted,
        "messages_upserted": messages_upserted,
        "classified": classified,
        "fetched": len(thread_ids),
        "skipped_existing": len(existing_thread_ids),
    }
