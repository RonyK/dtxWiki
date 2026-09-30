from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query, Response

from backend.api.deps import RestActorDep, SessionFactoryDep
from backend.app.errors import build_rest_error_response
from backend.schemas.common import RESTErrorBody
from backend.schemas.workspace_access import (
    WorkspaceAccessResponse,
    WorkspaceAccessUpdateRequest,
)
from backend.schemas.workspace_directory import WorkspaceDirectoryResponse
from backend.schemas.workspace_join import (
    WorkspaceJoinDecisionRequest,
    WorkspaceJoinMutationResponse,
    WorkspaceJoinRequestListResponse,
)
from backend.services.workspace_access_service import (
    WorkspaceAccessConfirmationRequiredError,
    WorkspaceAccessForbiddenError,
    WorkspaceAccessNotFoundError,
    WorkspaceAccessService,
    WorkspaceAccessServiceError,
    WorkspaceAccessStaleError,
    WorkspaceAccessValidationError,
    WorkspaceAccessView,
)
from backend.services.workspace_directory_service import (
    DIRECTORY_DEFAULT_LIMIT,
    DIRECTORY_MAX_LIMIT,
    WorkspaceDirectoryError,
    WorkspaceDirectoryService,
)
from backend.services.workspace_join_service import (
    WorkspaceJoinForbiddenError,
    WorkspaceJoinMutation,
    WorkspaceJoinNotAllowedError,
    WorkspaceJoinNotFoundError,
    WorkspaceJoinRequesterInactiveError,
    WorkspaceJoinRequestStaleError,
    WorkspaceJoinRequestTerminalError,
    WorkspaceJoinRequestView,
    WorkspaceJoinService,
    WorkspaceJoinServiceError,
)

