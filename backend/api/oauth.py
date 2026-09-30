from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from html import escape
from secrets import token_urlsafe
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.api.auth import (
    current_session_user,
    get_session_service,
    session_cookie_secure_for_request,
)
from backend.api.deps import SessionFactoryDep
from backend.api.metrics import inc_oauth_token_grant
from backend.api.middleware.security_headers import (
    CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY,
    CSP_SCRIPT_NONCE_STATE_KEY,
)
from backend.api.oauth_copy import (
    DRAFT_REVIEW_CAVEAT,
    DRAFT_REVIEW_CAVEAT_KO,
    scope_copy_for,
    surviving_denials,
)
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.audit.emitter import emit_oauth_event
from backend.policy.auth import (
    authenticate_bearer_token,
    public_base_url_from_request,
)
from backend.policy.rbac import list_user_workspaces
from backend.policy.remote_ip import resolve_remote_ip
from backend.schemas.auth import (
    OAuthConnectionCleanupResponse,
    OAuthConsentListResponse,
    OAuthConsentRevokeResponse,
    OAuthConsentSettingsUpdateRequest,
    OAuthConsentSettingsUpdateResponse,
)
from backend.schemas.common import RESTErrorBody
from backend.services.oauth_service import (
    OAUTH_CSRF_COOKIE_MAX_AGE,
    OAUTH_CSRF_COOKIE_NAME,
    OAUTH_CSRF_COOKIE_PATH,
    AuthorizeWorkspaceOption,
    InvalidClientRegistrationError,
    InvalidOAuthGrantError,
    InvalidOAuthRequestError,
    OAuthConsentNotFoundError,
    OAuthConsentTTLAboveInstanceError,
    OAuthRateLimitError,
    OAuthService,
    UnsupportedGrantTypeError,
    classify_oauth_csrf_failure,
    consume_oauth_csrf_state,
    consume_token_slot,
    record_oauth_csrf_state_issued,
)
from backend.services.session_service import AuthenticatedUser, SessionServiceError

router = APIRouter(tags=["oauth"])


@dataclass(frozen=True)
class RevokeActor:
    """P10.3 T3: no ``workspace_ids`` restriction any more -- RFC 7009 revoke
    always kills the token's whole (user, client) family (section 2.3); a
    workspace-scoped access reduction is expressed purely as a consent-row
    removal, a different operation (``revoke_workspace_oauth_consent``).
    """

    user_id: UUID | None
    kind: str
    actor_id: str | None
    client_id: str | None = None


@router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource(request: Request, session_factory: SessionFactoryDep):
    return oauth_service(request, session_factory).protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server(request: Request, session_factory: SessionFactoryDep):
    return oauth_service(request, session_factory).authorization_server_metadata()


@router.get("/.well-known/jwks.json")
def jwks(request: Request, session_factory: SessionFactoryDep):
    return oauth_service(request, session_factory).jwks()


@router.post("/oauth/register", status_code=201)
async def register_client(request: Request, session_factory: SessionFactoryDep):
    payload = await request.json()
    try:
        client = oauth_service(request, session_factory).register_client(
            payload=payload,
            client_ip=oauth_client_ip(request),
        )
    except InvalidClientRegistrationError as exc:
        return oauth_error_response(exc, 400)
    return {
        "client_id": client.client_id,
        "client_id_issued_at": client.issued_at,
        "client_secret_expires_at": 0,
        "redirect_uris": client.redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": client.scope,
        "client_name": client.client_name,
        "application_type": "native",
    }


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_form(request: Request, session_factory: SessionFactoryDep):
    session_service = get_session_service(request, session_factory)
    service = oauth_service(request, session_factory)
    try:
        user = current_session_user(request, session_service)
    except SessionServiceError:
        next_url = next_url_from_request(request)
        return RedirectResponse(f"/login?{urlencode({'next': next_url})}", 302)
    values = dict(request.query_params)
    client_id = str(values.get("client_id") or "")
    scope = str(values.get("scope") or "mcp:read")
    try:
        request_args = authorization_kwargs(values)
        client = service.validate_authorization_request(**request_args)
        # P10.3 T3 (section 2.3): consent is a (user, client)-GROUP fact now,
        # not per-workspace -- an existing live consent for this client
        # (covering however many workspaces) skips the checklist entirely
        # and goes straight to the handoff page. Set-editing from that point
        # is Settings' job, not this screen's.
        existing_consent = service.has_existing_consent(
            user_id=user.user_id,
            client_id=client_id,
            scope=scope,
        )
        service.record_authorization_request(
            user_id=user.user_id,
            client_id=client_id,
            client_name=str(getattr(client, "client_name", None) or "MCP client"),
            redirect_uri=str(values.get("redirect_uri") or ""),
            scope=scope,
            existing_consent=existing_consent,
        )
        if existing_consent:
            code = service.issue_authorization_code(
                user_id=user.user_id,
                grant_consent=False,
                **request_args,
            )
            forward = redirect_with_params(
                str(values.get("redirect_uri") or ""),
                {
                    "code": code,
                    "state": str(values.get("state") or ""),
                    "iss": service.issuer,
                },
            )
            # Re-consent skips the approval screen by design, but it used to
            # skip the handoff page too -- so the client's bare "Authentication
            # complete" page, the thing the handoff exists to explain, was
            # exactly what every login after the first one showed.
            if request_accepts_html(request):
                nonce = token_urlsafe(24)
                setattr(request.state, CSP_SCRIPT_NONCE_STATE_KEY, nonce)
                return _oauth_no_store(
                    HTMLResponse(authorization_handoff_page(forward, nonce=nonce))
                )
            return RedirectResponse(forward, 302)
        # query workspace_id (repeatable) is a preselect HINT only, still
        # membership-validated inside authorize_workspace_options -- never a
        # bypass of the checklist itself (section 2.3).
        hint_ids = parse_workspace_id_hints(
            request.query_params.getlist("workspace_id")
        )
        workspace_options = service.authorize_workspace_options(
            user_id=user.user_id,
            client_id=client_id,
            preselected_workspace_ids=hint_ids,
        )
    except (InvalidOAuthRequestError, InvalidOAuthGrantError) as exc:
        if request_accepts_html(request):
            return oauth_authorize_error_response(exc, 400)
        return oauth_error_response(exc, 400)
    nonce = token_urlsafe(24)
    setattr(request.state, CSP_SCRIPT_NONCE_STATE_KEY, nonce)
    loopback_origin = _loopback_redirect_origin(request_args["redirect_uri"])
    if loopback_origin:
        setattr(
            request.state,
            CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY,
            [loopback_origin],
        )
    response = HTMLResponse(
        consent_form(request, client, workspace_options, nonce=nonce)
    )
    _oauth_no_store(response)
    # Sprint_5 Phase_3 Task 3.3 (ADR A3): bind consent POST CSRF to GET-issued
    # state via a one-time HttpOnly + SameSite=Lax cookie. Secure flag follows
    # the cookie-secure rule that already governs the session cookie so the
    # plain-HTTP loopback DCR developer flow (ADR A2) keeps working.
    state_value = str(values.get("state") or "")
    if state_value:
        settings = request.app.state.settings
        secure_flag = session_cookie_secure_for_request(settings, request)
        response.set_cookie(
            key=OAUTH_CSRF_COOKIE_NAME,
            value=state_value,
            max_age=OAUTH_CSRF_COOKIE_MAX_AGE,
            httponly=True,
            secure=secure_flag,
            samesite="lax",
            path=OAUTH_CSRF_COOKIE_PATH,
        )
        record_oauth_csrf_state_issued(state_value)
    return response


