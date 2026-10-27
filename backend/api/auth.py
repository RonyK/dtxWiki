import time
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.api.auth_cookies import (
    ADMIN_PROOF_PATHS,
    account_reauthentication_cookie_name,
    clear_reauthentication_cookies,
    set_account_reauthentication_cookie,
)
from backend.api.auth_dependencies import (
    SessionServiceDep as SessionServiceDep,
)
from backend.api.auth_dependencies import (
    get_session_service as get_session_service,
)
from backend.api.auth_dependencies import (
    no_store_response as no_store_response,
)
from backend.api.auth_dependencies import (
    policy_for_request,
)
from backend.api.auth_dependencies import (
    set_no_store_headers as set_no_store_headers,
)
from backend.api.deps import SessionFactoryDep
from backend.app.errors import build_rest_error_response
from backend.app.settings import Settings
from backend.policy.remote_ip import resolve_remote_ip
from backend.schemas.auth import (
    ApiTokenCreateRequest,
    ApiTokenDeleteResponse,
    ApiTokenIssueResponse,
    ApiTokenListResponse,
    ApiTokenRevokeResponse,
    ApiTokenUpdateRequest,
    AuthCapabilitiesResponse,
    AuthLogoutResponse,
    AuthMeResponse,
    AuthSessionResponse,
    CredentialWorkspacePage,
    EmailVerificationConfirmRequest,
    EmailVerificationRequest,
    EmailVerificationResponse,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    PasswordResetStatusResponse,
    ReauthenticationRequest,
    ReauthenticationResponse,
    SignupRequest,
)
from backend.schemas.common import RESTErrorBody
from backend.services import deployment as deployment_service
from backend.services import server_admin_service
from backend.services.api_token_service import (
    ApiTokenNotFoundError,
    ApiTokenRecord,
    ApiTokenService,
    ApiTokenServiceError,
    ApiTokenWorkspaceAccessError,
    InvalidApiTokenRequestError,
    WorkspaceGrantRequest,
)
from backend.services.audit_service import AuditService
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.password_recovery_service import (
    EMAIL_VERIFICATION_RESEND_RATE_LIMIT_PER_EMAIL,
    EMAIL_VERIFICATION_RESEND_RATE_LIMIT_WINDOW_SECONDS,
    EmailVerificationSessionRequiredError,
    InvalidPasswordResetRequestError,
    InvalidPasswordResetTokenError,
    PasswordRecoveryConfigurationError,
    PasswordRecoveryError,
    PasswordRecoveryRateLimiter,
    PasswordRecoveryService,
)
from backend.services.session_service import (
    AuthenticatedUser,
    AuthRateLimitError,
    DisplayNameInputPolicyError,
    DuplicateEmailError,
    EmailInputPolicyError,
    InvalidCredentialsError,
    InvalidSessionError,
    PasswordPolicyError,
    RecentReauthenticationRequiredError,
    RecentReauthenticationResult,
    SessionResult,
    SessionService,
    SessionServiceError,
    SignupDisabledError,
    SuspendedUserError,
    VerifiedRecentReauthentication,
    consume_auth_slot,
)

router = APIRouter(prefix="/auth", tags=["auth"])

REST_ERROR_RESPONSES = {
    400: {"model": RESTErrorBody},
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
    429: {"model": RESTErrorBody},
}


def get_api_token_service(
    request: Request,
    session_factory: SessionFactoryDep,
) -> ApiTokenService:
    if hasattr(request.app.state, "api_token_service"):
        return request.app.state.api_token_service
    service = ApiTokenService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        audit_service=AuditService(),
        cursor_secret=request.app.state.settings.effective_session_secret,
    )
    request.app.state.api_token_service = service
    return service


ApiTokenServiceDep = Annotated[ApiTokenService, Depends(get_api_token_service)]


def get_password_recovery_service(
    request: Request,
    session_factory: SessionFactoryDep,
) -> PasswordRecoveryService:
    if hasattr(request.app.state, "password_recovery_service"):
        return request.app.state.password_recovery_service
    service = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=(
            request.app.state.settings.public_base_url
            if request.app.state.settings.canonical_https_ready
            else None
        ),
        mail_delivery_service=EmailDeliveryService(settings=request.app.state.settings),
    )
    request.app.state.password_recovery_service = service
    return service


