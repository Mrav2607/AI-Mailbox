from types import SimpleNamespace

from argon2 import PasswordHasher

from app.core import security


def test_password_hash_round_trip_and_wrong_password():
    password_hash = security.hash_password("correct horse battery staple")

    assert security.verify_password("correct horse battery staple", password_hash)
    assert not security.verify_password("wrong password", password_hash)


def test_missing_hash_verifies_against_dummy_and_returns_false(monkeypatch):
    seen = {}

    def verify(password_hash, password):
        seen["hash"] = password_hash
        seen["password"] = password
        return False

    monkeypatch.setattr(security, "_PASSWORD_HASHER", SimpleNamespace(verify=verify))

    assert security.verify_password("not stored", None) is False
    assert seen == {"hash": security._DUMMY_HASH, "password": "not stored"}


def test_missing_hash_never_authenticates_even_if_dummy_verify_passes(monkeypatch):
    # The dummy plaintext is random per process, but even a hypothetical match
    # must not log anyone in -- there is no stored password to match against.
    monkeypatch.setattr(
        security, "_PASSWORD_HASHER", SimpleNamespace(verify=lambda *_: True)
    )

    assert security.verify_password("anything", None) is False


def test_malformed_password_hash_is_an_authentication_failure():
    assert security.verify_password("not stored", "not an argon2 hash") is False


def test_needs_rehash_for_differently_parameterized_hash():
    weaker_hash = PasswordHasher(time_cost=2).hash("correct horse battery staple")

    assert security.needs_rehash(weaker_hash) is True


def test_normalize_email_strips_and_casefolds():
    assert security.normalize_email("  USER@Example.COM  ") == "user@example.com"
