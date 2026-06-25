"""CORS tests: allowed origins get the right headers; unknown origins don't.

These run offline (no DB/Redis). A CORS preflight is an OPTIONS request, which
the middleware answers before any route or auth logic runs.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


def test_preflight_allows_configured_origin():
    origins = settings.cors_origins_list
    if not origins:
        pytest.skip("no CORS origins configured")
    allowed_origin = origins[0]
    resp = client.options(
        "/api/v1/mail/triage",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == allowed_origin
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_unknown_origin_is_not_allowed():
    resp = client.options(
        "/api/v1/mail/triage",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette withholds the allow-origin header for disallowed origins, so the
    # browser blocks the response.
    assert "access-control-allow-origin" not in resp.headers
