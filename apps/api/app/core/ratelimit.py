"""Redis-backed fixed-window rate limiting for abuse-prone endpoints.

Two dependency factories: ``rate_limit`` keys on the caller's IP (for
anonymous routes like demo-login), ``user_rate_limit`` keys on the
authenticated user's id. Both fail open -- if Redis is down or unreachable we
log a warning and let the request through, because rate limiting is a
guardrail here, not a load-bearing wall. That also keeps offline dev and the
test suite working without a Redis.
"""

from __future__ import annotations

from typing import Callable

import redis
from fastapi import Depends, HTTPException, Request

from app.core.config import settings
from app.core.logging import logger
from app.db.models import AppUser
from app.deps import get_current_user

_REDIS_TIMEOUT_SECONDS = 2

_redis_client: redis.Redis | None = None


def _client() -> redis.Redis:
    """Build the Redis client on first use so importing this module never
    needs a live Redis (mirrors the lazy pattern in auth_google_dev)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )
    return _redis_client


def _enforce(scope: str, key: str, limit: int, window_seconds: int) -> None:
    """Fixed-window check: INCR a per-scope/per-caller counter and 429 once it
    passes the limit. The first hit in a window sets the expiry, so the counter
    resets on its own."""
    redis_key = f"rl:{scope}:{key}"
    try:
        client = _client()
        count = client.incr(redis_key)
        if count == 1:
            client.expire(redis_key, window_seconds)
        if count <= limit:
            return
        ttl = client.ttl(redis_key)
    except redis.RedisError as exc:
        # Fail open: a down Redis shouldn't take the whole API with it.
        logger.warning(
            "Rate limiter unavailable (%s); allowing request for scope %r",
            type(exc).__name__,
            scope,
        )
        return

    # TTL can come back <= 0 if the key expired between calls; fall back to the
    # full window so Retry-After is always a sane positive number.
    retry_after = ttl if ttl and ttl > 0 else window_seconds
    raise HTTPException(
        status_code=429,
        detail="Too many requests; try again shortly.",
        headers={"Retry-After": str(retry_after)},
    )


def rate_limit(scope: str, limit: int, window_seconds: int) -> Callable:
    """Per-IP limiter for routes with no authenticated user."""

    def dependency(request: Request) -> None:
        # request.client can be None under some test harnesses; bucket those
        # callers together rather than crash.
        host = request.client.host if request.client else "unknown"
        _enforce(scope, host, limit, window_seconds)

    return dependency


def user_rate_limit(scope: str, limit: int, window_seconds: int) -> Callable:
    """Per-user limiter for authenticated routes. FastAPI caches dependency
    results per request, so leaning on get_current_user here doesn't cost a
    second token lookup."""

    def dependency(current_user: AppUser = Depends(get_current_user)) -> None:
        _enforce(scope, str(current_user.id), limit, window_seconds)

    return dependency
