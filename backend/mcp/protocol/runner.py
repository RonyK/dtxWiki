"""Async/sync bridge for invoking dtxWiki MCP tools through the SDK."""

import asyncio
import json
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.api.metrics import inc_mcp_tool_call
from backend.app.container import ToolCallContext
from backend.mcp.conversation import resolve_conversation_token
from backend.mcp.envelope import (
    MCPErrorResponse,
    error_envelope,
    validation_error_envelope,
)
from backend.mcp.protocol.lifecycle import evaluate_actor_mcp_surface_lifecycle
from backend.mcp.protocol.output_validation import (
    WIRE_ENVELOPE_FIELDS,
    validate_tool_output,
)
from backend.mcp.router import dispatch
from backend.mcp.scopes import (
    CONTENT_TOOLS,
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    has_required_scope,
)
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    scope_actor_to_workspace_grant,
)
from backend.policy.trust_boundaries import (
    actor_with_target_workspace_scope,
    check_actor_can_access,
)
from backend.schemas.common import EnvelopeWarning, MCPResultEnvelope
from backend.services.context_write_service import (
    stale_conversation_warning_message,
    stale_pages_for_conversation,
    unresolved_diverged_branch_warning_message,
    unresolved_diverged_branches_for_conversation,
)
from backend.services.mcp_client_service import (
    MCPClientRuntimeDenied,
    evaluate_mcp_client_runtime,
)
from backend.services.mcp_trace_capture import persist_mcp_tool_trace
from backend.services.mcp_trace_redaction import (
    redact_tool_arguments,
    redact_tool_capture,
)
from backend.services.page_service import PageService

logger = logging.getLogger("dtxwiki.mcp")
ERROR_RESULT_STATUSES = {"failed", "rejected_by_policy", "conflict"}
CONVERSATION_TOKEN_NOT_SUPPLIED_CODE = "conversation_token_not_supplied"
CONVERSATION_TOKEN_NOT_SUPPLIED_MESSAGE = (
    "This call was not linked to an existing conversation. Echo "
    "conversation_token on later calls, including reads, to keep the activity "
    "in one chat."
)
UNRESOLVED_DIVERGED_BRANCH_CODE = "unresolved_diverged_branch"
CONVERSATION_READ_STALE_CODE = "conversation_read_stale"
CONVERSATION_TOKEN_META_KEY = "com.dtxwiki/conversationToken"
# These successful reads issue the exact version identity later write semantics
# rely on. Returning success while their durable read-ledger transaction failed
# would turn a correctness record into best-effort telemetry.
_CORRECTNESS_CRITICAL_TRACE_TOOLS = frozenset(
    {"read_context_outline", "read_structure_map"}
)


# --- Trace-persistence observability -----------------------------------
#
# `_persist_trace` swallows a persistence failure unless the call is one of
# the correctness-critical tools above. That swallow stays: dispatch commits
# the tool's domain transaction BEFORE the trace transaction opens, so
# rethrowing would report `internal_error` for a mutation that already
# succeeded and invite a duplicate retry. What the swallow must not do is
# leave the failure invisible -- traces and chat sessions can stop being
# recorded with nothing for an operator to notice. These counters are that
# signal; `/health/storage-topology` reports them and turns
# `trace_persistence.status` to "degraded" once any failure has been counted.
#
# Deliberately NOT wired into /health/ready: a readiness flip would pull the
# whole deployment out of its load balancer for a telemetry write, which is
# the outage this swallow exists to avoid.
#
# Process-local, like the counters in backend/api/metrics.py: a deployment
# running several app processes gets one count per process, and each reports
# only its own.
_trace_persistence_lock = threading.Lock()
_trace_persistence_failures = 0
_trace_persistence_last_failure_at: str | None = None


def record_trace_persistence_failure(when: datetime | None = None) -> None:
    """Count one trace-persistence failure. Never raises."""

    global _trace_persistence_failures, _trace_persistence_last_failure_at
    stamp = (when or _utc_now()).isoformat()
    with _trace_persistence_lock:
        _trace_persistence_failures += 1
        _trace_persistence_last_failure_at = stamp


