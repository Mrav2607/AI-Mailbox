from datetime import date, datetime
from typing import Literal

from .common import Response

# `PUT /settings/llm` deliberately has NO request body schema here. FastAPI
# only parses/validates a body when a body field is declared on the route --
# a pydantic model would make a type-invalid payload (e.g. api_key as a list)
# fail validation before the route runs, and the app's default validation
# handler echoes the rejected input back in the 422, key included. The route
# reads the raw body itself and validates api_key/provider/model/base_url by
# hand with fixed-detail errors instead. Only RESPONSE shapes live here.

LlmProvider = Literal["openai", "gemini", "openrouter", "groq", "mistral", "custom"]


class LlmSettingsOut(Response):
    """GET and PUT `/settings/llm` share this shape.

    Unconfigured (`configured=False`) means `provider`/`model`/`base_url`/
    `key_suffix`/`last_verified_at` are all null -- there's no stored
    credential to describe. The api_key itself never appears here in any
    form.
    """

    configured: bool
    provider: LlmProvider | None
    model: str | None
    base_url: str | None
    # Last 4 chars of the stored key, safe since the minimum key length is 8.
    key_suffix: str | None
    last_verified_at: datetime | None
    # Operator master switch (ACTION_EXTRACTION_ENABLED) -- separate from
    # whether this user specifically has usable coverage.
    extraction_enabled: bool
    # True when this user has no stored credential but the operator's
    # server key covers them (ACTION_EXTRACTION_SERVER_FALLBACK).
    fallback_active: bool
    custom_endpoints_enabled: bool
    private_endpoints_enabled: bool
    # True when a stored custom credential is disabled by the current
    # flag/policy state -- the UI explains why extraction stopped.
    custom_blocked: bool
    # Per-credential opt-in: does this user's key also pay for classification
    # (every ingested message), not just extraction (the settled-verdict
    # subset)? False for an unconfigured user -- there's no credential to
    # opt in.
    classification_byok: bool
    # Per-credential opt-in (plan: 2026-08-14-llm-failure-visibility phase 2,
    # D-A): if a BYOK classification call fails, serve the local encoder
    # instead of leaving the message unclassified. Only meaningful when
    # `classification_byok` is also true -- writable independently of it and
    # persisted as-is either way, since a user may reasonably set this before
    # flipping BYOK on. Default false: existing BYOK users get no automatic
    # fallback until they opt in.
    classification_fallback_local: bool
    # Whether the effective classifier backend ever reaches an LLM at all --
    # `classifier_backend != "heuristic"`. Gates the opt-in checkbox itself:
    # a heuristic-only deployment has no LLM path for the key to pay for.
    classifier_uses_llm: bool
    # The effective backend `classify()` actually dispatches on -- the same
    # normalized `(CLASSIFIER_BACKEND or "auto").lower()` expression, so the
    # UI explains local-first behavior honestly instead of guessing from the
    # raw (unconstrained) config string.
    classifier_backend: str
    # ELIGIBILITY on the DEFAULT path, not observed usage: `resolve_
    # classification_routing(...).mode == "user"` AND `classifier_uses_llm`.
    # The routing check alone isn't enough -- `CLASSIFIER_BACKEND=heuristic`
    # returns keyword rules before routing is ever read (classifier.py), so
    # without the second half this would report "your key would be used"
    # on a deployment where it provably never is. Never renamed "active",
    # which would overclaim: an eligible credential still isn't called when
    # the local encoder handles a message.
    classification_eligible: bool
    # Would picking the EXPLICIT "llm" backend for a run actually reach an
    # LLM? True when this user's own key routes ("user") OR the operator has
    # a server key AND routing didn't resolve to "off". Deliberately NOT
    # gated on `classifier_uses_llm` the way `classification_eligible` is --
    # an explicit per-run backend override (e.g. a backfill request) bypasses
    # a global `heuristic` default, so the two fields diverge on purpose. Do
    # not "fix" them back to matching; see llm_settings.py's formula comment.
    # `mode="off"` (an opted-in `custom` credential, or the resolver's
    # concurrent-state-change branch) must report False even with an
    # operator key configured -- that was a shipped bug (PR #18): the UI
    # offered the LLM option and the run silently fell back to keyword
    # rules.
    classification_llm_usable: bool


