"""BYOK LLM provider presets, credential resolution, and the custom-endpoint
destination policy.

The destination policy is the security core of this feature: a custom
endpoint is a caller-chosen URL the server POSTs a bearer credential to, so
it's an SSRF vector by construction. Every check here is enforced at THREE
points -- PUT save time (`validate_custom_base_url[_async]`), credential
resolution (`assert_url_still_allowed`, so a flag flip or a re-resolution
failure dead-stops a stored row instead of just blocking new saves), and
immediately before every HTTP request the extractor makes for a custom
credential (a sweep can run for minutes; a DNS answer flipping non-global
between two calls must block the second one before any request leaves).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import UserLlmCredential

# ResolvedExtraction.blocked_reason values for a stored custom row.
_CUSTOM_DISABLED = "custom_disabled"
_DESTINATION_REJECTED = "destination_rejected"

# https://key@host/ must be structurally impossible to store -- it would
# otherwise be echoed in GET and surface in httpx exception strings.
_MAX_BASE_URL_LEN = 200

_DNS_TIMEOUT_SECONDS = 3.0
# getaddrinfo has no timeout of its own, and a user-supplied hostname can
# point at authoritative DNS that stalls. Resolution always runs on this
# dedicated, bounded executor -- never the event loop, never a request's own
# threadpool thread -- so a hostile resolver can tie up at most these 4
# threads (documented bound), and every caller still returns within 3s.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4)


@dataclass(frozen=True)
class LlmCredential:
    provider: str
    base_url: str
    api_key: str
    model: str


PROVIDER_PRESETS: dict[str, str] = {
    # provider key -> pinned base_url (production OpenAI-compatible surface).
    # anthropic is deliberately ABSENT: its compat layer is documented as
    # testing-only and ignores response_format -- native support is deferred.
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
}


def resolve_preset_base_url(provider: str) -> str:
    """The preset's pinned base_url. Deliberately takes only `provider`, not
    a caller-supplied base_url -- a preset write must not be able to route
    anywhere else, or pinning would mean nothing; the bearer key for e.g. the
    openai preset would end up wherever a caller said."""
    return PROVIDER_PRESETS[provider]


@dataclass(frozen=True)
class ResolvedExtraction:
    credential: LlmCredential | None  # usable credential, or None
    source: str | None  # "user" | "fallback" | None
    stored: bool  # a user row exists (even if blocked)
    blocked_reason: str | None  # "custom_disabled" | "destination_rejected" | None
    revision: int | None  # stored row's revision; None for fallback/no row
    credential_id: UUID | None  # stored row's immutable id; None for fallback/no row


class DestinationRejected(Exception):
    """Raised by the custom-endpoint destination policy for any violation --
    structural (userinfo/query/fragment/scheme/length), tier-gating (a flag
    off), address-based (non-global IP/resolved address), or a DNS
    resolution timeout/failure. `reason` is always one of the two
    `ResolvedExtraction.blocked_reason` values a custom row can carry; the
    extractor maps either to its own `blocked_by_policy` call-failure
    category."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(detail)


def _submit_resolve(host: str) -> Future:
    """The single place DNS actually happens -- both wrapper forms below
    consume this one future so there's exactly one resolution path to keep
    bounded and to reason about."""
    return _DNS_EXECUTOR.submit(socket.getaddrinfo, host, None)


def _resolve_host_sync(host: str) -> list[str]:
    """Sync DNS resolution with a hard deadline. Used by Celery workers, the
    extractor's per-request re-check, and every sync route. Any failure --
    timeout included -- maps to rejection; never a pass."""
    future = _submit_resolve(host)
    try:
        results = future.result(timeout=_DNS_TIMEOUT_SECONDS)
    except Exception as exc:
        raise DestinationRejected(
            _DESTINATION_REJECTED, "hostname could not be resolved in time"
        ) from exc
    return [info[4][0] for info in results]


async def _resolve_host_async(host: str) -> list[str]:
    """Async form of the above, awaiting the SAME executor future via
    asyncio.wrap_future + wait_for -- used by the PUT route only, so its 3s
    DNS budget never blocks the event loop."""
    future = _submit_resolve(host)
    try:
        results = await asyncio.wait_for(
            asyncio.wrap_future(future), timeout=_DNS_TIMEOUT_SECONDS
        )
    except Exception as exc:
        raise DestinationRejected(
            _DESTINATION_REJECTED, "hostname could not be resolved in time"
        ) from exc
    return [info[4][0] for info in results]


def _reject_if_non_global(addresses: list[str]) -> None:
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise DestinationRejected(
                _DESTINATION_REJECTED, "hostname resolves to a non-global address"
            )


def _normalize(parsed: SplitResult) -> str:
    host = (parsed.hostname or "").lower()
    if ":" in host:  # IPv6 literal -- put the brackets back for a valid URL.
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{port}{path}"


