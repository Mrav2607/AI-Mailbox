"""Unit tests for the action extractor: three-way result semantics
(ExtractedAction | NoAction | None), the prompt/message framing `_call_llm`
adds around the shared wire call, strict response parsing, and
test_credential's category mapping.

The wire call itself (`llm_client.call_chat_completion` -- trust_env,
redirects/non-2xx, per-request destination pinning, response-shape checks,
key-never-logged) is tested in `test_llm_client.py`; here we monkeypatch
`extractor.call_chat_completion` directly, since extractor no longer touches
httpx or the destination policy at all -- it only builds a prompt/content
pair and maps the wire call's result (or `LlmCallError`) onto its own
three-way / test-credential contracts."""

import json
import logging
from datetime import datetime, timezone

import pytest

from app.services.nlp import extractor
from app.services.nlp.extractor import (
    ExtractedAction,
    ExtractionCallError,
    NoAction,
    extract_action,
)
from app.services.nlp.extractor import test_credential as _test_credential
from app.services.nlp.llm_client import LlmCallResult

# Imported under a private alias -- pytest collects any top-level `test_*`
# callable in a test module, including ones merely imported by that name.
from app.services.nlp.providers import LlmCredential

VALID_PAYLOAD = {
    "has_action": True,
    "kind": "payment",
    "title": "Pay invoice #429",
    "due_at": "2024-03-15T17:00:00Z",
    "due_is_date_only": False,
    "due_raw": "by Friday",
    "amount": 129.99,
    "currency": "USD",
    "confidence": 0.85,
}

SECRET_KEY = "sk-super-secret-do-not-log-1234"


def _make_credential(**overrides):
    kwargs = dict(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key=SECRET_KEY,
        model="gpt-4o-mini",
    )
    kwargs.update(overrides)
    return LlmCredential(**kwargs)


def _default_kwargs(**overrides):
    kwargs = dict(
        subject="Invoice due",
        sender="billing@example.com",
        snippet="Please pay by Friday",
        body_text="Your invoice #429 is due.",
        received_at=datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc),
        credential=_make_credential(),
    )
    kwargs.update(overrides)
    return kwargs


def _stub_returns(content, *, calls=None):
    """A fake `call_chat_completion` that returns `content` wrapped in the
    real wire type (usage untested here -- that's Wave 2a) and records the
    kwargs it was called with, so tests can assert what extractor built."""
    def fn(credential, *, prompt, user_content, max_tokens):
        if calls is not None:
            calls.append(dict(credential=credential, prompt=prompt, user_content=user_content, max_tokens=max_tokens))
        return LlmCallResult(content=content, usage=None)
    return fn


def _stub_raises(category, status=None):
    def fn(credential, *, prompt, user_content, max_tokens):
        raise ExtractionCallError(category, status)
    return fn


# ---------------------------------------------------------------------------
# _call_llm -- the prompt/content framing extractor adds around the shared
# wire call (`llm_client.call_chat_completion`), plus ExtractionCallError's
# propagation as the LlmCallError alias.
# ---------------------------------------------------------------------------


def test_call_llm_builds_email_prefixed_and_truncated_user_content(monkeypatch):
    calls = []
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD), calls=calls))
    long_text = "x" * 7000

    content = extractor._call_llm(_make_credential(), "the prompt", long_text)

    assert content == json.dumps(VALID_PAYLOAD)
    assert len(calls) == 1
    assert calls[0]["prompt"] == "the prompt"
    assert calls[0]["user_content"] == f"Email:\n{long_text[:6000]}"
    assert calls[0]["max_tokens"] == 512


def test_call_llm_propagates_llm_call_error_as_extraction_call_error(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("http_500", 500))
    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(_make_credential(), "prompt", "text")
    assert exc_info.value.category == "http_500"
    assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# extract_action -- public three-way contract
# ---------------------------------------------------------------------------


