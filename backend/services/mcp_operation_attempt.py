"""Durable arrival/completion records for authorized keyed MCP writes.

An arrival is committed before dispatch, never in the transaction holding the
idempotency claim. A committed domain receipt is always stronger evidence than
an arrival's completion flag, including after a worker loses its connection.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from backend.models.base import utc_now
from backend.models.mcp_trace import MCPOperationAttempt, MCPWriteIdempotency
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    update_in_workspace,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.mcp_write_idempotency import IDEMPOTENCY_WINDOW

DEFAULT_OPERATION_TIMEOUT_SECONDS = 120
OPERATION_COMMAND_NAMES = {"update_context_edit": "autosave_page"}
NOT_SEEN_GUIDANCE = (
    "No unexpired record is available; outcomes are retained for 24 hours. "
    "Resend the same request with the same key once. If its response is lost "
    "again and the status is still not_seen, stop resending the same payload. "
    "For a large body, split the work into patches with new keys or report "
    "the problem to the user."
)
INTERRUPTED_MESSAGE = (
    "No committed result was found before the operation time limit. "
    "Resend the original request with the same key and original arguments."
)


@dataclass(frozen=True)
class AttemptIdentity:
    id: UUID
    generation: int
    workspace_id: UUID


@dataclass(frozen=True)
class OperationSnapshot:
    status: str
    next_action: str
    tool_name: str | None = None
    result_payload: dict[str, Any] | None = None
    from_ledger: bool = False
    authorization_context: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    guidance: str | None = None
    retry_after_seconds: int | None = None


def start_operation_attempt(
    session_factory,
    *,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    tool_name: str,
    idempotency_key: str,
    authorization_context: dict[str, Any] | None = None,
) -> AttemptIdentity:
    """Fail closed if this separate arrival transaction cannot commit."""

    now = utc_now()
    with session_factory.begin() as session:
        acquire_workspace_write_intent(session)
        scope = (
            MCPOperationAttempt.actor_kind == actor_kind,
            MCPOperationAttempt.actor_id == actor_id,
            MCPOperationAttempt.tool_name == tool_name,
            MCPOperationAttempt.idempotency_key == idempotency_key,
        )
        row = session.scalar(
            select_in_workspace(
                MCPOperationAttempt, workspace_id=workspace_id, conditions=scope
            ).with_for_update()
        )
        inserted = False
        if row is None:
            candidate = MCPOperationAttempt(
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                attempts=1,
                started_at=now,
                expires_at=now + IDEMPOTENCY_WINDOW,
                authorization_context=authorization_context,
            )
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                row = candidate
                inserted = True
            except IntegrityError:
                # PostgreSQL concurrent first arrival: wait for, then lock,
                # the winning key without aborting the outer transaction.
                row = session.scalar(
                    select_in_workspace(
                        MCPOperationAttempt, workspace_id=workspace_id, conditions=scope
                    ).with_for_update()
                )
                if row is None:
                    raise
        if not inserted:
            if row.expires_at <= now:
                row.expires_at = now + IDEMPOTENCY_WINDOW
            # Keep generation monotonic even after expiry: an old worker must
            # never complete a newer attempt accidentally.
            row.attempts += 1
            row.started_at = now
            row.completed_at = None
            row.result_payload = None
            row.error_code = None
            row.error_message = None
            row.retryable = None
            row.authorization_context = authorization_context
            session.flush()
        return AttemptIdentity(row.id, row.attempts, workspace_id)


def finish_operation_attempt(
    session_factory,
    identity: AttemptIdentity,
    *,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retryable: bool | None = None,
) -> None:
    """A late worker cannot overwrite the state of a more recent resend."""

    with session_factory.begin() as session:
        session.execute(
            update_in_workspace(
                MCPOperationAttempt,
                workspace_id=identity.workspace_id,
                conditions=(
                    MCPOperationAttempt.id == identity.id,
                    MCPOperationAttempt.attempts == identity.generation,
                ),
                values=dict(
                    completed_at=utc_now(),
                    result_payload=result_payload,
                    error_code=error_code,
                    error_message=error_message,
                    retryable=retryable,
                ),
            )
        )


def lookup_operation_status(
    session,
    *,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    idempotency_key: str,
    timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> OperationSnapshot:
    """Read only; expired records and other actors are indistinguishable."""

    now = now or utc_now()
    attempt = session.scalar(
        select_in_workspace(
            MCPOperationAttempt,
            workspace_id=workspace_id,
            conditions=(
                MCPOperationAttempt.actor_kind == actor_kind,
                MCPOperationAttempt.actor_id == actor_id,
                MCPOperationAttempt.idempotency_key == idempotency_key,
                MCPOperationAttempt.expires_at > now,
            ),
        )
        .order_by(MCPOperationAttempt.started_at.desc(), MCPOperationAttempt.id.desc())
        .limit(1)
    )
    receipt = session.scalar(
        select_in_workspace(
            MCPWriteIdempotency,
            workspace_id=workspace_id,
            conditions=(
                MCPWriteIdempotency.actor_kind == actor_kind,
                MCPWriteIdempotency.actor_id == actor_id,
                MCPWriteIdempotency.idempotency_key == idempotency_key,
                MCPWriteIdempotency.expires_at > now,
            ),
        )
        .order_by(MCPWriteIdempotency.created_at.desc(), MCPWriteIdempotency.id.desc())
        .limit(1)
    )
    # A key is one logical operation. If a caller violates this by using it
    # for multiple tools, show the latest tool, not an older tool's success.
    tool_name = attempt.tool_name if attempt is not None else None
    if receipt is not None and (
        attempt is None or receipt.created_at > attempt.started_at
    ):
        tool_name = receipt.tool_name
    if tool_name is not None and receipt is not None:
        command = OPERATION_COMMAND_NAMES.get(tool_name, tool_name)
        if receipt.tool_name != command:
            receipt = session.scalar(
                select_in_workspace(
                    MCPWriteIdempotency,
                    workspace_id=workspace_id,
                    conditions=(
                        MCPWriteIdempotency.actor_kind == actor_kind,
                        MCPWriteIdempotency.actor_id == actor_id,
                        MCPWriteIdempotency.idempotency_key == idempotency_key,
                        MCPWriteIdempotency.tool_name == command,
                        MCPWriteIdempotency.expires_at > now,
                    ),
                )
            )
        if receipt is not None:
            return OperationSnapshot(
                "committed",
                "use_result",
                tool_name=tool_name,
                result_payload=dict(receipt.result_payload),
                from_ledger=True,
                authorization_context=(
                    receipt.result_payload.get("_operation_authorization")
                    or (attempt.authorization_context if attempt is not None else None)
                ),
            )
    if attempt is None:
        return OperationSnapshot(
            "not_seen", "resend_same_request", guidance=NOT_SEEN_GUIDANCE
        )
    if attempt.error_code is not None:
        return OperationSnapshot(
            "failed",
            "resend_same_request" if attempt.retryable else "fix_request",
            tool_name=attempt.tool_name,
            authorization_context=attempt.authorization_context,
            error={
                "code": attempt.error_code,
                "message": attempt.error_message or "The operation failed.",
                "retryable": attempt.retryable,
            },
        )
    if attempt.completed_at is not None and attempt.result_payload is not None:
        return OperationSnapshot(
            "committed",
            "use_result",
            tool_name=attempt.tool_name,
            result_payload=dict(attempt.result_payload),
            authorization_context=attempt.authorization_context,
        )
    if now - attempt.started_at >= timedelta(seconds=timeout_seconds):
        return OperationSnapshot(
            "failed",
            "resend_same_request",
            tool_name=attempt.tool_name,
            authorization_context=attempt.authorization_context,
            error={
                "code": "operation_interrupted",
                "message": INTERRUPTED_MESSAGE,
                "retryable": True,
            },
        )
    return OperationSnapshot(
        "in_progress",
        "wait_and_check",
        tool_name=attempt.tool_name,
        authorization_context=attempt.authorization_context,
        retry_after_seconds=2,
    )
