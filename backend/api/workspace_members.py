from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response

from backend.api.auth_dependencies import no_store_response, set_no_store_headers
from backend.api.deps import RestActorDep, SessionFactoryDep
from backend.app.errors import build_rest_error_response
from backend.schemas.common import RESTErrorBody
from backend.schemas.workspace_membership import (
    WorkspaceMemberAddRequest,
    WorkspaceMemberCandidatesResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberMutationResponse,
    WorkspaceMemberRemovalResponse,
    WorkspaceMemberRoleUpdateRequest,
)
from backend.services.session_service import (
    EmailInputPolicyError,
    InvalidCredentialsError,
)
from backend.services.workspace_membership_service import (
    FIRST_SHARE_WARNING_CODE,
    WorkspaceMemberCursorInvalidError,
    WorkspaceMemberMutation,
    WorkspaceMembershipConflictError,
    WorkspaceMembershipForbiddenError,
    WorkspaceMembershipNotFoundError,
    WorkspaceMembershipService,
    WorkspaceMembershipValidationError,
    WorkspaceMemberView,
)

router = APIRouter(prefix="/workspaces", tags=["workspace-members"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    409: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


def _service(session_factory: SessionFactoryDep) -> WorkspaceMembershipService:
    return WorkspaceMembershipService(session_factory=session_factory)


@router.get(
    "/{workspace_id}/member-candidates",
    response_model=WorkspaceMemberCandidatesResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_member_candidates(
    workspace_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
    request: Request,
    response: Response,
    query: Annotated[str, Query(max_length=320)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    set_no_store_headers(response)
    if actor.user_id is None:
        return no_store_response(_human_session_required())
    try:
        result = _service(session_factory).list_member_candidates(
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
            providers=getattr(request.app.state, "auth_providers", None),
            query=query,
            limit=limit,
        )
    except WorkspaceMembershipNotFoundError as exc:
        return no_store_response(_membership_error(exc, 404))
    except WorkspaceMembershipForbiddenError as exc:
        return no_store_response(_membership_error(exc, 403))
    return {
        "data": {
            "enabled": result.enabled,
            "items": [
                {
                    "user_id": item.user_id,
                    "display_name": item.display_name,
                    "provider_name": item.provider_name,
                    "provider_slug": item.provider_slug,
                    "provider_label": item.provider_label,
                    "identity_label": item.identity_label,
                    "identity_verified": True,
                }
                for item in result.items
            ],
            "has_more": result.has_more,
        }
    }


@router.get(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_workspace_members(
    workspace_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int | None, Query(ge=0)] = None,
    query: Annotated[str | None, Query(max_length=320)] = None,
    role: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    if actor.user_id is None:
        return _human_session_required()
    try:
        page = _service(session_factory).list_members_page(
            actor_user_id=actor.user_id,
            workspace_id=workspace_id,
            limit=limit,
            offset=offset,
            query=query,
            role=role,
            cursor=cursor,
        )
    except WorkspaceMembershipNotFoundError as exc:
        return _membership_error(exc, 404)
    except WorkspaceMembershipForbiddenError as exc:
        return _membership_error(exc, 403)
    except WorkspaceMembershipValidationError as exc:
        return _membership_error(exc, 422)
    except WorkspaceMemberCursorInvalidError as exc:
        return _membership_error(exc, 422)
    return {
        "data": {
            "items": [
                _member_payload(member, actor_user_id=actor.user_id)
                for member in page.items
            ],
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }
    }


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberMutationResponse,
    responses={
        **REST_ERROR_RESPONSES,
        201: {"model": WorkspaceMemberMutationResponse},
    },
)
def add_workspace_member(
    workspace_id: Annotated[UUID, Path()],
    request_body: WorkspaceMemberAddRequest,
    response: Response,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    if actor.user_id is None:
        return _human_session_required()
    try:
        result = _service(session_factory).add_member_with_metadata(
            actor_user_id=actor.user_id,
            workspace_id=workspace_id,
            email=request_body.email,
            user_id=request_body.user_id,
            role=request_body.role,
            confirm_first_share=request_body.confirm_first_share,
        )
    except WorkspaceMembershipNotFoundError as exc:
        return _membership_error(exc, 404)
    except WorkspaceMembershipForbiddenError as exc:
        return _membership_error(exc, 403)
    except EmailInputPolicyError as exc:
        return build_rest_error_response(
            error_code=exc.error_code, message=str(exc), status_code=422
        )
    except InvalidCredentialsError:
        return build_rest_error_response(
            error_code="user_not_found",
            message="user not found",
            status_code=404,
        )
    except WorkspaceMembershipConflictError as exc:
        return _membership_error(exc, 409)
    except WorkspaceMembershipValidationError as exc:
        return _membership_error(exc, 422)
    response.status_code = 201 if result.created else 200
    return {"data": _mutation_payload(result, actor_user_id=actor.user_id)}


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberMutationResponse,
    responses=REST_ERROR_RESPONSES,
)
def update_workspace_member_role(
    workspace_id: Annotated[UUID, Path()],
    user_id: Annotated[UUID, Path()],
    request_body: WorkspaceMemberRoleUpdateRequest,
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    if actor.user_id is None:
        return _human_session_required()
    try:
        result = _service(session_factory).change_member_role(
            actor_user_id=actor.user_id,
            workspace_id=workspace_id,
            target_user_id=user_id,
            role=request_body.role,
        )
    except WorkspaceMembershipNotFoundError as exc:
        return _membership_error(exc, 404)
    except WorkspaceMembershipForbiddenError as exc:
        return _membership_error(exc, 403)
    except WorkspaceMembershipConflictError as exc:
        return _membership_error(exc, 409)
    except WorkspaceMembershipValidationError as exc:
        return _membership_error(exc, 422)
    return {"data": _mutation_payload(result, actor_user_id=actor.user_id)}


@router.delete(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberRemovalResponse,
    responses=REST_ERROR_RESPONSES,
)
def remove_workspace_member(
    workspace_id: Annotated[UUID, Path()],
    user_id: Annotated[UUID, Path()],
    actor: RestActorDep,
    session_factory: SessionFactoryDep,
):
    if actor.user_id is None:
        return _human_session_required()
    try:
        result = _service(session_factory).remove_member_by_id(
            actor_user_id=actor.user_id,
            workspace_id=workspace_id,
            target_user_id=user_id,
        )
    except WorkspaceMembershipNotFoundError as exc:
        return _membership_error(exc, 404)
    except WorkspaceMembershipForbiddenError as exc:
        return _membership_error(exc, 403)
    except WorkspaceMembershipConflictError as exc:
        return _membership_error(exc, 409)
    return {
        "data": {
            "removed_user_id": result.removed_user_id,
            "previous_role": result.previous_role,
            "default_workspace_id": result.default_workspace_id,
            "fallback_workspace_id": result.fallback_workspace_id,
            "member_count": result.member_count,
        }
    }


def _member_payload(
    member: WorkspaceMemberView,
    *,
    actor_user_id: UUID,
) -> dict:
    return {
        "user_id": member.user_id,
        "email": member.email,
        "display_name": member.display_name,
        "status": member.status,
        "role": member.role,
        "joined_at": member.joined_at,
        "is_personal_anchor_owner": member.is_personal_anchor_owner,
        "is_current_user": member.user_id == actor_user_id,
    }


def _mutation_payload(
    result: WorkspaceMemberMutation,
    *,
    actor_user_id: UUID,
) -> dict:
    return {
        "member": _member_payload(result.member, actor_user_id=actor_user_id),
        "created": result.created,
        "first_share": result.first_share,
        "first_share_warning_code": (
            FIRST_SHARE_WARNING_CODE if result.first_share else None
        ),
        "member_count": result.member_count,
    }


def _membership_error(exc: Exception, status_code: int):
    return build_rest_error_response(
        error_code=getattr(exc, "error_code", "workspace_membership_error"),
        message=str(exc),
        status_code=status_code,
    )


def _human_session_required():
    return build_rest_error_response(
        error_code="human_session_required",
        message="A browser user session is required to manage workspace members.",
        status_code=403,
    )
