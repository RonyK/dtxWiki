"""MCP review-surface tool handlers (item 11-c / T15b).

The AI-facing half of review collaboration: an AI consumes the change-requests
routed to its effective principal P (its conversation owner) and resubmits a
revised draft. There is intentionally NO approve/reject/request_changes tool —
an AI can never adjudicate (Risk 6, structural).
"""

from uuid import UUID

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
    SubmitRevisionForReviewResult,
)
from backend.services.mcp_chat_service import (
    ensure_chat_session,
    resolve_effective_principal,
)
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.mcp_context_projection import page_links
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.read_errors import ReadNotFoundError, ReadServiceError
from backend.services.review_service import _revision_content_hash


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
            content_hash = _revision_content_hash(request.update, request.create)
            authorization = {
                "principal_id": str(principal),
                "review_item_id": str(request.review_item_id),
                "content_hash": content_hash,
            }

            def authorize_replay(stored):
                prior_authorization = stored.get("_operation_authorization", {})
                if prior_authorization.get("principal_id") != str(principal):
                    raise ReadNotFoundError("review item not found")
                ctx.services.review.authorize_revision_submission(
                    session,
                    workspace_id=ctx.workspace_id,
                    review_item_id=UUID(prior_authorization["review_item_id"]),
                    principal=principal,
                    principal_role=principal_role,
                    content_hash=prior_authorization.get("content_hash"),
                )

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

            def execute_revision():
                result = ctx.services.review.submit_revision_for_review(
                    session=session,
                    workspace_id=ctx.workspace_id,
                    review_item_id=request.review_item_id,
                    principal=principal,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_provenance=actor.provenance,
                    principal_role=principal_role,
                    context_write_service=ctx.services.context_write.with_actor(actor),
                    update=request.update,
                    create=request.create,
                    note=request.note,
                    origin_chat_session_id=origin_chat_session_id,
                )
                result._operation_authorization = authorization
                return result

            data = execute_idempotent_write(
                session,
                workspace_id=ctx.workspace_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                tool_name="submit_revision_for_review",
                idempotency_key=request.idempotency_key,
                fingerprint=request_fingerprint(
                    request.model_dump(
                        mode="json", exclude={"idempotency_key", "conversation_token"}
                    )
                    | {"principal": str(principal)}
                ),
                result_model=SubmitRevisionForReviewResult,
                execute=execute_revision,
                replay_authorizer=authorize_replay,
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


def authorize_revision_operation(ctx: ToolCallContext, authorization: dict) -> bool:
    """Keep a shared MCP actor's revision outcomes scoped to their principal."""
    with ctx.session_factory() as session:
        principal = resolve_effective_principal(
            session,
            workspace_id=ctx.workspace_id,
            mcp_session_id=ctx.conversation_token,
            fallback_user_id=ctx.actor.user_id,
        )
    if principal is None or authorization.get("principal_id") != str(principal):
        return False
    try:
        review_item_id = UUID(authorization.get("review_item_id"))
    except (TypeError, ValueError, AttributeError):
        return False
    principal_role = _principal_role(
        ctx, workspace_id=ctx.workspace_id, principal=principal
    )
    with ctx.session_factory() as session:
        ctx.services.review.authorize_revision_submission(
            session,
            workspace_id=ctx.workspace_id,
            review_item_id=review_item_id,
            principal=principal,
            principal_role=principal_role,
            content_hash=authorization.get("content_hash"),
        )
    return True


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
