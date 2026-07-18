"""Email/password authentication routes with verification-first credential writes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.ratelimit import _enforce, rate_limit
from app.core.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.db.models import AppUser, AuthToken
from app.db.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    ResetSentOut,
    ResendVerificationRequest,
    SignupRequest,
    TokenOut,
    VerificationSentOut,
    VerifyEmailRequest,
)
from app.deps import get_db
from app.services.auth_tokens import RESET_TTL, VERIFY_TTL, consume_token, issue_token
from app.workers.tasks_email import send_auth_email

router = APIRouter()

_INVALID_CREDENTIALS = HTTPException(
    status_code=401, detail="Invalid email or password."
)
_INVALID_LINK = HTTPException(status_code=400, detail="Invalid or expired link.")


def _token_out(user: AppUser) -> dict:
    """Build the shared session response from the user's committed token version."""
    return {
        "access_token": create_access_token(str(user.id), user.token_version),
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


def _verification_link(raw_token: str) -> str:
    """Build a fragment-only link so the token stays out of server request logs."""
    return f"{settings.frontend_base_url}/auth/verify-email#token={raw_token}"


def _reset_link(raw_token: str) -> str:
    """Build the password-reset SPA link with its token kept in the fragment."""
    return f"{settings.frontend_base_url}/auth/reset-password#token={raw_token}"


def _set_verified_at_if_missing(user: AppUser) -> None:
    """Record proof of mailbox control once without replacing an earlier timestamp."""
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(timezone.utc)


def _apply_verified_password(user: AppUser, pending_password_hash: str | None) -> None:
    """Promote a pending hash only for a currently passwordless account.

    This is the account-takeover boundary: a verification token may add a
    first password, but can never replace an existing one.
    """
    if user.password_hash is None:
        user.password_hash = pending_password_hash
    _set_verified_at_if_missing(user)


@router.post(
    "/signup",
    response_model=VerificationSentOut,
    dependencies=[Depends(rate_limit("signup", 5, 3600))],
)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> dict:
    """Start password signup without revealing whether an account already exists.

    The route intentionally writes only an expiring token. The worker chooses
    its template later, so account existence cannot affect this response.
    """
    email = normalize_email(str(payload.email))
    raw_token = issue_token(
        db,
        purpose="verify_email",
        email=email,
        pending_password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        ttl=VERIFY_TTL,
    )
    db.commit()
    send_auth_email.delay("verify_email", email, _verification_link(raw_token))
    return {"status": "verification_sent"}


@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[Depends(rate_limit("login", 10, 60))],
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    """Authenticate a password account, returning one generic failure for all misses."""
    email = normalize_email(str(payload.email))
    # The dependency limiter cannot inspect JSON bodies, so this bucket belongs
    # here before verification rather than leaking email-specific timing.
    _enforce("login-email", email, 10, 300)
    user = db.query(AppUser).filter(AppUser.email == email).first()
    password_hash = user.password_hash if user is not None else None
    if not verify_password(payload.password, password_hash):
        raise _INVALID_CREDENTIALS

    # A successful verification lets us migrate parameters without a reset.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        db.commit()
        db.refresh(user)
    return _token_out(user)


@router.post(
    "/verify-email",
    response_model=TokenOut,
    dependencies=[Depends(rate_limit("verify-email", 10, 60))],
)
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)) -> dict:
    """Consume a verification token and add its password only onto a NULL hash.

    The token deletion, optional user creation, and promotion share a
    transaction. A concurrent create retries after the lower(email) uniqueness
    backstop resolves the race, while retaining the NULL-only invariant.
    """
    row = consume_token(db, purpose="verify_email", raw_token=payload.token)
    if row is None:
        raise _INVALID_LINK

    try:
        user = (
            db.query(AppUser)
            .filter(AppUser.email == row.email)
            .with_for_update()
            .first()
        )
        if user is None:
            user = AppUser(email=row.email, display_name=row.display_name)
            db.add(user)
        _apply_verified_password(user, row.pending_password_hash)
        db.commit()
    except IntegrityError:
        # A different verifier won the new-user race. Roll back its failed
        # insert, then consume our token again because rollback restored it.
        db.rollback()
        row = consume_token(db, purpose="verify_email", raw_token=payload.token)
        if row is None:
            raise _INVALID_LINK
        user = (
            db.query(AppUser)
            .filter(AppUser.email == row.email)
            .with_for_update()
            .first()
        )
        if user is None:
            raise _INVALID_LINK
        _apply_verified_password(user, row.pending_password_hash)
        db.commit()

    # Commit before minting so the JWT always carries the committed version.
    db.refresh(user)
    return _token_out(user)


@router.post(
    "/resend-verification",
    response_model=VerificationSentOut,
    dependencies=[Depends(rate_limit("resend-verification", 3, 3600))],
)
def resend_verification(
    payload: ResendVerificationRequest, db: Session = Depends(get_db)
) -> dict:
    """Replace a pending verification link without revealing whether one exists."""
    email = normalize_email(str(payload.email))
    pending = (
        db.query(AuthToken)
        .filter(AuthToken.email == email, AuthToken.purpose == "verify_email")
        .first()
    )
    if pending is not None:
        raw_token = issue_token(
            db,
            purpose="verify_email",
            email=email,
            user_id=pending.user_id,
            pending_password_hash=pending.pending_password_hash,
            display_name=pending.display_name,
            ttl=VERIFY_TTL,
        )
        db.commit()
        send_auth_email.delay("verify_email", email, _verification_link(raw_token))
    return {"status": "verification_sent"}


@router.post(
    "/forgot-password",
    response_model=ResetSentOut,
    dependencies=[Depends(rate_limit("forgot-password", 5, 3600))],
)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> dict:
    """Queue a reset email for every address without revealing account existence."""
    email = normalize_email(str(payload.email))
    user = db.query(AppUser).filter(AppUser.email == email).first()
    raw_token = issue_token(
        db,
        purpose="password_reset",
        email=email,
        user_id=user.id if user is not None else None,
        ttl=RESET_TTL,
    )
    db.commit()
    # The worker silently drops unknown addresses, keeping this response uniform.
    send_auth_email.delay("password_reset", email, _reset_link(raw_token))
    return {"status": "reset_sent"}


@router.post(
    "/reset-password",
    response_model=TokenOut,
    dependencies=[Depends(rate_limit("reset-password", 10, 60))],
)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> dict:
    """Consume a reset token, replace the password, and revoke every old session."""
    row = consume_token(db, purpose="password_reset", raw_token=payload.token)
    if row is None:
        raise _INVALID_LINK
    user = (
        db.query(AppUser)
        .filter(AppUser.email == row.email)
        .with_for_update()
        .first()
    )
    if user is None:
        raise _INVALID_LINK

    user.password_hash = hash_password(payload.new_password)
    _set_verified_at_if_missing(user)
    # A SQL increment prevents two concurrent resets from both writing n + 1.
    db.execute(
        update(AppUser)
        .where(AppUser.id == user.id)
        .values(token_version=AppUser.token_version + 1)
    )
    db.commit()
    db.refresh(user)
    return _token_out(user)
