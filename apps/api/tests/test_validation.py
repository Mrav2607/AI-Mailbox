"""Input-validation tests: count params are bounded and bucket is checked.

Auth and DB are overridden with stubs so these exercise the validation layer
only -- every case here is rejected before the route touches the database.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app


@pytest.fixture
def client():
    user = MagicMock(id=uuid4())
    # A DB stub whose queries return empty result sets, so a request that clears
    # validation completes deterministically instead of blowing up on a bare
    # MagicMock.
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/mail/triage?limit=0",
        "/api/v1/mail/triage?limit=99999",
        "/api/v1/mail/ingest/gmail?max_results=0",
        "/api/v1/mail/ingest/gmail?max_results=10000",
        "/api/v1/mail/classify/backfill?limit=99999",
        "/api/v1/mail/classify/queue?limit=99999",
    ],
)
def test_out_of_range_counts_are_rejected(client, url):
    method = client.post if "ingest" in url or "classify" in url else client.get
    resp = method(url)
    assert resp.status_code == 422


def test_invalid_bucket_is_rejected(client):
    resp = client.get("/api/v1/mail/triage?bucket=not_a_bucket")
    assert resp.status_code == 422
    assert "Invalid bucket" in resp.json()["detail"]


def test_backfill_rejects_bad_bucket_and_backend(client):
    # Both are validated before any DB access, so the stub DB is untouched.
    bad_bucket = client.post("/api/v1/mail/classify/backfill?bucket=not_a_bucket")
    assert bad_bucket.status_code == 422
    assert "Invalid bucket" in bad_bucket.json()["detail"]

    bad_backend = client.post("/api/v1/mail/classify/backfill?backend=not_a_model")
    assert bad_backend.status_code == 422
    assert "Invalid backend" in bad_backend.json()["detail"]


def test_backfill_valid_params_pass_validation(client):
    # A known bucket + backend clears validation and, against the empty-result DB
    # stub, classifies nothing.
    resp = client.post(
        "/api/v1/mail/classify/backfill?bucket=unclassified&backend=heuristic&limit=10"
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "created": 0, "scanned": 0}


def test_invalid_reclassify_label_is_rejected(client):
    # The label is validated before any DB access, so the stub DB is untouched.
    resp = client.post(
        f"/api/v1/mail/thread/{uuid4()}/classification",
        json={"label": "not_a_label"},
    )
    assert resp.status_code == 422
    assert "Invalid label" in resp.json()["detail"]


def test_search_requires_a_query(client):
    # q is required and must be non-empty, so a bare/blank search is rejected
    # before touching the database.
    assert client.get("/api/v1/mail/search").status_code == 422
    assert client.get("/api/v1/mail/search?q=").status_code == 422


def test_search_returns_ok_with_empty_results(client):
    resp = client.get("/api/v1/mail/search?q=invoice")
    assert resp.status_code == 200
    assert resp.json() == {"query": "invoice", "items": []}


def test_delete_unknown_thread_is_404(client):
    # The stub thread's owner never matches the current user, so delete is
    # rejected as not-found without leaking existence.
    resp = client.delete(f"/api/v1/mail/thread/{uuid4()}")
    assert resp.status_code == 404


def test_valid_bucket_passes_validation(client):
    # A known bucket clears validation and, against the empty-result DB stub,
    # returns an empty triage page.
    resp = client.get("/api/v1/mail/triage?bucket=all&limit=10")
    assert resp.status_code == 200
    assert resp.json() == {"bucket": "all", "items": []}
