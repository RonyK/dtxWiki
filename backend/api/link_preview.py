"""Read-only editor link projection, scoped to the authenticated reader."""

from uuid import UUID

from fastapi import APIRouter

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.api.read import REST_ERROR_RESPONSES
from backend.policy.rbac import rebind_actor_to_workspace_access
from backend.schemas.link_preview import LinkPreviewRequest, LinkPreviewResponse
from backend.services.link_preview import preview_link_targets
from backend.services.read_errors import ReadServiceError

router = APIRouter(prefix="/workspaces", tags=["read"])


@router.post(
    "/{workspace_id}/link-targets/preview",
    response_model=LinkPreviewResponse,
    responses=REST_ERROR_RESPONSES,
)
def preview_markdown_links(
    workspace_id: UUID,
    request_body: LinkPreviewRequest,
    svc: ServicesDep,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": preview_link_targets(
                    session,
                    workspace_id=workspace_id,
                    request=request_body,
                    actor=rebind_actor_to_workspace_access(actor, access),
                    self_origins=svc.domain_read.context_link_origins,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)
