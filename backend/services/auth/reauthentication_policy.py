"""Choose confirmation methods from live account and provider state."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.auth_identity import UserIdentity
from backend.services import server_admin_service
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.session_service import (
    RECENT_REAUTHENTICATION_TTL_SECONDS,
    RecentReauthenticationRequiredError,
    SessionService,
    VerifiedRecentReauthentication,
    VerifiedSession,
)

VerificationScope = Literal["account", "admin"]
VerificationMethod = Literal["password", "sso", "email"]
VerificationMode = Literal["password", "sso", "email", "session"]


@dataclass(frozen=True)
class VerificationDecision:
    mode: VerificationMode
    session: VerifiedSession
    providers: tuple[str, ...] = ()
    allowed_methods: tuple[VerificationMethod, ...] = ()
    proof: VerifiedRecentReauthentication | None = None
    primary_until: datetime | None = None

    @property
    def reauthenticated_until(self) -> datetime | None:
        deadlines = [
            value
            for value in (
                self.primary_until,
                self.proof.expires_at if self.proof else None,
            )
            if value is not None
        ]
        return min(self.session.expires_at, max(deadlines)) if deadlines else None

    @property
    def verified(self) -> bool:
        return self.reauthenticated_until is not None


@dataclass(frozen=True)
class ReauthenticationPolicy:
    session_factory: sessionmaker[Session]
    sessions: SessionService
    providers: ProviderRegistry
    mail_configured: bool
    max_age_seconds: int = RECENT_REAUTHENTICATION_TTL_SECONDS

    def inspect(
        self, *, session_token: str, scope: VerificationScope, proof_token: str = ""
    ) -> VerificationDecision:
        if scope not in {"account", "admin"}:
            raise ValueError("unknown verification purpose")
        user, current = self.sessions.authenticate_session_context(session_token)
        with self.session_factory() as session:
            if scope == "admin" and not server_admin_service.is_server_admin(
                session, user.user_id
            ):
                raise AuthFlowError("server_admin_required")
            linked = set(
                session.scalars(
                    select(UserIdentity.provider_slug).where(
                        UserIdentity.user_id == user.user_id
                    )
                )
            )
        capable = tuple(sorted(linked.intersection(self.providers.providers)))
        methods: list[VerificationMethod] = []
        if user.has_password and user.email:
            methods.append("password")
        if capable:
            methods.append("sso")
        # Native email enrollment is the trust boundary. A legacy native email
        # remains eligible even if SMTP was disabled when it was registered.
        if self.mail_configured and user.email:
            methods.append("email")
        external = "ext" in (current.amr or ())
        if external and "sso" in methods:
            mode: VerificationMode = "sso"
        elif methods:
            mode = methods[0]
        else:
            mode = "session"
        now = self.sessions.token_codec.now()
        primary_until = None
        if current.authenticated_at is not None and current.authenticated_at <= now:
            deadline = min(
                current.expires_at,
                current.authenticated_at + timedelta(seconds=self.max_age_seconds),
            )
            if now < deadline:
                primary_until = deadline
        proof = None
        if proof_token:
            try:
                proof = self.sessions.verify_recent_reauthentication(
                    proof_token=proof_token,
                    user_id=user.user_id,
                    scope=scope,
                    session_token=session_token,
                )
            except RecentReauthenticationRequiredError:
                pass
        if not methods and primary_until is None and proof is None:
            raise RecentReauthenticationRequiredError("a primary login is required")
        return VerificationDecision(
            mode="session" if primary_until is not None else mode,
            session=current,
            providers=capable,
            allowed_methods=tuple(methods),
            proof=proof,
            primary_until=primary_until,
        )

    def require(
        self, *, session_token: str, scope: VerificationScope, proof_token: str = ""
    ) -> VerifiedRecentReauthentication:
        decision = self.inspect(
            session_token=session_token, scope=scope, proof_token=proof_token
        )
        if not decision.verified:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication is required"
            )
        return VerifiedRecentReauthentication(
            user_id=decision.session.user_id,
            session_generation=decision.session.session_generation,
            scope="primary" if decision.primary_until else decision.proof.scope,
            session_digest=self.sessions.session_binding(session_token),
            expires_at=decision.reauthenticated_until,
        )
