"""Gmail's provider-call step of a reply send.

Recipient computation, MIME assembly, and the attempt CAS ladder all live in
`common.py` -- this module is deliberately thin: it only knows how to hand
an already-assembled MIME message to `GmailClient.send_message`. Everything
account/thread/DB-facing (loading the thread, resolving scope, orchestrating
the attempt lifecycle around this call) belongs to the route that calls it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.ingest.gmail_client import GmailClient


def send_reply(client: GmailClient, *, raw: str, provider_thread_id: str) -> dict[str, Any]:
    """Thin wrapper over `GmailClient.send_message` -- kept as its own
    function so the send call site reads the same shape as
    `outlook_send.py`'s, and so a later wave has one obvious place to add
    telemetry/logging around the actual provider contact."""
    return client.send_message(raw=raw, thread_id=provider_thread_id)


def sent_at_now() -> datetime:
    """Gmail's send response carries no reliable client-usable send
    timestamp (`internalDate` is a `messages.get` field, not part of
    `messages.send`'s response) -- our own clock at the moment the call
    returns is the completion transaction's `sent_at`, consistent with how
    the rest of the attempt lifecycle is stamped on our clock."""
    return datetime.now(timezone.utc)