def trace_persistence_health() -> dict[str, Any]:
    """Snapshot of the trace-persistence signal for the health surface."""

    with _trace_persistence_lock:
        failures = _trace_persistence_failures
        last_failure_at = _trace_persistence_last_failure_at
    return {
        "status": "degraded" if failures else "ok",
        "failures_total": failures,
        "last_failure_at": last_failure_at,
    }


def reset_trace_persistence_health() -> None:
    """Test-only: clear the process-local counters."""

    global _trace_persistence_failures, _trace_persistence_last_failure_at
    with _trace_persistence_lock:
        _trace_persistence_failures = 0
        _trace_persistence_last_failure_at = None


class _ConversationTokenCarrierConflict(ValueError):
    """Raised when one call supplies disagreeing conversation-token carriers."""


class _WorkspaceTargetInput(BaseModel):
    workspace_id: UUID

    model_config = ConfigDict(extra="ignore")


class _WorkspaceTargetValidationError(ValueError):
    """Carries a target-only schema error through the common sanitize barrier."""

    def __init__(self, validation_error: ValidationError) -> None:
        super().__init__("workspace_id validation failed")
        self.validation_error = validation_error


class _ToolScopeDenied(PermissionError):
    """Raised before target parsing when the credential cannot call a tool."""

    error_code = "scope_denied"


