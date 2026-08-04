"""Tests for the BYOK settings routes: GET/PUT/test/DELETE.

Offline, same fake-session idiom as test_actions_routes.py/test_mailbox.py --
no live DB. ``_CredentialDB`` is deliberately dumb: it resolves every
statement by inspecting the actual WHERE clause SQLAlchemy compiled (via
``literal_binds``), so an ownership/isolation assertion is proving what the
ROUTE wrote, not just trusting a single-row fixture. Provider-layer behavior
(destination policy, credential resolution itself) is test_providers.py's
job; here we monkeypatch those entry points and test only this router's own
contract: raw-body validation, ownership, the id+revision verify stamp, and
secret hygiene.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Update
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request as StarletteRequest

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.main import app
from app.routes import llm_settings
from app.services.nlp.providers import (
    ClassificationRouting,
    DestinationRejected,
    ResolvedExtraction,
)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _make_row(
    *,
    id=None,
    user_id=None,
    provider="openai",
    base_url="https://api.openai.com/v1",
    api_key="sk-stored-key-1234",
    model="gpt-4o-mini",
    revision=1,
    last_verified_at=None,
    classification_byok=False,
):
    return SimpleNamespace(
        id=id or uuid4(),
        user_id=user_id or uuid4(),
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        revision=revision,
        last_verified_at=last_verified_at,
        classification_byok=classification_byok,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _resolved(
    *,
    credential=None,
    source=None,
    stored=False,
    blocked_reason=None,
    revision=None,
    credential_id=None,
) -> ResolvedExtraction:
    return ResolvedExtraction(
        credential=credential,
        source=source,
        stored=stored,
        blocked_reason=blocked_reason,
        revision=revision,
        credential_id=credential_id,
    )


class _CredentialDB:
    """Keys rows by ``user_id.hex``; a SELECT is answered by parsing the
    literal ``user_id`` out of the compiled WHERE clause, and an UPDATE (the
    /test verify stamp) by parsing the literal ``id`` + ``revision`` pair --
    so ownership and the id+revision race guard are proven against what the
    route actually issued, not asserted by fiat."""

    def __init__(self, rows: dict[str, object] | None = None):
        self.rows: dict[str, object] = dict(rows or {})
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.statements: list[object] = []

    def execute(self, stmt):
        self.statements.append(stmt)
        result = MagicMock()
        if isinstance(stmt, Update):
            matched = self._match_update(stmt)
            if matched is not None:
                matched.last_verified_at = datetime.now(timezone.utc)
            result.rowcount = 1 if matched is not None else 0
            return result
        result.scalar_one_or_none.return_value = self._match_select(stmt)
        return result

    def _match_select(self, stmt):
        compiled = _compiled(stmt)
        for user_hex, row in self.rows.items():
            if f"user_llm_credential.user_id = '{user_hex}'" in compiled:
                return row
        return None

    def _match_update(self, stmt):
        compiled = _compiled(stmt)
        for row in self.rows.values():
            if (
                f"user_llm_credential.id = '{row.id.hex}'" in compiled
                and f"user_llm_credential.revision = {row.revision}" in compiled
            ):
                return row
        return None

    def add(self, row):
        self.added.append(row)
        self.rows[row.user_id.hex] = row

    def delete(self, row):
        self.deleted.append(row)
        self.rows = {k: v for k, v in self.rows.items() if v is not row}

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def user():
    return MagicMock(id=uuid4())


def _override(user, db):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _default_classification_routing(monkeypatch):
    """GET calls `resolve_classification_routing` directly (unlike PUT, which
    derives eligibility from the row it just wrote) -- default every test to
    mode="server" so classification_eligible defaults to False without every
    GET test having to know about routing. Tests asserting eligibility
    override this explicitly."""
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="server", credential=None),
    )


# ---------------------------------------------------------------------------
# Structural: sync/async split, no declared body parameter
# ---------------------------------------------------------------------------


def test_get_delete_test_routes_are_sync_and_put_is_the_lone_async_def():
    assert not inspect.iscoroutinefunction(llm_settings.get_llm_settings)
    assert not inspect.iscoroutinefunction(llm_settings.delete_llm_settings)
    assert not inspect.iscoroutinefunction(llm_settings.test_llm_settings)
    assert inspect.iscoroutinefunction(llm_settings.put_llm_settings)


def test_put_declares_no_body_parameter_of_any_kind():
    # No pydantic model, no Body(...), not even bytes -- only Request plus
    # the two shared dependencies. A declared body field would make FastAPI
    # parse/validate the body before this function ever runs.
    params = list(inspect.signature(llm_settings.put_llm_settings).parameters)
    assert params == ["request", "current_user", "db"]


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_unconfigured_returns_nulls_and_flags(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", False)
    monkeypatch.setattr(settings, "llm_private_endpoints_enabled", False)
    monkeypatch.setattr(settings, "classifier_backend", "local")
    # Pinned, not assumed: classification_llm_usable falls back to the operator
    # key, so a developer with GEMINI_API_KEY in their environment would flip
    # this expectation without touching the code under test.
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False, source=None),
    )

    resp = TestClient(app).get("/api/v1/settings/llm")
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": False,
        "provider": None,
        "model": None,
        "base_url": None,
        "key_suffix": None,
        "last_verified_at": None,
        "extraction_enabled": True,
        "fallback_active": False,
        "custom_endpoints_enabled": False,
        "private_endpoints_enabled": False,
        "custom_blocked": False,
        "classification_byok": False,
        "classifier_uses_llm": True,
        "classifier_backend": "local",
        "classification_eligible": False,
        "classification_llm_usable": False,
    }


def test_get_fallback_active_when_no_row_but_fallback_covers_the_user(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False, source="fallback"),
    )
    resp = TestClient(app).get("/api/v1/settings/llm")
    body = resp.json()
    assert body["configured"] is False
    assert body["fallback_active"] is True


def test_get_configured_row_masks_key_and_shows_suffix(monkeypatch, caplog, user):
    secret = "sk-super-secret-key-0001"
    row = _make_row(user_id=user.id, api_key=secret, provider="openai", model="gpt-4o-mini")
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )

    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        resp = TestClient(app).get("/api/v1/settings/llm")

    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == "openai"
    assert body["key_suffix"] == secret[-4:]
    assert secret not in resp.text
    assert secret not in caplog.text


def test_get_custom_blocked_reflects_resolver_blocked_reason(monkeypatch, user):
    row = _make_row(user_id=user.id, provider="custom", base_url="https://llm.internal/v1")
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source=None, blocked_reason="custom_disabled"),
    )
    resp = TestClient(app).get("/api/v1/settings/llm")
    body = resp.json()
    assert body["configured"] is True
    assert body["custom_blocked"] is True
    assert body["provider"] == "custom"


def test_get_statement_filters_by_current_users_id(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False),
    )
    TestClient(app).get("/api/v1/settings/llm")
    select_statement = db.statements[0]
    assert f"user_llm_credential.user_id = '{user.id.hex}'" in _compiled(select_statement)


def test_get_two_user_isolation(monkeypatch):
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())
    row_a = _make_row(user_id=user_a.id, provider="openai", model="model-a")
    row_b = _make_row(user_id=user_b.id, provider="gemini", model="model-b")
    db = _CredentialDB({user_a.id.hex: row_a, user_b.id.hex: row_b})
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )

    _override(user_a, db)
    resp_a = TestClient(app).get("/api/v1/settings/llm")
    assert resp_a.json()["provider"] == "openai"
    assert resp_a.json()["model"] == "model-a"

    _override(user_b, db)
    resp_b = TestClient(app).get("/api/v1/settings/llm")
    assert resp_b.json()["provider"] == "gemini"
    assert resp_b.json()["model"] == "model-b"


# ---------------------------------------------------------------------------
# GET -- classification BYOK fields
# ---------------------------------------------------------------------------


def test_get_classification_eligible_true_when_routing_says_user(monkeypatch, user):
    row = _make_row(user_id=user.id, provider="openai", classification_byok=True)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="user", credential=None),
    )
    resp = TestClient(app).get("/api/v1/settings/llm")
    body = resp.json()
    assert body["classification_byok"] is True
    assert body["classification_eligible"] is True


def test_get_llm_usable_true_on_operator_key_even_when_user_is_not_eligible(monkeypatch, user):
    """The whole reason this field exists apart from classification_eligible.
    Not eligible means "your key won't be used" -- but with an operator key
    configured the LLM path still runs (operator-paid), so a client that
    disabled the LLM option on eligibility alone would wrongly hide a working
    backend on every server-key deployment."""
    row = _make_row(user_id=user.id, provider="openai", classification_byok=False)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="server", credential=None),
    )
    monkeypatch.setattr(llm_settings.settings, "gemini_api_key", "operator-key")
    body = TestClient(app).get("/api/v1/settings/llm").json()
    assert body["classification_eligible"] is False
    assert body["classification_llm_usable"] is True


def test_get_llm_usable_false_when_neither_user_nor_operator_has_a_key(monkeypatch, user):
    """The reported case: picking the LLM backend for a backfill silently
    classified with keyword rules instead. Nothing to call, so the client has
    to be told the option is dead rather than let it no-op."""
    row = _make_row(user_id=user.id, provider="openai", classification_byok=False)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="server", credential=None),
    )
    monkeypatch.setattr(llm_settings.settings, "gemini_api_key", None)
    body = TestClient(app).get("/api/v1/settings/llm").json()
    assert body["classification_eligible"] is False
    assert body["classification_llm_usable"] is False


def test_get_classification_eligible_false_for_out_of_band_custom_opt_in_row(monkeypatch, user):
    # A row can end up provider="custom" + classification_byok=True only
    # out-of-band (the PUT route force-clears the flag on that same write) --
    # simulate it directly and confirm the router's "off" verdict (tested in
    # test_providers.py) surfaces here as NOT eligible, even though the flag
    # itself is still stored as true.
    row = _make_row(
        user_id=user.id, provider="custom", base_url="https://llm.internal/v1",
        classification_byok=True,
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="off", credential=None),
    )
    resp = TestClient(app).get("/api/v1/settings/llm")
    body = resp.json()
    assert body["classification_byok"] is True
    assert body["classification_eligible"] is False


def test_get_llm_usable_false_for_off_routing_even_with_an_operator_key(monkeypatch, user):
    """The shipped bug (PR #18), fixed: `routing_is_user or bool(gemini_api_key)`
    reported True for `mode="off"` whenever an operator key existed --
    `classifier.py` returns heuristics for `off` before either key is even
    considered, so the UI offered an LLM option that silently ran keyword
    rules. `mode="off"` must report False regardless of the operator key.
    """
    row = _make_row(
        user_id=user.id, provider="custom", base_url="https://llm.internal/v1",
        classification_byok=True,
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, source="user"),
    )
    monkeypatch.setattr(
        llm_settings, "resolve_classification_routing",
        lambda db_, uid: ClassificationRouting(mode="off", credential=None),
    )
    monkeypatch.setattr(llm_settings.settings, "gemini_api_key", "operator-key")
    body = TestClient(app).get("/api/v1/settings/llm").json()
    assert body["classification_llm_usable"] is False


@pytest.mark.parametrize(
    "raw_backend, expected_backend, expected_uses_llm",
    [
        ("HEURISTIC", "heuristic", False),
        ("", "auto", True),
        ("Auto", "auto", True),
        ("gemini", "gemini", True),
    ],
    ids=["uppercase-heuristic", "empty", "mixed-case-auto", "gemini"],
)
def test_get_classifier_backend_fields_use_the_normalized_expression(
    monkeypatch, user, raw_backend, expected_backend, expected_uses_llm,
):
    # Must be the SAME normalization `classify()` applies -- comparing the
    # raw config string would misreport an uppercase or empty value.
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "classifier_backend", raw_backend)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False),
    )
    resp = TestClient(app).get("/api/v1/settings/llm")
    body = resp.json()
    assert body["classifier_backend"] == expected_backend
    assert body["classifier_uses_llm"] is expected_uses_llm


# ---------------------------------------------------------------------------
# GET /usage
# ---------------------------------------------------------------------------
#
# The route issues four separate aggregate queries (totals, by_stage,
# by_provider, daily) -- one per SQL GROUP BY dimension, per plan §6 ("aggregate
# in SQL, not in Python"). `_usage_db` stubs `db.execute` with one canned
# result per call, in that fixed order, so these tests exercise the route's
# own assembly/casting logic (Decimal -> int, empty-state zeroing, window
# math) without a live DB -- the actual SUM/GROUP BY correctness is Postgres's
# job, proven by the live migration round-trip, not this offline suite's.


def _usage_db(*, totals, by_stage, by_provider, daily):
    """Sequences four `db.execute(...)` results in the exact call order
    `get_llm_usage` issues them: totals via `.one()`, then by_stage/
    by_provider/daily via `.all()`.
    """
    results = []
    for method, value in (
        ("one", totals),
        ("all", by_stage),
        ("all", by_provider),
        ("all", daily),
    ):
        result = MagicMock()
        setattr(result, method, MagicMock(return_value=value))
        results.append(result)
    db = MagicMock()
    db.execute = MagicMock(side_effect=results)
    return db


_ZERO_COUNTERS = (0, 0, 0, 0, 0)
_ZERO_TOTALS = {
    "calls": 0,
    "calls_with_total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def test_usage_empty_state_is_zeroed_lists_and_200_not_404(user):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/usage")
    assert resp.status_code == 200
    assert resp.json() == {
        "window_days": 30,
        "totals": _ZERO_TOTALS,
        "by_stage": [],
        "by_provider": [],
        "daily": [],
    }


def test_usage_totals_and_splits_assemble_from_the_sql_aggregates(user):
    db = _usage_db(
        totals=(Decimal(12), Decimal(9), Decimal(1000), Decimal(500), Decimal(1500)),
        by_stage=[
            ("classification", Decimal(10), Decimal(8), Decimal(800), Decimal(400), Decimal(1200)),
            ("extraction", Decimal(2), Decimal(1), Decimal(200), Decimal(100), Decimal(300)),
        ],
        by_provider=[
            ("openai", Decimal(12), Decimal(9), Decimal(1000), Decimal(500), Decimal(1500)),
        ],
        daily=[
            (date(2026, 7, 30), Decimal(5), Decimal(700)),
            (date(2026, 7, 31), Decimal(7), Decimal(800)),
        ],
    )
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/usage?days=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 2
    assert body["totals"] == {
        "calls": 12,
        "calls_with_total_tokens": 9,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
    }
    assert body["by_stage"] == [
        {
            "stage": "classification",
            "calls": 10,
            "calls_with_total_tokens": 8,
            "prompt_tokens": 800,
            "completion_tokens": 400,
            "total_tokens": 1200,
        },
        {
            "stage": "extraction",
            "calls": 2,
            "calls_with_total_tokens": 1,
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
        },
    ]
    assert body["by_provider"] == [
        {
            "provider": "openai",
            "calls": 12,
            "calls_with_total_tokens": 9,
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
        },
    ]
    assert body["daily"] == [
        {"date": "2026-07-30", "calls": 5, "total_tokens": 700},
        {"date": "2026-07-31", "calls": 7, "total_tokens": 800},
    ]


def test_usage_route_casts_decimal_sums_to_real_python_ints(user):
    # Calling the route function directly (same idiom as
    # `mailbox._assemble_triage_items` in test_mailbox.py) since a JSON
    # round-trip can't distinguish a whole Decimal from an int -- this proves
    # the ROUTE itself, not just its serialized output, never hands a
    # Decimal onward.
    db = _usage_db(
        totals=(Decimal(3), Decimal(2), Decimal(30), Decimal(20), Decimal(50)),
        by_stage=[], by_provider=[], daily=[],
    )
    result = llm_settings.get_llm_usage(days=30, current_user=user, db=db)
    for value in result["totals"].values():
        assert type(value) is int


@pytest.mark.parametrize("days", [1, 400])
def test_usage_days_bounds_accepted(user, days):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    resp = TestClient(app).get(f"/api/v1/settings/llm/usage?days={days}")
    assert resp.status_code == 200
    assert resp.json()["window_days"] == days


@pytest.mark.parametrize("days", [0, 401])
def test_usage_days_out_of_range_is_422(user, days):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    resp = TestClient(app).get(f"/api/v1/settings/llm/usage?days={days}")
    assert resp.status_code == 422
    assert db.execute.call_count == 0  # rejected before any query is issued


def test_usage_non_integer_days_is_422(user):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/usage?days=not-a-number")
    assert resp.status_code == 422
    assert db.execute.call_count == 0


def test_usage_days_defaults_to_thirty(user):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/usage")
    assert resp.status_code == 200
    assert resp.json()["window_days"] == 30


def test_usage_window_boundary_days_minus_one_apart(user):
    """`days=N` must cut off exactly one calendar day earlier than
    `days=N+1` -- the plan's `usage_date >= today - (days - 1)` convention.
    Proven by reading the literal cutoff each call's totals query compiled,
    not by asserting against a frozen "today" (this suite doesn't stub
    `datetime.now`), so the assertion holds regardless of when the suite
    runs.
    """
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    TestClient(app).get("/api/v1/settings/llm/usage?days=5")
    cutoff_5 = _compiled(db.execute.call_args_list[0].args[0])

    db2 = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db2)
    TestClient(app).get("/api/v1/settings/llm/usage?days=6")
    cutoff_6 = _compiled(db2.execute.call_args_list[0].args[0])

    def _extract_date(compiled: str) -> date:
        marker = "usage_date >= '"
        start = compiled.index(marker) + len(marker)
        end = compiled.index("'", start)
        return date.fromisoformat(compiled[start:end])

    assert _extract_date(cutoff_5) - _extract_date(cutoff_6) == timedelta(days=1)


def test_usage_days_one_means_today_only(user):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    TestClient(app).get("/api/v1/settings/llm/usage?days=1")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    today = datetime.now(timezone.utc).date()
    assert f"usage_date >= '{today.isoformat()}'" in compiled


def test_usage_statement_filters_by_current_users_id_first(user):
    db = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user, db)
    TestClient(app).get("/api/v1/settings/llm/usage")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    assert f"llm_usage_daily.user_id = '{user.id.hex}'" in compiled


def test_usage_two_user_isolation_each_gets_their_own_scoped_query(monkeypatch):
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())

    db_a = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user_a, db_a)
    TestClient(app).get("/api/v1/settings/llm/usage")
    compiled_a = _compiled(db_a.execute.call_args_list[0].args[0])
    assert f"llm_usage_daily.user_id = '{user_a.id.hex}'" in compiled_a
    assert user_b.id.hex not in compiled_a

    db_b = _usage_db(totals=_ZERO_COUNTERS, by_stage=[], by_provider=[], daily=[])
    _override(user_b, db_b)
    TestClient(app).get("/api/v1/settings/llm/usage")
    compiled_b = _compiled(db_b.execute.call_args_list[0].args[0])
    assert f"llm_usage_daily.user_id = '{user_b.id.hex}'" in compiled_b
    assert user_a.id.hex not in compiled_b


# ---------------------------------------------------------------------------
# PUT -- raw-body reading and bounds
# ---------------------------------------------------------------------------


def test_put_oversized_content_length_is_413_without_reading(monkeypatch, user):
    # json.loads is the process-global stdlib function -- monkeypatching it
    # to just record calls (rather than delegate through) would also break
    # httpx's own Response.json() used below to inspect the body, since
    # that's the SAME module object. Spy-and-delegate instead, and check the
    # call count BEFORE touching resp.json() so that later, legitimate call
    # doesn't get counted as if the route had parsed something.
    db = _CredentialDB()
    _override(user, db)
    real_loads = json.loads
    parse_calls = []

    def _spy(*args, **kwargs):
        parse_calls.append(1)
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(llm_settings.json, "loads", _spy)
    payload = json.dumps(
        {"provider": "openai", "api_key": "sk-secret-1234", "model": "m"}
    ).encode()
    big_body = payload + b" " * max(0, 9000 - len(payload))

    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        content=big_body,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert parse_calls == []
    assert resp.json() == {"detail": "request body too large"}
    assert "sk-secret-1234" not in resp.text
    assert db.commits == 0


def test_read_bounded_body_rejects_an_oversized_streamed_body_with_no_content_length():
    chunks = [b"a" * 5000, b"b" * 5000]

    async def _run():
        remaining = list(chunks)

        async def receive():
            if remaining:
                chunk = remaining.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": bool(remaining)}
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "headers": [], "method": "PUT", "path": "/"}
        request = StarletteRequest(scope, receive=receive)
        with pytest.raises(HTTPException) as exc_info:
            await llm_settings._read_bounded_body(request)
        assert exc_info.value.status_code == 413
        assert exc_info.value.detail == "request body too large"

    asyncio.run(_run())


def test_put_malformed_json_is_422_and_never_echoes_the_body(caplog, user):
    db = _CredentialDB()
    _override(user, db)
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        resp = TestClient(app).put(
            "/api/v1/settings/llm",
            content=b'{"api_key": "sk-secret-1234", not valid json',
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "malformed request body"}
    assert "sk-secret-1234" not in resp.text
    assert "sk-secret-1234" not in caplog.text
    assert db.commits == 0


def test_put_non_utf8_body_is_422_and_never_echoes_the_body(caplog, user):
    db = _CredentialDB()
    _override(user, db)
    body = b"\xff\xfe" + b'{"api_key": "sk-secret-1234"}'
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        resp = TestClient(app).put(
            "/api/v1/settings/llm",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "malformed request body"}
    assert "sk-secret-1234" not in resp.text
    assert "sk-secret-1234" not in caplog.text


def test_put_body_must_be_a_json_object(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put("/api/v1/settings/llm", json=["not", "an", "object"])
    assert resp.status_code == 422
    assert resp.json() == {"detail": "request body must be a JSON object"}


# ---------------------------------------------------------------------------
# PUT -- manual field validation (fixed details, never echoing input)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_api_key", [["sk-real-key"], {"key": "sk-real-key"}, 12345, None])
def test_put_non_string_api_key_is_422_with_a_fixed_detail(user, bad_api_key):
    # application/json content-type + a type-invalid api_key: this is the
    # exact shape a pre-handler pydantic parse would have failed on with a
    # default validation-error body echoing the value. Our fixed, single-
    # string detail proves that never happened.
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": bad_api_key, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "api_key must be a string"}
    assert "sk-real-key" not in resp.text
    assert db.commits == 0


def test_put_non_string_provider_is_422(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": ["openai"], "api_key": "sk-abcdefgh", "model": "m"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "provider must be a string"}


def test_put_non_string_model_is_422(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": 123},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "model must be a string"}


@pytest.mark.parametrize("bad_key", ["short12", "s" * 513])
def test_put_api_key_length_out_of_bounds_is_422(user, bad_key):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": bad_key, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "api_key length out of bounds"}
    assert bad_key not in resp.text
    assert db.commits == 0


@pytest.mark.parametrize(
    "bad_model", ["", "   ", "m" * 201], ids=["empty", "whitespace-only", "over-long"]
)
def test_put_model_length_out_of_bounds_is_422(user, bad_model):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": bad_model},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "model length out of bounds"}
    assert db.commits == 0


def test_put_model_is_stripped_before_the_bound_check_and_storage(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": "  gpt-4o-mini  "},
    )
    assert resp.status_code == 200
    assert db.added[0].model == "gpt-4o-mini"


def test_put_api_key_is_stripped_before_the_bound_check_and_storage(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "  sk-abcdefgh  ", "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 200
    assert db.added[0].api_key == "sk-abcdefgh"


def test_put_unknown_provider_is_422(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "unknown-llm", "api_key": "sk-abcdefgh", "model": "m"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid provider"}
    assert db.commits == 0


def test_put_preset_ignores_caller_base_url_and_saves(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "api_key": "sk-abcdefgh",
            "model": "gpt-4o-mini",
            "base_url": "https://evil.example/v1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_url"] == "https://api.openai.com/v1"
    assert body["configured"] is True
    assert body["key_suffix"] == "efgh"
    assert body["fallback_active"] is False
    assert body["custom_blocked"] is False
    assert db.commits == 1
    saved = db.added[0]
    assert saved.base_url == "https://api.openai.com/v1"
    assert saved.api_key == "sk-abcdefgh"
    assert saved.revision == 1


def test_put_custom_provider_422_when_flag_off(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", False)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://llm.example/v1",
        },
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "custom endpoints are disabled"}
    assert db.commits == 0


def test_put_custom_provider_base_url_must_be_a_string(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "custom", "api_key": "sk-abcdefgh", "model": "m", "base_url": 123},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "base_url must be a string"}


def test_put_custom_provider_422_when_destination_rejected(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)

    async def _reject(url):
        raise DestinationRejected("destination_rejected", "internal reason, never shown")

    monkeypatch.setattr(llm_settings, "validate_custom_base_url_async", _reject)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://llm.example/v1",
        },
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "base_url is not an allowed destination"}
    assert "internal reason" not in resp.text
    assert db.commits == 0


def test_put_custom_provider_stores_the_normalized_base_url(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)

    async def _normalize(url):
        return "https://llm.example.internal:8080"

    monkeypatch.setattr(llm_settings, "validate_custom_base_url_async", _normalize)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://LLM.example.internal:8080/",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "https://llm.example.internal:8080"
    assert db.added[0].base_url == "https://llm.example.internal:8080"


# ---------------------------------------------------------------------------
# PUT -- classification_byok validation, the 422 matrix, and force-clear
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value", [1, 0, "true", "false", ["true"], {"on": True}],
    ids=["one", "zero", "str-true", "str-false", "list", "dict"],
)
def test_put_classification_byok_non_bool_is_422(user, bad_value):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "classification_byok": bad_value,
        },
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "classification_byok must be a boolean"}
    assert db.commits == 0


def test_put_classification_byok_true_with_custom_provider_is_422(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)

    async def _normalize(url):
        return "https://llm.example/v1"

    monkeypatch.setattr(llm_settings, "validate_custom_base_url_async", _normalize)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://llm.example/v1",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 422
    assert resp.json() == {
        "detail": "classification_byok requires a preset provider; custom endpoints "
        "are not eligible for classification in v1",
    }
    assert db.commits == 0


def test_put_classification_byok_false_with_custom_provider_is_not_rejected(monkeypatch, user):
    # Only an explicit `true` conflicts with `custom` -- an explicit `false`
    # (or absent, tested separately) must never trip the same 422.
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)

    async def _normalize(url):
        return "https://llm.example/v1"

    monkeypatch.setattr(llm_settings, "validate_custom_base_url_async", _normalize)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://llm.example/v1",
            "classification_byok": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["classification_byok"] is False


def test_put_classification_byok_absent_defaults_false_on_create(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": "m"},
    )
    assert resp.status_code == 200
    assert resp.json()["classification_byok"] is False
    assert resp.json()["classification_eligible"] is False
    assert db.added[0].classification_byok is False


def test_put_classification_byok_true_on_create_with_preset_provider(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["classification_byok"] is True
    assert resp.json()["classification_eligible"] is True
    assert db.added[0].classification_byok is True


def test_put_classification_byok_absent_is_unchanged_on_update(user):
    row = _make_row(user_id=user.id, provider="openai", classification_byok=True)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-new-key-1234", "model": "new-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["classification_byok"] is True
    assert resp.json()["classification_eligible"] is True
    assert row.classification_byok is True


def test_put_switching_to_custom_force_clears_classification_byok(monkeypatch, user):
    """Switching an existing opted-in credential TO custom must not 422 --
    the flag it's inheriting is silently cleared in the same write, so no
    stored row ever combines provider="custom" with classification_byok=true."""
    row = _make_row(user_id=user.id, provider="openai", classification_byok=True)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "llm_custom_endpoints_enabled", True)

    async def _normalize(url):
        return "https://llm.internal:8080"

    monkeypatch.setattr(llm_settings, "validate_custom_base_url_async", _normalize)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "custom",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "base_url": "https://llm.internal:8080",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["classification_byok"] is False
    assert body["classification_eligible"] is False
    assert row.classification_byok is False
    assert db.commits == 1


def test_put_response_carries_every_new_field_on_create(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "classifier_backend", "HEURISTIC")
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "api_key": "sk-abcdefgh",
            "model": "m",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["classification_byok"] is True
    assert body["classifier_uses_llm"] is False
    assert body["classifier_backend"] == "heuristic"
    # A heuristic-only backend has no LLM path for this opt-in to pay for --
    # `classification_eligible` is gated on `classifier_uses_llm` (plan §6),
    # so an opted-in preset credential still isn't eligible here even though
    # routing itself would resolve to "user".
    assert body["classification_eligible"] is False


def test_put_response_carries_every_new_field_on_update(monkeypatch, user):
    row = _make_row(user_id=user.id, provider="gemini", classification_byok=False)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "classifier_backend", "")
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "gemini",
            "api_key": "sk-new-key-1234",
            "model": "new-model",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["classification_byok"] is True
    assert body["classifier_uses_llm"] is True
    assert body["classifier_backend"] == "auto"
    assert body["classification_eligible"] is True
    assert row.classification_byok is True


# ---------------------------------------------------------------------------
# PUT -- absent api_key preserves the stored credential (P2 fix: a caller who
# no longer has the raw key must still be able to edit anything else,
# including revoking the classification opt-in)
# ---------------------------------------------------------------------------


def test_put_absent_api_key_on_update_preserves_the_key_and_applies_the_flag_change(user):
    secret = "sk-stored-key-1234"
    row = _make_row(
        user_id=user.id, provider="openai", api_key=secret, classification_byok=False,
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["classification_byok"] is True
    assert body["key_suffix"] == secret[-4:]
    assert row.api_key == secret
    assert row.classification_byok is True
    assert secret not in resp.text


def test_put_omitted_api_key_key_on_create_is_422(user):
    # Not just `api_key: None` -- the field is missing from the body
    # entirely, same as a real client that never sends it.
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "api_key must be a string"}
    assert db.commits == 0
    assert db.added == []


def test_put_explicit_null_api_key_on_update_preserves_the_key(user):
    # An explicit JSON `null` takes the same "absent" path as an omitted
    # field -- api_key_provided is `api_key is not None`, so null never
    # reaches the type/length checks.
    secret = "sk-stored-key-1234"
    row = _make_row(user_id=user.id, provider="openai", api_key=secret)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": None, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 200
    assert resp.json()["key_suffix"] == secret[-4:]
    assert row.api_key == secret


def test_put_explicit_null_api_key_on_create_is_422(user):
    # Same absent-path treatment on a create, where there's nothing stored
    # to fall back on -- still 422s, same as an omitted field.
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": None, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "api_key must be a string"}
    assert db.commits == 0
    assert db.added == []


@pytest.mark.parametrize("bad_key", ["", "short12", "s" * 513], ids=["empty", "short", "long"])
def test_put_present_but_invalid_api_key_still_422s_on_update(user, bad_key):
    # An explicit (even empty) api_key is never treated as "absent" -- this
    # isn't a way to blank a stored key by accident.
    row = _make_row(user_id=user.id, provider="openai", api_key="sk-stored-key-1234")
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": bad_key, "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "api_key length out of bounds"}
    assert row.api_key == "sk-stored-key-1234"
    assert db.commits == 0


def test_put_flag_only_edit_leaves_last_verified_at_untouched(user):
    verified_at = datetime.now(timezone.utc)
    row = _make_row(
        user_id=user.id,
        provider="openai",
        model="gpt-4o-mini",
        revision=3,
        last_verified_at=verified_at,
        classification_byok=False,
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_verified_at"] is not None
    assert row.last_verified_at == verified_at
    assert row.revision == 4
    assert row.classification_byok is True


def test_put_new_api_key_clears_last_verified_at_even_with_no_other_change(user):
    row = _make_row(
        user_id=user.id,
        provider="openai",
        model="gpt-4o-mini",
        revision=1,
        last_verified_at=datetime.now(timezone.utc),
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "api_key": "sk-brand-new-key-1234",
            "model": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["last_verified_at"] is None
    assert row.last_verified_at is None
    assert row.revision == 2


def test_put_model_change_clears_last_verified_at_even_when_api_key_is_absent(user):
    row = _make_row(
        user_id=user.id,
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-stored-key-1234",
        revision=1,
        last_verified_at=datetime.now(timezone.utc),
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "model": "a-different-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["last_verified_at"] is None
    assert row.last_verified_at is None
    assert row.revision == 2
    # The key itself was still preserved -- only the model changed.
    assert row.api_key == "sk-stored-key-1234"


def test_put_revision_increments_on_a_flag_only_edit_same_as_any_other_write(user):
    row = _make_row(user_id=user.id, provider="openai", model="gpt-4o-mini", revision=5)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "classification_byok": True,
        },
    )
    assert resp.status_code == 200
    assert row.revision == 6


# ---------------------------------------------------------------------------
# PUT -- concurrency: FOR UPDATE before the read-then-branch
# ---------------------------------------------------------------------------


def test_put_select_issues_for_update_before_the_insert_or_update_branch(user):
    """Every PUT locks the row (if any) before branching -- two concurrent
    PUTs against an EXISTING row would otherwise both read the same
    revision and bump it only once. FOR UPDATE takes no lock when this
    SELECT matches zero rows (see the IntegrityError-fallback test below for
    how the very-first-save race is closed separately)."""
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": "gpt-4o-mini"},
    )
    assert resp.status_code == 200
    select_statement = db.statements[0]
    compiled = _compiled(select_statement)
    assert "FOR UPDATE" in compiled
    assert f"user_llm_credential.user_id = '{user.id.hex}'" in compiled


def test_put_concurrent_first_save_falls_back_to_update_after_integrity_error(user):
    """FOR UPDATE takes no lock when the SELECT matches zero rows, so for a
    brand-new user the unique constraint (`uq_llm_credential_user`) is the
    only thing serializing two concurrent first-time PUTs -- the loser's own
    INSERT raises IntegrityError once the winner has committed. The route
    must roll back, re-read the winner's now-committed row, and apply the
    normal update path against it instead of 500ing: last writer wins,
    exactly as two sequential saves would behave."""
    db = _CredentialDB()
    _override(user, db)
    winner_row = _make_row(
        user_id=user.id,
        provider="gemini",
        model="winner-model",
        revision=1,
        last_verified_at=datetime.now(timezone.utc),
    )

    commit_calls = []
    real_commit = db.commit

    def _commit_raises_once():
        commit_calls.append(1)
        if len(commit_calls) == 1:
            raise IntegrityError("insert", {}, Exception("uq_llm_credential_user"))
        real_commit()

    def _rollback_reveals_the_winner():
        # A real rollback wouldn't conjure a row -- it undoes OUR failed
        # insert. What it exposes on the next read is whatever the OTHER
        # session already committed, which is this winner row.
        db.rows.pop(user.id.hex, None)
        db.rows[user.id.hex] = winner_row

    db.commit = _commit_raises_once
    db.rollback = _rollback_reveals_the_winner

    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-new-key-1234", "model": "new-model"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == "openai"
    assert body["model"] == "new-model"
    assert body["last_verified_at"] is None
    # The update path ran against the WINNER's row -- not a second insert.
    assert winner_row.provider == "openai"
    assert winner_row.model == "new-model"
    assert winner_row.revision == 2
    assert winner_row.last_verified_at is None
    assert len(commit_calls) == 2


def test_put_first_save_race_reraises_if_winner_row_vanished(user):
    """If the re-read after the IntegrityError somehow finds no row (e.g. a
    racing DELETE), the route re-raises rather than looping -- one retry
    only, never an unbounded retry. The app's own IntegrityError handler
    then turns that into a 409 (a real constraint conflict), not a 500."""
    db = _CredentialDB()
    _override(user, db)

    def _commit_always_raises():
        raise IntegrityError("insert", {}, Exception("uq_llm_credential_user"))

    db.commit = _commit_always_raises
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-new-key-1234", "model": "new-model"},
    )
    assert resp.status_code == 409
    assert "sk-new-key-1234" not in resp.text


# ---------------------------------------------------------------------------
# PUT -- upsert semantics + ownership
# ---------------------------------------------------------------------------


def test_put_upserts_existing_row_bumps_revision_and_clears_verified_at(user):
    row = _make_row(
        user_id=user.id,
        revision=3,
        last_verified_at=datetime.now(timezone.utc),
        provider="gemini",
        model="old-model",
    )
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "gemini", "api_key": "sk-new-key-1234", "model": "new-model"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_verified_at"] is None
    assert body["model"] == "new-model"
    assert row.revision == 4
    assert row.last_verified_at is None
    assert db.commits == 1
    assert db.added == []  # updated in place, not re-added as a second row


def test_put_two_user_isolation_never_touches_the_other_users_row():
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())
    row_b = _make_row(user_id=user_b.id, provider="gemini", model="untouched")
    db = _CredentialDB({user_b.id.hex: row_b})
    _override(user_a, db)

    resp = TestClient(app).put(
        "/api/v1/settings/llm",
        json={"provider": "openai", "api_key": "sk-abcdefgh", "model": "new-model"},
    )
    assert resp.status_code == 200
    assert row_b.provider == "gemini"
    assert row_b.model == "untouched"
    assert db.rows[user_a.id.hex].provider == "openai"


# ---------------------------------------------------------------------------
# PUT -- unexpected exception never leaks the key
# ---------------------------------------------------------------------------


def test_put_unexpected_exception_never_leaks_the_key(monkeypatch, caplog, user):
    db = _CredentialDB()
    _override(user, db)

    def _boom(provider):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(llm_settings, "resolve_preset_base_url", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="ai-mailbox"):
        resp = client.put(
            "/api/v1/settings/llm",
            json={"provider": "openai", "api_key": "sk-secret-1234", "model": "gpt-4o-mini"},
        )
    assert resp.status_code == 500
    assert "sk-secret-1234" not in resp.text
    assert "sk-secret-1234" not in caplog.text


# ---------------------------------------------------------------------------
# POST /test
# ---------------------------------------------------------------------------


def test_test_returns_409_when_master_switch_off(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", False)
    called = []
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: called.append(1),
    )
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "action extraction disabled"
    assert called == []  # never even resolves once the switch is off


def test_test_returns_409_when_only_fallback_covers_the_user(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False, source="fallback"),
    )
    called = []
    monkeypatch.setattr(
        llm_settings, "test_credential", lambda cred: called.append(cred) or (True, None, 1)
    )
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no LLM credential configured"
    assert called == []  # fallback coverage never makes the operator's key testable


def test_test_returns_blocked_by_policy_for_a_blocked_stored_row(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=True, blocked_reason="destination_rejected"),
    )
    called = []
    monkeypatch.setattr(
        llm_settings, "test_credential", lambda cred: called.append(cred) or (True, None, 1)
    )
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "latency_ms": 0, "error": "blocked_by_policy"}
    assert called == []


def test_test_success_stamps_last_verified_at_via_id_and_revision(monkeypatch, caplog, user):
    secret = "sk-stored-secret-777"
    row = _make_row(user_id=user.id, revision=2, api_key=secret)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    credential = SimpleNamespace(
        provider=row.provider, base_url=row.base_url, api_key=row.api_key, model=row.model
    )
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=credential,
            revision=row.revision, credential_id=row.id,
        ),
    )
    monkeypatch.setattr(llm_settings, "test_credential", lambda cred: (True, None, 123))

    assert row.last_verified_at is None
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "latency_ms": 123, "error": None}
    assert row.last_verified_at is not None
    assert secret not in resp.text
    assert secret not in caplog.text


def test_test_failure_never_stamps_and_reports_the_category(monkeypatch, user):
    row = _make_row(user_id=user.id, revision=1)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=SimpleNamespace(),
            revision=row.revision, credential_id=row.id,
        ),
    )
    monkeypatch.setattr(llm_settings, "test_credential", lambda cred: (False, "http_500", 45))

    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "latency_ms": 45, "error": "http_500"}
    assert row.last_verified_at is None


def test_test_put_during_test_race_never_stamps_the_stale_revision(monkeypatch, user):
    row = _make_row(user_id=user.id, revision=1)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=SimpleNamespace(),
            revision=1, credential_id=row.id,
        ),
    )

    def _slow_test(cred):
        # A concurrent PUT lands mid-test and bumps the revision.
        row.revision = 2
        return True, None, 10

    monkeypatch.setattr(llm_settings, "test_credential", _slow_test)
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The stamp's WHERE clause captured revision=1 at resolve time; the row
    # is now revision=2, so the conditional UPDATE matches zero rows.
    assert row.last_verified_at is None


def test_test_delete_then_recreate_during_test_race_never_stamps_the_new_row(monkeypatch, user):
    old_row = _make_row(user_id=user.id, revision=1)
    new_row = _make_row(user_id=user.id, revision=1)  # different id, same starting revision
    db = _CredentialDB({user.id.hex: old_row})
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=SimpleNamespace(),
            revision=1, credential_id=old_row.id,
        ),
    )

    def _slow_test(cred):
        # DELETE + PUT lands mid-test: an entirely new row (new id) replaces
        # the old one, also starting at revision 1 -- revision alone
        # couldn't catch this ABA; the id predicate is what does.
        db.rows[user.id.hex] = new_row
        return True, None, 10

    monkeypatch.setattr(llm_settings, "test_credential", _slow_test)
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert old_row.last_verified_at is None
    assert new_row.last_verified_at is None


def test_test_two_user_isolation_stamp_never_crosses_users(monkeypatch):
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())
    row_a = _make_row(user_id=user_a.id, revision=1)
    row_b = _make_row(user_id=user_b.id, revision=1)
    db = _CredentialDB({user_a.id.hex: row_a, user_b.id.hex: row_b})
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=SimpleNamespace(),
            revision=row_a.revision, credential_id=row_a.id,
        ),
    )
    monkeypatch.setattr(llm_settings, "test_credential", lambda cred: (True, None, 5))

    _override(user_a, db)
    resp = TestClient(app).post("/api/v1/settings/llm/test")
    assert resp.status_code == 200
    assert row_a.last_verified_at is not None
    assert row_b.last_verified_at is None


def test_test_is_rate_limited_at_five_per_window(monkeypatch, user):
    db = _CredentialDB()
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(stored=False, source="fallback"),
    )
    client = TestClient(app)
    for _ in range(5):
        resp = client.post("/api/v1/settings/llm/test")
        assert resp.status_code == 409
    resp = client.post("/api/v1/settings/llm/test")
    assert resp.status_code == 429


def test_test_unexpected_exception_never_leaks_the_key(monkeypatch, caplog, user):
    secret = "sk-secret-9999"
    row = _make_row(user_id=user.id, api_key=secret, revision=1)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    monkeypatch.setattr(settings, "action_extraction_enabled", True)
    credential = SimpleNamespace(
        provider=row.provider, base_url=row.base_url, api_key=row.api_key, model=row.model
    )
    monkeypatch.setattr(
        llm_settings, "resolve_extraction_credential",
        lambda db_, uid: _resolved(
            stored=True, source="user", credential=credential,
            revision=row.revision, credential_id=row.id,
        ),
    )

    def _boom(cred):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(llm_settings, "test_credential", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="ai-mailbox"):
        resp = client.post("/api/v1/settings/llm/test")
    assert resp.status_code == 500
    assert secret not in resp.text
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_removes_the_callers_row(user):
    row = _make_row(user_id=user.id)
    db = _CredentialDB({user.id.hex: row})
    _override(user, db)
    resp = TestClient(app).delete("/api/v1/settings/llm")
    assert resp.status_code == 204
    assert resp.content == b""
    assert user.id.hex not in db.rows
    assert db.commits == 1


def test_delete_when_unconfigured_is_still_204(user):
    db = _CredentialDB()
    _override(user, db)
    resp = TestClient(app).delete("/api/v1/settings/llm")
    assert resp.status_code == 204
    assert db.commits == 0


def test_delete_two_user_isolation():
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())
    row_a = _make_row(user_id=user_a.id)
    row_b = _make_row(user_id=user_b.id)
    db = _CredentialDB({user_a.id.hex: row_a, user_b.id.hex: row_b})
    _override(user_a, db)

    resp = TestClient(app).delete("/api/v1/settings/llm")
    assert resp.status_code == 204
    assert user_a.id.hex not in db.rows
    assert user_b.id.hex in db.rows


# ---------------------------------------------------------------------------
# GET /classifier-mix -- plan §7 + §5.1 (cases 15-24)
#
# Same offline idiom as `_usage_db`: `db.execute(...)` is stubbed with one
# canned `(model_version, count)` row set, since the actual GROUP BY
# correctness is Postgres's job (proven live), not this suite's. What these
# tests own is the route's OWN contract: the server-side model_version ->
# kind mapping, summing after mapping (not before), auth, and per-user
# scoping.
# ---------------------------------------------------------------------------


def _mix_db(rows):
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db = MagicMock()
    db.execute = MagicMock(return_value=result)
    return db


def test_classifier_mix_local_prefix_maps_to_local(user):
    db = _mix_db([("local:email-classifier-v3", 809)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.status_code == 200
    assert resp.json() == {"classifier_mix": [{"kind": "local", "count": 809}]}


@pytest.mark.parametrize(
    "preset", ["openai", "gemini", "openrouter", "groq", "mistral"],
)
def test_classifier_mix_each_preset_prefix_maps_to_user_key(user, preset):
    db = _mix_db([(f"{preset}:some-model", 5)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.json() == {"classifier_mix": [{"kind": "user_key", "count": 5}]}


def test_classifier_mix_bare_operator_model_name_maps_to_operator_key(user):
    # No colon at all -- the operator-paid path (`_classify_llm_server` in
    # classifier.py) stamps a bare `settings.gemini_model`, never a
    # "provider:model" pair, and it must not be confused with a BYOK row.
    db = _mix_db([("gemini-3.5-flash-lite", 86)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.json() == {"classifier_mix": [{"kind": "operator_key", "count": 86}]}


def test_classifier_mix_heuristic_v1_and_heuristic_fallback_both_map_to_heuristic(user):
    db = _mix_db([("heuristic-v1", 11), ("heuristic-fallback", 3)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.json() == {"classifier_mix": [{"kind": "heuristic", "count": 14}]}


def test_classifier_mix_user_override_maps_to_manual(user):
    db = _mix_db([("user-override", 1)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.json() == {"classifier_mix": [{"kind": "manual", "count": 1}]}


def test_classifier_mix_null_model_version_maps_to_unknown(user):
    db = _mix_db([(None, 2)])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.json() == {"classifier_mix": [{"kind": "unknown", "count": 2}]}


def test_classifier_mix_sums_distinct_versions_of_one_kind_into_a_single_row(user):
    # `local:v1`, `local:v2`, `local:v3` are three DIFFERENT model_versions
    # (three GROUP BY rows from Postgres) that all map to `local`. A naive
    # row-per-model_version response would emit three `local` rows here;
    # the contract is exactly one, with counts summed AFTER mapping.
    db = _mix_db(
        [
            ("local:v1", 100),
            ("local:v2", 50),
            ("local:v3", 9),
            ("openai:gpt-4o-mini", 4),
        ]
    )
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    body = resp.json()
    assert body["classifier_mix"] == [
        {"kind": "local", "count": 159},
        {"kind": "user_key", "count": 4},
    ]
    # Never more than one row per kind.
    kinds = [entry["kind"] for entry in body["classifier_mix"]]
    assert len(kinds) == len(set(kinds))


def test_classifier_mix_empty_state_is_200_not_404(user):
    db = _mix_db([])
    _override(user, db)
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.status_code == 200
    assert resp.json() == {"classifier_mix": []}


def test_classifier_mix_unauthenticated_is_rejected():
    # No dependency override for get_current_user -- the real dependency
    # runs and must reject a request carrying no bearer token at all.
    resp = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp.status_code == 401


def test_classifier_mix_filters_by_current_users_id(user):
    db = _mix_db([("local:v1", 1)])
    _override(user, db)
    TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    compiled = _compiled(db.execute.call_args_list[0].args[0])
    assert f"mail_thread.user_id = '{user.id.hex}'" in compiled


def test_classifier_mix_two_user_isolation_each_gets_their_own_scoped_query():
    user_a = MagicMock(id=uuid4())
    user_b = MagicMock(id=uuid4())

    db_a = _mix_db([("local:v1", 3)])
    _override(user_a, db_a)
    resp_a = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp_a.json() == {"classifier_mix": [{"kind": "local", "count": 3}]}
    compiled_a = _compiled(db_a.execute.call_args_list[0].args[0])
    assert f"mail_thread.user_id = '{user_a.id.hex}'" in compiled_a
    assert user_b.id.hex not in compiled_a

    db_b = _mix_db([("openai:gpt-4o-mini", 7)])
    _override(user_b, db_b)
    resp_b = TestClient(app).get("/api/v1/settings/llm/classifier-mix")
    assert resp_b.json() == {"classifier_mix": [{"kind": "user_key", "count": 7}]}
    compiled_b = _compiled(db_b.execute.call_args_list[0].args[0])
    assert f"mail_thread.user_id = '{user_b.id.hex}'" in compiled_b
    assert user_a.id.hex not in compiled_b
