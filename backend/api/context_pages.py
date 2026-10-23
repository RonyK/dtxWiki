"""Canonical Context page route family (P9.1).

Replaces three prior surfaces:

* ``/api/v1/pages*`` (``backend/api/pages.py``, deleted) -- the P8
  state-machine implementation (``PageService``), which this router now
  fronts directly under a workspace-scoped URL.
* the intermediate ``/api/v1/workspaces/{workspace_id}/context-pages*``
  collection/detail/draft/publish routes (``backend/api/read.py``, deleted)
  -- these called the pre-P8 ``ContextWriteService.write_draft``/
  ``update_draft``/``publish_draft`` title-upsert path, which MCP's
  ``write_draft``/``update_draft`` tools still use (P8.6, out of scope here)
  but which never had P8's editing/discarded state machine, editing_revision
  CAS, or idempotency ledger. Browser REST now always uses the P8 commands.
* ``ContextPageDetail`` (``backend/schemas/read.py``), which stays exactly as
  it was for MCP's ``read``/``read_target`` tool -- this router uses the new,
  richer ``backend.schemas.page.ContextPageDetail`` instead (a superset of
  ``PagePayload``), so MCP's existing output contract is untouched.

Every route here is workspace_id-first: ``page_id`` (and ``asset_id`` where
relevant) is independently verified to belong to the URL's ``workspace_id``
via ``backend.services.workspace_directory`` before anything else runs, and a
mismatch reads as a plain 404 -- the same "existence before role" contract
``backend.services.context_media_access.ContextMediaAccessPolicy`` already
documents. This check is NOT optional: unlike the old ``/pages/{page_id}``
routes (where ``workspace_id`` was only ever derived FROM ``page_id``, never
an independent caller input), a caller here supplies both, so nothing else in
``PageService`` guards against a caller naming a workspace they belong to
alongside a ``page_id`` that actually belongs to a different workspace.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from backend.api.deps import RestActorDep, read_service_error_response
from backend.app.errors import build_rest_error_response
from backend.app.state import get_container_from_app
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    rebind_actor_to_workspace_access,
    require_workspace_role,
)
from backend.schemas.common import (
    EnvelopeWarning,
    RESTConflictErrorBody,
    RESTErrorBody,
    RESTResultEnvelope,
)
from backend.schemas.context_reconciliation import (
    ContextReconciliationDetailResponse,
    ContextReconciliationDiscardRequest,
    ContextReconciliationRequest,
    ContextReconciliationResultResponse,
)
from backend.schemas.page import (
    PAGE_LIST_CURSOR_MAX_LENGTH,
    PAGE_LIST_CURSOR_PATTERN,
    PAGE_LIST_LIMIT_MAX,
    PAGE_SLUG_PATTERN,
    BeginContextEditRequest,
    ContextPageCreateRequest,
    ContextPageDetail,
    ContextPageDetailResponse,
    DiscardContextEditRequest,
    PageAutosaveRequest,
    PageBacklinkListResponse,
    PageInlineCommentCreateRequest,
    PageInlineCommentListResponse,
    PageInlineCommentResponse,
    PageRestoreRequest,
    PageTagListResponse,
    PageUpdateRequest,
    PageVersionDiffResponse,
    PageVersionListResponse,
    PageVersionResponse,
    PublishPageRequest,
    PublishPageVersionRequest,
    SubmitContextEditRequest,
)
from backend.schemas.read import ContextPageListResponse
from backend.services.lint import LintRegistry, LintStage
from backend.services.page_service import (
    PageConflictError,
    PageContextMediaError,
    PageNotFoundError,
    PagePublicExposureForbiddenError,
    PagePublicVersionConflictError,
    PageService,
    PageServiceError,
    PageValidationError,
    WorkspacePublicSharingDisabledError,
)
from backend.services.read_errors import ReadServiceError, RejectedByPolicyError

router = APIRouter(
    prefix="/workspaces/{workspace_id}/context/pages", tags=["context-pages"]
)

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}
REST_WRITE_ERROR_RESPONSES = {
    **REST_ERROR_RESPONSES,
    409: {"model": RESTConflictErrorBody},
}


@router.get("", response_model=ContextPageListResponse, responses=REST_ERROR_RESPONSES)
def list_context_pages(
    request: Request,
    workspace_id: UUID,
    actor: RestActorDep,
    page_type: str | None = None,
    status: str | None = None,
    has_draft: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    actor = _require_workspace_access(
        request, actor, workspace_id, required_role="viewer"
    )
    services = get_container_from_app(request).get_services()
    session_factory = get_container_from_app(request).db.session_for(workspace_id)
    try:
        with session_factory() as session:
            page = services.domain_read.list_context_pages(
                session=session,
                workspace_id=workspace_id,
                page_type=page_type,
                status=status,
                has_draft=has_draft,
                limit=limit,
                cursor=cursor,
                actor=actor,
            )
            return {
                "data": page.items,
                "next_cursor": page.next_cursor,
                "total": page.total,
                "has_more": page.has_more,
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get("/tags", response_model=PageTagListResponse, responses=REST_ERROR_RESPONSES)
def list_page_tags(
    request: Request,
    workspace_id: UUID,
    actor: RestActorDep,
    q: Annotated[str | None, Query(max_length=48)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        tags = page_service.list_tags(workspace_id, query=q, limit=limit, actor=actor)
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": tags}


@router.get(
    "/by-slug/{slug}",
    response_model=ContextPageDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def read_page_by_slug(
    request: Request,
    workspace_id: UUID,
    slug: Annotated[str, Path(min_length=1, max_length=64, pattern=PAGE_SLUG_PATTERN)],
    actor: RestActorDep,
):
    actor = _require_workspace_access(
        request, actor, workspace_id, required_role="viewer"
    )
    services = get_container_from_app(request).get_services()
    session_factory = get_container_from_app(request).db.session_for(workspace_id)
    page_service = PageService(
        session_factory,
        job_queue=services.job_queue,
        body_preparer=services.body_preparer,
        lint_registry=services.lint_registry,
        context_media_repository_factory=services.context_media_repository_factory,
    )
    try:
        with session_factory() as session:
            workspace_slug = services.workspace_directory.slug_by_workspace_id(
                session, workspace_id
            )
            if workspace_slug is None:
                raise PageNotFoundError("page not found")
        page = page_service.read_page(
            workspace_slug=workspace_slug, slug=slug, actor=actor
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return page_write_response(
        _to_detail(page),
        write_operation=False,
        lint_registry=page_service.lint_registry,
    )


@router.post(
    "",
    status_code=201,
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def create_context_page(
    request: Request,
    workspace_id: UUID,
    request_body: ContextPageCreateRequest,
    actor: RestActorDep,
):
    services = get_container_from_app(request).get_services()
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        with page_service.session_factory() as session:
            workspace_slug = services.workspace_directory.slug_by_workspace_id(
                session, workspace_id
            )
            if workspace_slug is None:
                raise PageNotFoundError("workspace not found")
        page = page_service.create_page_first(
            workspace_slug=workspace_slug,
            title=request_body.title,
            body_md=request_body.body_md,
            page_type=request_body.page_type,
            tags=request_body.tags,
            aliases=request_body.aliases,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            placement_parent_node_id=request_body.placement_parent_node_id,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.get(
    "/{page_id}",
    response_model=ContextPageDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_context_page(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    version_id: UUID | None = None,
):
    services = get_container_from_app(request).get_services()
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        with page_service.session_factory() as session:
            page, version, payload = page_service.resolve_page_for_detail(
                session,
                page_id=page_id,
                version_id=version_id,
                actor=actor,
            )
            enrichment = services.domain_read.context_page_read_enrichment(
                session, workspace_id, page, version, actor
            )
            latest_draft_version_id = services.domain_read._context_page_summary(
                page, actor=actor
            ).latest_draft_version_id
    except PageServiceError as exc:
        return service_error_response(exc)
    detail = ContextPageDetail(
        **payload.model_dump(),
        latest_draft_version_id=latest_draft_version_id,
        **enrichment,
    )
    return page_write_response(
        detail, write_operation=False, lint_registry=page_service.lint_registry
    )


@router.get(
    "/{page_id}/backlinks",
    response_model=PageBacklinkListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_page_backlinks(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIST_LIMIT_MAX)] = 50,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        backlinks = page_service.list_backlinks(
            page_id=page_id, actor=actor, limit=limit
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": backlinks}


@router.get(
    "/{page_id}/versions",
    response_model=PageVersionListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_page_versions(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIST_LIMIT_MAX)] = 50,
    cursor: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=PAGE_LIST_CURSOR_MAX_LENGTH,
            pattern=PAGE_LIST_CURSOR_PATTERN,
        ),
    ] = None,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        result = page_service.list_versions(
            page_id=page_id, limit=limit, cursor=cursor, actor=actor
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": result.versions, "next_cursor": result.next_cursor}


@router.get(
    "/{page_id}/versions/{branch_id}/reconciliation",
    response_model=ContextReconciliationDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def get_page_reconciliation(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    branch_id: UUID,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        detail = page_service.get_reconciliation(
            page_id=page_id, branch_id=branch_id, actor=actor
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": detail}


@router.post(
    "/{page_id}/versions/{branch_id}/reconciliation",
    response_model=ContextReconciliationResultResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def reconcile_page_branch(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    branch_id: UUID,
    request_body: ContextReconciliationRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        result = page_service.reconcile_branch(
            page_id=page_id, branch_id=branch_id, request=request_body, actor=actor
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": result}


@router.post(
    "/{page_id}/versions/{version_id}/discard",
    response_model=PageVersionResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def discard_alternative_draft(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    version_id: UUID,
    request_body: ContextReconciliationDiscardRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        version = page_service.discard_alternative(
            page_id=page_id,
            version_id=version_id,
            reason=request_body.reason,
            actor=actor,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return {"data": version}


@router.post(
    "/{page_id}/versions/{version_id}/publish",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def publish_page_version(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    version_id: UUID,
    request_body: PublishPageVersionRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        result = page_service.publish_draft(
            page_id=page_id,
            version_id=version_id,
            reason=request_body.reason,
            publish_despite_findings=request_body.publish_despite_findings,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            expected_content_hash=request_body.expected_content_hash,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return page_write_response(
        _to_detail(result.page),
        published_findings=result.findings,
        lint_registry=page_service.lint_registry,
    )


@router.get(
    "/{page_id}/diff",
    response_model=PageVersionDiffResponse,
    responses=REST_ERROR_RESPONSES,
)
def diff_page_versions(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    to_version_id: UUID,
    actor: RestActorDep,
    from_version_id: UUID | None = None,
    baseline: Literal["empty"] | None = None,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        diff = page_service.diff_versions(
            page_id=page_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            baseline=baseline,
            actor=actor,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": diff}


@router.get(
    "/{page_id}/comments",
    response_model=PageInlineCommentListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_page_inline_comments(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIST_LIMIT_MAX)] = 50,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="viewer"
        )
        comments = page_service.list_inline_comments(
            page_id=page_id, limit=limit, actor=actor
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": comments}


@router.post(
    "/{page_id}/comments",
    status_code=201,
    response_model=PageInlineCommentResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def create_page_inline_comment(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: PageInlineCommentCreateRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        comment = page_service.create_inline_comment(
            page_id=page_id,
            note=request_body.note,
            anchor=request_body.anchor,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return {"data": comment}


@router.post(
    "/{page_id}/begin-edit",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def begin_context_edit(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: BeginContextEditRequest,
    actor: RestActorDep,
):
    """P8.3: idempotently resume or open the caller's own editing session."""

    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page = page_service.begin_context_edit(
            page_id=page_id,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            idempotency_key=request_body.idempotency_key,
            base_version_id=request_body.base_version_id,
            base_content_hash=request_body.base_content_hash,
            retry_only=request_body.retry_only,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.post(
    "/{page_id}/submit-for-review",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def submit_context_edit_for_review(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: SubmitContextEditRequest,
    actor: RestActorDep,
):
    """P8.1: "Save draft" -- lock the caller's own editing row and submit it."""

    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page = page_service.submit_context_edit_for_review(
            page_id=page_id,
            version_id=request_body.version_id,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            expected_editing_revision=request_body.expected_editing_revision,
            idempotency_key=request_body.idempotency_key,
            reason=request_body.reason,
            retry_only=request_body.retry_only,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.post(
    "/{page_id}/discard-edit",
    status_code=204,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def discard_context_edit(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: DiscardContextEditRequest,
    actor: RestActorDep,
):
    """P8.3: discard the caller's own open editing row (terminal, not a hard-delete)."""

    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page_service.discard_context_edit(
            page_id=page_id,
            version_id=request_body.version_id,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            idempotency_key=request_body.idempotency_key,
            retry_only=request_body.retry_only,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return JSONResponse(status_code=204, content=None)


@router.post(
    "/{page_id}/restore",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def restore_page_version(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: PageRestoreRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        # P2.2/P2.6: restoring FROM a version this actor cannot otherwise see
        # (another author's private `editing` row) must not be a back door to
        # it -- reuse read_page's own explicit-version visibility gate rather
        # than letting restore_version's bare page-scoped id lookup skip it.
        page_service.read_page(
            page_id=page_id, version_id=request_body.version_id, actor=actor
        )
        page = page_service.restore_version(
            page_id=page_id,
            version_id=request_body.version_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.post(
    "/{page_id}/publish",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def set_page_public(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: PublishPageRequest,
    actor: RestActorDep,
):
    # Editor+ is the route floor. The service strengthens non-public Workspace
    # exposure to Owner + browser provenance + exact-version acknowledgement.
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page = page_service.set_page_public(
            page_id=page_id,
            is_public=request_body.is_public,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_role=actor.workspace_role,
            actor_provenance=actor.provenance,
            acknowledge_public_exposure=request_body.acknowledge_public_exposure,
            expected_published_version_id=request_body.expected_published_version_id,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.patch(
    "/{page_id}",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def update_page(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: PageUpdateRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page = page_service.update_page(
            page_id=page_id,
            title=request_body.title,
            body_md=request_body.body_md,
            page_type=request_body.page_type,
            tags=request_body.tags,
            aliases=request_body.aliases,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
            base_version_id=request_body.base_version_id,
            base_content_hash=request_body.base_content_hash,
            expected_editing_revision=request_body.expected_editing_revision,
            on_stale=request_body.on_stale,
            destination_node_id=request_body.destination_node_id,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


@router.post(
    "/{page_id}/autosave",
    response_model=ContextPageDetailResponse,
    responses=REST_WRITE_ERROR_RESPONSES,
)
def autosave_page(
    request: Request,
    workspace_id: UUID,
    page_id: UUID,
    request_body: PageAutosaveRequest,
    actor: RestActorDep,
):
    try:
        actor, page_service = _workspace_page_service(
            request, actor, workspace_id, required_role="editor"
        )
        page = page_service.autosave_page(
            page_id=page_id,
            workspace_id=workspace_id,
            title=request_body.title,
            body_md=request_body.body_md,
            page_type=request_body.page_type,
            tags=request_body.tags,
            aliases=request_body.aliases,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            base_version_id=request_body.base_version_id,
            base_content_hash=request_body.base_content_hash,
            actor_provenance=actor.provenance,
            idempotency_key=request_body.idempotency_key,
            expected_editing_revision=request_body.expected_editing_revision,
            session_version_id=request_body.session_version_id,
            on_stale=request_body.on_stale,
            destination_node_id=request_body.destination_node_id,
            retry_only=request_body.retry_only,
        )
    except PageServiceError as exc:
        return service_error_response(exc)
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    return page_write_response(
        _to_detail(page), lint_registry=page_service.lint_registry
    )


def _to_detail(page) -> ContextPageDetail:
    """Wrap a write command's ``PagePayload`` in the canonical detail shape.

    Deliberately does not run ``context_page_read_enrichment`` (tree
    placement, backlinks, reconciliation availability, pending review): that
    projection needs its own page/version read and is only computed for the
    dedicated ``GET /{page_id}`` detail route, exactly matching the old
    ``/pages/*`` write endpoints, which never returned it either. Every field
    this leaves at its default is still present on the wire -- just empty --
    so the response shape never varies by route.
    """

    return ContextPageDetail(**page.model_dump())


def page_write_response(
    page: ContextPageDetail,
    *,
    published_findings=None,
    write_operation: bool = True,
    lint_registry: LintRegistry | None = None,
) -> dict:
    """Wrap a page with operation-accurate warnings for unpublished content."""
    warnings = []
    registry = lint_registry if lint_registry is not None else LintRegistry.builtin()
    for reason in page.publish_blocked_reasons:
        rule = registry.rule(reason.code)
        if rule is not None and LintStage.PUBLISH_FLOOR in rule.stages:
            message = (
                f"Saved as a draft, not published. {reason.message}"
                if write_operation
                else f"This draft is not published. {reason.message}"
            )
            warnings.append(
                EnvelopeWarning(code="lint_publish_blocked", message=message)
            )
        else:
            warnings.append(
                EnvelopeWarning(code="draft_unpublished", message=reason.message)
            )
    warnings.extend(
        EnvelopeWarning(
            code="published_with_findings",
            message=f"Published despite a format finding. {finding.message}",
        )
        for finding in published_findings or []
    )
    if not warnings:
        return {"data": page}
    return {
        "envelope": RESTResultEnvelope(
            result_status="partial" if write_operation else "succeeded",
            warnings=warnings,
        ),
        "data": page,
    }


def _require_workspace_access(
    request: Request,
    actor: ActorContext,
    workspace_id: UUID,
    *,
    required_role: str,
) -> ActorContext:
    container = get_container_from_app(request)
    control_factory = container.db.control_session_factory()
    try:
        with control_factory() as session:
            access = require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=workspace_id,
                required_role=required_role,
                workspace_session_factory=container.db.session_for(workspace_id),
            )
    except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
        # D-01v3-e/D-01v3-b: previously uncaught here (neither is a
        # RejectedByPolicyError, deliberately -- see rbac.py) and would have
        # fallen through to a generic 500 identical to a real bug.
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
            },
        ) from exc
    except RejectedByPolicyError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": exc.error_code,
                "message": str(exc) or "workspace access denied",
            },
        ) from exc
    return rebind_actor_to_workspace_access(actor, access)


def _workspace_page_service(
    request: Request,
    actor: ActorContext,
    workspace_id: UUID,
    *,
    required_role: str,
    page_id: UUID | None = None,
) -> tuple[ActorContext, PageService]:
    """Verify ``page_id`` truly belongs to ``workspace_id`` (not merely that
    the actor has ``required_role`` on *some* workspace named in the URL),
    then build a ``PageService`` bound to that workspace's session.

    ``page_id`` is read from the route's own path parameters when omitted
    (every route below names its path param ``page_id``); passing it
    explicitly is only needed where FastAPI cannot infer it.
    """

    container = get_container_from_app(request)
    services = container.get_services()
    control_factory = container.db.control_session_factory()
    resolved_page_id = (
        page_id if page_id is not None else request.path_params.get("page_id")
    )
    try:
        with control_factory() as session:
            if resolved_page_id is not None:
                true_workspace_id = (
                    services.workspace_directory.workspace_id_by_context_page(
                        session, UUID(str(resolved_page_id))
                    )
                )
                if true_workspace_id is None or true_workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
            access = require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=workspace_id,
                required_role=required_role,
                workspace_session_factory=container.db.session_for(workspace_id),
            )
    except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
        # D-01v3-e/D-01v3-b: previously uncaught here (neither is a
        # RejectedByPolicyError, deliberately -- see rbac.py) and would have
        # fallen through to a generic 500 identical to a real bug.
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
            },
        ) from exc
    except RejectedByPolicyError as exc:
        raise_page_policy_error(exc)
    scoped_actor = rebind_actor_to_workspace_access(actor, access)
    return (
        scoped_actor,
        PageService(
            container.db.session_for(workspace_id),
            job_queue=services.job_queue,
            body_preparer=services.body_preparer,
            lint_registry=services.lint_registry,
            context_media_repository_factory=services.context_media_repository_factory,
        ),
    )


def raise_page_policy_error(exc: ReadServiceError) -> None:
    if not isinstance(exc, RejectedByPolicyError):
        raise exc
    if exc.error_code in {
        "workspace_membership_missing",
        "token_workspace_denied",
        "mcp_client_workspace_denied",
    }:
        raise PageNotFoundError("page not found") from exc
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": exc.error_code,
            "message": str(exc) or "workspace access denied",
        },
    ) from exc


def service_error_response(exc: PageServiceError) -> JSONResponse:
    status_code = status_code_for_error(exc)
    extra = None
    if isinstance(exc, PageConflictError):
        extra = {
            "server_version_id": exc.server_version_id,
            "server_content_hash": exc.server_content_hash,
        }
    if isinstance(exc, PagePublicVersionConflictError):
        extra = {
            "server_version_id": (
                str(exc.current_published_version_id)
                if exc.current_published_version_id is not None
                else None
            )
        }
    if isinstance(exc, PageContextMediaError) and exc.page_id is not None:
        extra = {"page_id": str(exc.page_id)}
    return build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc) or exc.error_code,
        status_code=status_code,
        extra=extra,
    )


def status_code_for_error(exc: PageServiceError) -> int:
    if isinstance(exc, PageNotFoundError):
        return 404
    if isinstance(exc, PageConflictError):
        return 409
    if isinstance(exc, PagePublicVersionConflictError):
        return 409
    if isinstance(exc, PageContextMediaError) and exc.error_code in {
        "legacy_media_cutover_conflict",
        "media_editing_not_open",
    }:
        return 409
    if isinstance(exc, PagePublicExposureForbiddenError):
        return 403
    if isinstance(exc, WorkspacePublicSharingDisabledError):
        return 409
    if isinstance(exc, PageValidationError):
        return 422
    return 500
