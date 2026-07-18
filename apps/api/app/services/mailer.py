"""Small Resend client for the account emails sent by background workers."""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import logger


class MailerError(Exception):
    """Raised when Resend could not accept an authentication email."""


def _send(kind: str, to: str, subject: str, html: str, text: str, note: str) -> None:
    """Send one email, or log its safe development fallback without HTTP."""
    if not settings.resend_api_key:
        logger.info("[dev-mail] %s for %s: %s", kind, to, note)
        return

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
            )
    except httpx.HTTPError as exc:
        logger.error(
            "Resend %s email failed for %s: transport=%s",
            kind,
            to,
            type(exc).__name__,
        )
        raise MailerError("Email delivery failed") from exc

    if response.status_code >= 400:
        logger.error(
            "Resend %s email failed for %s: status=%s detail=%s",
            kind,
            to,
            response.status_code,
            "Resend rejected the request",
        )
        raise MailerError("Email delivery failed")


def send_verification_email(to: str, link: str) -> None:
    """Send the explicit-click link that confirms a pending password addition."""
    _send(
        "verify_email",
        to,
        "Confirm your CortexMail password",
        f"<p>Someone added a password to the CortexMail account for this address. "
        f"Nothing changes unless you confirm it.</p><p><a href=\"{link}\">"
        "Confirm password</a></p>",
        "Someone added a password to the CortexMail account for this address. "
        f"Nothing changes unless you confirm it: {link}",
        link,
    )


def send_account_exists_email(to: str) -> None:
    """Tell a password-account owner to sign in or reset, without any token link."""
    _send(
        "account_exists",
        to,
        "Your CortexMail account already has a password",
        "<p>Your CortexMail account already has a password. Sign in, or use "
        "password reset if you need help.</p>",
        "Your CortexMail account already has a password. Sign in, or use password "
        "reset if you need help.",
        "account already has a password; sign in or reset it",
    )


def send_password_reset_email(to: str, link: str) -> None:
    """Send the explicit link used to reset a known account's password."""
    _send(
        "password_reset",
        to,
        "Reset your CortexMail password",
        f"<p>Use this link to reset your CortexMail password:</p><p><a href=\"{link}\">"
        "Reset password</a></p>",
        f"Use this link to reset your CortexMail password: {link}",
        link,
    )
