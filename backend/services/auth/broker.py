"""Core orchestration for login, explicit linking and primary reauthentication."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from backend.services.auth.account_resolver import AccountResolver
from backend.services.auth.events import AuthEvents
from backend.services.auth.login_transactions import (
    AuthFlowError,
    LoginTransactionStore,
    flow_secrets,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.session_service import (
    RecentReauthenticationResult,
    SessionResult,
)
from backend.spi.auth import CallbackInput


@dataclass(frozen=True)
class Completion:
    return_to: str
    session: SessionResult | None = None
    reauthentication: RecentReauthenticationResult | None = None


class AuthBroker:
    def __init__(self, session_factory, providers, sessions, *, now=None, events=None):
        self.session_factory = session_factory
        self.providers = providers
        self.sessions = sessions
        self.now = now or (lambda: datetime.now(UTC))
        self.transactions = LoginTransactionStore(session_factory, self.now)
        self.resolver = AccountResolver(sessions)
        self.events = events if events is not None else AuthEvents(session_factory)

    def provider(self, slug):
        try:
            return self.providers.get(slug)
        except KeyError as exc:
            raise AuthFlowError("sso_provider_unavailable") from exc

    def complete(self, slug, *, state, binding, query, form=None, current_session=None):
        provider = self.provider(slug)
        transaction = self.transactions.consume(provider, state=state, binding=binding)
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
                    user, identity, created, linked = self.resolver.resolve(
                        session,
                        provider,
                        assertion,
                        transaction,
                        now,
                    )
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

    def audit_failure(self, slug, reason):
        if slug in self.providers.providers:
            self.events.login_failed(slug, reason)
