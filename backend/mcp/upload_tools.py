from __future__ import annotations

from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.context_media.domain.policy import CONTEXT_MEDIA_POLICY_REVISION
from backend.mcp.envelope import error_envelope, ok_envelope, validation_error_envelope
from backend.schemas.mcp.upload_tools import (
    UploadImageInput,
    UploadImageResult,
    UploadImageToolOutput,
    decode_image_base64,
)
from backend.services.context_image_ingest import (
    ContextImageIngestCommand,
    ContextImageIngestError,
)
from backend.services.context_media_access import (
    ContextMediaAccessError,
    ContextMediaAccessPolicy,
)


def upload_image(payload: dict, ctx: ToolCallContext) -> UploadImageToolOutput:
    actor = ctx.actor
    try:
        # The runner consumes the transport field before dispatch. Reinsert the
        # already-validated conversation token so the public schema can keep it
        # required without handing transport state to the domain service.
        request = UploadImageInput.model_validate(
            {**payload, "conversation_token": ctx.conversation_token}
        )
        # Context media is workspace-owned data, so the write must go to the plane
        # this call was AUTHORIZED for -- ctx.workspace_id, which
        # backend.mcp.router binds from resolve_target_actor's
        # require_workspace_role check, not request.workspace_id, which is
        # merely what the caller asked for. ctx.session_factory
        # (backend/spi/context.py) already resolves to
        # ctx.db.session_for(ctx.workspace_id) once router.py binds it for
        # the 'editor' role this tool requires, so use it directly instead of
        # re-deriving from the request. The router authorizes workspace_id;
        # the scoped preflight below checks page ownership and archived state
        # before ingest can write into that workspace plane.
        session_factory = ctx.session_factory
        ContextMediaAccessPolicy(
            ctx.db, ctx.services.workspace_directory
        ).assert_page_not_archived(
            request.page_id, expected_workspace_id=ctx.workspace_id
        )
        result = ctx.services.context_image_ingest.ingest(
            session_factory=session_factory,
            command=ContextImageIngestCommand(
                workspace_id=ctx.workspace_id,
                marker_uuid=request.marker_uuid,
                filename=request.filename,
                declared_media_type=request.mime_type,
                content=decode_image_base64(request.content_base64),
                page_id=request.page_id,
                editing_version_id=request.editing_version_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                policy_revision=CONTEXT_MEDIA_POLICY_REVISION,
            ),
        )
    except ValidationError as exc:
        return UploadImageToolOutput(
            envelope=validation_error_envelope(
                actor,
                exc,
                allowed_fields=UploadImageInput.model_fields,
            )
        )
    except ContextMediaAccessError as exc:
        return UploadImageToolOutput(
            envelope=error_envelope(
                actor,
                exc.error_code,
                message=str(exc),
            )
        )
    except ContextImageIngestError as exc:
        return UploadImageToolOutput(
            envelope=error_envelope(
                actor,
                exc.error_code,
                message=str(exc),
            )
        )
    except ValueError as exc:
        return UploadImageToolOutput(
            envelope=error_envelope(
                actor,
                "validation_failed",
                message=str(exc),
                retryable=False,
            )
        )

    return UploadImageToolOutput(
        envelope=ok_envelope(actor),
        data=UploadImageResult(
            asset_id=result.asset_id,
            logical_ref=result.logical_ref,
            content_url=result.content_url,
            markdown=(f"![{_escape_alt_text(request.alt_text)}]({result.logical_ref})"),
            mime_type=result.canonical_media_type,
            bytes=result.byte_size,
            sha256=result.sha256,
            marker_uuid=request.marker_uuid,
            reused=result.reused,
        ),
    )


def _escape_alt_text(value: str | None) -> str:
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )
