"""Liveness and readiness probes.

``/health`` only says whether the process is up, so a transient dependency
outage doesn't get the container killed and restarted. ``/ready`` pings
Postgres and Redis and returns 503 if either is unreachable.
"""

import redis
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger
from app.db.base import engine
from app.db.schemas.health import Health, Readiness

router = APIRouter()

# Bound the probe
_REDIS_TIMEOUT_SECONDS = 2


@router.get("/health", response_model=Health)
async def health_check():
    """Verifies liveness."""
    return {"status": "ok"}


def _check_database() -> str | None:
    """Return None if Postgres answers, else a short error label."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        logger.warning("Readiness: database check failed: %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"


def _check_redis() -> str | None:
    """Return None if Redis answers PING, else a short error label."""
    # Construct inside the try so a bad REDIS_URL is caught as "not ready"
    # rather than escaping as a 500.
    client = None
    try:
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        client.ping()
        return None
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        logger.warning("Readiness: redis check failed: %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"
    finally:
        if client is not None:
            client.close()


@router.get("/ready", response_model=Readiness)
def readiness_check(response: Response):
    """Verifies readiness, checks whether backing services are reachable.

    Plain ``def`` (not ``async``) so FastAPI runs it in a threadpool -- the DB
    and Redis checks are blocking I/O and would otherwise stall the event loop
    during an outage.

    The failure branch sets the status code on the shared response rather than
    returning its own JSONResponse: a Response returned directly skips the
    response_model entirely, so the 503 body would be the one shape nobody ever
    validated -- which is exactly the shape an on-call probe is reading.
    """
    db_error = _check_database()
    redis_error = _check_redis()
    checks = {
        "database": db_error or "ok",
        "redis": redis_error or "ok",
    }
    ready = db_error is None and redis_error is None
    if not ready:
        # 503 so probes and load balancers treat the instance as out of rotation.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not ready", "checks": checks}
