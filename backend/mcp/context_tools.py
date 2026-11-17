import logging
from uuid import UUID

from pydantic import ValidationError

from backend.app.container import ToolCallContext
from backend.mcp.envelope import (
    apply_payload_warnings,
    error_envelope,
    ok_envelope,
    validation_error_envelope,
)
from backend.schemas.common import MCPErrorDetail
from backend.schemas.context_write import (
    CiteRequest,
    SubmitFeedbackRequest,
)
from backend.schemas.mcp.context_tools import (
    BeginContextEditInput,
    BeginContextEditResult,
    BeginContextEditToolOutput,
    BringInContextVersionInput,
    BringInContextVersionToolOutput,
    CiteInput,
    CiteResult,
    CiteToolOutput,
    ContextEditResult,
    CreateContextPageInput,
    CreateContextPageToolOutput,
    DiscardContextEditInput,
    DiscardContextEditToolOutput,
    ListMyContextEditsInput,
    ListMyContextEditsToolOutput,
    PatchContextEditInput,
    PatchContextEditToolOutput,
    PreviewContextMergeInput,
    PreviewContextMergeToolOutput,
    PublishContextEditInput,
    PublishContextEditToolOutput,
    SubmitContextEditForReviewInput,
    SubmitContextEditForReviewToolOutput,
    SubmitFeedbackInput,
    SubmitFeedbackToolOutput,
    UpdateContextEditInput,
    UpdateContextEditToolOutput,
)
from backend.schemas.page import PagePayload
from backend.services.context_link_advisory import (
    CONTEXT_PAGE_HAS_NO_GRAPH_VISIBLE_LINKS,
    context_link_advisory_state,
)
from backend.services.mcp_chat_service import ensure_chat_session
from backend.services.mcp_client_service import resolve_trace_mcp_client
from backend.services.page_service import (
    PageConflictError,
    PageNotFoundError,
    PageService,
    PageServiceError,
)
from backend.services.read_errors import ReadServiceError

logger = logging.getLogger(__name__)


def cite(payload: dict, ctx: ToolCallContext) -> CiteToolOutput:
    """Register the selected documents; discovery belongs to search/read."""
    actor = ctx.actor
    try:
        request = CiteInput.model_validate(payload)
        cite_request = CiteRequest.model_validate(
            request.model_dump(exclude={"workspace_id", "conversation_token"})
        )
        with ctx.session_factory.begin() as session:
            data = ctx.services.citation.cite(
                session=session,
                workspace_id=ctx.workspace_id,
                request=cite_request,
                actor=actor,
            )
    except ValidationError as exc:
        return CiteToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=CiteInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return CiteToolOutput(envelope=_service_error_envelope(actor, exc))
    # The service returns the shared REST result. Project the narrower MCP
    # reference-metadata response explicitly.
    return CiteToolOutput(
        envelope=ok_envelope(actor),
        data=CiteResult.model_validate(data.model_dump()),
    )


def submit_feedback(payload: dict, ctx: ToolCallContext) -> SubmitFeedbackToolOutput:
    actor = ctx.actor
    session_factory = ctx.session_factory
    try:
        request = SubmitFeedbackInput.model_validate(payload)
        feedback_request = SubmitFeedbackRequest.model_validate(
            request.model_dump(exclude={"workspace_id", "conversation_token"})
        )
        with session_factory.begin() as session:
            data = ctx.services.feedback.submit_feedback(
                session=session,
                workspace_id=ctx.workspace_id,
                request=feedback_request,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
            )
    except ValidationError as exc:
        return SubmitFeedbackToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=SubmitFeedbackInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return SubmitFeedbackToolOutput(envelope=_service_error_envelope(actor, exc))
    return SubmitFeedbackToolOutput(envelope=ok_envelope(actor), data=data)


