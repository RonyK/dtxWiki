"""HTTP delivery for purpose-bound account confirmation."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from backend.api.auth import (
    SessionServiceDep,
    auth_client_ip,
    auth_error_response,
    current_session_token,
    no_store_response,
    recent_reauthentication_cookie_name,
    session_cookie_secure_for_request,
    set_no_store_headers,
    set_recent_reauthentication_cookie,
)
from backend.api.auth_cookies import (
    account_reauthentication_cookie_name,
    set_account_reauthentication_cookie,
)
from backend.api.auth_dependencies import flow_error, policy_for_request
from backend.api.cookie_budget import set_bounded_cookie
from backend.api.deps import SessionFactoryDep
from backend.api.request_origin import require_same_origin
from backend.schemas.auth_verification import (
    EmailReauthenticationConfirmRequest,
    EmailReauthenticationConfirmResponse,
    EmailReauthenticationRequest,
    EmailReauthenticationRequestResponse,
    VerificationResponse,
    VerificationScope,
)
from backend.services.auth.email_reauthentication import (
    EMAIL_REAUTHENTICATION_TTL_SECONDS,
    EmailReauthenticationService,
)
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.return_path import safe_return_path
from backend.services.mail_delivery import build_email_delivery_service
from backend.services.session_service import (
    SessionServiceError,
    consume_auth_slot,
)

router = APIRouter(prefix="/auth", tags=["auth"])
EMAIL_BINDING_PATH = "/api/v1/auth/email-reauthentication"


def get_email(
    request: Request, factory: SessionFactoryDep, sessions: SessionServiceDep
) -> EmailReauthenticationService:
    return EmailReauthenticationService(
        factory,
        sessions,
        policy_for_request(request, sessions),
        build_email_delivery_service(request.app.state.settings),
        sessions.token_codec.now,
    )


EmailDep = Annotated[EmailReauthenticationService, Depends(get_email)]


def email_binding_name(scope: VerificationScope) -> str:
    return f"dtxwiki_email_reauth_{scope}"


@router.get("/verification", response_model=VerificationResponse)
def verification(
    request: Request,
    response: Response,
    sessions: SessionServiceDep,
    scope: VerificationScope = "account",
    return_to: str = Query(default="/account", max_length=2048),
):
    try:
        cookie = (
            recent_reauthentication_cookie_name(request.app.state.settings)
            if scope == "admin"
            else account_reauthentication_cookie_name(request.app.state.settings)
        )
        proof = request.cookies.get(cookie) or request.cookies.get(
            account_reauthentication_cookie_name(request.app.state.settings), ""
        )
        decision = policy_for_request(request, sessions).inspect(
            session_token=current_session_token(request),
            scope=scope,
            proof_token=proof,
        )
        providers = request.app.state.auth_providers.login_methods()
        set_no_store_headers(response)
        return {
            "data": {
                "scope": scope,
                "mode": decision.mode,
                "verified": decision.verified,
                "reauthenticated_until": decision.reauthenticated_until,
                "allowed_methods": decision.allowed_methods,
                "providers": [p for p in providers if p["slug"] in decision.providers],
            }
        }
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    except AuthFlowError as exc:
        return flow_error(exc)


@router.post(
    "/email-reauthentication/request",
    response_model=EmailReauthenticationRequestResponse,
)
def request_email(
    body: EmailReauthenticationRequest,
    request: Request,
    response: Response,
    email: EmailDep,
):
    require_same_origin(request)
    try:
        user = email.sessions.authenticate_session(current_session_token(request))
        consume_auth_slot(auth_client_ip(request), user_id=user.user_id)
        binding = email.request(
            session_token=current_session_token(request),
            scope=body.scope,
            return_to=safe_return_path(body.return_to),
        )
        set_bounded_cookie(
            response,
            key=email_binding_name(body.scope),
            value=binding,
            httponly=True,
            secure=session_cookie_secure_for_request(
                request.app.state.settings, request
            ),
            samesite="strict",
            path=EMAIL_BINDING_PATH,
            max_age=EMAIL_REAUTHENTICATION_TTL_SECONDS,
        )
        set_no_store_headers(response)
        return {"data": {"status": "email_sent"}}
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))


@router.post(
    "/email-reauthentication/confirm",
    response_model=EmailReauthenticationConfirmResponse,
)
def confirm_email(
    body: EmailReauthenticationConfirmRequest,
    request: Request,
    response: Response,
    email: EmailDep,
):
    require_same_origin(request)
    try:
        user = email.sessions.authenticate_session(current_session_token(request))
        consume_auth_slot(auth_client_ip(request), user_id=user.user_id)
        proof, return_to = email.confirm(
            session_token=current_session_token(request),
            scope=body.scope,
            binding=request.cookies.get(email_binding_name(body.scope)),
            token=body.token,
        )
        if body.scope == "admin":
            set_recent_reauthentication_cookie(
                response=response,
                settings=request.app.state.settings,
                request=request,
                result=proof,
            )
        else:
            set_account_reauthentication_cookie(
                response,
                request.app.state.settings,
                secure=session_cookie_secure_for_request(
                    request.app.state.settings, request
                ),
                result=proof,
            )
        set_no_store_headers(response)
        return {
            "data": {
                "reauthenticated_until": proof.reauthenticated_until,
                "return_to": return_to,
            }
        }
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
