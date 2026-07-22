"""Background delivery for authentication email, including private template choice."""

from __future__ import annotations

from sqlalchemy import select

from .celery_app import celery_app
from app.core.security import normalize_email
from app.db.base import SessionLocal
from app.db.models import AppUser
from app.services.mailer import (
    MailerError,
    send_account_exists_email,
    send_password_reset_email,
    send_verification_email,
)


@celery_app.task(
    autoretry_for=(MailerError,), retry_backoff=True, max_retries=5, ignore_result=True
)
def send_auth_email(purpose: str, email: str, link: str | None) -> None:
    """Choose and send a non-enumerating auth email in the worker.

    Unknown reset addresses are deliberately silent. Verification links are
    never sent when the address already owns a password, even if an older task
    reaches the worker after that password was created.
    """
    normalized_email = normalize_email(email)
    with SessionLocal() as db:
        user = db.scalar(
            select(AppUser).where(AppUser.email == normalized_email)
        )
        if purpose == "verify_email":
            if user is not None and user.password_hash is not None:
                send_account_exists_email(normalized_email)
                return
            if link is None:
                raise ValueError("Authentication email link is required")
            send_verification_email(normalized_email, link)
            return

        if purpose == "password_reset":
            if user is None:
                return
            if link is None:
                raise ValueError("Authentication email link is required")
            send_password_reset_email(normalized_email, link)
            return

        raise ValueError("Unknown authentication email purpose")
