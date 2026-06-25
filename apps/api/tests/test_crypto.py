"""Encryption-at-rest tests for provider tokens.

Cover the round-trip, legacy-plaintext passthrough, wrong-key detection, and
the transparent column type -- all without a database.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core import crypto
from app.db.types import EncryptedText


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    # The Fernet instance is cached; clear it so key-swapping tests are isolated.
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def test_encrypt_decrypt_roundtrip():
    secret = "ya29.a-real-looking-access-token"
    ciphertext = crypto.encrypt(secret)
    assert ciphertext != secret
    assert crypto._looks_like_fernet(ciphertext)
    assert crypto.decrypt(ciphertext) == secret


def test_decrypt_passes_through_legacy_plaintext():
    # A value written before encryption isn't a Fernet token: return it as-is.
    legacy = "1//legacy-refresh-token"
    assert not crypto._looks_like_fernet(legacy)
    assert crypto.decrypt(legacy) == legacy


def test_short_value_with_fernet_version_byte_is_not_misread():
    # "gA==" decodes to a single 0x80 byte: looks like the Fernet version byte
    # but is far too short to be a real token, so it must pass through, not
    # trigger a decrypt that raises.
    assert not crypto._looks_like_fernet("gA==")
    assert crypto.decrypt("gA==") == "gA=="


def test_decrypt_raises_on_wrong_key(monkeypatch):
    ciphertext = crypto.encrypt("secret")
    # Swap to a different valid key and clear the cache: real ciphertext that
    # won't decrypt must raise, not silently return garbage.
    monkeypatch.setattr(crypto.settings, "token_encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    with pytest.raises(InvalidToken):
        crypto.decrypt(ciphertext)


def test_encrypted_text_type_bind_and_result():
    col = EncryptedText()
    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None

    stored = col.process_bind_param("token", None)
    assert stored != "token"
    assert crypto._looks_like_fernet(stored)
    assert col.process_result_value(stored, None) == "token"


def test_encrypted_text_result_passes_through_legacy():
    col = EncryptedText()
    assert col.process_result_value("plain-legacy", None) == "plain-legacy"