async def call_tool_async(
    name: str,
    arguments: dict[str, Any],
    *,
    actor: ActorContext,
    page_service: PageService,
    session_factory=None,
    mcp_session_id: str | None = None,
    search_service=None,
    tool_context: ToolCallContext | None = None,
) -> CallToolResult:
    started_at = _utc_now()
    started = time.perf_counter()
    metric_status = "error"
    db = tool_context.db if tool_context is not None else None
    fallback_session_factory = session_factory or page_service.session_factory
    control_factory = (
        db.control_session_factory() if db is not None else fallback_session_factory
    )
    # Preflight — workspace resolution, authorization, shard selection, and the
    # conversation-token lookup — runs INSIDE the sanitize barrier and off the
    # event loop. These touch the control/data DB, so a connection or
    # operational error must become a sanitized envelope (never a str(exc) leak
    # of SQL/params) and must not block other concurrent MCP calls. Pre-init the
    # values the except/finally reference so an early failure stays sanitized.
    requested_workspace_id: UUID | None = None
    trace_workspace_id: UUID | None = None
    conversation_token: str | None = None
    requested_conversation_token: Any = None
    trace_arguments = redact_tool_arguments(name, arguments)
    # Referenced by the except path's trace, which can run before the assignment
    # below if preflight raises.
    is_content_call = name in CONTENT_TOOLS
    try:
        required_scopes = TOOL_REQUIRED_SCOPES.get(name)
        if required_scopes is not None and not has_required_scope(
            actor, required_scopes
        ):
            # Scope is actor-bound and cannot be repaired by any workspace
            # identifier. Preserve the historical scope_denied precedence
            # without consulting payload-derived page/slug targets.
            raise _ToolScopeDenied
        requested_workspace_id = await asyncio.to_thread(
            _workspace_id_for_token,
            name,
            arguments,
        )
        (
            trace_workspace_id,
            requested_workspace_authorized,
        ) = await asyncio.to_thread(
            _trace_workspace_target,
            actor=actor,
            requested_workspace_id=requested_workspace_id,
            control_factory=control_factory,
        )
        # Lifecycle is an execution gate, not Settings display metadata. Resolve
        # every applicable stable credential binding before conversation
        # creation, dispatch, or trace/chat/bundle persistence. In particular,
        # ``list_my_workspaces`` must not become an escape hatch: a Workspace-
        # bound PAT/direct client checks its bound Workspace, while a future
        # multi-Workspace OAuth grant checks every live, token-authorized
        # Workspace before returning the aggregate directory.
        runtime_workspace_ids = await asyncio.to_thread(
            _runtime_workspace_ids_for_call,
            name=name,
            actor=actor,
            trace_workspace_id=trace_workspace_id,
            control_factory=control_factory,
        )
        runtime_actors: dict[UUID, ActorContext] = {}
        for runtime_workspace_id in runtime_workspace_ids:
            runtime_session_factory = (
                db.session_for(runtime_workspace_id)
                if db is not None
                else fallback_session_factory
            )
            runtime_actors[runtime_workspace_id] = await asyncio.to_thread(
                _scope_runtime_actor_to_live_grant,
                actor=actor,
                workspace_id=runtime_workspace_id,
                control_factory=control_factory,
                workspace_session_factory=runtime_session_factory,
            )

        if name == "list_my_workspaces":
            lifecycle_result = await asyncio.to_thread(
                evaluate_actor_mcp_surface_lifecycle,
                actor=actor,
                fallback_session_factory=fallback_session_factory,
                db=db,
            )
            if (
                not lifecycle_result.active_workspace_ids
                and lifecycle_result.denial is not None
            ):
                raise lifecycle_result.denial
            actor = lifecycle_result.actor
            if tool_context is not None:
                tool_context = replace(tool_context, actor=actor)
            if trace_workspace_id not in lifecycle_result.active_workspace_ids:
                trace_workspace_id = actor.workspace_id
            runtime_workspace_ids = ()
        for runtime_workspace_id in runtime_workspace_ids:
            runtime_session_factory = (
                db.session_for(runtime_workspace_id)
                if db is not None
                else fallback_session_factory
            )
            runtime_denial = await asyncio.to_thread(
                _evaluate_runtime_client,
                session_factory=runtime_session_factory,
                workspace_id=runtime_workspace_id,
                actor=runtime_actors[runtime_workspace_id],
            )
            if runtime_denial is not None:
                raise runtime_denial
        # Conversation ownership and trace routing use only an authorized
        # workspace: prefer the resolved tool target, then the actor's bound
        # workspace, so a payload-claimed workspace cannot select a data-plane
        # shard before the normal dispatch authorization gate runs.
        conversation_session_factory = None
        if trace_workspace_id is not None:
            conversation_session_factory = (
                db.session_for(trace_workspace_id)
                if db is not None
                else fallback_session_factory
            )
        requested_conversation_token = _requested_conversation_token(arguments)
        conversation = await asyncio.to_thread(
            resolve_conversation_token,
            session_factory=conversation_session_factory,
            actor=actor,
            requested_token=requested_conversation_token,
            requested_workspace_id=trace_workspace_id,
            requested_workspace_authorized=requested_workspace_authorized,
            require_token=is_content_call,
        )
        conversation_token = conversation.token
        # conversation_token is a transport concern consumed above for chat
        # grouping; strip it before dispatch/trace so tool input schemas never
        # validate it (list_my_workspaces forbids it, and every tool rejects a
        # blank "" under min_length=1). Tool handlers already exclude it.
        # `_meta` is the negotiable carrier for clients that cannot thread an
        # extra tool argument; it is stripped for the same reason.
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in ("conversation_token", "_meta")
        }
        trace_arguments = redact_tool_arguments(name, arguments)
        if not conversation.ok:
            envelope = error_envelope(
                actor,
                conversation.error_code,
                result_status="failed",
            )
            envelope.result_summary = (
                # Omitting the token no longer starts a conversation, so this
                # branch has to say how to recover rather than implying a retry.
                "This tool writes workspace content and needs an explicit "
                "conversation_token. Echo envelope.conversation_token from an "
                'earlier response to continue that chat, or pass "new" to start '
                "one. The token may also be sent as "
                '_meta["com.dtxwiki/conversationToken"]; '
                "_meta.conversation_token remains accepted for legacy clients."
                if conversation.error_code == "conversation_token_required"
                # L-06: do NOT tell the client to retry the SAME request with the
                # returned token — when the target workspace is unauthorized the
                # retry loops (the anti-oracle path re-mints and re-rejects). Guide
                # them to adopt the token for a NEW request or start fresh.
                else (
                    "Conversation token was not recognized for this "
                    "actor/workspace. Use envelope.conversation_token for a new "
                    'request, or pass "new" to start a new conversation.'
                )
            )
            if requested_workspace_id is not None:
                envelope.workspace_id = requested_workspace_id
            envelope.conversation_token = conversation.token
            payload = _tool_error_payload(envelope)
            trace_arguments, trace_payload, payload = _capture_trace_and_wire_payload(
                name,
                arguments,
                payload,
            )
            _persist_trace(
                session_factory=fallback_session_factory,
                db=db,
                actor=actor,
                name=name,
                arguments=trace_arguments,
                payload=trace_payload,
                mcp_session_id=conversation.token,
                started_at=started_at,
                latency_ms=int((time.perf_counter() - started) * 1000),
                trace_workspace_id=trace_workspace_id,
                # A rejected token still registers the minted replacement, which
                # is what the envelope tells the client to adopt for its next
                # request (see L-06 above) — dropping it would break that
                # self-correction. `conversation_token_required` is different:
                # there is no token to register, and creating a chat for a call
                # that never ran is exactly the churn this contract removes.
                create_chat_session=(
                    conversation.error_code != "conversation_token_required"
                ),
            )
            # metric_status stays "error"; the finally emits it.
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                ],
                structuredContent=payload,
                isError=True,
            )
        response = await asyncio.to_thread(
            dispatch,
            name,
            arguments,
            actor,
            page_service,
            session_factory,
            conversation.token,
            search_service=search_service,
            tool_context=tool_context,
        )
        payload = response.model_dump(mode="json")
        envelope = payload.get("envelope", {})
        envelope["conversation_token"] = conversation.token
        if isinstance(payload.get("envelope"), dict):
            payload["envelope"] = envelope
        status = envelope.get("result_status")
        if (
            status == "succeeded"
            and not is_content_call
            and name != "list_my_workspaces"
            and requested_conversation_token is None
        ):
            envelope.setdefault("warnings", []).append(
                EnvelopeWarning(
                    code=CONVERSATION_TOKEN_NOT_SUPPLIED_CODE,
                    message=CONVERSATION_TOKEN_NOT_SUPPLIED_MESSAGE,
                ).model_dump(mode="json")
            )
        if status == "succeeded" and conversation.token is not None:
            nag_workspace_id = trace_workspace_id or requested_workspace_id
            nag_session_factory = (
                db.session_for(nag_workspace_id)
                if db is not None and nag_workspace_id is not None
                else fallback_session_factory
            )
            if nag_workspace_id is not None and nag_session_factory is not None:
                try:
                    with nag_session_factory() as session:
                        branches = unresolved_diverged_branches_for_conversation(
                            session,
                            workspace_id=nag_workspace_id,
                            conversation_token=conversation.token,
                        )
                except Exception:
                    logger.exception("unresolved diverged branch nag lookup failed")
                else:
                    if branches:
                        envelope.setdefault("warnings", []).append(
                            EnvelopeWarning(
                                code=UNRESOLVED_DIVERGED_BRANCH_CODE,
                                message=unresolved_diverged_branch_warning_message(
                                    branches, workspace_id=nag_workspace_id
                                ),
                            ).model_dump(mode="json")
                        )
                try:
                    with nag_session_factory() as session:
                        stale_pages = stale_pages_for_conversation(
                            session,
                            workspace_id=nag_workspace_id,
                            conversation_token=conversation.token,
                            actor=actor,
                            # patch_draft (the only tool this ever excluded a
                            # page for, to suppress a stale warning about the
                            # page it just resolved) is deregistered;
                            # patch_context_edit never diverges (always
                            # applies to the caller's own row), so it has no
                            # equivalent push_outcome to key off.
                            excluded_page_ids=frozenset(),
                        )
                except Exception:
                    # This notification is advisory, but its ownership and map
                    # proof are not.  On any lookup/parser inconsistency, omit
                    # it without reflecting database details into the envelope.
                    logger.exception("conversation stale-page lookup failed")
                else:
                    if stale_pages:
                        envelope.setdefault("warnings", []).append(
                            EnvelopeWarning(
                                code=CONVERSATION_READ_STALE_CODE,
                                message=stale_conversation_warning_message(stale_pages),
                            ).model_dump(mode="json")
                        )
        metric_status = _metric_status(status)
        trace_arguments, trace_payload, payload = _capture_trace_and_wire_payload(
            name,
            arguments,
            payload,
        )
        _persist_trace(
            session_factory=fallback_session_factory,
            db=db,
            actor=actor,
            name=name,
            arguments=trace_arguments,
            payload=trace_payload,
            read_projection=getattr(response, "_served_read_projection", None),
            mcp_session_id=conversation.token
            or mcp_session_id
            or str(envelope.get("request_id") or ""),
            started_at=started_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_workspace_id=trace_workspace_id,
            fail_closed=(
                status == "succeeded" and name in _CORRECTNESS_CRITICAL_TRACE_TOOLS
            ),
        )
        logger.info(
            "tools/call name=%s actor=%s ms=%d status=%s",
            name,
            actor.actor_id,
            int((time.perf_counter() - started) * 1000),
            status,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=status in ERROR_RESULT_STATUSES,
        )
    except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
        # Credential-replica and workspace-plane availability are pre-dispatch
        # failures. They must remain retryable and, unlike ordinary rejected
        # tool results, must not first-touch an MCPClient or create trace/chat
        # rows for a credential that has not passed its workspace-plane gate.
        envelope = error_envelope(actor, exc.error_code, message=str(exc))
        if trace_workspace_id is not None:
            envelope.workspace_id = trace_workspace_id
        payload = _tool_error_payload(envelope)
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in ("conversation_token", "_meta")
        }
        _trace_arguments, _trace_payload, payload = _capture_trace_and_wire_payload(
            name,
            arguments,
            payload,
            validate_contract=False,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=True,
        )
    except (MCPClientRuntimeDenied, _ToolScopeDenied) as exc:
        metric_status = "denied"
        envelope = error_envelope(
            actor,
            exc.error_code,
            result_status="rejected_by_policy",
            message=str(exc),
            retryable=False,
        )
        if trace_workspace_id is not None:
            envelope.workspace_id = trace_workspace_id
        payload = _tool_error_payload(envelope)
        # The denial is raised before the success path strips the transport
        # carriers, so strip them here too -- they must not reach redaction.
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in ("conversation_token", "_meta")
        }
        # Deliberately no _persist_trace call: neither an unscoped credential
        # nor a lifecycle-blocked credential has passed target authorization.
        # A lifecycle denial may create only its dedicated audit record. The
        # trace copies this helper returns are therefore discarded here. It is
        # called for its WIRE shaping: without it this return served the full
        # 23-field audit envelope while every other return served the slimmed
        # one, and the wire-phase schema classifies that as a hard violation.
        # validate_contract=False for the same reason as the two handlers
        # below: this is inside an except block, so a raise has no sanitize
        # barrier above it, and the payload is _tool_error_payload's fixed
        # shape, proved conformant for every tool by
        # test_tool_error_payload_satisfies_every_tool_contract_in_both_phases.
        _trace_arguments, _trace_payload, payload = _capture_trace_and_wire_payload(
            name,
            arguments,
            payload,
            validate_contract=False,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=True,
        )
    except (_ConversationTokenCarrierConflict, _WorkspaceTargetValidationError) as exc:
        # Do not pass transport carriers to trace storage or schema validation,
        # and never reflect their values in the validation response. A missing
        # workspace target also lands here before any directory or shard lookup.
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in ("conversation_token", "_meta")
        }
        trace_arguments = redact_tool_arguments(name, arguments)
        envelope = (
            validation_error_envelope(
                actor,
                exc.validation_error,
                allowed_fields=_WorkspaceTargetInput.model_fields,
            )
            if isinstance(exc, _WorkspaceTargetValidationError)
            else error_envelope(
                actor,
                "validation_failed",
                result_status="failed",
                message=str(exc),
                retryable=False,
            )
        )
        if requested_workspace_id is not None:
            envelope.workspace_id = requested_workspace_id
        payload = _tool_error_payload(envelope)
        # Re-entrancy guard: a raise from inside an except block has no
        # sanitize barrier above it, so the fail-closed payload is never
        # itself validated.
        trace_arguments, trace_payload, payload = _capture_trace_and_wire_payload(
            name,
            arguments,
            payload,
            validate_contract=False,
        )
        _persist_trace(
            session_factory=fallback_session_factory,
            db=db,
            actor=actor,
            name=name,
            arguments=trace_arguments,
            payload=trace_payload,
            mcp_session_id=mcp_session_id or str(envelope.request_id),
            started_at=started_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_workspace_id=trace_workspace_id,
            create_chat_session=False,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=True,
        )
    except Exception:
        # Set before anything else: the success path assigns metric_status
        # "ok" immediately before the capture call, so a contract violation
        # raised there would otherwise be counted as a success while this
        # handler returns isError=True.
        metric_status = "error"
        # The SDK would otherwise convert an uncaught exception into an
        # error result built from str(exc) — which for SQLAlchemy errors
        # includes the SQL statement and bound parameters — and the failed
        # call would never reach the MCP-chats audit trail. This barrier now
        # also covers the preflight (workspace/authz/shard/conversation), so a
        # control- or data-plane DB error there is sanitized, not leaked.
        logger.exception(
            "tools/call name=%s actor=%s dispatch raised", name, actor.actor_id
        )
        envelope = error_envelope(actor, "internal_error", result_status="failed")
        if requested_workspace_id is not None:
            envelope.workspace_id = requested_workspace_id
        envelope.result_summary = (
            "Tool call failed due to an unexpected server error. "
            "Details were logged on the server."
        )
        if conversation_token is not None:
            envelope.conversation_token = conversation_token
        payload = _tool_error_payload(envelope)
        # Re-entrancy guard: this handler is where a contract violation lands,
        # so validating its own replacement payload could only loop.
        trace_arguments, trace_payload, payload = _capture_trace_and_wire_payload(
            name,
            arguments,
            payload,
            validate_contract=False,
        )
        _persist_trace(
            session_factory=fallback_session_factory,
            db=db,
            actor=actor,
            name=name,
            arguments=trace_arguments,
            payload=trace_payload,
            mcp_session_id=conversation_token
            or mcp_session_id
            or str(envelope.request_id),
            started_at=started_at,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_workspace_id=trace_workspace_id,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=True,
        )
    finally:
        inc_mcp_tool_call(tool=name, status=metric_status)