router = APIRouter(tags=["workspace-access"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


def _require_browser_actor(actor):
    if actor.token_kind != "session" or actor.user_id is None:
        return build_rest_error_response(
            error_code="human_session_required",
            message="A browser user session is required.",
            status_code=403,
        )
    return None


@router.get(
    "/workspace-directory",
    response_model=WorkspaceDirectoryResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_directory(
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
    q: Annotated[str | None, Query(max_length=128)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=DIRECTORY_MAX_LIMIT)] = (
        DIRECTORY_DEFAULT_LIMIT
    ),
    pending_only: Annotated[bool, Query()] = False,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        page = WorkspaceDirectoryService(session_factory).list_directory(
            actor_user_id=actor.user_id,
            query=q,
            cursor=cursor,
            limit=limit,
            pending_only=pending_only,
        )
    except WorkspaceDirectoryError as exc:
        return _workspace_error(exc, 422)
    return {
        "data": {
            "items": [item.__dict__ for item in page.items],
            "next_cursor": page.next_cursor,
        }
    }


@router.get(
    "/workspaces/{workspace_id}/settings/access",
    include_in_schema=False,
)
@router.get(
    "/workspaces/{workspace_id}/access",
    response_model=WorkspaceAccessResponse,
    responses=REST_ERROR_RESPONSES,
)
def get_workspace_access_settings(
    workspace_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        view = WorkspaceAccessService(session_factory).get_access(
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
        )
    except WorkspaceAccessServiceError as exc:
        return _access_error(exc)
    return {"data": _access_payload(view)}


@router.patch(
    "/workspaces/{workspace_id}/settings/access",
    include_in_schema=False,
)
@router.patch(
    "/workspaces/{workspace_id}/access",
    response_model=WorkspaceAccessResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_workspace_access_settings(
    workspace_id: Annotated[UUID, Path()],
    request_body: WorkspaceAccessUpdateRequest,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        view = WorkspaceAccessService(session_factory).update_access(
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
            visibility=request_body.visibility,
            join_policy=request_body.join_policy,
            expected_updated_at=request_body.expected_updated_at,
            confirmed=request_body.confirm_access_change,
            expected_pending_join_request_count=(
                request_body.expected_pending_join_request_count
            ),
            expected_public_page_count=request_body.expected_public_page_count,
        )
    except WorkspaceAccessServiceError as exc:
        return _access_error(exc)
    return {"data": _access_payload(view)}


@router.post(
    "/workspaces/{workspace_id}/join-requests",
    response_model=WorkspaceJoinMutationResponse,
    responses={
        **REST_ERROR_RESPONSES,
        201: {"model": WorkspaceJoinMutationResponse},
    },
)
def request_workspace_join(
    workspace_id: Annotated[UUID, Path()],
    response: Response,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        mutation = WorkspaceJoinService(session_factory).request_join(
            workspace_id=workspace_id,
            requester_user_id=actor.user_id,
        )
    except WorkspaceJoinServiceError as exc:
        return _join_error(exc)
    response.status_code = 201 if mutation.new_attempt else 200
    return {"data": _join_mutation_payload(mutation)}


@router.delete(
    "/workspaces/{workspace_id}/join-requests/me",
    response_model=WorkspaceJoinMutationResponse,
    responses=REST_ERROR_RESPONSES,
)
def cancel_workspace_join(
    workspace_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        mutation = WorkspaceJoinService(session_factory).cancel_join(
            workspace_id=workspace_id,
            requester_user_id=actor.user_id,
        )
    except WorkspaceJoinServiceError as exc:
        return _join_error(exc)
    return {"data": _join_mutation_payload(mutation)}


@router.get(
    "/workspaces/{workspace_id}/join-requests",
    response_model=WorkspaceJoinRequestListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_join_requests(
    workspace_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
    status: Annotated[
        Literal[
            "pending",
            "approved",
            "rejected",
            "cancelled",
            "closed",
            "all",
        ],
        Query(),
    ] = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        page = WorkspaceJoinService(session_factory).list_join_requests(
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
            status=None if status == "all" else status,
            limit=limit,
            offset=offset,
        )
    except WorkspaceJoinServiceError as exc:
        return _join_error(exc)
    return {
        "data": {
            "items": [_join_request_payload(item) for item in page.items],
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
            "has_more": page.has_more,
        }
    }


@router.patch(
    "/workspaces/{workspace_id}/join-requests/{request_id}",
    response_model=WorkspaceJoinMutationResponse,
    responses=REST_ERROR_RESPONSES,
)
def decide_workspace_join_request(
    workspace_id: Annotated[UUID, Path()],
    request_id: Annotated[UUID, Path()],
    request_body: WorkspaceJoinDecisionRequest,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    denied = _require_browser_actor(actor)
    if denied is not None:
        return denied
    try:
        mutation = WorkspaceJoinService(session_factory).decide_join_request(
            workspace_id=workspace_id,
            request_id=request_id,
            actor_user_id=actor.user_id,
            decision=request_body.decision,
            expected_attempt_no=request_body.expected_attempt_no,
            decision_note=request_body.decision_note,
        )
    except WorkspaceJoinServiceError as exc:
        return _join_error(exc)
    return {"data": _join_mutation_payload(mutation)}


def _access_payload(view: WorkspaceAccessView) -> dict:
    return view.__dict__


def _join_request_payload(view: WorkspaceJoinRequestView) -> dict:
    return view.__dict__


def _join_mutation_payload(mutation: WorkspaceJoinMutation) -> dict:
    return {
        "request": _join_request_payload(mutation.request),
        "new_attempt": mutation.new_attempt,
        "membership_created": mutation.membership_created,
    }


def _access_error(exc: WorkspaceAccessServiceError):
    if isinstance(exc, WorkspaceAccessNotFoundError):
        return _workspace_error(exc, 404)
    if isinstance(exc, WorkspaceAccessForbiddenError):
        return _workspace_error(exc, 403)
    if isinstance(exc, WorkspaceAccessConfirmationRequiredError):
        return _workspace_error(exc, 409)
    if isinstance(exc, WorkspaceAccessStaleError):
        return _workspace_error(exc, 409)
    if isinstance(exc, WorkspaceAccessValidationError):
        return _workspace_error(exc, 422)
    return _workspace_error(exc, 500)


def _join_error(exc: WorkspaceJoinServiceError):
    if isinstance(exc, WorkspaceJoinNotFoundError):
        return _workspace_error(exc, 404)
    if isinstance(exc, WorkspaceJoinForbiddenError):
        return _workspace_error(exc, 403)
    if isinstance(
        exc,
        WorkspaceJoinNotAllowedError
        | WorkspaceJoinRequesterInactiveError
        | WorkspaceJoinRequestStaleError,
    ):
        return _workspace_error(exc, 409)
    if isinstance(exc, WorkspaceJoinRequestTerminalError):
        return _workspace_error(exc, 409)
    return _workspace_error(exc, 500)


def _workspace_error(exc: Exception, status_code: int):
    return build_rest_error_response(
        error_code=getattr(exc, "error_code", "internal_error"),
        message=str(exc) or getattr(exc, "error_code", "internal_error"),
        status_code=status_code,
    )