PasswordRecoveryServiceDep = Annotated[
    PasswordRecoveryService,
    Depends(get_password_recovery_service),
]

# M-2: the query parameter the retired `?token=` recovery links and
# `GET /password-reset/status?token=` used.
ONE_TIME_TOKEN_QUERY_PARAMETER = "token"


class PasswordResetStatusRequest(BaseModel):
    """A password-reset token to check without using it.

    The token travels in this body and never in the URL, which access logs
    record.
    """

    # Same bounds as PasswordResetConfirmRequest.token: the check accepts
    # exactly what the confirm step accepts.
    token: str = Field(min_length=16, max_length=1024)

    model_config = ConfigDict(extra="forbid")


def reject_token_in_query_string(request: Request) -> None:
    """Refuse a one-time recovery token carried in the URL (M-2).

    Password-reset and email-verification tokens travel only in POST JSON
    bodies. A request line is written verbatim to the uvicorn and reverse-proxy
    access logs, so a token in the query string outlives the request for as
    long as those logs are kept -- and a reset token is an account takeover
    until it expires. The endpoints never read the query string; this guard
    also refuses a request that carries the token there anyway, so a client
    that leaks it fails loudly instead of quietly working. It is a route
    dependency, so it runs before the body is validated: a token sent only in
    the URL is refused for the URL, not reported as a missing body field.
    """

    if ONE_TIME_TOKEN_QUERY_PARAMETER in request.query_params:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "invalid_password_reset_request",
                "message": (
                    "send the one-time token in the JSON request body, not in the URL"
                ),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


RECOVERY_TOKEN_IN_BODY_ONLY = [Depends(reject_token_in_query_string)]


@router.get(
    "/capabilities",
    response_model=AuthCapabilitiesResponse,
)
def auth_capabilities(request: Request, response: Response):
    set_no_store_headers(response)
    return {
        "data": {
            "signup_enabled": (
                request.app.state.settings.effective_signup_enabled
                and request.app.state.settings.auth_password_login_enabled
            ),
            "password_login": {
                "enabled": request.app.state.settings.auth_password_login_enabled,
                "email_enrollment_enabled": (
                    request.app.state.settings.mail_configured
                    and request.app.state.settings.auth_password_login_enabled
                ),
            },
            "login_methods": request.app.state.auth_providers.login_methods(),
            "password_recovery": {
                "mode": request.app.state.settings.effective_password_recovery_mode,
                "admin_assisted": (
                    request.app.state.settings.password_recovery_admin_assisted
                ),
            },
        }
    }


