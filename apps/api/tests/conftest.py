"""Shared fixtures for the offline test suite.

The rate limiter counts requests in whatever redis settings.redis_url points
at. These tests never want that: with the docker-compose redis up, repeated
suite runs would accumulate real per-IP counters and start returning 429s for
endpoints under test (the demo-login tests post from the same fake client IP
every run). Give every test its own throwaway in-memory counter instead, so
runs are isolated from live infrastructure and from each other.
"""

import pytest

from app.core import ratelimit


class _MemoryRedis:
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


@pytest.fixture(autouse=True)
def _isolated_rate_limiter(monkeypatch):
    """Fresh in-memory counter per test; tests that need to poke the limiter
    directly (test_ratelimit) just patch over this with their own fake."""
    fake = _MemoryRedis()
    monkeypatch.setattr(ratelimit, "_client", lambda: fake)
    return fake
