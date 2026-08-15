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
from app.services.nlp import llm_client
from app.services.nlp.extractor import (
    ExtractedAction,
    ExtractionAttempt,
    ExtractionCallError,
    NoAction,
    extract_action,
    extract_action_with_usage,
)
from app.services.nlp.extractor import test_credential as _test_credential
from app.services.nlp.llm_client import LlmCallResult, LlmUsage

# Imported under a private alias -- pytest collects any top-level `test_*`
# callable in a test module, including ones merely imported by that name.
from app.services.nlp.providers import DestinationRejected, LlmCredential

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


def _stub_returns(content, *, calls=None, usage=None):
    """A fake `call_chat_completion` that returns `content` wrapped in the
    real wire type, optionally carrying `usage`, and records the kwargs it
    was called with so tests can assert what extractor built."""
    def fn(credential, *, prompt, user_content, max_tokens):
        if calls is not None:
            calls.append(dict(credential=credential, prompt=prompt, user_content=user_content, max_tokens=max_tokens))
        return LlmCallResult(content=content, usage=usage)
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

    call_result = extractor._call_llm(_make_credential(), "the prompt", long_text)

    assert isinstance(call_result, LlmCallResult)
    assert call_result.content == json.dumps(VALID_PAYLOAD)
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
    with caplog.at_level(logging.WARNING, logger="cortexmail"):
        result = extract_action(**_default_kwargs(credential=credential))
    if expect_result_is_none:
        assert result is None
    assert SECRET_KEY not in caplog.text


# ---------------------------------------------------------------------------
# extract_action_with_usage -- usage-bearing wrapper around _extract_attempt.
# provider_call_succeeded means "the wire call came back", not "we got a
# usable answer" -- the parse-failure case below is the one most likely to
# get broken by a future refactor, since it's tempting to conflate the two.
# ---------------------------------------------------------------------------


def test_extract_action_with_usage_success_carries_usage_and_marks_call_succeeded(monkeypatch):
    usage = LlmUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    monkeypatch.setattr(
        extractor, "call_chat_completion", _stub_returns(json.dumps(VALID_PAYLOAD), usage=usage)
    )
    attempt = extract_action_with_usage(**_default_kwargs())
    assert isinstance(attempt, ExtractionAttempt)
    assert isinstance(attempt.result, ExtractedAction)
    assert attempt.provider_call_succeeded is True
    assert attempt.usage == usage
    # A successful extraction still issued a real request -- llm_attempted is
    # always True for extraction (unlike classification, there's no encoder/
    # heuristic path that skips the call entirely) -- but nothing failed.
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is False
    assert attempt.failure_category is None


def test_extract_action_with_usage_parse_failure_after_wire_success_still_counts_the_call(monkeypatch):
    """A response that comes back over the wire fine but fails to parse still
    means the provider was reached (and, for BYOK, billed) -- it must count
    as a real call even though there's no usable result. It's also a failure
    for reporting purposes, categorized invalid_response even though it never
    raised an ExtractionCallError (plan: 2026-08-14-llm-failure-visibility)."""
    usage = LlmUsage(prompt_tokens=50, completion_tokens=None, total_tokens=None)
    monkeypatch.setattr(
        extractor, "call_chat_completion", _stub_returns("not json at all", usage=usage)
    )
    attempt = extract_action_with_usage(**_default_kwargs())
    assert attempt.result is None
    assert attempt.provider_call_succeeded is True
    assert attempt.usage == usage
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is False
    assert attempt.failure_category == "invalid_response"


def test_extract_action_with_usage_wire_failure_does_not_count_the_call(monkeypatch):
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises("connection_failed", None))
    attempt = extract_action_with_usage(**_default_kwargs())
    assert attempt.result is None
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None
    # The wire call itself was still attempted and failed -- llm_attempted is
    # True even though provider_call_succeeded (a different fact: did the
    # request come back at all) is False.
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is False
    assert attempt.failure_category == "connection_failed"


@pytest.mark.parametrize(
    "category,status,expected_llm_attempted",
    [
        ("http_429", 429, True),
        ("timed_out", None, True),
        # blocked_by_policy is a destination-policy PREFLIGHT rejection
        # (llm_client.py's pin_custom_destination) -- it raises before the
        # request ever leaves the process, so llm_attempted must be False
        # even though this is still a real, categorized failure. Codex
        # review caught this test blessing the wrong boolean (hard-coded
        # True on every LlmCallError path) before the fix landed.
        ("blocked_by_policy", None, False),
    ],
)
def test_extract_action_with_usage_carries_llm_call_error_category_through(
    monkeypatch, category, status, expected_llm_attempted
):
    """Every ExtractionCallError category the wire call can raise (llm_client.py)
    must survive onto ExtractionAttempt.failure_category, not just
    connection_failed -- this is the count the extraction sweep's http_429
    reporting depends on. llm_attempted tracks whether a request was actually
    issued, which is category-dependent (see llm_client.request_was_issued)."""
    monkeypatch.setattr(extractor, "call_chat_completion", _stub_raises(category, status))
    attempt = extract_action_with_usage(**_default_kwargs())
    assert attempt.result is None
    assert attempt.failure_category == category
    assert attempt.llm_attempted is expected_llm_attempted


def test_extract_action_with_usage_real_preflight_rejection_never_attempted(monkeypatch):
    """A REAL destination-policy rejection, not a stubbed category -- drives
    the actual `call_chat_completion` (not `extractor.call_chat_completion`
    mocked away) through a fake `pin_custom_destination` that raises
    `DestinationRejected`, same pattern as test_llm_client.py's own coverage
    of this path. Proves llm_attempted=False end to end, not just that the
    category string happens to match."""
    def fake_pin(url):
        raise DestinationRejected("destination_rejected", "nope")

    monkeypatch.setattr(llm_client, "pin_custom_destination", fake_pin)
    credential = _make_credential(provider="custom", base_url="https://ollama.example.com/v1")

    attempt = extract_action_with_usage(**_default_kwargs(credential=credential))

    assert attempt.result is None
    assert attempt.provider_call_succeeded is False
    assert attempt.llm_attempted is False
    assert attempt.fallback_used is False
    assert attempt.failure_category == "blocked_by_policy"


def test_extract_action_with_usage_no_action_result_still_carries_call_succeeded(monkeypatch):
    payload = {"has_action": False, "confidence": 0.9}
    usage = LlmUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(
        extractor, "call_chat_completion", _stub_returns(json.dumps(payload), usage=usage)
    )
    attempt = extract_action_with_usage(**_default_kwargs())
    assert isinstance(attempt.result, NoAction)
    assert attempt.provider_call_succeeded is True
    assert attempt.usage == usage
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is False
    assert attempt.failure_category is None


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
    with caplog.at_level(logging.WARNING, logger="cortexmail"):
        _test_credential(credential)
    assert SECRET_KEY not in caplog.text