def _metric_status(result_status) -> str:
    if result_status == "rejected_by_policy":
        return "denied"
    if result_status in ERROR_RESULT_STATUSES:
        return "error"
    return "ok"


def _evaluate_runtime_client(
    *,
    session_factory,
    workspace_id: UUID,
    actor: ActorContext,
) -> MCPClientRuntimeDenied | None:
    if session_factory is None:
        return None
    with session_factory.begin() as session:
        return evaluate_mcp_client_runtime(
            session,
            workspace_id=workspace_id,
            actor=actor,
        )


def _scope_runtime_actor_to_live_grant(
    *,
    actor: ActorContext,
    workspace_id: UUID,
    control_factory,
    workspace_session_factory,
) -> ActorContext:
    """Resolve control scope, then fail closed on the replica before first touch."""

    with control_factory() as control_session:
        scoped_actor = actor_with_target_workspace_scope(
            actor,
            workspace_id,
            session=control_session,
        )
    return scope_actor_to_workspace_grant(
        actor=scoped_actor,
        workspace_id=workspace_id,
        workspace_session_factory=workspace_session_factory,
    )


def _tool_error_payload(envelope: MCPResultEnvelope) -> dict[str, Any]:
    """Shape a pre-dispatch failure into wire payload.

    Every advertised output model requires only the common envelope, so the
    shared error envelope satisfies every tool's contract unmodified.
    """

    return MCPErrorResponse(envelope=envelope).model_dump(mode="json")


