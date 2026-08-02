"""Unit tests for the BYOK provider presets, the custom-endpoint destination
policy (the security core of this feature), and credential resolution --
all offline: DNS is monkeypatched (socket.getaddrinfo), no real network or
database. resolve_extraction_credential is exercised against a FakeDB that
answers db.execute(...).scalar_one_or_none() with a canned row, mirroring
test_extraction_run.py's fake-session idiom.
"""

import asyncio
import socket
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.nlp import providers
from app.services.nlp.providers import (
    PROVIDER_PRESETS,
    DestinationRejected,
    PinnedDestination,
    ResolvedExtraction,
    assert_url_still_allowed,
    assert_url_still_allowed_async,
    extraction_available,
    extraction_feature_enabled,
    pin_custom_destination,
    resolve_extraction_credential,
    resolve_preset_base_url,
    validate_custom_base_url,
)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    """Answers the one SELECT resolve_extraction_credential issues with a
    canned row (or None) regardless of the statement's exact shape -- the
    statement itself is plain (filter by user_id) and not worth asserting
    on here."""

    def __init__(self, row):
        self.row = row

    def execute(self, stmt):
        return _FakeResult(self.row)


def _make_row(
    *,
    provider="openai",
    base_url=None,
    api_key="sk-stored-key-1234",
    model="gpt-4o-mini",
    revision=1,
    user_id=None,
    credential_id=None,
):
    return SimpleNamespace(
        id=credential_id or uuid4(),
        user_id=user_id or uuid4(),
        provider=provider,
        base_url=base_url or PROVIDER_PRESETS.get(provider, "https://example.com/v1"),
        api_key=api_key,
        model=model,
        revision=revision,
    )


def _enable_custom(monkeypatch, *, private=False):
    monkeypatch.setattr(providers.settings, "llm_custom_endpoints_enabled", True)
    monkeypatch.setattr(providers.settings, "llm_private_endpoints_enabled", private)


def _disable_custom(monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_custom_endpoints_enabled", False)
    monkeypatch.setattr(providers.settings, "llm_private_endpoints_enabled", False)


def _mock_resolves_to(monkeypatch, *ips):
    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(providers.socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_preset_base_urls_are_pinned():
    assert PROVIDER_PRESETS == {
        "openai": "https://api.openai.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "mistral": "https://api.mistral.ai/v1",
    }
    assert "anthropic" not in PROVIDER_PRESETS


def test_resolve_preset_base_url_ignores_caller_value():
    """The function only takes `provider`, not a caller-supplied base_url --
    structurally there's nothing for a caller value to override."""
    assert resolve_preset_base_url("openai") == PROVIDER_PRESETS["openai"]
    assert resolve_preset_base_url("gemini") == PROVIDER_PRESETS["gemini"]


# ---------------------------------------------------------------------------
# Custom-endpoint destination policy: flag gating
# ---------------------------------------------------------------------------


def test_validate_custom_base_url_rejected_when_custom_flag_off(monkeypatch):
    _disable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("https://example.com/v1")
    assert exc_info.value.reason == "custom_disabled"


def test_assert_url_still_allowed_rejected_when_custom_flag_off(monkeypatch):
    _disable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        assert_url_still_allowed("https://example.com/v1")
    assert exc_info.value.reason == "custom_disabled"


# ---------------------------------------------------------------------------
# Structural rejection: userinfo / query / fragment / length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://key@example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#fragment",
    ],
    ids=["userinfo-only", "userinfo-password", "query", "fragment"],
)
def test_validate_custom_base_url_rejects_structural_violations(monkeypatch, url):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")  # global -- would pass the address check
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url(url)
    assert exc_info.value.reason == "destination_rejected"


def test_validate_custom_base_url_rejects_over_length(monkeypatch):
    _enable_custom(monkeypatch)
    long_host = "a" * 250
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url(f"https://{long_host}.example.com/v1")
    assert exc_info.value.reason == "destination_rejected"


@pytest.mark.parametrize(
    "url",
    ["https://[::1/v1", "https://[::1:v1"],
    ids=["unbalanced-ipv6-bracket", "missing-closing-bracket-with-port-like-suffix"],
)
def test_validate_custom_base_url_rejects_malformed_url_instead_of_500ing(monkeypatch, url):
    """`urlsplit` raises a bare ValueError on a malformed URL like an
    unbalanced IPv6 bracket -- every caller only catches DestinationRejected,
    so an uncaught ValueError here would 500 a PUT for a value that's never
    even stored."""
    _enable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url(url)
    assert exc_info.value.reason == "destination_rejected"


