"""Google OAuth flow tests: state CSRF verification and error hygiene.

These run offline (no Redis, no Google). The Redis-backed state helpers are
monkeypatched, and the one test that reaches the token exchange swaps in a
fake httpx client, so nothing here touches the network.
"""

from urllib.parse import parse_qs, urlparse

import redis
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.routes import auth_google

client = TestClient(app)

START = "/api/v1/auth/google/start"
CALLBACK = "/api/v1/auth/google/callback"


def _configure_google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings, "google_redirect_uri", "http://localhost:5173/auth/google/callback"
    )


def _redis_down(state):
    raise redis.ConnectionError("redis is down")


def test_start_stores_the_state_it_returns(monkeypatch):
    _configure_google(monkeypatch)
    stored = []
    monkeypatch.setattr(auth_google, "_store_state", stored.append)
    resp = client.get(START)
    assert resp.status_code == 200
    query = parse_qs(urlparse(resp.json()["auth_url"]).query)
    assert stored == query["state"]


def test_start_fails_closed_when_redis_is_down(monkeypatch):
    _configure_google(monkeypatch)
    monkeypatch.setattr(auth_google, "_store_state", _redis_down)
    assert client.get(START).status_code == 503


def test_callback_rejects_missing_state():
    resp = client.get(CALLBACK, params={"code": "abc"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_rejects_unknown_state(monkeypatch):
    monkeypatch.setattr(auth_google, "_consume_state", lambda state: False)
    resp = client.get(CALLBACK, params={"code": "abc", "state": "not-ours"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_returns_503_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(auth_google, "_consume_state", _redis_down)
    resp = client.get(CALLBACK, params={"code": "abc", "state": "whatever"})
    assert resp.status_code == 503


def test_callback_hides_google_error_bodies(monkeypatch):
    """A failed token exchange logs the upstream body but returns a generic
    detail -- Google's error text can echo our client_id."""
    _configure_google(monkeypatch)
    monkeypatch.setattr(auth_google, "_consume_state", lambda state: True)

    class FakeResponse:
        status_code = 400
        text = '{"error": "invalid_client", "client_id": "test-client-id"}'

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(auth_google.httpx, "AsyncClient", FakeAsyncClient)
    resp = client.get(CALLBACK, params={"code": "abc", "state": "ours"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Google sign-in failed; try again."
    assert "client_id" not in resp.text