def _requested_conversation_token(arguments: dict[str, Any]) -> Any:
    """Resolve the canonical argument, namespaced `_meta`, then legacy `_meta`.

    The plain argument stays canonical because it is visible to the model.
    Custom MCP metadata uses dtxWiki's vendor namespace; the unnamespaced key is
    read-only legacy compatibility. Disagreeing carriers are rejected rather
    than resolved by precedence.
    """
    carriers: list[tuple[str, Any]] = []
    token = arguments.get("conversation_token")
    if token is not None:
        carriers.append(("conversation_token", token))
    meta = arguments.get("_meta")
    if isinstance(meta, dict):
        namespaced_token = meta.get(CONVERSATION_TOKEN_META_KEY)
        if namespaced_token is not None:
            carriers.append(
                ('_meta["com.dtxwiki/conversationToken"]', namespaced_token)
            )
        legacy_token = meta.get("conversation_token")
        if legacy_token is not None:
            carriers.append(("_meta.conversation_token", legacy_token))
    if not carriers:
        return None

    selected_token = carriers[0][1]
    if any(value != selected_token for _, value in carriers[1:]):
        carrier_names = ", ".join(name for name, _ in carriers)
        raise _ConversationTokenCarrierConflict(
            f"Conversation token carriers have conflicting values: {carrier_names}."
        )
    return selected_token


