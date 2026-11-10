"""Recover a keyed operation's result without resending its authored content."""

from uuid import UUID

from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.mcp.context_tools import _begin_edit_result, _edit_result
from backend.mcp.envelope import error_envelope, ok_envelope, validation_error_envelope
from backend.mcp.protocol.wire_defaults import omit_declared_defaults
from backend.schemas.mcp.operation_tools import (
    GetOperationStatusInput,
    GetOperationStatusOutput,
    OperationStatusResult,
)
from backend.schemas.page import PagePayload
from backend.services.mcp_operation_attempt import (
    NOT_SEEN_GUIDANCE,
    lookup_operation_status,
)
from backend.services.mcp_write_idempotency import _stored_edit_receipt
from backend.services.read_errors import ReadServiceError


def _public_result(snapshot) -> dict:
    payload = dict(snapshot.result_payload or {})
    if not snapshot.from_ledger:
        return payload
    if snapshot.tool_name in {
        "begin_context_edit",
        "create_context_page",
        "update_context_edit",
        "autosave_page",
        "patch_context_edit",
        "submit_context_edit_for_review",
    }:
        receipt = payload.pop("_edit_receipt", None)
        overrides = payload.pop("_metadata_overrides", [])
        can_submit = payload.pop("_can_submit_for_review", False)
        placement_changed = payload.pop("_placement_changed", False)
        placement_parent = payload.pop("_placement_parent_node_id", None)
        page = PagePayload.model_validate(payload)
        page._edit_receipt = (
            _stored_edit_receipt(receipt) if receipt is not None else None
        )
        page._metadata_overrides = overrides
        page._can_submit_for_review = can_submit is True
        page._placement_changed = placement_changed
        page._placement_parent_node_id = (
            UUID(placement_parent) if placement_parent is not None else None
        )
        page._replayed = True
        if snapshot.tool_name == "begin_context_edit":
            # Use the declared begin result, including its public page_id
            # spelling, while preserving begin's full-body reading behavior.
            result = _begin_edit_result(page)
        else:
            result = _edit_result(page)
        return omit_declared_defaults(type(result), result.model_dump(mode="json"))
    # Agent, cite, discard and revision results already have public shapes.
    payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    if "replayed" in payload or snapshot.tool_name == "cite":
        payload["replayed"] = True
    return payload


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
            timeout_seconds=timeout,
        )
    original_tool = (
        "update_context_edit"
        if snapshot.tool_name == "autosave_page"
        else snapshot.tool_name
    )
    if original_tool is not None:
        # The status key is not a capability: apply the recorded tool's current
        # scope/role/grant gates before releasing its result or error.
        from backend.mcp.router import PolicyRejection, resolve_target_actor

        original_actor = resolve_target_actor(
            tool_name=original_tool,
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
    if original_tool == "submit_revision_for_review":
        from backend.mcp.review_tools import authorize_revision_operation

        try:
            authorized = authorize_revision_operation(
                ctx, snapshot.authorization_context or {}
            )
        except ReadServiceError as exc:
            return GetOperationStatusOutput(
                envelope=error_envelope(actor, exc.error_code)
            )
        if not authorized:
            return GetOperationStatusOutput(
                envelope=ok_envelope(actor),
                data=OperationStatusResult(
                    status="not_seen",
                    next_action="resend_same_request",
                    guidance=NOT_SEEN_GUIDANCE,
                ),
            )
    data = OperationStatusResult(
        status=snapshot.status,
        next_action=snapshot.next_action,
        result=_public_result(snapshot) if snapshot.status == "committed" else None,
        error=snapshot.error,
        guidance=snapshot.guidance,
        retry_after_seconds=snapshot.retry_after_seconds,
    )
    return GetOperationStatusOutput(envelope=ok_envelope(actor), data=data)