class LlmTestResultOut(Response):
    """`POST /settings/llm/test` response.

    `error` is always one of the extractor's category constants (e.g.
    `blocked_by_policy`, `invalid_response`) -- never `str(exc)` and never
    anything echoed from the provider.
    """

    ok: bool
    latency_ms: int
    error: str | None


class LlmUsageCounters(Response):
    """The five additive counters shared verbatim by `totals` and every
    `by_stage`/`by_provider` row -- one shape, so a totals-only reader and a
    per-dimension reader see identical field names for identical numbers.
    Always ints here, never `Decimal`/`None`: the route coalesces SQL `NULL`
    (zero matching rows) to 0 and casts every summed column, since Postgres
    returns `SUM(bigint)` as `numeric`.
    """

    calls: int
    # A response can report `prompt_tokens` and omit `total_tokens`; this
    # counts only the calls where a genuine `total_tokens` came back, so the
    # UI can tell a full total from a partial one instead of rendering a
    # partial sum as if it covered every call.
    calls_with_total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LlmUsageByStage(LlmUsageCounters):
    """One row per stage (`classification`/`extraction`) within the window."""

    stage: str


class LlmUsageByProvider(LlmUsageCounters):
    """One row per provider (a preset or `custom`) within the window."""

    provider: str


class LlmUsageDailyPoint(Response):
    """One point in the daily series -- calls and total tokens only, not the
    full counter set: the UI's trend line doesn't need a per-day stage/
    provider/partial-token breakdown, and the totals/by_stage/by_provider
    blocks already carry that detail for the whole window.
    """

    date: date
    calls: int
    total_tokens: int


class LlmUsageOut(Response):
    """`GET /settings/llm/usage` response -- a readout, not billing (see
    docs/plans/2026-08-02-llm-usage-visibility-plan.md §1). No currency
    field lives here, on purpose: we have no authoritative pricing table,
    least of all for a `custom` endpoint, so this never carries a number
    that invites one.

    A user with zero usage rows still gets this shape back with zeroed
    totals and empty lists -- never a 404 and never a null field -- so the
    UI can render a genuine empty state instead of special-casing "no data."
    """

    # Echoes the validated `?days=` query param back, so the UI never has to
    # guess what window it's looking at after a request with a stale/default
    # value.
    window_days: int
    totals: LlmUsageCounters
    by_stage: list[LlmUsageByStage]
    by_provider: list[LlmUsageByProvider]
    daily: list[LlmUsageDailyPoint]


# The closed set `Classification.model_version` maps onto, server-side --
# plan §7. `custom` deliberately has no kind of its own: a `custom`
# credential can never route classification (presets-only, see
# providers.py), so no row can ever be stamped with one.
ClassifierMixKind = Literal["local", "user_key", "operator_key", "heuristic", "manual", "unknown"]


class ClassifierMixEntry(Response):
    """One row of `GET /settings/llm/classifier-mix` -- a `kind` summed
    across every `model_version` that maps onto it (plan §7's "sum after
    mapping" rule), never one row per raw `model_version`.
    """

    kind: ClassifierMixKind
    count: int


class ClassifierMixOut(Response):
    """`GET /settings/llm/classifier-mix` response: which classifier
    produced the mail a user currently has, not a time-windowed history (see
    the route's own docstring for why). Zero classifications yields
    `{"classifier_mix": []}` through the inner joins -- a 200, never a 404,
    same convention as `LlmUsageOut`.
    """

    classifier_mix: list[ClassifierMixEntry]
