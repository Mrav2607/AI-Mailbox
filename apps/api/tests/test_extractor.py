"""Unit tests for the action extractor: three-way result semantics
(ExtractedAction | NoAction | None), the OpenAI-compatible wire call
(_call_llm / ExtractionCallError), strict response parsing, and
test_credential's category mapping -- all offline via httpx.MockTransport,
no real LLM or network required.

Every httpx.Client the extractor builds is routed through MockTransport by
subclassing httpx.Client for the duration of a test (_install_mock_transport)
-- this is the only way to inject a fake transport without changing
_call_llm's signature, and it exercises the REAL trust_env=False behavior
(httpx itself decides whether to honor a proxy env var; a subclass can't
fake that part)."""

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app.services.nlp import extractor
from app.services.nlp.extractor import (
    ExtractedAction,
    ExtractionCallError,
    NoAction,
    extract_action,
)
from app.services.nlp.extractor import test_credential as _test_credential

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


def _choice_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _install_mock_transport(monkeypatch, handler):
    """Route every httpx.Client the extractor constructs through
    MockTransport(handler) -- no real network, no new dependency."""
    real_client_cls = httpx.Client

    class _MockedClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(extractor.httpx, "Client", _MockedClient)


def _json_handler(status_code, body, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status_code, json=body)

    return handler


def _raising_handler(exc):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# ---------------------------------------------------------------------------
# _call_llm / ExtractionCallError categories
# ---------------------------------------------------------------------------


def test_call_llm_success_round_trip_asserts_wire_shape(monkeypatch):
    calls = []
    _install_mock_transport(
        monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD)), calls)
    )
    credential = _make_credential()

    content = extractor._call_llm(credential, "the prompt", "the email text")

    assert content == json.dumps(VALID_PAYLOAD)
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 512
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert "the prompt" in body["messages"][0]["content"]
    assert "the email text" in body["messages"][0]["content"]


def test_call_llm_raises_connection_failed_on_network_error(monkeypatch):
    _install_mock_transport(monkeypatch, _raising_handler(httpx.ConnectError("boom")))
    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(_make_credential(), "prompt", "text")
    assert exc_info.value.category == "connection_failed"
    assert exc_info.value.status is None


def test_call_llm_raises_http_status_category_on_non_2xx(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(_make_credential(), "prompt", "text")
    assert exc_info.value.category == "http_500"
    assert exc_info.value.status == 500


def test_call_llm_raises_http_status_category_on_3xx(monkeypatch):
    """Redirects are never followed, so `raise_for_status()` alone wouldn't
    catch this -- a 3xx must be categorized as `http_<status>`, not fall
    through to `response.json()` and surface as `invalid_response`."""
    _install_mock_transport(monkeypatch, _json_handler(302, {"error": "moved"}))
    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(_make_credential(), "prompt", "text")
    assert exc_info.value.category == "http_302"
    assert exc_info.value.status == 302


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": "not a dict"}]},
        {},
    ],
    ids=["empty-choices", "missing-message", "null-content", "message-not-dict", "missing-choices"],
)
def test_call_llm_raises_invalid_response_for_malformed_shapes(monkeypatch, body):
    _install_mock_transport(monkeypatch, _json_handler(200, body))
    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(_make_credential(), "prompt", "text")
    assert exc_info.value.category == "invalid_response"


def test_call_llm_blocked_by_policy_for_custom_provider(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, {}, calls))

    def fake_assert(url):
        raise DestinationRejected("destination_rejected", "nope")

    monkeypatch.setattr(extractor, "assert_url_still_allowed", fake_assert)
    credential = _make_credential(provider="custom", base_url="https://ollama.example.com/v1")

    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(credential, "prompt", "text")
    assert exc_info.value.category == "blocked_by_policy"
    assert not calls  # rejected before any request left


def test_call_llm_rechecks_destination_before_every_request(monkeypatch):
    """A sweep resolves a credential once but can run for minutes -- a DNS
    answer flipping non-global between two calls must block the SECOND one
    before any request reaches the wire, not just the first."""
    calls = []
    _install_mock_transport(
        monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD)), calls)
    )

    check_count = {"n": 0}

    def fake_assert(url):
        check_count["n"] += 1
        if check_count["n"] > 1:
            raise DestinationRejected("destination_rejected", "flipped non-global")

    monkeypatch.setattr(extractor, "assert_url_still_allowed", fake_assert)
    credential = _make_credential(provider="custom", base_url="https://ollama.example.com/v1")

    content = extractor._call_llm(credential, "prompt", "text")
    assert content
    assert len(calls) == 1

    with pytest.raises(ExtractionCallError) as exc_info:
        extractor._call_llm(credential, "prompt", "text")
    assert exc_info.value.category == "blocked_by_policy"
    assert len(calls) == 1  # second call never reached the wire