@router.post(
    "/signup",
    status_code=201,
    response_model=AuthSessionResponse,
    responses=REST_ERROR_RESPONSES,
)
def signup(
    request_body: SignupRequest,
    response: Response,
    request: Request,
    session_service: SessionServiceDep,
):
    if not request.app.state.settings.auth_password_login_enabled:
        return no_store_response(
            auth_error_response(
                SignupDisabledError(
                    "password signup is disabled",
                )
            )
        )
    try:
        # Disabled signup is a capability decision, not an authentication
        # attempt. Reject it before consuming shared brute-force quota.
        session_service.assert_signup_enabled()
        consume_auth_slot(
            client_ip=auth_client_ip(request),
            email=request_body.email,
        )
        result = session_service.signup(
            email=request_body.email,
            password=request_body.password,
            display_name=request_body.display_name,
            remember_me=request_body.remember_me,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    set_session_cookie(response, request.app.state.settings, request, result)
    return {"data": session_payload(result)}


@router.post(
    "/login",
    response_model=AuthSessionResponse,
    responses=REST_ERROR_RESPONSES,
)
def login(
    request_body: LoginRequest,
    response: Response,
    request: Request,
    session_service: SessionServiceDep,
):
    if not request.app.state.settings.auth_password_login_enabled:
        return no_store_response(
            build_rest_error_response(
                "password_login_disabled",
                "Password login is disabled.",
                status_code=403,
            )
        )
    try:
        consume_auth_slot(
            client_ip=auth_client_ip(request),
            email=request_body.email,
        )
        result = session_service.login(
            email=request_body.email,
            password=request_body.password,
            remember_me=request_body.remember_me,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    set_session_cookie(response, request.app.state.settings, request, result)
    return {"data": session_payload(result)}


@router.post(
    "/reauthenticate",
    response_model=ReauthenticationResponse,
    responses=REST_ERROR_RESPONSES,
)
def reauthenticate(
    request_body: ReauthenticationRequest,
    response: Response,
    request: Request,
    session_service: SessionServiceDep,
):
    try:
        user = current_session_user(request, session_service)
        try:
            consume_auth_slot(
                client_ip=auth_client_ip(request),
                user_id=user.user_id,
            )
        except AuthRateLimitError:
            session_service.audit_reauthentication_failure(
                user_id=user.user_id,
                reason="rate_limited",
            )
            raise
        result = session_service.reauthenticate(
            user_id=user.user_id,
            current_password=request_body.current_password,
            session_token=current_session_token(request),
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    set_recent_reauthentication_cookie(
        response=response,
        settings=request.app.state.settings,
        request=request,
        result=result,
    )
    set_account_reauthentication_cookie(
        response,
        request.app.state.settings,
        secure=session_cookie_secure_for_request(request.app.state.settings, request),
        result=result,
    )
    return {
        "data": {
            "reauthenticated_until": result.reauthenticated_until,
        }
    }


@router.post(
    "/logout",
    response_model=AuthLogoutResponse,
    responses=REST_ERROR_RESPONSES,
)
def logout(
    request: Request,
    response: Response,
    session_service: SessionServiceDep,
):
    token = request.cookies.get(request.app.state.settings.session_cookie_name)
    try:
        session_service.logout(token)
    except InvalidSessionError:
        pass
    clear_session_cookie(response, request.app.state.settings, request)
    clear_recent_reauthentication_cookie(
        response,
        request.app.state.settings,
        request,
    )
    return {"data": {"logged_out": True}}


@router.get(
    "/me",
    response_model=AuthMeResponse,
    responses=REST_ERROR_RESPONSES,
)
def me(
    request: Request,
    response: Response,
    session_service: SessionServiceDep,
    session_factory: SessionFactoryDep,
):
    # Set before the first `return`: an unauthenticated /auth/me still answers
    # with session state, and a directly-returned error Response never picks up
    # the injected sub-response headers.
    set_no_store_headers(response)
    try:
        token = current_session_token(request)
        verified = session_service.token_codec.verify_session(token)
        user, refreshed_session = session_service.authenticate_session_with_refresh(
            token
        )
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    # The SPA loads /me on startup; carry the instance server-admin flag plus the
    # authoritative OAuth availability here so the MCP-access UI steers users with
    # the server's truth (https + DCR policy) instead of a client-side URL guess
    # that can contradict the deployment tab (merged I5). The full DeploymentStatus
    # stays admin-only; only this capability bit is exposed to authenticated users.
    settings = request.app.state.settings
    if refreshed_session is not None:
        set_session_cookie(response, settings, request, refreshed_session)
    with session_factory.begin() as session:
        is_admin = server_admin_service.is_server_admin(session, user.user_id)
    return {
        "data": {
            **user_payload(user),
            "session_method": "ext" if "ext" in (verified.amr or ()) else "pwd",
            "session_provider_slug": verified.idp,
            "is_server_admin": is_admin,
            "oauth_available": deployment_service.oauth_available(settings),
            "oauth_unavailable_reason": deployment_service.oauth_unavailable_reason(
                settings
            ),
            "oauth_dcr_enabled": deployment_service.oauth_dcr_enabled(settings),
            "mcp_url": deployment_service.mcp_url(settings),
        }
    }


@router.post(
    "/password",
    response_model=AuthSessionResponse,
    responses=REST_ERROR_RESPONSES,
)
def change_password(
    request_body: PasswordChangeRequest,
    response: Response,
    request: Request,
    session_service: SessionServiceDep,
):
    try:
        token = current_session_token(request)
        user, verified_session = session_service.authenticate_session_context(token)
        try:
            # AUTH-04: unlike reauthenticate, this endpoint had no rate limit
            # at all — a stolen session cookie let an attacker grind Argon2
            # verification against current_password with no throttle and no
            # trace. Mirror reauthenticate's slot + rate-limit-failure audit.
            consume_auth_slot(
                client_ip=auth_client_ip(request),
                user_id=user.user_id,
            )
        except AuthRateLimitError:
            session_service.audit_password_change_failure(
                user_id=user.user_id,
                reason="rate_limited",
            )
            raise
        result = session_service.change_password(
            user_id=user.user_id,
            current_password=request_body.current_password,
            new_password=request_body.new_password,
            remember_me=verified_session.remember_me,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    set_session_cookie(response, request.app.state.settings, request, result)
    return {"data": session_payload(result)}


@router.post(
    "/password-reset/request",
    status_code=202,
    response_model=PasswordResetRequestResponse,
    responses=REST_ERROR_RESPONSES,
)
def request_password_reset(
    request_body: PasswordResetRequest,
    response: Response,
    request: Request,
    session_factory: SessionFactoryDep,
    recovery_service: PasswordRecoveryServiceDep,
):
    """Accept public reset requests without disclosing account state."""

    started = time.perf_counter()
    settings = request.app.state.settings
    try:
        eligible = False
        if settings.effective_password_recovery_mode == "email":
            limiter = PasswordRecoveryRateLimiter(
                secret=settings.effective_session_secret
            )
            with session_factory.begin() as session:
                limiter.consume(
                    session=session,
                    client_ip=auth_client_ip(request),
                    email=request_body.email,
                    purpose="password_reset",
                )
                eligible = recovery_service.is_public_password_reset_eligible(
                    session=session,
                    email=request_body.email,
                )
                if eligible:
                    # Claimed only once a request is known to produce a
                    # delivery — an anonymous spray against unregistered
                    # addresses must not exhaust the single global backstop
                    # key and starve every real user's recovery (AUTH-06).
                    limiter.consume_global(session=session, purpose="password_reset")
            if eligible:
                # AUTH-01: no revoke_api_tokens kwarg here on purpose —
                # PasswordRecoveryService._issue_password_reset_serialized
                # forces it to True unconditionally for requested_via="public"
                # at the service boundary, so this caller cannot opt out.
                recovery_service.issue_password_reset(
                    email=request_body.email,
                    delivery="email",
                    requested_via="public",
                    actor_kind="human",
                    actor_id="public:password-recovery",
                )
    except (PasswordRecoveryError, SessionServiceError):
        # Rate limits, absent/inactive/unverified users, and delivery queue
        # failures all converge on the same public response. Operational state
        # remains visible to server admins through audit/outbox metadata.
        pass
    # Keep obvious database/provider fast paths from becoming a practical
    # account-state timing oracle. The fixed floor is intentionally modest so
    # public abuse cannot pin a worker for seconds per request.
    remaining = 0.075 - (time.perf_counter() - started)
    if remaining > 0:
        time.sleep(remaining)
    set_no_store_headers(response)
    return {
        "data": {
            "requested": True,
            "message": (
                "If the email is registered, password reset instructions were sent."
            ),
        }
    }


@router.post(
    "/email-verification/request",
    status_code=202,
    response_model=EmailVerificationResponse,
    responses=REST_ERROR_RESPONSES,
)
def request_email_verification(
    response: Response,
    request: Request,
    session_factory: SessionFactoryDep,
    session_service: SessionServiceDep,
    recovery_service: PasswordRecoveryServiceDep,
    request_body: Annotated[EmailVerificationRequest | None, Body()] = None,
):
    try:
        user = current_session_user(request, session_service)
        settings = request.app.state.settings
        candidate = request_body.email if request_body else None
        if user.email is None:
            from backend.api.request_origin import require_same_origin

            require_same_origin(request)
            if not settings.mail_configured or not settings.auth_password_login_enabled:
                raise PasswordRecoveryConfigurationError(
                    "native email enrollment is unavailable"
                )
            if candidate is None:
                raise InvalidPasswordResetRequestError("an email address is required")
            policy_for_request(request, session_service).require(
                session_token=current_session_token(request),
                scope="account",
                proof_token=request.cookies.get(
                    account_reauthentication_cookie_name(settings), ""
                ),
            )
        recipient = candidate or user.email
        if recipient is None:
            raise InvalidPasswordResetRequestError("an email address is required")
        consume_auth_slot(auth_client_ip(request), user_id=user.user_id)
        with session_factory.begin() as session:
            PasswordRecoveryRateLimiter(
                secret=settings.effective_session_secret
            ).consume(
                session=session,
                client_ip=auth_client_ip(request),
                email=recipient,
                purpose="email_verification",
            )
            PasswordRecoveryRateLimiter(
                secret=settings.effective_session_secret,
                per_email=EMAIL_VERIFICATION_RESEND_RATE_LIMIT_PER_EMAIL,
                window_seconds=EMAIL_VERIFICATION_RESEND_RATE_LIMIT_WINDOW_SECONDS,
            ).consume_email_only(
                session=session,
                email=recipient,
                purpose="email_verification_resend",
            )
        result = recovery_service.issue_email_verification(
            user_id=user.user_id,
            actor_id=str(user.user_id),
            email=candidate,
        )
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    except PasswordRecoveryError as exc:
        return no_store_response(password_recovery_error_response(exc))
    set_no_store_headers(response)
    return {
        "data": {
            "requested": bool(result.reset_url),
            "verified": not bool(result.reset_url),
        }
    }


@router.post(
    "/email-verification/confirm",
    response_model=EmailVerificationResponse,
    responses=REST_ERROR_RESPONSES,
    dependencies=RECOVERY_TOKEN_IN_BODY_ONLY,
)
def confirm_email_verification(
    request_body: EmailVerificationConfirmRequest,
    response: Response,
    request: Request,
    recovery_service: PasswordRecoveryServiceDep,
    session_service: SessionServiceDep,
):
    current_session = None
    token = request.cookies.get(request.app.state.settings.session_cookie_name)
    if token:
        try:
            _, current_session = session_service.authenticate_session_context(token)
        except SessionServiceError:
            pass
    try:
        recovery_service.consume_email_verification(
            token=request_body.token, current_session=current_session
        )
    except PasswordRecoveryError as exc:
        return no_store_response(password_recovery_error_response(exc))
    set_no_store_headers(response)
    return {"data": {"requested": False, "verified": True}}


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetConfirmResponse,
    responses=REST_ERROR_RESPONSES,
    dependencies=RECOVERY_TOKEN_IN_BODY_ONLY,
)
def confirm_password_reset(
    request_body: PasswordResetConfirmRequest,
    response: Response,
    request: Request,
    recovery_service: PasswordRecoveryServiceDep,
):
    try:
        recovery_service.consume_password_reset(
            token=request_body.token,
            new_password=request_body.new_password,
            revoke_api_tokens=request_body.revoke_api_tokens,
        )
    except PasswordPolicyError as exc:
        return no_store_response(auth_error_response(exc))
    except PasswordRecoveryError as exc:
        return no_store_response(password_recovery_error_response(exc))
    clear_session_cookie(response, request.app.state.settings, request)
    clear_recent_reauthentication_cookie(
        response,
        request.app.state.settings,
        request,
    )
    set_no_store_headers(response)
    return {"data": {"reset": True}}


@router.post(
    "/password-reset/status",
    response_model=PasswordResetStatusResponse,
    responses=REST_ERROR_RESPONSES,
    dependencies=RECOVERY_TOKEN_IN_BODY_ONLY,
)
def password_reset_status(
    request_body: PasswordResetStatusRequest,
    response: Response,
    recovery_service: PasswordRecoveryServiceDep,
):
    # M-23 (260908 UI audit): read-only, so the reset screen can tell an
    # expired/used/malformed link apart from a fresh one before showing the
    # new-password form -- see PasswordRecoveryService.peek_password_reset_token
    # for why this needs no additional rate limiting. M-2: this was
    # `GET ?token=`, which wrote the token into every access log on the path;
    # it is a POST with the token in the JSON body now, and there is no GET.
    try:
        recovery_service.peek_password_reset_token(token=request_body.token)
    except PasswordRecoveryError as exc:
        return no_store_response(password_recovery_error_response(exc))
    set_no_store_headers(response)
    return {"data": {"valid": True}}


@router.get(
    "/credential-workspaces",
    response_model=CredentialWorkspacePage,
    responses=REST_ERROR_RESPONSES,
)
def list_credential_workspace_candidates(
    request: Request,
    session_service: SessionServiceDep,
    session_factory: SessionFactoryDep,
    q: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
):
    from backend.services.credential_workspace_service import list_credential_workspaces

    try:
        user = current_session_user(request, session_service)
        with session_factory() as session:
            return list_credential_workspaces(
                session,
                user_id=user.user_id,
                secret=request.app.state.settings.effective_session_secret,
                query=q,
                limit=limit,
                cursor=cursor,
            )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ValueError:
        return build_rest_error_response(
            status_code=422,
            error_code="validation_failed",
            message="Invalid workspace cursor",
        )


@router.get(
    "/tokens",
    response_model=ApiTokenListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_api_tokens(
    request: Request,
    session_service: SessionServiceDep,
    api_token_service: ApiTokenServiceDep,
    status_filter: Annotated[
        Literal["active", "inactive", "expired", "revoked", "all"],
        Query(alias="status"),
    ] = "active",
    q: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    # D-01v2/T4: this is now the canonical, account-scoped Settings listing --
    # it replaced GET /workspaces/{id}/settings/access-keys, which was
    # workspace-scoped and made no sense once one credential can reach many
    # workspaces.
    try:
        user = current_session_user(request, session_service)
        page = api_token_service.list_user_tokens(
            user_id=user.user_id,
            status=status_filter,
            q=q,
            limit=limit,
            cursor=cursor,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ApiTokenServiceError as exc:
        return api_token_error_response(exc)
    return {
        "data": [api_token_payload(item.api_token) for item in page.data],
        "next_cursor": page.next_cursor,
        "evaluated_at": page.evaluated_at,
        "total_count": page.total_count,
        "filtered_count": page.filtered_count,
        "status_counts": {
            "active": page.status_counts.active,
            "expired": page.status_counts.expired,
            "revoked": page.status_counts.revoked,
        },
    }


@router.post(
    "/tokens",
    status_code=201,
    response_model=ApiTokenIssueResponse,
    responses=REST_ERROR_RESPONSES,
)
def create_api_token(
    request_body: ApiTokenCreateRequest,
    request: Request,
    session_service: SessionServiceDep,
    api_token_service: ApiTokenServiceDep,
):
    try:
        user = current_session_user(request, session_service)
        issued = api_token_service.create_token(
            user_id=user.user_id,
            workspace_grants=[
                WorkspaceGrantRequest(
                    workspace_id=grant.workspace_id, scopes=grant.scopes
                )
                for grant in request_body.workspace_grants
            ],
            name=request_body.name,
            scopes=request_body.scopes,
            expires_at=request_body.resolved_expires_at(now=datetime.now(UTC)),
            mcp_client_type=request_body.mcp_client_type,
            catalog_app_id=request_body.catalog_app_id,
            custom_app_label=request_body.custom_app_label,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ApiTokenServiceError as exc:
        return api_token_error_response(exc)
    return {
        "data": {
            "api_token": api_token_payload(issued.api_token),
            "plain_token": issued.plain_token,
        }
    }


@router.patch(
    "/tokens/{token_id}",
    response_model=ApiTokenRevokeResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_api_token(
    token_id: Annotated[UUID, Path()],
    request_body: ApiTokenUpdateRequest,
    request: Request,
    session_service: SessionServiceDep,
    api_token_service: ApiTokenServiceDep,
):
    try:
        user = current_session_user(request, session_service)
        updated = api_token_service.update_token(
            user_id=user.user_id,
            token_id=token_id,
            name=request_body.name,
            workspace_grants=(
                [
                    WorkspaceGrantRequest(
                        workspace_id=grant.workspace_id, scopes=grant.scopes
                    )
                    for grant in request_body.workspace_grants
                ]
                if request_body.workspace_grants is not None
                else None
            ),
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ApiTokenServiceError as exc:
        return api_token_error_response(exc)
    return {"data": api_token_payload(updated)}


@router.delete(
    "/tokens/{token_id}/record",
    response_model=ApiTokenDeleteResponse,
    responses=REST_ERROR_RESPONSES,
)
def delete_revoked_api_token(
    token_id: Annotated[UUID, Path()],
    request: Request,
    session_service: SessionServiceDep,
    api_token_service: ApiTokenServiceDep,
):
    try:
        user = current_session_user(request, session_service)
        deleted_id = api_token_service.delete_revoked_token(
            user_id=user.user_id,
            token_id=token_id,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ApiTokenServiceError as exc:
        return api_token_error_response(exc)
    return {"data": {"id": deleted_id, "deleted": True}}


@router.delete(
    "/tokens/{token_id}",
    response_model=ApiTokenRevokeResponse,
    responses=REST_ERROR_RESPONSES,
)
def revoke_api_token(
    token_id: Annotated[UUID, Path()],
    request: Request,
    session_service: SessionServiceDep,
    api_token_service: ApiTokenServiceDep,
):
    try:
        user = current_session_user(request, session_service)
        revoked = api_token_service.revoke_token(
            user_id=user.user_id,
            token_id=token_id,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except ApiTokenServiceError as exc:
        return api_token_error_response(exc)
    return {"data": api_token_payload(revoked)}


def current_session_user(
    request: Request,
    session_service: SessionService,
) -> AuthenticatedUser:
    token = current_session_token(request)
    return session_service.authenticate_session(token)


def current_session_token(request: Request) -> str:
    token = request.cookies.get(request.app.state.settings.session_cookie_name)
    if token is None:
        raise InvalidSessionError("session cookie is missing")
    return token


def require_server_admin(
    request: Request,
    session_service: SessionServiceDep,
    session_factory: SessionFactoryDep,
) -> AuthenticatedUser:
    """FastAPI dependency for instance-level (server) admin endpoints. Requires an
    authenticated session-cookie user holding a server-admin grant — 401 without a
    valid session, 403 without the grant. Bearer tokens (no session) get 401; this
    surface is session-only by design."""

    try:
        user = current_session_user(request, session_service)
    except SessionServiceError as exc:
        raise HTTPException(
            status_code=status_code_for_auth_error(exc),
            detail={
                "error_code": exc.error_code,
                "message": str(exc) or exc.error_code,
            },
        ) from exc
    with session_factory.begin() as session:
        if not server_admin_service.is_server_admin(session, user.user_id):
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": "server_admin_required",
                    "message": "instance server-admin privileges are required",
                },
            )
    return user


def require_recent_server_admin_reauthentication(
    request: Request,
    admin: Annotated[AuthenticatedUser, Depends(require_server_admin)],
    session_service: SessionServiceDep,
) -> VerifiedRecentReauthentication:
    try:
        verified = policy_for_request(request, session_service).require(
            session_token=current_session_token(request),
            scope="admin",
            proof_token=request.cookies.get(
                recent_reauthentication_cookie_name(request.app.state.settings), ""
            ),
        )
    except SessionServiceError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "recent_reauthentication_required",
                "message": "recent reauthentication is required",
            },
        ) from exc
    except AuthFlowError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": exc.code,
                "message": "server-admin privileges are required",
            },
        ) from exc
    return verified


def session_secret(settings: Settings) -> str:
    return settings.effective_session_secret


def auth_client_ip(request: Request) -> str:
    return resolve_remote_ip(request) or "unknown"


def set_session_cookie(
    response: Response,
    settings: Settings,
    request: Request,
    result: SessionResult,
) -> None:
    cookie_options = {
        "key": settings.session_cookie_name,
        "value": result.session_token,
        "httponly": True,
        "secure": session_cookie_secure_for_request(settings, request),
        "samesite": "lax",
    }
    if result.remember_me:
        cookie_options["max_age"] = max(
            0,
            int((result.expires_at - result.issued_at).total_seconds()),
        )
        cookie_options["expires"] = result.expires_at
    from backend.api.cookie_budget import set_bounded_cookie

    set_bounded_cookie(response, **cookie_options)


def clear_session_cookie(
    response: Response,
    settings: Settings,
    request: Request,
) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=session_cookie_secure_for_request(settings, request),
        samesite="lax",
    )


def recent_reauthentication_cookie_name(settings: Settings) -> str:
    return f"{settings.session_cookie_name}_reauth"


def set_recent_reauthentication_cookie(
    *,
    response: Response,
    settings: Settings,
    request: Request,
    result: RecentReauthenticationResult,
) -> None:
    from backend.api.cookie_budget import set_bounded_cookie

    for path in ADMIN_PROOF_PATHS:
        set_bounded_cookie(
            response,
            key=recent_reauthentication_cookie_name(settings),
            value=result.proof_token,
            httponly=True,
            secure=session_cookie_secure_for_request(settings, request),
            samesite="strict",
            max_age=settings.auth_reauthentication_max_age_seconds,
            expires=result.reauthenticated_until,
            path=path,
        )


def clear_recent_reauthentication_cookie(
    response: Response,
    settings: Settings,
    request: Request,
) -> None:
    clear_reauthentication_cookies(
        response,
        settings,
        secure=session_cookie_secure_for_request(settings, request),
    )


def session_cookie_secure_for_request(settings: Settings, request: Request) -> bool:
    if settings.session_cookie_secure is not None:
        return settings.session_cookie_secure
    if settings.public_base_url is not None or settings.is_production:
        return settings.effective_session_cookie_secure
    if settings.behind_proxy:
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        first_proto = forwarded_proto.split(",", 1)[0].strip().lower()
        if first_proto == "https":
            return True
    return settings.effective_session_cookie_secure


def session_payload(result: SessionResult) -> dict:
    return {
        "user": user_payload(result.user),
        "expires_at": result.expires_at,
    }


def user_payload(user: AuthenticatedUser) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
        "email_verified_at": user.email_verified_at,
        "has_password": user.has_password,
    }


def api_token_payload(token: ApiTokenRecord) -> dict:
    return {
        "id": token.id,
        "workspaces": [
            {
                "id": grant.workspace_id,
                "title": grant.title,
                "slug": grant.slug,
                "membership_role": grant.membership_role,
                "granted_scopes": grant.granted_scopes,
                "sync_state": grant.sync_state,
            }
            for grant in token.workspaces
        ],
        "name": token.name,
        "token_preview": token.token_preview,
        "scopes": token.scopes,
        "catalog_app_id": token.catalog_app_id,
        "catalog_app_label_snapshot": token.catalog_app_label_snapshot,
        "catalog_revision": token.catalog_revision,
        "mcp_client_type": token.mcp_client_type,
        "mcp_client_label": token.mcp_client_label,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "expires_at": token.expires_at,
        "revoked_at": token.revoked_at,
    }


def auth_error_response(exc: SessionServiceError) -> JSONResponse:
    return build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc) or exc.error_code,
        status_code=status_code_for_auth_error(exc),
    )


def api_token_error_response(exc: ApiTokenServiceError) -> JSONResponse:
    return build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc) or exc.error_code,
        status_code=status_code_for_api_token_error(exc),
    )


