"""MCP review-surface tool handlers (item 11-c / T15b).

The AI-facing half of review collaboration: an AI consumes the change-requests
routed to its effective principal P (its conversation owner) and resubmits a
revised draft. There is intentionally NO approve/reject/request_changes tool —
an AI can never adjudicate (Risk 6, structural).
"""

from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.mcp.envelope import (
    error_envelope,
    ok_envelope,
    validation_error_envelope,
)
from backend.policy.rbac import get_workspace_access
from backend.schemas.mcp.review_tools import (
    ListRequestedChangesInput,
    ListRequestedChangesOutput,
)
from backend.services.mcp_chat_service import (
    resolve_effective_principal,
)
from backend.services.mcp_context_projection import page_links
from backend.services.read_errors import ReadServiceError


def list_requested_changes(
    payload: dict, ctx: ToolCallContext
) -> ListRequestedChangesOutput:
    actor = ctx.actor
    conversation_token = ctx.conversation_token
    try:
        request = ListRequestedChangesInput.model_validate(payload)
        # Route the shard through the AUTHORIZED workspace (ctx.workspace_id,
        # bound by router.resolve_target_actor), not the caller-requested
        # one -- see upload_tools.py for the same pattern and its rationale.
        session_factory = ctx.session_factory
        with session_factory() as session:
            principal = resolve_effective_principal(
                session,
                workspace_id=ctx.workspace_id,
                mcp_session_id=conversation_token,
                fallback_user_id=actor.user_id,
            )
        principal_role = _principal_role(
            ctx,
            workspace_id=ctx.workspace_id,
            principal=principal,
        )
        with session_factory() as session:
            data = ctx.services.review.list_requested_changes_for_principal(
                session=session,
                workspace_id=ctx.workspace_id,
                principal=principal,
                principal_role=principal_role,
                limit=request.limit,
                cursor=request.cursor,
                actor=actor,
            )
    except ValidationError as exc:
        return ListRequestedChangesOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=ListRequestedChangesInput.model_fields
            )
        )
    except ReadServiceError as exc:
        return ListRequestedChangesOutput(
            envelope=error_envelope(actor, exc.error_code, message=str(exc) or None)
        )
    for item in data.items:
        target = item.target
        if target is None or target.target_type not in {
            "context_page",
            "context_page_version",
        }:
            continue
        # Version targets identify a version. Use the authorized review
        # evidence's document identity to produce a page link.
        document = next(
            (
                operation.document
                for operation in (item.evidence.operations if item.evidence else [])
                if operation.target_type == target.target_type
                and operation.target_id == target.target_id
                and operation.document is not None
                and operation.document.document_kind == "context"
                and operation.document.can_open
                and operation.document.document_id is not None
            ),
            None,
        )
        if document is None:
            continue
        links = page_links(
            ctx.workspace_id,
            document.document_id,
            document.title or target.title or "",
        )
        target.stable_url = links["stable_url"]
        target.link_markdown = links["link_markdown"]
    return ListRequestedChangesOutput(envelope=ok_envelope(actor), data=data)


def _principal_role(ctx: ToolCallContext, *, workspace_id, principal) -> str:
    if principal is None:
        return "viewer"
    control_factory = ctx.db.control_session_factory()
    with control_factory() as control_session:
        access = get_workspace_access(
            control_session,
            user_id=principal,
            workspace_id=workspace_id,
        )
    return access.role if access is not None else "viewer"
