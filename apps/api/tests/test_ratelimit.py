"""Rate limiter tests: fixed-window behavior and fail-open.

Everything runs offline. Redis is replaced by a tiny in-memory fake (or one
that always raises) via the module's lazy client helper, and the demo-login
endpoint test stubs the DB so results are deterministic without Postgres.
"""

import logging

import pytest
import redis
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core import ratelimit
from app.deps import get_db
from app.main import app as main_app


class FakeRedis:
    """Just enough of the redis API for the fixed-window limiter."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -1)


class BrokenRedis:
    """Every call fails the way a down Redis would."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise redis.ConnectionError("redis is down")

        return boom


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ratelimit, "_client", lambda: fake)
    return fake


def _limited_app(limit: int = 3, window: int = 60) -> FastAPI:
    """Minimal app with one IP-keyed rate-limited route."""
    test_app = FastAPI()

    @test_app.get("/ping", dependencies=[Depends(ratelimit.rate_limit("ping", limit, window))])
    def ping() -> dict:
        return {"ok": True}

    return test_app


def test_requests_under_the_limit_pass(fake_redis):
    client = TestClient(_limited_app(limit=3))
    for _ in range(3):
        assert client.get("/ping").status_code == 200


def test_requests_over_the_limit_get_429_with_retry_after(fake_redis):
    client = TestClient(_limited_app(limit=3, window=60))
    for _ in range(3):
        client.get("/ping")
    resp = client.get("/ping")
    assert resp.status_code == 429
    assert resp.json()["detail"] == "Too many requests; try again shortly."
    # Retry-After should reflect the key's TTL, which the fake pins to the window.
    assert resp.headers["Retry-After"] == "60"


def test_first_hit_starts_the_window(fake_redis):
    client = TestClient(_limited_app(limit=3, window=45))
    client.get("/ping")
    # One key, keyed on the caller's host, with the expiry set on first INCR.
    (key,) = fake_redis.counts
    assert key.startswith("rl:ping:")
    assert fake_redis.ttls[key] == 45


def test_redis_failure_fails_open_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(ratelimit, "_client", lambda: BrokenRedis())
    client = TestClient(_limited_app(limit=1))
    with caplog.at_level(logging.WARNING, logger="ai-mailbox"):
        # Way past the limit, but every request must still get through.
        for _ in range(5):
            assert client.get("/ping").status_code == 200
    assert any("Rate limiter unavailable" in record.message for record in caplog.records)


def test_demo_login_is_limited_per_ip(fake_redis):
    """The 11th demo-login from one IP is rejected before any DB access."""

    def _db_down():
        raise SQLAlchemyError("no database in offline tests")

    main_app.dependency_overrides[get_db] = _db_down
    try:
        client = TestClient(main_app, raise_server_exceptions=False)
        payload = {"email": "abuse@example.com"}
        # Under the limit the limiter waves requests through and they die at the
        # stubbed DB instead -- 503, never 429.
        for _ in range(10):
            assert client.post("/api/v1/auth/demo-login", json=payload).status_code == 503
        resp = client.post("/api/v1/auth/demo-login", json=payload)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
    finally:
        main_app.dependency_overrides.clear()
