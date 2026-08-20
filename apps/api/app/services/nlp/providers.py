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

`assert_url_still_allowed` re-validates but still hands httpx a hostname to
resolve on its own -- a narrow TOCTOU window remains between the check and
httpx's own lookup. `pin_custom_destination` closes it: it runs the same
policy and returns a `PinnedDestination` connected to the exact address just
validated, so there's no second resolution left to race. The extractor uses
it for every `provider="custom"` request; `assert_url_still_allowed` stays
as-is for non-request callers (credential resolution, save-time checks).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal
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


@dataclass(frozen=True)
class CallContext:
    """Who a resolved credential bills, for the usage-recording call sites
    Wave 2b wires up. Kept here, next to `LlmCredential`, rather than in
    `llm_client` -- that module owns transport and security only, never
    `user_id` or billing attribution (see plan §3)."""

    credential: LlmCredential
    payer: Literal["user", "operator"]


PROVIDER_PRESETS: dict[str, str] = {
    # provider key -> pinned base_url. Every entry here is OpenAI-compatible
    # EXCEPT anthropic, which llm_client.py dispatches to its own native
    # Messages API wire shape -- see that module's Anthropic branch.
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "anthropic": "https://api.anthropic.com/v1",
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


def _format_host(host: str) -> str:
    """Bracket a bare IPv6 literal for use in a URL or Host header -- shared
    by every place below that assembles one from a hostname or an address."""
    return f"[{host}]" if ":" in host else host


def _normalize(parsed: SplitResult) -> str:
    host = _format_host((parsed.hostname or "").lower())
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{port}{path}"


def _host_header(parsed: SplitResult) -> str:
    """The ORIGINAL host (+ :port when the URL had one) for the Host header
    `pin_custom_destination` sends alongside a pinned IP -- the server on
    the other end still needs to see the hostname it was configured with."""
    host = _format_host((parsed.hostname or "").lower())
    port = f":{parsed.port}" if parsed.port else ""
    return f"{host}{port}"


def _build_pinned_url(parsed: SplitResult, address: str) -> str:
    """The request URL with `address` -- one of the addresses the policy
    just validated -- substituted for the parsed URL's host. Port and path
    are preserved; an IPv6 address gets bracketed."""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{_format_host(address)}{port}{path}"


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

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        # e.g. an unbalanced IPv6 bracket ("https://[::1/v1") -- urlsplit
        # raises instead of returning a SplitResult here, and every caller
        # only catches DestinationRejected, so an uncaught ValueError would
        # 500 a PUT for a value that's never even stored.
        raise DestinationRejected(_DESTINATION_REJECTED, "base_url is malformed") from exc

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


@dataclass(frozen=True)
class PinnedDestination:
    """What the extractor actually connects to for one `provider="custom"`
    request, closing the gap `assert_url_still_allowed` alone leaves open:
    validating a hostname and then handing that SAME hostname to httpx for
    its own (separate) DNS lookup is a TOCTOU -- a hostname can answer with
    a public address during the check and a private one microseconds later
    for httpx's lookup. Connecting to the address we actually validated
    removes the second lookup, and therefore the window, entirely."""

    url: str  # request URL with the host swapped for the validated address
    host_header: str  # original host (+ :port if present), sent as Host
    sni_hostname: str | None  # original hostname for TLS SNI + cert check; None for http or an IP-literal host


def pin_custom_destination(url: str) -> PinnedDestination:
    """Runs the full destination policy -- same as `validate_custom_base_url`,
    reusing its `_prepare` / `_resolve_host_sync` / `_reject_if_non_global`
    helpers rather than re-implementing the policy a second time -- and then
    pins the request to ONE of the addresses that policy actually validated.
    Raises `DestinationRejected` on any violation, same as
    `validate_custom_base_url`.

    An IP-literal host has nothing to pin (there's no separate resolution
    for httpx to redo) -- it passes through unchanged with `sni_hostname`
    left `None`, since there's no hostname to verify a cert against.
    """
    parsed, private_tier, host_to_resolve = _prepare(url)
    host_header = _host_header(parsed)

    if host_to_resolve is None:
        return PinnedDestination(url=_normalize(parsed), host_header=host_header, sni_hostname=None)

    addresses = _resolve_host_sync(host_to_resolve)
    if not private_tier:
        _reject_if_non_global(addresses)

    # Any validated address is safe to use -- when the standard tier
    # applies every one of them just passed the all-global check; the
    # private tier permits non-global ones by design. There's no reason to
    # prefer one over another, so just take the first.
    pinned_url = _build_pinned_url(parsed, addresses[0])
    sni_hostname = host_to_resolve if parsed.scheme.lower() == "https" else None
    return PinnedDestination(url=pinned_url, host_header=host_header, sni_hostname=sni_hostname)


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

    A user can hold several named credentials (plan: 2026-08-19-multi-
    credential-llm-profiles), of which at most one is ACTIVE (the partial
    unique index on `user_id WHERE is_active`) -- this filters on that flag,
    so "no active row" (a user with only inactive spares, or none at all)
    falls straight through to the fallback/no-coverage branches below,
    identically to today's "no row" case.
    """
    row = db.execute(
        select(UserLlmCredential).where(
            UserLlmCredential.user_id == user_id, UserLlmCredential.is_active
        )
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


# ---------------------------------------------------------------------------
# Classification BYOK routing
#
# Classification runs on every ingested message, not just the settled-verdict
# subset extraction covers -- so a user who saves a key for extraction has NOT
# agreed to pay for classifying every marketing email they receive. Hence a
# SEPARATE per-credential opt-in (`classification_byok`) and a resolver that
# is deliberately NOT the extraction one: extraction is fine decrypting one
# row per credentialed user; classification would decrypt one row per message
# for every user in a running ingest, presets-only, tri-state, no fallback.
# ---------------------------------------------------------------------------

# Presets only in v1: a custom endpoint's per-request destination re-check is
# a synchronous DNS round-trip with a 3s deadline, unaffordable at one call
# per ingested message. Presets are pinned server-side and need no such
# check, so this resolver contains no code path that can perform DNS.
_CLASSIFICATION_ELIGIBLE_PROVIDERS = tuple(PROVIDER_PRESETS.keys())

# Bounded staleness for `ClassificationRouter`'s per-run memo. A Gmail ingest
# classifies message-by-message for up to 30 minutes; a once-per-run decision
# would mean un-ticking the opt-in checkbox has no effect until the run ends
# -- during exactly the flood the opt-in exists to guard against.
_ROUTING_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class ClassificationRouting:
    """
    Tri-state routing decision for one classification call -- deliberately
    NOT `Optional[LlmCredential]`. The classifier reads a bare `None` as "no
    BYOK routing", so an optional type would silently make it ambiguous
    whether a user's credential is absent or just became blocked or
    ineligible. `credential` is set if and only if `mode == "user"`.

    `fallback_local` (plan: 2026-08-14-llm-failure-visibility phase 2, D-A/
    D-H) is the user's opt-in to serve the local encoder when their own LLM
    call fails. Only meaningful for `mode == "user"`; stays at its default
    `False` for `server`/`off` -- there's no BYOK failure to fall back from
    on either of those.
    """

    # "server" means no BYOK routing: local model or heuristic, never an LLM.
    # Name kept for stability -- it no longer means the operator pays.
    mode: str  # "user" | "server" | "off"
    credential: LlmCredential | None
    fallback_local: bool = False


def resolve_classification_routing(db: Session, user_id: UUID) -> ClassificationRouting:
    """
    Resolve whether this user's messages get BYOK LLM classification, and
    with which credential, if and when the LLM path is reached (routing never
    overrides `CLASSIFIER_BACKEND` or the local encoder -- see
    `classifier.classify`). No mode here ever reaches an LLM the user didn't
    bring a key for; `mode="server"` falls back to the local model or the
    heuristic.

    A user can hold several named credentials, of which at most one is
    ACTIVE (partial unique index on `user_id WHERE is_active`) -- BOTH reads
    below filter on that flag, so an inactive spare (even one that still
    carries `classification_byok=true` from before it was switched out)
    never routes classification. No row, or no ACTIVE row, means the same
    thing here: `mode="server"`.

    Two-step read, and the ORDER is a security requirement, not an
    optimization:

    1. Project ONLY `(provider, classification_byok, classification_
       fallback_local)` -- never select the whole entity here. `api_key` is
       `EncryptedText`, which transparently DECRYPTS on row materialization;
       selecting the entity just to read two booleans would decrypt the
       plaintext key of every extraction-only user, in every active ingest
       worker, once a minute. `classification_fallback_local` rides along in
       this projection but isn't branched on here -- only `mode == "user"`
       cares about it, and step 2 re-reads it fresh for that case anyway
       (see below), same reasoning as the credential fields.
    2. Only when step 1 says opted-in AND the provider is a preset, issue a
       SECOND read that re-asserts every condition in its OWN WHERE clause
       (`user_id` AND `classification_byok IS true` AND `provider IN
       presets`) and builds the credential from what THAT returns. The
       predicate is never inherited from step 1's verdict: a PUT can rewrite
       provider/key/model/revision with no read lock between these two
       reads, and reusing step 1's answer could hand `mode="user"` to a
       credential the user has since disabled or switched to custom.

       This second read ALSO projects columns rather than the entity, same
       reasoning as step 1 but for a different hazard: an entity `select`
       goes through the Session's identity map, and if this same Session
       already loaded that row earlier in the ingest/backfill batch (still
       open, uncommitted), SQLAlchemy hands back that cached instance instead
       of re-reading -- so a key/model rotated mid-batch would stay stale for
       the rest of it, past the 60s TTL below. A column `select` always hits
       the database, so it can't return a stale identity-map instance.

    Resolution:
      - no row, or opted out -> `("server", None)` -- no BYOK routing; the
        classifier falls back to the local model or the heuristic.
      - opted in, `provider == "custom"` -> `("off", None)`, decided from the
        projected provider string alone. Presets-only in v1; no destination
        check, no DNS.
      - opted in, preset, second read returns zero rows -> `("off", None)`.
        The state changed mid-resolve; erring to `off` rather than `server`
        is deliberate -- an unresolvable race must never silently route to
        an LLM call the user hasn't (yet, reliably) authorized. The next
        refresh (see `ClassificationRouter`) settles it either way.
      - opted in, preset, second read confirms it -> `("user", credential)`.

    A DB error propagates -- the caller is mid-ingest and about to write to
    this same session, so swallowing it here would trade a loud failure for
    a silent wrong-routing decision.
    """
    projection = db.execute(
        select(
            UserLlmCredential.provider,
            UserLlmCredential.classification_byok,
            UserLlmCredential.classification_fallback_local,
        ).where(UserLlmCredential.user_id == user_id, UserLlmCredential.is_active)
    ).first()

    if projection is None or not projection.classification_byok:
        return ClassificationRouting(mode="server", credential=None)

    if projection.provider not in _CLASSIFICATION_ELIGIBLE_PROVIDERS:
        return ClassificationRouting(mode="off", credential=None)

    row = db.execute(
        select(
            UserLlmCredential.provider,
            UserLlmCredential.base_url,
            UserLlmCredential.api_key,
            UserLlmCredential.model,
            UserLlmCredential.classification_fallback_local,
        ).where(
            UserLlmCredential.user_id == user_id,
            UserLlmCredential.classification_byok.is_(True),
            UserLlmCredential.provider.in_(_CLASSIFICATION_ELIGIBLE_PROVIDERS),
            UserLlmCredential.is_active,
        )
    ).first()

    if row is None:
        return ClassificationRouting(mode="off", credential=None)

    credential = LlmCredential(
        provider=row.provider, base_url=row.base_url, api_key=row.api_key, model=row.model
    )
    return ClassificationRouting(
        mode="user", credential=credential, fallback_local=row.classification_fallback_local
    )


class ClassificationRouter:
    """
    Per-run memo over `resolve_classification_routing`, re-reading every
    `_ROUTING_TTL_SECONDS`. Constructed ONCE per ingest run (never inside a
    per-message or per-page loop) and exposes `routing_for(db)`, which call
    sites invoke once per message -- the memo makes that ~free between
    refreshes.

    Without this, a Gmail ingest classifying message-by-message for up to 30
    minutes would resolve routing once at the start, so un-ticking the
    consent box mid-run would have no effect until the run finished --
    during precisely the flood the opt-in exists to guard against. Re-reading
    every minute instead settles a revocation within a minute, at the cost of
    one cheap indexed query per minute per run rather than one per message.
    """

    def __init__(self, user_id: UUID) -> None:
        self._user_id = user_id
        self._routing: ClassificationRouting | None = None
        self._resolved_at: float | None = None

    def routing_for(self, db: Session) -> ClassificationRouting:
        now = time.monotonic()
        if (
            self._routing is None
            or self._resolved_at is None
            or now - self._resolved_at >= _ROUTING_TTL_SECONDS
        ):
            self._routing = resolve_classification_routing(db, self._user_id)
            self._resolved_at = now
        return self._routing
