from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.mcp.envelope import (
    error_envelope,
    ok_envelope,
    validation_error_envelope,
)
from backend.schemas.agent import (
    AgentPageDraftRequest,
    AgentPageDraftResult,
    BuildBundleRequest,
)
from backend.schemas.mcp.agent_tools import (
    BuildAgentInputBundleInput,
    BuildAgentInputBundleToolOutput,
    WriteAgentPageDraftInput,
    WriteAgentPageDraftToolOutput,
)
from backend.services.mcp_chat_service import ensure_chat_session
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.read_errors import ReadServiceError


def write_agent_page_draft(
    payload: dict, ctx: ToolCallContext
) -> WriteAgentPageDraftToolOutput:
    actor = ctx.actor
    session_factory = ctx.session_factory
    conversation_token = ctx.conversation_token
    try:
        request = WriteAgentPageDraftInput.model_validate(payload)
        draft_request = AgentPageDraftRequest.model_validate(
            request.model_dump(
                exclude={"workspace_id", "conversation_token", "idempotency_key"}
            )
        )
        with session_factory.begin() as session:
            # T15/F2 + Codex #10: chat_owner review routing for agent-page
            # drafts. Create the chat row up-front (not lookup-only) so the
            # FIRST write of a new conversation is resolvable, not NULL.
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
            data = execute_idempotent_write(
                session,
                workspace_id=ctx.workspace_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                tool_name="write_agent_page_draft",
                idempotency_key=request.idempotency_key,
                fingerprint=request_fingerprint(
                    request.model_dump(
                        mode="json",
                        exclude={"conversation_token", "idempotency_key"},
                    )
                ),
                result_model=AgentPageDraftResult,
                execute=lambda: ctx.services.agent_page.write_draft(
                    session=session,
                    workspace_id=ctx.workspace_id,
                    request=draft_request,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_provenance=actor.provenance,
                    origin_chat_session_id=origin_chat_session_id,
                ),
            )
    except ValidationError as exc:
        return WriteAgentPageDraftToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=WriteAgentPageDraftInput.model_fields
            )
        )
    except ReadServiceError as exc:
        return WriteAgentPageDraftToolOutput(
            envelope=error_envelope(actor, exc.error_code, message=str(exc) or None)
        )
    return WriteAgentPageDraftToolOutput(envelope=ok_envelope(actor), data=data)


def build_agent_input_bundle(
    payload: dict, ctx: ToolCallContext
) -> BuildAgentInputBundleToolOutput:
    actor = ctx.actor
    session_factory = ctx.session_factory
    try:
        request = BuildAgentInputBundleInput.model_validate(payload)
        bundle_request = BuildBundleRequest.model_validate(
            request.model_dump(exclude={"workspace_id", "conversation_token"})
        )
        with session_factory.begin() as session:
            data = ctx.services.bundle.build(
                session=session,
                workspace_id=ctx.workspace_id,
                request=bundle_request,
                actor=actor,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
            )
            bundle = ctx.services.bundle.mark_used(
                session=session,
                workspace_id=ctx.workspace_id,
                bundle_id=data.bundle_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
            )
            data = data.model_copy(update={"status": bundle.status})
    except ValidationError as exc:
        return BuildAgentInputBundleToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=BuildAgentInputBundleInput.model_fields
            )
        )
    except ReadServiceError as exc:
        return BuildAgentInputBundleToolOutput(
            envelope=error_envelope(actor, exc.error_code, message=str(exc) or None)
        )
    return BuildAgentInputBundleToolOutput(envelope=ok_envelope(actor), data=data)
