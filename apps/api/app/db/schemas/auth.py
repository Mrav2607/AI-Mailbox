from datetime import datetime
from uuid import UUID

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


class Connections(Response):
    connections: list[ConnectionOut]
