from uuid import UUID

from fastapi import APIRouter

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.schemas.agent import AgentPageBundleListResponse
from backend.schemas.common import RESTErrorBody
from backend.services.read_errors import ReadServiceError

router = APIRouter(
    prefix="/workspaces/{workspace_id}/agent/pages",
    tags=["agent"],
)

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
}


@router.get(
    "/{agent_page_id}/bundles",
    response_model=AgentPageBundleListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_agent_page_bundles(
    svc: ServicesDep,
    workspace_id: UUID,
    agent_page_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
):
    try:
        with session_factory() as session:
            return {
                "data": svc.bundle.list_for_agent_page(
                    session=session,
                    workspace_id=workspace_id,
                    agent_page_id=agent_page_id,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)
