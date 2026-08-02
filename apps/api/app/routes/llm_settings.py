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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.ratelimit import user_rate_limit
from app.deps import get_current_user, get_db
from app.db.models import AppUser, UserLlmCredential
from app.db.schemas.llm_settings import LlmSettingsOut, LlmTestResultOut
from app.services.nlp.extractor import test_credential
from app.services.nlp.providers import (
    PROVIDER_PRESETS,
    DestinationRejected,
    ResolvedExtraction,
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


def _settings_payload(row: UserLlmCredential | None, resolved: ResolvedExtraction) -> dict:
    """The shared GET response shape. ``row`` carries every field that
    ``ResolvedExtraction`` doesn't (last_verified_at, the display fields) --
    ``resolved`` is still the ONE source for coverage-derived fields
    (``fallback_active``, ``custom_blocked``) so this never re-derives
    coverage from partial signals the way ``ResolvedExtraction``'s own
    docstring warns against.
    """
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
    return _settings_payload(row, resolved)


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

    if not isinstance(provider, str):
        raise HTTPException(status_code=422, detail="provider must be a string")
    if not isinstance(api_key, str):
        raise HTTPException(status_code=422, detail="api_key must be a string")
    if not isinstance(model, str):
        raise HTTPException(status_code=422, detail="model must be a string")

    api_key = api_key.strip()
    if not (_MIN_KEY_LEN <= len(api_key) <= _MAX_KEY_LEN):
        raise HTTPException(status_code=422, detail="api_key length out of bounds")

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

    row = db.execute(
        select(UserLlmCredential).where(UserLlmCredential.user_id == current_user.id)
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = UserLlmCredential(
            user_id=current_user.id,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            revision=1,
        )
        db.add(row)
    else:
        row.provider = provider
        row.base_url = base_url
        row.api_key = api_key
        row.model = model
        row.revision += 1
        row.last_verified_at = None
    row.updated_at = now
    db.commit()

    # Built directly from what we just wrote and validated, NOT a fresh
    # `resolve_extraction_credential` call: that helper's custom-provider
    # path calls the SYNC destination-policy re-check, which would block
    # this async route's event loop for up to its 3s DNS budget -- the exact
    # thing being async here is meant to avoid. Nothing here needs
    # re-deriving: a row we just wrote is always `configured`, never
    # fallback-covered, and (having just passed validation, custom included)
    # never blocked.
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
