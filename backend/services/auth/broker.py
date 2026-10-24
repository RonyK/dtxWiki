"""Core orchestration for login, explicit linking and primary reauthentication."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.models.auth_identity import AuthLoginTransaction
from backend.services.auth.account_resolver import (
    AccountResolver,
    PendingSignupRequired,
)
from backend.services.auth.events import AuthEvents
from backend.services.auth.external_signup import (
    ExternalSignupService,
    PendingSignupResult,
)
from backend.services.auth.login_transactions import (
    AuthFlowError,
    FlowContext,
    LoginTransactionStore,
    flow_secrets,
)
from backend.services.auth.provider_registry import AuthProvider, ProviderRegistry
from backend.services.db_write_intent import acquire_write_intent
from backend.services.session_service import (
    RecentReauthenticationResult,
    SessionResult,
    SessionService,
    SessionServiceError,
    VerifiedSession,
)
from backend.spi.auth import CallbackInput


@dataclass(frozen=True)
class Completion:
    return_to: str
    session: SessionResult | None = None
    reauthentication: RecentReauthenticationResult | None = None
    pending_signup: PendingSignupResult | None = None


class AuthBroker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        providers: ProviderRegistry,
        sessions: SessionService,
        *,
        now: Callable[[], datetime] | None = None,
        events: AuthEvents | None = None,
        signups: ExternalSignupService | None = None,
        mail_configured: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.sessions = sessions
        self.now = now or sessions.token_codec.now
        self.transactions = LoginTransactionStore(session_factory, self.now)
        self.events = events if events is not None else AuthEvents(session_factory)
        self.resolver = AccountResolver(sessions, mail_configured)
        self.signups = signups

    def provider(self, slug: str) -> AuthProvider:
        try:
            return self.providers.get(slug)
        except KeyError as exc:
            raise AuthFlowError("sso_provider_unavailable") from exc

    def complete(
        self,
        slug: str,
        *,
        state: str | None,
        binding: str | None,
        query: Mapping[str, str],
        form: Mapping[str, str] | None = None,
        current_session: VerifiedSession | None = None,
    ) -> Completion:
        provider = self.provider(slug)
        transaction = self.transactions.consume(provider, state=state, binding=binding)
        context = FlowContext.from_transaction(transaction)
        try:
            return self._complete(
                provider, transaction, binding, query, form, current_session
            )
        except AuthFlowError as exc:
            exc.context = context
            raise
        except SessionServiceError as exc:
            raise AuthFlowError(exc.error_code, context) from exc
        except Exception as exc:
            raise AuthFlowError("sso_provider_unavailable", context) from exc

    def _complete(
        self,
        provider: AuthProvider,
        transaction: AuthLoginTransaction,
        binding: str,
        query: Mapping[str, str],
        form: Mapping[str, str] | None,
        current_session: VerifiedSession | None,
    ) -> Completion:
        slug = provider.slug
        if transaction.purpose != "login" and (
            current_session is None
            or current_session.user_id != transaction.user_id
            or current_session.session_generation != transaction.session_generation
        ):
            raise AuthFlowError("sso_transaction_invalid")
        if query.get("error") or (form and form.get("error")):
            raise AuthFlowError("sso_idp_error")
        nonce, verifier = flow_secrets(binding)
        caps = provider.method.descriptor.capabilities
        try:
            assertion = provider.method.complete(
                provider.context,
                CallbackInput(
                    http_method="POST" if form is not None else "GET",
                    query=query,
                    form=form or {},
                    expected_nonce=nonce if caps.nonce == "supported" else None,
                    pkce_verifier=verifier if caps.pkce != "unsupported" else None,
                    protocol_request_id=transaction.protocol_request_id,
                ),
            )
        except Exception as exc:
            raise AuthFlowError("sso_assertion_invalid") from exc
        for attempt in range(2):
            try:
                with self.session_factory.begin() as session:
                    acquire_write_intent(session)
                    now = self.now()
                    resolved = self.resolver.resolve(
                        session,
                        provider,
                        assertion,
                        transaction,
                        now,
                    )
                    if isinstance(resolved, PendingSignupRequired):
                        if self.signups is None:
                            raise AuthFlowError("sso_provider_unavailable")
                        pending = self.signups.begin(
                            session,
                            provider=provider,
                            assertion=assertion,
                            transaction=transaction,
                            email=resolved.email,
                        )
                        return Completion("/auth/signup/verify", pending_signup=pending)
                    user, identity, created, linked = resolved
                    if transaction.purpose == "reauth":
                        when = assertion.authenticated_at
                        if (
                            not caps.reauthentication
                            or when is None
                            or when.tzinfo is None
                            or when < transaction.created_at - timedelta(seconds=60)
                            or when > now + timedelta(seconds=60)
                        ):
                            raise AuthFlowError("reauthentication_mismatch")
                        proof = self.sessions.issue_external_reauthentication(
                            session,
                            user_id=user.id,
                            session_generation=transaction.session_generation,
                        )
                        result = Completion(
                            transaction.return_to, reauthentication=proof
                        )
                    else:
                        hook = getattr(provider.method, "on_login", None)
                        if hook is not None:
                            try:
                                hook(
                                    session, user.id, provider.slug, assertion, created
                                )
                            except Exception:
                                raise AuthFlowError("sso_assertion_invalid") from None
                        issued = (
                            self.sessions.issue_session(
                                user,
                                remember_me=transaction.remember_me,
                                amr=("ext",),
                                idp=provider.slug,
                            )
                            if transaction.purpose == "login"
                            else None
                        )
                        if issued is not None:
                            self.events.user_event(
                                session,
                                user.id,
                                "auth.session.created",
                                {"method": "sso", "provider_slug": slug},
                            )
                        result = Completion(transaction.return_to, session=issued)
                    if linked:
                        self.events.user_event(
                            session,
                            user.id,
                            "auth.identity.linked",
                            {
                                "provider_slug": slug,
                                "identity_id": str(identity.id),
                                "linked_via": identity.linked_via,
                            },
                        )
                    if created:
                        self.events.user_event(
                            session,
                            user.id,
                            "auth.user.provisioned",
                            {
                                "provider_slug": slug,
                                "identity_id": str(identity.id),
                            },
                        )
                    identity_id = identity.id
                if linked:
                    self.events.notify(
                        provider, "on_identity_linked", slug, identity_id
                    )
                return result
            except IntegrityError as exc:
                if attempt:
                    raise AuthFlowError("identity_already_linked") from exc
        raise AuthFlowError("sso_assertion_invalid")

    def audit_failure(self, slug: str, reason: str) -> None:
        if slug in self.providers.providers:
            self.events.login_failed(slug, reason)
