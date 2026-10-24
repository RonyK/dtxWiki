"""Choose one confirmation method from live account and provider state."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.auth_identity import UserIdentity
from backend.services import server_admin_service
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.session_service import (
    RecentReauthenticationRequiredError,
    SessionService,
    VerifiedRecentReauthentication,
    VerifiedSession,
)

VerificationScope = Literal["account", "admin"]
VerificationMode = Literal["password", "sso", "email", "session"]


@dataclass(frozen=True)
class VerificationDecision:
    mode: VerificationMode
    session: VerifiedSession
    providers: tuple[str, ...] = ()
    proof: VerifiedRecentReauthentication | None = None

    @property
    def verified(self) -> bool:
        return self.mode == "session" or self.proof is not None


@dataclass(frozen=True)
class ReauthenticationPolicy:
    session_factory: sessionmaker[Session]
    sessions: SessionService
    providers: ProviderRegistry
    mail_configured: bool

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
        # Unlinking the source identity rotates the native session but keeps
        # its original authentication claims. Remaining linked methods still
        # make this a valid external session for account confirmation.
        external = "ext" in (current.amr or ()) and bool(linked)
        methods = self.providers.providers
        capable = tuple(
            slug
            for slug in linked
            if slug in methods
            and methods[slug].method.descriptor.capabilities.reauthentication
        )
        if external:
            mode = "sso" if capable else "email" if self.mail_configured else "session"
        elif user.has_password:
            mode = "password"
        else:
            raise RecentReauthenticationRequiredError("a primary login is required")
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
        return VerificationDecision(mode, current, capable, proof)

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
        # Current-session acceptance is a policy decision; it issues no new proof.
        return decision.proof or VerifiedRecentReauthentication(
            decision.session.user_id,
            decision.session.session_generation,
            expires_at=decision.session.expires_at,
        )
