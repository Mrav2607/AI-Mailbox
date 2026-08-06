"""Tests for `integrity_error_detail`: pulling the violated constraint name
out of an IntegrityError so a log line can point straight at the cause
instead of just saying "IntegrityError".
"""

from sqlalchemy.exc import IntegrityError

from app.core.logging import integrity_error_detail


class _FakeDiag:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _FakeOrig(Exception):
    """Stands in for psycopg 3's driver exception, which carries `.diag`."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.diag = _FakeDiag(constraint_name)


def test_returns_constraint_name_when_diag_is_present():
    exc = IntegrityError("insert", {}, _FakeOrig("uq_provider_account_provider_external_user"))
    assert integrity_error_detail(exc) == "uq_provider_account_provider_external_user"


def test_falls_back_to_type_name_for_plain_exception_orig():
    # Matches the test doubles in test_oauth_google.py / test_auth_microsoft.py,
    # which raise IntegrityError with a plain Exception as `orig`.
    exc = IntegrityError("insert", {}, Exception("constraint"))
    assert integrity_error_detail(exc) == "Exception"


def test_does_not_raise_when_orig_is_missing():
    exc = IntegrityError("insert", {}, None)
    assert integrity_error_detail(exc) == "IntegrityError"
