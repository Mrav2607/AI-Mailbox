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
import threading
import time

import httpx
import pytest

from app.services.nlp import llm_client
from app.services.nlp.llm_client import LlmCallError, LlmCallResult, LlmUsage, call_chat_completion
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


def _choice_payload_with_usage(content: str, usage: object) -> dict:
    payload = _choice_payload(content)
    payload["usage"] = usage
    return payload


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

    result = call_chat_completion(
        credential, prompt="the prompt", user_content="the email text", max_tokens=512
    )

    assert isinstance(result, LlmCallResult)
    assert result.content == VALID_CONTENT
    # _choice_payload never sets a "usage" key, so this doubles as the
    # usage-absent case -- no usage block at all must not raise.
    assert result.usage is None
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


# ---------------------------------------------------------------------------
# Defensive `usage` parsing -- see `_parse_usage` in llm_client.py for the
# reasoning. These are wire-result tests (plan §3): they only check what
# `call_chat_completion` hands back, not any downstream stage contract.
# ---------------------------------------------------------------------------


def test_call_chat_completion_parses_a_complete_usage_block(monkeypatch):
    payload = _choice_payload_with_usage(
        VALID_CONTENT,
        {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    )
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.content == VALID_CONTENT
    assert result.usage == LlmUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)


def test_call_chat_completion_parses_a_partial_usage_block_without_synthesizing_total(monkeypatch):
    """A provider can report `prompt_tokens` and just not send a total --
    the parse must NOT add prompt + completion to invent one. A synthesized
    total would corrupt the "calls with a usable total" counter downstream."""
    payload = _choice_payload_with_usage(VALID_CONTENT, {"prompt_tokens": 100})
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.usage == LlmUsage(prompt_tokens=100, completion_tokens=None, total_tokens=None)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": "100"},
        {"prompt_tokens": 12.5},
        {"prompt_tokens": None},
        {},
    ],
    ids=["string", "float", "null", "empty-dict"],
)
def test_call_chat_completion_ignores_non_int_usage_fields(monkeypatch, usage):
    payload = _choice_payload_with_usage(VALID_CONTENT, usage)
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.content == VALID_CONTENT
    assert result.usage is None


def test_call_chat_completion_ignores_a_negative_usage_field(monkeypatch):
    payload = _choice_payload_with_usage(VALID_CONTENT, {"prompt_tokens": -5, "total_tokens": 10})
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.usage == LlmUsage(prompt_tokens=None, completion_tokens=None, total_tokens=10)


def test_call_chat_completion_rejects_a_bool_as_a_token_count(monkeypatch):
    """`bool` is a subclass of `int` in Python -- `True` must not be counted
    as a token, or a provider sending a boolean flag under a token-shaped key
    would silently register as one token."""
    payload = _choice_payload_with_usage(VALID_CONTENT, {"prompt_tokens": True, "total_tokens": 10})
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.usage == LlmUsage(prompt_tokens=None, completion_tokens=None, total_tokens=10)


def test_call_chat_completion_treats_a_non_dict_usage_as_absent(monkeypatch):
    payload = _choice_payload(VALID_CONTENT)
    payload["usage"] = "not a dict"
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.content == VALID_CONTENT
    assert result.usage is None


def test_call_chat_completion_survives_a_malformed_usage_block_and_still_returns_content(monkeypatch):
    """The core guarantee: usage is telemetry, not the payload. A response
    with a completely garbage `usage` shape must still be a successful call."""
    payload = _choice_payload(VALID_CONTENT)
    payload["usage"] = [1, 2, 3]
    _install_mock_transport(monkeypatch, _json_handler(200, payload))

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.content == VALID_CONTENT
    assert result.usage is None


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

    result = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert result.content
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

    result = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)

    assert result.content
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

    result = call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)

    assert result.content
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

    result = call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512
    )

    assert result.content == VALID_CONTENT
    assert len(captured) == 1
    client = captured[0]
    assert client.trust_env is False
    # With trust_env=False httpx never consults HTTPS_PROXY -- if it had,
    # this client would carry a mounted proxy transport for the https:// pattern.
    # `_mounts` is a private httpx attribute, so this is coupled to httpx's
    # internals and could break on an unrelated httpx upgrade -- what it's
    # really pinning is "no proxy transport got mounted."
    assert not any(client._mounts.values())


def _install_spy_client(monkeypatch):
    """Like _install_mock_transport, but hands back the constructed clients
    so a test can read the timeout config httpx actually received."""
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
    return captured


def test_call_chat_completion_default_timeout_splits_connect_from_read(monkeypatch):
    """A hung endpoint must fail cheap regardless of how slow generation is
    allowed to be -- connect is capped at 5s, separate from the (default 30s)
    budget for the rest of the call."""
    captured = _install_spy_client(monkeypatch)

    call_chat_completion(_make_credential(), prompt="prompt", user_content="text", max_tokens=512)

    assert len(captured) == 1
    timeout = captured[0].timeout
    assert timeout.connect == 5.0
    assert timeout.read == 30.0


