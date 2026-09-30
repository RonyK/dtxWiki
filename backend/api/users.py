from fastapi import APIRouter

from backend.api.deps import RestActorDep, SessionFactoryDep
from backend.app.errors import build_rest_error_response
from backend.schemas.common import RESTErrorBody
from backend.schemas.user import (
    DefaultWorkspaceUpdateRequest,
    DefaultWorkspaceUpdateResponse,
)
from backend.services.workspace_write import (
    DefaultWorkspaceMembershipMissingError,
    DefaultWorkspaceUserNotFoundError,
    set_user_default_workspace,
)

router = APIRouter(prefix="/users", tags=["users"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


@router.patch(
    "/me/default-workspace",
    response_model=DefaultWorkspaceUpdateResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_my_default_workspace(
    request_body: DefaultWorkspaceUpdateRequest,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    if actor.user_id is None:
        return build_rest_error_response(
            error_code="human_session_required",
            message="A user session is required to set the default workspace.",
            status_code=403,
        )
    try:
        set_user_default_workspace(
            session_factory,
            user_id=actor.user_id,
            workspace_id=request_body.workspace_id,
        )
    except DefaultWorkspaceMembershipMissingError as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=403,
        )
    except DefaultWorkspaceUserNotFoundError as exc:
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc),
            status_code=404,
        )
    return {"data": {"default_workspace_id": request_body.workspace_id}}