# P8.6: the clean Context-editing-state-machine tool surface. Each handler is
# a thin wrapper over the matching PageService command that Browser REST
# (backend/api/context_pages.py) already calls -- there is exactly one
# implementation of each command, shared by both surfaces. A fresh
# `PageService` is built per call against `ctx.session_factory` (routed
# through `ctx.workspace_id`, the AUTHORIZED workspace resolved by
# router.dispatch -- never a caller-claimed one), matching
# `context_pages.py`'s own `_workspace_page_service` helper, rather than
# reusing the container-wide `ctx.page_service` (bound to
# `control_session_factory()`): today's single-database topology makes the
# two equivalent, but only the per-call construction stays correct once a
# sharding overlay gives `session_for(workspace_id)` its own physical
# database (DB D-2/backend/app/container.py's `DatabaseProvider` docstring).
def _context_edit_page_service(ctx: ToolCallContext) -> PageService:
    bindings = getattr(ctx.session_factory, "kw", {}).get("info", {})
    writer = (
        bindings.get("context_version_writer") or ctx.services.context_version_writer
    )
    return PageService(
        ctx.session_factory,
        context_version_writer=writer,
        actor_context=ctx.actor,
        job_queue=ctx.services.job_queue,
        body_preparer=ctx.services.body_preparer,
        lint_registry=ctx.services.lint_registry,
        context_media_repository_factory=ctx.services.context_media_repository_factory,
    )


def _chat_session_id(ctx: ToolCallContext) -> UUID | None:
    """Resolve the conversation recorded in draft and review audit evidence."""

    if not ctx.conversation_token:
        return None
    with ctx.session_factory.begin() as session:
        client = resolve_trace_mcp_client(
            session, workspace_id=ctx.workspace_id, actor=ctx.actor
        )
        return ensure_chat_session(
            session,
            workspace_id=ctx.workspace_id,
            client_id=client.id,
            mcp_session_id=ctx.conversation_token,
            owner_user_id=ctx.actor.user_id,
        )


def _stale_base_message(exc: PageConflictError) -> str:
    if exc.no_open_edit:
        return "you have no open edit on this page any more"
    message = (
        f"server_version_id={exc.server_version_id} "
        f"server_content_hash={exc.server_content_hash}"
    )
    if exc.server_editing_revision is not None:
        message += f" server_editing_revision={exc.server_editing_revision}"
    return message


_REREAD_OPEN_EDIT = (
    "Re-read the same draft with read_structure_map or read_context_outline "
    "using its version_id, rebuild your change on the text you read, then send "
    "it with the version_id and editing_revision from that read. Do not "
    "resend the same body with only these values changed: whatever moved the "
    "edit may be work you have not seen."
)


def _stale_base_details(exc: PageConflictError) -> list[MCPErrorDetail]:
    """Name the readable shared draft and revision for a stale write."""

    if exc.no_open_edit:
        return [
            MCPErrorDetail(
                field="version_id",
                code=exc.error_code,
                message=("The named version is no longer an open draft."),
                fix=(
                    "Read content by page_id and version_id if needed. To edit, "
                    "call begin_context_edit with a new key."
                ),
            )
        ]
    if exc.server_editing_revision is None or exc.server_version_id is None:
        return []
    return [
        MCPErrorDetail(
            field="version_id",
            code=exc.error_code,
            message=(
                f"The shared draft on this page is version_id={exc.server_version_id}."
            ),
            fix=_REREAD_OPEN_EDIT,
        ),
        MCPErrorDetail(
            field="expected_editing_revision",
            code=exc.error_code,
            message=(
                "The shared draft is at "
                f"editing_revision={exc.server_editing_revision}."
            ),
            fix=_REREAD_OPEN_EDIT,
        ),
    ]


def _stale_base_envelope(actor, exc: PageConflictError):
    return error_envelope(
        actor,
        exc.error_code,
        "conflict",
        message=_stale_base_message(exc),
        details=_stale_base_details(exc),
    )


