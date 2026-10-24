"""Transactional account login-method management behind the HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.auth_identity import UserIdentity
from backend.models.user import User
from backend.services.auth.events import AuthEvents
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.db_write_intent import acquire_write_intent
from backend.services.session_service import (
    AuthenticatedUser,
    SessionResult,
    SessionService,
    VerifiedSession,
    validate_password,
)


class IdentityNotFoundError(RuntimeError):
    """The authenticated account does not own this identity."""


@dataclass
class AuthAccountService:
    session_factory: sessionmaker[Session]
    providers: ProviderRegistry
    sessions: SessionService
    events: AuthEvents

    def list_identities(
        self, user: AuthenticatedUser, *, password_login_enabled: bool
    ) -> dict:
        with self.session_factory() as session:
            rows = session.scalars(
                select(UserIdentity).where(UserIdentity.user_id == user.user_id)
            ).all()
            enabled_count = int(user.has_password and password_login_enabled) + sum(
                row.provider_slug in self.providers.providers for row in rows
            )
            return {
                "has_password": user.has_password,
                "identities": [
                    {
                        "id": row.id,
                        "provider_slug": row.provider_slug,
                        "last_email": row.last_email,
                        "last_login_at": row.last_login_at.isoformat()
                        if row.last_login_at
                        else None,
                        "can_unlink": enabled_count
                        - int(row.provider_slug in self.providers.providers)
                        > 0,
                    }
                    for row in rows
                ],
                "available_providers": self.providers.login_methods(),
            }

    def unlink_identity(
        self,
        user: AuthenticatedUser,
        verified: VerifiedSession,
        identity_id: UUID,
        *,
        password_login_enabled: bool,
    ) -> SessionResult:
        with self.session_factory.begin() as session:
            live = _locked_user(session, user, verified)
            rows = session.scalars(
                select(UserIdentity)
                .where(UserIdentity.user_id == user.user_id)
                .with_for_update()
            ).all()
            target = next((row for row in rows if row.id == identity_id), None)
            if target is None:
                raise IdentityNotFoundError()
            remaining = int(
                live.password_hash is not None and password_login_enabled
            ) + sum(
                row.id != identity_id and row.provider_slug in self.providers.providers
                for row in rows
            )
            if not remaining:
                raise AuthFlowError("last_login_method")
            slug = target.provider_slug
            session.delete(target)
            live.session_generation += 1
            issued = _issue_replacement(self.sessions, live, verified)
            self.events.user_event(
                session,
                live.id,
                "auth.identity.unlinked",
                {"provider_slug": slug, "identity_id": str(identity_id), "by": "self"},
            )
        if slug in self.providers.providers:
            self.events.notify(
                self.providers.get(slug), "on_identity_unlinked", slug, identity_id
            )
        return issued

    def set_initial_password(
        self,
        user: AuthenticatedUser,
        verified: VerifiedSession,
        password: str,
    ) -> SessionResult:
        validate_password(password)
        # Argon2 must not hold SQLite's database-wide writer lock. The locked
        # transaction below rechecks every state used to authorize the write.
        password_hash = self.sessions.credential_hasher.hash_password(password)
        with self.session_factory.begin() as session:
            live = _locked_user(session, user, verified)
            if live.password_hash is not None:
                raise AuthFlowError("password_already_set")
            live.password_hash = password_hash
            live.session_generation += 1
            issued = _issue_replacement(self.sessions, live, verified)
            self.events.user_event(session, live.id, "auth.password.set", {})
        return issued


def _locked_user(session, user, verified):
    acquire_write_intent(session)
    live = session.scalar(select(User).where(User.id == user.user_id).with_for_update())
    if (
        live is None
        or live.status != "active"
        or live.session_generation != verified.session_generation
    ):
        raise AuthFlowError("sso_transaction_invalid")
    return live


def _issue_replacement(sessions, live, verified):
    return sessions.issue_session(
        live,
        remember_me=verified.remember_me,
        authenticated_at=verified.authenticated_at,
        amr=verified.amr,
        idp=verified.idp,
    )
