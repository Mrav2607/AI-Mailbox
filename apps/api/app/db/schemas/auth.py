from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import EmailStr, Field

from .common import Response


class UserOut(Response):
    id: UUID
    email: str
    # Google sign-in doesn't collect one, so it stays optional rather than each
    # login path returning a differently-shaped user.
    display_name: str | None = None


class Providers(Response):
    providers: list[str]


class AuthUrl(Response):
    auth_url: str


class ConnectResult(Response):
    status: Literal["connected"]
    provider_email: str


class TokenOut(Response):
    access_token: str
    token_type: str
    user: UserOut


class RevokeOut(Response):
    status: str


class ConnectionOut(Response):
    id: UUID
    provider: str
    created_at: datetime
    email_address: str
    reauth_required: bool
    label_sync_enabled: bool
    # Drift-count "syncing... N remaining" line (plan §3.4/§3.6). None when
    # sync is off -- there's nothing converging to report, and it lets a
    # client tell "off" apart from "fully converged" (0).
    label_sync_drift: int | None = None


class Connections(Response):
    connections: list[ConnectionOut]


class UpdateConnectionRequest(Response):
    label_sync_enabled: bool


class SignupRequest(Response):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = None


class LoginRequest(Response):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class VerifyEmailRequest(Response):
    token: str


class ResendVerificationRequest(Response):
    email: EmailStr


class ForgotPasswordRequest(Response):
    email: EmailStr


class ResetPasswordRequest(Response):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class VerificationSentOut(Response):
    status: Literal["verification_sent"]


class ResetSentOut(Response):
    status: Literal["reset_sent"]
