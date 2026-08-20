"""Real-Postgres concurrency coverage for the multi-credential LLM surface
(plan: docs/plans/2026-08-19-multi-credential-llm-profiles-plan.md, D6).

Same idiom as test_auth_integration.py/test_feedback_integration.py: opt-in
via TEST_DATABASE_URL so the offline suite stays green without a live
database, schema built with `Base.metadata.create_all` against a `*_test`-
only database, real row-lock concurrency proven across two sessions in two
threads. What no fake DB can prove: that the D6 `app_user` guard lock (a
real `SELECT ... FOR UPDATE`) actually serializes two concurrent requests
for the same user, that the partial unique index
(`uq_llm_credential_user_active`) is enforced non-deferrably, and the
flush-order regression the activate route's forced flush protects against.

Route functions are called DIRECTLY (not through TestClient/ASGI) -- each
worker thread gets its own SQLAlchemy Session bound to the shared engine and
its own asyncio event loop (`asyncio.run`, for the one async route this
module needs -- `create_llm_credential`), so two calls genuinely overlap at
the DB layer instead of serializing on a single TestClient portal thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request as StarletteRequest

from app.db.base import Base
from app.db.models import AppUser, UserLlmCredential
from app.routes import llm_settings

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run the real-Postgres integration tier",
)

_JOIN_TIMEOUT = 10.0
_BARRIER_TIMEOUT = 5.0


@pytest.fixture(scope="session")
def _engine():
    """Build the schema once per test run against a real Postgres.

    Refuses anything but a `*_test` database up front -- every test in this
    module truncates app_user, and a typo'd env var pointing at dev or prod
    must fail loudly here, not quietly wipe real data.
    """
    db_name = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {db_name!r}, not a *_test database -- "
            "refusing to run tests that TRUNCATE tables against it"
        )
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(_engine):
    """A fresh `sessionmaker` per test, with a clean `app_user` table.
    TRUNCATE ... CASCADE takes `user_llm_credential` with it via its FK, so
    each test starts from empty without needing to know every downstream
    table by name. Every worker thread below opens its OWN session from
    this factory -- never sharing a session across threads.
    """
    factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    setup = factory()
    setup.execute(text("TRUNCATE app_user CASCADE"))
    setup.commit()
    setup.close()
    return factory


def _seed_user(factory) -> UUID:
    db = factory()
    try:
        user = AppUser(email=f"{uuid4()}@example.com")
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _seed_credential(
    factory, user_id: UUID, *, name: str, is_active: bool, provider: str = "openai"
) -> UUID:
    db = factory()
    try:
        row = UserLlmCredential(
            user_id=user_id,
            name=name,
            is_active=is_active,
            provider=provider,
            base_url="https://api.openai.com/v1",
            api_key=f"sk-seed-{name}-key",
            model="gpt-4o-mini",
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _active_rows(factory, user_id: UUID) -> list[UserLlmCredential]:
    db = factory()
    try:
        rows = db.execute(
            select(UserLlmCredential).where(UserLlmCredential.user_id == user_id)
        ).scalars().all()
        return [row for row in rows if row.is_active]
    finally:
        db.close()


def _all_rows(factory, user_id: UUID) -> list[UserLlmCredential]:
    db = factory()
    try:
        return list(
            db.execute(
                select(UserLlmCredential).where(UserLlmCredential.user_id == user_id)
            ).scalars().all()
        )
    finally:
        db.close()


def _json_request(payload: dict) -> StarletteRequest:
    """A real Starlette `Request` over a hand-built ASGI scope/receive --
    same idiom test_llm_settings.py's own body-size test uses for
    `_read_bounded_body`. Lets `create_llm_credential` read a genuine (not
    faked) raw body from a plain worker thread, with no ASGI server or
    TestClient portal involved -- the thing that would otherwise force every
    "concurrent" call through one shared event loop.
    """
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "headers": [(b"content-type", b"application/json")],
        "method": "POST",
        "path": "/api/v1/settings/llm/credentials",
    }
    return StarletteRequest(scope, receive=receive)


def _create(factory, user_id: UUID, payload: dict) -> tuple[int, dict]:
    """Runs the real `create_llm_credential` route against its OWN session,
    in its OWN event loop (`asyncio.run`) -- called as a thread target, so
    multiple invocations genuinely race at the DB layer.
    """
    db = factory()
    try:
        request = _json_request(payload)
        current_user = SimpleNamespace(id=user_id)
        try:
            body = asyncio.run(llm_settings.create_llm_credential(request, current_user, db))
            return 201, body
        except HTTPException as exc:
            db.rollback()
            return exc.status_code, {"detail": exc.detail}
    finally:
        db.close()


def _activate(factory, user_id: UUID, credential_id: UUID) -> tuple[int, dict]:
    db = factory()
    try:
        current_user = SimpleNamespace(id=user_id)
        try:
            body = llm_settings.activate_llm_credential(credential_id, current_user, db)
            return 200, body
        except HTTPException as exc:
            db.rollback()
            return exc.status_code, {"detail": exc.detail}
    finally:
        db.close()


def _run_concurrently(*targets) -> None:
    """Starts every `(callable, args)` pair as its own thread, synchronized
    on a shared `threading.Barrier` so they all begin their route call at
    essentially the same instant -- the thing that turns "two sequential
    calls that happen to both be correct" into an actual test of the D6
    guard lock's real-PG serialization.
    """
    barrier = threading.Barrier(len(targets))

    def _wrap(fn, args):
        barrier.wait(timeout=_BARRIER_TIMEOUT)
        fn(*args)

    threads = [threading.Thread(target=_wrap, args=(fn, args)) for fn, args in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)
        assert not thread.is_alive(), "worker thread never finished -- deadlock?"


# ---------------------------------------------------------------------------
# (a) Two concurrent creates at the cap (4 existing rows): exactly one
# inserts, the other hits the 422 cap rejection.
# ---------------------------------------------------------------------------


def test_two_concurrent_creates_at_cap_exactly_one_inserts_one_rejects(session_factory):
    user_id = _seed_user(session_factory)
    for i in range(4):
        _seed_credential(session_factory, user_id, name=f"cred-{i}", is_active=(i == 0))

    results: dict[str, tuple[int, dict]] = {}

    def _run(key: str, name: str) -> None:
        results[key] = _create(
            session_factory, user_id,
            {"name": name, "provider": "gemini", "api_key": "sk-new-key-12345", "model": "m"},
        )

    _run_concurrently((_run, ("a", "fifth")), (_run, ("b", "sixth")))

    statuses = sorted(status for status, _ in results.values())
    assert statuses == [201, 422]
    rejected = next(body for status, body in results.values() if status == 422)
    assert rejected["detail"] == "credential limit reached"

    rows = _all_rows(session_factory, user_id)
    assert len(rows) == 5  # the cap, not 6 -- the guard closed the phantom-create gap


# ---------------------------------------------------------------------------
# (b) Two concurrent FIRST creates (zero existing rows): both succeed under
# the guard (different names), and exactly one row ends active.
# ---------------------------------------------------------------------------


def test_two_concurrent_first_creates_exactly_one_row_ends_active(session_factory):
    user_id = _seed_user(session_factory)
    results: dict[str, tuple[int, dict]] = {}

    def _run(key: str, name: str) -> None:
        results[key] = _create(
            session_factory, user_id,
            {"name": name, "provider": "openai", "api_key": "sk-new-key-12345", "model": "m"},
        )

    _run_concurrently((_run, ("a", "first")), (_run, ("b", "second")))

    statuses = [status for status, _ in results.values()]
    assert statuses == [201, 201]

    rows = _all_rows(session_factory, user_id)
    assert len(rows) == 2
    active = [row for row in rows if row.is_active]
    assert len(active) == 1  # never zero, never both


# ---------------------------------------------------------------------------
# (c) Two concurrent activates of DIFFERENT credentials: serialized
# last-writer-wins -- BOTH return 200, exactly one row ends active.
# ---------------------------------------------------------------------------


def test_two_concurrent_activates_of_different_credentials_both_succeed(session_factory):
    user_id = _seed_user(session_factory)
    default_id = _seed_credential(session_factory, user_id, name="default", is_active=True)
    work_id = _seed_credential(session_factory, user_id, name="work", is_active=False)

    results: dict[str, tuple[int, dict]] = {}

    def _run(key: str, credential_id: UUID) -> None:
        results[key] = _activate(session_factory, user_id, credential_id)

    _run_concurrently((_run, ("a", work_id)), (_run, ("b", default_id)))

    statuses = [status for status, _ in results.values()]
    # Neither 409s -- the guard lock means the second request validly
    # re-switches after the first commits, not a conflict.
    assert statuses == [200, 200]

    active = _active_rows(session_factory, user_id)
    assert len(active) == 1  # never zero, never both -- whichever committed last


# ---------------------------------------------------------------------------
# (d) The flush-order activation regression: the partial unique index is
# non-deferrable, so writing the two rows in the wrong order (even one
# statement at a time, no ORM dirty-tracking ambiguity involved) rejects a
# legitimate switch outright; the real route, which flushes the old row's
# flip to false before setting the new one true, succeeds regardless of
# which row's id happens to sort first.
# ---------------------------------------------------------------------------


def test_partial_unique_index_is_non_deferrable_and_checks_per_statement(session_factory):
    """Proves the exact hazard `activate_llm_credential`'s forced flush order
    protects against: `uq_llm_credential_user_active` is NOT deferrable, so
    it's checked the moment EACH UPDATE statement runs, not at commit --
    flipping the new row's `is_active` to true FIRST, while the old row is
    still active, is rejected IMMEDIATELY by that one statement (never even
    reaching a second one), because the row it would touch is still active.

    Deliberately raw `UPDATE` statements, not ORM attribute assignment +
    flush: this SQLAlchemy version's unit-of-work does not emit dirty
    UPDATEs in a stable, attribute-assignment-order-independent sequence
    (confirmed empirically -- attempting to reproduce this hazard through
    plain attribute writes was flaky run-to-run), so asserting on that
    internal ordering here would be asserting on an implementation detail
    this module doesn't own. The INDEX's non-deferrable, per-statement
    semantics are what actually matter here, and they're deterministic.
    """
    user_id = _seed_user(session_factory)
    old_id = _seed_credential(session_factory, user_id, name="default", is_active=True)
    new_id = _seed_credential(session_factory, user_id, name="work", is_active=False)

    db = session_factory()
    try:
        # The NAIVE (wrong) order: flip the new row true FIRST, old row
        # still true -- Postgres rejects THIS statement immediately (not the
        # second one), since the index isn't deferrable: the moment this
        # UPDATE completes, two rows would be active at once.
        with pytest.raises(IntegrityError):
            db.execute(
                update(UserLlmCredential)
                .where(UserLlmCredential.id == new_id)
                .values(is_active=True)
            )
        db.rollback()
    finally:
        db.close()

    # The row states on disk are untouched by the failed transaction.
    active = _active_rows(session_factory, user_id)
    assert [row.id for row in active] == [old_id]


def test_activate_credential_route_succeeds_regardless_of_this_ordering_hazard(session_factory):
    user_id = _seed_user(session_factory)
    _seed_credential(session_factory, user_id, name="default", is_active=True)
    new_id = _seed_credential(session_factory, user_id, name="work", is_active=False)

    status, body = _activate(session_factory, user_id, new_id)
    assert status == 200
    assert body["active"] is True

    active = _active_rows(session_factory, user_id)
    assert [row.id for row in active] == [new_id]
    all_rows = _all_rows(session_factory, user_id)
    assert len(all_rows) == 2  # both rows still exist -- activate never deletes