def _validate_structure_and_tier(url: str) -> tuple[SplitResult, bool]:
    """Structural, scheme, and length checks -- no I/O. Returns the parsed
    URL and whether the private tier applies. Raises `DestinationRejected`
    on any violation, including the base flag being off (checked first: a
    flag-off deployment shouldn't leak any more detail than "disabled")."""
    if not settings.llm_custom_endpoints_enabled:
        raise DestinationRejected(_CUSTOM_DISABLED, "custom endpoints are disabled")

    private_tier = settings.llm_private_endpoints_enabled

    if len(url) > _MAX_BASE_URL_LEN:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url exceeds max length")

    parsed = urlsplit(url)

    # A secret embedded as https://key@host/ must be structurally
    # impossible -- it would otherwise be stored, echoed in GET, and surface
    # in httpx exception strings.
    if parsed.username or parsed.password:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url must not contain credentials")
    if parsed.query:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url must not contain a query string")
    if parsed.fragment:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url must not contain a fragment")

    try:
        parsed.port  # .port lazily validates and raises ValueError on a malformed one.
    except ValueError as exc:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url has an invalid port") from exc

    allowed_schemes = {"https", "http"} if private_tier else {"https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url scheme is not allowed")

    if not parsed.hostname:
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url must include a host")

    return parsed, private_tier


def _prepare(url: str) -> tuple[SplitResult, bool, str | None]:
    """Structure/tier validation plus the IP-literal case (no I/O). Returns
    `(parsed, private_tier, host_to_resolve)` -- `host_to_resolve` is `None`
    when the host is already an IP literal (nothing left to resolve)."""
    parsed, private_tier = _validate_structure_and_tier(url)
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return parsed, private_tier, host

    if not private_tier and not ip.is_global:
        raise DestinationRejected(
            _DESTINATION_REJECTED, "base_url IP address is not globally routable"
        )
    return parsed, private_tier, None


def validate_custom_base_url(url: str) -> str:
    """Sync destination-policy validation: normalizes and returns the
    base_url on success, raising `DestinationRejected` on any violation.
    Save-time entry point; also reused by `assert_url_still_allowed` for the
    use-time re-check, since the same policy applies at both points."""
    parsed, private_tier, host_to_resolve = _prepare(url)
    if host_to_resolve is not None:
        addresses = _resolve_host_sync(host_to_resolve)
        if not private_tier:
            _reject_if_non_global(addresses)
    return _normalize(parsed)


async def validate_custom_base_url_async(url: str) -> str:
    """Async form of the above -- the PUT route's lone async def, so its DNS
    check is awaited rather than blocking the event loop."""
    parsed, private_tier, host_to_resolve = _prepare(url)
    if host_to_resolve is not None:
        addresses = await _resolve_host_async(host_to_resolve)
        if not private_tier:
            _reject_if_non_global(addresses)
    return _normalize(parsed)


def assert_url_still_allowed(url: str) -> None:
    """Use-time re-check, sync: Celery workers, the extractor's per-request
    re-check for `provider="custom"`, and every sync route. Re-runs the full
    policy (including the flag checks) so a flag flip or a DNS record change
    since save time is caught before the next request -- not just a pass."""
    validate_custom_base_url(url)


async def assert_url_still_allowed_async(url: str) -> None:
    """Async form of the above -- used by the PUT route only."""
    await validate_custom_base_url_async(url)


def resolve_extraction_credential(db: Session, user_id: UUID) -> ResolvedExtraction:
    """THE single source of extraction coverage for one user. Every consumer
    -- GET /settings/llm, the backfill 409, /test, and the pipeline
    preflights -- reads off this one type instead of re-deriving partial
    signals.

    Frozen policy: a stored custom row that's currently policy-blocked does
    NOT fall through to the server fallback -- the user explicitly routed
    their extraction elsewhere, and silently billing the operator's key
    instead would defeat the feature. Their extraction is simply unavailable
    until they fix or delete the credential.
    """
    row = db.execute(
        select(UserLlmCredential).where(UserLlmCredential.user_id == user_id)
    ).scalar_one_or_none()

    if row is not None:
        if row.provider == "custom":
            try:
                assert_url_still_allowed(row.base_url)
            except DestinationRejected as exc:
                return ResolvedExtraction(
                    credential=None,
                    source=None,
                    stored=True,
                    blocked_reason=exc.reason,
                    revision=row.revision,
                    credential_id=row.id,
                )
        credential = LlmCredential(
            provider=row.provider, base_url=row.base_url, api_key=row.api_key, model=row.model
        )
        return ResolvedExtraction(
            credential=credential,
            source="user",
            stored=True,
            blocked_reason=None,
            revision=row.revision,
            credential_id=row.id,
        )

    if settings.action_extraction_server_fallback and settings.gemini_api_key:
        credential = LlmCredential(
            provider="gemini",
            base_url=PROVIDER_PRESETS["gemini"],
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
        return ResolvedExtraction(
            credential=credential,
            source="fallback",
            stored=False,
            blocked_reason=None,
            revision=None,
            credential_id=None,
        )

    return ResolvedExtraction(
        credential=None,
        source=None,
        stored=False,
        blocked_reason=None,
        revision=None,
        credential_id=None,
    )


def extraction_feature_enabled() -> bool:
    """Cheap, DB-free flag check -- the recovery tick's early no-op and the
    settings payload's `extraction_enabled` field. Not a substitute for
    `extraction_available`, which also needs a resolved credential."""
    return bool(settings.action_extraction_enabled)


def extraction_available(db: Session, user_id: UUID) -> bool:
    """Replaces the old no-arg predicate everywhere: the operator flag AND
    actual resolved coverage for this specific user."""
    return bool(
        settings.action_extraction_enabled
        and resolve_extraction_credential(db, user_id).credential is not None
    )