def test_extract_action_success_round_trip(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD)))
    result = extract_action(**_default_kwargs())
    assert isinstance(result, ExtractedAction)
    assert result.kind == "payment"
    assert result.title == "Pay invoice #429"
    assert result.due_raw == "by Friday"
    assert result.amount == 129.99
    assert result.currency == "USD"
    assert result.confidence == 0.85
    assert result.due_precision == "datetime"
    assert result.due_at == datetime(2024, 3, 15, 17, 0, 0, tzinfo=timezone.utc)


def test_extract_action_has_action_false_returns_no_action_not_none(monkeypatch):
    payload = {"has_action": False, "confidence": 0.9}
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(payload)))
    result = extract_action(**_default_kwargs())
    assert isinstance(result, NoAction)
    assert result is not None


def test_extract_action_returns_none_on_non_2xx(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("http_503", 503))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_returns_none_on_network_error(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("connection_failed", None))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_returns_none_on_prose_reply(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns("Sure, I'll get right on it!"))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_returns_none_on_invalid_response(monkeypatch):
    """The exhaustive malformed-response-shape enumeration is a wire-level
    concern owned by `llm_client.call_chat_completion` now (covered in
    test_llm_client.py); this proves extract_action's mapping of that
    category to `None` still holds."""
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("invalid_response", 200))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_date_only_resolves_to_end_of_day_utc(monkeypatch):
    payload = {**VALID_PAYLOAD, "due_at": "2024-03-15", "due_is_date_only": True}
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(payload)))
    result = extract_action(**_default_kwargs())
    assert isinstance(result, ExtractedAction)
    assert result.due_precision == "date"
    assert result.due_at == datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)


def test_extract_action_prompt_includes_received_at(monkeypatch):
    calls = []
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD), calls=calls))
    received_at = datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    extract_action(**_default_kwargs(received_at=received_at))
    assert received_at.isoformat() in calls[0]["prompt"]


def test_extract_action_model_version_attribution(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD)))
    credential = _make_credential(provider="openai", model="gpt-4o-mini")
    result = extract_action(**_default_kwargs(credential=credential))
    assert isinstance(result, ExtractedAction)
    assert result.model_version == "openai:gpt-4o-mini"

    credential = _make_credential(provider="groq", model="llama-3.3-70b")
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD)))
    result = extract_action(**_default_kwargs(credential=credential))
    assert result.model_version == "groq:llama-3.3-70b"


@pytest.mark.parametrize(
    "make_stub,expect_result_is_none",
    [
        (lambda: _stub_returns(json.dumps(VALID_PAYLOAD)), False),
        (lambda: _stub_raises("http_500", 500), True),
        (lambda: _stub_raises("connection_failed", None), True),
        (lambda: _stub_returns("not json at all"), True),
        (lambda: _stub_raises("invalid_response", 200), True),
    ],
    ids=["success", "http-error", "network-error", "prose-reply", "malformed-shape"],
)
def test_extract_action_never_logs_the_api_key(monkeypatch, caplog, make_stub, expect_result_is_none):
    monkeypatch.setattr(extractor, "call_chat_completion", make_stub())
    credential = _make_credential(api_key=SECRET_KEY)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        result = extract_action(**_default_kwargs(credential=credential))
    if expect_result_is_none:
        assert result is None
    assert SECRET_KEY not in caplog.text


# ---------------------------------------------------------------------------
# test_credential -- the /test route's entry point
# ---------------------------------------------------------------------------


def test_test_credential_success(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD)))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is True
    assert category is None
    assert latency_ms >= 0


def test_test_credential_maps_call_error_category(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("http_500", 500))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is False
    assert category == "http_500"


def test_test_credential_maps_parse_failure_to_invalid_response(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_returns("not json"))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is False
    assert category == "invalid_response"


def test_test_credential_never_logs_the_api_key(monkeypatch, caplog):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("http_500", 500))
    credential = _make_credential(api_key=SECRET_KEY)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        _test_credential(credential)
    assert SECRET_KEY not in caplog.text
