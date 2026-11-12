"""Recover a keyed operation's result without resending its authored content."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.app.container import ToolCallContext
from backend.mcp.context_tools import _begin_edit_result, _edit_result
from backend.mcp.envelope import error_envelope, ok_envelope, validation_error_envelope
from backend.mcp.protocol.wire_defaults import omit_declared_defaults
from backend.schemas.agent import AgentPageDraftResult
from backend.schemas.context_write import CiteResult
from backend.schemas.mcp.context_tools import CiteResult as MCPCiteResult
from backend.schemas.mcp.operation_tools import (
    GetOperationStatusInput,
    GetOperationStatusOutput,
    OperationStatusResult,
)
from backend.schemas.mcp.review_tools import SubmitRevisionForReviewResult
from backend.schemas.page import DiscardContextEditResult, PagePayload
from backend.services.mcp_operation_attempt import lookup_operation_status
from backend.services.mcp_write_idempotency import restore_ledger_result


@dataclass(frozen=True)
class KeyedTool:
    """Where a keyed tool's receipt is stored and how its result is served."""

    command: str
    result_model: type[BaseModel]
    serve: Callable[[Any], BaseModel] | None = None


def _mcp_cite_result(result: CiteResult) -> MCPCiteResult:
    return MCPCiteResult.model_validate(result.model_dump())


# The one registry of keyed MCP writes. Arrival recording, receipt lookup and
# result recovery all read it, so a new keyed tool is registered here once.
KEYED_TOOLS: dict[str, KeyedTool] = {
    "create_context_page": KeyedTool("create_context_page", PagePayload, _edit_result),
    "begin_context_edit": KeyedTool(
        "begin_context_edit", PagePayload, _begin_edit_result
    ),
    "update_context_edit": KeyedTool("autosave_page", PagePayload, _edit_result),
    "patch_context_edit": KeyedTool("patch_context_edit", PagePayload, _edit_result),
    "submit_context_edit_for_review": KeyedTool(
        "submit_context_edit_for_review", PagePayload, _edit_result
    ),
    "discard_context_edit": KeyedTool(
        "discard_context_edit", DiscardContextEditResult
    ),
    "cite": KeyedTool("cite", CiteResult, _mcp_cite_result),
    "write_agent_page_draft": KeyedTool(
        "write_agent_page_draft", AgentPageDraftResult
    ),
    "submit_revision_for_review": KeyedTool(
        "submit_revision_for_review", SubmitRevisionForReviewResult
    ),
}
KEYED_TOOL_COMMANDS = {name: tool.command for name, tool in KEYED_TOOLS.items()}


def keyed_result(tool_name: str, stored: dict[str, Any]) -> dict[str, Any]:
    """The stored result as a same-key resend would serve it."""

    tool = KEYED_TOOLS[tool_name]
    result = restore_ledger_result(stored, tool.result_model)
    if tool.serve is not None:
        result = tool.serve(result)
    return omit_declared_defaults(type(result), result.model_dump(mode="json"))


def get_operation_status(
    payload: dict, ctx: ToolCallContext
) -> GetOperationStatusOutput:
    actor = ctx.actor
    try:
        request = GetOperationStatusInput.model_validate(payload)
    except ValidationError as exc:
        return GetOperationStatusOutput(
            envelope=validation_error_envelope(
                actor,
                exc,
                allowed_fields=GetOperationStatusInput.model_fields,
            )
        )
    settings = getattr(ctx.services, "settings", None)
    timeout = getattr(settings, "mcp_operation_timeout_seconds", 120)
    with ctx.session_factory() as session:
        snapshot = lookup_operation_status(
            session,
            workspace_id=ctx.workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            idempotency_key=request.idempotency_key,
            commands=KEYED_TOOL_COMMANDS,
            timeout_seconds=timeout,
        )
    if snapshot.tool_name is not None:
        # The key is not a capability: the recorded tool's own scope and role
        # still apply, exactly as they would to a resend.
        from backend.mcp.router import PolicyRejection, resolve_target_actor

        original_actor = resolve_target_actor(
            tool_name=snapshot.tool_name,
            payload={"workspace_id": str(ctx.workspace_id)},
            actor=actor,
            session_factory=ctx.db.control_session_factory(),
            db=ctx.db,
            directory=ctx.services.workspace_directory,
        )
        if isinstance(original_actor, PolicyRejection):
            return GetOperationStatusOutput(
                envelope=error_envelope(actor, original_actor.error_code)
            )
    data = OperationStatusResult(
        status=snapshot.status,
        next_action=snapshot.next_action,
        result=(
            keyed_result(snapshot.tool_name, snapshot.receipt_payload)
            if snapshot.receipt_payload is not None
            else None
        ),
        error=snapshot.error,
        guidance=snapshot.guidance,
        retry_after_seconds=snapshot.retry_after_seconds,
    )
    return GetOperationStatusOutput(envelope=ok_envelope(actor), data=data)
