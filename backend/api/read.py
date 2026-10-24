import json
import logging
import sqlite3
from dataclasses import replace
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import OperationalError

from backend.api.deps import (
    AskServiceDep,
    RestActorDep,
    SearchServiceDep,
    ServicesDep,
    SessionFactoryDep,
    WorkspaceAdminAccessDep,
    WorkspaceEditorAccessDep,
    WorkspaceOwnerAccessDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.api.media_errors import media_staging_error_response
from backend.api.middleware.request_body_limit import BodyLimit, request_body_limit
from backend.api.middleware.security_headers import (
    CSP_SANDBOXED_DOCUMENT_STATE_KEY,
)
from backend.app.container import PurgeableBlobStore
from backend.app.errors import build_rest_error_response
from backend.app.settings import Settings
from backend.app.state import get_container_from_app
from backend.context_media.domain import ActorIdentity, UsageState
from backend.library_media.ports import LibraryLogicalQuotaExceededError
from backend.mcp.envelope import advisory_warnings
from backend.media.application import if_none_match_matches
from backend.media.domain import MediaStagingError
from backend.media.ports import MediaInstallInProgressError
from backend.policy.auth import ActorContext
from backend.policy.rbac import WorkspaceAccess, role_allows
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    check_actor_can_access,
    check_actor_scope,
    required_role_to_action,
)
from backend.policy.workspace_resolution import (
    WorkspaceAmbiguityError,
    resolve_workspace_for_actor,
    workspace_existence_is_hidden,
)
from backend.schemas.activity import (
    ActivityFeedResponse,
    ActivityListResponse,
    ActivityReadGroupResponse,
)
from backend.schemas.agent import (
    AgentPageActivateRequest,
    AgentPageDraftRequest,
    AgentPageDraftResultResponse,
    AgentPageRestoreRequest,
    BuildBundleRequest,
    BuildBundleResponse,
)
from backend.schemas.ask import (
    AskSessionCreate,
    AskSessionDetailResponse,
    AskSessionListResponse,
    AskTurnCreate,
    AskTurnDetailResponse,
)
from backend.schemas.common import RESTConflictErrorBody, RESTErrorBody
from backend.schemas.context_ingress import (
    AskTurnPromoteRequest,
    ContextIngressAcceptRequest,
    ContextIngressAcceptResponse,
    ContextIngressProposalListResponse,
    ContextIngressProposalResponse,
    ContextIngressRejectRequest,
)
from backend.schemas.context_media import (
    ContextMediaAttachmentListResponse,
    ContextMediaOrphanListResponse,
)
from backend.schemas.context_reconciliation import (
    ContextReconciliationBulkDiscardRequest,
    ContextReconciliationBulkDiscardResponse,
)
from backend.schemas.context_write import (
    FeedbackListResponse,
    ReviewAutoApprovalSweepResponse,
    ReviewBulkDecisionRequest,
    ReviewBulkDecisionResponse,
    ReviewCountsResponse,
    ReviewDecisionRecentListResponse,
    ReviewDecisionRequest,
    ReviewItemListResponse,
    ReviewItemResponse,
    SubmitFeedbackRequest,
    SubmitFeedbackResponse,
)
from backend.schemas.coverage import CoverageGapListResponse
from backend.schemas.jobs import JobListResponse, JobStatus
from backend.schemas.lint import (
    LintFindingBatchDecisionRequest,
    LintFindingBatchDecisionResponse,
    LintFindingDecisionRequest,
    LintFindingListResponse,
    LintFindingResponse,
    LintFormatRequest,
    LintFormatResponse,
    LintRuleListResponse,
    LintRunCreateRequest,
    LintRunResponse,
)
from backend.schemas.page import PageVersionDiffResponse
from backend.schemas.read import (
    AgentCategoryListResponse,
    AgentPageDetailResponse,
    AgentPageListResponse,
    GraphNeighborsResponse,
    LibraryArtifactResponse,
    LibraryCapabilitiesResponse,
    LibraryItemDetailResponse,
    LibraryItemListResponse,
    LibraryItemReferenceListResponse,
    LibraryItemTombstoneListResponse,
    LibraryItemVersionListResponse,
    LibraryItemVersionResponse,
    ListChangesResponse,
    SearchCapabilitiesResponse,
    SearchRequest,
    SearchResponse,
    TreeListResponse,
    TreeNodeIndexPreviewResponse,
    TreeNodeResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateResponse,
    WorkspaceDeleteResponse,
    WorkspaceDetailResponse,
    WorkspaceListResponse,
)
from backend.schemas.settings import (
    SettingsMcpClientDeleteResponse,
    SettingsMcpClientListResponse,
    SettingsMcpClientUpdateRequest,
    SettingsPoliciesResponse,
    SettingsPolicyBatchUpdateRequest,
    SettingsPolicyUpdateRequest,
)
from backend.schemas.tree import (
    FavoriteResponse,
    FavoriteTargetKind,
    FavoriteUpdateRequest,
    TreeFolderCreateRequest,
    TreeMoveRequest,
    TreeNodeUpdate,
    TreeReorderRequest,
)
from backend.services.activity_feed_service import (
    ActivityFeedService,
    InvalidActivityTokenError,
)
from backend.services.actor_visible_read import context_page_id_visible
from backend.services.ask_service import InvalidUserMessageError
from backend.services.context_media_attachments import (
    attachment_read_projection,
    decode_attachment_cursor,
    encode_attachment_cursor,
)
from backend.services.context_media_orphan_sweep import (
    list_orphans as list_context_media_orphans,
)
from backend.services.context_media_orphan_sweep import orphan_read_projection
from backend.services.context_media_policy import read_context_media_policy
from backend.services.conversion_orchestrator import (
    LibraryConversionRetryNotAvailableError,
    LibraryConversionRetryNotFoundError,
)
from backend.services.coverage_service import (
    list_coverage_gaps,
    record_coverage_gap,
    resolve_coverage_gap,
)
from backend.services.favorite_service import set_favorite
from backend.services.library_binary_registration import (
    LibraryUploadPolicyChangedError,
)
from backend.services.library_blob_storage import (
    LibraryBlobStorageError,
    LibraryBlobTooLargeError,
)
from backend.services.library_content_service import LibraryByteRangeError
from backend.services.library_formats import (
    LibraryFormatError,
    normalize_library_filename,
)
from backend.services.library_ingest import (
    LibraryFileEmptyError,
    LibraryUploadIdempotencyConflictError,
)
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadServiceError,
    RejectedByPolicyError,
)
from backend.services.restricted_library_policy import is_restricted_library_target
from backend.services.search_service import (
    KEYWORD_LANE_UNAVAILABLE_WARNING,
    RESTRICTED_LIBRARY_TRUST_LEVELS,
    can_read_restricted_library_items,
)
from backend.services.settings_service import (
    RETIRED_LIBRARY_SOURCE_RULE_KEYS,
    LibraryPolicyBatchUpdateRequired,
    SettingsMcpClientCursorInvalid,
    SettingsMcpClientDeleteRejected,
    SettingsPolicyRevisionConflict,
    SettingsPolicyUpdateRejected,
    format_library_upload_limit,
    library_format_is_enabled,
    library_upload_limit_bytes,
    normalized_library_policy,
    settings_policy_revision,
)
from backend.services.tree_service import resolve_tree_parent
from backend.services.workspace_purge_grant import (
    consume_purge_grant,
    issue_purge_grant,
)
from backend.services.workspace_write import (
    WorkspaceCreateConflictError,
    WorkspaceDeleteRejectedError,
)
from backend.spi.conversion import ConversionLimits
from backend.utils.actor_display import (
    DeferredIdentityLabels,
    resolve_actor_display_names,
)

router = APIRouter(prefix="/workspaces", tags=["read"])
logger = logging.getLogger(__name__)

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


def upsert_rest_responses(model: type, reuse_description: str) -> dict:
    """OpenAPI responses for create-or-reuse POST routes.

    The decorator-level ``status_code=201`` documents the creation path;
    the explicit 200 entry documents the reuse path so clients can rely on
    201 to detect that a new resource was created.
    """
    return {
        200: {"model": model, "description": reuse_description},
        **REST_ERROR_RESPONSES,
    }


def require_workspace_access(
    request: Request,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
) -> None:
    raw_workspace_id = request.path_params.get("workspace_id")
    if raw_workspace_id is None:
        return
    workspace_id = UUID(str(raw_workspace_id))
    with session_factory() as session:
        try:
            ws = resolve_workspace_for_actor(
                session=session,
                actor=actor,
                requested_workspace_id=workspace_id,
            )
            check_actor_can_access(
                actor,
                ws.workspace_id,
                required_role_to_action(required_role_for_request(request)),
                session=session,
            )
        except WorkspaceAmbiguityError as exc:
            if workspace_existence_is_hidden(
                session,
                actor=actor,
                workspace_id=workspace_id,
            ):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "not_found",
                        "message": "workspace not found",
                    },
                ) from exc
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": "workspace_ambiguous",
                    "message": str(exc) or "workspace access denied",
                },
            ) from exc
        except PermissionError as exc:
            if workspace_existence_is_hidden(
                session,
                actor=actor,
                workspace_id=workspace_id,
            ):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "not_found",
                        "message": "workspace not found",
                    },
                ) from exc
            error_code = str(exc).split(":", 1)[0] or "workspace_access_denied"
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": error_code,
                    "message": str(exc) or "workspace access denied",
                },
            ) from exc


def _gate_sensitive_agent_page_body(
    svc,
    session,
    actor: ActorContext,
    access: WorkspaceAccess,
    workspace_id: UUID,
    agent_page_id: UUID,
) -> None:
    """Owner-gate the body of a sensitive AgentPage (agent_guide/memory),
    mirroring the MCP read_agent_page gate. No-op for non-sensitive pages;
    raises a role-insufficient ReadServiceError (mapped to 403) otherwise."""
    requires_sensitive_access = svc.domain_read.agent_page_requires_sensitive_access(
        session=session,
        workspace_id=workspace_id,
        agent_page_id=agent_page_id,
    )
    if requires_sensitive_access:
        _require_sensitive_agent_page_access(
            actor, access, resource_workspace_id=workspace_id
        )


def _require_sensitive_agent_page_access(
    actor: ActorContext,
    access: WorkspaceAccess,
    *,
    resource_workspace_id: UUID | None = None,
) -> None:
    """Owner role plus the admin scope THIS workspace grants.

    A REST actor is never rebound to the path workspace the way an MCP actor
    is by ``resolve_target_actor``: for a PAT/OAuth actor ``actor.workspace_id``
    is unresolved (D-01v2: the credential names a SET of allowed workspaces,
    not one) until ``actor_with_target_workspace_scope`` narrows it, and for
    a session actor it is the user's default, frequently not the workspace in
    the request path. So the path workspace is passed explicitly and a
    multi-workspace credential cannot answer this gate with a different
    workspace's broader grant.
    """

    if not role_allows(access.role, "owner"):
        raise RejectedByPolicyError(
            "workspace owner role is required",
            error_code="workspace_role_insufficient",
        )
    try:
        check_actor_scope(actor, "admin", resource_workspace_id=resource_workspace_id)
    except PermissionError as exc:
        raise RejectedByPolicyError(
            "workspace owner role and write scope are required",
            error_code="workspace_role_insufficient",
        ) from exc


