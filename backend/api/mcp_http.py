from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from mcp import types as mcp_types
from mcp.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Message, Receive, Scope, Send

from backend.api.mcp_session_registry import (
    discard_sessions,
    record_session_openings,
    sessions_opened_during_request,
)
from backend.app.container import DatabaseProvider, ToolCallContext
from backend.app.settings import Settings
from backend.app.state import (
    get_container_from_app,
    get_page_service_from_app,
    get_session_factory_from_app,
)
from backend.context_media.domain.policy import MAX_CONTEXT_IMAGE_UPLOAD_BYTES
from backend.domain.credential_input import authorization_header_is_valid
from backend.mcp.protocol.instructions import MCP_SERVER_INSTRUCTIONS
from backend.mcp.protocol.lifecycle import enforce_actor_mcp_surface_lifecycle
from backend.mcp.protocol.prompts_resources import (
    get_dtxwiki_prompt,
    list_dtxwiki_prompts,
    list_dtxwiki_resource_templates,
    list_dtxwiki_resources,
    read_dtxwiki_resource,
)
from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.protocol.server import create_mcp_server
from backend.mcp.protocol.session_pool import (
    manager_has_session,
    manager_session_pool,
)
from backend.mcp.protocol.tool_descriptors import (
    list_dtxwiki_tools,
    mcp_server_version,
)
from backend.mcp.scopes import (
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    has_required_scope,
    visible_tool_names,
)
from backend.policy.allowed_hosts import mcp_transport_allowlists
from backend.policy.auth import (
    ActorContext,
    authenticate_bearer_token,
    public_base_url_from_request,
)
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    rebind_actor_to_workspace_access,
    require_workspace_role,
)
from backend.policy.trust_boundaries import (
    Action,
    actor_with_target_workspace_scope,
    check_actor_can_access,
    check_restricted_library_allowed,
    required_role_to_action,
)
from backend.policy.workspace_resolution import (
    WorkspaceAmbiguityError,
    resolve_and_check,
    resolve_workspace_for_actor,
)
from backend.services.page_service import PageService
from backend.services.read_errors import RejectedByPolicyError
from backend.services.restricted_library_policy import (
    resolve_restricted_library_target,
)

logger = logging.getLogger("dtxwiki.mcp.http")

router = APIRouter(tags=["mcp"])
MCP_REQUEST_JSON_OVERHEAD_BYTES = 64 * 1024
MCP_MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_CONTEXT_IMAGE_UPLOAD_BYTES + 2) // 3)
MCP_MAX_REQUEST_BODY_BYTES = (
    MCP_MAX_ENCODED_IMAGE_BYTES + MCP_REQUEST_JSON_OVERHEAD_BYTES
)
# A stateful session nobody has sent a request on for this long is closed;
# the client re-initializes on the 404 that follows.
MCP_SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60


def mcp_transport_security(settings: Settings) -> TransportSecuritySettings:
    """The SDK's DNS-rebinding check, fed from the app's Host allowlist.

    HostAllowlistMiddleware already refuses foreign hosts before /mcp is
    routed; this repeats the Host check inside the transport and adds the
    Origin check, so a browser page on another origin cannot drive /mcp.
    Requests without an Origin header (every non-browser MCP client) pass.
    """

    allowed = settings.effective_allowed_hosts
    if allowed is None:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    allowed_hosts, allowed_origins = mcp_transport_allowlists(
        allowed,
        extra_origins=settings.cors_origin_list,
    )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


_SESSION_MANAGER_PARAMETERS = frozenset(
    inspect.signature(StreamableHTTPSessionManager.__init__).parameters
)


def _new_session_manager(**kwargs: Any) -> StreamableHTTPSessionManager:
    """Build a manager, keeping our body bound on SDKs that add their own.

    Newer SDK releases default to a 4 MiB request-body cap of their own,
    below the base64 image payloads /mcp accepts; /mcp's reader already
    bounds the body (MCP_MAX_REQUEST_BODY_BYTES), so the SDK is given the
    same bound wherever it takes one.
    """

    if "max_request_body_size" in _SESSION_MANAGER_PARAMETERS:
        kwargs.setdefault("max_request_body_size", MCP_MAX_REQUEST_BODY_BYTES)
    return StreamableHTTPSessionManager(**kwargs)


async def _discard_unowned_mcp_sessions(
    app_state,
    manager: StreamableHTTPSessionManager,
    session_ids: list[str],
) -> None:
    """Close sessions this request opened but never bound to a bearer.

    A session gets its owner when its initialize response succeeds. Anything
    else the request left in the SDK pool -- an initialize the SDK refused
    (Host/Origin check, malformed request), a client that went away, an owner
    ledger already at capacity -- is unusable by anyone and would otherwise
    hold a server task until the idle timeout.
    """

    owners = getattr(app_state, "mcp_session_owners", None) or {}
    await discard_sessions(
        manager,
        [session_id for session_id in session_ids if session_id not in owners],
    )


