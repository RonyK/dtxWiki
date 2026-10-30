"""Actor-visible interactive Graph View read API."""

from uuid import UUID

from fastapi import APIRouter

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    SessionFactoryDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.api.read import REST_ERROR_RESPONSES
from backend.schemas.graph import GraphQueryRequest, GraphQueryResponse
from backend.schemas.workspace_graph import (
    WorkspaceGraphCapability,
    WorkspaceGraphCapabilityResponse,
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphSceneResponse,
)
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot
from backend.services.read_errors import ReadNotFoundError, ReadServiceError
from backend.services.workspace_graph_capability import (
    usage_relation_signals_enabled,
    workspace_graph_capability,
)

router = APIRouter(prefix="/workspaces", tags=["graph"])


@router.post(
    "/{workspace_id}/graph/query",
    response_model=GraphQueryResponse,
    responses=REST_ERROR_RESPONSES,
)
def query_graph(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: GraphQueryRequest,
    actor: RestActorDep,
    _access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    """Read a bounded scene; POST carries a structured query, not a mutation."""

    try:
        # The owner's usage-signal switch is a control-plane Workspace column.
        with control_session_factory() as control_session:
            usage_signals = usage_relation_signals_enabled(
                control_session, workspace_id
            )
        with session_factory() as session:
            # Candidates, rank denominators, hydration and history are separate
            # statements; one snapshot keeps them describing one state.
            acquire_consistent_read_snapshot(session)
            return {
                "data": svc.graph_query.query(
                    session,
                    workspace_id,
                    request=request_body,
                    actor=actor,
                    usage_signals_enabled=usage_signals,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.post(
    "/{workspace_id}/graph/overview/query",
    response_model=WorkspaceGraphSceneResponse,
    responses=REST_ERROR_RESPONSES,
)
def query_workspace_graph(
    svc: ServicesDep,
    workspace_id: UUID,
    request_body: WorkspaceGraphOverviewRequest,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
):
    """Read the feature-gated actor-visible Workspace Knowledge Map."""

    try:
        # Re-checked on every request: hiding the menu is not the boundary.
        capability = _capability(svc, workspace_id, access, control_session_factory)
        if not capability.workspace_map.effective_enabled:
            raise ReadNotFoundError("workspace graph is not enabled")
        with session_factory() as session:
            # Documents, placements, relations and revision must describe one
            # state even when a publication commits between their statements.
            acquire_consistent_read_snapshot(session)
            return {
                "data": svc.workspace_graph.query(
                    session,
                    workspace_id,
                    request=request_body,
                    actor=actor,
                )
            }
    except ReadServiceError as exc:
        return read_service_error_response(exc)


@router.get(
    "/{workspace_id}/graph/capability",
    response_model=WorkspaceGraphCapabilityResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_workspace_graph_capability(
    svc: ServicesDep,
    workspace_id: UUID,
    _actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    control_session_factory: SessionFactoryDep,
):
    """Report whether the Workspace Knowledge Map is available here."""

    try:
        return {"data": _capability(svc, workspace_id, access, control_session_factory)}
    except ReadServiceError as exc:
        return read_service_error_response(exc)


def _capability(
    svc,
    workspace_id: UUID,
    access,
    control_session_factory,
) -> WorkspaceGraphCapability:
    # The switches are control-plane Workspace columns; the map itself reads
    # the workspace plane. A missing Settings object fails closed.
    service_available = bool(
        svc.settings is not None and svc.settings.graph_service_available
    )
    with control_session_factory() as control_session:
        return workspace_graph_capability(
            control_session,
            workspace_id,
            service_available=service_available,
            actor_role=access.role,
        )