# Defined in output_validation.py (which derives the wire-phase schema from
# it and cannot import this module back) and aliased here under the name the
# slimming function and its tests already use.
_WIRE_ENVELOPE_FIELDS = WIRE_ENVELOPE_FIELDS


def _slim_envelope_for_wire(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim ``payload["envelope"]`` to the wire-facing fields, folding the
    rest into ``envelope.meta``. Applied to the WIRE copy only -- the caller
    passes the untrimmed ``payload`` to ``_persist_trace`` separately, so
    mcp_trace_capture.py keeps reading the full envelope unchanged."""

    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        return payload
    kept = {k: v for k, v in envelope.items() if k in _WIRE_ENVELOPE_FIELDS}
    meta = {k: v for k, v in envelope.items() if k not in _WIRE_ENVELOPE_FIELDS}
    kept["meta"] = meta
    return {**payload, "envelope": kept}


def _capture_trace_and_wire_payload(
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    *,
    validate_contract: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Redact a tool result for durable capture and slim it for the wire.

    Two DISTINCT contracts are checked, not one. ``payload`` is the full,
    untrimmed result the handler returned; ``wire_payload`` is what the client
    actually receives after trace redaction and envelope slimming. Checking
    only the first would miss any defect those two steps introduce.

    A violation raises ``MCPOutputContractViolation``, which the caller's
    sanitize barrier converts into the fixed internal_error result -- fail
    closed, because dropping outputSchema from the wire left no client-side
    check to catch it. ``validate_contract=False`` is for the three ``except``
    handlers in ``call_tool_async`` above only (``MCPClientRuntimeDenied``,
    ``_ConversationTokenCarrierConflict`` and the catch-all ``Exception``):
    their payload is ``_tool_error_payload``'s fixed shape (proved conformant
    for every tool by
    ``test_tool_error_payload_satisfies_every_tool_contract_in_both_phases``),
    and a raise from inside an except block has no barrier above it. They
    still call this helper, so their result is redacted and slimmed like any
    other served payload -- only the validation is skipped.
    """

    if validate_contract:
        # Not redundant with the wire check below. The wire copy folds the 13
        # audit/telemetry envelope fields into `envelope.meta`, typed
        # `dict[str, Any] | None`, where any JSON is legal -- so for those
        # fields THIS is the only gate. Pinned by
        # test_call_tool_async_fails_closed_on_a_violation_the_wire_phase_cannot_see.
        validate_tool_output(name, payload)
    trace_arguments, trace_payload = redact_tool_capture(name, arguments, payload)
    wire_payload = _slim_envelope_for_wire(trace_payload)
    if validate_contract:
        validate_tool_output(name, wire_payload, phase="wire")
    return trace_arguments, trace_payload, wire_payload


def _persist_trace(
    *,
    session_factory,
    db=None,
    actor: ActorContext,
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    mcp_session_id: str,
    started_at,
    latency_ms: int,
    trace_workspace_id: UUID | None = None,
    create_chat_session: bool = True,
    fail_closed: bool = False,
    read_projection: dict[str, Any] | None = None,
) -> None:
    if not mcp_session_id or trace_workspace_id is None:
        return
    arguments, payload = redact_tool_capture(name, arguments, payload)
    envelope = payload.get("envelope") or {}
    trace_payload = {
        **payload,
        "envelope": {
            **envelope,
            "workspace_id": str(trace_workspace_id),
        },
    }
    trace_factory = (
        db.session_for(trace_workspace_id) if db is not None else session_factory
    )
    if trace_factory is None:
        return
    try:
        with trace_factory.begin() as session:
            persist_mcp_tool_trace(
                session=session,
                actor=actor,
                tool_name=name,
                arguments=arguments,
                payload=trace_payload,
                mcp_session_id=mcp_session_id,
                started_at=started_at,
                completed_at=_utc_now(),
                latency_ms=latency_ms,
                create_chat_session=create_chat_session,
                read_projection=read_projection,
            )
    except Exception:
        # Counted BEFORE the fail_closed branch: a correctness-critical trace
        # that raises is still a persistence failure, and the operator signal
        # must not depend on which tool happened to fail.
        record_trace_persistence_failure()
        logger.exception("failed to persist MCP tool trace")
        if fail_closed:
            raise


def _trace_workspace_target(
    *,
    actor: ActorContext,
    requested_workspace_id: UUID | None,
    control_factory,
) -> tuple[UUID | None, bool]:
    """Resolve trace ownership only from workspaces the actor may read."""
    candidates = (
        requested_workspace_id,
        actor.workspace_id,
        *actor.token_workspace_ids,
    )
    checked: set[UUID] = set()
    requested_workspace_authorized = requested_workspace_id is None
    for workspace_id in candidates:
        if workspace_id is None or workspace_id in checked:
            continue
        checked.add(workspace_id)
        try:
            with control_factory() as session:
                check_actor_can_access(
                    actor,
                    workspace_id,
                    "read",
                    session=session,
                )
        except PermissionError:
            continue
        if workspace_id == requested_workspace_id:
            requested_workspace_authorized = True
        return workspace_id, requested_workspace_authorized
    return None, requested_workspace_authorized


def _runtime_workspace_ids_for_call(
    *,
    name: str,
    actor: ActorContext,
    trace_workspace_id: UUID | None,
    control_factory,
) -> tuple[UUID, ...]:
    """Return live authorized Workspaces whose client lifecycle must pass.

    Most tools have one resolved target. ``list_my_workspaces`` is the other
    caller: D-01 means a PAT/OAuth actor now names at most its own one bound
    Workspace here too, so this degenerates to that single candidate (or the
    actor's bound/default Workspace) rather than a multi-workspace directory.
    Membership and token scoping are rechecked before a lifecycle row may be
    read or first-created.
    """

    if name != "list_my_workspaces":
        return (trace_workspace_id,) if trace_workspace_id is not None else ()

    candidates = actor.token_workspace_ids
    if not candidates:
        candidates = (actor.workspace_id,) if actor.workspace_id is not None else ()
    if not candidates and trace_workspace_id is not None:
        candidates = (trace_workspace_id,)

    authorized: list[UUID] = []
    seen: set[UUID] = set()
    for workspace_id in candidates:
        if workspace_id in seen:
            continue
        seen.add(workspace_id)
        try:
            with control_factory() as session:
                check_actor_can_access(
                    actor,
                    workspace_id,
                    "read",
                    session=session,
                )
        except PermissionError:
            continue
        authorized.append(workspace_id)
    return tuple(authorized)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _workspace_id_for_token(
    tool_name: str,
    arguments: dict[str, Any],
) -> UUID | None:
    if tool_name not in TOOL_REQUIRED_ROLES or TOOL_REQUIRED_ROLES[tool_name] is None:
        return _workspace_id_from_arguments(arguments)
    # Every current workspace-bound tool schema requires workspace_id. Validate
    # that field in isolation before any page/slug directory lookup; malformed
    # input must not use target existence to choose a shard or error shape.
    try:
        return _WorkspaceTargetInput.model_validate(arguments).workspace_id
    except ValidationError as exc:
        raise _WorkspaceTargetValidationError(exc) from exc


def _workspace_id_from_arguments(arguments: dict[str, Any]) -> UUID | None:
    value = arguments.get("workspace_id")
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