def test_call_llm_trust_env_false_ignores_private_proxy_env_var(monkeypatch):
    """trust_env=False is mandatory: httpx otherwise honors HTTP(S)_PROXY
    and would route the bearer credential through a proxy address the
    destination policy never validated."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    real_client_cls = httpx.Client
    captured = []

    class _SpyClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD)))
            )
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(extractor.httpx, "Client", _SpyClient)

    content = extractor._call_llm(_make_credential(), "prompt", "text")

    assert content == json.dumps(VALID_PAYLOAD)
    assert len(captured) == 1
    client = captured[0]
    assert client.trust_env is False
    # With trust_env=False httpx never consults HTTPS_PROXY -- if it had,
    # this client would carry a mounted proxy transport for the https:// pattern.
    assert not any(client._mounts.values())


# ---------------------------------------------------------------------------
# extract_action -- public three-way contract
# ---------------------------------------------------------------------------


def test_extract_action_success_round_trip(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD))))
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
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(payload))))
    result = extract_action(**_default_kwargs())
    assert isinstance(result, NoAction)
    assert result is not None


def test_extract_action_returns_none_on_non_2xx(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(503, {"error": "unavailable"}))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_returns_none_on_network_error(monkeypatch):
    _install_mock_transport(monkeypatch, _raising_handler(httpx.ConnectTimeout("timed out")))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_returns_none_on_prose_reply(monkeypatch):
    _install_mock_transport(
        monkeypatch, _json_handler(200, _choice_payload("Sure, I'll get right on it!"))
    )
    assert extract_action(**_default_kwargs()) is None


@pytest.mark.parametrize(
    "body",
    [{"choices": []}, {"choices": [{}]}, {"choices": [{"message": {"content": None}}]}],
    ids=["empty-choices", "missing-message", "null-content"],
)
def test_extract_action_returns_none_on_malformed_shape(monkeypatch, body):
    _install_mock_transport(monkeypatch, _json_handler(200, body))
    assert extract_action(**_default_kwargs()) is None


def test_extract_action_date_only_resolves_to_end_of_day_utc(monkeypatch):
    payload = {**VALID_PAYLOAD, "due_at": "2024-03-15", "due_is_date_only": True}
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(payload))))
    result = extract_action(**_default_kwargs())
    assert isinstance(result, ExtractedAction)
    assert result.due_precision == "date"
    assert result.due_at == datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)


def test_extract_action_prompt_includes_received_at(monkeypatch):
    calls = []
    _install_mock_transport(
        monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD)), calls)
    )
    received_at = datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    extract_action(**_default_kwargs(received_at=received_at))
    body = json.loads(calls[0].content)
    assert received_at.isoformat() in body["messages"][0]["content"]


def test_extract_action_model_version_attribution(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD))))
    credential = _make_credential(provider="openai", model="gpt-4o-mini")
    result = extract_action(**_default_kwargs(credential=credential))
    assert isinstance(result, ExtractedAction)
    assert result.model_version == "openai:gpt-4o-mini"

    credential = _make_credential(provider="groq", model="llama-3.3-70b")
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD))))
    result = extract_action(**_default_kwargs(credential=credential))
    assert result.model_version == "groq:llama-3.3-70b"


@pytest.mark.parametrize(
    "make_handler,expect_result_is_none",
    [
        (lambda: _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD))), False),
        (lambda: _json_handler(500, {"error": "boom"}), True),
        (lambda: _raising_handler(httpx.ConnectError("boom")), True),
        (lambda: _json_handler(200, _choice_payload("not json at all")), True),
        (lambda: _json_handler(200, {"choices": []}), True),
    ],
    ids=["success", "http-error", "network-error", "prose-reply", "malformed-shape"],
)
def test_extract_action_never_logs_the_api_key(monkeypatch, caplog, make_handler, expect_result_is_none):
    _install_mock_transport(monkeypatch, make_handler())
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
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(json.dumps(VALID_PAYLOAD))))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is True
    assert category is None
    assert latency_ms >= 0


def test_test_credential_maps_call_error_category(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is False
    assert category == "http_500"


def test_test_credential_maps_parse_failure_to_invalid_response(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload("not json")))
    ok, category, latency_ms = _test_credential(_make_credential())
    assert ok is False
    assert category == "invalid_response"


def test_test_credential_never_logs_the_api_key(monkeypatch, caplog):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    credential = _make_credential(api_key=SECRET_KEY)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        _test_credential(credential)
    assert SECRET_KEY not in caplog.text
