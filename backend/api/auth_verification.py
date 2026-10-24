"""HTTP delivery for browser-bound signup and purpose-bound confirmation."""

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
    session_payload,
    set_no_store_headers,
    set_recent_reauthentication_cookie,
    set_session_cookie,
)
from backend.api.auth_cookies import (
    PENDING_SIGNUP_COOKIE,
    account_reauthentication_cookie_name,
    set_account_reauthentication_cookie,
)
from backend.api.auth_sso import SignupDep, flow_error
from backend.api.cookie_budget import set_bounded_cookie
from backend.api.deps import SessionFactoryDep
from backend.api.request_origin import require_same_origin
from backend.schemas.auth_verification import (
    ConfirmationTokenRequest,
    EmailReauthenticationConfirmRequest,
    EmailReauthenticationConfirmResponse,
    EmailReauthenticationRequest,
    EmailReauthenticationRequestResponse,
    PendingSignupConfirmResponse,
    PendingSignupEmailRequest,
    PendingSignupEmailResponse,
    PendingSignupResponse,
    VerificationResponse,
    VerificationScope,
)
from backend.services.auth.email_reauthentication import (
    EMAIL_REAUTHENTICATION_TTL_SECONDS,
    EmailReauthenticationService,
)
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.auth.reauthentication_policy import ReauthenticationPolicy
from backend.services.auth.return_path import safe_return_path
from backend.services.mail_delivery import build_email_delivery_service
from backend.services.session_service import (
    SessionService,
    SessionServiceError,
    consume_auth_slot,
)

router = APIRouter(prefix="/auth", tags=["auth"])
EMAIL_BINDING_PATH = "/api/v1/auth/email-reauthentication"


def policy_for_request(
    request: Request, sessions: SessionService
) -> ReauthenticationPolicy:
    return ReauthenticationPolicy(
        sessions.session_factory,
        sessions,
        request.app.state.auth_providers,
        request.app.state.settings.mail_configured,
    )


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
                "reauthenticated_until": decision.proof.expires_at
                if decision.proof
                else None,
                "providers": [p for p in providers if p["slug"] in decision.providers],
            }
        }
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    except AuthFlowError as exc:
        return flow_error(exc)


@router.get("/pending-signups", response_model=PendingSignupResponse)
def pending_signup(request: Request, response: Response, signup: SignupDep):
    try:
        set_no_store_headers(response)
        return {"data": signup.status(request.cookies.get(PENDING_SIGNUP_COOKIE))}
    except AuthFlowError as exc:
        return flow_error(exc)


@router.post("/pending-signups/email", response_model=PendingSignupEmailResponse)
def pending_email(
    body: PendingSignupEmailRequest,
    request: Request,
    response: Response,
    signup: SignupDep,
):
    require_same_origin(request)
    try:
        consume_auth_slot(auth_client_ip(request), body.email)
        completed, return_to = signup.submit_email(
            binding=request.cookies.get(PENDING_SIGNUP_COOKIE), email=body.email
        )
        set_no_store_headers(response)
        if completed is not None:
            set_session_cookie(
                response, request.app.state.settings, request, completed.session
            )
        return {
            "data": {
                "status": "complete" if completed else "email_sent",
                "session": session_payload(completed.session) if completed else None,
                "return_to": return_to,
            }
        }
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))


@router.post("/pending-signups/confirm", response_model=PendingSignupConfirmResponse)
def pending_confirm(
    body: ConfirmationTokenRequest,
    request: Request,
    response: Response,
    signup: SignupDep,
):
    require_same_origin(request)
    try:
        consume_auth_slot(auth_client_ip(request), "")
        result = signup.confirm(
            binding=request.cookies.get(PENDING_SIGNUP_COOKIE), token=body.token
        )
        set_session_cookie(
            response, request.app.state.settings, request, result.session
        )
        set_no_store_headers(response)
        return {
            "data": {
                "session": session_payload(result.session),
                "return_to": result.return_to,
            }
        }
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))


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
        consume_auth_slot(auth_client_ip(request), "")
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
        consume_auth_slot(auth_client_ip(request), "")
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
