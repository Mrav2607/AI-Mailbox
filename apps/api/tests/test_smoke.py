"""Smoke tests for endpoints that don't require external services (DB/Redis)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_providers():
    resp = client.get("/api/v1/auth/providers")
    assert resp.status_code == 200
    # Gmail is the only provider that actually works; we used to also advertise
    # Outlook, which is implemented nowhere.
    assert resp.json() == {"providers": ["gmail"]}


def test_bare_prefix_is_404():
    # /api/v1 is only a prefix, not a registered route.
    assert client.get("/api/v1").status_code == 404
