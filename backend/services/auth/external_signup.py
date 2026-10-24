"""Finish external registration without creating a provisional native user."""

import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.domain.identity_policy import normalize_email
from backend.models.auth_identity import AuthLoginTransaction, UserIdentity
from backend.models.auth_verification import AuthPendingSignup
from backend.models.user import User
from backend.services.auth.account_resolver import (
    AccountResolver,
    PendingSignupRequired,
)
from backend.services.auth.events import AuthEvents
from backend.services.auth.login_transactions import AuthFlowError, FlowContext, digest
from backend.services.auth.provider_registry import AuthProvider, ProviderRegistry
from backend.services.db_write_intent import acquire_write_intent
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.session_service import SessionResult, SessionService
from backend.spi.auth import ExternalAssertion

PENDING_SIGNUP_TTL_SECONDS = 30 * 60
CONFIRMATION_RESEND_SECONDS = 60


@dataclass(frozen=True)
class PendingSignupResult:
    binding: str


@dataclass(frozen=True)
class SignupCompletion:
    session: SessionResult
    return_to: str
    provider_slug: str
    identity_id: UUID


@dataclass
class ExternalSignupService:
    session_factory: sessionmaker[Session]
    providers: ProviderRegistry
    sessions: SessionService
    mail: EmailDeliveryService
    events: AuthEvents
    now: Callable[[], datetime]

    def begin(
        self,
        session: Session,
        *,
        provider: AuthProvider,
        assertion: ExternalAssertion,
        transaction: AuthLoginTransaction,
        email: str | None,
    ) -> PendingSignupResult:
        data = asdict(assertion)
        when = data.get("authenticated_at")
        data["authenticated_at"] = when.isoformat() if when is not None else None
        if len(json.dumps(data).encode()) > 16384:
            raise AuthFlowError("sso_assertion_invalid")
        binding = secrets.token_urlsafe(32)
        now = self.now()
        expired = (
            select(AuthPendingSignup.id)
            .where(AuthPendingSignup.expires_at <= now)
            .limit(100)
        )
        session.execute(
            delete(AuthPendingSignup).where(AuthPendingSignup.id.in_(expired))
        )
        pending = AuthPendingSignup(
            id=uuid4(),
            binding_digest=digest(binding),
            provider_slug=provider.slug,
            assertion_data=data,
            email=email,
            return_to=transaction.return_to,
            remember_me=transaction.remember_me,
            created_at=now,
            expires_at=now + timedelta(seconds=PENDING_SIGNUP_TTL_SECONDS),
        )
        session.add(pending)
        session.flush()
        if email and self.mail.settings.mail_configured:
            self._queue(session, pending)
        return PendingSignupResult(binding)

    def _pending(
        self, session: Session, binding: str | None, *, lock: bool = False
    ) -> AuthPendingSignup:
        if not isinstance(binding, str) or not 32 <= len(binding) <= 128:
            raise AuthFlowError("sso_transaction_invalid")
        query = select(AuthPendingSignup).where(
            AuthPendingSignup.binding_digest == digest(binding)
        )
        if lock:
            query = query.with_for_update()
        pending = session.scalar(query)
        if (
            pending is None
            or pending.consumed_at is not None
            or pending.expires_at <= self.now()
        ):
            raise AuthFlowError("sso_transaction_invalid")
        if pending.provider_slug not in self.providers.providers:
            raise AuthFlowError("sso_provider_unavailable")
        return pending

    def status(self, binding: str | None) -> dict[str, str | bool | None]:
        with self.session_factory() as session:
            pending = self._pending(session, binding)
            return {
                "email": pending.email,
                "email_confirmation_required": self.mail.settings.mail_configured,
                "requested": pending.email_requested_at is not None,
            }

    def submit_email(
        self, *, binding: str | None, email: str
    ) -> tuple[SignupCompletion | None, str]:
        try:
            result, return_to = self._submit_email(binding=binding, email=email)
        except IntegrityError as exc:
            raise AuthFlowError("account_link_required") from exc
        if result is not None:
            self._notify(result)
        return result, return_to

    def _submit_email(
        self, *, binding: str | None, email: str
    ) -> tuple[SignupCompletion | None, str]:
        email = normalize_email(email)
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            pending = self._pending(session, binding, lock=True)
            self._validate_new_email(session, pending, email)
            if self.mail.settings.mail_configured:
                if (
                    pending.email_requested_at
                    and (self.now() - pending.email_requested_at).total_seconds()
                    < CONFIRMATION_RESEND_SECONDS
                ):
                    raise AuthFlowError("rate_limited")
                pending.email = email
                self._queue(session, pending)
                return None, pending.return_to
            pending.email = email
            return self._finish(
                session, pending, email_confirmed=False
            ), pending.return_to

    def confirm(self, *, binding: str | None, token: str) -> SignupCompletion:
        try:
            with self.session_factory.begin() as session:
                acquire_write_intent(session)
                pending = self._pending(session, binding, lock=True)
                if (
                    not isinstance(token, str)
                    or not 32 <= len(token) <= 128
                    or not hmac.compare_digest(
                        pending.token_digest or "", digest(token)
                    )
                ):
                    raise AuthFlowError("email_verification_invalid")
                result = self._finish(session, pending, email_confirmed=True)
        except IntegrityError as exc:
            raise AuthFlowError("account_link_required") from exc
        self._notify(result)
        return result

    def _notify(self, result: SignupCompletion) -> None:
        self.events.notify(
            self.providers.get(result.provider_slug),
            "on_identity_linked",
            result.provider_slug,
            result.identity_id,
        )

    def _validate_new_email(
        self, session: Session, pending: AuthPendingSignup, email: str
    ) -> None:
        provider = self.providers.get(pending.provider_slug)
        if not provider.policy.jit_provisioning or (
            provider.policy.allowed_email_domains
            and email.rsplit("@", 1)[-1] not in provider.policy.allowed_email_domains
        ):
            raise AuthFlowError("signup_not_allowed")
        if session.scalar(select(User.id).where(User.email == email)) is not None:
            raise AuthFlowError("account_link_required")

    def _queue(self, session: Session, pending: AuthPendingSignup) -> None:
        token = secrets.token_urlsafe(32)
        pending.token_digest = digest(token)
        pending.email_requested_at = self.now()
        self.mail.queue_signup_confirmation(session, pending=pending, token=token)

    def _finish(
        self, session: Session, pending: AuthPendingSignup, *, email_confirmed: bool
    ) -> SignupCompletion:
        if not pending.email:
            raise AuthFlowError("email_policy_failed")
        self._validate_new_email(session, pending, pending.email)
        data = dict(pending.assertion_data)
        when = data.get("authenticated_at")
        data["authenticated_at"] = datetime.fromisoformat(when) if when else None
        data["amr"] = tuple(data.get("amr", ()))
        data["groups"] = tuple(data.get("groups", ()))
        assertion = replace(
            ExternalAssertion(**data),
            email=pending.email,
            email_verified=bool(
                data.get("email_verified") and data.get("email") == pending.email
            ),
        )
        if (
            session.scalar(
                select(UserIdentity.id).where(
                    UserIdentity.issuer == assertion.issuer,
                    UserIdentity.subject == assertion.subject,
                )
            )
            is not None
        ):
            raise AuthFlowError("account_link_required")
        provider = self.providers.get(pending.provider_slug)
        transaction = FlowContext(purpose="login", return_to=pending.return_to)
        result = AccountResolver(
            self.sessions, self.mail.settings.mail_configured
        ).resolve(
            session,
            provider,
            assertion,
            transaction,
            self.now(),
            email_confirmed=email_confirmed,
        )
        if isinstance(result, PendingSignupRequired):
            raise AuthFlowError("email_unverified")
        user, identity, created, _ = result
        hook = getattr(provider.method, "on_login", None)
        if hook is not None:
            try:
                hook(session, user.id, provider.slug, assertion, created)
            except Exception as exc:
                raise AuthFlowError("sso_assertion_invalid") from exc
        pending.consumed_at = self.now()
        pending.token_digest = None
        issued = self.sessions.issue_session(
            user, remember_me=pending.remember_me, amr=("ext",), idp=provider.slug
        )
        self.events.user_event(
            session,
            user.id,
            "auth.user.provisioned",
            {"provider_slug": provider.slug, "identity_id": str(identity.id)},
        )
        self.events.user_event(
            session,
            user.id,
            "auth.identity.linked",
            {
                "provider_slug": provider.slug,
                "identity_id": str(identity.id),
                "linked_via": identity.linked_via,
            },
        )
        self.events.user_event(
            session,
            user.id,
            "auth.session.created",
            {"method": "sso", "provider_slug": provider.slug},
        )
        return SignupCompletion(issued, pending.return_to, provider.slug, identity.id)
