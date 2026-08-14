"""Outlook's provider-call step of a reply send: createReply -> cap check ->
send, with the draft deleted on a cap violation.

Graph computes recipients from the target message's own Reply-To/To/Cc
(honoring RFC 2822 the way `common.py`'s Gmail-side recipient computation
does by hand) -- so the §3.3 cap can only be enforced AFTER createReply
returns, by inspecting the draft it built. Recipient computation, the
attempt CAS ladder, and the fence/resolve helper all live in `common.py`;
this module only knows Outlook's own two-call choreography and the
cap/draft-cleanup step in between.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.logging import logger
from app.services.ingest.outlook_client import OutlookClient
from app.services.mail_send.common import MAX_RECIPIENTS, SendError

# A 403 from createReply/send means the stored-scope check up front passed
# but the permission was revoked in between (e.g. an admin pulled consent)
# -- surfaced the same way the up-front scope gate is (F6), not as a
# generic send_rejected.
_MISSING_SCOPE_MESSAGE = "This account needs to be reconnected to grant reply-send permission."


def _raise_missing_scope_if_forbidden(exc: httpx.HTTPStatusError) -> None:
    if exc.response is not None and exc.response.status_code == 403:
        raise SendError("missing_scope", _MISSING_SCOPE_MESSAGE) from exc


def create_reply_draft(
    client: OutlookClient, *, message_id: str, reply_all: bool, comment: str
) -> dict[str, Any]:
    """POST createReply/createReplyAll and enforce the §3.3 recipient cap
    against Graph's own computed recipients. Deletes the draft and raises
    `SendError("too_many_recipients")` on violation -- draft inspection is
    the only way to see the count Graph actually chose to CC/BCC.
    """
    try:
        draft = client.create_reply(message_id, reply_all=reply_all, comment=comment)
    except httpx.HTTPStatusError as exc:
        _raise_missing_scope_if_forbidden(exc)
        raise
    draft_id = draft.get("id")
    if not draft_id:
        raise SendError("send_rejected", "Graph did not return a draft id")

    to_count = len(draft.get("toRecipients") or [])
    cc_count = len(draft.get("ccRecipients") or [])
    total = to_count + cc_count
    if total > MAX_RECIPIENTS:
        try:
            client.delete_draft(draft_id)
        except httpx.HTTPError:
            # Widened from HTTPStatusError (F5): a Timeout/ConnectError here
            # means cleanup failed, not that the cap violation didn't
            # happen -- it must not escape and get misclassified upstream
            # as send_outcome_unknown (nothing was ever sent). The intended
            # too_many_recipients error below still raises either way.
            logger.warning(
                "failed to delete over-cap Outlook reply draft %s", draft_id
            )
        raise SendError(
            "too_many_recipients", f"{total} addresses exceeds the {MAX_RECIPIENTS} cap"
        )

    return draft


def send_reply_draft(client: OutlookClient, *, draft_id: str) -> None:
    """POST /send for an already-created, already-cap-checked draft."""
    try:
        client.send_draft(draft_id)
    except httpx.HTTPStatusError as exc:
        _raise_missing_scope_if_forbidden(exc)
        raise


def build_ephemeral_message(
    draft: dict[str, Any],
    *,
    attempt_id: Any,
    self_address: str,
    to: list[str],
    cc: list[str],
    sent_at: Any,
) -> dict[str, Any]:
    """The normalized, CLIENT-DISPLAY-ONLY representation of a sent Outlook
    reply (plan §3.4/P7-3): nothing is written to `mail_message` for
    Outlook, so this is built straight from the createReply response and
    handed back for the route's `ReplySent.message` -- with the ATTEMPT id
    as its synthetic `id` (never a `MailMessage.id`; no server mutation
    endpoint ever accepts it). The real row arrives later via correlated
    sentitems ingest, classified from the authoritative copy.
    """
    body = draft.get("body") or {}
    content_type = (body.get("contentType") or "").lower()
    content = body.get("content")
    return {
        "id": str(attempt_id),
        "sender": self_address,
        "recipient": to or None,
        "cc": cc or None,
        "sent_at": sent_at,
        "snippet": draft.get("bodyPreview"),
        "body_text": content if content_type == "text" else None,
        "body_html": content if content_type == "html" else None,
    }