def required_role_for_request(request: Request) -> str:
    if request.method == "GET":
        return "viewer"
    if request.method == "PUT" and _authorization_path(request).endswith(
        "/favorites/{target_kind}/{target_id}"
    ):
        return "viewer"
    if request.method == "POST" and _authorization_path(request).endswith(
        ("/search", "/feedback")
    ):
        return "viewer"
    return "editor"


def _authorization_path(request: Request) -> str:
    """The path an authorization decision may look at.

    The matched route's template when there is one, otherwise the ASGI path
    the router matched. Never ``request.url``: Starlette rebuilds that from
    the client-supplied Host header, so ``Host: x/search?`` made every POST
    look like a viewer-level search.
    """

    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return str(request.scope.get("path") or "")


router.dependencies.append(Depends(require_workspace_access))


@router.get("", response_model=WorkspaceListResponse, responses=REST_ERROR_RESPONSES)
def list_workspaces(
    svc: ServicesDep,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    # Listing also repairs a stale/null default to a live Personal workspace.
    with session_factory.begin() as session:
        return {
            "data": svc.workspace_read.list_workspaces(
                session=session,
                actor=actor,
            ),
        }


@router.put(
    "/{workspace_id}/favorites/{target_kind}/{target_id}",
    response_model=FavoriteResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_favorite(
    workspace_id: UUID,
    target_kind: FavoriteTargetKind,
    target_id: UUID,
    request_body: FavoriteUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": set_favorite(
                    session,
                    workspace_id=workspace_id,
                    actor=actor,
                    target_kind=target_kind,
                    target_id=target_id,
                    is_favorite=request_body.is_favorite,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "",
    status_code=201,
    response_model=WorkspaceCreateResponse,
    responses=REST_ERROR_RESPONSES,
)
def create_workspace(
    svc: ServicesDep,
    request_body: WorkspaceCreateRequest,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.workspace_write.create_workspace(
                    session=session,
                    request=request_body,
                    actor=actor,
                )
            }
    except RejectedByPolicyError as exc:
        return read_service_error_response(exc)
    except WorkspaceCreateConflictError as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=409,
        )


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_workspace(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    control_session_factory: SessionFactoryDep,
):
    # Every field of WorkspaceDetail is a `workspace` column, and `workspace`
    # is control-plane -- a shard does not carry the table at all. This is the
    # same read `get_settings_policies` serves the policy cards from, so the
    # two routes have to answer off the same plane or `library_policy` means
    # two different things depending on which one you ask.
    try:
        with control_session_factory() as session:
            return {
                "data": svc.workspace_read.get_workspace(
                    session=session,
                    workspace_id=workspace_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}",
    response_model=WorkspaceDeleteResponse,
    responses={
        **REST_ERROR_RESPONSES,
        409: {"model": RESTConflictErrorBody},
        503: {"model": RESTErrorBody},
    },
)
def delete_workspace(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    request: Request,
    session_factory: WorkspaceSessionFactoryDep,
    purge_contents: Annotated[
        bool,
        Query(
            alias="purge",
            description=(
                "Delete the workspace's content and operational rows as part "
                "of the deletion. Defaults to false, which refuses with a 409 "
                "listing what is retained; the client re-sends with purge=true "
                "once the owner has confirmed that second, destructive step."
            ),
        ),
    ] = False,
    # PURGE_AUTHZ_PLAN S-7: a header, never a query parameter -- a query
    # string lands in access logs and Referer headers, which a 5-minute
    # destructive credential must not.
    x_purge_grant: Annotated[str | None, Header(alias="X-Purge-Grant")] = None,
):
    from backend.domain.credential_input import (
        PURGE_GRANT_MAX_BYTES,
        ascii_token_is_valid,
    )
    from backend.services.credential_hashing import PURGE_GRANT_PREFIX

    if (
        purge_contents
        and x_purge_grant is not None
        and (
            not ascii_token_is_valid(x_purge_grant, PURGE_GRANT_MAX_BYTES)
            or not x_purge_grant.startswith(PURGE_GRANT_PREFIX)
        )
    ):
        return build_rest_error_response(
            error_code="purge_grant_required",
            message="workspace purge grant is missing, invalid, or expired",
            status_code=403,
        )
    # 260912 review, F-1: resolved and capability-checked BEFORE anything is
    # deleted. `store_for` used to be called only after the delete
    # transaction had already committed, so a backend store that does not
    # implement `purge_workspace` (duck-typed, no Protocol) raised
    # AttributeError into the broad `except Exception` below -- by which
    # point the workspace row and its content rows were already gone. That
    # made an unsupported store a silent, permanent byte leak instead of a
    # rejected request. Only relevant when purge_contents is requested at
    # all: an unqualified delete never calls purge_workspace.
    store = None
    if purge_contents:
        # `.get_services().blob_store_provider`, not `.blob_store_provider`
        # directly: the latter does not exist on `AppContainer` at all (the
        # cached_property lives on `ServiceRegistry`, see
        # `backend/api/context_media.py`'s content-read route for the
        # correct shape). Before this fix that typo meant `store_for` was
        # never reached for ANY workspace purge -- the AttributeError was
        # swallowed by the broad `except Exception` below, so blob cleanup
        # silently no-op'd on every purge this codebase has ever run.
        container = get_container_from_app(request)
        store = container.get_services().blob_store_provider.store_for(workspace_id)
        if not isinstance(store, PurgeableBlobStore):
            return build_rest_error_response(
                error_code="purge_unavailable",
                message=(
                    "This deployment's storage backend does not support "
                    "workspace purge."
                ),
                status_code=503,
            )
    try:
        with session_factory.begin() as session:
            if purge_contents:
                # Take the media-intent -> workspace -> user -> membership
                # locks before touching the grant row. Besides closing the
                # earlier dependency-check race, this matches media writes and
                # membership changes without introducing a lock cycle.
                svc.workspace_write.require_delete_authorization(
                    session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                )
                # S-4: one conditional DELETE, in the SAME transaction as the
                # purge it gates, before purge_workspace_data runs -- doing
                # this after would find its own grant row already destroyed
                # by the purge and 403 every valid request (T-4/T-9). A
                # rollback of this transaction (T-8) undoes the DELETE along
                # with everything else, so a failed purge leaves the grant
                # usable for a retry.
                if not consume_purge_grant(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    token=x_purge_grant,
                ):
                    raise RejectedByPolicyError(
                        "workspace purge grant is missing, invalid, or expired",
                        error_code="purge_grant_required",
                    )
                # S-2: the service re-checks this scope itself; it is not
                # persisted anywhere and exists only for this one call.
                actor = replace(actor, scopes=actor.scopes + ("workspace-purge",))
            deleted = svc.workspace_write.delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
                purge_contents=purge_contents,
            )
    except ReadServiceError as exc:
        return read_service_error_response(exc)
    except WorkspaceDeleteRejectedError as exc:
        extra = (
            {
                "blockers": [
                    {"code": code, "count": count}
                    for code, count in sorted(exc.blockers.items())
                ]
            }
            if exc.blockers
            else None
        )
        if exc.blockers:
            # S-3: minted in a SEPARATE, freshly-committed transaction. The
            # transaction above already rolled back (it raised from inside
            # `session_factory.begin()`), so a grant written there would
            # never have survived to be returned. Only for a
            # purge-recoverable refusal (non-empty blockers). Policy denials
            # never reach this branch, and no grant can fix those.
            try:
                with session_factory.begin() as grant_session:
                    # The refusal transaction has rolled back.  Re-lock and
                    # re-check both the workspace and the live owner before
                    # inserting its FK child: another owner may have deleted
                    # the workspace (or changed this actor's role) in between.
                    svc.workspace_write.require_delete_authorization(
                        session=grant_session,
                        workspace_id=workspace_id,
                        actor=actor,
                    )
                    purge_grant, purge_grant_expires_at = issue_purge_grant(
                        grant_session,
                        user_id=actor.user_id,
                        workspace_id=workspace_id,
                    )
            except ReadServiceError as grant_exc:
                return read_service_error_response(grant_exc)
            extra["purge_grant"] = purge_grant
            extra["purge_grant_expires_at"] = purge_grant_expires_at
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=409,
            extra=extra,
        )
    # Only after the transaction above has committed. The database is the
    # record of what exists, so bytes must not disappear while a rollback could
    # still restore the rows that referenced them. Orphaned bytes are wasted
    # disk, not a correctness failure, so a purge failure is logged rather than
    # turned into an error for a deletion the owner already confirmed and that
    # has, at this point, already happened. `store` was already resolved and
    # capability-checked above; only the call itself is still guarded here.
    if purge_contents:
        try:
            store.purge_workspace(workspace_id)
        except Exception:  # pragma: no cover - defensive, see comment above
            logger.exception(
                "workspace blob purge failed after deletion committed",
                extra={"workspace_id": str(workspace_id)},
            )
    return {"data": {"deleted_workspace_id": deleted}}


