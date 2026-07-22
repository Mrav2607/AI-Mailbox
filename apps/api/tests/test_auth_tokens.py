from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.services import auth_tokens


class _Result:
    def __init__(self, row=None):
        self.row = row

    def scalars(self):
        return self

    def one_or_none(self):
        return self.row


class _TokenDB:
    """Capture the service's database operations without a Postgres server."""

    def __init__(self, consume_rows=()):
        self.statements = []
        self.added = []
        self.consume_rows = iter(consume_rows)
        self.flushes = 0

    def execute(self, statement):
        self.statements.append(statement)
        if getattr(statement, "_returning", ()):
            return _Result(next(self.consume_rows, None))
        return _Result()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flushes += 1


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


def test_issue_then_consume_round_trip_without_committing():
    row = SimpleNamespace(purpose="verify_email")
    db = _TokenDB(consume_rows=[row])

    raw_token = auth_tokens.issue_token(
        db,
        purpose="verify_email",
        email=" USER@example.com ",
        ttl=auth_tokens.VERIFY_TTL,
    )
    consumed = auth_tokens.consume_token(
        db, purpose="verify_email", raw_token=raw_token
    )

    assert consumed is row
    assert db.added[0].email == "user@example.com"
    assert db.added[0].token_hash == auth_tokens._token_hash(raw_token)
    assert db.flushes == 1
    assert "delete from auth_token" in _sql(db.statements[-1])
    assert "returning" in _sql(db.statements[-1])


def test_consuming_twice_only_returns_the_first_row():
    row = SimpleNamespace(purpose="verify_email")
    db = _TokenDB(consume_rows=[row, None])

    assert auth_tokens.consume_token(db, purpose="verify_email", raw_token="raw") is row
    assert auth_tokens.consume_token(db, purpose="verify_email", raw_token="raw") is None


def test_expired_and_wrong_purpose_are_indistinguishable():
    db = _TokenDB(consume_rows=[None, None])

    assert auth_tokens.consume_token(db, purpose="verify_email", raw_token="expired") is None
    assert (
        auth_tokens.consume_token(db, purpose="password_reset", raw_token="verify-token")
        is None
    )


def test_reissue_replaces_the_old_token_and_purges_expired_rows():
    db = _TokenDB()
    first = auth_tokens.issue_token(
        db, purpose="verify_email", email="user@example.com", ttl=timedelta(hours=1)
    )
    second = auth_tokens.issue_token(
        db, purpose="verify_email", email="user@example.com", ttl=timedelta(hours=1)
    )

    assert first != second
    assert db.added[0].token_hash != db.added[1].token_hash
    statements = [_sql(statement) for statement in db.statements]
    assert any("auth_token.email" in statement and "auth_token.purpose" in statement for statement in statements)
    assert any("auth_token.expires_at < now()" in statement for statement in statements)
