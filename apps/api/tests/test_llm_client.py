"""Unit tests for the shared OpenAI-compatible wire call
(`call_chat_completion` / `LlmCallError`) -- trust_env, non-2xx/redirect
handling, per-request destination pinning for `provider="custom"` (including
the DNS-rebinding regression), response-shape checks, and the
key-never-logged property. All offline via httpx.MockTransport, no real LLM
or network required.

Every httpx.Client this module builds is routed through MockTransport by
subclassing httpx.Client for the duration of a test (_install_mock_transport)
-- this is the only way to inject a fake transport without changing
`call_chat_completion`'s signature, and it exercises the REAL
trust_env=False behavior (httpx itself decides whether to honor a proxy env
var; a subclass can't fake that part)."""

import json
import logging
import socket

import httpx
import pytest

from app.services.nlp import llm_client
from app.services.nlp.llm_client import LlmCallError, call_chat_completion
from app.services.nlp.providers import DestinationRejected, LlmCredential
from app.services.nlp import providers as providers_module

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


def _choice_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _install_mock_transport(monkeypatch, handler):
    """Route every httpx.Client call_chat_completion constructs through
    MockTransport(handler) -- no real network, no new dependency."""
    real_client_cls = httpx.Client

    class _MockedClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm_client.httpx, "Client", _MockedClient)


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


VALID_CONTENT = json.dumps({"has_action": True})


# ---------------------------------------------------------------------------
# call_chat_completion / LlmCallError categories
# ---------------------------------------------------------------------------


def test_call_chat_completion_success_round_trip_asserts_wire_shape(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(VALID_CONTENT), calls))
    credential = _make_credential()

    content = call_chat_completion(
        credential, prompt="the prompt", user_content="the email text", max_tokens=512
    )

    assert content == VALID_CONTENT
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
    assert body["messages"][0]["content"] == "the prompt\n\nthe email text"


def test_call_chat_completion_raises_connection_failed_on_network_error(monkeypatch):
    _install_mock_transport(monkeypatch, _raising_handler(httpx.ConnectError("boom")))
    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)
    assert exc_info.value.category == "connection_failed"
    assert exc_info.value.status is None


def test_call_chat_completion_raises_http_status_category_on_non_2xx(monkeypatch):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)
    assert exc_info.value.category == "http_500"
    assert exc_info.value.status == 500


def test_call_chat_completion_raises_http_status_category_on_3xx(monkeypatch):
    """Redirects are never followed, so `raise_for_status()` alone wouldn't
    catch this -- a 3xx must be categorized as `http_<status>`, not fall
    through to `response.json()` and surface as `invalid_response`."""
    _install_mock_transport(monkeypatch, _json_handler(302, {"error": "moved"}))
    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)
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
def test_call_chat_completion_raises_invalid_response_for_malformed_shapes(monkeypatch, body):
    _install_mock_transport(monkeypatch, _json_handler(200, body))
    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)
    assert exc_info.value.category == "invalid_response"


def test_call_chat_completion_blocked_by_policy_for_custom_provider(monkeypatch):
    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, {}, calls))

    def fake_pin(url):
        raise DestinationRejected("destination_rejected", "nope")

    monkeypatch.setattr(llm_client, "pin_custom_destination", fake_pin)
    credential = _make_credential(provider="custom", base_url="https://ollama.example.com/v1")

    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert exc_info.value.category == "blocked_by_policy"
    assert not calls  # rejected before any request left


def test_call_chat_completion_rechecks_destination_before_every_request(monkeypatch):
    """A sweep resolves a credential once but can run for minutes -- a DNS
    answer flipping non-global between two calls must block the SECOND one
    before any request reaches the wire, not just the first."""
    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(VALID_CONTENT), calls))

    check_count = {"n": 0}
    pinned = providers_module.PinnedDestination(
        url="https://93.184.216.34/v1",
        host_header="ollama.example.com",
        sni_hostname="ollama.example.com",
    )

    def fake_pin(url):
        check_count["n"] += 1
        if check_count["n"] > 1:
            raise DestinationRejected("destination_rejected", "flipped non-global")
        return pinned

    monkeypatch.setattr(llm_client, "pin_custom_destination", fake_pin)
    credential = _make_credential(provider="custom", base_url="https://ollama.example.com/v1")

    content = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert content
    assert len(calls) == 1

    with pytest.raises(LlmCallError) as exc_info:
        call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert exc_info.value.category == "blocked_by_policy"
    assert len(calls) == 1  # second call never reached the wire


