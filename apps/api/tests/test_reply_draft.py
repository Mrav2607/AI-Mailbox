"""Tests for app/services/nlp/reply_draft.py: credential resolution reuse,
context assembly/truncation, and the never-raises three-way call contract.
Offline only -- call_chat_completion is monkeypatched, mirroring
test_extractor.py's style for extractor.py (the module this one mirrors).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.nlp import reply_draft
from app.services.nlp.llm_client import LlmCallError, LlmCallResult, LlmUsage
from app.services.nlp.providers import LlmCredential

_CRED = LlmCredential(provider="gemini", base_url="https://example.test", api_key="key", model="gemini-x")


def _message(*, sender="bob@example.com", sent_at=None, body_text="hi", snippet=None):
    return SimpleNamespace(
        sender=sender,
        sent_at=sent_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        body_text=body_text,
        snippet=snippet,
    )


def test_max_tokens_is_512():
    assert reply_draft._MAX_TOKENS == 512


def test_thread_context_cap_is_6000_chars():
    assert reply_draft._THREAD_CONTEXT_MAX_LEN == 6000


def test_resolve_reply_draft_credential_reuses_extraction_resolver(monkeypatch):
    """Contract requirement: reply-draft credential resolution reuses
    resolve_extraction_credential's semantics via a shared helper, never a
    forked copy of the resolution logic."""
    sentinel = object()
    called_with = {}

    def fake_resolve(db, user_id):
        called_with["db"] = db
        called_with["user_id"] = user_id
        return sentinel

    monkeypatch.setattr(reply_draft, "resolve_extraction_credential", fake_resolve)

    db = MagicMock()
    result = reply_draft.resolve_reply_draft_credential(db, "user-1")

    assert result is sentinel
    assert called_with == {"db": db, "user_id": "user-1"}


def test_build_thread_context_truncates_to_max_len():
    long_body = "x" * 10_000
    messages = [_message(body_text=long_body)]
    context = reply_draft._build_thread_context("Subject", messages)
    assert len(context) <= reply_draft._THREAD_CONTEXT_MAX_LEN


def test_build_thread_context_includes_subject_and_sender():
    messages = [_message(sender="carol@example.com", body_text="please reply")]
    context = reply_draft._build_thread_context("Budget review", messages)
    assert "Budget review" in context
    assert "carol@example.com" in context
    assert "please reply" in context


def test_build_thread_context_falls_back_to_snippet_when_no_body():
    messages = [_message(body_text=None, snippet="short preview")]
    context = reply_draft._build_thread_context(None, messages)
    assert "short preview" in context


def test_generate_reply_draft_success(monkeypatch):
    call_result = LlmCallResult(
        content=json.dumps({"draft_text": "Sounds great, see you then."}),
        usage=LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    monkeypatch.setattr(
        reply_draft, "call_chat_completion", MagicMock(return_value=call_result)
    )

    attempt = reply_draft.generate_reply_draft(
        credential=_CRED, subject="Lunch", messages=[_message()]
    )

    assert attempt.draft_text == "Sounds great, see you then."
    assert attempt.provider_call_succeeded is True
    assert attempt.usage.total_tokens == 15


def test_generate_reply_draft_passes_max_tokens_and_credential(monkeypatch):
    call_result = LlmCallResult(content=json.dumps({"draft_text": "ok"}), usage=None)
    mock_call = MagicMock(return_value=call_result)
    monkeypatch.setattr(reply_draft, "call_chat_completion", mock_call)

    reply_draft.generate_reply_draft(credential=_CRED, subject="Lunch", messages=[_message()])

    call = mock_call.call_args
    assert call.args[0] is _CRED
    assert call.kwargs["max_tokens"] == 512


def test_generate_reply_draft_call_failure_never_raises(monkeypatch):
    monkeypatch.setattr(
        reply_draft,
        "call_chat_completion",
        MagicMock(side_effect=LlmCallError("timed_out", None)),
    )

    attempt = reply_draft.generate_reply_draft(credential=_CRED, subject="Lunch", messages=[_message()])

    assert attempt.draft_text is None
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None


def test_generate_reply_draft_unparseable_response_still_counts_as_call_succeeded(monkeypatch):
    """A response that comes back but fails to parse is still a billed call
    -- provider_call_succeeded stays True (mirrors extractor.py's contract)."""
    call_result = LlmCallResult(content="not json", usage=LlmUsage(1, 1, 2))
    monkeypatch.setattr(reply_draft, "call_chat_completion", MagicMock(return_value=call_result))

    attempt = reply_draft.generate_reply_draft(credential=_CRED, subject="Lunch", messages=[_message()])

    assert attempt.draft_text is None
    assert attempt.provider_call_succeeded is True
    assert attempt.usage.total_tokens == 2


def test_generate_reply_draft_empty_draft_text_is_rejected(monkeypatch):
    call_result = LlmCallResult(content=json.dumps({"draft_text": "   "}), usage=None)
    monkeypatch.setattr(reply_draft, "call_chat_completion", MagicMock(return_value=call_result))

    attempt = reply_draft.generate_reply_draft(credential=_CRED, subject="Lunch", messages=[_message()])

    assert attempt.draft_text is None
    assert attempt.provider_call_succeeded is True


def test_usage_stage_constant():
    assert reply_draft.USAGE_STAGE == "reply_draft"
