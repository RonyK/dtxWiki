"""Native session results and issuance shared by every primary login method."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from backend.models.user import User


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str | None
    display_name: str
    status: str
    email_verified_at: datetime | None = None
    has_password: bool = True


@dataclass(frozen=True)
class SessionResult:
    user: AuthenticatedUser
    session_token: str
    issued_at: datetime
    expires_at: datetime
    remember_me: bool


class NativeSessionCodec(Protocol):
    now: Callable[[], datetime]

    def issue(
        self,
        user: User,
        *,
        remember_me: bool = False,
        authenticated_at: datetime | None = None,
        issued_at: datetime | None = None,
        amr: tuple[str, ...] | None = None,
        idp: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, datetime]: ...


def authenticated_user(user: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        email_verified_at=user.email_verified_at,
        has_password=user.password_hash is not None,
    )


def issue_native_session(
    codec: NativeSessionCodec,
    user: User,
    *,
    remember_me: bool = False,
    authenticated_at: datetime | None = None,
    amr: tuple[str, ...] | None = None,
    idp: str | None = None,
    session_id: str | None = None,
) -> SessionResult:
    issued_at = codec.now().astimezone(UTC).replace(microsecond=0)
    token, expires_at = codec.issue(
        user,
        remember_me=remember_me,
        authenticated_at=authenticated_at,
        issued_at=issued_at,
        amr=amr,
        idp=idp,
        session_id=session_id,
    )
    return SessionResult(
        user=authenticated_user(user),
        session_token=token,
        issued_at=issued_at,
        expires_at=expires_at,
        remember_me=remember_me,
    )