@router.get(
    "/{workspace_id}/settings/policies",
    response_model=SettingsPoliciesResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_settings_policies(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    # Policy cards are Workspace columns (control plane); the MCP connection
    # list under them is MCPClient (workspace plane). Read-only on both. Kept
    # as a comment, not a docstring: FastAPI publishes a route docstring as the
    # OpenAPI `description`, and this change is deliberately contract-neutral.
    try:
        with (
            control_session_factory() as control_session,
            session_factory() as session,
        ):
            return {
                "data": svc.settings_svc.get_policies(
                    control_session=control_session,
                    workspace_session=session,
                    workspace_id=workspace_id,
                    mcp_client_limit=limit,
                    mcp_client_cursor=cursor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/settings/mcp-clients",
    response_model=SettingsMcpClientListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_settings_mcp_clients(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    request: Request,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    status_filter: Annotated[
        Literal["active", "disabled", "all"],
        Query(alias="status"),
    ] = "active",
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    # The listing is MCPClient (workspace plane) filtered and faceted by OAuth
    # connection health (control plane). Read-only on both; see
    # SettingsService.list_mcp_clients for how the two are correlated without a
    # cross-database join.
    try:
        with (
            control_session_factory() as control_session,
            session_factory() as session,
        ):
            page = svc.settings_svc.list_mcp_clients(
                control_session=control_session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=actor,
                status=status_filter,
                query=q,
                limit=limit,
                cursor=cursor,
                cursor_secret=request.app.state.settings.effective_session_secret,
            )
            return {
                "data": page.data,
                "next_cursor": page.next_cursor,
                "total_count": page.total_count,
                "filtered_count": page.filtered_count,
                "status_counts": page.status_counts,
            }
    except SettingsMcpClientCursorInvalid as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=422,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.patch(
    "/{workspace_id}/settings/policies/batch",
    response_model=SettingsPoliciesResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_settings_policy_batch(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: SettingsPolicyBatchUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    # Every policy column is on Workspace, so the WRITE transaction is the
    # control one; the workspace session is read-only and only feeds the MCP
    # connection list back into the refreshed cards. One plane written, so no
    # distributed transaction.
    try:
        with (
            session_factory() as session,
            control_session_factory.begin() as control_session,
        ):
            return {
                "data": svc.settings_svc.update_policy_batch(
                    control_session=control_session,
                    workspace_session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                    group=request_body.group,
                    changes=request_body.changes,
                    expected_revision=request_body.expected_revision,
                )
            }
    except SettingsPolicyRevisionConflict as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=409,
        )
    except SettingsPolicyUpdateRejected as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=422,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.patch(
    "/{workspace_id}/settings/policies",
    response_model=SettingsPoliciesResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_settings_policy(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: SettingsPolicyUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    # Same split as the batch route above: control writes, workspace reads.
    if (
        request_body.group == "library"
        and request_body.key not in RETIRED_LIBRARY_SOURCE_RULE_KEYS
    ):
        exc = LibraryPolicyBatchUpdateRequired(
            "Library policy changes require an atomic revision-guarded batch."
        )
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=422,
        )
    try:
        with (
            session_factory() as session,
            control_session_factory.begin() as control_session,
        ):
            return {
                "data": svc.settings_svc.update_policy(
                    control_session=control_session,
                    workspace_session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                    group=request_body.group,
                    key=request_body.key,
                    value=request_body.value,
                )
            }
    except SettingsPolicyUpdateRejected as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=422,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.patch(
    "/{workspace_id}/settings/mcp-clients/{client_id}",
    response_model=SettingsPoliciesResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_settings_mcp_client(
    svc: ServicesDep,
    workspace_id: UUID,
    client_id: UUID,
    request_body: SettingsMcpClientUpdateRequest,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    # MCPClient is workspace-plane, so the WRITE transaction is the workspace
    # one and the control session stays read-only (existence gate + the policy
    # cards in the response). One plane written.
    try:
        with (
            control_session_factory() as control_session,
            session_factory.begin() as session,
        ):
            return {
                "data": svc.settings_svc.update_mcp_client_status(
                    control_session=control_session,
                    workspace_session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                    client_id=client_id,
                    status=request_body.status,
                )
            }
    except SettingsPolicyUpdateRejected as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=422,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}/settings/mcp-clients/{client_id}",
    response_model=SettingsMcpClientDeleteResponse,
    responses=REST_ERROR_RESPONSES,
)
def delete_settings_mcp_client(
    svc: ServicesDep,
    workspace_id: UUID,
    client_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    # Same split as the status route above: the tombstone is workspace-plane,
    # so the workspace session owns the write.
    if actor.token_kind != "session":
        return build_rest_error_response(
            error_code="human_session_required",
            message="MCP client deletion requires a browser session",
            status_code=401,
        )
    try:
        with (
            control_session_factory() as control_session,
            session_factory.begin() as session,
        ):
            return {
                "data": svc.settings_svc.delete_mcp_client(
                    control_session=control_session,
                    workspace_session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                    client_id=client_id,
                )
            }
    except SettingsMcpClientDeleteRejected as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=409,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/tree",
    response_model=TreeListResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_tree(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    section: str | None = None,
    include_archived: bool = False,
):
    with session_factory() as session:
        return {
            "data": svc.tree_read.list_tree_data(
                session=session,
                workspace_id=workspace_id,
                section=section,
                # archived titles (folders, ask-session refs) are an editor
                # affordance; a viewer's request silently degrades to the
                # visible tree instead of leaking them (design 3.3)
                include_archived=include_archived and actor_can_read_drafts(actor),
                # A-9: the tree is an access surface — filter per actor like
                # get_index/resources instead of serving every node title.
                actor=actor,
            )
        }


@router.post(
    "/{workspace_id}/tree/nodes",
    response_model=TreeNodeResponse,
    status_code=status.HTTP_201_CREATED,
    responses=REST_ERROR_RESPONSES,
)
def create_tree_folder(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: TreeFolderCreateRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.tree.create_folder(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/tree/nodes/{node_id}",
    response_model=TreeNodeResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_tree_node(
    svc: ServicesDep,
    workspace_id: UUID,
    node_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.tree_read.get_node(
                    session=session,
                    workspace_id=workspace_id,
                    node_id=node_id,
                    # gate the single-node read per actor (A-9): a viewer must
                    # not read a sensitive/draft node's metadata by node id
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/tree/nodes/{node_id}/index-preview",
    response_model=TreeNodeIndexPreviewResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_tree_node_index_preview(
    svc: ServicesDep,
    workspace_id: UUID,
    node_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    # C-6 (design 12.1). Kept as a comment, not a docstring: FastAPI publishes
    # a route docstring as the OpenAPI `description` in the unauthenticated
    # /openapi.json, where a design-item code -- and this route's internals --
    # are noise at best and a leak at worst. (260912 review: the earlier
    # version of this comment said "kept out of the docstring" and then wrote
    # one anyway two lines below; get_settings_policies right below chose the
    # correct shape.)
    #
    # One folder's routing index, rendered exactly as ``get_index`` returns it
    # to an agent -- fanout truncation included, so the preview shows what the
    # agent will actually read rather than the untruncated folder.
    from backend.services.okf import resolve_workspace_slug
    from backend.services.okf_index import (
        IndexNodeNotFoundError,
        build_routing_index,
    )

    try:
        with session_factory() as session:
            workspace_slug = resolve_workspace_slug(session, workspace_id)
            if workspace_slug is None:
                raise ReadNotFoundError("workspace not found")
            # resolve the node's section first: the Agent screen previews
            # agent-section folders, which the default (context, library)
            # projection would 404
            node = svc.tree_read.get_node(
                session=session,
                workspace_id=workspace_id,
                node_id=node_id,
                actor=actor,
            )
            try:
                markdown = build_routing_index(
                    session,
                    workspace_id,
                    actor=actor,
                    workspace_slug=workspace_slug,
                    sections=(node.section,),
                    node=str(node_id),
                )
            except IndexNodeNotFoundError as exc:
                raise ReadNotFoundError("tree node not found") from exc
            return {"data": {"markdown": markdown}}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.patch(
    "/{workspace_id}/tree/nodes/{node_id}",
    response_model=TreeNodeResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_tree_node(
    svc: ServicesDep,
    workspace_id: UUID,
    node_id: UUID,
    request_body: TreeNodeUpdate,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.tree.update_node(
                    session=session,
                    workspace_id=workspace_id,
                    node_id=node_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}/tree/nodes/{node_id}",
    response_model=TreeNodeResponse,
    responses=REST_ERROR_RESPONSES,
)
def delete_tree_node(
    svc: ServicesDep,
    workspace_id: UUID,
    node_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.tree.archive_node(
                    session=session,
                    workspace_id=workspace_id,
                    node_id=node_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/tree/move",
    response_model=TreeNodeResponse,
    responses=REST_ERROR_RESPONSES,
)
def move_tree_node(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: TreeMoveRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.tree.move_node(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/tree/reorder",
    response_model=TreeNodeResponse,
    responses=REST_ERROR_RESPONSES,
)
def reorder_tree_node(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: TreeReorderRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.tree.reorder_node(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/library/items",
    response_model=LibraryItemListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_library_items(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    connector: str | None = None,
    trust: str | None = None,
    lifecycle: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    with session_factory() as session:
        # Restricted actors must not learn hidden-item counts: exclude the
        # restricted trust levels in-query so `total`/`has_more`/the page all
        # reflect only the items the actor may see.
        exclude_trust_levels = (
            None
            if can_read_restricted_library_items(actor)
            else RESTRICTED_LIBRARY_TRUST_LEVELS
        )
        page = svc.domain_read.list_library_items(
            session=session,
            workspace_id=workspace_id,
            connector=connector,
            trust=trust,
            lifecycle=lifecycle,
            limit=limit,
            cursor=cursor,
            exclude_trust_levels=exclude_trust_levels,
        )
        return {
            "data": page.items,
            "next_cursor": page.next_cursor,
            "total": page.total,
            "has_more": page.has_more,
        }


def check_rest_restricted_library_access(
    actor: ActorContext,
    *,
    restricted: bool,
) -> None:
    if restricted and not can_read_restricted_library_items(actor):
        raise RejectedByPolicyError(
            "restricted Library item access denied",
            error_code="restricted_library_denied",
        )


LIBRARY_UPLOAD_SETTINGS_LINK = "/settings?section=library"
LIBRARY_UPLOAD_OPERATION_HEADER = "X-DTX-Library-Upload-Operation-Id"
# Multipart framing around the file: boundaries, part headers (the filename
# included) and the optional parent_node_id field.
LIBRARY_UPLOAD_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
LIBRARY_STORAGE_RETRY_AFTER_SECONDS = 1
TEXT_LIBRARY_EXTENSIONS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".csv": "csv",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@router.get(
    "/{workspace_id}/library/capabilities",
    response_model=LibraryCapabilitiesResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_library_capabilities(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    control_session_factory: SessionFactoryDep,
):
    # `library_policy` is a `workspace` column, so it comes off CONTROL here
    # for the same reason `get_settings_policies` reads it there: one policy
    # value, one plane. Projecting it is pure computation over the loaded row.
    try:
        with control_session_factory() as session:
            workspace = svc.workspace_read.get_workspace(session, workspace_id)
            return {"data": svc.library_capabilities.project(workspace.library_policy)}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


def library_upload_body_limit(settings: Settings) -> BodyLimit:
    """Pre-routing body bound for the two Library upload routes.

    The instance hard cap bounds every per-workspace and per-format limit, so
    a body larger than the cap plus multipart framing cannot be an acceptable
    upload; it is refused before multipart parsing and authentication.
    """

    cap = settings.library_upload_hard_cap_bytes
    return BodyLimit(
        limit_bytes=cap + LIBRARY_UPLOAD_MULTIPART_OVERHEAD_BYTES,
        error_code="library_upload_too_large",
        message=(
            f"Library uploads must be {format_library_upload_limit(cap)} or smaller."
        ),
        extra={"limit_bytes": cap, "settings_link": LIBRARY_UPLOAD_SETTINGS_LINK},
    )


_LIBRARY_UPLOAD_ERROR_RESPONSES = upsert_rest_responses(
    LibraryItemDetailResponse,
    "Idempotent replay of a previous upload operation.",
) | {
    413: {
        "model": RESTErrorBody,
        "description": "Library upload exceeds the effective file-size limit.",
    },
    503: {
        "model": RESTErrorBody,
        "description": "Library source storage is temporarily unavailable.",
    },
}


@router.post(
    "/{workspace_id}/library/items",
    response_model=LibraryItemDetailResponse,
    status_code=201,
    responses=_LIBRARY_UPLOAD_ERROR_RESPONSES,
)
@request_body_limit(library_upload_body_limit)
async def create_library_item(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    file: Annotated[UploadFile, File()],
    response: Response,
    request: Request,
    parent_node_id: Annotated[UUID | None, Form()] = None,
    upload_operation_id: Annotated[
        UUID | None,
        Header(alias=LIBRARY_UPLOAD_OPERATION_HEADER),
    ] = None,
):
    """P4.2: always a brand-new item -- no filename-derived upsert. Two
    uploads sharing a filename become two separate items; replaying the same
    ``upload_operation_id`` returns the same item instead of a duplicate."""

    return await _register_library_upload(
        svc=svc,
        workspace_id=workspace_id,
        actor=actor,
        session_factory=session_factory,
        file=file,
        response=response,
        request=request,
        upload_operation_id=upload_operation_id,
        existing_item_id=None,
        parent_node_id=parent_node_id,
    )


@router.post(
    "/{workspace_id}/library/items/{item_id}/versions",
    response_model=LibraryItemDetailResponse,
    status_code=201,
    responses=_LIBRARY_UPLOAD_ERROR_RESPONSES
    | {
        404: {
            "model": RESTErrorBody,
            "description": "Library item not found in this workspace.",
        },
    },
)
@request_body_limit(library_upload_body_limit)
async def add_library_item_version(
    svc: ServicesDep,
    workspace_id: UUID,
    item_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    file: Annotated[UploadFile, File()],
    response: Response,
    request: Request,
    upload_operation_id: Annotated[
        UUID | None,
        Header(alias=LIBRARY_UPLOAD_OPERATION_HEADER),
    ] = None,
):
    """P4.2: explicitly adds a version to ``item_id`` -- never a filename
    lookup. Identical bytes already on this item are reused (200); new bytes
    become a new version (201)."""

    return await _register_library_upload(
        svc=svc,
        workspace_id=workspace_id,
        actor=actor,
        session_factory=session_factory,
        file=file,
        response=response,
        request=request,
        upload_operation_id=upload_operation_id,
        existing_item_id=item_id,
        parent_node_id=None,
    )


@router.get(
    "/{workspace_id}/library/items/{item_id}/versions",
    response_model=LibraryItemVersionListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_library_item_versions(
    svc: ServicesDep,
    workspace_id: UUID,
    item_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            # require_visible_library_item (inside list_library_item_versions)
            # already denies a restricted-trust item to an actor without
            # elevated access -- see LibraryContentService's equivalent gate.
            payload = svc.domain_read.list_library_item_versions(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item_id,
                actor=actor,
            )
            return {"data": payload}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


async def _register_library_upload(
    *,
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    session_factory: WorkspaceSessionFactoryDep,
    file: Annotated[UploadFile, File()],
    response: Response,
    request: Request,
    upload_operation_id: UUID | None,
    existing_item_id: UUID | None,
    parent_node_id: UUID | None,
):
    operation_id = upload_operation_id or uuid4()
    response.headers[LIBRARY_UPLOAD_OPERATION_HEADER] = str(operation_id)
    settings = svc.settings
    if settings is None or not settings.library_blob_ingest_enabled:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code="library_blob_ingest_disabled",
                message="Library binary upload is disabled for this deployment.",
                status_code=503,
            ),
            operation_id,
        )
    upload_stage = "library_upload_preflight"
    try:
        original_filename = normalize_library_filename(file.filename or "upload")
        with session_factory() as session:
            workspace = svc.workspace_read.get_workspace(session, workspace_id)
            if parent_node_id is not None:
                # Validate an explicit placement before reading or publishing
                # attacker-controlled bytes. Registration repeats this check
                # inside its write transaction, while this preflight prevents
                # an invalid parent from amplifying orphaned
                # content-addressed blobs. Only the new-item endpoint ever
                # passes a parent_node_id -- an existing item already has one.
                resolve_tree_parent(
                    session=session,
                    workspace_id=workspace_id,
                    section="library",
                    parent_id=parent_node_id,
                    default_parent_slug="library",
                )
            policy = normalized_library_policy(
                workspace.library_policy,
                family_policy=svc.library_family_policy,
            )
            policy_revision = settings_policy_revision("library", policy)
            builtin_descriptor_overrides = (
                svc.library_family_policy.builtin_descriptor_overrides(
                    policy["builtin_format_overrides"]
                )
            )
            custom_descriptors = svc.library_family_policy.custom_descriptors(
                policy["custom_formats"],
                builtin_format_overrides=policy["builtin_format_overrides"],
            )
    except LibraryFormatError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.code,
                message=exc.safe_message,
                status_code=422,
            ),
            operation_id,
        )
    except ReadServiceError as exc:
        return with_library_upload_operation(
            read_service_error_response(exc), operation_id
        )
    except OperationalError as exc:
        if not is_transient_library_database_error(exc):
            raise
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )
    except OSError:
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )
    upload_limit = settings.library_upload_hard_cap_bytes
    all_descriptors = (
        *svc.library_format_registry.descriptors(
            builtin_descriptor_overrides=builtin_descriptor_overrides
        ),
        *custom_descriptors,
    )
    format_limits = {
        descriptor.format_id: library_upload_limit_bytes(
            policy,
            format_id=descriptor.format_id,
            instance_hard_cap_bytes=settings.library_upload_hard_cap_bytes,
            family_policy=svc.library_family_policy,
        )
        for descriptor in all_descriptors
    }
    enabled_formats = frozenset(
        descriptor.format_id
        for descriptor in all_descriptors
        if library_format_is_enabled(
            policy,
            descriptor.format_id,
            family_policy=svc.library_family_policy,
        )
    )
    converter_formats = (
        frozenset(svc.library_converter_registry.ready_formats())
        if settings.library_conversion_enabled
        else frozenset()
    )
    converter_max_bytes = {
        descriptor.format_id: svc.library_converter_registry.ready_input_limit(
            descriptor.format_id
        )
        for descriptor in all_descriptors
    }
    try:
        upload_stage = "library_source_admission"
        with session_factory.begin() as session:
            locked_policy: dict | None = None

            def ensure_source_capacity(detection, digest: str, byte_size: int) -> None:
                nonlocal locked_policy
                locked_policy = (
                    svc.library_binary_registration.ensure_source_quota_capacity(
                        session=session,
                        workspace_id=workspace_id,
                        existing_item_id=existing_item_id,
                        upload_operation_id=operation_id,
                        sha256=digest,
                        byte_size=byte_size,
                        expected_policy_revision=policy_revision,
                    )
                )

            accepted = await svc.library_upload_admission_for_session(
                session, workspace_id
            ).admit(
                file,
                workspace_id=workspace_id,
                filename=original_filename,
                declared_media_type=file.content_type,
                max_bytes=upload_limit,
                enabled_formats=enabled_formats,
                converter_formats=converter_formats,
                allow_store_without_converter=policy["allow_store_without_converter"],
                format_max_bytes=format_limits,
                converter_max_bytes=converter_max_bytes,
                custom_descriptors=custom_descriptors,
                builtin_descriptor_overrides=builtin_descriptor_overrides,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                upload_operation_id=operation_id,
                upload_parent_node_id=parent_node_id,
                existing_item_id=existing_item_id,
                before_install=ensure_source_capacity,
            )
            if locked_policy is None:
                raise RuntimeError("Library upload admission skipped policy locking")
            detected_limit = format_limits[accepted.descriptor.format_id]
            converter_limit = converter_max_bytes.get(accepted.descriptor.format_id)
            limits = ConversionLimits(
                max_input_bytes=(
                    min(detected_limit, converter_limit)
                    if converter_limit is not None
                    else detected_limit
                ),
                max_output_bytes=settings.library_conversion_max_output_bytes,
                max_asset_bytes=settings.library_conversion_max_asset_bytes,
                timeout_seconds=settings.library_conversion_timeout_seconds,
            )
            registration = svc.library_binary_registration.register(
                session=session,
                workspace_id=workspace_id,
                accepted=accepted,
                workspace_policy=locked_policy,
                limits=limits,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                upload_operation_id=operation_id,
                existing_item_id=existing_item_id,
                parent_node_id=parent_node_id,
                schedule_optional_processing=False,
            )
            library_item_id = registration.library_item.id
            version_id = registration.version.id
            created_library_item = registration.created_library_item
            created_version = registration.created_version

        # 201 whenever this call actually created something new (a new item,
        # a new version, or both). A pure replay -- same operation id or same
        # content hash on the same item, nothing new created -- is 200.
        if not created_library_item and not created_version:
            response.status_code = 200

        upload_stage = "library_conversion_schedule"
        try:
            with session_factory.begin() as session:
                svc.library_binary_registration.schedule_conversion(
                    session=session,
                    workspace_id=workspace_id,
                    version_id=version_id,
                    workspace_policy=locked_policy,
                    limits=limits,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    allow_conversion=accepted.processing_disposition == "queued",
                )
        except Exception:
            logger.exception(
                "Library optional conversion scheduling failed",
                extra={
                    "stage": "library_conversion_schedule",
                    "safe_error_code": "library_conversion_schedule_failed",
                },
            )

        upload_stage = "library_preview_schedule"
        try:
            with session_factory.begin() as session:
                svc.library_binary_registration.schedule_preview(
                    session=session,
                    workspace_id=workspace_id,
                    version_id=version_id,
                    limits=limits,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                )
        except Exception:
            logger.exception(
                "Library optional Preview scheduling failed",
                extra={
                    "stage": "library_preview_schedule",
                    "safe_error_code": "library_preview_schedule_failed",
                },
            )

        upload_stage = "library_detail_projection"
        with session_factory() as session:
            return {
                "data": svc.domain_read.get_library_item(
                    session=session,
                    workspace_id=workspace_id,
                    library_item_id=library_item_id,
                    actor=actor,
                )
            }
    except LibraryFileEmptyError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.error_code,
                message=exc.safe_message,
                status_code=422,
            ),
            operation_id,
        )
    except LibraryUploadIdempotencyConflictError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.error_code,
                message=exc.safe_message,
                status_code=409,
            ),
            operation_id,
        )
    except LibraryBlobTooLargeError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code="library_upload_too_large",
                message=(
                    "Library uploads must be "
                    f"{format_library_upload_limit(exc.limit_bytes)} or smaller."
                ),
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                extra={
                    "limit_bytes": exc.limit_bytes,
                    "settings_link": LIBRARY_UPLOAD_SETTINGS_LINK,
                },
            ),
            operation_id,
        )
    except LibraryFormatError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.code,
                message=exc.safe_message,
                status_code=422,
            ),
            operation_id,
        )
    except LibraryBlobStorageError:
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )
    except MediaInstallInProgressError:
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )
    except MediaStagingError as exc:
        return with_library_upload_operation(
            media_staging_error_response(exc), operation_id
        )
    except LibraryLogicalQuotaExceededError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.error_code,
                message="The Library logical media quota was exceeded.",
                status_code=422,
                extra={"limit_bytes": exc.limit_bytes},
            ),
            operation_id,
        )
    except LibraryUploadPolicyChangedError as exc:
        return with_library_upload_operation(
            build_rest_error_response(
                error_code=exc.error_code,
                message=str(exc),
                status_code=409,
            ),
            operation_id,
        )
    except ReadServiceError as exc:
        return with_library_upload_operation(
            read_service_error_response(exc), operation_id
        )
    except OperationalError as exc:
        if not is_transient_library_database_error(exc):
            raise
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )
    except OSError:
        return library_storage_temporarily_unavailable_response(
            request=request,
            operation_id=operation_id,
            stage=upload_stage,
        )


def with_library_upload_operation(response: Response, operation_id: UUID) -> Response:
    response.headers[LIBRARY_UPLOAD_OPERATION_HEADER] = str(operation_id)
    return response


def library_storage_temporarily_unavailable_response(
    *,
    request: Request,
    operation_id: UUID,
    stage: str,
) -> Response:
    request_id = getattr(request.state, "request_id", None)
    try:
        rest_request_id = UUID(str(request_id)) if request_id is not None else None
    except ValueError:
        rest_request_id = None
    logger.warning(
        json.dumps(
            {
                "request_id": str(request_id) if request_id is not None else None,
                "stage": stage,
                "error_code": "library_storage_temporarily_unavailable",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    response = build_rest_error_response(
        error_code="library_storage_temporarily_unavailable",
        message="Library source storage is temporarily unavailable; retry later.",
        status_code=503,
        request_id=rest_request_id,
    )
    response.headers["Retry-After"] = str(LIBRARY_STORAGE_RETRY_AFTER_SECONDS)
    return with_library_upload_operation(response, operation_id)


def is_transient_library_database_error(exc: OperationalError) -> bool:
    """Recognize SQLite storage/busy failures without masking schema defects."""

    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    error_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        if primary_code in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_IOERR,
            sqlite3.SQLITE_FULL,
            sqlite3.SQLITE_READONLY,
            sqlite3.SQLITE_CANTOPEN,
        }:
            return True
    message = str(original).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "disk i/o error",
            "database or disk is full",
            "unable to open database file",
            "attempt to write a readonly database",
        )
    )


def clean_upload_filename(filename: str | None) -> str:
    cleaned = (filename or "upload.txt").strip().replace("\\", "/")
    cleaned = cleaned.rsplit("/", 1)[-1].strip()
    return cleaned or "upload.txt"


def library_upload_suffix(filename: str) -> str:
    """Return the lowercase extension, or "" when there is none.

    Extensionless and dot-prefixed names (e.g. ".env") yield "" so they are
    rejected by the upload allow-list instead of silently passing as ".txt".
    """
    stem, separator, suffix = filename.rpartition(".")
    if not separator or not stem:
        return ""
    return f".{suffix.lower()}"


# Registered before the "/{library_item_id}" route so "tombstones" is not parsed
# as a UUID path parameter.
@router.get(
    "/{workspace_id}/library/tombstones",
    response_model=LibraryItemTombstoneListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_library_tombstones(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.library.list_tombstones(
                    session=session,
                    workspace_id=workspace_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/library/items/{library_item_id}",
    response_model=LibraryItemDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_library_item(
    svc: ServicesDep,
    workspace_id: UUID,
    library_item_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            payload = svc.domain_read.get_library_item(
                session=session,
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                actor=actor,
            )
            check_rest_restricted_library_access(
                actor,
                restricted=payload.trust_level in RESTRICTED_LIBRARY_TRUST_LEVELS,
            )
            return {"data": payload}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/library/versions/{version_id}/conversion/retry",
    response_model=LibraryItemDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def retry_library_conversion(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    """Retry the current item's conversion without exposing its queue Job."""

    try:
        with session_factory.begin() as session:
            version = svc.domain_read.get_library_item_version(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
            )
            library_item_id = version.library_item_id
            current = svc.domain_read.require_visible_library_item(
                session=session,
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                actor=actor,
            )
            check_rest_restricted_library_access(
                actor,
                restricted=(current.trust_level in RESTRICTED_LIBRARY_TRUST_LEVELS),
            )
            svc.library_conversion.retry_failed_conversion(
                session=session,
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                version_id=version_id,
                actor_kind=actor.actor_kind,
                actor_id=str(actor.actor_id),
            )
            return {
                "data": svc.domain_read.get_library_item(
                    session=session,
                    workspace_id=workspace_id,
                    library_item_id=library_item_id,
                    actor=actor,
                )
            }
    except LibraryConversionRetryNotFoundError:
        return build_rest_error_response(
            error_code="not_found",
            message="Library item version was not found",
            status_code=404,
        )
    except LibraryConversionRetryNotAvailableError:
        return build_rest_error_response(
            error_code="library_conversion_retry_not_available",
            message="This Library conversion is not available for retry",
            status_code=409,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/library/items/{library_item_id}/references",
    response_model=LibraryItemReferenceListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_library_item_references(
    svc: ServicesDep,
    workspace_id: UUID,
    library_item_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.library.list_item_references(
                    session=session,
                    workspace_id=workspace_id,
                    library_item_id=library_item_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.delete(
    "/{workspace_id}/library/items/{library_item_id}",
    responses=REST_ERROR_RESPONSES,
)
def delete_library_item(
    svc: ServicesDep,
    workspace_id: UUID,
    library_item_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            svc.library.delete_library_item(
                session=session,
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor=actor,
            )
            return {"data": {"id": str(library_item_id), "deleted": True}}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/library/versions/{version_id}",
    response_model=LibraryItemVersionResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_library_item_version(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            payload = svc.domain_read.get_library_item_version(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
            )
            restricted = is_restricted_library_target(
                session=session,
                workspace_id=workspace_id,
                target_type="library_item_version",
                target_id=version_id,
            )
            check_rest_restricted_library_access(
                actor,
                restricted=bool(restricted),
            )
            return {"data": payload}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.head(
    "/{workspace_id}/library/versions/{version_id}/content",
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/library/versions/{version_id}/content",
    responses={
        **REST_ERROR_RESPONSES,
        206: {"description": "A single authorized byte range."},
        304: {"description": "The original content is unchanged."},
        416: {"model": RESTErrorBody},
    },
)
def download_library_original(
    svc: ServicesDep,
    workspace_id: UUID,
    version_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    request: Request,
):
    try:
        with session_factory.begin() as session:
            # Flat lookup (P9.2): the item is workspace-scoped-derived from the
            # version rather than carried in the URL.
            version_payload = svc.domain_read.get_library_item_version(
                session=session,
                workspace_id=workspace_id,
                version_id=version_id,
                actor=actor,
            )
            library_item_id = version_payload.library_item_id
            content = svc.library_content.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                version_id=version_id,
                actor=actor,
                range_header=request.headers.get("range"),
                if_range_header=request.headers.get("if-range"),
            )
            not_modified = content.byte_range is None and if_none_match_matches(
                request.headers.get("if-none-match"), content.etag
            )
            if not not_modified:
                svc.library.audit_service.emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    action="library.original.downloaded",
                    target_kind="library_item_version",
                    target_id=version_id,
                    payload={
                        "library_item_id": str(library_item_id),
                        "source_sha256": content.sha256,
                        "range": content.content_range,
                    },
                )
    except LibraryByteRangeError as exc:
        response = build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=416,
        )
        response.headers["Content-Range"] = f"bytes */{exc.complete_length}"
        response.headers["Accept-Ranges"] = "bytes"
        return response
    except ReadServiceError as exc:
        return read_service_error_response(exc)

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": _library_content_disposition(content.filename),
        "ETag": content.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if not_modified:
        return Response(status_code=304, headers=headers)
    if content.content_range is not None:
        headers["Content-Range"] = content.content_range
    headers["Content-Length"] = str(content.content_length)
    status_code = 206 if content.byte_range is not None else 200
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=content.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _stream_library_original(content),
        status_code=status_code,
        media_type=content.media_type,
        headers=headers,
    )


def _stream_library_original(content):
    with content.blob.open_binary() as source:
        remaining = content.content_length
        if content.byte_range is not None:
            source.seek(content.byte_range.start)
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Stored Library content ended unexpectedly")
            remaining -= len(chunk)
            yield chunk


def _library_content_disposition(
    filename: str,
    *,
    disposition: Literal["attachment", "inline"] = "attachment",
) -> str:
    fallback = filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = fallback.replace('"', "_").replace("\\", "_") or "library.bin"
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get(
    "/{workspace_id}/library/artifacts/{artifact_id}",
    response_model=LibraryArtifactResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_artifact(
    svc: ServicesDep,
    workspace_id: UUID,
    artifact_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            payload = svc.domain_read.get_library_artifact(
                session=session,
                workspace_id=workspace_id,
                artifact_id=artifact_id,
                actor=actor,
            )
            restricted = is_restricted_library_target(
                session=session,
                workspace_id=workspace_id,
                target_type="library_artifact",
                target_id=artifact_id,
            )
            check_rest_restricted_library_access(
                actor,
                restricted=bool(restricted),
            )
            return {"data": payload}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.head(
    "/{workspace_id}/library/artifacts/{artifact_id}/content",
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/library/artifacts/{artifact_id}/content",
    responses={
        **REST_ERROR_RESPONSES,
        206: {"description": "A single authorized artifact byte range."},
        304: {"description": "The artifact content is unchanged."},
        416: {"model": RESTErrorBody},
    },
)
def read_library_artifact_content(
    svc: ServicesDep,
    workspace_id: UUID,
    artifact_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    request: Request,
):
    # P9.2: this transport now also carries what the retired
    # ``preview/attempts/.../artifacts`` and ``preview/semantic-artifacts``
    # routes served -- both applied the isolated Preview sandbox, so every
    # response through this flat family keeps it rather than the app-shell CSP.
    setattr(request.state, CSP_SANDBOXED_DOCUMENT_STATE_KEY, True)
    try:
        with session_factory.begin() as session:
            content = svc.library_content.resolve_artifact(
                session=session,
                workspace_id=workspace_id,
                artifact_id=artifact_id,
                actor=actor,
                range_header=request.headers.get("range"),
                if_range_header=request.headers.get("if-range"),
            )
            not_modified = content.byte_range is None and if_none_match_matches(
                request.headers.get("if-none-match"), content.etag
            )
            if not not_modified:
                svc.library.audit_service.emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    action="library.artifact.read",
                    target_kind="library_artifact",
                    target_id=artifact_id,
                    payload={
                        "library_item_id": str(content.library_item_id),
                        "library_item_version_id": str(content.version_id),
                        "content_sha256": content.sha256,
                        "range": content.content_range,
                    },
                )
    except LibraryByteRangeError as exc:
        response = build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=416,
        )
        response.headers["Content-Range"] = f"bytes */{exc.complete_length}"
        response.headers["Accept-Ranges"] = "bytes"
        return response
    except ReadServiceError as exc:
        return read_service_error_response(exc)

    disposition = (
        "attachment" if content.media_type == "application/octet-stream" else "inline"
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": _library_content_disposition(
            content.filename,
            disposition=disposition,
        ),
        "Cross-Origin-Resource-Policy": "same-origin",
        "ETag": content.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if not_modified:
        return Response(status_code=304, headers=headers)
    if content.content_range is not None:
        headers["Content-Range"] = content.content_range
    headers["Content-Length"] = str(content.content_length)
    status_code = 206 if content.byte_range is not None else 200
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=content.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _stream_library_original(content),
        status_code=status_code,
        media_type=content.media_type,
        headers=headers,
    )


@router.get(
    "/{workspace_id}/agent/categories",
    response_model=AgentCategoryListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_agent_categories(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.domain_read.list_agent_categories(
                    session=session,
                    workspace_id=workspace_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/agent/pages",
    response_model=AgentPageListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_agent_pages(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    category: str | None = None,
    lifecycle_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    with session_factory() as session:
        page = svc.domain_read.list_agent_pages(
            session=session,
            workspace_id=workspace_id,
            category=category,
            lifecycle_status=lifecycle_status,
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


@router.post(
    "/{workspace_id}/agent/pages",
    response_model=AgentPageDraftResultResponse,
    status_code=201,
    responses=upsert_rest_responses(
        AgentPageDraftResultResponse,
        "Draft added to the existing AgentPage with the same slug.",
    ),
)
def create_agent_page_draft(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: AgentPageDraftRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    response: Response,
):
    try:
        with session_factory.begin() as session:
            # The service reuses an existing AgentPage matched by slug, so
            # probe before the write to report 201 only for real creation.
            page_existed = svc.agent_page.page_exists(
                session, workspace_id, request_body.slug
            )
            result = svc.agent_page.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=request_body,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor_provenance=actor.provenance,
            )
            if page_existed:
                response.status_code = 200
            return {"data": result}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/agent/pages/{agent_page_id}",
    response_model=AgentPageDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_agent_page(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    version: str = "active",
):
    try:
        with session_factory() as session:
            if svc.domain_read.agent_page_requires_sensitive_access(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=agent_page_id,
            ):
                _require_sensitive_agent_page_access(
                    actor, access, resource_workspace_id=workspace_id
                )
            data = svc.domain_read.read_agent_page(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=agent_page_id,
                version_selector=version,
                actor=actor,
            )
            return {"data": data}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/agent/pages/{agent_page_id}/diff",
    response_model=PageVersionDiffResponse,
    responses=REST_ERROR_RESPONSES,
)
def diff_agent_page_versions(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    from_version_id: UUID,
    to_version_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            # A diff exposes body content, so use the same body-only owner gate as the
            # detail read for sensitive AgentPage categories.
            _gate_sensitive_agent_page_body(
                svc, session, actor, access, workspace_id, agent_page_id
            )
            return {
                "data": svc.agent_page.diff_versions(
                    session=session,
                    workspace_id=workspace_id,
                    agent_page_id=agent_page_id,
                    from_version_id=from_version_id,
                    to_version_id=to_version_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/agent/pages/{agent_page_id}/draft",
    response_model=AgentPageDraftResultResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_agent_page_draft(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    request_body: AgentPageDraftRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.agent_page.write_draft(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_provenance=actor.provenance,
                    agent_page_id=agent_page_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/agent/pages/{agent_page_id}/restore",
    response_model=AgentPageDraftResultResponse,
    responses=REST_ERROR_RESPONSES,
)
def restore_agent_page_version(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    request_body: AgentPageRestoreRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.agent_page.restore_version(
                    session=session,
                    workspace_id=workspace_id,
                    agent_page_id=agent_page_id,
                    version_id=request_body.version_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_provenance=actor.provenance,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/agent/pages/{agent_page_id}/activate",
    response_model=AgentPageDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def activate_agent_page(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    request_body: AgentPageActivateRequest,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            activation = svc.agent_page.activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=agent_page_id,
                version_id=request_body.version_id,
                reason=request_body.reason,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor_role=access.role,
            )
            return {
                "data": svc.domain_read.read_agent_page(
                    session=session,
                    workspace_id=workspace_id,
                    agent_page_id=activation.agent_page_id,
                    version_selector="active",
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/context/pages/{page_id}/activity",
    response_model=ActivityListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_context_page_activity(
    svc: ServicesDep,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    period: str = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    # Same two-plane split as list_activity above: the page's own history is
    # workspace-plane, the human names behind it are identity-plane `users`.
    deferred_identity = DeferredIdentityLabels()
    with session_factory() as session:
        if not context_page_id_visible(
            session, page_id, actor, workspace_id=workspace_id
        ):
            return read_service_error_response(
                ReadNotFoundError("context page not found")
            )
        data = svc.activity.list_activity(
            session=session,
            workspace_id=workspace_id,
            period=period,
            lane="history",
            limit=limit,
            cursor=cursor,
            audience_for=actor.user_id,
            actor_role=access.role,
            actor=actor,
            page_id=page_id,
            deferred_identity=deferred_identity,
        )
    with control_session_factory() as identity_session:
        deferred_identity.apply(identity_session)
    return {"data": data}


@router.get(
    "/{workspace_id}/context/pages/{page_id}/neighbors",
    response_model=GraphNeighborsResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_context_page_neighbors(
    svc: ServicesDep,
    workspace_id: UUID,
    page_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    format: Literal["json", "mermaid"] = "json",
):
    try:
        with session_factory() as session:
            return {
                "data": svc.graph_service.context_page_neighbors_panel(
                    session,
                    workspace_id,
                    page_id=page_id,
                    actor=actor,
                    include_mermaid=format == "mermaid",
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/agent/bundles/preview",
    response_model=BuildBundleResponse,
    responses=REST_ERROR_RESPONSES,
)
def build_bundle_preview(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: BuildBundleRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.bundle.build(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor=actor,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/agent/bundles/{bundle_id}",
    response_model=BuildBundleResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_bundle(
    svc: ServicesDep,
    workspace_id: UUID,
    bundle_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.bundle.get_bundle(
                    session=session,
                    workspace_id=workspace_id,
                    bundle_id=bundle_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/ask/sessions",
    response_model=AskSessionListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_ask_sessions(
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    ask_service: AskServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    with session_factory() as session:
        return {
            "data": ask_service.list_sessions(
                session=session,
                workspace_id=workspace_id,
                limit=limit,
                cursor=cursor,
            )
        }


@router.post(
    "/{workspace_id}/ask/sessions",
    response_model=AskSessionDetailResponse,
    responses=REST_ERROR_RESPONSES,
    status_code=201,
)
def create_ask_session(
    workspace_id: UUID,
    request_body: AskSessionCreate,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    ask_service: AskServiceDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": ask_service.create_session(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/ask/sessions/{ask_session_id}",
    response_model=AskSessionDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_ask_session(
    workspace_id: UUID,
    ask_session_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    ask_service: AskServiceDep,
):
    try:
        with session_factory() as session:
            return {
                "data": ask_service.get_session(
                    session=session,
                    workspace_id=workspace_id,
                    ask_session_id=ask_session_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/ask/sessions/{ask_session_id}/turns",
    response_model=AskTurnDetailResponse,
    responses=REST_ERROR_RESPONSES,
)
def create_ask_turn(
    workspace_id: UUID,
    ask_session_id: UUID,
    request_body: AskTurnCreate,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    ask_service: AskServiceDep,
):
    try:
        with session_factory.begin() as session:
            detail, warning_codes = ask_service.create_turn(
                session=session,
                workspace_id=workspace_id,
                ask_session_id=ask_session_id,
                request=request_body,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor=actor,
            )
            return {
                "data": detail,
                "envelope": {"warnings": advisory_warnings(warning_codes)},
            }
    except InvalidUserMessageError as exc:
        return build_rest_error_response(
            error_code="invalid_user_message",
            message=str(exc),
            status_code=422,
        )
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/ask/turns/{ask_turn_id}/promote",
    response_model=ContextIngressProposalResponse,
    responses=REST_ERROR_RESPONSES,
    status_code=201,
)
def promote_ask_turn(
    svc: ServicesDep,
    workspace_id: UUID,
    ask_turn_id: UUID,
    request_body: AskTurnPromoteRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.context_ingress.promote_ask_turn(
                    session=session,
                    workspace_id=workspace_id,
                    ask_turn_id=ask_turn_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/context/ingress-proposals",
    response_model=ContextIngressProposalListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_context_ingress_proposals(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    status: str | None = None,
    source_kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with session_factory() as session:
        return {
            "data": svc.context_ingress.list_proposals(
                session=session,
                workspace_id=workspace_id,
                status=status,
                source_kind=source_kind,
                limit=limit,
                actor=actor,
            )
        }


@router.get(
    "/{workspace_id}/context/ingress-proposals/{proposal_id}",
    response_model=ContextIngressProposalResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_context_ingress_proposal(
    svc: ServicesDep,
    workspace_id: UUID,
    proposal_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.context_ingress.get_proposal(
                    session=session,
                    workspace_id=workspace_id,
                    proposal_id=proposal_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/context/ingress-proposals/{proposal_id}/accept",
    response_model=ContextIngressAcceptResponse,
    responses=REST_ERROR_RESPONSES,
)
def accept_context_ingress_proposal(
    svc: ServicesDep,
    workspace_id: UUID,
    proposal_id: UUID,
    request_body: ContextIngressAcceptRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.context_ingress.accept_proposal(
                    session=session,
                    workspace_id=workspace_id,
                    proposal_id=proposal_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/context/ingress-proposals/{proposal_id}/reject",
    response_model=ContextIngressProposalResponse,
    responses=REST_ERROR_RESPONSES,
)
def reject_context_ingress_proposal(
    svc: ServicesDep,
    workspace_id: UUID,
    proposal_id: UUID,
    request_body: ContextIngressRejectRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.context_ingress.reject_proposal(
                    session=session,
                    workspace_id=workspace_id,
                    proposal_id=proposal_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/feedback",
    response_model=SubmitFeedbackResponse,
    responses=REST_ERROR_RESPONSES,
)
def submit_feedback(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: SubmitFeedbackRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.feedback.submit_feedback(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/feedback",
    response_model=FeedbackListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_feedback(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    status: str | None = None,
    target_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    with session_factory() as session:
        return {
            "data": svc.feedback.list_feedback(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
                status=status,
                target_type=target_type,
                limit=limit,
            )
        }


@router.get(
    "/{workspace_id}/reviews",
    response_model=ReviewItemListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_reviews(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    status: str | None = None,
    risk_level: str | None = None,
    scope: Literal["to_me", "from_me", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    target_page_id: UUID | None = None,
    include_deferred_reconciliations: bool = False,
):
    try:
        with session_factory.begin() as session:
            # Sub-editor roles only ever see their personal queue, never the
            # workspace-wide review state — a client-supplied scope="all" is
            # clamped to to_me for viewers (Codex #6).
            effective_scope = scope
            if scope == "all" and not role_allows(access.role, "editor"):
                effective_scope = "to_me"
            return {
                "data": svc.review.list_reviews(
                    session=session,
                    workspace_id=workspace_id,
                    status=status,
                    risk_level=risk_level,
                    limit=limit,
                    cursor=cursor,
                    target_page_id=target_page_id,
                    include_deferred_reconciliations=(include_deferred_reconciliations),
                    # Principal is server-derived from the authenticated actor,
                    # never client-supplied (Risk 2 / F1). to_me/from_me need a
                    # concrete user; non-user actors (mcp_client/system) have
                    # user_id None, so the scope predicate is simply inert.
                    audience_for=actor.user_id,
                    scope=effective_scope,
                    actor_role=access.role,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/reviews/counts",
    response_model=ReviewCountsResponse,
    responses=REST_ERROR_RESPONSES,
)
def count_reviews(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    risk_level: str | None = None,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.review.count_reviews(
                    session=session,
                    workspace_id=workspace_id,
                    risk_level=risk_level,
                    audience_for=actor.user_id,
                    actor_role=access.role,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/reconciliations/discard-bulk",
    response_model=ContextReconciliationBulkDiscardResponse,
    responses=REST_ERROR_RESPONSES,
)
def discard_reconciliations_bulk(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: ContextReconciliationBulkDiscardRequest,
    actor: RestActorDep,
    access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    del access
    try:
        with session_factory.begin() as session:
            result = svc.review.discard_reconciliations_bulk(
                session=session,
                workspace_id=workspace_id,
                review_item_ids=request_body.review_item_ids,
                reason=request_body.reason,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
            )
        return {"data": result}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/decide-bulk",
    response_model=ReviewBulkDecisionResponse,
    responses=REST_ERROR_RESPONSES,
)
def decide_reviews_bulk(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: ReviewBulkDecisionRequest,
    actor: RestActorDep,
    access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            if request_body.review_item_ids is not None:
                target_ids = request_body.review_item_ids
            else:
                filters = request_body.filters
                target_ids = svc.review.bulk_review_ids(
                    session=session,
                    workspace_id=workspace_id,
                    status=filters.status if filters else None,
                    risk_level=filters.risk_level if filters else None,
                    audience_for=actor.user_id,
                    scope=filters.scope if filters else "all",
                    actor_user_id=actor.user_id,
                    actor_role=access.role,
                )
    except ReadServiceError as exc:
        return read_service_error_response(exc)

    approved: list[UUID] = []
    skipped: list[dict[str, object]] = []
    seen: set[UUID] = set()

    if request_body.dry_run:
        # L7: the preview only needs decidability, not the full review detail, so
        # evaluate every target in ONE read-only session via the lightweight
        # can_decide_review check — instead of a per-item write transaction plus
        # a full get_review()/_detail() load (decisions/evidence/change-set/
        # chat-owner). Output shape is unchanged.
        with session_factory() as session:
            for review_item_id in target_ids:
                if review_item_id in seen:
                    continue
                seen.add(review_item_id)
                try:
                    if svc.review.can_decide_review(
                        session=session,
                        workspace_id=workspace_id,
                        review_item_id=review_item_id,
                        actor_user_id=actor.user_id,
                        actor_role=access.role,
                    ):
                        approved.append(review_item_id)
                    else:
                        skipped.append(
                            {
                                "id": review_item_id,
                                "reason": "not eligible for approval",
                            }
                        )
                except ReadServiceError as exc:
                    skipped.append(
                        {
                            "id": review_item_id,
                            "reason": str(exc) or getattr(exc, "error_code", "skipped"),
                        }
                    )
        return {"data": {"approved": approved, "skipped": skipped}}

    for review_item_id in target_ids:
        if review_item_id in seen:
            continue
        seen.add(review_item_id)
        # Each real decision runs in its own write transaction so one failure
        # does not roll back the others.
        try:
            with session_factory.begin() as session:
                svc.review.apply_decision(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=review_item_id,
                    request=ReviewDecisionRequest(decision_type="approve"),
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_user_id=actor.user_id,
                    actor_role=access.role,
                    actor_provenance=actor.provenance,
                )
            approved.append(review_item_id)
        except ReadServiceError as exc:
            skipped.append(
                {
                    "id": review_item_id,
                    "reason": str(exc) or getattr(exc, "error_code", "skipped"),
                }
            )
    return {"data": {"approved": approved, "skipped": skipped}}


@router.get(
    "/{workspace_id}/reviews/decisions/recent",
    response_model=ReviewDecisionRecentListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_recent_review_decisions(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    try:
        with session_factory.begin() as session:
            scoped_actor = replace(
                actor,
                workspace_id=workspace_id,
                workspace_role=access.role,
                role=access.role,
            )
            return {
                "data": svc.review.list_recent_decisions(
                    session=session,
                    workspace_id=workspace_id,
                    limit=limit,
                    audience_for=actor.user_id,
                    actor_role=access.role,
                    actor=scoped_actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/reviews/{review_item_id}",
    response_model=ReviewItemResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_review(
    svc: ServicesDep,
    workspace_id: UUID,
    review_item_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            scoped_actor = replace(
                actor,
                workspace_id=workspace_id,
                workspace_role=access.role,
                role=access.role,
            )
            return {
                "data": svc.review.get_review(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=review_item_id,
                    actor_user_id=actor.user_id,
                    actor_role=access.role,
                    actor=scoped_actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/sweep-auto-approvals",
    response_model=ReviewAutoApprovalSweepResponse,
    responses=REST_ERROR_RESPONSES,
)
def sweep_review_auto_approvals(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceAdminAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            approved = svc.review.sweep_auto_approve_reviews(
                session=session,
                workspace_id=workspace_id,
            )
            return {"data": {"approved": approved}}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/{review_item_id}/decision",
    response_model=ReviewItemResponse,
    responses=REST_ERROR_RESPONSES,
)
def decide_review(
    svc: ServicesDep,
    workspace_id: UUID,
    review_item_id: UUID,
    request_body: ReviewDecisionRequest,
    actor: RestActorDep,
    access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            scoped_actor = replace(
                actor,
                workspace_id=workspace_id,
                workspace_role=access.role,
                role=access.role,
            )
            return {
                "data": svc.review.apply_decision(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=review_item_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_user_id=actor.user_id,
                    actor_role=access.role,
                    actor_provenance=actor.provenance,
                    actor=scoped_actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/{review_item_id}/claim",
    response_model=ReviewItemResponse,
    responses=REST_ERROR_RESPONSES,
)
def claim_review(
    svc: ServicesDep,
    workspace_id: UUID,
    review_item_id: UUID,
    actor: RestActorDep,
    access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            scoped_actor = replace(
                actor,
                workspace_id=workspace_id,
                workspace_role=access.role,
                role=access.role,
            )
            return {
                "data": svc.review.claim_review(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=review_item_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor_role=access.role,
                    actor=scoped_actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/reviews/{review_item_id}/release",
    response_model=ReviewItemResponse,
    responses=REST_ERROR_RESPONSES,
)
def release_review(
    svc: ServicesDep,
    workspace_id: UUID,
    review_item_id: UUID,
    actor: RestActorDep,
    access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            scoped_actor = replace(
                actor,
                workspace_id=workspace_id,
                workspace_role=access.role,
                role=access.role,
            )
            return {
                "data": svc.review.release_review(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=review_item_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=scoped_actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/activity",
    response_model=ActivityListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_activity(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    period: str = "today",
    lane: str | None = None,
    sub_tab: str | None = None,
    # Constrained so a typo (e.g. kind=workspce) is a 422, not a silent
    # fall-through to unfiltered "all"; also surfaces the allowed values in the
    # generated client. "mcp" is expressed via sub_tab, not kind.
    kind: Literal["all", "workspace", "system", "review_decision"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    # Two planes, resolved in order, never nested -- the same split (and the
    # same ordering, for the same reason: two SQLite IMMEDIATE transactions on
    # one file in the single-database profile) as the Context-media orphan
    # listing below and as backend/api/deps.py's workspace_access. Every row of
    # the feed is workspace-plane; the display name behind a human actor id is
    # `users`, which is identity plane (plan §2), so the workspace session
    # deliberately does not read it: it records which labels it could not
    # produce, and they are filled in afterwards on the control session.
    deferred_identity = DeferredIdentityLabels()
    with session_factory() as session:
        data = svc.activity.list_activity(
            session=session,
            workspace_id=workspace_id,
            period=period,
            lane=lane,
            sub_tab=sub_tab,
            kind=kind,
            limit=limit,
            cursor=cursor,
            audience_for=actor.user_id,
            actor_role=access.role,
            actor=actor,
            deferred_identity=deferred_identity,
        )
    with control_session_factory() as identity_session:
        deferred_identity.apply(identity_session)
    return {"data": data}


@router.get(
    "/{workspace_id}/activity/feed",
    response_model=ActivityFeedResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_activity_feed(
    request: Request,
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    period: Literal["today", "yesterday", "week", "month", "all"] = "today",
    kind: Literal["all", "workspace", "mcp", "system", "review_decision"] = "all",
    subtype: str = "all",
    lane: Literal["pending", "advisory", "history"] | None = None,
    group_reads: bool = True,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    cursor: str | None = None,
):
    actor = replace(
        actor,
        workspace_id=workspace_id,
        workspace_role=access.role,
        role=access.role,
        scopes=access.granted_scopes
        if access.granted_scopes is not None
        else actor.scopes,
    )
    deferred_identity = DeferredIdentityLabels()
    service = ActivityFeedService(
        svc.activity, cursor_secret=request.app.state.settings.effective_session_secret
    )
    try:
        with session_factory() as session:
            data = service.list_feed(
                session,
                workspace_id,
                actor=actor,
                actor_role=access.role,
                period=period,
                kind=kind,
                subtype=subtype,
                lane=lane,
                group_reads=group_reads,
                limit=limit,
                cursor=cursor,
                deferred_identity=deferred_identity,
            )
        # Never nest workspace and identity sessions. Projected actor models
        # retain the deferred label targets through event/group serialization.
        with control_session_factory() as identity_session:
            deferred_identity.apply(identity_session)
        return {"data": data}
    except InvalidActivityTokenError as exc:
        return build_rest_error_response(
            "invalid_activity_token",
            str(exc),
            status_code=400,
        )


@router.get(
    "/{workspace_id}/activity/read-group",
    response_model=ActivityReadGroupResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_activity_read_group(
    request: Request,
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
    token: Annotated[str, Query(min_length=1, max_length=16000)],
):
    actor = replace(
        actor,
        workspace_id=workspace_id,
        workspace_role=access.role,
        role=access.role,
        scopes=access.granted_scopes
        if access.granted_scopes is not None
        else actor.scopes,
    )
    deferred_identity = DeferredIdentityLabels()
    service = ActivityFeedService(
        svc.activity, cursor_secret=request.app.state.settings.effective_session_secret
    )
    try:
        with session_factory() as session:
            data = service.read_group(
                session,
                workspace_id,
                actor=actor,
                actor_role=access.role,
                token=token,
                deferred_identity=deferred_identity,
            )
        with control_session_factory() as identity_session:
            deferred_identity.apply(identity_session)
        return {"data": data}
    except InvalidActivityTokenError as exc:
        return build_rest_error_response(
            "invalid_activity_token",
            str(exc),
            status_code=400,
        )


@router.get(
    "/{workspace_id}/jobs",
    response_model=JobListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_jobs(
    request: Request,
    svc: ServicesDep,
    workspace_id: UUID,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    status: JobStatus | None = None,
    job_type: str | None = None,
    # "Problems only": terminal failures + jobs running past their worker
    # lease (a crashed worker's job, pending the next reclaim sweep) -- the
    # two things D-6 asks this view to surface without a raw status filter.
    problems_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    lease_timeout_seconds = request.app.state.settings.worker_lease_timeout_seconds
    with session_factory() as session:
        return {
            "data": svc.job_read.list_workspace_jobs(
                session=session,
                workspace_id=workspace_id,
                status=status,
                job_type=job_type,
                problems_only=problems_only,
                limit=limit,
                cursor=cursor,
                lease_timeout_seconds=lease_timeout_seconds,
            )
        }


@router.get(
    "/{workspace_id}/context/media/orphans",
    response_model=ContextMediaOrphanListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_context_media_orphans(
    workspace_id: UUID,
    access: WorkspaceAdminAccessDep,
    actor: RestActorDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    # Epic-2 D-7. Kept as a comment, not a docstring: FastAPI publishes a
    # route docstring as this route's `description` in the unauthenticated
    # /openapi.json. (260912 review: the earlier version of this comment said
    # "kept out of the docstring" and then wrote one anyway two lines below --
    # a comment isn't a statement, so that text stayed the docstring and
    # shipped in the public schema. get_settings_policies chose the right
    # shape; this route now matches it.)
    #
    # Every current Context-media orphan for this workspace, regardless of
    # age: visibility is not retention-gated -- only the automatic
    # ``context_media_orphan_sweep`` job is.
    #
    # Workspace-admin scope, unlike the viewer-readable job listing: the rows
    # carry asset filenames and uploader identities, so this is storage
    # housekeeping rather than general status visibility.
    with session_factory() as session:
        items = list_context_media_orphans(
            session,
            workspace_id=workspace_id,
            actor=replace(
                actor,
                workspace_id=workspace_id,
                role=access.role,
                workspace_role=access.role,
            ),
        )
    # The uploader label is resolved on the CONTROL session: `users` is an
    # identity-plane table (plan §2), so the workspace-plane listing above
    # deliberately does not touch it. Same directory-then-route split the
    # media access policy already uses.
    #
    # "Deliberately" describes THIS route and the rule, not the whole file. The
    # rule -- `users` is control-plane -- is uniform, and the two Activity
    # routes above now follow it too (via DeferredIdentityLabels). Other routes
    # in this file still resolve identity labels inside a service on the
    # workspace session (list_changes, list_recent_review_decisions); those are
    # recorded as UNFIXED, with the full site list, in
    # tests/static/test_workspace_plane_boundary.py -- FINDING 3 of this file's
    # SESSION_FACTORY_DEP_ALLOWLIST entry. Do not read this comment as
    # evidence that the file as a whole is split-clean.
    with control_session_factory() as control_session:
        names = resolve_actor_display_names(
            control_session,
            ((item.created_by_actor_kind, item.created_by_actor_id) for item in items),
        )
    return {
        "data": {
            "items": [
                orphan_read_projection(
                    item,
                    uploaded_by_display_name=names[
                        (item.created_by_actor_kind, item.created_by_actor_id)
                    ],
                )
                for item in items
            ],
        }
    }


@router.get(
    "/{workspace_id}/context/media/attachments",
    response_model=ContextMediaAttachmentListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_context_media_attachments(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    control_session_factory: SessionFactoryDep,
    scope: Annotated[Literal["mine", "workspace"], Query()] = "mine",
    usage: Annotated[Literal["all", "used", "unused"], Query()] = "all",
    orphan_only: bool = False,
    page_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    # Kept as a comment, not a docstring: FastAPI publishes a route docstring
    # as this route's `description` in the unauthenticated /openapi.json, and
    # the P8.4 mask this comment describes is exactly the kind of internal a
    # public schema should not carry. (260912 review: the earlier version of
    # this comment claimed to keep the text out of the docstring and then
    # wrote one anyway two lines below -- the third route in this file with
    # that same "comment, then docstring" contradiction, all three publishing
    # confirmed in openapi.json before this fix.)
    #
    # Context attachments in one workspace, newest first.
    #
    # ``scope=mine`` is the default and needs only viewer membership: it
    # discloses nothing the caller did not upload themselves, and defaulting
    # to it keeps the contract role-independent. ``scope=workspace`` names
    # other people's files and uploaders, so it carries the same owner gate
    # the orphan listing established.
    #
    # An owner's "every attachment" is every attachment NOT privately staged
    # by another author right now. An open ``editing`` row is private by
    # identity rather than by role, so the P8.4 mask applies to owners too --
    # see ``list_workspace_usage``.
    #
    # ``orphan_only`` is not ``usage=unused``: unused still contains assets an
    # open editing version protects, while orphan_only applies the sweep's own
    # predicate, so what this filter shows is exactly what the sweep reclaims.

    if scope == "workspace" and not role_allows(access.role, "owner"):
        return build_rest_error_response(
            error_code="workspace_role_insufficient",
            message="Listing every uploader's attachments requires the owner role.",
            status_code=403,
        )
    viewer = ActorIdentity(actor.actor_kind, str(actor.actor_id))
    usage_state = None
    if usage == "used":
        usage_state = UsageState.ACTIVE
    elif usage == "unused":
        usage_state = UsageState.UNUSED
    # `has_more`/`next_cursor` come from what SQL saw, not from `len(page)`:
    # the `usage` filter above runs in Python (`usage_state` has no SQL
    # column, see list_workspace_usage's WorkspaceUsagePage), so a page whose
    # filter kept few or zero rows can still have more unseen rows behind it.
    # Computing paging state from the filtered length here silently truncated
    # a filtered listing (260912 review) -- this route no longer computes it
    # at all, only forwards what the use case already fixed.
    page, has_more, next_after = svc.context_media.list_workspace_assets(
        workspace_id=workspace_id,
        viewer=viewer,
        viewer_role=access.role,
        created_by=viewer if scope == "mine" else None,
        page_id=page_id,
        orphan_only=orphan_only,
        filename_contains=q,
        after=decode_attachment_cursor(cursor),
        limit=limit,
        usage=usage_state,
    )
    # A page can be empty while `next_cursor` is non-null -- the window SQL
    # fetched was entirely filtered out by `usage`, but rows remain beyond
    # it. That is correct here, not a bug: see the WorkspaceUsagePage/
    # ListWorkspaceMedia docstrings for why the old "no cursor for an empty
    # page" invariant is exactly what caused the truncation.
    next_cursor = (
        encode_attachment_cursor(next_after[0], next_after[1])
        if has_more and next_after is not None
        else None
    )
    # The identity-plane read runs only after the workspace session above has
    # closed: `users` is control-plane, and holding both open puts two
    # IMMEDIATE transactions on one SQLite file. Same split as the orphan
    # listing directly above.
    with control_session_factory() as control_session:
        retention_policy = read_context_media_policy(
            control_session,
            workspace_id,
            svc.settings_svc.orphan_retention_hours,
        )
        names = resolve_actor_display_names(
            control_session,
            (
                (item.asset.asset.created_by.kind, str(item.asset.asset.created_by.id))
                for item in page
            ),
        )
    caller_may_delete = role_allows(access.role, "editor")
    return {
        "data": {
            "items": [
                attachment_read_projection(
                    item,
                    workspace_id=workspace_id,
                    uploaded_by_display_name=names[
                        (
                            item.asset.asset.created_by.kind,
                            str(item.asset.asset.created_by.id),
                        )
                    ],
                    viewer_actor_kind=actor.actor_kind,
                    viewer_actor_id=str(actor.actor_id),
                    caller_may_delete=caller_may_delete,
                )
                for item in page
            ],
            "next_cursor": next_cursor,
            "scope": scope,
            "retention_policy": retention_policy.metadata(),
        }
    }


@router.get(
    "/{workspace_id}/lint/findings",
    response_model=LintFindingListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_lint_findings(
    svc: ServicesDep,
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    severity: str | None = None,
    rule: str | None = None,
    status: str | None = None,
    layer: str | None = None,
    finding_id: UUID | None = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: str | None = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=100)] = None,
):
    numeric_pagination = page is not None or page_size is not None
    if numeric_pagination and (limit is not None or cursor is not None):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "validation_failed",
                "message": "page/page_size cannot be combined with limit/cursor",
            },
        )
    with session_factory() as session:
        return {
            "data": svc.lint.list_findings(
                session=session,
                workspace_id=workspace_id,
                severity=severity,
                rule=rule,
                status=status,
                layer=layer,
                finding_id=finding_id,
                limit=limit,
                cursor=cursor,
                page=page,
                page_size=page_size,
                actor=actor,
            )
        }


@router.get(
    "/{workspace_id}/coverage-gaps",
    response_model=CoverageGapListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_coverage_gaps(
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    status: str | None = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    with session_factory() as session:
        return {
            "data": list_coverage_gaps(
                session,
                workspace_id=workspace_id,
                status=status,
                limit=limit,
                actor=actor,
            )
        }


@router.get(
    "/{workspace_id}/lint/rules",
    response_model=LintRuleListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_lint_rules(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    with session_factory() as session:
        return {"data": svc.lint.list_rules(session, workspace_id)}


@router.post(
    "/{workspace_id}/lint/format",
    response_model=LintFormatResponse,
    responses=REST_ERROR_RESPONSES,
)
def format_markdown(
    workspace_id: UUID,
    request_body: LintFormatRequest,
    svc: ServicesDep,
    _actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    with session_factory() as session:
        prepared = svc.context_write.preview_body(
            session,
            workspace_id=workspace_id,
            markdown=request_body.markdown,
            actor_kind=_actor.actor_kind,
            actor_id=_actor.actor_id,
            actor_provenance=_actor.provenance,
        )
    return {"data": {"markdown": prepared.body}}


@router.post(
    "/{workspace_id}/lint/runs",
    response_model=LintRunResponse,
    responses=REST_ERROR_RESPONSES,
    status_code=202,
)
def create_lint_run(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: LintRunCreateRequest,
    actor: RestActorDep,
    _access: WorkspaceEditorAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.lint.enqueue_run(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/lint/runs/{run_id}",
    response_model=LintRunResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_lint_run(
    svc: ServicesDep,
    workspace_id: UUID,
    run_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.lint.get_run(session, workspace_id, run_id, actor=actor)
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/lint/findings/batch/decision",
    response_model=LintFindingBatchDecisionResponse,
    responses=REST_ERROR_RESPONSES,
)
def batch_decide_lint_findings(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: LintFindingBatchDecisionRequest,
    actor: RestActorDep,
    session_factory: WorkspaceSessionFactoryDep,
    _access: WorkspaceEditorAccessDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.lint.batch_decide_findings(
                    session=session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


# Keep the static batch route above this dynamic UUID route. Starlette matches
# routes in declaration order; otherwise the literal "batch" is consumed as a
# finding_id and rejected as an invalid UUID before the batch handler can run.
@router.post(
    "/{workspace_id}/lint/findings/{finding_id}/decision",
    response_model=LintFindingResponse,
    responses=REST_ERROR_RESPONSES,
)
def decide_lint_finding(
    svc: ServicesDep,
    workspace_id: UUID,
    finding_id: UUID,
    request_body: LintFindingDecisionRequest,
    actor: RestActorDep,
    session_factory: WorkspaceSessionFactoryDep,
    _access: WorkspaceEditorAccessDep,
):
    try:
        with session_factory.begin() as session:
            return {
                "data": svc.lint.decide_finding(
                    session=session,
                    workspace_id=workspace_id,
                    finding_id=finding_id,
                    request=request_body,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/changesets",
    response_model=ListChangesResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_changes(
    svc: ServicesDep,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    include_diff: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
):
    with session_factory() as session:
        return {
            "data": svc.domain_read.list_changes(
                session=session,
                workspace_id=workspace_id,
                target_type=target_type,
                target_id=target_id,
                include_diff=include_diff,
                limit=limit,
                cursor=cursor,
                actor=actor,
            )
        }


@router.post(
    "/{workspace_id}/search",
    response_model=SearchResponse,
    responses=REST_ERROR_RESPONSES,
)
def search(
    workspace_id: UUID,
    request_body: SearchRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    search_service: SearchServiceDep,
):
    try:
        with session_factory.begin() as session:
            payload = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=request_body.query,
                scope=request_body.scope,
                filters=request_body.filters,
                retrieval_mode=request_body.retrieval_mode,
                limit=request_body.limit,
                cursor=request_body.cursor,
                actor=actor,
            )
            if (
                request_body.cursor is None
                and payload.total_count == 0
                and KEYWORD_LANE_UNAVAILABLE_WARNING not in payload.warnings
            ):
                gap = record_coverage_gap(
                    session,
                    workspace_id=workspace_id,
                    query=request_body.query,
                    surface="search_rest",
                    tried_lanes=payload.tried_lanes,
                    nearest_candidates=payload.nearest_candidates,
                )
                if gap is not None and "coverage_gap_recorded" not in payload.warnings:
                    payload.warnings.append("coverage_gap_recorded")
            elif request_body.cursor is None and payload.total_count > 0:
                resolve_coverage_gap(
                    session,
                    workspace_id=workspace_id,
                    query=request_body.query,
                )
            return {
                "data": payload,
                "envelope": {"warnings": advisory_warnings(payload.warnings)},
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/search/capabilities",
    response_model=SearchCapabilitiesResponse,
    responses=REST_ERROR_RESPONSES,
)
def search_capabilities(
    workspace_id: UUID,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    search_service: SearchServiceDep,
):
    with session_factory() as session:
        runtime_warnings = getattr(search_service, "runtime_warnings", None)
        warning_codes = runtime_warnings(session) if callable(runtime_warnings) else []
    return {
        "data": {
            "supported_retrieval_modes": search_service.supported_modes(),
        },
        "envelope": {"warnings": advisory_warnings(warning_codes)},
    }
