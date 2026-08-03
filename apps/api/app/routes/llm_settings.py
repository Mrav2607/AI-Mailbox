"""BYOK LLM settings routes: view/save/test/remove the caller's own
extraction credential.

GET, DELETE, and ``/test`` are sync ``def``s -- the repo's deliberate
threadpool convention (REVIEW.md), which is what keeps ``/test``'s blocking
provider call (and, for a stored ``custom`` row, its blocking DNS
re-check) off the event loop. ``PUT`` is the one ``async def`` in this
router: it has to read the raw request body itself (see its docstring for
why no body parameter is declared), and its own blocking work -- the
custom-endpoint DNS check -- is awaited through the policy's async wrapper
so the event loop is never touched either way.

Every lookup/mutation below filters ``UserLlmCredential.user_id ==
current_user.id`` -- ownership is a WHERE clause, never an assumption.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.ratelimit import user_rate_limit
from app.deps import get_current_user, get_db
from app.db.models import AppUser, LlmUsageDaily, UserLlmCredential
from app.db.schemas.llm_settings import LlmSettingsOut, LlmTestResultOut, LlmUsageOut
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


def _effective_backend() -> str:
    """The SAME normalized expression `classifier.classify` dispatches on --
    `(settings.classifier_backend or "auto").lower()`. The config value is an
    unconstrained string, so comparing it raw would misreport e.g.
    `CLASSIFIER_BACKEND=HEURISTIC` or an empty value as LLM-backed, showing a
    consent toggle that can never fire.
    """
    return (settings.classifier_backend or "auto").lower()


def _settings_payload(
    row: UserLlmCredential | None,
    resolved: ResolvedExtraction,
    *,
    classification_eligible: bool,
) -> dict:
    """The shared GET response shape. ``row`` carries every field that
    ``ResolvedExtraction`` doesn't (last_verified_at, the display fields) --
    ``resolved`` is still the ONE source for coverage-derived fields
    (``fallback_active``, ``custom_blocked``) so this never re-derives
    coverage from partial signals the way ``ResolvedExtraction``'s own
    docstring warns against. ``classification_eligible`` is likewise the
    caller's ONE source (``resolve_classification_routing(...).mode ==
    "user"``) -- eligibility, not observed usage.
    """
    backend = _effective_backend()
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
        "classifier_uses_llm": backend != "heuristic",
        "classifier_backend": backend,
        "classification_eligible": classification_eligible,
    }


@router.get("", response_model=LlmSettingsOut)
def get_llm_settings(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        select(UserLlmCredential).where(UserLlmCredential.user_id == current_user.id)
    ).scalar_one_or_none()
    resolved = resolve_extraction_credential(db, current_user.id)
    routing = resolve_classification_routing(db, current_user.id)
    return _settings_payload(row, resolved, classification_eligible=routing.mode == "user")


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


@router.put("", response_model=LlmSettingsOut)
async def put_llm_settings(
    request: Request,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Save (or replace) the caller's one credential.

    Deliberately takes ONLY ``request: Request`` -- no body parameter of any
    kind (not a pydantic model, not ``Body(...)``, not even ``bytes``).
    FastAPI only parses/validates a request body when a body FIELD is
    declared on the route; declaring one here would let a type-invalid
    payload (e.g. ``api_key`` as a list) fail pydantic before this function
    ever runs, and the app's default validation handler echoes the rejected
    input straight back in the 422 -- api_key included. Reading and
    validating the body by hand is what keeps it genuinely raw.

    This is the router's one ``async def`` because its only blocking work,
    the custom-endpoint DNS check, is awaited through
    ``validate_custom_base_url_async`` -- the shared executor future is
    awaited rather than blocked on, so the event loop is never held for the
    3s DNS budget.

    ``api_key`` is the one field that can be OMITTED on an update: the
    server never returns the raw key, so requiring it on every save would
    mean a user who's forgotten it can't touch anything else -- including
    turning off the classification opt-in they came here to revoke. A
    create still needs one, since there's nothing stored to fall back on.
    """
    raw_body = await _read_bounded_body(request)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="malformed request body")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")

    provider = payload.get("provider")
    api_key = payload.get("api_key")
    model = payload.get("model")
    base_url_input = payload.get("base_url")
    classification_byok_input = payload.get("classification_byok")

    if not isinstance(provider, str):
        raise HTTPException(status_code=422, detail="provider must be a string")
    if not isinstance(model, str):
        raise HTTPException(status_code=422, detail="model must be a string")
    if classification_byok_input is not None and not isinstance(classification_byok_input, bool):
        raise HTTPException(status_code=422, detail="classification_byok must be a boolean")

    # Absent OR explicit null api_key both mean "keep the stored one" -- the
    # server never echoes the raw key back, so that's the ONLY way to edit
    # anything else (e.g. the classification flag) without the caller having
    # the original key in hand. There's nothing to keep on a brand-new row,
    # so a create with no key (or an explicit null) still 422s below, once we
    # know whether a row exists to fall back on. Only a non-null value gets
    # validated right away.
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

    def _resolve_classification_byok(existing: UserLlmCredential | None) -> bool:
        """Absent flag means false on create, unchanged on update. A
        `custom` provider always forces false here -- an explicit true+custom
        ask already 422'd above, so what's left is a flag the credential is
        INHERITING across a switch to `custom`; clearing it means no stored
        row ever combines provider="custom" with classification_byok=true,
        which `resolve_classification_routing` would otherwise have to treat
        as "off" anyway.
        """
        if classification_byok_input is not None:
            value = classification_byok_input
        elif existing is not None:
            value = bool(existing.classification_byok)
        else:
            value = False
        return False if provider == "custom" else value

    # FOR UPDATE before the read-then-branch: two concurrent PUTs for the
    # same user otherwise both read the same revision and bump it only
    # once, weakening the id+revision guard /test relies on. The lock
    # serializes the second caller behind the first -- for an EXISTING row.
    row = db.execute(
        select(UserLlmCredential)
        .where(UserLlmCredential.user_id == current_user.id)
        .with_for_update()
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
        # Read BEFORE mutating -- both `_resolve_classification_byok` and
        # `material_changed` need `target`'s state as it stood before this
        # write. A flag-only edit (no new key, same provider/base_url/model)
        # must NOT clear last_verified_at -- that'd be a confusing "your
        # verified key just went unverified" regression for a save that
        # didn't touch the credential itself.
        material_changed = (
            api_key_provided
            or target.provider != provider
            or target.base_url != base_url
            or target.model != model
        )
        target.classification_byok = _resolve_classification_byok(target)
        target.provider = provider
        target.base_url = base_url
        target.api_key = effective_api_key
        target.model = model
        target.revision += 1
        if material_changed:
            target.last_verified_at = None
        target.updated_at = now

    if row is None:
        # FOR UPDATE takes NO lock when the SELECT matches zero rows --
        # Postgres only locks rows it actually returns -- so for a
        # brand-new user the unique constraint (`uq_llm_credential_user`),
        # not the lock, is what serializes two concurrent first-time PUTs.
        # The loser's INSERT raises IntegrityError here; roll back, re-read
        # the winner's now-committed row WITH the lock (it's lockable now
        # that it exists), and apply the normal update path against it --
        # last writer wins, exactly as two sequential saves would behave.
        row = UserLlmCredential(
            user_id=current_user.id,
            provider=provider,
            base_url=base_url,
            api_key=effective_api_key,
            model=model,
            classification_byok=_resolve_classification_byok(None),
            revision=1,
        )
        row.updated_at = now
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.execute(
                select(UserLlmCredential)
                .where(UserLlmCredential.user_id == current_user.id)
                .with_for_update()
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
    # validation, custom included) never blocked. Eligibility follows the
    # same rule `resolve_classification_routing` applies -- opted in AND a
    # preset provider -- and both are already guaranteed above (a `custom`
    # provider always leaves `classification_byok` false here).
    backend = _effective_backend()
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
        "classifier_uses_llm": backend != "heuristic",
        "classifier_backend": backend,
        "classification_eligible": (
            bool(row.classification_byok) and row.provider in PROVIDER_PRESETS
        ),
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
    row = db.execute(
        select(UserLlmCredential).where(UserLlmCredential.user_id == current_user.id)
    ).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()
    return Response(status_code=204)