@router.post("/oauth/authorize")
async def authorize_submit(request: Request, session_factory: SessionFactoryDep):
    values = await parse_request_values(request)
    # P10.3 T3 (section 2.3): the checklist submits ``workspace_ids`` as a
    # repeated form field (or a JSON array) -- parse_request_values collapses
    # a repeated field to its last value, so the full set is read separately
    # here (Starlette caches request.body()/.json(), so this is not a second
    # stream read).
    workspace_ids_raw = await parse_form_workspace_ids(request, values)
    session_service = get_session_service(request, session_factory)
    service = oauth_service(request, session_factory)
    try:
        user = current_session_user(request, session_service)
    except SessionServiceError:
        next_url = next_url_from_request(request)
        return _oauth_no_store(
            RedirectResponse(f"/login?{urlencode({'next': next_url})}", 302)
        )
    # Sprint_5 Phase_3 Task 3.3 (ADR A3): verify the state-bound CSRF cookie
    # *before* parsing the decision. A POST without a matching GET-issued
    # cookie is treated as CSRF regardless of payload validity.
    form_state = str(values.get("state") or "")
    cookie_state = request.cookies.get(OAUTH_CSRF_COOKIE_NAME)
    csrf_ok = bool(form_state) and cookie_state == form_state
    if not csrf_ok:
        reason = classify_oauth_csrf_failure(
            form_state=form_state, cookie_state=cookie_state,
        )
        # Best-effort forensic context only (ADR A11) -- the first submitted
        # workspace_ids entry, if any parses as a UUID.
        audit_workspace_id: UUID | None = None
        for raw in workspace_ids_raw:
            try:
                audit_workspace_id = UUID(raw)
                break
            except ValueError:
                continue
        return _consent_csrf_failure_response(
            session_factory=session_factory,
            user=user,
            client_id=str(values.get("client_id") or ""),
            workspace_id=audit_workspace_id,
            reason=reason,
            request=request,
        )
    # Match confirmed; consume the state one-time so a replay attempt fails.
    consume_oauth_csrf_state(form_state)
    decision = values.get("decision")
    try:
        if decision is None:
            raise InvalidOAuthRequestError("authorization decision is required")
        decision = str(decision)
        if decision == "deny":
            service.deny_authorization_request(
                user_id=user.user_id,
                **authorization_kwargs(values),
            )
            response = RedirectResponse(
                redirect_with_params(
                    str(values.get("redirect_uri") or ""),
                    {
                        "error": "access_denied",
                        "state": form_state,
                        "iss": service.issuer,
                    },
                ),
                303,
            )
            _delete_oauth_csrf_cookie(response)
            return _oauth_no_store(response)
        if decision != "allow":
            raise InvalidOAuthRequestError("unsupported authorization decision")
        if not workspace_ids_raw:
            raise InvalidOAuthRequestError("workspace_ids is required")
        try:
            workspace_ids = [UUID(raw) for raw in workspace_ids_raw]
        except ValueError as exc:
            raise InvalidOAuthRequestError("workspace_ids is invalid") from exc
        code = service.issue_authorization_code(
            user_id=user.user_id,
            grant_consent=True,
            workspace_ids=workspace_ids,
            **authorization_kwargs(values),
        )
    except IntegrityError:
        err = oauth_error_response(
            InvalidOAuthRequestError("workspace is no longer available"),
            400,
        )
        _delete_oauth_csrf_cookie(err)
        return _oauth_no_store(err)
    except (InvalidOAuthRequestError, InvalidOAuthGrantError) as exc:
        err = oauth_error_response(exc, 400)
        _delete_oauth_csrf_cookie(err)
        return _oauth_no_store(err)
    # One place builds the forward URL. Two would eventually disagree and drop
    # the code or the state.
    forward = redirect_with_params(
        str(values.get("redirect_uri") or ""),
        {
            "code": code,
            "state": form_state,
            "iss": service.issuer,
        },
    )
    if request_accepts_html(request):
        nonce = token_urlsafe(24)
        setattr(request.state, CSP_SCRIPT_NONCE_STATE_KEY, nonce)
        response: Response = HTMLResponse(
            authorization_handoff_page(forward, nonce=nonce)
        )
    else:
        response = RedirectResponse(forward, 303)
    _delete_oauth_csrf_cookie(response)
    return _oauth_no_store(response)


def _delete_oauth_csrf_cookie(response: Response) -> None:
    """One-time deletion of the consent CSRF cookie (ADR A3).

    Called from both the success and failure branches of POST /oauth/authorize
    so a follow-up POST cannot replay the previous request.
    """

    response.delete_cookie(OAUTH_CSRF_COOKIE_NAME, path=OAUTH_CSRF_COOKIE_PATH)


def _consent_csrf_failure_response(
    *,
    session_factory,
    user,
    client_id: str,
    workspace_id: UUID | None,
    reason: str,
    request: Request,
):
    """Build the 400 response for a CSRF-binding failure on POST.

    Emits a single ``oauth.consent.csrf`` audit row (ADR A11) before
    returning the standard OAuth error body. The audit row carries the
    diagnosed ``reason`` ∈ {missing, mismatch, replay, expired}.
    """

    if request_accepts_html(request):
        err_response = HTMLResponse(_consent_csrf_failure_page(), status_code=400)
    else:
        err_response = oauth_error_response(
            InvalidOAuthRequestError("state binding failed"),
            400,
        )
    _oauth_no_store(err_response)
    _delete_oauth_csrf_cookie(err_response)
    actor_id = str(user.user_id) if user is not None else "anonymous"
    with session_factory.begin() as session:
        audit_workspace_id = _consent_csrf_audit_workspace_id(
            session=session,
            user_id=user.user_id if user is not None else None,
            requested_workspace_id=workspace_id,
        )
        emit_oauth_event(
            session=session,
            action="oauth.consent.csrf",
            target_id=uuid4(),
            target_kind="oauth_consent_session",
            workspace_id=audit_workspace_id,
            actor_kind="user_session",
            actor_id=actor_id,
            payload={
                "client_id": client_id,
                "reason": reason,
            },
            retention_class="sensitive",
        )
    return err_response


def _consent_csrf_failure_page() -> str:
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <title>dtxWiki OAuth Approval</title>
    {oauth_page_styles()}
  </head>
  <body>
    <main class="oauth-shell">
      <section class="oauth-panel oauth-panel-error">
        <p class="oauth-kicker">dtxWiki MCP</p>
        <h1>Approval already processed or expired</h1>
        <p class="oauth-copy">
          This approval was already processed, or the link has expired.
          Return to your MCP client and restart its OAuth connection if
          sign-in did not complete.
        </p>
        <p class="oauth-copy" lang="ko">
          이 승인은 이미 처리되었거나 링크가 만료되었습니다.
          MCP 클라이언트로 돌아가 로그인이 완료되지 않았다면 OAuth 연결을
          다시 시작하세요.
        </p>
      </section>
    </main>
  </body>
