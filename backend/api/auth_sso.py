"""Core-owned browser login and account management routes."""

import html
from typing import Annotated
from urllib.parse import urlencode, urlsplit
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
from backend.api.deps import SessionFactoryDep
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
from backend.services.auth.login_transactions import LOGIN_TTL_SECONDS, AuthFlowError
from backend.services.auth.return_path import safe_return_path
from backend.services.session_service import (
    RECENT_REAUTHENTICATION_TTL_SECONDS,
    SessionServiceError,
    consume_auth_slot,
)

router = APIRouter(prefix="/auth", tags=["auth"])
capabilities_router = APIRouter(tags=["capabilities"])
BINDING_COOKIE = "dtxwiki_login_txn"
BINDING_PATH = "/api/v1/auth/sso"


def get_auth_events(session_factory: SessionFactoryDep):
    return AuthEvents(session_factory)


AuthEventsDep = Annotated[AuthEvents, Depends(get_auth_events)]


def get_broker(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
):
    return AuthBroker(
        session_factory, request.app.state.auth_providers, sessions, events=events
    )


BrokerDep = Annotated[AuthBroker, Depends(get_broker)]


def get_accounts(
    request: Request,
    session_factory: SessionFactoryDep,
    sessions: SessionServiceDep,
    events: AuthEventsDep,
):
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
            else 400,
        )
    )


def same_origin(request):
    base = urlsplit(request.app.state.settings.public_base_url or "")
    expected = f"{base.scheme}://{base.netloc}"
    if request.headers.get("origin") != expected or not base.netloc:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "origin_required",
                "message": "A same-origin request is required.",
            },
        )


def account_cookie_name(settings):
    return f"{settings.session_cookie_name}_reauth_account"


def set_account_proof(response, settings, request, result):
    from backend.api.cookie_budget import set_bounded_cookie

    set_bounded_cookie(
        response,
        key=account_cookie_name(settings),
        value=result.proof_token,
        httponly=True,
        secure=session_cookie_secure_for_request(settings, request),
        samesite="strict",
        path="/api/v1/auth",
        max_age=RECENT_REAUTHENTICATION_TTL_SECONDS,
        expires=result.reauthenticated_until,
    )


def require_account_proof(request, sessions, user_id):
    sessions.verify_recent_reauthentication(
        proof_token=request.cookies.get(
            account_cookie_name(request.app.state.settings), ""
        ),
        user_id=user_id,
    )


def set_binding(response, request, binding, provider):
    cross_post = provider.method.descriptor.protocol == "saml2"
    secure = session_cookie_secure_for_request(request.app.state.settings, request)
    if cross_post and not secure:
        raise AuthFlowError("sso_provider_unavailable")
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        httponly=True,
        secure=secure,
        samesite="none" if cross_post else "lax",
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
    try:
        # No account identifier exists yet. Consume only the IP axis rather
        # than making every visitor share one global per-email bucket.
        consume_auth_slot(auth_client_ip(request), "")
        provider = broker.provider(slug)
        instruction, binding = broker.transactions.begin(
            provider,
            return_to=safe_return_path(next),
            remember_me=remember_me,
        )
        response = instruction_response(instruction)
        set_binding(response, request, binding, provider)
        return response
    except AuthFlowError as exc:
        return callback_error(exc.code)
    except SessionServiceError as exc:
        return no_store_response(auth_error_response(exc))
    except Exception:
        return callback_error("sso_provider_unavailable")


def callback_error(code):
    return no_store_response(
        RedirectResponse(
            "/login?" + urlencode({"auth_error": code}),
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


@router.post("/sso/{slug}/callback")
async def form_callback(
    slug: str, request: Request, broker: BrokerDep, sessions: SessionServiceDep
):
    form = await request.form(max_fields=20, max_files=0)
    if any(not isinstance(value, str) for value in form.values()):
        return callback_error("sso_transaction_invalid")
    return await run_in_threadpool(
        complete_callback,
        slug,
        request,
        broker,
        sessions,
        {},
        dict(form),
    )


def complete_callback(slug, request, broker, sessions, query, form=None):
    response = None
    try:
        consume_auth_slot(auth_client_ip(request), "")
        current = None
        token = request.cookies.get(request.app.state.settings.session_cookie_name)
        if token:
            try:
                _, current = sessions.authenticate_session_context(token)
            except SessionServiceError:
                pass
        params = form if form is not None else query
        result = broker.complete(
            slug,
            state=params.get("state") or params.get("RelayState"),
            binding=request.cookies.get(BINDING_COOKIE),
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
        response = callback_error(exc.code)
    except SessionServiceError:
        broker.audit_failure(slug, "sso_assertion_invalid")
        response = callback_error("sso_assertion_invalid")
    except Exception:
        broker.audit_failure(slug, "sso_provider_unavailable")
        response = callback_error("sso_provider_unavailable")
    finally:
        if response is not None:
            response.delete_cookie(BINDING_COOKIE, path=BINDING_PATH)
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
