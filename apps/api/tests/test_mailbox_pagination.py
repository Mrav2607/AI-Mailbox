"""Triage/search pagination, account filtering, and account sort.

Validation cases (bad offset/sort) go through the TestClient like
test_validation.py. The statement-shape cases inspect the actual SQLAlchemy
statement handed to the (mocked) db.execute -- same style as
test_ingest_queue.py's `sync_paused_at IS NULL` check -- since a stub DB can't
tell us what WHERE/ORDER BY/OFFSET a route actually built.
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
    # Empty-result DB stub, same shape as test_validation's: enough for the
    # triage/search scalars().all() path and the counts .all()/.scalar_one()
    # paths.
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.all.return_value = []
    db.execute.return_value.scalar_one.return_value = 0
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _last_statement(db):
    return db.execute.call_args[0][0]


def _compiled(statement):
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# ---- validation ----


def test_triage_negative_offset_is_422(client):
    c, _ = client
    resp = c.get("/api/v1/mail/triage?offset=-1")
    assert resp.status_code == 422


def test_triage_invalid_sort_is_422(client):
    c, _ = client
    resp = c.get("/api/v1/mail/triage?sort=bogus")
    assert resp.status_code == 422
    assert "Invalid sort" in resp.json()["detail"]


@pytest.mark.parametrize("sort", ["recency", "account"])
def test_triage_valid_sort_values_pass_validation(client, sort):
    c, _ = client
    resp = c.get(f"/api/v1/mail/triage?sort={sort}")
    assert resp.status_code == 200
    assert resp.json() == {"bucket": "needs_reply", "items": []}


# ---- triage statement shape ----


def test_triage_statement_carries_offset(client):
    c, db = client
    c.get("/api/v1/mail/triage?offset=40&limit=20")
    compiled = _compiled(_last_statement(db))
    assert "OFFSET 40" in compiled
    assert "LIMIT 20" in compiled


def test_triage_statement_carries_provider_account_predicate_when_set(client):
    c, db = client
    account_id = uuid4()
    c.get(f"/api/v1/mail/triage?provider_account_id={account_id}")
    compiled = _compiled(_last_statement(db))
    # literal_binds renders the UUID column value without hyphens.
    assert f"mail_thread.provider_account_id = '{account_id.hex}'" in compiled


def test_triage_statement_omits_provider_account_predicate_by_default(client):
    c, db = client
    c.get("/api/v1/mail/triage")
    compiled = _compiled(_last_statement(db))
    assert "provider_account_id =" not in compiled


def test_triage_default_sort_carries_id_tiebreak(client):
    c, db = client
    c.get("/api/v1/mail/triage")
    compiled = _compiled(_last_statement(db))
    assert "ORDER BY" in compiled
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "mail_thread.id DESC" in order_by_clause


def test_triage_sort_account_joins_provider_account_and_orders_by_external_user_id(client):
    c, db = client
    c.get("/api/v1/mail/triage?sort=account")
    compiled = _compiled(_last_statement(db))
    assert "JOIN provider_account" in compiled
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "provider_account.external_user_id" in order_by_clause
    # Recency (and its id tiebreak) still applies after the account grouping.
    assert "mail_thread.id DESC" in order_by_clause


# ---- search statement shape ----


def test_search_statement_carries_offset_and_account_predicate(client):
    c, db = client
    account_id = uuid4()
    c.get(f"/api/v1/mail/search?q=invoice&offset=10&provider_account_id={account_id}")
    compiled = _compiled(_last_statement(db))
    assert "OFFSET 10" in compiled
    assert f"mail_thread.provider_account_id = '{account_id.hex}'" in compiled


def test_search_statement_carries_id_tiebreak(client):
    # Same transaction-stamped created_at nondeterminism as triage, so paged
    # search needs the same id DESC tiebreak.
    c, db = client
    c.get("/api/v1/mail/search?q=invoice")
    compiled = _compiled(_last_statement(db))
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "mail_thread.id DESC" in order_by_clause


# ---- counts statement shape ----


def test_counts_statements_carry_provider_account_predicate_when_set(client):
    c, db = client
    account_id = uuid4()
    resp = c.get(f"/api/v1/mail/counts?provider_account_id={account_id}")
    assert resp.status_code == 200
    statements = [call.args[0] for call in db.execute.call_args_list]
    # Both the grouped open-buckets query and the done-count query must carry
    # the predicate, so counts["all"] (from the grouped query) and
    # counts["done"] agree on the same account scope.
    assert len(statements) == 2
    for statement in statements:
        compiled = _compiled(statement)
        assert f"mail_thread.provider_account_id = '{account_id.hex}'" in compiled


def test_counts_statements_omit_predicate_by_default(client):
    c, db = client
    c.get("/api/v1/mail/counts")
    statements = [call.args[0] for call in db.execute.call_args_list]
    for statement in statements:
        assert "provider_account_id =" not in _compiled(statement)