def _edit_result(data: PagePayload) -> ContextEditResult:
    """What an edit command saved, without repeating the body.

    The receipt instance is kept, not re-dumped, so its private Chats preview
    reaches the runner's trace capture.
    """

    result = ContextEditResult.model_validate(
        {
            **data.model_dump(include=set(ContextEditResult.model_fields)),
            "page_id": data.id,
            "replayed": data._replayed,
            "placement_parent_node_id": (
                data._placement_parent_node_id if data._placement_changed else None
            ),
            "edit_receipt": data._edit_receipt,
        }
    )
    object.__setattr__(result, "_metadata_overrides", data._metadata_overrides)
    return result


def _begin_edit_result(data: PagePayload) -> BeginContextEditResult:
    result = BeginContextEditResult.model_validate(
        {
            **data.model_dump(
                include=set(BeginContextEditResult.model_fields) - {"id"}
            ),
            "page_id": data.id,
            "replayed": data._replayed,
        }
    )
    result._metadata_overrides = data._metadata_overrides
    return result


def _edit_success_envelope(actor, data):
    envelope = ok_envelope(actor)
    overrides = getattr(data, "_metadata_overrides", ())
    if overrides:
        import json

        from backend.schemas.common import EnvelopeWarning

        envelope.warnings.append(
            EnvelopeWarning(
                code="metadata_frontmatter_overridden",
                message="Explicit metadata overrides frontmatter: "
                + json.dumps(
                    list(overrides), ensure_ascii=False, separators=(",", ":")
                ),
            )
        )
    return envelope


def _service_error_envelope(actor, exc: PageServiceError | ReadServiceError):
    """The one failure mapping of every tool here.

    The error code decides status, retryability and guidance (envelope.py);
    a refusal keeps the facts it found -- operation, place, occurrences --
    because dropping them forced a re-read.
    """

    if isinstance(exc, PageConflictError):
        return _stale_base_envelope(actor, exc)
    envelope = error_envelope(
        actor,
        exc.error_code,
        message=str(exc) or None,
        details=list(getattr(exc, "details", []) or []),
    )
    envelope.warnings = list(getattr(exc, "warnings", []) or [])
    return envelope


