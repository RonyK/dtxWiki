"""Core-owned browser login and account management routes."""

import html
import secrets
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from backend.api.auth import (
    SessionServiceDep,
    auth_client_ip,
    auth_error_response,
    current_session_token,
    no_store_response,
    session_cookie_secure_for_request,
    session_payload,
    set_no_store_headers,
    set_recent_reauthentication_cookie,
    set_session_cookie,
)
from backend.api.auth_cookies import (
    PENDING_SIGNUP_COOKIE,
    PENDING_SIGNUP_PATH,
    account_reauthentication_cookie_name,
    set_account_reauthentication_cookie,
)
from backend.api.deps import SessionFactoryDep
from backend.api.middleware.security_headers import CSP_SCRIPT_NONCE_STATE_KEY
from backend.api.request_origin import require_same_origin
from backend.app.errors import build_rest_error_response
from backend.schemas.auth import (
    AuthSessionResponse,
    CapabilitiesResponse,
    LoginMethodCapability,
)
from backend.services.auth.account_management import (
    AuthAccountService,
    IdentityNotFoundError,
)
from backend.services.auth.broker import AuthBroker
from backend.services.auth.events import AuthEvents
from backend.services.auth.external_signup import ExternalSignupService
from backend.services.auth.login_transactions import (
    LOGIN_TTL_SECONDS,
    AuthFlowError,
    FlowContext,
)
from backend.services.auth.return_path import safe_return_path
from backend.services.mail_delivery import build_email_delivery_service
from backend.services.session_service import (
    AuthRateLimitError,
    SessionServiceError,
    consume_auth_slot,
)

router = APIRouter(prefix="/auth", tags=["auth"])
capabilities_router = APIRouter(tags=["capabilities"])
BINDING_COOKIE = "dtxwiki_login_txn"
BINDING_PATH = "/api/v1/auth/sso"
CALLBACK_MAX_BYTES = 64 * 1024
CALLBACK_MAX_FIELDS = 20


def get_auth_events(session_factory: SessionFactoryDep) -> AuthEvents:
    return AuthEvents(session_factory)


AuthEventsDep = Annotated[AuthEvents, Depends(get_auth_events)]


def get_signups(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
) -> ExternalSignupService:
    return ExternalSignupService(
        session_factory,
        request.app.state.auth_providers,
        sessions,
        build_email_delivery_service(request.app.state.settings),
        events,
        sessions.token_codec.now,
    )


SignupDep = Annotated[ExternalSignupService, Depends(get_signups)]


def get_broker(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
    signups: SignupDep,
) -> AuthBroker:
    return AuthBroker(
        session_factory,
        request.app.state.auth_providers,
        sessions,
        events=events,
        signups=signups,
        mail_configured=request.app.state.settings.mail_configured,
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


def flow_error(exc):
    return no_store_response(
        build_rest_error_response(
            exc.code,
            "The external authentication operation could not be completed.",
            status_code=409
            if exc.code in {"last_login_method", "identity_already_linked"}
            else 429
            if exc.code == "rate_limited"
            else 403
            if exc.code == "server_admin_required"
            else 400,
        )
    )


def same_origin(request):
    require_same_origin(request)


def account_cookie_name(settings):
    return account_reauthentication_cookie_name(settings)


def set_account_proof(response, settings, request, result):
    set_account_reauthentication_cookie(
        response,
        settings,
        secure=session_cookie_secure_for_request(settings, request),
        result=result,
    )


def require_account_proof(request, sessions, user_id):
    from backend.services.auth.reauthentication_policy import ReauthenticationPolicy

    ReauthenticationPolicy(
        sessions.session_factory,
        sessions,
        request.app.state.auth_providers,
        request.app.state.settings.mail_configured,
    ).require(
        session_token=current_session_token(request),
        scope="account",
        proof_token=request.cookies.get(
            account_cookie_name(request.app.state.settings), ""
        ),
    )


def set_binding(response, request, binding, provider):
    secure = session_cookie_secure_for_request(request.app.state.settings, request)
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=BINDING_PATH,
        max_age=LOGIN_TTL_SECONDS,
    )


