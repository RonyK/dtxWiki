"""Shared HTTP authentication dependencies, independent of auth routers."""

from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse

from backend.api.deps import SessionFactoryDep
from backend.app.errors import build_rest_error_response
from backend.services.audit_service import AuditService
from backend.services.auth.account_management import AuthAccountService
from backend.services.auth.broker import AuthBroker
from backend.services.auth.events import AuthEvents
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.reauthentication_policy import ReauthenticationPolicy
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import SessionService, build_session_token_codec


def get_session_service(
    request: Request,
    session_factory: SessionFactoryDep,
) -> SessionService:
    if hasattr(request.app.state, "session_service"):
        return request.app.state.session_service
    settings = request.app.state.settings
    service = SessionService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=build_session_token_codec(settings),
        app_mode=settings.app_mode,
        signup_enabled=settings.effective_signup_enabled,
        audit_service=AuditService(),
        reauthentication_max_age_seconds=settings.auth_reauthentication_max_age_seconds,
    )
    request.app.state.session_service = service
    return service


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]


def set_no_store_headers(response: Response) -> None:
    """Mark a response uncacheable.

    Applies to the injected sub-response of a handler that returns a plain
    dict. FastAPI merges those headers into the final response only on that
    path -- see :func:`no_store_response` for handlers that return a Response
    object directly.
    """

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def no_store_response[ResponseT: Response](response: ResponseT) -> ResponseT:
    """Mark a directly-returned Response uncacheable.

    When a handler returns a Response instance, FastAPI skips the
    ``sub_response.headers`` merge entirely, so headers set on the injected
    ``response`` parameter are silently dropped. Every early ``return`` of an
    error response therefore has to carry the directives itself.
    """

    set_no_store_headers(response)
    return response


def get_auth_events(session_factory: SessionFactoryDep) -> AuthEvents:
    return AuthEvents(session_factory)


AuthEventsDep = Annotated[AuthEvents, Depends(get_auth_events)]


def get_broker(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
) -> AuthBroker:
    return AuthBroker(
        session_factory, request.app.state.auth_providers, sessions, events=events
    )


BrokerDep = Annotated[AuthBroker, Depends(get_broker)]


def get_accounts(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
) -> AuthAccountService:
    return AuthAccountService(
        session_factory, request.app.state.auth_providers, sessions, events
    )


AccountServiceDep = Annotated[AuthAccountService, Depends(get_accounts)]


def policy_for_request(
    request: Request, sessions: SessionService
) -> ReauthenticationPolicy:
    return ReauthenticationPolicy(
        sessions.session_factory,
        sessions,
        request.app.state.auth_providers,
        request.app.state.settings.mail_configured,
        max_age_seconds=request.app.state.settings.auth_reauthentication_max_age_seconds,
    )


def flow_error(exc: AuthFlowError) -> JSONResponse:
    status = {
        "last_login_method": 409,
        "identity_already_linked": 409,
        "rate_limited": 429,
        "server_admin_required": 403,
    }.get(exc.code, 400)
    return no_store_response(
        build_rest_error_response(
            exc.code,
            "The external authentication operation could not be completed.",
            status_code=status,
        )
    )
