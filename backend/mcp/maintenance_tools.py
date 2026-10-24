from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.app.settings import get_settings
from backend.mcp.envelope import (
    apply_payload_warnings,
    error_envelope,
    ok_envelope,
    validation_error_envelope,
)
from backend.mcp.maintenance_wire import select_adapter
from backend.schemas.mcp.maintenance_tools import (
    WikiMaintenanceInput,
    WikiMaintenanceOutput,
)
from backend.services.client_round_state import (
    MAINTENANCE_STATE_KIND,
    ClientRoundStateCodec,
    ClientRoundStateError,
)
from backend.services.maintenance_rounds import lease_round, submit_answers


def wiki_maintenance(
    payload: dict,
    ctx: ToolCallContext,
) -> WikiMaintenanceOutput:
    actor = ctx.actor
    try:
        request = WikiMaintenanceInput.model_validate(payload)
    except ValidationError as exc:
        return WikiMaintenanceOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=WikiMaintenanceInput.model_fields
            )
        )
    settings = getattr(ctx.services, "settings", None) or get_settings()
    adapter, warning_codes = select_adapter(settings, request.wire)
    codec = ClientRoundStateCodec(
        secret=settings.effective_session_secret,
        kind=MAINTENANCE_STATE_KIND,
    )
    # Route the shard through the AUTHORIZED workspace (ctx.workspace_id,
    # bound by router.resolve_target_actor's require_workspace_role check),
    # not the caller-requested one -- see upload_tools.py for the same
    # pattern and its rationale. ctx.session_factory already resolves to
    # ctx.db.session_for(ctx.workspace_id).
    session_factory = ctx.session_factory
    try:
        with session_factory.begin() as session:
            if request.resume_token is None:
                round_ = lease_round(
                    session,
                    workspace_id=ctx.workspace_id,
                    codec=codec,
                    budget=request.budget,
                    actor=actor,
                )
                rendered = adapter.render_round(round_)
            else:
                outcome = submit_answers(
                    session,
                    workspace_id=ctx.workspace_id,
                    resume_token=request.resume_token,
                    answers=request.input_responses or {},
                    codec=codec,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_provenance=actor.provenance,
                    context_write_service=ctx.services.context_write,
                )
                rendered = {
                    "wire_contract": adapter.wire_contract,
                    "staged": list(outcome.staged),
                    "rejected": [
                        {"task_id": str(task_id), "reason": reason}
                        for task_id, reason in outcome.rejected
                    ],
                    "remaining_pending": outcome.remaining_pending,
                }
    except ClientRoundStateError as exc:
        return WikiMaintenanceOutput(
            envelope=error_envelope(actor, exc.code, message=str(exc) or None),
            wire_contract=adapter.wire_contract,
        )
    envelope = apply_payload_warnings(ok_envelope(actor), warning_codes)
    return WikiMaintenanceOutput(envelope=envelope, **rendered)