</html>
"""


def _oauth_no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect_authority(parsed) -> str:
    """host[:port] built from the parsed host, never from `netloc`.

    `netloc` carries the userinfo, so `https://dtxwiki.example.com@evil.tld/cb`
    renders as `dtxwiki.example.com@evil.tld` — the real recipient pushed behind
    text the attacker chose. Registration now rejects userinfo outright; this is
    the second layer, so nothing that reaches a person or a CSP origin can be
    read off the wrong half of the authority.
    """

    host = parsed.hostname or ""
    if ":" in host:
        # `hostname` drops the brackets an IPv6 literal needs in a URL.
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


def _loopback_redirect_origin(redirect_uri: str) -> str | None:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return None
    return f"{parsed.scheme}://{_redirect_authority(parsed)}"


def _consent_csrf_audit_workspace_id(
    *,
    session,
    user_id: UUID | None,
    requested_workspace_id: UUID | None,
) -> UUID | None:
    """Best-effort workspace context for consent-CSRF forensic audit rows.

    CSRF rejection happens before the normal authorization request validation,
    so this helper must fail closed: record a workspace only when it can be
    safely tied to the authenticated user's memberships; otherwise keep the
    ADR A11 nullable fallback.
    """

    if user_id is None:
        return None
    memberships = list_user_workspaces(session, user_id)
    if requested_workspace_id is not None and any(
        workspace.id == requested_workspace_id for workspace, _role in memberships
    ):
        return requested_workspace_id
    if len(memberships) == 1:
        workspace, _role = memberships[0]
        return workspace.id
    return None


@router.get(
    "/oauth/consents",
    response_model=OAuthConsentListResponse,
    responses={401: {"model": RESTErrorBody}},
)
def list_consents(
    request: Request,
    session_factory: SessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    """List the authenticated user's OAuth consents for the
    integrations panel. Returns 401 when the session cookie is missing or
    invalid (the panel is part of the authenticated frontend).
    """

    session_service = get_session_service(request, session_factory)
    try:
        user = current_session_user(request, session_service)
    except SessionServiceError:
        return build_rest_error_response(
            error_code="unauthorized",
            message="session is required",
            status_code=401,
        )
    page = oauth_service(request, session_factory).list_user_consents(
        user_id=user.user_id,
        limit=limit,
        cursor=cursor,
    )
    return {"data": page["items"], "next_cursor": page["next_cursor"]}


@router.patch(
    "/oauth/consents",
    response_model=OAuthConsentSettingsUpdateResponse,
    responses={
        401: {"model": RESTErrorBody},
        404: {"model": RESTErrorBody},
        422: {"model": RESTErrorBody},
    },
)
def update_consent_settings(
    payload: OAuthConsentSettingsUpdateRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    client_id: Annotated[str, Query(min_length=1)],
):
    """Update the authenticated user's policy and/or allowed-workspace set
    for one OAuth client (P10.3 T3, section 2.3/2.4).

    The unit is the whole (user, client) consent GROUP now, not one
    workspace's row -- ``client_id`` alone identifies it. Issued refresh
    rows are intentionally untouched by the TTL/sliding fields; that policy
    is read when the next child token is minted during rotation.
    ``workspace_ids``, when given, is a full-set REPLACE of the allowed
    workspaces (mirroring ``PATCH /auth/tokens/{id}``'s ``workspace_grants``).
    """

    user = _current_oauth_user(request, session_factory)
    if not isinstance(user, AuthenticatedUser):
        return user
    try:
        updated = oauth_service(request, session_factory).update_consent_settings(
            user_id=user.user_id,
            client_id=client_id,
            refresh_ttl_seconds=payload.refresh_ttl_seconds,
            update_refresh_ttl="refresh_ttl_seconds" in payload.model_fields_set,
            sliding_enabled=payload.sliding_enabled,
            update_sliding="sliding_enabled" in payload.model_fields_set,
            workspace_ids=payload.workspace_ids,
        )
    except OAuthConsentNotFoundError:
        return build_rest_error_response(
            error_code="consent_not_found",
            message="consent not found",
            status_code=404,
        )
    except OAuthConsentTTLAboveInstanceError as exc:
        return build_rest_error_response(
            error_code="refresh_ttl_above_instance_max",
            message=str(exc),
            status_code=422,
        )
    except InvalidOAuthRequestError as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=422,
        )
    return {"data": updated}


def _current_oauth_user(
    request: Request,
    session_factory: SessionFactoryDep,
) -> AuthenticatedUser | JSONResponse:
    session_service = get_session_service(request, session_factory)
    try:
        return current_session_user(request, session_service)
    except SessionServiceError:
        return build_rest_error_response(
            error_code="unauthorized",
            message="session is required",
            status_code=401,
        )


@router.get(
    "/oauth/consents/cleanup",
    response_model=OAuthConnectionCleanupResponse,
    responses={401: {"model": RESTErrorBody}},
)
def preview_connection_cleanup(
    request: Request,
    session_factory: SessionFactoryDep,
):
    user = _current_oauth_user(request, session_factory)
    if not isinstance(user, AuthenticatedUser):
        return user
    data = oauth_service(request, session_factory).cleanup_user_connections(
        user_id=user.user_id,
        dry_run=True,
    )
    return {"data": data}


@router.post(
    "/oauth/consents/cleanup",
    response_model=OAuthConnectionCleanupResponse,
    responses={401: {"model": RESTErrorBody}},
)
def cleanup_connections(
    request: Request,
    session_factory: SessionFactoryDep,
):
    user = _current_oauth_user(request, session_factory)
    if not isinstance(user, AuthenticatedUser):
        return user
    data = oauth_service(request, session_factory).cleanup_user_connections(
        user_id=user.user_id,
    )
    return {"data": data}


@router.delete(
    "/oauth/consents",
    response_model=OAuthConsentRevokeResponse,
    responses={
        401: {"model": RESTErrorBody},
        404: {"model": RESTErrorBody},
    },
)
def revoke_consent(
    request: Request,
    session_factory: SessionFactoryDep,
    client_id: Annotated[str, Query(min_length=1)],
):
    """Revoke the authenticated user's consent for a single OAuth client.
    Also revokes any outstanding refresh tokens issued under that consent so
    a subsequent ``/oauth/token`` refresh attempt fails with ``invalid_grant``.
    """

    session_service = get_session_service(request, session_factory)
    try:
        user = current_session_user(request, session_service)
    except SessionServiceError:
        return build_rest_error_response(
            error_code="unauthorized",
            message="session is required",
            status_code=401,
        )
    removed = oauth_service(request, session_factory).revoke_consent(
        user_id=user.user_id,
        client_id=client_id,
    )
    if not removed:
        return build_rest_error_response(
            error_code="consent_not_found",
            message="consent not found",
            status_code=404,
        )
    return {"data": {"revoked": True}}


@router.post("/oauth/token")
async def token(request: Request, session_factory: SessionFactoryDep):
    try:
        values = await parse_form_request_values(request)
    except InvalidOAuthRequestError as exc:
        return oauth_token_error_response(exc, 400)
    service = oauth_service(request, session_factory)
    grant_type = str(values.get("grant_type") or "")
    request.state.client_id = str(values.get("client_id") or "")
    client_ip = oauth_client_ip(request)
    client_id = str(values.get("client_id") or "")
    try:
        consume_token_slot(client_ip=client_ip, client_id=client_id)
    except OAuthRateLimitError as exc:
        inc_oauth_token_grant(grant_type=grant_type, result="rate_limited")
        return oauth_token_error_response(exc, 429)
    try:
        if grant_type == "authorization_code":
            result = service.exchange_authorization_code(
                code=str(values.get("code") or ""),
                client_id=client_id,
                redirect_uri=str(values.get("redirect_uri") or ""),
                code_verifier=str(values.get("code_verifier") or ""),
            )
        elif grant_type == "refresh_token":
            result = service.refresh_access_token(
                refresh_token=str(values.get("refresh_token") or ""),
                client_id=client_id,
                scope=(
                    str(values["scope"])
                    if values.get("scope") not in (None, "")
                    else None
                ),
            )
        elif not grant_type:
            # RFC 6749 §5.2: a missing required parameter is invalid_request.
            # Only a grant type we recognise-but-do-not-implement is
            # unsupported_grant_type.
            raise InvalidOAuthRequestError("grant_type is required")
        else:
            raise UnsupportedGrantTypeError("unsupported grant_type")
    except (InvalidOAuthGrantError, InvalidOAuthRequestError) as exc:
        inc_oauth_token_grant(
            grant_type=grant_type,
            result=exc.error_code,
            refusal_reason=getattr(exc, "refusal_reason", None),
        )
        return oauth_token_error_response(exc, 400)
    except OperationalError as exc:
        if not is_sqlite_lock_contention(exc):
            raise
        inc_oauth_token_grant(grant_type=grant_type, result="error")
        return oauth_token_busy_response()
    inc_oauth_token_grant(grant_type=grant_type, result="success")
    return JSONResponse(
        content={
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "token_type": "Bearer",
            "expires_in": result.expires_in,
            "refresh_token_expires_in": result.refresh_token_expires_in,
            "scope": result.scope,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/oauth/revoke")
async def revoke_token(request: Request, session_factory: SessionFactoryDep):
    """RFC 7009 OAuth 2.0 Token Revocation.

    Body (application/x-www-form-urlencoded):
      ``token`` — required, the token to revoke
      ``token_type_hint`` — optional, ``access_token`` | ``refresh_token``
      ``client_id`` — optional, lets a native client revoke its own token

    Auth (any one of):
      (1) ``Authorization: Bearer <PAT>`` (``dtxw_`` prefix) → may revoke
          only the authenticated user's own tokens.
      (2) ``Authorization: Bearer <session>`` (cookie or header) → same.
      (3) Form ``client_id`` matches the token's recorded client_id (native
          token-only client owns its own tokens).

    Response:
      200 ``{}`` on revoke success OR unknown token (RFC 7009 §2.2).
      401 with WWW-Authenticate header on auth failure (RFC 7009 §2.1).
      400 when the ``token`` field is missing.

    Wrong-user bearer auth is treated as ``unknown token`` and returns 200 to
    avoid revealing token existence to unauthorised callers. Native
    ``client_id`` mismatch without bearer auth returns the same 401 challenge as
    missing auth because the client self-revoke condition is not satisfied.
    """

    try:
        values = await parse_form_request_values(request)
    except InvalidOAuthRequestError as exc:
        return oauth_error_response(exc, 400)
    token_value = str(values.get("token") or "")
    if not token_value:
        return oauth_error_response(
            InvalidOAuthRequestError("token is required"),
            400,
        )
    token_type_hint = str(values.get("token_type_hint") or "")
    form_client_id = str(values.get("client_id") or "")
    service = oauth_service(request, session_factory)
    settings = request.app.state.settings

    # --- Auth-policy resolution (3-way) ---------------------------------
    revoker = _resolve_revoke_actor(
        request=request,
        session_factory=session_factory,
        service=service,
        token_value=token_value,
        form_client_id=form_client_id,
        token_type_hint=token_type_hint,
        settings=settings,
    )
    if revoker is None:
        return _revoke_unauthorized_response()

    # --- Revoke (idempotent + RFC 7009 §2.2 silent on unknown) ---------
    service.revoke_token(
        token=token_value,
        token_type_hint=token_type_hint,
        revoker_user_id=revoker.user_id,
        revoker_kind=revoker.kind,
        revoker_actor_id=revoker.actor_id,
        revoker_client_id=revoker.client_id,
    )
    return JSONResponse({}, status_code=200)


def _resolve_revoke_actor(
    *,
    request: Request,
    session_factory,
    service: OAuthService,
    token_value: str,
    form_client_id: str,
    token_type_hint: str,
    settings,
) -> RevokeActor | None:
    """Return the resolved /oauth/revoke actor, or ``None`` if no auth path
    passes.

    The first successful path wins; bearer-based paths take priority over
    native ``client_id`` form-match so a stolen client_id cannot bypass
    user-bound checks. OAuth JWT bearers additionally carry their client
    claim into the service layer, so they cannot revoke tokens for another
    client even when the underlying user is the same. P10.3 T3: no workspace
    claim any more -- RFC 7009 revoke always kills the whole (user, client)
    family (section 2.3), so there is nothing left to restrict it by.
    """

    # Bearer path: PAT or OAuth access JWT or session cookie.
    actor = _try_bearer_revoker(
        request=request, session_factory=session_factory, settings=settings,
    )
    if actor is not None:
        return actor

    # Native client_id self-revoke (ADR A4 case 3). The endpoint matches
    # form.client_id against the token's recorded client_id. Refresh
    # tokens carry the client_id in the DB row; access JWTs carry it in
    # the ``client_id`` claim.
    if form_client_id:
        token_client_id: str | None
        if token_type_hint == "access_token":
            token_client_id = service.get_access_token_client_id(token_value)
        else:
            token_client_id = service.get_refresh_token_client_id(token_value)
            if token_client_id is None and token_type_hint != "refresh_token":
                token_client_id = service.get_access_token_client_id(token_value)
        if token_client_id is not None and token_client_id == form_client_id:
            return RevokeActor(
                user_id=None,
                kind="oauth_client",
                actor_id=form_client_id,
                client_id=form_client_id,
            )
    return None


def _try_bearer_revoker(
    *,
    request: Request,
    session_factory,
    settings,
) -> RevokeActor | None:
    """Resolve a PAT, OAuth JWT, or session bearer into a revoke actor.

    Returns ``None`` when no usable bearer/session is present.
    """

    from backend.services.api_token_service import (
        ApiTokenService,
        InvalidApiTokenError,
    )
    from backend.services.credential_hashing import CredentialHasher

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        bearer = authorization.removeprefix("Bearer ").removeprefix(
            "bearer "
        ).strip()
        if bearer.startswith("dtxw_"):
            try:
                actor = ApiTokenService(
                    session_factory=session_factory,
                    credential_hasher=CredentialHasher(),
                ).authenticate_token(bearer)
            except InvalidApiTokenError:
                return None
            return RevokeActor(
                user_id=actor.user.user_id,
                kind="user_session",
                actor_id=str(actor.user.user_id),
            )
        try:
            actor = authenticate_bearer_token(request, bearer)
        except HTTPException:
            return None
        if (
            actor.token_kind == "oauth_jwt"
            and actor.user_id is not None
            and actor.client_id is not None
        ):
            return RevokeActor(
                user_id=actor.user_id,
                kind="oauth_client",
                actor_id=actor.client_id,
                client_id=actor.client_id,
            )
        return None
    session_service = get_session_service(request, session_factory)
    try:
        user = current_session_user(request, session_service)
    except SessionServiceError:
        return None
    return RevokeActor(
        user_id=user.user_id,
        kind="user_session",
        actor_id=str(user.user_id),
    )


def _revoke_unauthorized_response() -> JSONResponse:
    """401 with WWW-Authenticate header per RFC 7009 §2.1.

    The challenge advertises the bearer scheme so RFC 7009 §2.1-compliant
    clients (and the dev console) re-attempt with credentials. The body
    uses the OAuth standard ``{"error_code","message"}`` envelope so the
    /oauth/* error baseline (ADR A9) stays consistent.
    """

    return JSONResponse(
        status_code=401,
        content={
            "error_code": "unauthorized",
            "message": "revoke auth failed",
        },
        headers={"WWW-Authenticate": 'Bearer realm="dtxwiki-oauth"'},
    )


def oauth_service(request: Request, session_factory) -> OAuthService:
    return OAuthService(
        session_factory=session_factory,
        settings=request.app.state.settings,
        public_base_url=public_base_url_from_request(request),
        # Consent management reads workspace-plane MCPClient rows; without the
        # router those paths raise instead of reporting every connection
        # healthy off a control session that cannot see the table.
        database_provider=get_container_from_app(request).db,
    )


def oauth_client_ip(request: Request) -> str:
    return resolve_remote_ip(request) or "unknown"


async def parse_request_values(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    body = (await request.body()).decode()
    parsed = {}
    for key, values in parse_qs(body).items():
        parsed[key] = values[-1] if values else ""
    return parsed


async def parse_form_request_values(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        raise InvalidOAuthRequestError(
            "request body must be application/x-www-form-urlencoded"
        )
    return await parse_request_values(request)


async def parse_form_workspace_ids(
    request: Request, values: dict[str, object]
) -> list[str]:
    """Read every ``workspace_ids`` value from a POST body without the
    single-value collapse ``parse_request_values`` applies.

    A JSON caller sends an actual list under this key (already present in
    ``values``, since ``request.json()`` is not collapsed); a form caller
    repeats the field once per checked checklist box, which
    ``parse_request_values`` would otherwise reduce to its last value only.
    Starlette caches ``request.body()``, so reading it again here is not a
    second stream read.
    """

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        raw = values.get("workspace_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if raw not in (None, ""):
            return [str(raw)]
        return []
    body = (await request.body()).decode()
    return parse_qs(body).get("workspace_ids", [])


def authorization_kwargs(values: dict[str, object]) -> dict[str, object]:
    return {
        "response_type": str(values.get("response_type") or ""),
        "client_id": str(values.get("client_id") or ""),
        "redirect_uri": str(values.get("redirect_uri") or ""),
        "code_challenge": str(values.get("code_challenge") or ""),
        "code_challenge_method": str(values.get("code_challenge_method") or ""),
        "scope": str(values.get("scope") or "mcp:read"),
        "state": str(values.get("state") or ""),
    }


def parse_workspace_id_hints(raw_values: list[str]) -> tuple[UUID, ...]:
    """Best-effort UUID parse of the GET ``workspace_id`` preselect hint(s).

    A malformed value is dropped rather than raising -- section 2.3 makes
    this hint informational only (membership-validated inside
    ``OAuthService.authorize_workspace_options``), never a bypass of the
    checklist.
    """

    parsed: list[UUID] = []
    for raw in raw_values:
        try:
            parsed.append(UUID(raw))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


def next_url_from_request(request: Request) -> str:
    # The routed ASGI path, not request.url: Starlette rebuilds the URL from
    # the client's Host header, which could turn this login redirect target
    # into another path or a protocol-relative URL.
    path = str(request.scope.get("path") or "/")
    if request.query_params:
        return path + "?" + str(request.query_params)
    return path


def redirect_with_params(redirect_uri: str, params: dict[str, str]) -> str:
    separator = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{separator}{urlencode(params)}"


# Seconds the handoff page shows before forwarding. The authorization code
# expires 60s after issue (oauth_service.CODE_TTL_SECONDS), and that clock is
# already running here, so this must stay a small fraction of it.
HANDOFF_FORWARD_SECONDS = 5

# Nothing in the flow caps a registered host's length, so an attacker can pad
# one until the real host scrolls out of the 620px panel.
MAX_DESTINATION_HOST_CHARS = 80


def authorization_handoff_page(forward_url: str, *, nonce: str) -> str:
    """The page dtxWiki shows after Allow, before handing back to the client.

    A browser used to go straight from Allow to whatever the client's loopback
    server renders -- usually an unstyled "Authentication complete. You may
    close this window." Users read that as dtxWiki's own screen and reported it
    as a missing design. dtxWiki cannot replace it: the client owns that page,
    and the server cannot call the loopback address itself (on a remote
    deployment `127.0.0.1` is the *server's* loopback, and letting a request
    name the port would be an SSRF). What it can do is say what happened, in
    its own voice, and warn that the bare page coming next is normal.

    Forwarding is a top-level navigation, which no directive in the response
    CSP restricts, so this needs no `form-action` widening.

    The URL is written into the page exactly once, HTML-escaped, and the script
    reads it back off the anchor. Serializing it a second time into a JS string
    literal is what an earlier version did, and `json.dumps` does not escape
    `</scr`+`ipt>`: since DCR is unauthenticated and `validate_redirect_uri`
    places no character restriction on an https path, a registered redirect_uri
    could close the script element and inject markup onto this origin. One
    value, one escaping context, no JS-string sink.
    """

    href = escape(forward_url, quote=True)
    return f"""
<!doctype html>
<html>
  <head>
    <title>Returning to the app</title>
    {oauth_page_styles()}
  </head>
  <body>
    <main class="oauth-shell">
      <section class="oauth-panel">
        <p class="oauth-kicker">dtxWiki MCP</p>
        <h1>Access approved</h1>
        <p class="oauth-copy">
          Sending you back to the app to finish signing in
          (<span id="oauth-countdown">{HANDOFF_FORWARD_SECONDS}</span>s).
          Sign-in is not complete until the app receives this, and the approval
          expires in about a minute, so do not leave this page open.
        </p>
        <p class="oauth-copy" lang="ko">
          로그인을 마치도록 앱으로 돌려보내는 중입니다
          (<span id="oauth-countdown-ko">{HANDOFF_FORWARD_SECONDS}</span>초).
          앱이 이 값을 받아야 로그인이 끝나며 승인은 약 1분 뒤 만료되므로 이
          페이지를 열어둔 채 두지 마세요.
        </p>
        <p class="oauth-copy oauth-note">
          The next screen — often a bare "Authentication complete. You may close
          this window." — is drawn by the app you are signing in to, not by
          dtxWiki. Seeing it means this worked.
        </p>
        <p class="oauth-copy oauth-note" lang="ko">
          다음에 뜨는 화면(대개 아무 장식 없는 "Authentication complete. You may
          close this window.")은 dtxWiki가 아니라 로그인 대상 앱이 그리는
          것입니다. 그 화면이 보이면 정상입니다.
        </p>
        <div class="oauth-actions">
          <a
            class="oauth-button oauth-button-primary"
            id="oauth-forward"
            href="{href}"
            >Continue now / 지금 계속</a
          >
        </div>
        <script nonce="{escape(nonce)}">
          (function () {{
            var link = document.getElementById("oauth-forward");
            if (!link) return;
            var left = {HANDOFF_FORWARD_SECONDS};
            var cells = [
              document.getElementById("oauth-countdown"),
              document.getElementById("oauth-countdown-ko"),
            ];
            var timer = setInterval(function () {{
              left -= 1;
              cells.forEach(function (cell) {{
                if (cell) cell.textContent = left < 0 ? 0 : left;
              }});
              if (left <= 0) {{
                clearInterval(timer);
                window.location.assign(link.href);
              }}
            }}, 1000);
          }})();
        </script>
      </section>
    </main>
  </body>
</html>
"""


def _consent_grant_section(raw_scope: str) -> str:
    """Say what the scope lets the client do, in words, before the buttons.

    `validate_authorization_request` has already rejected any scope outside
    `ALLOWED_OAUTH_SCOPES`, so every token here resolves to copy.
    """

    blocks: list[str] = []
    grants_write = False
    for _, copy in scope_copy_for(raw_scope):
        grants_write = grants_write or "draft-write" in copy.covers
        allow = "".join(f"<li>{escape(line)}</li>" for line in copy.grants)
        allow_ko = "".join(f"<li>{escape(line)}</li>" for line in copy.grants_ko)
        blocks.append(
            f"""          <section class="oauth-grant">
            <h2>{escape(copy.title)}</h2>
            <h2 lang="ko">{escape(copy.title_ko)}</h2>
            <ul class="oauth-grant-allow">{allow}</ul>
            <ul class="oauth-grant-allow" lang="ko">{allow_ko}</ul>
          </section>"""
        )
    # One limits block for the whole request, not one per scope: the limits a
    # narrower scope names are only true if no other requested scope lifts them.
    denials = surviving_denials(raw_scope)
    if denials:
        deny = "".join(f"<li>{escape(item.text)}</li>" for item in denials)
        deny_ko = "".join(f"<li>{escape(item.text_ko)}</li>" for item in denials)
        blocks.append(
            f"""          <section class="oauth-grant oauth-grant-limits">
            <p class="oauth-grant-label">It cannot / 할 수 없는 것</p>
            <ul class="oauth-grant-deny">{deny}</ul>
            <ul class="oauth-grant-deny" lang="ko">{deny_ko}</ul>
          </section>"""
        )
    if grants_write:
        caveat = escape(DRAFT_REVIEW_CAVEAT)
        caveat_ko = escape(DRAFT_REVIEW_CAVEAT_KO)
        blocks.append(
            f"""          <p class="oauth-copy oauth-note">{caveat}</p>
          <p class="oauth-copy oauth-note" lang="ko">{caveat_ko}</p>"""
        )
    return "\n".join(blocks)


def _consent_destination_section(redirect_uri: str) -> str:
    """Show where the sign-in code is about to be sent.

    This is the one field on the page an attacker cannot fake into looking
    harmless, and it was the only one the old screen never showed: a client
    name, an icon and a scope string are all self-reported, but the redirect
    target is enforced by the flow. A loopback address means the code stays on
    this machine; anything else is a remote party worth naming loudly.

    That claim only holds if the host shown is the host that receives the code,
    so this reads the parsed host and clamps its length. `netloc` would carry
    the userinfo, and an unbounded host can push the real one out of the panel.
    """

    loopback = _loopback_redirect_origin(redirect_uri)
    if loopback:
        return f"""        <p class="oauth-destination">
          The sign-in code goes to a program on this computer
          (<code>{escape(loopback)}</code>). It does not leave your machine.
        </p>
        <p class="oauth-destination" lang="ko">
          로그인 코드는 이 컴퓨터에서 실행 중인 프로그램
          (<code>{escape(loopback)}</code>)으로 전달되며, 이 컴퓨터를 벗어나지
          않습니다.
        </p>"""
    host = _redirect_authority(urlsplit(redirect_uri))
    if len(host) > MAX_DESTINATION_HOST_CHARS:
        host = f"{host[:MAX_DESTINATION_HOST_CHARS]}…"
    return f"""        <p class="oauth-destination oauth-destination-remote">
          The sign-in code will be sent to <strong>{escape(host)}</strong>.
          Continue only if you recognise it.
        </p>
        <p class="oauth-destination oauth-destination-remote" lang="ko">
          로그인 코드가 <strong>{escape(host)}</strong>(으)로 전송됩니다.
          아는 주소일 때만 계속하세요.
        </p>"""


def _consent_workspace_checklist(
    options: list[AuthorizeWorkspaceOption], *, nonce: str
) -> str:
    """Section 2.3/3.3: the workspace checklist injected into the consent
    screen. Default-checked rows are supplied by
    ``OAuthService.authorize_workspace_options``; this only renders them.
    Search/select-all/counter/zero-disables local filter is a nonce'd inline
    script -- no network request (section 3.3).
    """

    if not options:
        return """
        <div class="oauth-workspace-picker oauth-workspace-picker-empty">
          <p class="oauth-copy oauth-note">
            No workspace is available to grant access to.
          </p>
          <p class="oauth-copy oauth-note" lang="ko">
            접근을 허용할 수 있는 workspace가 없습니다.
          </p>
        </div>
        """
    show_search = len(options) > 7
    rows = "\n".join(
        f"""          <label class="oauth-workspace-row" role="option" data-search="{
            escape((option.title + " " + option.slug).lower())
        }">
            <input
              type="checkbox"
              name="workspace_ids"
              value="{option.id}"
              {"checked" if option.checked else ""}
            >
            <span class="oauth-workspace-row-title">{escape(option.title)}</span>
            <span class="oauth-workspace-row-slug">{escape(option.slug)}</span>
          </label>"""
        for option in options
    )
    search_markup = (
        """
          <input
            type="search"
            class="oauth-workspace-search"
            id="oauth-workspace-search"
            placeholder="Search workspaces / workspace 검색"
            aria-label="Search workspaces / workspace 검색"
          >"""
        if show_search
        else ""
    )
    return f"""
        <div
          class="oauth-workspace-picker"
          role="listbox"
          aria-multiselectable="true"
          aria-label="Workspaces / workspace 선택"
        >
          <p class="oauth-copy oauth-note">
            Choose which workspaces may access this connection. This is not
            limited to your current workspace.
          </p>
          <p class="oauth-copy oauth-note" lang="ko">
            이 연결이 접근할 workspace를 고르세요. 현재 workspace로 한정되지
            않습니다.
          </p>{search_markup}
          <div class="oauth-workspace-picker-toolbar">
            <button
              type="button"
              class="oauth-link-button"
              data-action="select-all"
            >Select all shown / 표시된 항목 모두 선택</button>
            <button
              type="button"
              class="oauth-link-button"
              data-action="clear-all"
            >Clear / 모두 해제</button>
            <span class="oauth-workspace-picker-counter" data-counter></span>
          </div>
          <div class="oauth-workspace-picker-list">
{rows}
          </div>
          <p
            class="oauth-copy oauth-note oauth-workspace-picker-zero-warning"
            data-zero-warning
            hidden
          >
            Select at least one workspace, or this connection cannot access
            anything.
          </p>
          <p
            class="oauth-copy oauth-note oauth-workspace-picker-zero-warning"
            data-zero-warning
            lang="ko"
            hidden
          >
            workspace를 하나 이상 선택하세요. 선택된 workspace가 없으면 이
            연결은 아무 workspace에도 접근할 수 없습니다.
          </p>
        </div>
        <script nonce="{escape(nonce)}">
          (function () {{
            var picker = document.querySelector(".oauth-workspace-picker");
            if (!picker) return;
            var checkboxes = Array.prototype.slice.call(
              picker.querySelectorAll('input[name="workspace_ids"]')
            );
            var counter = picker.querySelector("[data-counter]");
            var warnings = picker.querySelectorAll("[data-zero-warning]");
            var allowButton = document.querySelector(
              'button[name="decision"][value="allow"]'
            );
            var search = document.getElementById("oauth-workspace-search");

            function visibleCheckboxes() {{
              return checkboxes.filter(function (box) {{
                var row = box.closest(".oauth-workspace-row");
                return row && !row.hidden;
              }});
            }}

            function refresh() {{
              var checkedCount = checkboxes.filter(function (box) {{
                return box.checked;
              }}).length;
              if (counter) {{
                counter.textContent = checkedCount + " / " + checkboxes.length
                  + " selected";
              }}
              var zero = checkedCount === 0;
              warnings.forEach(function (node) {{
                node.hidden = !zero;
              }});
              if (allowButton) allowButton.disabled = zero;
            }}

            checkboxes.forEach(function (box) {{
              box.addEventListener("change", refresh);
            }});

            var selectAllBtn = picker.querySelector('[data-action="select-all"]');
            if (selectAllBtn) {{
              selectAllBtn.addEventListener("click", function () {{
                visibleCheckboxes().forEach(function (box) {{
                  box.checked = true;
                }});
                refresh();
              }});
            }}
            var clearAllBtn = picker.querySelector('[data-action="clear-all"]');
            if (clearAllBtn) {{
              clearAllBtn.addEventListener("click", function () {{
                checkboxes.forEach(function (box) {{
                  box.checked = false;
                }});
                refresh();
              }});
            }}
            if (search) {{
              search.addEventListener("input", function () {{
                var needle = search.value.trim().toLowerCase();
                picker.querySelectorAll(".oauth-workspace-row").forEach(
                  function (row) {{
                    var haystack = row.getAttribute("data-search") || "";
                    row.hidden = needle.length > 0
                      && haystack.indexOf(needle) === -1;
                  }}
                );
              }});
            }}

            refresh();
          }})();
        </script>
    """


def consent_form(
    request: Request,
    client,
    workspace_options: list[AuthorizeWorkspaceOption],
    *,
    nonce: str,
) -> str:
    """P10.3 T3 (section 2.3/3.3): the account-unbound consent screen. What
    used to be a single hidden ``workspace_id`` field is now a checklist of
    every workspace the user is a member of, default-checked per
    ``OAuthService.authorize_workspace_options``.
    """

    params = request.query_params
    # `decision` is excluded: the submit handler re-adds it from
    # `event.submitter`, and where that is undefined (Safari 15.0-15.3) the
    # only `decision` left in the body would be one the caller put in the
    # authorize URL. Reflecting the query param let a link decide what the
    # button press meant. `workspace_id` (the GET preselect hint) is also
    # excluded -- the checklist's own `workspace_ids` checkboxes are the only
    # workspace selection the POST body carries now.
    hidden_values = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"workspace_id", "workspace_ids", "decision"}
    ]
    hidden = "\n".join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
        for key, value in hidden_values
    )
    client_name = escape(str(getattr(client, "client_name", None) or "MCP client"))
    client_id = escape(
        str(getattr(client, "id", None) or params.get("client_id") or "")
    )
    trust_label = (
        (
            "동적 클라이언트 — 이 로그인 중 등록되었으며, 신원은 독립적으로 "
            "검증되지 않았습니다. (Dynamic client — Registered during this "
            "login; its identity has not been independently verified.)"
        )
        if getattr(client, "created_via", "") == "dcr"
        else "등록된 클라이언트 (Registered client)"
    )
    raw_scope = str(params.get("scope") or "mcp:read")
    scope = escape(raw_scope)
    redirect_uri = str(params.get("redirect_uri") or "")
    grant_section = _consent_grant_section(raw_scope)
    destination_section = _consent_destination_section(redirect_uri)
    workspace_checklist = _consent_workspace_checklist(workspace_options, nonce=nonce)
    allow_disabled = not any(option.checked for option in workspace_options)
    return f"""
<!doctype html>
<html>
  <head>
    <title>dtxWiki OAuth Consent</title>
    {oauth_page_styles()}
  </head>
  <body>
    <main class="oauth-shell">
      <section class="oauth-panel">
        <p class="oauth-kicker">dtxWiki MCP</p>
        <h1>Authorize dtxWiki MCP access</h1>
        <p class="oauth-copy">
          <strong>{client_name}</strong>
          <span class="oauth-claim">self-reported · unverified / 자칭 · 미검증</span>
          is asking for access.
        </p>
        <p class="oauth-copy oauth-note">
          The name above is whatever the client sent; dtxWiki has not checked
          it. Approve based on what you started, not on the name.
        </p>
        <p class="oauth-copy oauth-note" lang="ko">
          위 이름은 클라이언트가 스스로 보낸 값이며 dtxWiki가 확인한 것이
          아닙니다. 이름이 아니라 방금 본인이 시작한 작업인지로 판단하세요.
        </p>
{grant_section}
{destination_section}
        <p class="oauth-copy oauth-note">
          You can revoke this at any time in dtxWiki under
          Settings → MCP access → OAuth apps.
        </p>
        <p class="oauth-copy oauth-note" lang="ko">
          승인 후에도 dtxWiki의 설정 → MCP 접근 → OAuth 앱에서 언제든지 해제할
          수 있습니다.
        </p>
        <details class="oauth-technical">
          <summary>Technical details / 기술 세부정보</summary>
          <dl class="oauth-details">
            <div><dt>Requested scopes</dt><dd>{scope}</dd></div>
            <div><dt>Client ID</dt><dd>{client_id}</dd></div>
            <div><dt>Redirect URI</dt><dd>{escape(redirect_uri)}</dd></div>
            <div><dt>Trust</dt><dd>{escape(trust_label)}</dd></div>
          </dl>
        </details>
        <form method="post" action="/oauth/authorize" class="oauth-actions">
        {hidden}
{workspace_checklist}
          <button
            class="oauth-button oauth-button-secondary"
            type="submit"
            name="decision"
            value="deny"
          >Deny</button>
          <button
            class="oauth-button oauth-button-primary"
            type="submit"
            name="decision"
            value="allow"
            {"disabled" if allow_disabled else ""}
          >Allow</button>
        </form>
        <script nonce="{escape(nonce)}">
          const form = document.querySelector(".oauth-actions");
          form.addEventListener("submit", (event) => {{
            if (form.dataset.submitted) {{
              event.preventDefault();
              return;
            }}
            form.dataset.submitted = "true";
            const submittedBy = event.submitter;
            if (submittedBy) {{
              const decision = document.createElement("input");
              decision.type = "hidden";
              decision.name = "decision";
              decision.value = submittedBy.value;
              form.appendChild(decision);
            }}
            form.querySelectorAll("button").forEach((button) => {{
              button.disabled = true;
            }});
            if (submittedBy) submittedBy.textContent = "Authorizing…";
          }});
        </script>
      </section>
    </main>
  </body>
</html>
"""


def oauth_authorize_error_response(exc: Exception, status_code: int) -> HTMLResponse:
    title = "OAuth request could not continue"
    message = escape(str(exc) or "The OAuth request is invalid.")
    error_code = escape(getattr(exc, "error_code", "invalid_request"))
    return HTMLResponse(
        f"""
<!doctype html>
<html>
  <head>
    <title>dtxWiki OAuth Error</title>
    {oauth_page_styles()}
  </head>
  <body>
    <main class="oauth-shell">
      <section class="oauth-panel oauth-panel-error">
        <p class="oauth-kicker">dtxWiki MCP</p>
        <h1>{escape(title)}</h1>
        <p class="oauth-copy">{message}</p>
        <dl class="oauth-details">
          <div><dt>Error code</dt><dd>{error_code}</dd></div>
        </dl>
        <div class="oauth-actions">
          <a
            class="oauth-button oauth-button-secondary"
            href="/settings/integrations"
          >Open MCP settings</a>
          <a class="oauth-button oauth-button-primary" href="/">Open dtxWiki</a>
        </div>
      </section>
    </main>
  </body>
</html>
""",
        status_code=status_code,
    )


def request_accepts_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


def oauth_page_styles() -> str:
    return """
    <style>
      :root {
        color-scheme: light;
        font-family:
          Inter,
          ui-sans-serif,
          system-ui,
          -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          sans-serif;
        color: #17202a;
        background: #f4f6f8;
      }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; background: #f4f6f8; }
      .oauth-shell {
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 32px 18px;
      }
      .oauth-panel {
        width: min(100%, 620px);
        border: 1px solid #d6dde5;
        border-radius: 8px;
        background: #fff;
        box-shadow: 0 12px 30px rgba(23, 32, 42, 0.08);
        padding: 28px;
      }
      .oauth-panel-error { border-color: #f0c3bd; }
      .oauth-kicker {
        margin: 0 0 8px;
        color: #4f6377;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
      }
      h1 { margin: 0; font-size: 28px; line-height: 1.2; letter-spacing: 0; }
      .oauth-copy { margin: 12px 0 22px; color: #425466; line-height: 1.55; }
      .oauth-copy strong { color: #17202a; }
      .oauth-note { margin: 0 0 8px; font-size: 13px; color: #5f7184; }
      .oauth-claim {
        display: inline-block;
        margin: 0 2px;
        border: 1px solid #e0c68f;
        border-radius: 999px;
        padding: 1px 8px;
        background: #fdf6e6;
        color: #7a5c12;
        font-size: 11px;
        font-weight: 700;
        vertical-align: 2px;
      }
      .oauth-grant {
        margin: 18px 0;
        border: 1px solid #e5eaf0;
        border-radius: 6px;
        padding: 14px 16px;
      }
      .oauth-grant h2 { margin: 0 0 8px; font-size: 15px; }
      .oauth-grant h2[lang="ko"] { margin: -6px 0 8px; color: #5f7184; }
      .oauth-grant ul { margin: 0 0 8px; padding-left: 20px; line-height: 1.55; }
      .oauth-grant-allow { color: #23415c; }
      .oauth-grant-deny { color: #6b5140; }
      .oauth-grant-label {
        margin: 12px 0 6px;
        color: #5f7184;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
      }
      .oauth-destination {
        margin: 0 0 8px;
        border-left: 3px solid #7fa8d9;
        padding: 6px 0 6px 12px;
        color: #23415c;
        line-height: 1.55;
      }
      .oauth-destination-remote {
        border-left-color: #d98b6b;
        color: #7a3d1c;
      }
      /* Both, not just `code`: the remote branch names the host in `strong`,
         and an unwrapped one pushes the part that matters off the panel. */
      .oauth-destination code,
      .oauth-destination strong { overflow-wrap: anywhere; }
      .oauth-technical { margin: 18px 0 22px; }
      .oauth-technical summary {
        color: #5f7184;
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
      }
      .oauth-technical .oauth-details { margin: 10px 0 0; }
      .oauth-details {
        margin: 0 0 24px;
        border-top: 1px solid #e5eaf0;
      }
      .oauth-details div {
        display: grid;
        grid-template-columns: 150px minmax(0, 1fr);
        gap: 16px;
        padding: 12px 0;
        border-bottom: 1px solid #e5eaf0;
      }
      dt { color: #5f7184; font-size: 13px; font-weight: 700; }
      dd { margin: 0; overflow-wrap: anywhere; }
      dd span { color: #66788a; }
      .oauth-actions {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        flex-wrap: wrap;
      }
      .oauth-button {
        border: 1px solid #bdc9d6;
        border-radius: 6px;
        padding: 10px 16px;
        font: inherit;
        font-weight: 700;
        text-decoration: none;
        cursor: pointer;
      }
      .oauth-button-primary {
        border-color: #1d4ed8;
        background: #1d4ed8;
        color: #fff;
      }
      .oauth-button-secondary {
        background: #fff;
        color: #263747;
      }
      .oauth-button:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }
      .oauth-workspace-picker {
        margin: 4px 0 18px;
        border: 1px solid #e5eaf0;
        border-radius: 6px;
        padding: 14px 16px;
      }
      .oauth-workspace-picker-empty {
        border-color: #f0c3bd;
      }
      .oauth-workspace-search {
        width: 100%;
        margin: 4px 0 10px;
        border: 1px solid #d6dde5;
        border-radius: 6px;
        padding: 8px 10px;
        font: inherit;
      }
      .oauth-workspace-picker-toolbar {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
        margin: 0 0 8px;
      }
      .oauth-link-button {
        border: none;
        background: none;
        padding: 0;
        color: #1d4ed8;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        text-decoration: underline;
      }
      .oauth-workspace-picker-counter {
        margin-left: auto;
        color: #5f7184;
        font-size: 13px;
        font-weight: 700;
      }
      .oauth-workspace-picker-list {
        max-height: 260px;
        overflow-y: auto;
        border: 1px solid #e5eaf0;
        border-radius: 6px;
      }
      .oauth-workspace-row {
        display: flex;
        align-items: baseline;
        gap: 10px;
        padding: 10px 12px;
        border-bottom: 1px solid #eef1f5;
        cursor: pointer;
      }
      .oauth-workspace-row:last-child { border-bottom: none; }
      .oauth-workspace-row:hover { background: #f8fafc; }
      .oauth-workspace-row-title {
        color: #17202a;
        font-weight: 700;
      }
      .oauth-workspace-row-slug { color: #66788a; }
      .oauth-workspace-picker-zero-warning {
        margin: 10px 0 0;
        color: #7a3d1c;
      }
      @media (max-width: 560px) {
        .oauth-panel { padding: 22px; }
        .oauth-details div { grid-template-columns: 1fr; gap: 4px; }
        .oauth-actions { justify-content: stretch; }
        .oauth-button { width: 100%; text-align: center; }
      }
    </style>
    """


def oauth_error_response(exc: Exception, status_code: int) -> JSONResponse:
    return build_rest_error_response(
        error_code=getattr(exc, "error_code", "invalid_request"),
        message=str(exc) or "invalid OAuth request",
        status_code=status_code,
    )


def oauth_token_error_response(exc: Exception, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={
            "error": getattr(exc, "error_code", "invalid_request"),
            "error_description": str(exc) or "invalid OAuth request",
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def is_sqlite_lock_contention(exc: OperationalError) -> bool:
    """Match only SQLite busy/locked failures, including extended result codes."""

    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    error_code = getattr(original, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    primary_code = error_code & 0xFF
    return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def oauth_token_busy_response() -> JSONResponse:
    """Return retry guidance without inventing an OAuth token error code."""

    return JSONResponse(
        content={
            "message": "token service is temporarily unavailable; retry later",
        },
        status_code=503,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Retry-After": "1",
        },
    )
