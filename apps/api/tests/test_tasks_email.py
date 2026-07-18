import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import mailer
from app.workers import tasks_email


def test_dev_mail_logs_link_and_skips_http(monkeypatch, caplog):
    client = Mock()
    monkeypatch.setattr(mailer.settings, "resend_api_key", None)
    monkeypatch.setattr(mailer.httpx, "Client", client)

    with caplog.at_level(logging.INFO, logger="ai-mailbox"):
        mailer.send_verification_email("user@example.com", "https://app/#token=secret")

    assert "[dev-mail] verify_email for user@example.com: https://app/#token=secret" in caplog.text
    client.assert_not_called()


def test_verify_existing_password_sends_account_exists_template(monkeypatch):
    db = Mock()
    db.scalar.return_value = SimpleNamespace(password_hash="stored")
    account_exists = Mock()
    verify = Mock()
    monkeypatch.setattr(tasks_email, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_email, "send_account_exists_email", account_exists)
    monkeypatch.setattr(tasks_email, "send_verification_email", verify)

    tasks_email.send_auth_email.run("verify_email", "USER@example.com", "https://app/#token=secret")

    account_exists.assert_called_once_with("user@example.com")
    verify.assert_not_called()


def test_verify_unknown_or_passwordless_user_sends_verify_template(monkeypatch):
    verify = Mock()
    monkeypatch.setattr(tasks_email, "send_verification_email", verify)

    for user in (None, SimpleNamespace(password_hash=None)):
        db = Mock()
        db.scalar.return_value = user
        monkeypatch.setattr(tasks_email, "SessionLocal", lambda db=db: nullcontext(db))
        tasks_email.send_auth_email.run(
            "verify_email", "user@example.com", "https://app/#token=secret"
        )

    assert verify.call_count == 2


def test_reset_for_unknown_address_is_a_silent_noop(monkeypatch):
    db = Mock()
    db.scalar.return_value = None
    reset = Mock()
    monkeypatch.setattr(tasks_email, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(tasks_email, "send_password_reset_email", reset)

    tasks_email.send_auth_email.run(
        "password_reset", "unknown@example.com", "https://app/#token=secret"
    )

    reset.assert_not_called()
