"""AI-drafted reply text generation (plan §3.7 -- `POST
/mail/thread/{id}/reply-draft`, mounted by a later wave).

Mirrors `extractor.py`'s shape (`_MAX_TOKENS`, truncated context, the
never-raises three-way result contract) but drafts reply TEXT instead of
extracting a structured obligation, and is stateless -- nothing here writes
to the DB except (via the caller) usage under the `reply_draft` stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.services.nlp.llm_client import INLINE_RETRIES, LlmCallError, LlmUsage, RetryPolicy, call_chat_completion
from app.services.nlp.providers import (
    LlmCredential,
    ResolvedExtraction,
    resolve_extraction_credential,
)

# The reply-draft call's own token budget -- same ceiling as extraction
# (a different caller of call_chat_completion sets its own).
_MAX_TOKENS = 512

# Thread context is truncated before sending, same 6,000-char discipline as
# extraction's message-body cap (extractor.py's _MESSAGE_TEXT_MAX_LEN).
_THREAD_CONTEXT_MAX_LEN = 6000

# The stage name migration 0023 adds to llm_usage_daily's CHECK constraint.
USAGE_STAGE = "reply_draft"


@dataclass(frozen=True)
class ReplyDraftAttempt:
    """Everything one reply-draft call produced, including whether it
    reached the provider at all -- same split as extraction's
    `ExtractionAttempt`: `provider_call_succeeded` means the wire call came
    back (and, for BYOK, was charged) regardless of what came back
    afterward; `draft_text` is `None` for either a call failure or an
    unparseable response.
    """

    draft_text: str | None
    provider_call_succeeded: bool
    usage: LlmUsage | None


def resolve_reply_draft_credential(db: Session, user_id: UUID) -> ResolvedExtraction:
    """Credential resolution for the reply-draft stage REUSES
    `resolve_extraction_credential`'s BYOK semantics wholesale -- same
    BYOK-only 409s, blocked-custom-endpoint dead-stop, server-fallback flag
    -- rather than forking it. There is exactly one stored credential row
    per user; extraction and reply-draft both resolve it the same way, they
    just record usage under different stages.
    """
    return resolve_extraction_credential(db, user_id)


def _build_thread_context(subject: str | None, messages: list[Any]) -> str:
    """Assemble the thread's context text, NEWEST-FIRST (the order the
    caller is expected to hand `messages` in already -- this function
    doesn't re-sort, since the caller already has the thread's natural
    order from its own query). Truncated to `_THREAD_CONTEXT_MAX_LEN`
    total, same discipline as extraction's 6,000-char body cap.
    """
    lines = [f"Subject: {subject or '(no subject)'}", ""]
    for message in messages:
        sender = getattr(message, "sender", None) or "(unknown)"
        sent_at = getattr(message, "sent_at", None)
        sent_at_str = sent_at.isoformat() if sent_at else "(unknown time)"
        body = getattr(message, "body_text", None) or getattr(message, "snippet", None) or ""
        lines.append(f"From: {sender} ({sent_at_str})")
        lines.append(body)
        lines.append("")
    context = "\n".join(lines).strip()
    return context[:_THREAD_CONTEXT_MAX_LEN]


def _build_prompt() -> str:
    return (
        "You draft a reply to an email thread on behalf of the RECIPIENT. "
        "You are given the thread's messages, newest first.\n\n"
        "Rules:\n"
        "- Write a complete, ready-to-send reply body in plain text -- no "
        "subject line, no quoted original text, and no signature block "
        "unless the thread clearly expects one.\n"
        "- Keep it concise and match the thread's tone.\n"
        "- NEVER invent facts, commitments, dates, or amounts that aren't "
        "already in the thread -- when unsure, leave it a placeholder the "
        "sender can fill in rather than guessing.\n\n"
        "Return JSON only with one key: draft_text."
    )


def _parse_draft(content: str) -> str:
    """Strictly validate the model's JSON reply. Raises `ValueError` or
    `json.JSONDecodeError` on anything invalid; the caller treats both as
    an unusable response."""
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Response is not a JSON object")
    draft_text = payload.get("draft_text")
    if not isinstance(draft_text, str) or not draft_text.strip():
        raise ValueError("Invalid draft_text")
    return draft_text.strip()


def generate_reply_draft(
    *,
    credential: LlmCredential,
    subject: str | None,
    messages: list[Any],
    policy: RetryPolicy = INLINE_RETRIES,
) -> ReplyDraftAttempt:
    """Run the reply-draft call for one thread against the caller's
    resolved `credential`. Never raises -- mirrors extraction's
    `_extract_attempt` contract exactly: `provider_call_succeeded` flips
    `True` the moment the wire call returns (a BYOK credential is charged
    for the call whether or not its body turns out to be usable), and
    `draft_text` is `None` for any failure -- call failure or an
    unparseable response -- each logged as a warning.

    `policy` (plan: phase 3 of the LLM-failure work) defaults to
    `INLINE_RETRIES`, not `NO_RETRIES` -- this function has exactly ONE
    production call site (`POST /mail/thread/{id}/reply-draft`), and a human
    is always watching that request, so there's no ambiguous context to
    thread a policy through from the way `classify_with_usage`/`run_backfill`
    need to be.
    """
    context = _build_thread_context(subject, messages)
    prompt = _build_prompt()

    try:
        call_result = call_chat_completion(
            credential, prompt=prompt, user_content=context, max_tokens=_MAX_TOKENS, policy=policy
        )
    except LlmCallError as exc:
        logger.warning(
            "Reply draft generation failed for provider %s: %s",
            credential.provider,
            exc.category,
        )
        return ReplyDraftAttempt(draft_text=None, provider_call_succeeded=False, usage=None)

    try:
        draft_text = _parse_draft(call_result.content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Reply draft generation returned an unusable response for provider %s: %s",
            credential.provider,
            type(exc).__name__,
        )
        return ReplyDraftAttempt(
            draft_text=None, provider_call_succeeded=True, usage=call_result.usage
        )

    return ReplyDraftAttempt(
        draft_text=draft_text, provider_call_succeeded=True, usage=call_result.usage
    )