def _with_utf8_sse_content_type(
    headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Return ASGI headers with one UTF-8 charset on every SSE media type.

    The SDK owns the response headers, so this transport shim changes only an
    existing ``text/event-stream`` Content-Type value.  Other parameters and
    all unrelated headers are preserved in their original order.
    """

    normalized_headers: list[tuple[bytes, bytes]] = []
    for key, value in headers:
        if key.lower() != b"content-type":
            normalized_headers.append((key, value))
            continue

        segments = _split_mime_parameters(value.decode("latin-1"))
        media_type = segments[0].strip()
        if media_type.lower() != "text/event-stream":
            normalized_headers.append((key, value))
            continue

        parameters = []
        for segment in segments[1:]:
            parameter = segment.strip()
            if not parameter:
                continue
            name = parameter.partition("=")[0]
            if name.strip().lower() == "charset":
                continue
            parameters.append(parameter)
        normalized = "; ".join([media_type, *parameters, "charset=utf-8"])
        normalized_headers.append((key, normalized.encode("latin-1")))
    return normalized_headers


def _split_mime_parameters(value: str) -> list[str]:
    """Split a MIME value on unquoted semicolons without rewriting values."""

    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == ";":
            segments.append(value[start:index])
            start = index + 1
    segments.append(value[start:])
    return segments


def _mcp_manager_session_pool(manager):
    return manager_session_pool(manager)


def _authenticate_mcp_actor(
    request: Request, token: str
) -> ActorContext | JSONResponse:
    """Authenticate the bearer, mapping only auth failures to 401.

    ``authenticate_bearer_token`` signals an invalid credential via
    ``HTTPException`` (``raise_unauthorized``). Anything else is a server
    fault (DB outage, OAuth misconfiguration, programming error) and must
    not surface as 401 — the 401 challenge makes MCP clients discard the
    token and restart the OAuth flow. Log the fault and re-raise so it
    surfaces as a 500 instead.
    """
    try:
        return authenticate_bearer_token(request, token)
    except HTTPException:
        return mcp_unauthorized(request)
    except Exception:
        logger.exception("MCP bearer authentication failed with a server fault")
        raise


@router.post("/mcp", include_in_schema=False)
async def mcp_http(request: Request):
    # Sprint_5 Phase_2 Task 2.2: mode-aware dispatch. Keep this as an
    # internal comment, not a route docstring, so the public OpenAPI schema
    # does not drift for an MCP endpoint whose contract is JSON-RPC based.
    mode = getattr(request.app.state.settings, "mcp_http_mode", "json_stateless")
    authorization = request.headers.get("authorization", "")
    if not authorization_header_is_valid(
        authorization
    ) or not authorization.lower().startswith("bearer "):
        return mcp_unauthorized(request)
    token = authorization[7:].strip(" \t")
    actor = _authenticate_mcp_actor(request, token)
    if isinstance(actor, JSONResponse):
        return actor
    bearer_fingerprint = _bearer_fingerprint(token)
    body = await _read_bounded_mcp_body(request)
    if isinstance(body, JSONResponse):
        return body

    # Phase_2 Task 2.5: Accept negotiation (ADR A1) gates every POST.
    # ``conflict`` → 415 as a JSON-RPC error envelope with the expected
    # mode in error.data. The negotiated scope carries the json_stateless
    # compat-hack rewrite when applicable (§2.6 H5).
    branch, negotiated_scope = _negotiate_transport(request.scope, mode)
    if branch == "not_acceptable":
        return _not_acceptable_response(body)
    if branch == "conflict":
        return _transport_mode_mismatch_response(mode, body)
    parse_error = _mcp_parse_error_response(request, body)
    if parse_error is not None:
        return parse_error

    if mode == "json_stateless" or (mode == "auto" and branch == "json"):
        # Sprint_4 baseline preserved (H6 invariant 3): request-scope manager
        # with stateless=True, json_response=True per existing contract.
        #
        # Sprint_5 Phase_2 fix-round-2 (HIGH-1, Codex review 260517-1131): the
        # ``auto + Accept: application/json`` branch (Phase_2 §2.4 rows 17,
        # 19) must actually return JSON. The previous implementation routed
        # auto/JSON through the lifespan-scoped ``json_response=False``
        # manager which always answered over SSE — making the plan's row
        # 17/19 contract pytest-asserttable in name only. We now dispatch
        # auto/JSON through the same request-scope manager that
        # ``json_stateless`` uses, which produces a true ``application/
        # json`` response. ``auto + SSE`` and the dual-q row 21 still flow
        # through the lifespan-scoped SSE manager below (SDK delegation).
        # Only actual workspace-scoped tools need the transport-level
        # defense-in-depth check. JSON-RPC lifecycle methods and tools/list are
        # actor-scoped, and cross-workspace tools such as list_my_workspaces
        # deliberately do not resolve a single workspace.
        if _mcp_transport_workspace_check_required(body):
            policy_error = _resolve_and_check_mcp_workspace(request, actor, body)
            if policy_error is not None:
                return policy_error
        restricted_error = _restricted_library_policy_error(request, actor, body)
        if restricted_error is not None:
            return restricted_error
        server = create_mcp_server(
            settings=request.app.state.settings,
            actor=actor,
            page_service=get_page_service_from_app(request),
            container=getattr(request.app.state, "container", None),
            session_factory=get_session_factory_from_app(request),
            mcp_session_id=_request_mcp_trace_session_id(request, body),
        )
        manager = _new_session_manager(
            app=server,
            stateless=True,
            json_response=True,
            security_settings=mcp_transport_security(request.app.state.settings),
        )
        async with manager.run():
            return await _call_streamable_http_manager(
                manager, body, scope=negotiated_scope
            )

    # streamable / auto-SSE: delegate to lifespan-scoped manager via dispatcher.
    # Phase_2 §3.10.1 row 4 + §3.10.2 step (5): the workspace resolve runs
    # *inside* the dispatcher's @call_tool() at the moment the JSON-RPC
    # method is known, not here. tools/list + initialize MUST NOT trigger
    # workspace resolution (ARCH-1 fix, review 260517-0400).
    manager = getattr(request.app.state, "mcp_session_manager", None)
    if manager is None:
        # Lifespan did not run (e.g. test setup that bypasses lifespan, or a
        # misconfigured deployment). Phase_6 health/ready will block this in
        # production; emit a deterministic 503 here as a developer hint
        # (Phase_2 §10 "Lifespan 미작동 시나리오").
        return JSONResponse(
            _mcp_json_rpc_error_payload(
                body,
                "manager_unavailable",
                "server not ready",
                rpc_code=-32000,
            ),
            status_code=503,
        )

    # Phase_2 Task 2.6: Mcp-Session-Id validation runs after bearer auth
    # + Accept negotiation, before SDK dispatch (§3.10.5 ordering). The
    # initialize-exemption + 400/404/410 branches own this step. The SDK
    # would otherwise produce inconsistent shapes (404 JSON-RPC error
    # vs 400 plain-text) — we surface JSON-RPC error envelopes uniformly.
    session_error = _validate_mcp_session_id(
        request, manager, actor, bearer_fingerprint, body
    )
    if session_error is not None:
        return session_error
    restricted_error = _restricted_library_policy_error(request, actor, body)
    if restricted_error is not None:
        return restricted_error

    # Streamable/auto SDK uses ``json_response=False`` → every POST
    # response is served over the SSE branch (EventSourceResponse).
    # The buffered ``_call_streamable_http_manager`` would block forever
    # waiting for the SSE stream to close, so we return a streaming
    # passthrough response that hands (scope, receive, send) directly to
    # the SDK. Same pattern as Task 2.3 GET, now extended to POST with a
    # body + negotiated scope (Task 2.6 hang fix).
    return _StreamingMCPResponse(
        request,
        manager,
        actor,
        bearer_fingerprint,
        body=body,
        scope_override=negotiated_scope,
    )


@router.get("/mcp", include_in_schema=False)
async def mcp_http_get(request: Request):
    # Phase_2 Task 2.3: GET /mcp is the SSE handshake for the server to
    # client notification stream. Keep this as internal documentation so
    # OpenAPI does not carry the MCP transport design text.
    mode = getattr(request.app.state.settings, "mcp_http_mode", "json_stateless")
    authorization = request.headers.get("authorization", "")
    if not authorization_header_is_valid(
        authorization
    ) or not authorization.lower().startswith("bearer "):
        return mcp_unauthorized(request)
    token = authorization[7:].strip(" \t")
    actor = _authenticate_mcp_actor(request, token)
    if isinstance(actor, JSONResponse):
        return actor
    if mode == "json_stateless":
        return _mcp_json_rpc_error_response(
            None,
            "method_not_allowed",
            "GET requires streamable or auto mode",
            status_code=405,
            headers={"Allow": "POST, DELETE"},
        )
    bearer_fingerprint = _bearer_fingerprint(token)

    manager = getattr(request.app.state, "mcp_session_manager", None)
    if manager is None:
        return _mcp_manager_unavailable_response(None)
    if not _request_mcp_session_id(request):
        # The stream belongs to an initialized session. Handed to the SDK, a
        # GET without one would register a fresh session per request (only
        # to answer 400), letting a bearer grow the pool without bound.
        return _mcp_json_rpc_error_response(
            None,
            "session_required",
            "Mcp-Session-Id header required to open the server-to-client stream",
            status_code=400,
        )
    session_error = _validate_mcp_session_owner_for_request(
        request, manager, actor, bearer_fingerprint
    )
    if session_error is not None:
        return session_error
    # Phase_2 §3.10.2 GET branch: contextvar set → SDK handshake.
    # resolve_and_check stays out of this path (lazy on first tool call).
    # The GET response is SSE (streaming, never completes until client
    # disconnect), so we cannot buffer the response — return an ASGI
    # delegate that the framework will invoke with the same scope.
    return _StreamingMCPResponse(request, manager, actor, bearer_fingerprint)


@router.delete("/mcp", include_in_schema=False)
async def mcp_http_delete(request: Request):
    # Phase_2 Task 2.4: DELETE /mcp terminates streamable sessions and
    # remains idempotent for missing or unknown session ids.
    mode = getattr(request.app.state.settings, "mcp_http_mode", "json_stateless")
    authorization = request.headers.get("authorization", "")
    if not authorization_header_is_valid(
        authorization
    ) or not authorization.lower().startswith("bearer "):
        return mcp_unauthorized(request)
    token = authorization[7:].strip(" \t")
    actor = _authenticate_mcp_actor(request, token)
    if isinstance(actor, JSONResponse):
        return actor
    if mode == "json_stateless":
        # No session state exists, so an authenticated DELETE is an idempotent
        # no-op. Authentication still applies uniformly to every /mcp method.
        return JSONResponse({"status": "ok"}, status_code=200)
    bearer_fingerprint = _bearer_fingerprint(token)

    manager = getattr(request.app.state, "mcp_session_manager", None)
    if manager is None:
        return _mcp_manager_unavailable_response(None)

    # Fast path: if the request has no Mcp-Session-Id header, there is
    # nothing for the SDK to terminate. RFC 9110 §9.3.5 idempotency: a
    # DELETE against a non-existent resource is a 200 no-op. Avoid the
    # SDK call entirely (the SDK would otherwise create a fresh
    # transport and return 400 for the missing header).
    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        return JSONResponse({"status": "ok"}, status_code=200)

    # Unknown session id fast path: the SDK returns 404 "Session not
    # found", but the Phase_2 plan + MCP spec define DELETE as
    # idempotent, so normalize unknown ids to 200 before invoking the
    # SDK. This also avoids spinning up an SDK error response that we
    # would have to translate downstream.
    has_session = _mcp_manager_has_session(manager, session_id)
    if has_session is None:
        return _mcp_manager_unavailable_response(None)
    if not has_session:
        return JSONResponse({"status": "ok"}, status_code=200)

    session_error = _validate_mcp_session_owner(
        request.app.state, session_id, actor, bearer_fingerprint
    )
    if session_error is not None:
        return session_error

    # Known session id: delegate to the SDK so the transport's
    # ``terminate()`` runs (sets ``_terminated=True`` + closes the
    # streams + removes the entry from its session pool). The SDK
    # responds 200 on success — we forward that response verbatim.
    response = await _dispatch_via_lifespan_manager(request, manager, actor, b"")
    # Phase_2 Task 2.6 branch (4) cooperation: record the session id in
    # the terminated-ledger so a subsequent POST surfaces 410 (expired)
    # instead of 404 (unknown). The SDK removes the id immediately on
    # terminate, so without this
    # ledger we could not distinguish the two cases.
    if response.status_code < 300:
        _record_terminated_session_id(request.app.state, session_id)
        _forget_mcp_session_owner(request.app.state, session_id)
        # The supported SDK releases keep a terminated session in their pool
        # until shutdown; initialize/DELETE cycles would grow it unbounded.
        await discard_sessions(manager, [session_id])
    # Defensive idempotency: if the SDK returned a non-2xx for any
    # transient race (e.g. session terminated between the membership
    # check and the SDK call), still surface 200 to the client because
    # the resource is now effectively gone (RFC 9110 §9.3.5).
    if response.status_code >= 300:  # pragma: no cover - race-only branch
        return JSONResponse({"status": "ok"}, status_code=200)
    return response


class _StreamingMCPResponse(Response):
    """ASGI passthrough for SDK-driven streaming responses.

    Task 2.3 introduced this for GET /mcp SSE handshakes — the buffered
    :func:`_call_streamable_http_manager` cannot serve SSE because the
    response body never completes (it accumulates ``http.response.body``
    chunks until the SDK closes the stream, which happens only after
    the client disconnects).

    Task 2.6 extends the same pattern to streamable/auto **POST**
    because the SDK with ``json_response=False`` answers every POST
    (including ``initialize`` and ``tools/list``) over the SSE branch,
    which also blocks the buffered handler. Both methods now flow
    through the streaming response, with body forwarded for POST.

    Phase_2 §10 leak-prevention rule: contextvars bound for the
    duration of the stream and reset in ``finally``.
    """

    def __init__(
        self,
        request: Request,
        manager: StreamableHTTPSessionManager,
        actor: ActorContext,
        bearer_fingerprint: str,
        *,
        body: bytes = b"",
        scope_override: dict | None = None,
    ) -> None:
        # FastAPI's route layer inspects ``self.background`` before invoking
        # __call__, so we keep the Response-compatible attribute surface
        # without buffering a body.
        self.background = None
        self.status_code = 200
        self._request = request
        self._manager = manager
        self._actor = actor
        self._bearer_fingerprint = bearer_fingerprint
        # POST handler buffers the request body up front (for body peek
        # + Task 2.6 method extraction). We re-emit it to the SDK via the
        # ASGI receive() callback.
        self._body = body
        # Negotiated scope from _negotiate_transport (Task 2.5). When
        # provided, replaces the framework-supplied scope so the SDK
        # sees the post-negotiation Accept header.
        self._scope_override = scope_override

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        actor_var: ContextVar[ActorContext | None] = (
            self._request.app.state.mcp_actor_var
        )
        page_service_var: ContextVar[PageService | None] = (
            self._request.app.state.mcp_page_service_var
        )
        settings_var: ContextVar = self._request.app.state.mcp_settings_var
        session_factory_var: ContextVar = (
            self._request.app.state.mcp_session_factory_var
        )
        session_id_var: ContextVar = self._request.app.state.mcp_session_id_var

        actor_token = actor_var.set(self._actor)
        page_service_token = page_service_var.set(
            get_page_service_from_app(self._request)
        )
        settings_token = settings_var.set(self._request.app.state.settings)
        session_factory_token = session_factory_var.set(
            get_session_factory_from_app(self._request)
        )

        # POST path: the framework already consumed the body via
        # ``request.body()`` (so the caller could peek the JSON-RPC
        # method for Task 2.6 validation). The original ``receive``
        # would return an empty body chunk, which the SDK would treat
        # as an empty payload — so we substitute a receive wrapper
        # that re-emits the buffered body once, then forwards further
        # messages (including http.disconnect) to the SDK.
        emitted_body = False
        captured_body = self._body
        method = _mcp_request_method(captured_body)
        session_id_token = session_id_var.set(
            _request_mcp_trace_session_id(self._request, captured_body)
        )

        async def _receive() -> Message:
            nonlocal emitted_body
            if not emitted_body:
                emitted_body = True
                return {
                    "type": "http.request",
                    "body": captured_body,
                    "more_body": False,
                }
            return await receive()

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": _with_utf8_sse_content_type(message.get("headers", [])),
                }
            if (
                message["type"] == "http.response.start"
                and method == "initialize"
                and message.get("status", 500) < 300
            ):
                session_id = _raw_header_value(
                    message.get("headers", []), b"mcp-session-id"
                )
                if session_id:
                    _record_mcp_session_owner(
                        self._request.app.state,
                        session_id,
                        self._actor,
                        self._bearer_fingerprint,
                        manager=self._manager,
                    )
            await send(message)

        forwarded_scope = (
            self._scope_override if self._scope_override is not None else scope
        )
        try:
            with sessions_opened_during_request() as opened_session_ids:
                try:
                    await self._manager.handle_request(forwarded_scope, _receive, _send)
                finally:
                    await _discard_unowned_mcp_sessions(
                        self._request.app.state, self._manager, opened_session_ids
                    )
        finally:
            session_id_var.reset(session_id_token)
            session_factory_var.reset(session_factory_token)
            settings_var.reset(settings_token)
            page_service_var.reset(page_service_token)
            actor_var.reset(actor_token)


async def _dispatch_via_lifespan_manager(
    request: Request,
    manager: StreamableHTTPSessionManager,
    actor: ActorContext,
    body: bytes,
    *,
    scope: dict | None = None,
) -> Response:
    """Bind actor/page_service to contextvars and delegate to SDK manager.

    The lifespan-scoped manager is bound to a single dispatcher server (SDK
    constraint 3). The dispatcher reads the actor + page_service from the
    contextvars set here, so each request reaches a per-request actor-scoped
    tool surface without rebinding the manager.

    Phase_2 §3.10.2 canonical sequence: contextvar set → SDK dispatch.
    contextvars MUST be reset in ``finally`` to avoid inter-request leak
    (Phase_2 §10 "Dispatcher pattern actor context leak").

    ``scope`` overrides ``request.scope`` when provided — used by the POST
    handler to pass the Accept-negotiated scope from Task 2.5
    ``_negotiate_transport`` so the SDK sees the post-negotiation Accept
    header (e.g. with the json_stateless compat-hack applied). Non-POST
    callers (GET/DELETE session management, which never run Accept
    negotiation) omit it, so ``request.scope`` is normalized here the same
    way ``_negotiate_transport``'s json_stateless branch does -- the SDK's
    strict Accept validation needs a concrete ``application/json``/``*/*``
    header, not whatever the caller happened to send.
    """
    actor_var: ContextVar[ActorContext | None] = request.app.state.mcp_actor_var
    page_service_var: ContextVar[PageService | None] = (
        request.app.state.mcp_page_service_var
    )
    settings_var: ContextVar = request.app.state.mcp_settings_var
    session_factory_var: ContextVar = request.app.state.mcp_session_factory_var
    session_id_var: ContextVar = request.app.state.mcp_session_id_var

    actor_token = actor_var.set(actor)
    page_service_token = page_service_var.set(get_page_service_from_app(request))
    settings_token = settings_var.set(request.app.state.settings)
    session_factory_token = session_factory_var.set(
        get_session_factory_from_app(request)
    )
    session_id_token = session_id_var.set(_request_mcp_trace_session_id(request, body))
    resolved_scope = scope if scope is not None else _apply_compat_hack(request.scope)
    try:
        return await _call_streamable_http_manager(manager, body, scope=resolved_scope)
    finally:
        session_id_var.reset(session_id_token)
        session_factory_var.reset(session_factory_token)
        settings_var.reset(settings_token)
        page_service_var.reset(page_service_token)
        actor_var.reset(actor_token)


def install_mcp_session_manager(app: FastAPI) -> StreamableHTTPSessionManager | None:
    """Build the lifespan-scoped dispatcher manager when mode != json_stateless.

    Called from the FastAPI lifespan in ``backend/app/main.py``. Stores the
    contextvars used by the dispatcher on ``app.state`` so the POST handler
    and dispatcher share the same bindings.

    Returns the manager (or ``None`` for ``json_stateless``). The caller is
    responsible for entering the manager's ``run()`` context.
    """
    # Always create the contextvars so dispatcher / handler can read them
    # uniformly regardless of mode. Module-level vars would leak across
    # ``create_app`` instances (H6 invariant 1: instance-scoped, not
    # module-level singleton).
    app.state.mcp_actor_var = ContextVar("mcp_actor", default=None)
    app.state.mcp_page_service_var = ContextVar("mcp_page_service", default=None)
    app.state.mcp_settings_var = ContextVar("mcp_settings", default=None)
    app.state.mcp_session_factory_var = ContextVar("mcp_session_factory", default=None)
    app.state.mcp_session_id_var = ContextVar("mcp_session_id", default=None)
    # Phase_2 Task 2.6 branch (4): SDK removes terminated session ids
    # after DELETE, so we cannot distinguish
    # "terminated" from "never existed" by querying the manager alone.
    # Track terminated ids here to surface 410 (expired) vs 404 (unknown)
    # per Phase_2 §Task 2.6 contract. Bounded FIFO ledger — capped at
    # ``_TERMINATED_SESSION_CAP`` to prevent unbounded growth under a
    # long-running deployment.
    app.state.mcp_terminated_session_ids = OrderedDict()
    # Streamable sessions are bearer-bound after initialize. A valid
    # Mcp-Session-Id alone is not an authority to reuse or terminate the
    # server-side transport.
    app.state.mcp_session_owners = {}

    mode = getattr(app.state.settings, "mcp_http_mode", "json_stateless")
    if mode == "json_stateless":
        app.state.mcp_session_manager = None
        return None

    dispatcher = _build_dispatcher_server(app)
    manager = _new_session_manager(
        app=dispatcher,
        stateless=False,
        json_response=False,
        security_settings=mcp_transport_security(app.state.settings),
        session_idle_timeout=MCP_SESSION_IDLE_TIMEOUT_SECONDS,
    )
    record_session_openings(manager)
    app.state.mcp_session_manager = manager
    return manager


def _build_dispatcher_server(app: FastAPI) -> MCPServer:
    """Single MCPServer bound to the lifespan-scoped manager.

    SDK constraint 3 (Phase_2 §6): ``manager.app`` is a single MCPServer
    instance, but dtxWiki's tool surface is actor-scoped. The dispatcher
    server reads the actor + page_service from contextvars (set by the
    POST handler) at the moment each ``tools/list`` / ``tools/call`` arrives
    and delegates to the same runner used by ``create_mcp_server``.

    Phase_2 §3.10.2 step (5): for ``streamable`` / ``auto`` modes the
    workspace ``resolve_and_check`` happens *inside* the dispatcher's
    ``@call_tool()`` callback once the tool name is known. ``tools/list``
    + ``initialize`` skip the workspace check (§3.10.3 — tool registry is
    actor-scope, not workspace-scope). Tools with
    ``TOOL_REQUIRED_ROLES[name] is None`` (e.g. ``list_my_workspaces``)
    also skip the workspace check because they enumerate workspaces
    cross-scope.
    """
    settings = app.state.settings
    dispatcher = MCPServer(
        # Same ``serverInfo.version`` as the request-scoped json_stateless
        # server: a client must not read a different tool contract version
        # depending on which HTTP mode serves the request.
        name="dtxwiki",
        version=mcp_server_version(settings.app_version),
        instructions=MCP_SERVER_INSTRUCTIONS,
    )

    @dispatcher.list_tools()
    async def _list_tools(request: mcp_types.ListToolsRequest):
        # Phase_2 §3.10.3: tools/list skips workspace resolution; the tool
        # surface is actor-scope (scopes/role) only.
        actor = app.state.mcp_actor_var.get()
        if actor is None:  # pragma: no cover - guarded by handler ordering
            return []
        session_factory = app.state.mcp_session_factory_var.get()
        if session_factory is None:  # pragma: no cover - guarded by handler
            return []
        container = getattr(app.state, "container", None)
        visible_actor = actor
        if request is not None:
            visible_actor = enforce_actor_mcp_surface_lifecycle(
                actor=actor,
                fallback_session_factory=session_factory,
                db=container.db if container is not None else None,
            )
        return list_dtxwiki_tools(visible_tool_names(visible_actor))

    @dispatcher.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict | None):
        actor = app.state.mcp_actor_var.get()
        page_service = app.state.mcp_page_service_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or page_service is None:  # pragma: no cover
            raise RuntimeError("dispatcher invoked without actor binding")
        container = getattr(app.state, "container", None)
        search_service = (
            container.get_search_service() if container is not None else None
        )
        tool_context = (
            ToolCallContext(
                actor=actor,
                db=container.db,
                page_service=page_service,
                search_service=search_service,
                services=container.get_services(),
                conversation_token=app.state.mcp_session_id_var.get(),
            )
            if container is not None
            else None
        )

        tool_arguments = arguments or {}
        # Phase_2 §3.10.2 step (5): lazy resolve_and_check inside dispatcher.
        # ``TOOL_REQUIRED_ROLES[name] is None`` (e.g. list_my_workspaces) is
        # a cross-workspace enumeration tool — skip the workspace check.
        action = _map_action(name)
        if action is not None:
            requested_workspace_id = _mcp_requested_workspace_id_from_arguments(
                tool_arguments
            )
            with session_factory() as session:
                resolve_and_check(
                    session=session,
                    actor=actor,
                    action=action,
                    requested_workspace_id=requested_workspace_id,
                )
        return await call_tool_async(
            name,
            tool_arguments,
            actor=actor,
            page_service=page_service,
            session_factory=session_factory,
            mcp_session_id=app.state.mcp_session_id_var.get(),
            search_service=search_service,
            tool_context=tool_context,
        )

    @dispatcher.list_prompts()
    async def _list_prompts():
        actor = app.state.mcp_actor_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or session_factory is None:  # pragma: no cover
            return []
        return list_dtxwiki_prompts(
            actor=actor,
            session_factory=session_factory,
            tool_context=_surface_tool_context(app, actor, session_factory),
        )

    @dispatcher.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None):
        actor = app.state.mcp_actor_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or session_factory is None:  # pragma: no cover
            raise RuntimeError("dispatcher invoked without actor binding")
        return get_dtxwiki_prompt(
            name=name,
            arguments=arguments,
            actor=actor,
            session_factory=session_factory,
            tool_context=_surface_tool_context(app, actor, session_factory),
        )

    @dispatcher.list_resources()
    async def _list_resources():
        actor = app.state.mcp_actor_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or session_factory is None:  # pragma: no cover
            return []
        return list_dtxwiki_resources(
            actor=actor,
            session_factory=session_factory,
            tool_context=_surface_tool_context(app, actor, session_factory),
        )

    @dispatcher.list_resource_templates()
    async def _list_resource_templates():
        actor = app.state.mcp_actor_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or session_factory is None:  # pragma: no cover
            return []
        return list_dtxwiki_resource_templates(
            actor=actor,
            session_factory=session_factory,
            tool_context=_surface_tool_context(app, actor, session_factory),
        )

    @dispatcher.read_resource()
    async def _read_resource(uri):
        actor = app.state.mcp_actor_var.get()
        session_factory = app.state.mcp_session_factory_var.get()
        if actor is None or session_factory is None:  # pragma: no cover
            raise RuntimeError("dispatcher invoked without actor binding")
        return read_dtxwiki_resource(
            uri=uri,
            actor=actor,
            session_factory=session_factory,
            tool_context=_surface_tool_context(app, actor, session_factory),
        )

    return dispatcher


def _surface_tool_context(
    app: FastAPI,
    actor: ActorContext,
    session_factory,
) -> ToolCallContext | None:
    """Bind the prompt/resource surface to the app's composition.

    The injected container's DatabaseProvider and ServiceRegistry are
    preserved — wrapping them in a fresh core provider would silently discard
    a cloud overlay's DB routing (Sprint-2 review TCC-1). A provider is
    synthesized only when no container exists (bare test apps).
    """

    container = getattr(app.state, "container", None)
    page_service = app.state.mcp_page_service_var.get()
    if page_service is None:
        return None
    if container is not None:
        db = container.db
        services = container.get_services()
        search_service = container.get_search_service()
    else:
        db = DatabaseProvider(None, session_factory=session_factory)
        services = None
        search_service = None
    return ToolCallContext(
        actor=actor,
        db=db,
        page_service=page_service,
        search_service=search_service,
        services=services,
        conversation_token=app.state.mcp_session_id_var.get(),
    )


def _map_action(tool_name: str) -> Action | None:
    """Phase_2 §3.10.3 — map a tool name to the access action.

    ``TOOL_REQUIRED_ROLES`` (``backend/mcp/scopes.py``) is the single
    source of truth. ``None`` means the tool runs cross-workspace and
    must not trigger ``resolve_and_check`` (e.g. ``list_my_workspaces``).
    Unknown tool names default to ``"read"`` (conservative: a future tool
    not yet in ``TOOL_REQUIRED_ROLES`` still receives an access check
    rather than silently bypassing one).
    """
    required_role = TOOL_REQUIRED_ROLES.get(tool_name, "viewer")
    if required_role is None:
        return None
    return required_role_to_action(required_role)


def _mcp_transport_workspace_check_required(body_bytes: bytes) -> bool:
    tool_name = _mcp_request_tool_name(body_bytes)
    if tool_name is None:
        return False
    return _map_action(tool_name) is not None


def _resolve_and_check_mcp_workspace(
    request: Request, actor, body: bytes
) -> JSONResponse | None:
    """Resolve + access-check the workspace for an MCP POST.

    Sprint_5 Phase_0~2 fix-round (SEC-1, SEC-6): the previous implementation
    silently returned ``None`` when a client supplied ``params.arguments.
    workspace_id`` and the resolver/checker raised, on the assumption that
    per-tool ``enforce_workspace_role_for_tool`` would catch the deny later.
    That assumption is fragile (defense-in-depth would degrade to a single
    layer; a future tool with ``TOOL_REQUIRED_ROLES[name] is None`` would
    bypass workspace policy entirely). Phase_1 §11.1 contract requires
    ``WorkspaceAmbiguityError -> 400`` and ``PermissionError -> 403``
    unconditionally.
    """

    requested_workspace_id = _mcp_requested_workspace_id(body)
    session_factory = get_session_factory_from_app(request)
    with session_factory.begin() as session:
        try:
            workspace = resolve_workspace_for_actor(
                session=session,
                actor=actor,
                requested_workspace_id=requested_workspace_id,
            )
            check_actor_can_access(
                actor, workspace.workspace_id, "read", session=session
            )
        except WorkspaceAmbiguityError as exc:
            return mcp_policy_error(
                "workspace_ambiguous",
                str(exc),
                status_code=400,
                body=body,
            )
        except PermissionError as exc:
            error_code = str(exc).split(":", 1)[0] or "workspace_access_denied"
            return mcp_policy_error(
                error_code,
                str(exc),
                status_code=403,
                body=body,
            )
    return None


def _mcp_requested_workspace_id(body_bytes: bytes) -> UUID | None:
    try:
        body = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    arguments = params.get("arguments")
    return _mcp_requested_workspace_id_from_arguments(arguments)


def _mcp_request_tool_name(body_bytes: bytes) -> str | None:
    try:
        body = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return None
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


def _mcp_requested_workspace_id_from_arguments(arguments: dict | None) -> UUID | None:
    if not isinstance(arguments, dict):
        return None
    raw_workspace_id = arguments.get("workspace_id")
    if raw_workspace_id in (None, ""):
        return None
    try:
        return UUID(str(raw_workspace_id))
    except ValueError:
        return None


def _restricted_library_policy_error(
    request: Request,
    actor: ActorContext,
    body: bytes,
) -> JSONResponse | None:
    target = _mcp_read_library_target(body)
    if target is None:
        return None
    requested_workspace_id, target_type, target_id = target
    if requested_workspace_id is None:
        # ReadInput.workspace_id is a required field; a call missing it is
        # malformed and will fail pydantic validation downstream. Nothing to
        # pre-check here without a workspace to scope the lookup to.
        return None
    container = get_container_from_app(request)
    control_session_factory = get_session_factory_from_app(request)
    # Epic-3 D-4/D-5: route the item lookup by the workspace_id the request
    # already carries, on that workspace's own shard -- do not fall back to a
    # global reverse lookup (control-plane session.get by bare id) to
    # *discover* which workspace owns the target. A target that belongs to a
    # different workspace than the one requested simply won't resolve here,
    # the same way it won't resolve for the real handler later; it must never
    # surface via a cross-shard reverse lookup.
    workspace_session_factory = container.db.session_for(requested_workspace_id)
    with control_session_factory() as session:
        try:
            scoped_actor = actor_with_target_workspace_scope(
                actor,
                requested_workspace_id,
                session=session,
            )
            access = require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=requested_workspace_id,
                required_role="viewer",
                workspace_session_factory=workspace_session_factory,
            )
        except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
            return mcp_policy_error(
                exc.error_code,
                str(exc),
                status_code=503,
                body=body,
            )
        except (PermissionError, RejectedByPolicyError) as exc:
            error_code = getattr(exc, "error_code", None)
            if not isinstance(error_code, str) or not error_code:
                error_code = str(exc).split(":", 1)[0] or "workspace_access_denied"
            return mcp_policy_error(
                error_code,
                str(exc),
                status_code=403,
                body=body,
            )
        scoped_actor = rebind_actor_to_workspace_access(scoped_actor, access)
    if not has_required_scope(scoped_actor, TOOL_REQUIRED_SCOPES["read"]):
        return mcp_policy_error(
            "scope_denied",
            "insufficient token scope for read",
            status_code=403,
            body=body,
        )
    with workspace_session_factory() as session:
        resolved = resolve_restricted_library_target(
            session=session,
            workspace_id=requested_workspace_id,
            target_type=target_type,
            target_id=target_id,
        )
    if resolved is None:
        return None
    target_workspace_id, restricted = resolved
    if not restricted:
        return None
    # ``resolved`` was constrained by requested_workspace_id above, but retain
    # the returned id as a defensive consistency check before applying the
    # restricted-content policy.
    if target_workspace_id != requested_workspace_id:
        return None
    try:
        check_restricted_library_allowed(scoped_actor, restricted=restricted)
    except PermissionError as exc:
        error_code = getattr(exc, "error_code", None) or (
            str(exc).split(":", 1)[0] or "restricted_library_denied"
        )
        return mcp_policy_error(
            error_code,
            str(exc),
            status_code=403,
            body=body,
        )
    return None


def _mcp_read_library_target(
    body_bytes: bytes,
) -> tuple[UUID | None, str, UUID] | None:
    try:
        body = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return None
    params = body.get("params")
    if not isinstance(params, dict) or params.get("name") != "read":
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None
    target_type = arguments.get("target_type")
    if target_type not in {"library_item_version", "library_artifact"}:
        return None
    raw_target_id = arguments.get("target_id")
    if raw_target_id in (None, ""):
        return None
    try:
        target_id = UUID(str(raw_target_id))
    except ValueError:
        return None
    workspace_id = _mcp_requested_workspace_id_from_arguments(arguments)
    return workspace_id, str(target_type), target_id


def mcp_policy_error(
    error: str,
    message: str,
    *,
    status_code: int,
    body: bytes | None = None,
) -> JSONResponse:
    return JSONResponse(
        _mcp_json_rpc_error_payload(
            body,
            error,
            message or error,
            rpc_code=-32000,
        ),
        status_code=status_code,
    )


def _mcp_json_rpc_error_payload(
    body: bytes | None,
    error: str,
    message: str,
    *,
    rpc_code: int,
    data: dict | None = None,
) -> dict:
    error_data = {"error": error}
    if data:
        error_data.update(data)
    return {
        "jsonrpc": "2.0",
        "id": _mcp_request_id(body),
        "error": {
            "code": rpc_code,
            "message": message,
            "data": error_data,
        },
    }


def _mcp_json_rpc_error_response(
    body: bytes | None,
    error: str,
    message: str,
    *,
    status_code: int,
    rpc_code: int = -32000,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        _mcp_json_rpc_error_payload(
            body,
            error,
            message,
            rpc_code=rpc_code,
            data=data,
        ),
        status_code=status_code,
        headers=headers,
    )


async def _read_bounded_mcp_body(request: Request) -> bytes | JSONResponse:
    """Read one MCP POST without ever buffering beyond the upload envelope cap."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > MCP_MAX_REQUEST_BODY_BYTES:
            return _mcp_request_too_large_response()

    chunks: list[bytes] = []
    byte_count = 0
    async for chunk in request.stream():
        byte_count += len(chunk)
        if byte_count > MCP_MAX_REQUEST_BODY_BYTES:
            return _mcp_request_too_large_response()
        chunks.append(chunk)
    return b"".join(chunks)


def _mcp_parse_error_response(request: Request, body: bytes) -> JSONResponse | None:
    """Reject malformed or non-UTF-8 MCP JSON before SDK dispatch.

    JSON-RPC defines parse errors with a null request id because the server
    cannot trust any partially decoded ``id``.  Valid JSON values continue to
    the SDK, which remains responsible for JSON-RPC shape validation.
    """

    content_type = request.headers.get("content-type", "")
    if not _contains_json_media_type(content_type):
        # Preserve the SDK's existing unsupported-media-type handling and its
        # precedence relative to JSON-RPC parsing.
        return None
    if _declares_non_utf8_charset(content_type):
        return _mcp_json_rpc_error_response(
            None,
            "parse_error",
            "Parse error: MCP request body must be valid UTF-8 JSON",
            status_code=400,
            rpc_code=-32700,
        )
    try:
        decoded = body.decode("utf-8", errors="strict")
        json.loads(decoded, parse_constant=_reject_non_json_constant)
    except (UnicodeDecodeError, ValueError):
        return _mcp_json_rpc_error_response(
            None,
            "parse_error",
            "Parse error: MCP request body must be valid UTF-8 JSON",
            status_code=400,
            rpc_code=-32700,
        )
    return None


def _reject_non_json_constant(value: str) -> None:
    """Reject Python JSON extensions that RFC 8259 does not permit."""

    raise ValueError(f"invalid JSON constant: {value}")


def _declares_non_utf8_charset(content_type: str) -> bool:
    """Return whether Content-Type explicitly declares a non-UTF-8 charset."""

    for parameter in _split_mime_parameters(content_type)[1:]:
        name, separator, value = parameter.partition("=")
        if not separator or name.strip().lower() != "charset":
            continue
        normalized = value.strip().strip("\"'").lower().replace("_", "-")
        if normalized not in {"utf-8", "utf8"}:
            return True
    return False


def _contains_json_media_type(content_type: str) -> bool:
    """Match JSON in valid or SDK-tolerated comma-joined Content-Type values."""

    media_types = _split_mime_parameters(content_type)[0].split(",")
    return any(value.strip().lower() == "application/json" for value in media_types)


def _mcp_request_too_large_response() -> JSONResponse:
    return _mcp_json_rpc_error_response(
        None,
        "request_too_large",
        "MCP request body exceeds the encoded image upload limit.",
        status_code=413,
        rpc_code=-32001,
        data={"limit_bytes": MCP_MAX_REQUEST_BODY_BYTES},
    )


def _mcp_manager_unavailable_response(body: bytes | None) -> JSONResponse:
    return _mcp_json_rpc_error_response(
        body,
        "manager_unavailable",
        "server not ready",
        status_code=503,
    )


def _mcp_manager_has_session(
    manager: StreamableHTTPSessionManager,
    session_id: str,
) -> bool | None:
    return manager_has_session(manager, session_id)


def _mcp_request_id(body: bytes | None):
    if not body:
        return None
    try:
        payload = json.loads(body.decode() or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("id")
    if (
        isinstance(value, str)
        or (isinstance(value, int) and not isinstance(value, bool))
        or value is None
    ):
        return value
    return None


def _request_mcp_session_id(request: Request) -> str | None:
    return request.headers.get("mcp-session-id") or None


def _request_mcp_trace_session_id(request: Request, body: bytes | None) -> str:
    session_id = request.headers.get("mcp-session-id") or request.headers.get(
        "Mcp-Session-Id"
    )
    if session_id:
        return session_id
    request_id = _mcp_request_id(body)
    if request_id is not None:
        return f"single-call:{request_id}"
    return f"single-call:{uuid4()}"


# ---------------------------------------------------------------------------
# Sprint_5 Phase_2 Task 2.6 — Mcp-Session-Id validation
# ---------------------------------------------------------------------------

# Cap the terminated-session-id ledger to prevent unbounded growth in a
# long-running deployment. Old ids beyond the cap fall back to the
# ``unknown`` (404) branch — acceptable degradation since clients should
# discard terminated ids anyway.
_TERMINATED_SESSION_CAP = 1024
_SESSION_OWNER_CAP = 1024


def _bearer_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _actor_fingerprint(actor: ActorContext, bearer_fingerprint: str) -> tuple:
    # D-01v2: token_workspace_ids is a live allowed SET that can change
    # between requests in the same session without re-auth. Including it in
    # the fingerprint would make ownership flap on a legitimate set change
    # and falsely reject the session's own continuation -- key ownership on
    # token_id (the stable PAT id / OAuth grant UUID) instead.
    #
    # OAuth access JWTs rotate while the authorization grant remains the same.
    # New tokens carry that stable grant UUID in ``token_id``; bind streamable
    # sessions to it so a refresh can continue the existing Mcp-Session-Id.
    # Legacy JWTs have no grant UUID and remain bound to their exact bearer.
    credential_fingerprint = (
        None
        if actor.token_kind == "oauth_jwt" and actor.token_id is not None
        else bearer_fingerprint
    )
    return (
        actor.actor_kind,
        actor.actor_id,
        actor.token_kind,
        str(actor.token_id) if actor.token_id is not None else None,
        str(actor.user_id) if actor.user_id is not None else None,
        credential_fingerprint,
    )


def _raw_header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _record_mcp_session_owner(
    app_state,
    session_id: str,
    actor: ActorContext,
    bearer_fingerprint: str,
    manager: StreamableHTTPSessionManager | None = None,
) -> None:
    owners: dict[str, tuple] | None = getattr(app_state, "mcp_session_owners", None)
    if owners is None:  # pragma: no cover - install_mcp_session_manager guard
        return
    owners[session_id] = _actor_fingerprint(actor, bearer_fingerprint)
    if len(owners) <= _SESSION_OWNER_CAP:
        return
    # Over cap: drop entries whose sessions are already gone (terminated
    # ledger or no longer in the SDK pool). Live owners are never evicted —
    # losing the owner record would 403 a still-valid session forever. If
    # the cap is genuinely full of live sessions, reject the NEW
    # registration instead (the capacity gate in _validate_mcp_session_id
    # makes this race-only).
    _prune_dead_mcp_session_owners(app_state, manager)
    if len(owners) > _SESSION_OWNER_CAP:
        owners.pop(session_id, None)


def _prune_dead_mcp_session_owners(
    app_state,
    manager: StreamableHTTPSessionManager | None,
) -> None:
    owners: dict[str, tuple] | None = getattr(app_state, "mcp_session_owners", None)
    if not owners:
        return
    terminated = getattr(app_state, "mcp_terminated_session_ids", None) or ()
    for session_id in list(owners):
        if session_id in terminated:
            owners.pop(session_id, None)
            continue
        if manager is None:
            continue
        if _mcp_manager_has_session(manager, session_id) is False:
            owners.pop(session_id, None)


def _mcp_session_capacity_error(
    app_state,
    manager: StreamableHTTPSessionManager,
    body: bytes | None,
) -> JSONResponse | None:
    owners: dict[str, tuple] | None = getattr(app_state, "mcp_session_owners", None)
    if owners is None or len(owners) < _SESSION_OWNER_CAP:
        return None
    _prune_dead_mcp_session_owners(app_state, manager)
    if len(owners) < _SESSION_OWNER_CAP:
        return None
    return _mcp_json_rpc_error_response(
        body,
        "session_capacity_exhausted",
        "session capacity reached; terminate idle sessions and retry",
        status_code=503,
    )


def _forget_mcp_session_owner(app_state, session_id: str) -> None:
    owners: dict[str, tuple] | None = getattr(app_state, "mcp_session_owners", None)
    if owners is not None:
        owners.pop(session_id, None)


def _validate_mcp_session_owner(
    app_state,
    session_id: str,
    actor: ActorContext,
    bearer_fingerprint: str,
    body: bytes | None = None,
) -> JSONResponse | None:
    owners: dict[str, tuple] | None = getattr(app_state, "mcp_session_owners", None)
    if owners is None:
        return None
    owner = owners.get(session_id)
    if owner is None or owner != _actor_fingerprint(actor, bearer_fingerprint):
        return _mcp_json_rpc_error_response(
            body,
            "session_owner_mismatch",
            "Mcp-Session-Id belongs to a different bearer",
            status_code=403,
        )
    return None


def _validate_mcp_session_owner_for_request(
    request: Request,
    manager: StreamableHTTPSessionManager,
    actor: ActorContext,
    bearer_fingerprint: str,
) -> JSONResponse | None:
    session_id = request.headers.get("mcp-session-id") or request.headers.get(
        "Mcp-Session-Id"
    )
    if not session_id:
        return None

    terminated: set[str] = getattr(
        request.app.state, "mcp_terminated_session_ids", set()
    )
    if session_id in terminated:
        return _mcp_json_rpc_error_response(
            None,
            "session_expired",
            "session was terminated",
            status_code=410,
        )

    has_session = _mcp_manager_has_session(manager, session_id)
    if has_session is None:
        return _mcp_manager_unavailable_response(None)
    if not has_session:
        return _mcp_json_rpc_error_response(
            None,
            "session_unknown",
            "Mcp-Session-Id not found",
            status_code=404,
        )

    return _validate_mcp_session_owner(
        request.app.state, session_id, actor, bearer_fingerprint
    )


def _mcp_request_method(body_bytes: bytes) -> str | None:
    """Peek the JSON-RPC ``method`` field from a buffered request body.

    Returns ``None`` when the body is not valid JSON or lacks a
    ``method`` field. Phase_2 §Task 2.6 strategy: peek before SDK
    dispatch so the ``initialize`` exemption + ``session_required``
    (400) branch can run without re-reading the SDK stream.
    """
    try:
        payload = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if not isinstance(method, str):
        return None
    return method


def _record_terminated_session_id(app_state, session_id: str) -> None:
    """Mark ``session_id`` as terminated so a subsequent POST surfaces
    410 (expired) rather than 404 (unknown).

    Bounded — drops the oldest entry once the ledger exceeds
    ``_TERMINATED_SESSION_CAP`` so eviction is deterministic.
    """
    ledger: OrderedDict[str, None] | set[str] | None = getattr(
        app_state, "mcp_terminated_session_ids", None
    )
    if ledger is None:  # pragma: no cover - install_mcp_session_manager guard
        return
    if isinstance(ledger, OrderedDict):
        ledger[session_id] = None
        ledger.move_to_end(session_id)
        while len(ledger) > _TERMINATED_SESSION_CAP:
            ledger.popitem(last=False)
        return

    # Backward-compatible handling for tests or app instances created before
    # the FIFO ledger shape existed.
    ledger.add(session_id)
    while len(ledger) > _TERMINATED_SESSION_CAP:
        ledger.pop()


def _validate_mcp_session_id(
    request: Request,
    manager: StreamableHTTPSessionManager,
    actor: ActorContext,
    bearer_fingerprint: str,
    body: bytes,
) -> JSONResponse | None:
    """Phase_2 §Task 2.6 4-branch session id validation.

    Runs only for ``streamable`` / ``auto`` POSTs — ``json_stateless``
    has no session lifecycle (stateless transport per request).

    Branches:
      (1) ``method == "initialize"`` + no Mcp-Session-Id  → exempt
          (return ``None``; SDK creates a new session id and emits it
          on the response header) unless the session-owner ledger is
          at capacity with live sessions → 503 JSON-RPC error envelope
          with ``session_capacity_exhausted`` in ``error.data.error``.
      (2) non-initialize + no Mcp-Session-Id              → 400 JSON-RPC
          error envelope with ``session_required`` in ``error.data.error``.
      (3) Mcp-Session-Id in the terminated-ledger         → 410 JSON-RPC
          error envelope with ``session_expired`` in ``error.data.error``.
      (4) Mcp-Session-Id not in manager session pool      → 404 JSON-RPC
          error envelope with ``session_unknown`` in ``error.data.error``.

    Branch (3) is checked *before* (4) so a session id that the
    operator has already DELETE'd surfaces as ``410 expired``, not
    ``404 unknown`` (the plan's distinction). The
    ``terminated_session_ids`` ledger lives on ``app.state`` and is
    populated by the DELETE handler.

    All POST transport errors follow the JSON-RPC error envelope and avoid
    REST ``error_code``/``details`` contamination.
    """
    session_id = request.headers.get("mcp-session-id") or request.headers.get(
        "Mcp-Session-Id"
    )
    method = _mcp_request_method(body)

    if session_id is None or session_id == "":
        if method == "initialize":
            # Branch (1): SDK will mint a new session id — unless the
            # owner ledger is already full of live sessions, in which
            # case the new session is refused up front (503) so existing
            # owners are never evicted.
            return _mcp_session_capacity_error(request.app.state, manager, body)
        # Branch (2).
        return _mcp_json_rpc_error_response(
            body,
            "session_required",
            "Mcp-Session-Id header required for non-initialize requests",
            status_code=400,
        )

    terminated: set[str] = getattr(
        request.app.state, "mcp_terminated_session_ids", set()
    )
    if session_id in terminated:
        # Branch (3) — checked before (4) so the operator-distinguishable
        # case wins.
        return _mcp_json_rpc_error_response(
            body,
            "session_expired",
            "session was terminated",
            status_code=410,
        )

    has_session = _mcp_manager_has_session(manager, session_id)
    if has_session is None:
        return _mcp_manager_unavailable_response(body)
    if not has_session:
        # Branch (4).
        return _mcp_json_rpc_error_response(
            body,
            "session_unknown",
            "Mcp-Session-Id not found",
            status_code=404,
        )

    return _validate_mcp_session_owner(
        request.app.state, session_id, actor, bearer_fingerprint, body
    )


def mcp_unauthorized(request: Request) -> JSONResponse:
    metadata_url = (
        f"{public_base_url_from_request(request)}/.well-known/oauth-protected-resource"
    )
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": (f'Bearer resource_metadata="{metadata_url}"')},
    )


async def _call_streamable_http_manager(
    manager: StreamableHTTPSessionManager,
    body: bytes,
    *,
    scope: dict,
) -> Response:
    """Bridge FastAPI's request handler to the SDK's ASGI-style transport.

    ``scope`` is the post-negotiation scope from ``_negotiate_transport``
    (which may contain the json_stateless compat hack rewrite) — every
    caller runs Accept negotiation first and supplies it.
    """

    started: Message | None = None
    body_parts: list[bytes] = []

    async def send(message: Message) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            started = message
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    async def receive() -> Message:
        nonlocal body
        chunk = body
        body = b""
        return {"type": "http.request", "body": chunk, "more_body": False}

    await manager.handle_request(scope, receive, send)
    if started is None:  # pragma: no cover - SDK should always start a response.
        return Response(status_code=500)

    response_headers = _with_utf8_sse_content_type(started.get("headers", []))
    response = Response(
        content=b"".join(body_parts),
        status_code=started["status"],
    )
    response_header_names = {key.lower() for key, _ in response_headers}
    generated_headers = [
        (key, value)
        for key, value in response.raw_headers
        if key.lower() not in response_header_names
    ]
    response.raw_headers = [*response_headers, *generated_headers]
    return response


# ---------------------------------------------------------------------------
# Sprint_5 Phase_2 Task 2.5 — Accept header negotiation (ADR A1)
# ---------------------------------------------------------------------------

# Candidate media-ranges per family (Phase_2 §2.3 Step 4).
_JSON_CANDIDATES: tuple[str, ...] = ("application/json", "application/*", "*/*")
_SSE_CANDIDATES: tuple[str, ...] = ("text/event-stream", "text/*", "*/*")


def _get_header_value(scope: dict, name: bytes) -> str:
    """Return the latest matching header value from an ASGI scope, or ''."""
    target = name.lower()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return ""


def _parse_accept_q(accept_raw: str) -> dict[str, float]:
    """Parse Accept per RFC 7231 §5.3.1 into ``{media_range: q}``.

    Step 1 of Phase_2 §2.3:
    - ``type/subtype;q=0.8`` → ``("type/subtype", 0.8)``
    - ``type/subtype`` (q absent) → ``("type/subtype", 1.0)``
    - parameter parse failure → skip that media-range (do not fail the
      whole header).

    Subsequent duplicate media-ranges keep the *last* q value seen
    (matches what curl / browsers typically generate).
    """
    if not accept_raw:
        return {}
    result: dict[str, float] = {}
    for raw_part in accept_raw.split(","):
        part = raw_part.strip()
        if not part:
            continue
        # Split off parameters.
        if ";" in part:
            media_range_raw, *params = (segment.strip() for segment in part.split(";"))
        else:
            media_range_raw = part
            params = []
        media_range = media_range_raw.lower()
        if "/" not in media_range:
            # Not a valid media-range — skip rather than fail the whole header.
            continue
        q_value = 1.0
        skip = False
        for param in params:
            if not param:
                continue
            if "=" not in param:
                # Unknown parameter without value (e.g. bare "charset") —
                # ignore; do not skip the media-range.
                continue
            name, _, value = param.partition("=")
            if name.strip().lower() != "q":
                continue
            try:
                q_value = float(value.strip())
            except ValueError:
                skip = True
                break
            # RFC 7231 §5.3.1: q ∈ [0, 1].
            if not 0.0 <= q_value <= 1.0:
                skip = True
                break
        if skip:
            continue
        result[media_range] = q_value
    return result


def _score(q_map: dict[str, float], candidates: tuple[str, ...]) -> float:
    """Return the highest q across ``candidates`` present in ``q_map``."""
    best = 0.0
    for media_range in candidates:
        q = q_map.get(media_range)
        if q is not None and q > best:
            best = q
    return best


def _negotiate_transport(scope: dict, mode: str) -> tuple[str, dict]:
    """Phase_2 §2.3 Accept negotiation decision tree (ADR A1).

    Returns one of:
      - ``("json", new_scope)`` — JSON branch. ``new_scope`` has the
        compat-hack applied (``*/*`` / missing → ``application/json``)
        when ``mode == 'json_stateless'``; for ``auto`` JSON branch the
        scope is forwarded unchanged so the SDK's own Accept handling
        runs.
      - ``("sse", new_scope)`` — SSE / streamable branch. Scope is
        forwarded unchanged (SDK strict Accept validation is intended
        behavior here).
      - ``("not_acceptable", {})`` — caller must emit 406 because a
        non-empty Accept header has no JSON/SSE-compatible candidate.
      - ``("conflict", {})`` — caller must emit 415 with a JSON-RPC
        error envelope carrying the expected mode in ``error.data``.

    Step 2 (q=0 exclusion) is applied here before the mode branches.
    """
    accept_raw = _get_header_value(scope, b"accept")
    accept_missing = accept_raw is None or not accept_raw.strip()
    q_map = _parse_accept_q(accept_raw)
    # Step 2: drop q=0 media-ranges per RFC 7231 §5.3.1.
    q_map = {media: q for media, q in q_map.items() if q > 0}

    json_score = _score(q_map, _JSON_CANDIDATES)
    sse_score = _score(q_map, _SSE_CANDIDATES)
    star_only = set(q_map.keys()) == {"*/*"}
    missing = accept_missing
    unsupported = not missing and (
        not q_map or (not star_only and json_score == 0 and sse_score == 0)
    )

    if unsupported:
        return ("not_acceptable", {})

    if mode == "json_stateless":
        # (a) star_only or missing → JSON (Sprint_4 baseline).
        # (b) json_score > 0 AND (sse_score == 0 OR json_score >= sse_score).
        # (c) sse_score > 0 AND sse_score > json_score → conflict.
        # (d) dual-q → JSON (env forced).
        if missing or star_only or json_score >= sse_score:
            return ("json", _apply_compat_hack(scope))
        return ("conflict", {})

    if mode == "streamable":
        # (a)/(b) → SSE; (c) json > sse → conflict; (d) dual-q → SSE.
        if missing or star_only or sse_score >= json_score:
            return ("sse", scope)
        return ("conflict", {})

    # mode == "auto"
    # (a) star_only or missing → mode default (SSE).
    # (b)/(c) higher score wins.
    # (d) dual-q (json == sse > 0) → SDK delegation — we hand the scope
    #     through unchanged with the "sse" branch label because the
    #     instance-scope manager's stateless/json_response are both False,
    #     so the SDK will pick based on Accept order / its internal
    #     priority. Negotiation outcome is "let SDK decide".
    if missing or star_only:
        return ("sse", scope)
    if json_score > sse_score:
        return ("json", scope)
    if sse_score > json_score:
        return ("sse", scope)
    # dual-q
    return ("sse", scope)


def _apply_compat_hack(scope: dict) -> dict:
    """Sprint_4 Accept compat normalize — only used on the
    ``json_stateless`` path (Phase_2 §2.6 H5).

    ``*/*`` or missing Accept → ``application/json`` so the SDK's strict
    Accept validation accepts the request. This is the literal Sprint_4
    behavior that the cross-transport baseline depends on.
    """
    headers = list(scope.get("headers", []))
    has_accept = False
    normalized_headers: list[tuple[bytes, bytes]] = []
    for key, value in headers:
        if key.lower() == b"accept":
            has_accept = True
            if value.strip() == b"*/*":
                value = b"application/json"
        normalized_headers.append((key, value))
    if not has_accept:
        normalized_headers.append((b"accept", b"application/json"))
    return {**scope, "headers": normalized_headers}


def _transport_mode_mismatch_response(mode: str, body: bytes) -> JSONResponse:
    return _mcp_json_rpc_error_response(
        body,
        "transport_mode_mismatch",
        f"MCP HTTP mode expects {mode}",
        status_code=415,
        rpc_code=-32000,
        data={"expected": mode},
    )


def _not_acceptable_response(body: bytes) -> JSONResponse:
    return _mcp_json_rpc_error_response(
        body,
        "not_acceptable",
        "Accept must include application/json or text/event-stream",
        status_code=406,
        rpc_code=-32000,
    )