def test_call_chat_completion_rebinding_regression_connects_to_pinned_address_not_a_second_lookup(monkeypatch):
    """The finding this closes: `assert_url_still_allowed` validates a
    HOSTNAME and then hands that same hostname to httpx, which does its OWN
    DNS lookup -- a hostname can answer public for the check and private
    microseconds later for httpx's lookup (DNS rebinding). Simulate exactly
    that with a getaddrinfo stub returning a public address on the
    validation call and a private one on any later call, and assert the
    request actually reaches the transport addressed at the PINNED public
    IP -- proving httpx never got a chance to re-resolve the hostname."""
    monkeypatch.setattr(providers_module.settings, "llm_custom_endpoints_enabled", True)
    monkeypatch.setattr(providers_module.settings, "llm_private_endpoints_enabled", False)

    call_count = {"n": 0}

    def rebinding_getaddrinfo(host, port):
        call_count["n"] += 1
        ip = "93.184.216.34" if call_count["n"] == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(providers_module.socket, "getaddrinfo", rebinding_getaddrinfo)

    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(VALID_CONTENT), calls))
    credential = _make_credential(provider="custom", base_url="https://rebind.example.com/v1")

    content = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)

    assert content
    assert call_count["n"] == 1  # DNS resolved exactly once -- httpx never re-resolved
    request = calls[0]
    assert str(request.url) == "https://93.184.216.34/v1/chat/completions"
    assert request.headers["host"] == "rebind.example.com"
    assert request.extensions["sni_hostname"] == "rebind.example.com"


def test_call_chat_completion_preset_request_unchanged_no_host_override_or_extensions(monkeypatch):
    """Presets are pinned to fixed, operator-controlled hostnames -- pinning
    them too would add failure modes on CDN-fronted endpoints for no
    security gain (out of scope, per the frozen contract). A preset
    credential's request must be byte-for-byte the same as before this
    change: no Host override, no sni_hostname extension."""
    calls = []
    _install_mock_transport(monkeypatch, _json_handler(200, _choice_payload(VALID_CONTENT), calls))
    credential = _make_credential(provider="openai", base_url="https://api.openai.com/v1")

    content = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)

    assert content
    request = calls[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    # httpx's own auto-generated Host header from the URL -- not an override.
    assert request.headers["host"] == "api.openai.com"
    assert request.extensions.get("sni_hostname") is None


def test_call_chat_completion_trust_env_false_ignores_private_proxy_env_var(monkeypatch):
    """trust_env=False is mandatory: httpx otherwise honors HTTP(S)_PROXY
    and would route the bearer credential through a proxy address the
    destination policy never validated."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    real_client_cls = httpx.Client
    captured = []

    class _SpyClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_json_handler(200, _choice_payload(VALID_CONTENT)))
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(llm_client.httpx, "Client", _SpyClient)

    content = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert content == VALID_CONTENT
    assert len(captured) == 1
    client = captured[0]
    assert client.trust_env is False
    # With trust_env=False httpx never consults HTTPS_PROXY -- if it had,
    # this client would carry a mounted proxy transport for the https:// pattern.
    # `_mounts` is a private httpx attribute, so this is coupled to httpx's
    # internals and could break on an unrelated httpx upgrade -- what it's
    # really pinning is "no proxy transport got mounted."
    assert not any(client._mounts.values())


def test_call_chat_completion_default_timeout_splits_connect_from_read(monkeypatch):
    """A hung endpoint must fail cheap regardless of how slow generation is
    allowed to be -- connect is always capped at 5s, separate from the
    (default 30s) budget for the rest of the call."""
    real_client_cls = httpx.Client
    captured = []

    class _SpyClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                _json_handler(200, _choice_payload(VALID_CONTENT))
            )
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(llm_client.httpx, "Client", _SpyClient)

    call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)

    assert len(captured) == 1
    timeout = captured[0].timeout
    assert timeout.connect == 5.0
    assert timeout.read == 30.0


def test_call_chat_completion_honors_an_explicit_timeout(monkeypatch):
    """Classification passes its own tighter timeout -- this must actually
    reach httpx, not just be accepted and ignored."""
    real_client_cls = httpx.Client
    captured = []

    class _SpyClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                _json_handler(200, _choice_payload(VALID_CONTENT))
            )
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(llm_client.httpx, "Client", _SpyClient)

    call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512, timeout=10.0
    )

    assert len(captured) == 1
    timeout = captured[0].timeout
    assert timeout.connect == 5.0
    assert timeout.read == 10.0


def test_call_chat_completion_never_logs_the_api_key(monkeypatch, caplog):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    credential = _make_credential(api_key=SECRET_KEY)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        with pytest.raises(LlmCallError):
            call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert SECRET_KEY not in caplog.text