def instruction_response(instruction):
    parsed = urlsplit(instruction.url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise AuthFlowError("sso_provider_unavailable")
    if instruction.http_method == "GET":
        return no_store_response(RedirectResponse(instruction.url, status_code=302))
    if instruction.http_method == "POST":
        fields = "".join(
            f'<input type="hidden" name="{html.escape(key, quote=True)}" '
            f'value="{html.escape(value, quote=True)}">'
            for key, value in instruction.form_fields.items()
        )
        return no_store_response(
            HTMLResponse(
                '<!doctype html><meta name="referrer" content="no-referrer">'
                '<form method="post" '
                f'action="{html.escape(instruction.url, quote=True)}">'
                f'{fields}<button type="submit">Continue</button></form>'
            )
        )
    raise AuthFlowError("sso_provider_unavailable")


@router.get("/sso/{slug}/start")
def start(
    slug: str,
    request: Request,
    broker: BrokerDep,
    next: str | None = None,
    remember_me: bool = False,
):
    context = FlowContext("login", safe_return_path(next))
    try:
        # No account identifier exists yet. Consume only the IP axis rather
        # than making every visitor share one global per-email bucket.
        consume_auth_slot(auth_client_ip(request), "")
        provider = broker.provider(slug)
        instruction, binding = broker.transactions.begin(
            provider,
            return_to=context.return_to,
            remember_me=remember_me,
        )
        response = instruction_response(instruction)
        set_binding(response, request, binding, provider)
        return response
    except AuthFlowError as exc:
        return callback_error(exc.code, context)
    except SessionServiceError as exc:
        return callback_error(exc.error_code, context)
    except Exception:
        return callback_error("sso_provider_unavailable", context)


def callback_error(code, context: FlowContext | None = None):
    if context is not None and context.purpose != "login":
        destination = urlsplit(context.return_to)
        query = parse_qsl(destination.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "auth_error"]
        query.append(("auth_error", code))
        target = urlunsplit(destination._replace(query=urlencode(query)))
    else:
        query = [("auth_error", code)]
        if context is not None:
            query.append(("next", context.return_to))
        target = "/login?" + urlencode(query)
    return no_store_response(
        RedirectResponse(
            target,
            status_code=302,
        )
    )


@router.get("/sso/{slug}/callback")
def callback(
    slug: str, request: Request, broker: BrokerDep, sessions: SessionServiceDep
):
    return complete_callback(
        slug, request, broker, sessions, dict(request.query_params)
    )


async def callback_form(request: Request) -> dict[str, str]:
    """Bound the wire payload before multipart or URL-encoded parsing."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CALLBACK_MAX_BYTES:
            raise AuthFlowError("sso_transaction_invalid")

    async def buffered_body():
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    buffered = Request(request.scope, buffered_body)
    try:
        async with buffered.form(
            max_fields=CALLBACK_MAX_FIELDS,
            max_files=0,
            max_part_size=CALLBACK_MAX_BYTES,
        ) as form:
            fields = form.multi_items()
            if (
                len(fields) > CALLBACK_MAX_FIELDS
                or len({key for key, _ in fields}) != len(fields)
                or any(not isinstance(value, str) for _, value in fields)
            ):
                raise AuthFlowError("sso_transaction_invalid")
            return dict(fields)
    except AuthFlowError:
        raise
    except Exception as exc:
        raise AuthFlowError("sso_transaction_invalid") from exc


@router.post("/sso/{slug}/callback")
async def form_callback(slug: str, request: Request):
    # Cross-site POST omits the native Lax session cookie. Do not consume the
    # transaction or call the IdP until a same-origin form restores cookies.
    try:
        form = await callback_form(request)
    except AuthFlowError:
        return callback_error("sso_transaction_invalid")
    nonce = secrets.token_urlsafe(24)
    setattr(request.state, CSP_SCRIPT_NONCE_STATE_KEY, nonce)
    action = request.url_for("form_callback_continue", slug=slug).path
    fields = "".join(
        f'<input type="hidden" name="{html.escape(key, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
        for key, value in form.items()
    )
    return no_store_response(
        HTMLResponse(
            '<!doctype html><meta name="referrer" content="same-origin">'
            "<title>Continue sign-in</title>"
            '<form id="sso-callback" method="post" '
            f'action="{html.escape(action, quote=True)}">'
            f'{fields}<button type="submit">Continue</button></form>'
            f'<script nonce="{nonce}">'
            "HTMLFormElement.prototype.submit.call("
            'document.getElementById("sso-callback"));</script>',
            headers={
                "Content-Security-Policy": "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
                # no-referrer makes browser form navigations send Origin:null,
                # defeating the continuation's same-origin check. Only the
                # token-free callback location is shared within this origin.
                "Referrer-Policy": "same-origin",
            },
        )
    )


@router.post("/sso/{slug}/callback/continue")
async def form_callback_continue(
    slug: str, request: Request, broker: BrokerDep, sessions: SessionServiceDep
):
    same_origin(request)
    try:
        form = await callback_form(request)
    except AuthFlowError:
        return callback_error("sso_transaction_invalid")
    return await run_in_threadpool(
        complete_callback,
        slug,
        request,
        broker,
        sessions,
        {},
        form,
    )


def recover_callback_context(slug, request, broker, state, binding):
    base = (request.app.state.settings.public_base_url or "").rstrip("/")
    if not base:
        return None
    try:
        return broker.transactions.context_for_callback(
            slug,
            f"{base}/api/v1/auth/sso/{slug}/callback",
            state=state,
            binding=binding,
        )
    except AuthFlowError:
        return None


def complete_callback(slug, request, broker, sessions, query, form=None):
    params = form if form is not None else query
    state = params.get("state") or params.get("RelayState")
    binding = request.cookies.get(BINDING_COOKIE)
    try:
        consume_auth_slot(auth_client_ip(request), "")
        current = None
        token = request.cookies.get(request.app.state.settings.session_cookie_name)
        if token:
            try:
                _, current = sessions.authenticate_session_context(token)
            except SessionServiceError:
                pass
        result = broker.complete(
            slug,
            state=state,
            binding=binding,
            query=query,
            form=form,
            current_session=current,
        )
        response = no_store_response(
            RedirectResponse(result.return_to, status_code=302)
        )
        if result.session is not None:
            set_session_cookie(
                response, request.app.state.settings, request, result.session
            )
        if result.pending_signup is not None:
            from backend.services.auth.external_signup import PENDING_SIGNUP_TTL_SECONDS

            response.set_cookie(
                PENDING_SIGNUP_COOKIE,
                result.pending_signup.binding,
                httponly=True,
                secure=session_cookie_secure_for_request(
                    request.app.state.settings, request
                ),
                samesite="lax",
                path=PENDING_SIGNUP_PATH,
                max_age=PENDING_SIGNUP_TTL_SECONDS,
            )
        if result.reauthentication is not None:
            set_recent_reauthentication_cookie(
                response=response,
                settings=request.app.state.settings,
                request=request,
                result=result.reauthentication,
            )
            set_account_proof(
                response, request.app.state.settings, request, result.reauthentication
            )
    except AuthFlowError as exc:
        broker.audit_failure(slug, exc.code)
        context = exc.context
        if context is None and exc.code == "sso_provider_unavailable":
            context = recover_callback_context(slug, request, broker, state, binding)
        return callback_error(exc.code, context)
    except AuthRateLimitError as exc:
        context = recover_callback_context(slug, request, broker, state, binding)
        broker.audit_failure(slug, exc.error_code)
        return callback_error(exc.error_code, context)
    except SessionServiceError as exc:
        broker.audit_failure(slug, exc.error_code)
        return callback_error(exc.error_code)
    except Exception:
        broker.audit_failure(slug, "sso_provider_unavailable")
        return callback_error("sso_provider_unavailable")
    # Callback responses never mutate the shared binding cookie: a delayed
    # response must not erase the binding installed by a more recent start.
    return response


class StartAccountRequest(BaseModel):
    return_to: str = Field(default="/account", max_length=2048)
    model_config = ConfigDict(extra="forbid")


class RedirectData(BaseModel):
    redirect_url: str


class RedirectResponseBody(BaseModel):
    data: RedirectData


def start_account_flow(slug, body, request, broker, sessions, purpose):
    same_origin(request)
    try:
        user, verified = sessions.authenticate_session_context(
            current_session_token(request)
        )
        consume_auth_slot(auth_client_ip(request), user.email)
        if purpose == "link":
            require_account_proof(request, sessions, user.user_id)
        provider = broker.provider(slug)
        if (
            purpose == "reauth"
            and not provider.method.descriptor.capabilities.reauthentication
        ):
            raise AuthFlowError("reauthentication_mismatch")
        instruction, binding = broker.transactions.begin(
            provider,
            purpose=purpose,
            return_to=safe_return_path(body.return_to),
            remember_me=verified.remember_me,
            user_id=user.user_id,
            session_generation=verified.session_generation,
        )
        if instruction.http_method != "GET":
            raise AuthFlowError("sso_provider_unavailable")
        instruction_response(instruction)  # Validate before exposing the URL.
        from fastapi.responses import JSONResponse

        response = no_store_response(
            JSONResponse({"data": {"redirect_url": instruction.url}})
        )
        set_binding(response, request, binding, provider)
        return response
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    except Exception:
        return flow_error(AuthFlowError("sso_provider_unavailable"))


@router.post("/sso/{slug}/link", response_model=RedirectResponseBody)
def link(
    slug: str,
    body: StartAccountRequest,
    request: Request,
    broker: BrokerDep,
    sessions: SessionServiceDep,
):
    return start_account_flow(slug, body, request, broker, sessions, "link")


@router.post("/sso/{slug}/reauthenticate", response_model=RedirectResponseBody)
def reauthenticate(
    slug: str,
    body: StartAccountRequest,
    request: Request,
    broker: BrokerDep,
    sessions: SessionServiceDep,
):
    return start_account_flow(slug, body, request, broker, sessions, "reauth")


class IdentityData(BaseModel):
    id: UUID
    provider_slug: str
    last_email: str | None
    last_login_at: str | None
    can_unlink: bool


class IdentityListData(BaseModel):
    has_password: bool
    identities: list[IdentityData]
    available_providers: list[LoginMethodCapability]


class IdentityListResponse(BaseModel):
    data: IdentityListData


@router.get("/identities", response_model=IdentityListResponse)
def identities(
    request: Request,
    response: Response,
    accounts: AccountServiceDep,
    sessions: SessionServiceDep,
):
    set_no_store_headers(response)
    try:
        user = sessions.authenticate_session(current_session_token(request))
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    return {
        "data": accounts.list_identities(
            user,
            password_login_enabled=request.app.state.settings.auth_password_login_enabled,
        )
    }


@router.delete("/identities/{identity_id}", response_model=AuthSessionResponse)
def unlink(
    identity_id: UUID,
    request: Request,
    accounts: AccountServiceDep,
    sessions: SessionServiceDep,
):
    same_origin(request)
    try:
        user, verified = sessions.authenticate_session_context(
            current_session_token(request)
        )
        require_account_proof(request, sessions, user.user_id)
        issued = accounts.unlink_identity(
            user,
            verified,
            identity_id,
            password_login_enabled=request.app.state.settings.auth_password_login_enabled,
        )
        from fastapi.encoders import jsonable_encoder
        from fastapi.responses import JSONResponse

        response = no_store_response(
            JSONResponse(jsonable_encoder({"data": session_payload(issued)}))
        )
        set_session_cookie(response, request.app.state.settings, request, issued)
        return response
    except IdentityNotFoundError:
        raise HTTPException(status_code=404) from None
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))


class SetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=1024)
    model_config = ConfigDict(extra="forbid")


@router.post("/password/set", response_model=AuthSessionResponse)
def set_password(
    body: SetPasswordRequest,
    request: Request,
    accounts: AccountServiceDep,
    sessions: SessionServiceDep,
):
    same_origin(request)
    try:
        user, verified = sessions.authenticate_session_context(
            current_session_token(request)
        )
        require_account_proof(request, sessions, user.user_id)
        issued = accounts.set_initial_password(user, verified, body.new_password)
        from fastapi.encoders import jsonable_encoder
        from fastapi.responses import JSONResponse

        response = no_store_response(
            JSONResponse(jsonable_encoder({"data": session_payload(issued)}))
        )
        set_session_cookie(response, request.app.state.settings, request, issued)
        return response
    except AuthFlowError as exc:
        return flow_error(exc)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))


@router.get("/methods/{method_id}/icon.png")
def icon(method_id: str, request: Request):
    data = request.app.state.auth_providers.icons.get(method_id)
    if data is None:
        raise HTTPException(status_code=404)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@capabilities_router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(request: Request, response: Response):
    from backend.api.auth import auth_capabilities

    return {
        "data": {
            "auth": auth_capabilities(request, response)["data"],
            "plugins": list(request.app.state.plugin_registry.capabilities()),
        }
    }
