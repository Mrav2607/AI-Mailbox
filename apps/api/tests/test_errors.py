"""Error-handler tests: DB and uncaught errors return a consistent JSON shape.

These build a throwaway app that wires the real handlers onto routes that
raise, so no DB is involved. ``raise_server_exceptions=False`` makes the
TestClient return the handler's response instead of re-raising, mirroring
production.
"""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.errors import register_exception_handlers


def _build_client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/integrity")
    def _integrity():
        raise IntegrityError("INSERT ...", params=None, orig=Exception("duplicate key"))

    @app.get("/boom/db")
    def _db():
        raise OperationalError("SELECT ...", params=None, orig=Exception("connection lost"))

    @app.get("/boom/unhandled")
    def _unhandled():
        raise RuntimeError("something unexpected")

    @app.get("/boom/http")
    def _http():
        raise HTTPException(status_code=404, detail="Nope")

    return TestClient(app, raise_server_exceptions=False)


client = _build_client()


def test_integrity_error_returns_409():
    resp = client.get("/boom/integrity")
    assert resp.status_code == 409
    body = resp.json()
    assert "conflict" in body["detail"].lower()
    assert "error_id" in body


def test_database_error_returns_503():
    resp = client.get("/boom/db")
    assert resp.status_code == 503
    body = resp.json()
    assert "error_id" in body


def test_unhandled_error_returns_generic_500():
    resp = client.get("/boom/unhandled")
    assert resp.status_code == 500
    body = resp.json()
    # The real exception message must not leak to the client.
    assert "something unexpected" not in str(body)
    assert body["detail"] == "An internal error occurred."
    assert "error_id" in body


def test_http_exception_keeps_default_handling():
    # HTTPException still gets FastAPI's built-in handler (no error_id added).
    resp = client.get("/boom/http")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Nope"}
