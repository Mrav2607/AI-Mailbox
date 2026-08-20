"""BYOK LLM settings routes: view/save/test/remove the caller's credentials.

A caller can hold several named credentials (plan: 2026-08-19-multi-
credential-llm-profiles), of which at most one is active; ``GET``/``PUT``/
``DELETE`` under ``/settings/llm`` are active-credential VIEWS (D4) kept for
backward compatibility, while ``/settings/llm/credentials`` and its
sub-routes are the additive multi-credential surface (D5). Every STRUCTURAL
mutation across both surfaces (PUT's rewrite, create, activate, either
DELETE) runs under the D6 guard protocol -- see ``_lock_guard_user``'s
docstring for why a per-user lock beats a credential-row lock here.

GET, DELETE, and ``/test`` under the singular surface are sync ``def``s --
the repo's deliberate threadpool convention (REVIEW.md), which is what
keeps ``/test``'s blocking provider call (and, for a stored ``custom`` row,
its blocking DNS re-check) off the event loop. ``PUT`` and ``POST
/credentials`` are the router's ``async def``s: both read a raw request
body themselves (see ``put_llm_settings``'s docstring for why no body
parameter is declared), and their own blocking work -- the custom-endpoint
DNS check -- is awaited through the policy's async wrapper so the event
loop is never touched either way. ``activate``/the by-id ``DELETE`` take no
body and no DNS re-check, so they're sync ``def``s like the rest.

Every lookup/mutation below filters ``UserLlmCredential.user_id ==
current_user.id`` -- ownership is a WHERE clause, never an assumption.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.ratelimit import user_rate_limit
from app.deps import get_current_user, get_db
from app.db.models import (
    ActionItem,
    AppUser,
    Classification,
    LlmUsageDaily,
    MailMessage,
    MailThread,
    UserLlmCredential,
)
from app.db.schemas.llm_settings import (
    ClassifierMixKind,
    ClassifierMixOut,
    LlmCredentialDeleted,
    LlmCredentialList,
    LlmCredentialSummary,
    LlmSettingsOut,
    LlmTestResultOut,
    LlmUsageOut,
)
from app.services.nlp import persistence
from app.services.nlp.extractor import test_credential
from app.services.nlp.providers import (
    PROVIDER_PRESETS,
    DestinationRejected,
    ResolvedExtraction,
    resolve_classification_routing,
    resolve_extraction_credential,
    resolve_preset_base_url,
    validate_custom_base_url_async,
)

router = APIRouter(prefix="/settings/llm")

# A settings payload is a few hundred bytes; this is a hard cap, not a
# realistic size, so an authenticated caller can't make the route buffer an
# arbitrarily large body on the event loop before the 512-char key check
# ever runs (the deployment puts no cap on this in front of the API).
_MAX_BODY_BYTES = 8192
_MIN_KEY_LEN = 8
_MAX_KEY_LEN = 512
# Same discipline as api_key: an empty/whitespace-only model gets stored and
# fails opaquely against the provider later, and a wildly long one is never
# a real model name -- both are rejected here with a fixed detail, not left
# for the provider's own error message to surface.
_MAX_MODEL_LEN = 200
# name: same bounds discipline, plan D7. 60 chars is comfortably more than
# any reasonable label ("work openai key") while still a hard cap.
_MIN_NAME_LEN = 1
_MAX_NAME_LEN = 60
# D8: five named credentials per user. Checked under the D6 guard lock, not
# a DB constraint -- no single-column check can express "count(*) <= 5".
_MAX_CREDENTIALS_PER_USER = 5


def _effective_backend() -> str:
    """The SAME normalized expression `classifier.classify` dispatches on --
    `(settings.classifier_backend or "auto").lower()`.

    Settings' own field validator now normalizes, maps the legacy spellings
    and rejects anything unrecognized at boot, so by the time this runs the
    value is already canonical and this expression is a no-op. Kept anyway:
    it costs nothing, and it means this function and `classify()` still read
    identically, which is what stops them drifting if either side's
    normalization ever moves again.
    """
    return (settings.classifier_backend or "auto").lower()


def _lock_guard_user(db: Session, user_id: UUID) -> None:
    """D6's serialization anchor: locks the caller's `app_user` row via
    `SELECT ... FOR UPDATE` before any STRUCTURAL read or write of their
    credential rows (create, activate, delete, PUT's field rewrite --
    `/test`'s verify stamp stays guard-free, as today).

    Row locks on the credential rows alone can't serialize a create: with
    zero-to-four existing rows there's no row to lock the gap with, so two
    concurrent creates could both count 4 and insert a 5th and 6th -- no DB
    constraint enforces the cap. `app_user` always exists (exactly one row,
    trivial to find) and needs no advisory-lock key-collision bookkeeping,
    so it's the stable thing every credential mutation locks instead. This
    REPLACES the old single-row `with_for_update()` PUT used to take on the
    credential row itself -- no route below takes a separate credential-row
    lock of its own; this is THE lock.

    Delegates to `persistence.lock_guard_user` -- the same implementation
    `record_extraction`'s D2 credential-identity check takes, so there's one
    SQL statement for this lock, not two copies that could drift (plan:
    2026-08-19-extraction-cost-hardening D2).
    """
    persistence.lock_guard_user(db, user_id)


def _reset_extraction_attempts(db: Session, user_id: UUID) -> None:
    """D2 recovery: zero ``attempts`` on the caller's ``outcome='failed'``
    ``action_item`` rows, under the SAME guard-locked transaction as the
    credential mutation that's about to commit.

    A credential-class failure (D2's cap-jump in `record_extraction`) leaves
    a row terminally capped at ``MAX_ATTEMPTS`` under the credential that
    just failed it -- the whole point is to stop burning calls on a
    credential known to be dead. But once the user actually FIXES that
    credential (a material PUT, an activate, or a first-create landing
    active), those rows must get a fresh claimable life, or D2's storm fix
    would trade "storms forever" for "silently stuck forever" instead.

    Callers gate this on what actually changed: a material rewrite or an
    activate that switches which credential is active, yes; a flag-only PUT,
    an inactive spare create, a single spare delete, or the kill switch, no
    -- none of those change anything about the credential a failed row was
    capped under, so resetting there would just re-spend calls against the
    exact same dead credential.
    """
    db.execute(
        update(ActionItem)
        .where(ActionItem.user_id == user_id, ActionItem.outcome == "failed")
        .values(attempts=0)
    )


def _credential_rows(db: Session, user_id: UUID) -> list[UserLlmCredential]:
    """Plain (lock-free) read of every credential row the caller owns.
    Safe to call ONLY after `_lock_guard_user` has locked this user's guard
    row -- every other writer for this user is already serialized behind
    that lock, so there's nothing left here for a row-level lock to add.
    """
    return list(
        db.execute(
            select(UserLlmCredential).where(UserLlmCredential.user_id == user_id)
        ).scalars().all()
    )


def _active_row(rows: list[UserLlmCredential]) -> UserLlmCredential | None:
    return next((row for row in rows if row.is_active), None)


def _validate_name(name_input: object) -> str:
    """Trimmed, 1..60 chars (D7) -- same fixed-detail, never-echo discipline
    as every other field this router validates by hand."""
    if not isinstance(name_input, str):
        raise HTTPException(status_code=422, detail="name must be a string")
    name = name_input.strip()
    if not (_MIN_NAME_LEN <= len(name) <= _MAX_NAME_LEN):
        raise HTTPException(status_code=422, detail="name length out of bounds")
    return name


def _resolve_classification_byok(
    classification_byok_input: bool | None,
    provider: str,
    existing: UserLlmCredential | None,
) -> bool:
    """Absent flag means false on create, unchanged on update (`existing`
    is `None` for every create path, including `POST /credentials`, so this
    reduces to "false unless explicitly set" there). A `custom` provider
    always forces false -- an explicit true+custom ask already 422'd at the
    caller, so what's left is a flag the credential is INHERITING across a
    switch to `custom`; clearing it means no stored row ever combines
    provider="custom" with classification_byok=true, which `resolve_
    classification_routing` would otherwise have to treat as "off" anyway.
    """
    if classification_byok_input is not None:
        value = classification_byok_input
    elif existing is not None:
        value = bool(existing.classification_byok)
    else:
        value = False
    return False if provider == "custom" else value


def _resolve_classification_fallback_local(
    classification_fallback_local_input: bool | None,
    existing: UserLlmCredential | None,
) -> bool:
    """Same absent-preserves convention as `_resolve_classification_byok`:
    absent means false on create, unchanged on update. Unlike that flag,
    this one is never forced by provider or by `classification_byok` itself
    -- it's simply inert until `classification_byok` is also true (see the
    model's own docstring), so writing it while BYOK is off (or the
    provider is `custom`) is allowed and persisted exactly as given, never
    coerced or reset.
    """
    if classification_fallback_local_input is not None:
        return classification_fallback_local_input
    if existing is not None:
        return bool(existing.classification_fallback_local)
    return False


def _credential_summary(row: UserLlmCredential) -> dict:
    """The shared response shape for every multi-credential route (`GET
    /credentials`, `POST /credentials`, `POST /credentials/{id}/activate`)
    -- never the api_key, same discipline as `_settings_payload`."""
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "model": row.model,
        # Last 4 chars only, safe since the minimum stored key length is 8.
        "key_suffix": row.api_key[-4:],
        "last_verified_at": row.last_verified_at,
        "active": row.is_active,
        "classification_byok": bool(row.classification_byok),
        "classification_fallback_local": bool(row.classification_fallback_local),
    }


def _settings_payload(
    row: UserLlmCredential | None,
    resolved: ResolvedExtraction,
    *,
    routing_is_user: bool,
) -> dict:
    """The shared GET response shape. ``row`` carries every field that
    ``ResolvedExtraction`` doesn't (last_verified_at, the display fields) --
    ``resolved`` is still the ONE source for coverage-derived fields
    (``fallback_active``, ``custom_blocked``) so this never re-derives
    coverage from partial signals the way ``ResolvedExtraction``'s own
    docstring warns against.

    ``routing_is_user`` comes from a SINGLE ``resolve_classification_routing
    (...)`` call at the caller -- this never re-resolves routing itself, so a
    mid-request state change can't produce two different answers within one
    response.

    ``classification_eligible`` and ``classification_llm_usable`` read
    similarly but answer different questions, and they diverge ON PURPOSE
    even though classification has no operator-paid path anymore (the
    server-paid classify path was removed; classification is BYOK-or-
    heuristic, full stop):

    - ``classification_eligible`` describes the DEFAULT path (no per-run
      override), which is why it's gated on ``classifier_uses_llm`` too --
      `CLASSIFIER_BACKEND=heuristic` returns keyword rules before routing is
      even read (classifier.py), so eligibility without that gate would lie
      about a deployment where the LLM path is provably dead.
    - ``classification_llm_usable`` describes an EXPLICIT "llm" backend
      override (e.g. a backfill request), which bypasses a global
      `heuristic` default entirely -- so it is NOT gated on
      `classifier_uses_llm`. It's true only when this user's own opted-in
      key routes (`routing_is_user`).

    History: both formulas used to also check an operator `gemini_api_key`,
    and PR #18 fixed a bug where that operator-key term reported "usable"
    for `mode="off"` (an opted-in `custom` credential, or the resolver's
    concurrent-state-change branch) even though `classifier.py` runs
    keyword rules for `off` before either key is ever considered. Removing
    the operator-paid classify path removed the operator-key term entirely,
    which is why the two formulas now read identically in shape -- but the
    `eligible`/`usable` divergence itself is untouched and still real: do
    not collapse them back into one formula.
    """
    backend = _effective_backend()
    classifier_uses_llm = backend != "heuristic"
    classification_eligible = routing_is_user and classifier_uses_llm
    classification_llm_usable = routing_is_user
    return {
        "configured": row is not None,
        "provider": row.provider if row is not None else None,
        "model": row.model if row is not None else None,
        "base_url": row.base_url if row is not None else None,
        # Last 4 chars only, safe since the minimum stored key length is 8.
        "key_suffix": row.api_key[-4:] if row is not None else None,
        "last_verified_at": row.last_verified_at if row is not None else None,
        "extraction_enabled": bool(settings.action_extraction_enabled),
        "fallback_active": resolved.source == "fallback",
        "custom_endpoints_enabled": bool(settings.llm_custom_endpoints_enabled),
        "private_endpoints_enabled": bool(settings.llm_private_endpoints_enabled),
        "custom_blocked": resolved.blocked_reason is not None,
        "classification_byok": bool(row.classification_byok) if row is not None else False,
        "classification_fallback_local": (
            bool(row.classification_fallback_local) if row is not None else False
        ),
        "classifier_uses_llm": classifier_uses_llm,
        "classifier_backend": backend,
        "classification_eligible": classification_eligible,
        "classification_llm_usable": classification_llm_usable,
    }


@router.get("", response_model=LlmSettingsOut)
def get_llm_settings(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's ACTIVE credential, as a view (D4): a user can now hold
    several named credentials, so this filters on `is_active` -- without it,
    `scalar_one_or_none()` below RAISES the moment a second row exists.
    """
    row = db.execute(
        select(UserLlmCredential).where(
            UserLlmCredential.user_id == current_user.id, UserLlmCredential.is_active
        )
    ).scalar_one_or_none()
    resolved = resolve_extraction_credential(db, current_user.id)
    routing = resolve_classification_routing(db, current_user.id)
    return _settings_payload(row, resolved, routing_is_user=routing.mode == "user")


# `days` bounds: a 400-day cap matches the retention tick (plan §8), so the
# window can never outrun what's actually still on disk. An unbounded value
# would be an unbounded scan over `llm_usage_daily`, hence the hard `le`.
_MIN_USAGE_WINDOW_DAYS = 1
_MAX_USAGE_WINDOW_DAYS = 400

# Every breakdown (totals, by_stage, by_provider) sums the same five
# columns -- named once here so the three queries below can't drift apart
# on which columns they aggregate.
_USAGE_COUNTER_COLUMNS = (
    LlmUsageDaily.calls,
    LlmUsageDaily.calls_with_total_tokens,
    LlmUsageDaily.prompt_tokens,
    LlmUsageDaily.completion_tokens,
    LlmUsageDaily.total_tokens,
)


def _usage_sums(*columns: ColumnElement[int]) -> list[ColumnElement[int]]:
    """`SUM` alone returns SQL `NULL` over zero matching rows -- coalescing
    to 0 here is what lets an unused account get zeroed totals back instead
    of nulls.
    """
    return [func.coalesce(func.sum(column), 0) for column in columns]


def _usage_counters_dict(counters: Iterable[Decimal | int]) -> dict[str, int]:
    """Postgres returns `SUM(bigint)` as `numeric`, which arrives as
    `Decimal` -- cast every counter to `int` here so nothing downstream
    (the response schema, the UI's JSON parsing) has to special-case that.
    `counters` is any 5-item iterable in `_USAGE_COUNTER_COLUMNS` order.
    """
    calls, calls_with_total_tokens, prompt_tokens, completion_tokens, total_tokens = counters
    return {
        "calls": int(calls),
        "calls_with_total_tokens": int(calls_with_total_tokens),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
    }


@router.get("/usage", response_model=LlmUsageOut)
def get_llm_usage(
    days: int = Query(default=30, ge=_MIN_USAGE_WINDOW_DAYS, le=_MAX_USAGE_WINDOW_DAYS),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Account-level background LLM usage over a trailing window: calls and
    tokens split by stage and provider, plus a daily series. A readout, never
    billing -- no currency amount is computed or returned here, on purpose
    (docs/plans/2026-08-02-llm-usage-visibility-plan.md §1). Pricing varies
    per provider/model/tier and we have no authoritative table, least of all
    for a `custom` endpoint, so this route never carries a number that
    invites one.

    Deliberately its OWN endpoint, not a field folded onto `GET
    /settings/llm`: `_settings_payload` above is already duplicated verbatim
    in the PUT response, so every field added there has to be added twice,
    and a plain settings read shouldn't drag an aggregate query along with
    it.

    Window convention: `usage_date >= today - (days - 1)`, UTC calendar
    days -- so `days=1` means "today only," not "the last 24 hours." Get
    that `-1` backwards and every window silently reports one extra (or one
    fewer) day than the caller asked for.

    All four aggregates are computed in SQL (`func.sum`/`func.coalesce`),
    never by pulling raw rows and summing them in Python -- a user with no
    rows at all still gets a well-formed response back (zeroed totals, empty
    lists), never a 404.
    """
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    # user_id filtered FIRST, same self-scoping convention as every other
    # lookup in this router -- ownership is a WHERE clause, never an
    # assumption.
    where = (LlmUsageDaily.user_id == current_user.id, LlmUsageDaily.usage_date >= cutoff)

    totals_row = db.execute(select(*_usage_sums(*_USAGE_COUNTER_COLUMNS)).where(*where)).one()
    totals = _usage_counters_dict(totals_row)

    by_stage_rows = db.execute(
        select(LlmUsageDaily.stage, *_usage_sums(*_USAGE_COUNTER_COLUMNS))
        .where(*where)
        .group_by(LlmUsageDaily.stage)
        .order_by(LlmUsageDaily.stage)
    ).all()
    by_stage = [{"stage": stage, **_usage_counters_dict(counters)} for stage, *counters in by_stage_rows]

    by_provider_rows = db.execute(
        select(LlmUsageDaily.provider, *_usage_sums(*_USAGE_COUNTER_COLUMNS))
        .where(*where)
        .group_by(LlmUsageDaily.provider)
        .order_by(LlmUsageDaily.provider)
    ).all()
    by_provider = [
        {"provider": provider, **_usage_counters_dict(counters)}
        for provider, *counters in by_provider_rows
    ]

    daily_rows = db.execute(
        select(
            LlmUsageDaily.usage_date,
            *_usage_sums(LlmUsageDaily.calls, LlmUsageDaily.total_tokens),
        )
        .where(*where)
        .group_by(LlmUsageDaily.usage_date)
        .order_by(LlmUsageDaily.usage_date)
    ).all()
    daily = [
        {"date": usage_date, "calls": int(calls), "total_tokens": int(total_tokens)}
        for usage_date, calls, total_tokens in daily_rows
    ]

    return {
        "window_days": days,
        "totals": totals,
        "by_stage": by_stage,
        "by_provider": by_provider,
        "daily": daily,
    }


def _classifier_mix_kind(model_version: str | None) -> ClassifierMixKind:
    """Maps a raw ``classification.model_version`` string onto the closed
    set of kinds the UI renders (plan §7). Matched against literal prefixes
    and the specific values each code path actually stamps -- never against
    "any string with a colon" -- so a `custom`-credential quirk or a
    historical, oddly-named operator model stamp can't be misfiled by
    accident of string shape alone. This is the ONE place that mapping
    lives; the SQL aggregate below groups by `model_version`, but every
    response row is by `kind`, so a second copy of this logic would be how
    the two silently drift.
    """
    if model_version is None:
        return "unknown"
    if model_version.startswith("local:"):
        return "local"
    if model_version in ("heuristic-v1", "heuristic-fallback"):
        return "heuristic"
    if model_version == "user-override":
        return "manual"
    # All three historic seed stamps map to "demo": "demo-seed" is what the
    # committed seeder writes today, "demo-1" and "seeded" came from the two
    # gitignored legacy scripts. Mapping only some of them would leave the
    # rest misfiled as operator_key -- 100% paid usage on a mailbox that
    # never made a call.
    if model_version in ("demo-seed", "demo-1", "seeded"):
        return "demo"
    preset, sep, _rest = model_version.partition(":")
    if sep and preset in PROVIDER_PRESETS:
        return "user_key"
    # Everything else is a bare model name (settings.gemini_model) from the
    # now-removed server-paid classify path -- historical only. No row of
    # this shape can be written anymore (that path is gone), but existing
    # rows in the DB still carry the stamp, and `custom` is excluded from
    # PROVIDER_PRESETS on purpose (providers.py), so no row this router can
    # ever see was stamped by one. The mapping stays so those old rows don't
    # get misfiled into `unknown`.
    return "operator_key"


@router.get("/classifier-mix", response_model=ClassifierMixOut)
def get_classifier_mix(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Which classifier labeled the mail this user CURRENTLY has -- point-in-
    time state, not a time-windowed history. `classification` rows are
    upserted in place on reclassification without touching a timestamp
    (persistence.py), so a "last N days by created_at" view would silently
    omit exactly the reclassified messages that matter (plan §7). This is
    its own endpoint, not a field on `LlmSettingsOut`: that schema is shared
    verbatim by GET and PUT, and a required new field here would force PUT
    to run this aggregate too or fail response validation.

    No rate limit, matching `/usage`'s precedent -- only `/test` is limited,
    because it alone makes a live outbound call. Cost grows with the user's
    total mail (R5 in the plan), but that's confined to this endpoint, which
    only runs when the panel showing it is open.

    `:uid` is always `current_user.id`, never a caller-supplied parameter --
    there is no router-level auth dependency in this file, so every route
    (this one included) declares and scopes its own.

    Zero classifications yields `{"classifier_mix": []}` through the inner
    joins -- a 200, matching `/usage`'s empty state, never a 404.
    """
    rows = db.execute(
        select(Classification.model_version, func.count())
        .select_from(Classification)
        .join(MailMessage, MailMessage.id == Classification.message_id)
        .join(MailThread, MailThread.id == MailMessage.thread_id)
        .where(MailThread.user_id == current_user.id)
        .group_by(Classification.model_version)
    ).all()

    # Summed AFTER mapping, not before: the mapping is many-to-one (e.g.
    # `heuristic-v1` and `heuristic-fallback` both land on `heuristic`), so a
    # naive row-per-model_version response would emit duplicate rows for one
    # kind and the UI would double-print a category.
    counts: dict[ClassifierMixKind, int] = {}
    for model_version, count in rows:
        kind = _classifier_mix_kind(model_version)
        counts[kind] = counts.get(kind, 0) + int(count)

    return {"classifier_mix": [{"kind": kind, "count": count} for kind, count in counts.items()]}


async def _read_bounded_body(request: Request) -> bytes:
    """Reads the raw request body under a hard cap, checked BEFORE any
    parsing happens. ``Content-Length`` is checked first so an oversized
    declared length is rejected without reading a single byte; the running
    total during streaming catches a caller that understates or omits that
    header.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        if declared is not None and declared > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")

    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
    return bytes(chunks)


async def _validate_credential_payload(
    payload: dict,
) -> tuple[str, str, str, str | None, bool, bool | None, bool | None]:
    """Context-free field validation shared by `PUT /settings/llm` and
    `POST /settings/llm/credentials` -- everything about a credential body
    except how `api_key`'s absence should be handled (PUT can fall back to
    a stored key; a create has nothing to fall back on, so that decision is
    the caller's). Same fixed-detail, never-echo discipline throughout;
    see `put_llm_settings`'s own docstring for why this whole router reads
    bodies by hand instead of declaring a pydantic one.

    Returns `(provider, model, base_url, api_key, api_key_provided,
    classification_byok_input, classification_fallback_local_input)` --
    `api_key` is the stripped value if present, else `None`.
    """
    provider = payload.get("provider")
    api_key = payload.get("api_key")
    model = payload.get("model")
    base_url_input = payload.get("base_url")
    classification_byok_input = payload.get("classification_byok")
    classification_fallback_local_input = payload.get("classification_fallback_local")

    if not isinstance(provider, str):
        raise HTTPException(status_code=422, detail="provider must be a string")
    if not isinstance(model, str):
        raise HTTPException(status_code=422, detail="model must be a string")
    if classification_byok_input is not None and not isinstance(classification_byok_input, bool):
        raise HTTPException(status_code=422, detail="classification_byok must be a boolean")
    if classification_fallback_local_input is not None and not isinstance(
        classification_fallback_local_input, bool
    ):
        raise HTTPException(
            status_code=422, detail="classification_fallback_local must be a boolean"
        )

    api_key_provided = api_key is not None
    if api_key_provided:
        if not isinstance(api_key, str):
            raise HTTPException(status_code=422, detail="api_key must be a string")
        api_key = api_key.strip()
        if not (_MIN_KEY_LEN <= len(api_key) <= _MAX_KEY_LEN):
            raise HTTPException(status_code=422, detail="api_key length out of bounds")

    model = model.strip()
    if not (1 <= len(model) <= _MAX_MODEL_LEN):
        raise HTTPException(status_code=422, detail="model length out of bounds")

    if provider in PROVIDER_PRESETS:
        # A preset write always takes the pinned base_url -- any
        # caller-supplied one is ignored, or pinning would mean nothing.
        base_url = resolve_preset_base_url(provider)
    elif provider == "custom":
        if not settings.llm_custom_endpoints_enabled:
            raise HTTPException(status_code=422, detail="custom endpoints are disabled")
        if not isinstance(base_url_input, str):
            raise HTTPException(status_code=422, detail="base_url must be a string")
        try:
            base_url = await validate_custom_base_url_async(base_url_input)
        except DestinationRejected:
            raise HTTPException(status_code=422, detail="base_url is not an allowed destination")
    else:
        raise HTTPException(status_code=422, detail="invalid provider")

    # Presets-only in v1: classification's per-request destination re-check
    # is a synchronous DNS round-trip, unaffordable at one call per ingested
    # message (see providers.py). An explicit ask to turn the flag on for a
    # `custom` credential is rejected outright, rather than silently
    # dropped, so the caller isn't left thinking it took effect.
    if classification_byok_input is True and provider == "custom":
        raise HTTPException(
            status_code=422,
            detail="classification_byok requires a preset provider; custom endpoints "
            "are not eligible for classification in v1",
        )

    return (
        provider,
        model,
        base_url,
        api_key,
        api_key_provided,
        classification_byok_input,
        classification_fallback_local_input,
    )


@router.put("", response_model=LlmSettingsOut)
async def put_llm_settings(
    request: Request,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Save (or replace) the caller's ACTIVE credential (D4): a user can now
    hold several named ones, but PUT stays a view onto the single active
    slot, for backward compatibility -- naming/switching between several
    goes through ``POST /settings/llm/credentials`` and its ``/activate``
    sibling instead.

    Deliberately takes ONLY ``request: Request`` -- no body parameter of any
    kind (not a pydantic model, not ``Body(...)``, not even ``bytes``).
    FastAPI only parses/validates a request body when a body FIELD is
    declared on the route; declaring one here would let a type-invalid
    payload (e.g. ``api_key`` as a list) fail pydantic before this function
    ever runs, and the app's default validation handler echoes the rejected
    input straight back in the 422 -- api_key included. Reading and
    validating the body by hand (``_validate_credential_payload``, shared
    with the create route) is what keeps it genuinely raw.

    This is one of the router's two ``async def``s because its only
    blocking work, the custom-endpoint DNS check, is awaited through
    ``validate_custom_base_url_async`` -- the shared executor future is
    awaited rather than blocked on, so the event loop is never held for the
    3s DNS budget.

    ``api_key`` is the one field that can be OMITTED on an update: the
    server never returns the raw key, so requiring it on every save would
    mean a user who's forgotten it can't touch anything else -- including
    turning off the classification opt-in they came here to revoke. A
    create still needs one, since there's nothing stored to fall back on.

    ``classification_byok: true`` is accepted here regardless of the
    deployment's ``CLASSIFIER_BACKEND`` -- including while it's
    ``heuristic``, which never even reads routing (plan:
    2026-08-16-classifier-default-honesty §1). That is deliberate, not an
    oversight: rejecting the opt-in would punish "use my key when you can"
    for a deployment-level setting the caller doesn't control. The flag can
    therefore sit **dormant** -- saved and reported as
    ``classification_eligible: false`` -- and be activated LATER, with no
    further action from this caller, purely by an administrator changing
    ``CLASSIFIER_BACKEND`` away from ``heuristic``. This is the one place
    that dormancy is documented for an API-only caller, since the console
    hides the opt-in checkbox entirely under a heuristic backend.

    Concurrency (D6): guard-locks ``app_user`` first, then reads the active
    row in the locked-set -- there is no separate credential-row
    ``FOR UPDATE`` anymore; the guard lock IS the serialization. On a
    brand-new user (no active row, no credentials at all), this creates the
    first one, named ``"default"`` (matching the migration backfill's
    choice) and active. The IntegrityError "existing row wins" branch below
    re-reads the ACTIVE row rather than any row by user_id, since a user can
    now legitimately own more than one.
    """
    raw_body = await _read_bounded_body(request)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="malformed request body")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")

    (
        provider,
        model,
        base_url,
        api_key,
        api_key_provided,
        classification_byok_input,
        classification_fallback_local_input,
    ) = await _validate_credential_payload(payload)

    # D6: lock the guard row before reading or writing this user's
    # credentials -- see _lock_guard_user's docstring for why a per-user
    # lock beats a credential-row lock here.
    _lock_guard_user(db, current_user.id)
    row = db.execute(
        select(UserLlmCredential).where(
            UserLlmCredential.user_id == current_user.id, UserLlmCredential.is_active
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if api_key_provided:
        effective_api_key = api_key
    elif row is not None:
        effective_api_key = row.api_key
    else:
        # Nothing stored yet to fall back on -- a brand-new credential
        # always needs a real key. Same fixed detail as a type-invalid key,
        # since from the caller's point of view both are "no usable key came
        # through."
        raise HTTPException(status_code=422, detail="api_key must be a string")

    def _apply_update(target: UserLlmCredential) -> None:
        # Read BEFORE mutating -- both flag resolvers and `material_changed`
        # need `target`'s state as it stood before this write. A flag-only
        # edit (no new key, same provider/base_url/model)
        # must NOT clear last_verified_at -- that'd be a confusing "your
        # verified key just went unverified" regression for a save that
        # didn't touch the credential itself.
        material_changed = (
            api_key_provided
            or target.provider != provider
            or target.base_url != base_url
            or target.model != model
        )
        target.classification_byok = _resolve_classification_byok(
            classification_byok_input, provider, target
        )
        target.classification_fallback_local = _resolve_classification_fallback_local(
            classification_fallback_local_input, target
        )
        target.provider = provider
        target.base_url = base_url
        target.api_key = effective_api_key
        target.model = model
        target.revision += 1
        if material_changed:
            target.last_verified_at = None
            # D2 recovery: a material rewrite is exactly the fix a
            # credential-class cap-jump was waiting for -- reset this
            # caller's terminally-capped rows so they're claimable again
            # under whatever now-different credential this write leaves
            # active. A flag-only edit changes nothing about the credential
            # itself, so it deliberately does NOT reset (the row is still
            # capped under the SAME dead credential).
            _reset_extraction_attempts(db, current_user.id)
        target.updated_at = now

    if row is None:
        # Guard-locked, so this is the ONLY writer for this user right now
        # -- the IntegrityError branch below is belt-and-suspenders (D6),
        # not the primary serialization mechanism the way it was before the
        # guard existed.
        row = UserLlmCredential(
            user_id=current_user.id,
            name="default",
            provider=provider,
            base_url=base_url,
            api_key=effective_api_key,
            model=model,
            classification_byok=_resolve_classification_byok(classification_byok_input, provider, None),
            classification_fallback_local=_resolve_classification_fallback_local(
                classification_fallback_local_input, None
            ),
            is_active=True,
            revision=1,
        )
        row.updated_at = now
        db.add(row)
        # D2 recovery: this caller's first-ever credential always lands
        # active -- same "the credential just changed" reasoning as a
        # material PUT above, for whatever failed rows predate having any
        # credential at all (e.g. rows capped under the server fallback).
        _reset_extraction_attempts(db, current_user.id)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.execute(
                select(UserLlmCredential).where(
                    UserLlmCredential.user_id == current_user.id, UserLlmCredential.is_active
                )
            ).scalar_one_or_none()
            if row is None:
                # The winning row vanished between the failed insert and
                # this re-read (e.g. a racing DELETE) -- one retry only,
                # never an unbounded loop.
                raise
            _apply_update(row)
            db.commit()
    else:
        _apply_update(row)
        db.commit()

    # Built directly from what we just wrote and validated, NOT a fresh
    # `resolve_extraction_credential`/`resolve_classification_routing` call:
    # the former's custom-provider path calls the SYNC destination-policy
    # re-check, which would block this async route's event loop for up to
    # its 3s DNS budget -- the exact thing being async here is meant to
    # avoid. Nothing here needs re-deriving: a row we just wrote is always
    # `configured`, never fallback-covered, and (having just passed
    # validation, custom included) never blocked. `routing_is_user` follows
    # the same rule `resolve_classification_routing` applies -- opted in AND
    # a preset provider -- and both are already guaranteed above (a `custom`
    # provider always leaves `classification_byok` false here). Unlike the
    # GET path, there's no third `mode="off"` case to reconstruct here: a row
    # that isn't `routing_is_user` was either never opted in or opted in on
    # `custom` (force-cleared above), which resolve_classification_routing's
    # own "no row, or opted out -> server" branch treats as `mode="server"`
    # -- never `"off"`. That distinction no longer matters for
    # `classification_llm_usable` below, though: with the operator-paid
    # classify path removed, the formula is BYOK-or-nothing regardless of
    # what mode a hypothetical fresh resolve would have returned.
    backend = _effective_backend()
    classifier_uses_llm = backend != "heuristic"
    routing_is_user = bool(row.classification_byok) and row.provider in PROVIDER_PRESETS
    eligible = routing_is_user and classifier_uses_llm
    # Deliberately NOT gated on `classifier_uses_llm` -- same divergence as
    # `_settings_payload`'s, spelled out there. Do not "fix" this to match
    # `eligible`.
    llm_usable = routing_is_user
    return {
        "configured": True,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "key_suffix": row.api_key[-4:],
        "last_verified_at": row.last_verified_at,
        "extraction_enabled": bool(settings.action_extraction_enabled),
        "fallback_active": False,
        "custom_endpoints_enabled": bool(settings.llm_custom_endpoints_enabled),
        "private_endpoints_enabled": bool(settings.llm_private_endpoints_enabled),
        "custom_blocked": False,
        "classification_byok": bool(row.classification_byok),
        "classification_fallback_local": bool(row.classification_fallback_local),
        "classifier_uses_llm": classifier_uses_llm,
        "classifier_backend": backend,
        "classification_eligible": eligible,
        "classification_llm_usable": llm_usable,
    }


@router.post(
    "/test",
    response_model=LlmTestResultOut,
    dependencies=[Depends(user_rate_limit("llm-test", 5, 60))],
)
def test_llm_settings(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Exercise the caller's own stored credential with a live call.

    The operator master switch is gated FIRST -- an incident/cost freeze
    must also stop user-triggered outbound calls, not just background
    extraction. The credential comes ONLY from ``resolve_extraction_credential``
    -- a raw owner-row lookup would bypass the destination policy -- and a
    resolution that only turned up the server fallback (``stored=False``)
    409s: fallback coverage must never make the operator's own key
    testable through this route.
    """
    if not settings.action_extraction_enabled:
        raise HTTPException(status_code=409, detail="action extraction disabled")

    resolved = resolve_extraction_credential(db, current_user.id)
    if not resolved.stored:
        raise HTTPException(status_code=409, detail="no LLM credential configured")
    if resolved.blocked_reason is not None:
        return {"ok": False, "latency_ms": 0, "error": "blocked_by_policy"}

    ok, error, latency_ms = test_credential(resolved.credential)
    if ok:
        # Conditioned on BOTH the immutable id and the revision captured at
        # resolve time -- revision alone has an ABA hole (delete + recreate
        # also starts at revision 1), so matching the id too is what stops a
        # slow test of a since-replaced credential from stamping the new
        # one as verified. Zero matching rows just means no stamp; the
        # current credential stays unverified.
        db.execute(
            update(UserLlmCredential)
            .where(
                UserLlmCredential.id == resolved.credential_id,
                UserLlmCredential.revision == resolved.revision,
            )
            .values(last_verified_at=datetime.now(timezone.utc))
        )
        db.commit()

    return {"ok": ok, "latency_ms": latency_ms, "error": error}


# response_model=None: a 204 carries no body, so declaring one would be a
# lie in the OpenAPI schema.
@router.delete("", status_code=204, response_model=None)
def delete_llm_settings(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Kill switch (D4): removes EVERY credential row the caller owns, not
    just the active one -- a wider sweep than the pre-multi-credential
    behavior, which had at most one row to begin with. Keeps its
    documented "remove my key entirely" meaning; use the by-id
    ``DELETE /settings/llm/credentials/{id}`` instead to remove a single
    inactive spare.

    Guard-locked (D6) like every other structural mutation, so a concurrent
    create/activate for this user can't interleave with the wipe.

    Deliberately does NOT reset D2's terminally-capped ``failed`` rows
    (unlike PUT/create/activate) -- there's no credential left to retry
    under, so resetting here would just leave them capped-at-zero with
    nothing to claim them until a NEW credential shows up, at which point
    that credential's own mutation resets them anyway.
    """
    _lock_guard_user(db, current_user.id)
    rows = _credential_rows(db, current_user.id)
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return Response(status_code=204)


@router.get("/credentials", response_model=LlmCredentialList)
def list_llm_credentials(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Every credential the caller owns, active or not (D5). Unguarded --
    a plain read reflects whatever's already committed; a mutation racing
    this GET is fine to observe either before or after, same as any other
    list endpoint in this API.
    """
    rows = db.execute(
        select(UserLlmCredential)
        .where(UserLlmCredential.user_id == current_user.id)
        .order_by(UserLlmCredential.created_at)
    ).scalars().all()
    return {"items": [_credential_summary(row) for row in rows]}


@router.post("/credentials", response_model=LlmCredentialSummary, status_code=201)
async def create_llm_credential(
    request: Request,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Add a new named credential (D5). Same raw-body, no-key-echo
    validation path as ``PUT /settings/llm`` (``_validate_credential_
    payload``) -- see that route's docstring for why no pydantic body model
    is declared. Unlike PUT, ``api_key`` is always required here: a create
    has nothing stored to fall back on.

    Active iff it's the caller's first credential (D5) -- every subsequent
    create starts inactive, so a switch is always an explicit ``/activate``
    call, never an implicit side effect of adding a spare.

    Guard-locked (D6): the cap (D8, five per user) and the per-user name
    uniqueness are both re-checked in the SAME locked read, since row locks
    alone can't serialize a create -- see ``_lock_guard_user``'s docstring
    for why a zero-to-four-row create race needs the app_user lock instead.
    """
    raw_body = await _read_bounded_body(request)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="malformed request body")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")

    name = _validate_name(payload.get("name"))

    (
        provider,
        model,
        base_url,
        api_key,
        api_key_provided,
        classification_byok_input,
        classification_fallback_local_input,
    ) = await _validate_credential_payload(payload)

    if not api_key_provided:
        # Same fixed detail PUT uses for the analogous "nothing to fall
        # back on" case -- from the caller's point of view both are "no
        # usable key came through."
        raise HTTPException(status_code=422, detail="api_key must be a string")

    _lock_guard_user(db, current_user.id)
    rows = _credential_rows(db, current_user.id)
    if len(rows) >= _MAX_CREDENTIALS_PER_USER:
        raise HTTPException(status_code=422, detail="credential limit reached")
    if any(existing.name == name for existing in rows):
        raise HTTPException(status_code=422, detail="a credential with this name already exists")

    now = datetime.now(timezone.utc)
    row = UserLlmCredential(
        user_id=current_user.id,
        name=name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        classification_byok=_resolve_classification_byok(classification_byok_input, provider, None),
        classification_fallback_local=_resolve_classification_fallback_local(
            classification_fallback_local_input, None
        ),
        is_active=not rows,
        revision=1,
    )
    row.updated_at = now
    db.add(row)
    if not rows:
        # D2 recovery: landing active because it's the caller's first
        # credential is the same "the active credential just changed" event
        # as a material PUT -- reset. An inactive spare (rows non-empty)
        # changes nothing about which credential is active, so it does not.
        _reset_extraction_attempts(db, current_user.id)
    db.commit()
    return _credential_summary(row)


@router.post("/credentials/{credential_id}/activate", response_model=LlmCredentialSummary)
def activate_llm_credential(
    credential_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Switch the caller's active credential (D5). 404s if ``credential_id``
    doesn't belong to the caller -- ownership is a WHERE clause inside
    ``_credential_rows``, never an assumption.

    Concurrency contract (D6): serialized LAST-WRITER-WINS, not "one 409s."
    Two concurrent activates of DIFFERENT credentials both return 200 under
    the guard lock -- the second request validly re-switches after the
    first commits, so the later commit's target is the one left active.

    Forced flush order, and it's a correctness requirement, not style: ORM
    attribute-assignment order does NOT control the emitted SQL order, and
    the partial unique index (``uq_llm_credential_user_active``) checks
    IMMEDIATELY -- it isn't deferrable. Flipping the old active row to
    false and flushing THAT before setting the target true is what stops a
    legitimate switch from bouncing off the index (both rows would
    otherwise briefly be active in the same statement batch).

    Does NOT bump ``revision`` -- that field guards ``/test``'s verify
    stamp against a since-replaced credential's CONTENTS changing;
    activating changes which row is read, not the row's contents.
    ``ClassificationRouter``'s per-run memo (providers.py) means an
    in-flight ingest can keep classifying on the previously-active key for
    up to ~60s after this call returns -- the same key-rotation window that
    already exists today, just now reachable by a switch instead of only a
    key replacement.
    """
    _lock_guard_user(db, current_user.id)
    rows = _credential_rows(db, current_user.id)
    target = next((row for row in rows if row.id == credential_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="credential not found")

    if not target.is_active:
        old_active = _active_row(rows)
        if old_active is not None:
            old_active.is_active = False
            db.flush()
        target.is_active = True
        # D2 recovery: a genuine switch changes which credential is active,
        # so any of the caller's rows capped under the old one deserve a
        # fresh claimable life. A call that targets the already-active
        # credential (this branch doesn't run) changes nothing.
        _reset_extraction_attempts(db, current_user.id)
    db.commit()
    return _credential_summary(target)


@router.delete("/credentials/{credential_id}", response_model=LlmCredentialDeleted)
def delete_llm_credential(
    credential_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Remove one named credential (D5) -- 409s if it's the active one
    (switch to another with ``/activate`` first, or use the singular
    ``DELETE /settings/llm`` kill switch to remove everything at once).
    404s if ``credential_id`` isn't the caller's.

    Always removes an INACTIVE spare, so the active credential (whatever
    D2's cap-jumped rows are capped under) never changes here -- no
    `_reset_extraction_attempts` call, same reasoning as the kill switch.
    """
    _lock_guard_user(db, current_user.id)
    rows = _credential_rows(db, current_user.id)
    target = next((row for row in rows if row.id == credential_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="credential not found")
    if target.is_active:
        raise HTTPException(status_code=409, detail="cannot delete the active credential")

    db.delete(target)
    db.commit()
    return {"status": "deleted"}
