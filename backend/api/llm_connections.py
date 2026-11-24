"""Browser-session management routes for LLM connections."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from backend.api.auth_dependencies import no_store_response
from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    SessionFactoryDep,
    WorkspaceOwnerAccessDep,
    WorkspaceSessionFactoryDep,
)
from backend.app.errors import build_rest_error_response
from backend.schemas.common import RESTErrorBody
from backend.schemas.llm_connection import (
    LLMConnectionCreate,
    LLMConnectionListResponse,
    LLMConnectionResponse,
    LLMConnectionUpdate,
)
from backend.services.llm_connection_service import LLMConnectionError
from backend.spi.types import ActorContext


class LLMConnectionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                # Fields can contain write-only input, even in error locations.
                response = build_rest_error_response(
                    error_code="validation_failed",
                    message="Invalid LLM connection request.",
                    status_code=422,
                )
            except LLMConnectionError as exc:
                response = build_rest_error_response(
                    error_code=exc.error_code,
                    message=str(exc),
                    status_code=exc.status_code,
                )
            return no_store_response(response)

        return handle


router = APIRouter(tags=["llm-connections"], route_class=LLMConnectionRoute)


class LLMConnectionRESTError(RESTErrorBody):
    error_code: str = Field(
        description=(
            "Error code, including adapter_endpoint_required when an adapter "
            "needs an endpoint profile."
        )
    )


REST_ERROR_RESPONSES = {
    status: {"model": LLMConnectionRESTError}
    for status in (401, 403, 404, 409, 422, 503)
}


@dataclass(frozen=True)
class ConnectionOwner:
    scope: Literal["user", "workspace"]
    owner_id: UUID
    actor: ActorContext
    session_factory: sessionmaker[Session]
    control_session_factory: sessionmaker[Session] | None = None


def account_owner(
    actor: RestActorDep, session_factory: SessionFactoryDep
) -> ConnectionOwner:
    if actor.user_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "human_session_required",
                "message": "A user session is required to manage LLM connections.",
            },
        )
    return ConnectionOwner("user", actor.user_id, actor, session_factory)


def workspace_owner(
    workspace_id: UUID,
    actor: RestActorDep,
    _access: WorkspaceOwnerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
    control_session_factory: SessionFactoryDep,
) -> ConnectionOwner:
    return ConnectionOwner(
        "workspace", workspace_id, actor, session_factory, control_session_factory
    )


def add_connection_routes(
    path: str, resolve_owner: Callable[..., ConnectionOwner], *, name: str
) -> None:
    OwnerDep = Annotated[ConnectionOwner, Depends(resolve_owner)]

    def list_connections(owner: OwnerDep, services: ServicesDep):
        with owner.session_factory() as session:
            records = services.llm_connections.list(
                session, scope=owner.scope, owner_id=owner.owner_id
            )
        return {"data": records}

    def get_connection(connection_id: UUID, owner: OwnerDep, services: ServicesDep):
        with owner.session_factory() as session:
            record = services.llm_connections.get(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                connection_id=connection_id,
            )
        return {"data": record}

    def create_connection(
        request: LLMConnectionCreate,
        owner: OwnerDep,
        services: ServicesDep,
    ):
        if owner.scope == "workspace":
            if owner.control_session_factory is None:
                raise RuntimeError(
                    "Workspace connection writes require a control session"
                )
            with owner.control_session_factory() as control:
                services.llm_connections.authorize_workspace_write(
                    control,
                    owner_id=owner.owner_id,
                    actor=owner.actor,
                )
        with owner.session_factory.begin() as session:
            record = services.llm_connections.create(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                actor=owner.actor,
                request=request,
            )
        return {"data": record}

    def update_connection(
        connection_id: UUID,
        request: LLMConnectionUpdate,
        owner: OwnerDep,
        services: ServicesDep,
    ):
        if owner.scope == "workspace":
            if owner.control_session_factory is None:
                raise RuntimeError(
                    "Workspace connection writes require a control session"
                )
            with owner.control_session_factory() as control:
                services.llm_connections.authorize_workspace_write(
                    control,
                    owner_id=owner.owner_id,
                    actor=owner.actor,
                )
        with owner.session_factory.begin() as session:
            record = services.llm_connections.update(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                actor=owner.actor,
                connection_id=connection_id,
                request=request,
            )
        return {"data": record}

    def delete_connection(
        connection_id: UUID,
        expected_revision: Annotated[int, Query(ge=1)],
        owner: OwnerDep,
        services: ServicesDep,
    ):
        if owner.scope == "workspace":
            if owner.control_session_factory is None:
                raise RuntimeError(
                    "Workspace connection writes require a control session"
                )
            with owner.control_session_factory() as control:
                services.llm_connections.authorize_workspace_write(
                    control,
                    owner_id=owner.owner_id,
                    actor=owner.actor,
                )
        with owner.session_factory.begin() as session:
            record = services.llm_connections.delete(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                actor=owner.actor,
                connection_id=connection_id,
                expected_revision=expected_revision,
            )
        return {"data": record}

    item_path = f"{path}/{{connection_id}}"
    router.add_api_route(
        path,
        list_connections,
        methods=["GET"],
        name=f"list_{name}_llm_connections",
        response_model=LLMConnectionListResponse,
        responses=REST_ERROR_RESPONSES,
    )
    for method, route_path, action, handler, status in (
        ("POST", path, "create", create_connection, 201),
        ("GET", item_path, "get", get_connection, 200),
        ("PATCH", item_path, "update", update_connection, 200),
        ("DELETE", item_path, "delete", delete_connection, 200),
    ):
        router.add_api_route(
            route_path,
            handler,
            methods=[method],
            name=f"{action}_{name}_llm_connection",
            status_code=status,
            response_model=LLMConnectionResponse,
            responses=REST_ERROR_RESPONSES,
        )


add_connection_routes("/me/llm-connections", account_owner, name="personal")
add_connection_routes(
    "/workspaces/{workspace_id}/llm-connections", workspace_owner, name="workspace"
)
