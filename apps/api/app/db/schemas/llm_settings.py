from datetime import datetime
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


class LlmTestResultOut(Response):
    """`POST /settings/llm/test` response.

    `error` is always one of the extractor's category constants (e.g.
    `blocked_by_policy`, `invalid_response`) -- never `str(exc)` and never
    anything echoed from the provider.
    """

    ok: bool
    latency_ms: int
    error: str | None
