"""Recover keyed writes using the existing call log and atomic write receipts.

An arrival is committed before dispatch, never in the transaction holding the
idempotency claim. The status of a key is read from its latest arrival; a
committed domain receipt for that call is always stronger evidence than the
arrival's own completion fields, including after a worker loses its connection.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from backend.models.base import utc_now
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import select_in_workspace
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.mcp_chat_service import mcp_session_trace_uuid
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.mcp_trace_capture import persist_mcp_tool_trace
from backend.services.mcp_trace_redaction import redact_tool_arguments
from backend.services.mcp_write_idempotency import IDEMPOTENCY_WINDOW

DEFAULT_OPERATION_TIMEOUT_SECONDS = 120
RESEND_GUIDANCE = (
    "Resend the same request with the same key once; resending it is safe. "
    "If the response is lost again or the result is the same, stop resending "
    "that payload. For a large body, split the work into patches with new keys "
    "or report the problem to the user. Outcomes are retained for 24 hours."
)
INTERRUPTED_MESSAGE = (
    "No committed result was found before the operation time limit. "
    "Resend the original request with the same key and original arguments."
)


@dataclass(frozen=True)
class AttemptIdentity:
    id: UUID
    workspace_id: UUID
    request_id: UUID
    started_at: datetime


@dataclass(frozen=True)
class OperationSnapshot:
    status: str
    next_action: str
    tool_name: str | None = None
    receipt_payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    guidance: str | None = None
    retry_after_seconds: int | None = None


def start_operation_attempt(
    session_factory,
    *,
    workspace_id: UUID,
    actor: ActorContext,
    tool_name: str,
    idempotency_key: str,
    arguments: dict[str, Any] | None = None,
    mcp_session_id: str | None = None,
    request_id: UUID | None = None,
    started_at: datetime | None = None,
) -> AttemptIdentity:
    """Commit one call row before dispatch; retries retain their own history."""

    request_id = request_id or uuid4()
    started_at = started_at or utc_now()
    with session_factory.begin() as session:
        acquire_workspace_write_intent(session)
        client = resolve_trace_mcp_client(
            session, workspace_id=workspace_id, actor=actor
        )
        row = MCPToolCall(
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=mcp_session_trace_uuid(mcp_session_id or str(request_id)),
            request_id=request_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            started_at=started_at,
            result_status="in_progress",
            input_summary=redact_tool_arguments(tool_name, arguments or {}),
        )
        session.add(row)
        session.flush()
        return AttemptIdentity(row.id, workspace_id, request_id, started_at)


def finish_operation_attempt(
    session_factory,
    identity: AttemptIdentity,
    *,
    actor: ActorContext,
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    mcp_session_id: str,
    latency_ms: int,
    edit_first_change: dict[str, Any] | None = None,
) -> None:
    """Finalize this call and its Chats projection, even after a disconnect."""

    payload = {
        **payload,
        "envelope": {
            **(payload.get("envelope") or {}),
            "workspace_id": str(identity.workspace_id),
            "request_id": str(identity.request_id),
        },
    }
    with session_factory.begin() as session:
        persist_mcp_tool_trace(
            session=session,
            actor=actor,
            tool_name=tool_name,
            arguments=arguments,
            payload=payload,
            mcp_session_id=mcp_session_id,
            started_at=identity.started_at,
            completed_at=utc_now(),
            latency_ms=latency_ms,
            edit_first_change=edit_first_change,
        )


def lookup_operation_status(
    session,
    *,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    idempotency_key: str,
    commands: Mapping[str, str],
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> OperationSnapshot:
    """Read only. Status starts from the latest arrival of this actor's key.

    ``commands`` maps each keyed tool to the ledger command its receipt is
    stored under. Calls without an arrival -- expired ones, other actors' and
    non-MCP writes -- are indistinguishable from a request never received.
    """

    now = now or utc_now()
    attempt = session.scalar(
        select_in_workspace(
            MCPToolCall,
            workspace_id=workspace_id,
            conditions=(
                MCPToolCall.actor_kind == actor_kind,
                MCPToolCall.actor_id == actor_id,
                MCPToolCall.idempotency_key == idempotency_key,
                MCPToolCall.started_at > now - IDEMPOTENCY_WINDOW,
            ),
        )
        .order_by(MCPToolCall.started_at.desc(), MCPToolCall.id.desc())
        .limit(1)
    )
    if attempt is None:
        return OperationSnapshot(
            "not_seen", "resend_same_request", guidance=RESEND_GUIDANCE
        )
    command = commands.get(attempt.tool_name)
    receipt = (
        session.scalar(
            select_in_workspace(
                MCPWriteIdempotency,
                workspace_id=workspace_id,
                conditions=(
                    MCPWriteIdempotency.actor_kind == actor_kind,
                    MCPWriteIdempotency.actor_id == actor_id,
                    MCPWriteIdempotency.tool_name == command,
                    MCPWriteIdempotency.idempotency_key == idempotency_key,
                    MCPWriteIdempotency.expires_at > now,
                ),
            )
        )
        if command is not None
        else None
    )
    if receipt is not None:
        return OperationSnapshot(
            "committed",
            "use_result",
            tool_name=attempt.tool_name,
            receipt_payload=dict(receipt.result_payload),
        )
    if attempt.error_code is not None:
        return _failed(
            attempt.tool_name,
            code=attempt.error_code,
            message=attempt.result_summary or "The operation failed.",
            retryable=attempt.retryable,
        )
    if attempt.completed_at is not None or now - attempt.started_at >= timedelta(
        seconds=timeout_seconds
    ):
        return _failed(
            attempt.tool_name,
            code="operation_interrupted",
            message=INTERRUPTED_MESSAGE,
            retryable=True,
        )
    return OperationSnapshot(
        "in_progress",
        "wait_and_check",
        tool_name=attempt.tool_name,
        retry_after_seconds=2,
    )


def _failed(
    tool_name: str, *, code: str, message: str, retryable: bool | None
) -> OperationSnapshot:
    # Only a failure known to repeat needs a changed request. Resending the
    # same key is otherwise always safe: a commit would replay, not repeat.
    resend = retryable is not False
    return OperationSnapshot(
        "failed",
        "resend_same_request" if resend else "fix_request",
        tool_name=tool_name,
        error={"code": code, "message": message, "retryable": retryable},
        guidance=RESEND_GUIDANCE if resend else None,
    )