def password_recovery_error_response(exc: PasswordRecoveryError) -> JSONResponse:
    if isinstance(exc, EmailVerificationSessionRequiredError):
        status_code = 401
    else:
        status_code = 400 if isinstance(exc, InvalidPasswordResetTokenError) else 422
    return build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc) or exc.error_code,
        status_code=status_code,
    )


def status_code_for_auth_error(exc: SessionServiceError) -> int:
    # AUTH-03: signup returning 409 duplicate_email is a known, accepted
    # account-enumeration oracle — unlike /password-reset/request, which
    # deliberately swallows every failure into one 202. This is a recorded
    # trade-off, not a silent inconsistency: the signup surface chose
    # immediate-login UX over enumeration resistance, and consume_auth_slot
    # (per-email/per-IP throttling) is the accepted mitigation. Closing the
    # oracle properly requires gating signup on email verification before any
    # session is issued (so new and duplicate addresses get an identical
    # response) — a signup-flow redesign, not a patch, and out of scope until
    # that product decision is made.
    if isinstance(exc, DuplicateEmailError):
        return 409
    if isinstance(
        exc,
        InvalidCredentialsError
        | InvalidSessionError
        | RecentReauthenticationRequiredError,
    ):
        return 401
    if isinstance(exc, SignupDisabledError | SuspendedUserError):
        return 403
    if isinstance(
        exc, PasswordPolicyError | EmailInputPolicyError | DisplayNameInputPolicyError
    ):
        return 422
    if isinstance(exc, AuthRateLimitError):
        return 429
    return 500


def status_code_for_api_token_error(exc: ApiTokenServiceError) -> int:
    if isinstance(exc, ApiTokenNotFoundError):
        return 404
    if isinstance(exc, ApiTokenWorkspaceAccessError):
        return 403
    if isinstance(exc, InvalidApiTokenRequestError):
        return 422
    return 401