def test_call_chat_completion_honors_an_explicit_timeout(monkeypatch):
    """Classification passes its own tighter timeout -- this must actually
    reach httpx, not just be accepted and ignored."""
    captured = _install_spy_client(monkeypatch)

    call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512, timeout=10.0
    )

    assert len(captured) == 1
    timeout = captured[0].timeout
    assert timeout.connect == 5.0
    assert timeout.read == 10.0


def test_call_chat_completion_connect_cap_never_exceeds_a_smaller_budget(monkeypatch):
    """A budget tighter than the 5s connect cap wins -- otherwise connect
    alone could outlast the whole call."""
    captured = _install_spy_client(monkeypatch)

    call_chat_completion(
        _make_credential(), prompt="prompt", user_content="text", max_tokens=512, timeout=2.0
    )

    assert captured[0].timeout.connect == 2.0


class _ChunkedStream(httpx.SyncByteStream):
    """A body that arrives in pieces. MockTransport hands back a single-shot
    body by default, which never exercises the between-chunks deadline check
    that a trickling endpoint is supposed to trip."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        yield from self._chunks

    def close(self):
        pass


def test_call_chat_completion_aborts_a_body_that_trickles_past_the_deadline(monkeypatch):
    """The core of the wall-clock bound: each chunk landing just inside
    httpx's per-read timeout resets that timeout forever, so `read` alone
    never ends the call. The deadline does.

    Deterministic and instant -- the clock is scripted, so nothing here waits
    on real time or on thread scheduling.
    """
    payload = json.dumps(_choice_payload(VALID_CONTENT)).encode()
    chunks = [payload[i : i + 4] for i in range(0, len(payload), 4)]
    assert len(chunks) > 2, "need several chunks for the trickle to be meaningful"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkedStream(chunks),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, handler)

    # Scripted clock: the call starts at 0 with a 10s budget, the first chunk
    # lands at 1s, and the second at 11s -- past the deadline. Every later
    # call repeats the last value.
    ticks = iter([0.0, 0.0, 1.0, 11.0])
    last = [0.0]

    def fake_monotonic():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(llm_client.time, "monotonic", fake_monotonic)

    with pytest.raises(LlmCallError) as excinfo:
        call_chat_completion(
            _make_credential(), prompt="prompt", user_content="text", max_tokens=512, timeout=10.0
        )

    assert excinfo.value.category == "timed_out"
    assert excinfo.value.status is None


def test_read_body_within_deadline_closes_a_response_that_goes_silent():
    """The half httpx can't cover: once a read is ALREADY blocked on a socket
    that went quiet, no amount of checking between chunks helps -- only
    closing the response interrupts it. Proves the watchdog actually fires
    and that the call ends at the deadline rather than one full read timeout
    later.

    Event-driven, so it returns the instant the watchdog closes the response
    instead of waiting out a fixed sleep.
    """
    closed = threading.Event()

    class _SilentResponse:
        def close(self):
            closed.set()

        def iter_bytes(self):
            yield b'{"cho'
            # Stands in for a read blocked on a silent socket: the only thing
            # that ends it is our own close(). The timeout here is a test
            # backstop, not the behavior under test.
            if not closed.wait(timeout=5.0):
                raise AssertionError("the watchdog never closed the response")
            raise httpx.ReadError("connection closed")

    start = time.monotonic()
    with pytest.raises(LlmCallError) as excinfo:
        llm_client._read_body_within_deadline(
            _SilentResponse(), deadline=time.monotonic() + 0.2, provider="openai"
        )
    elapsed = time.monotonic() - start

    assert excinfo.value.category == "timed_out"
    assert closed.is_set(), "the deadline must close the response, not just report late"
    assert elapsed < 2.0, f"cancellation should land near the 0.2s deadline, took {elapsed:.2f}s"


def test_read_body_within_deadline_reraises_a_real_failure_untouched():
    """A transport error that isn't our watchdog must stay a transport error
    -- mislabeling it `timed_out` would hide genuine connection failures."""

    class _BrokenResponse:
        def close(self):
            pass

        def iter_bytes(self):
            raise httpx.ReadError("connection reset")
            yield  # pragma: no cover -- makes this a generator

    with pytest.raises(httpx.ReadError):
        llm_client._read_body_within_deadline(
            _BrokenResponse(), deadline=time.monotonic() + 30.0, provider="openai"
        )


def test_call_chat_completion_never_logs_the_api_key(monkeypatch, caplog):
    _install_mock_transport(monkeypatch, _json_handler(500, {"error": "boom"}))
    credential = _make_credential(api_key=SECRET_KEY)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        with pytest.raises(LlmCallError):
            call_chat_completion(credential, prompt="prompt", user_content="text", max_tokens=512)
    assert SECRET_KEY not in caplog.text
