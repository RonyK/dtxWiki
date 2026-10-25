"""Single-use email confirmation bound to one live session and purpose."""

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.auth_verification import EmailReauthenticationChallenge
from backend.models.user import User
from backend.services import server_admin_service
from backend.services.auth.login_transactions import AuthFlowError, digest
from backend.services.auth.reauthentication_policy import (
    ReauthenticationPolicy,
    VerificationScope,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.session_service import (
    RecentReauthenticationResult,
    SessionService,
    VerifiedSession,
)

EMAIL_REAUTHENTICATION_TTL_SECONDS = 5 * 60
CONFIRMATION_RESEND_SECONDS = 60


@dataclass
class EmailReauthenticationService:
    session_factory: sessionmaker[Session]
    sessions: SessionService
    policy: ReauthenticationPolicy
    mail: EmailDeliveryService
    now: Callable[[], datetime]

    def _user(
        self, session: Session, current: VerifiedSession, scope: VerificationScope
    ) -> User:
        user = session.scalar(
            select(User).where(User.id == current.user_id).with_for_update()
        )
        if (
            user is None
            or user.status != "active"
            or not user.email
            or user.session_generation != current.session_generation
        ):
            raise AuthFlowError("sso_transaction_invalid")
        if scope == "admin" and not server_admin_service.is_server_admin(
            session, user.id
        ):
            raise AuthFlowError("server_admin_required")
        return user

    def request(
        self, *, session_token: str, scope: VerificationScope, return_to: str
    ) -> str:
        decision = self.policy.inspect(session_token=session_token, scope=scope)
        if "email" not in decision.allowed_methods:
            raise AuthFlowError("recent_reauthentication_required")
        binding, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.now()
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            user = self._user(session, decision.session, scope)
            latest = session.scalar(
                select(EmailReauthenticationChallenge)
                .where(EmailReauthenticationChallenge.user_id == user.id)
                .order_by(EmailReauthenticationChallenge.created_at.desc())
                .limit(1)
            )
            if (
                latest
                and (now - latest.created_at).total_seconds()
                < CONFIRMATION_RESEND_SECONDS
            ):
                raise AuthFlowError("rate_limited")
            expired = (
                select(EmailReauthenticationChallenge.id)
                .where(EmailReauthenticationChallenge.expires_at <= now)
                .limit(100)
            )
            session.execute(
                delete(EmailReauthenticationChallenge).where(
                    EmailReauthenticationChallenge.id.in_(expired)
                )
            )
            for old in session.scalars(
                select(EmailReauthenticationChallenge).where(
                    EmailReauthenticationChallenge.user_id == user.id,
                    EmailReauthenticationChallenge.scope == scope,
                    EmailReauthenticationChallenge.consumed_at.is_(None),
                    EmailReauthenticationChallenge.revoked_at.is_(None),
                )
            ):
                old.revoked_at = now
            challenge = EmailReauthenticationChallenge(
                id=uuid4(),
                user_id=user.id,
                session_generation=user.session_generation,
                session_digest=self.sessions.session_binding(session_token),
                binding_digest=digest(binding),
                recipient_digest=digest(user.email),
                token_digest=digest(token),
                scope=scope,
                return_to=return_to,
                created_at=now,
                expires_at=now + timedelta(seconds=EMAIL_REAUTHENTICATION_TTL_SECONDS),
            )
            session.add(challenge)
            session.flush()
            self.mail.queue_reauthentication_email(
                session, user=user, challenge=challenge, token=token
            )
        return binding

    def confirm(
        self,
        *,
        session_token: str,
        scope: VerificationScope,
        binding: str | None,
        token: str,
    ) -> tuple[RecentReauthenticationResult, str]:
        _, current = self.sessions.authenticate_session_context(session_token)
        if not all(
            isinstance(value, str) and 32 <= len(value) <= 128
            for value in (binding, token)
        ):
            raise AuthFlowError("email_verification_invalid")
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            user = self._user(session, current, scope)
            challenge = session.scalar(
                select(EmailReauthenticationChallenge)
                .where(EmailReauthenticationChallenge.token_digest == digest(token))
                .with_for_update()
            )
            if (
                challenge is None
                or challenge.user_id != user.id
                or challenge.scope != scope
                or challenge.session_generation != user.session_generation
                or challenge.consumed_at is not None
                or challenge.revoked_at is not None
                or challenge.expires_at <= self.now()
                or not hmac.compare_digest(challenge.binding_digest, digest(binding))
                or not hmac.compare_digest(
                    challenge.session_digest,
                    self.sessions.session_binding(session_token),
                )
                or not hmac.compare_digest(
                    challenge.recipient_digest, digest(user.email)
                )
            ):
                raise AuthFlowError("email_verification_invalid")
            challenge.consumed_at = self.now()
            proof = self.sessions.issue_external_reauthentication(
                session,
                user_id=user.id,
                session_generation=user.session_generation,
                scope=scope,
                session_token=session_token,
            )
            return proof, challenge.return_to
