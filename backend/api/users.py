from fastapi import APIRouter, Request, Response

from backend.api.auth import (
    SessionServiceDep,
    auth_client_ip,
    auth_error_response,
    clear_recent_reauthentication_cookie,
    clear_session_cookie,
    current_session_user,
)
from backend.api.deps import DatabaseProviderDep, RestActorDep, SessionFactoryDep
from backend.app.container import SINGLE_DATABASE_TOPOLOGY, declared_storage_topology
from backend.app.errors import build_rest_error_response
from backend.schemas.common import RESTErrorBody
from backend.schemas.user import (
    DefaultWorkspaceUpdateRequest,
    DefaultWorkspaceUpdateResponse,
)
from backend.schemas.user_deletion import (
    SelfUserDeletionRequest,
    UserDeletionPolicyResponse,
    UserDeletionResponse,
)
from backend.services.session_service import SessionServiceError, consume_auth_slot
from backend.services.user_deletion_service import (
    UserDeletionError,
    UserDeletionService,
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
USER_DELETION_ERROR_RESPONSES = {
    **REST_ERROR_RESPONSES,
    429: {"model": RESTErrorBody},
    503: {"model": RESTErrorBody},
}


def user_deletion_service(session_factory, database_provider) -> UserDeletionService:
    # Account cleanup crosses identity and workspace tables in one transaction.
    # A future physically split provider must supply its own orchestration.
    if declared_storage_topology(database_provider) != SINGLE_DATABASE_TOPOLOGY:
        raise UserDeletionError(
            "Account deletion is unavailable for this storage topology.",
            error_code="user_deletion_unavailable",
            status_code=503,
        )
    return UserDeletionService(session_factory=session_factory)


def user_deletion_error_response(exc: UserDeletionError):
    return build_rest_error_response(
        error_code=exc.error_code, message=str(exc), status_code=exc.status_code
    )


@router.get(
    "/me/deletion-policy",
    response_model=UserDeletionPolicyResponse,
    responses=USER_DELETION_ERROR_RESPONSES,
)
def my_deletion_policy(
    request: Request,
    response: Response,
    session_service: SessionServiceDep,
    session_factory: SessionFactoryDep,
):
    try:
        current_session_user(request, session_service)
        days = UserDeletionService(session_factory=session_factory).get_policy()
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except UserDeletionError as exc:
        return user_deletion_error_response(exc)
    response.headers["Cache-Control"] = "no-store"
    return {"data": {"retention_days": days}}


@router.delete(
    "/me",
    response_model=UserDeletionResponse,
    responses=USER_DELETION_ERROR_RESPONSES,
)
def delete_my_account(
    request_body: SelfUserDeletionRequest,
    request: Request,
    response: Response,
    session_service: SessionServiceDep,
    session_factory: SessionFactoryDep,
    database_provider: DatabaseProviderDep,
):
    try:
        user = current_session_user(request, session_service)
        consume_auth_slot(client_ip=auth_client_ip(request), email=user.email)
        result = user_deletion_service(
            session_factory, database_provider
        ).request_deletion(
            user_id=user.user_id,
            actor_user_id=user.user_id,
            confirmation_email=request_body.confirmation_email,
            current_password=request_body.current_password,
            expected_retention_days=request_body.expected_retention_days,
        )
    except SessionServiceError as exc:
        return auth_error_response(exc)
    except UserDeletionError as exc:
        return user_deletion_error_response(exc)
    clear_session_cookie(response, request.app.state.settings, request)
    clear_recent_reauthentication_cookie(response, request.app.state.settings, request)
    response.headers["Cache-Control"] = "no-store"
    return {"data": result}


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