def create_context_page(
    payload: dict, ctx: ToolCallContext
) -> CreateContextPageToolOutput:
    actor = ctx.actor
    try:
        request = CreateContextPageInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        with page_service.session_factory() as session:
            workspace_slug = ctx.services.workspace_directory.slug_by_workspace_id(
                session, ctx.workspace_id
            )
        if workspace_slug is None:
            raise PageNotFoundError("workspace not found")
        data = page_service.create_page_first(
            workspace_slug=workspace_slug,
            title=request.title,
            body_md=request.body_md,
            page_type=request.page_type,
            tags=request.tags,
            aliases=request.aliases,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            placement_parent_node_id=request.placement_parent_node_id,
            idempotency_key=request.idempotency_key,
            with_edit_receipt=True,
        )
        data = _edit_result(data)
    except ValidationError as exc:
        return CreateContextPageToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=CreateContextPageInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return CreateContextPageToolOutput(envelope=_service_error_envelope(actor, exc))
    return CreateContextPageToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def begin_context_edit(
    payload: dict, ctx: ToolCallContext
) -> BeginContextEditToolOutput:
    actor = ctx.actor
    try:
        request = BeginContextEditInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.begin_context_edit(
            page_id=request.page_id,
            workspace_id=ctx.workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            idempotency_key=request.idempotency_key,
            from_version_id=request.from_version_id,
            on_conflict=request.on_conflict,
            base_version_id=request.base_version_id,
            base_content_hash=request.base_content_hash,
            chat_session_id=_chat_session_id(ctx),
        )
    except ValidationError as exc:
        return BeginContextEditToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=BeginContextEditInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return BeginContextEditToolOutput(envelope=_service_error_envelope(actor, exc))
    data = _begin_edit_result(data)
    return BeginContextEditToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def update_context_edit(
    payload: dict, ctx: ToolCallContext
) -> UpdateContextEditToolOutput:
    actor = ctx.actor
    try:
        request = UpdateContextEditInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.autosave_page(
            page_id=request.page_id,
            workspace_id=ctx.workspace_id,
            title=request.title,
            body_md=request.body_md,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            base_version_id=None,
            base_content_hash=None,
            actor_provenance=actor.provenance,
            idempotency_key=request.idempotency_key,
            expected_editing_revision=request.expected_editing_revision,
            page_type=request.page_type,
            tags=request.tags,
            aliases=request.aliases,
            destination_node_id=request.placement_parent_node_id,
            with_edit_receipt=True,
            version_id=request.version_id,
        )
        data = _edit_result(data)
    except ValidationError as exc:
        return UpdateContextEditToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=UpdateContextEditInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return UpdateContextEditToolOutput(envelope=_service_error_envelope(actor, exc))
    return UpdateContextEditToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def submit_context_edit_for_review(
    payload: dict, ctx: ToolCallContext
) -> SubmitContextEditForReviewToolOutput:
    actor = ctx.actor
    try:
        request = SubmitContextEditForReviewInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.submit_context_edit_for_review(
            page_id=request.page_id,
            version_id=request.version_id,
            workspace_id=ctx.workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            expected_editing_revision=request.expected_editing_revision,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            chat_session_id=_chat_session_id(ctx),
            feedback_id=request.feedback_id,
        )
        data = _edit_result(data)
    except ValidationError as exc:
        return SubmitContextEditForReviewToolOutput(
            envelope=validation_error_envelope(
                actor,
                exc,
                allowed_fields=SubmitContextEditForReviewInput.model_fields,
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return SubmitContextEditForReviewToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    envelope = _edit_success_envelope(actor, data)
    try:
        with ctx.session_factory() as session:
            advisory = context_link_advisory_state(
                session,
                workspace_id=ctx.workspace_id,
                page_id=data.id,
                version_id=data.version_id,
            )
    except Exception:
        # The submit transaction has already committed. This read is advisory,
        # so an unavailable shard or a malformed legacy row must not turn a
        # successful, idempotent submission into an apparent write failure.
        logger.exception(
            "Context link advisory lookup failed",
            extra={
                "workspace_id": str(ctx.workspace_id),
                "page_id": str(data.id),
                "version_id": str(data.version_id),
            },
        )
    else:
        if advisory.should_warn:
            envelope = apply_payload_warnings(
                envelope,
                [CONTEXT_PAGE_HAS_NO_GRAPH_VISIBLE_LINKS],
            )
    return SubmitContextEditForReviewToolOutput(envelope=envelope, data=data)


def discard_context_edit(
    payload: dict, ctx: ToolCallContext
) -> DiscardContextEditToolOutput:
    actor = ctx.actor
    try:
        request = DiscardContextEditInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.discard_context_edit(
            page_id=request.page_id,
            version_id=request.version_id,
            workspace_id=ctx.workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            idempotency_key=request.idempotency_key,
        )
    except ValidationError as exc:
        return DiscardContextEditToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=DiscardContextEditInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return DiscardContextEditToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    return DiscardContextEditToolOutput(envelope=ok_envelope(actor), data=data)


def list_my_context_edits(
    payload: dict, ctx: ToolCallContext
) -> ListMyContextEditsToolOutput:
    actor = ctx.actor
    try:
        request = ListMyContextEditsInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.list_my_editing_versions(
            workspace_id=ctx.workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            limit=request.limit,
            cursor=request.cursor,
        )
    except ValidationError as exc:
        return ListMyContextEditsToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=ListMyContextEditsInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return ListMyContextEditsToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    return ListMyContextEditsToolOutput(envelope=ok_envelope(actor), data=data)


def patch_context_edit(
    payload: dict, ctx: ToolCallContext
) -> PatchContextEditToolOutput:
    actor = ctx.actor
    try:
        request = PatchContextEditInput.model_validate(payload)
        page_service = _context_edit_page_service(ctx)
        data = page_service.patch_context_edit(
            page_id=request.page_id,
            version_id=request.version_id,
            workspace_id=ctx.workspace_id,
            operations=request.operations,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            expected_editing_revision=request.expected_editing_revision,
            idempotency_key=request.idempotency_key,
            chat_session_id=_chat_session_id(ctx),
        )
        data = _edit_result(data)
    except ValidationError as exc:
        return PatchContextEditToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=PatchContextEditInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return PatchContextEditToolOutput(envelope=_service_error_envelope(actor, exc))
    return PatchContextEditToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def preview_context_merge(
    payload: dict, ctx: ToolCallContext
) -> PreviewContextMergeToolOutput:
    actor = ctx.actor
    try:
        request = PreviewContextMergeInput.model_validate(payload)
        data = _context_edit_page_service(ctx).preview_context_merge(
            page_id=request.page_id,
            version_id=request.version_id,
            workspace_id=ctx.workspace_id,
            on_conflict=request.on_conflict,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            body_md=request.body_md,
            page_metadata=_metadata_patch(request.page_metadata),
        )
    except ValidationError as exc:
        return PreviewContextMergeToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=PreviewContextMergeInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return PreviewContextMergeToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    # Without a conflict both choices equal the result; send them only when
    # there is something to choose between.
    if data["kind"] != "conflict":
        data["keep_both"] = data["latest"] = None
    data = {"page_id": request.page_id, **data}
    return PreviewContextMergeToolOutput(envelope=ok_envelope(actor), data=data)


def bring_in_context_version(
    payload: dict, ctx: ToolCallContext
) -> BringInContextVersionToolOutput:
    actor = ctx.actor
    try:
        request = BringInContextVersionInput.model_validate(payload)
        data = _edit_result(
            _context_edit_page_service(ctx).bring_in_context_version(
                page_id=request.page_id,
                version_id=request.version_id,
                workspace_id=ctx.workspace_id,
                expected_editing_revision=request.expected_editing_revision,
                idempotency_key=request.idempotency_key,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor_provenance=actor.provenance,
                commit_version_id=request.commit_version_id,
                on_conflict=request.on_conflict,
            )
        )
    except ValidationError as exc:
        return BringInContextVersionToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=BringInContextVersionInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return BringInContextVersionToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    return BringInContextVersionToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def publish_context_edit(
    payload: dict, ctx: ToolCallContext
) -> PublishContextEditToolOutput:
    actor = ctx.actor
    try:
        request = PublishContextEditInput.model_validate(payload)
        result = _context_edit_page_service(ctx).publish_draft(
            page_id=request.page_id,
            version_id=request.version_id,
            workspace_id=ctx.workspace_id,
            reason=request.reason,
            publish_despite_findings=False,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            expected_editing_revision=request.expected_editing_revision,
            expected_published_version_id=request.expected_published_version_id,
            result_hash=request.result_hash,
            on_conflict=request.on_conflict,
            result_body_md=request.result_body_md,
            result_page_metadata=_metadata_patch(request.result_page_metadata),
            idempotency_key=request.idempotency_key,
        )
        data = _edit_result(result.page)
    except ValidationError as exc:
        return PublishContextEditToolOutput(
            envelope=validation_error_envelope(
                actor, exc, allowed_fields=PublishContextEditInput.model_fields
            )
        )
    except (PageServiceError, ReadServiceError) as exc:
        return PublishContextEditToolOutput(
            envelope=_service_error_envelope(actor, exc)
        )
    return PublishContextEditToolOutput(
        envelope=_edit_success_envelope(actor, data), data=data
    )


def _metadata_patch(patch) -> dict | None:
    # The merge service reads None as "unchanged" for every field.
    return patch.model_dump(exclude_none=True) if patch is not None else None
