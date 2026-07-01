"""Health/readiness tests.

Readiness is exercised by stubbing the per-dependency checks, so the suite 
needs no live Postgres or Redis -- the actual SELECT 1 / PING calls are thin 
wrappers validated against real services in deployment.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routes import health

client = TestClient(app)


def test_health_is_static_ok():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_returns_200_when_dependencies_up(monkeypatch):
    monkeypatch.setattr(health, "_check_database", lambda: None)
    monkeypatch.setattr(health, "_check_redis", lambda: None)
    resp = client.get("/api/v1/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
    }


def test_ready_returns_503_when_a_dependency_is_down(monkeypatch):
    monkeypatch.setattr(health, "_check_database", lambda: None)
    monkeypatch.setattr(health, "_check_redis", lambda: "error: ConnectionError")
    resp = client.get("/api/v1/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"].startswith("error")