# ---------------------------------------------------------------------------
# Non-global rejection: IP literal and resolved address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "127.0.0.1", "10.0.0.5", "192.168.1.1"],
    ids=["link-local-metadata", "loopback", "rfc1918-10", "rfc1918-192"],
)
def test_validate_custom_base_url_rejects_non_global_ip_literal(monkeypatch, host):
    _enable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url(f"https://{host}/v1")
    assert exc_info.value.reason == "destination_rejected"


def test_validate_custom_base_url_rejects_non_global_resolved_address(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("https://metadata.internal/v1")
    assert exc_info.value.reason == "destination_rejected"


def test_validate_custom_base_url_rejects_if_any_resolved_address_non_global(monkeypatch):
    """A hostname that resolves to multiple addresses is rejected if even
    ONE of them is non-global."""
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34", "127.0.0.1")
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("https://multi.example.com/v1")
    assert exc_info.value.reason == "destination_rejected"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_validate_custom_base_url_normalizes(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    assert validate_custom_base_url("HTTPS://Example.COM/v1/") == "https://example.com/v1"


# ---------------------------------------------------------------------------
# Two-tier matrix
# ---------------------------------------------------------------------------


def test_standard_tier_allows_https_global(monkeypatch):
    _enable_custom(monkeypatch, private=False)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    assert validate_custom_base_url("https://example.com/v1") == "https://example.com/v1"


def test_standard_tier_rejects_http(monkeypatch):
    _enable_custom(monkeypatch, private=False)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("http://example.com/v1")
    assert exc_info.value.reason == "destination_rejected"


def test_standard_tier_rejects_non_global_hostname(monkeypatch):
    _enable_custom(monkeypatch, private=False)
    _mock_resolves_to(monkeypatch, "192.168.1.5")
    with pytest.raises(DestinationRejected):
        validate_custom_base_url("https://ollama.local/v1")


def test_private_tier_allows_http_and_non_global(monkeypatch):
    _enable_custom(monkeypatch, private=True)
    _mock_resolves_to(monkeypatch, "192.168.1.5")
    assert (
        validate_custom_base_url("http://ollama.local:11434/v1")
        == "http://ollama.local:11434/v1"
    )


def test_private_tier_still_rejects_structural_violations(monkeypatch):
    """The private tier only lifts the scheme/global restrictions --
    userinfo/query/fragment bans and length still apply."""
    _enable_custom(monkeypatch, private=True)
    _mock_resolves_to(monkeypatch, "192.168.1.5")
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("http://user:pass@ollama.local:11434/v1")
    assert exc_info.value.reason == "destination_rejected"


def test_private_tier_requires_custom_flag_too(monkeypatch):
    monkeypatch.setattr(providers.settings, "llm_custom_endpoints_enabled", False)
    monkeypatch.setattr(providers.settings, "llm_private_endpoints_enabled", True)
    with pytest.raises(DestinationRejected) as exc_info:
        validate_custom_base_url("http://ollama.local:11434/v1")
    assert exc_info.value.reason == "custom_disabled"


# ---------------------------------------------------------------------------
# resolve_extraction_credential: coverage matrix + dead-stop + no fallback
# ---------------------------------------------------------------------------


def test_resolve_extraction_credential_preset_row_usable(monkeypatch):
    row = _make_row(provider="openai")
    result = resolve_extraction_credential(_FakeDB(row), row.user_id)
    assert result.credential is not None
    assert result.credential.provider == "openai"
    assert result.source == "user"
    assert result.stored is True
    assert result.blocked_reason is None
    assert result.revision == row.revision
    assert result.credential_id == row.id


def test_resolve_extraction_credential_usable_custom_row(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    row = _make_row(provider="custom", base_url="https://example.com/v1", revision=7)
    result = resolve_extraction_credential(_FakeDB(row), row.user_id)
    assert result.credential is not None
    assert result.credential.base_url == "https://example.com/v1"
    assert result.source == "user"
    assert result.stored is True
    assert result.blocked_reason is None
    assert result.revision == 7
    assert result.credential_id == row.id


def test_resolve_extraction_credential_dead_stops_stored_row_when_custom_disabled(monkeypatch):
    _disable_custom(monkeypatch)
    row = _make_row(provider="custom", base_url="https://example.com/v1")
    result = resolve_extraction_credential(_FakeDB(row), row.user_id)
    assert result.credential is None
    assert result.source is None
    assert result.stored is True
    assert result.blocked_reason == "custom_disabled"
    # id/revision still ride along -- the row exists, it's just blocked.
    assert result.revision == row.revision
    assert result.credential_id == row.id


def test_resolve_extraction_credential_dead_stops_stored_row_when_private_flag_disabled(monkeypatch):
    """A private-tier row (http/non-global) stops working the moment the
    private flag alone is turned off, even with the base custom flag still on."""
    _enable_custom(monkeypatch, private=False)
    _mock_resolves_to(monkeypatch, "192.168.1.5")
    row = _make_row(provider="custom", base_url="http://ollama.local:11434/v1")
    result = resolve_extraction_credential(_FakeDB(row), row.user_id)
    assert result.credential is None
    assert result.blocked_reason == "destination_rejected"


def test_resolve_extraction_credential_blocked_custom_row_does_not_fall_back(monkeypatch):
    """Frozen policy: a blocked stored custom row must NOT fall through to
    the operator's server key -- the user explicitly routed elsewhere."""
    _disable_custom(monkeypatch)
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", True)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "server-key")
    row = _make_row(provider="custom", base_url="https://example.com/v1")
    result = resolve_extraction_credential(_FakeDB(row), row.user_id)
    assert result.credential is None
    assert result.source is None
    assert result.stored is True
    assert result.blocked_reason == "custom_disabled"


def test_resolve_extraction_credential_fallback_synthesis(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", True)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "server-key")
    monkeypatch.setattr(providers.settings, "gemini_model", "gemini-2.5-flash")
    result = resolve_extraction_credential(_FakeDB(None), uuid4())
    assert result.source == "fallback"
    assert result.stored is False
    assert result.blocked_reason is None
    assert result.credential.provider == "gemini"
    assert result.credential.api_key == "server-key"
    assert result.credential.model == "gemini-2.5-flash"
    assert result.credential.base_url == PROVIDER_PRESETS["gemini"]
    assert result.revision is None
    assert result.credential_id is None


def test_resolve_extraction_credential_byok_only_mode_blocks_uncredentialed_user(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", False)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "server-key")
    result = resolve_extraction_credential(_FakeDB(None), uuid4())
    assert result == ResolvedExtraction(None, None, False, None, None, None)


def test_resolve_extraction_credential_no_row_no_fallback_key(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", True)
    monkeypatch.setattr(providers.settings, "gemini_api_key", None)
    result = resolve_extraction_credential(_FakeDB(None), uuid4())
    assert result == ResolvedExtraction(None, None, False, None, None, None)


# ---------------------------------------------------------------------------
# extraction_available / extraction_feature_enabled
# ---------------------------------------------------------------------------


def test_extraction_available_false_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_enabled", False)
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", True)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "server-key")
    assert extraction_available(_FakeDB(None), uuid4()) is False


def test_extraction_available_true_with_fallback_coverage(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_enabled", True)
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", True)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "server-key")
    assert extraction_available(_FakeDB(None), uuid4()) is True


def test_extraction_available_false_with_no_coverage(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_enabled", True)
    monkeypatch.setattr(providers.settings, "action_extraction_server_fallback", False)
    monkeypatch.setattr(providers.settings, "gemini_api_key", None)
    assert extraction_available(_FakeDB(None), uuid4()) is False


def test_extraction_feature_enabled_is_flag_only_no_db(monkeypatch):
    monkeypatch.setattr(providers.settings, "action_extraction_enabled", True)
    assert extraction_feature_enabled() is True
    monkeypatch.setattr(providers.settings, "action_extraction_enabled", False)
    assert extraction_feature_enabled() is False


# ---------------------------------------------------------------------------
# Dedicated DNS executor: bounded deadline, sync AND async wrapper forms
# ---------------------------------------------------------------------------


def test_assert_url_still_allowed_sync_rejects_within_deadline_on_stalling_dns(monkeypatch):
    _enable_custom(monkeypatch)

    def stalling_getaddrinfo(host, port):
        time.sleep(4.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(providers.socket, "getaddrinfo", stalling_getaddrinfo)

    start = time.monotonic()
    with pytest.raises(DestinationRejected) as exc_info:
        assert_url_still_allowed("https://stalls-sync.example.com/v1")
    elapsed = time.monotonic() - start

    assert exc_info.value.reason == "destination_rejected"
    # Bounded by the 3s executor deadline, not the 4s stall.
    assert elapsed < 3.9


def test_assert_url_still_allowed_async_rejects_within_deadline_on_stalling_dns(monkeypatch):
    _enable_custom(monkeypatch)

    def stalling_getaddrinfo(host, port):
        time.sleep(4.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(providers.socket, "getaddrinfo", stalling_getaddrinfo)

    async def run():
        start = time.monotonic()
        with pytest.raises(DestinationRejected) as exc_info:
            await assert_url_still_allowed_async("https://stalls-async.example.com/v1")
        elapsed = time.monotonic() - start
        assert exc_info.value.reason == "destination_rejected"
        assert elapsed < 3.9

    asyncio.run(run())


# ---------------------------------------------------------------------------
# pin_custom_destination: the pinned-IP transport closing the DNS-rebinding
# TOCTOU window -- assert_url_still_allowed validates a hostname and then
# hands that SAME hostname to httpx for its own separate lookup; this is the
# entry point that connects to the address actually validated instead.
# ---------------------------------------------------------------------------


def test_pin_custom_destination_pins_to_resolved_ip_preserves_port_and_path(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    pinned = pin_custom_destination("https://example.com:8443/v1/custom")
    assert isinstance(pinned, PinnedDestination)
    assert pinned.url == "https://93.184.216.34:8443/v1/custom"
    assert pinned.host_header == "example.com:8443"
    assert pinned.sni_hostname == "example.com"


def test_pin_custom_destination_brackets_ipv6_resolved_address(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "2606:4700:4700::1111")
    pinned = pin_custom_destination("https://example.com/v1")
    assert pinned.url == "https://[2606:4700:4700::1111]/v1"
    assert pinned.host_header == "example.com"


def test_pin_custom_destination_host_header_has_no_port_when_url_had_none(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    pinned = pin_custom_destination("https://example.com/v1")
    assert pinned.host_header == "example.com"


def test_pin_custom_destination_sni_hostname_none_for_http(monkeypatch):
    _enable_custom(monkeypatch, private=True)
    _mock_resolves_to(monkeypatch, "192.168.1.5")
    pinned = pin_custom_destination("http://ollama.local:11434/v1")
    assert pinned.url == "http://192.168.1.5:11434/v1"
    assert pinned.host_header == "ollama.local:11434"
    assert pinned.sni_hostname is None


def test_pin_custom_destination_ip_literal_host_passes_through_unchanged(monkeypatch):
    _enable_custom(monkeypatch)
    # No DNS resolution should even be attempted for an IP-literal host.
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve an IP literal")),
    )
    pinned = pin_custom_destination("https://93.184.216.34/v1")
    assert pinned.url == "https://93.184.216.34/v1"
    assert pinned.host_header == "93.184.216.34"
    assert pinned.sni_hostname is None


def test_pin_custom_destination_rejected_when_custom_flag_off(monkeypatch):
    _disable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        pin_custom_destination("https://example.com/v1")
    assert exc_info.value.reason == "custom_disabled"


def test_pin_custom_destination_rejects_non_global_resolved_address(monkeypatch):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(DestinationRejected) as exc_info:
        pin_custom_destination("https://metadata.internal/v1")
    assert exc_info.value.reason == "destination_rejected"


@pytest.mark.parametrize(
    "url",
    [
        "https://key@example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#fragment",
    ],
    ids=["userinfo-only", "userinfo-password", "query", "fragment"],
)
def test_pin_custom_destination_rejects_structural_violations(monkeypatch, url):
    _enable_custom(monkeypatch)
    _mock_resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(DestinationRejected) as exc_info:
        pin_custom_destination(url)
    assert exc_info.value.reason == "destination_rejected"


def test_pin_custom_destination_rejects_malformed_url_instead_of_500ing(monkeypatch):
    _enable_custom(monkeypatch)
    with pytest.raises(DestinationRejected) as exc_info:
        pin_custom_destination("https://[::1/v1")
    assert exc_info.value.reason == "destination_rejected"
