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
    SubmitRevisionForReviewInput,
    SubmitRevisionForReviewOutput,
)
from backend.services.mcp_chat_service import (
    ensure_chat_session,
    resolve_effective_principal,
)
from backend.services.mcp_client_service import resolve_trace_mcp_client
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
    return ListRequestedChangesOutput(envelope=ok_envelope(actor), data=data)


def submit_revision_for_review(
    payload: dict, ctx: ToolCallContext
) -> SubmitRevisionForReviewOutput:
    actor = ctx.actor
    conversation_token = ctx.conversation_token
    try:
        request = SubmitRevisionForReviewInput.model_validate(payload)
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
        with session_factory.begin() as session:
            # The revised draft is stamped with THIS AI conversation's chat
            # session. Create the row up-front (Codex #10) so a first-write
            # revision still routes chat_owner reviews instead of stamping NULL.
            origin_chat_session_id = None
            if conversation_token:
                client = resolve_trace_mcp_client(
                    session, workspace_id=ctx.workspace_id, actor=actor
                )
                origin_chat_session_id = ensure_chat_session(
                    session,
                    workspace_id=ctx.workspace_id,
                    client_id=client.id,
                    mcp_session_id=conversation_token,
                    owner_user_id=actor.user_id,
                )
            data = ctx.services.review.submit_revision_for_review(
                session=session,
                workspace_id=ctx.workspace_id,
                review_item_id=request.review_item_id,
                principal=principal,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor_provenance=actor.provenance,
                principal_role=principal_role,
                context_write_service=ctx.services.context_write,
                update=request.update,
                create=request.create,
                note=request.note,
                origin_chat_session_id=origin_chat_session_id,
            )
    except ValidationError as exc:
        return SubmitRevisionForReviewOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=SubmitRevisionForReviewInput.model_fields
            )
        )
    except ReadServiceError as exc:
        return SubmitRevisionForReviewOutput(
            envelope=error_envelope(actor, exc.error_code, message=str(exc) or None)
        )
    return SubmitRevisionForReviewOutput(envelope=ok_envelope(actor), data=data)


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
