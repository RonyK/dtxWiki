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
    LLMAdapterListResponse,
    LLMConnectionCreate,
    LLMConnectionListResponse,
    LLMConnectionResponse,
    LLMConnectionUpdate,
    LLMCredentialReplace,
    LLMVerificationRequest,
    LLMVerificationResponse,
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

    @property
    def control(self) -> sessionmaker[Session]:
        """Where verifications and account checks live.

        Personal connections are on the control plane already.
        """
        if self.scope == "user":
            return self.session_factory
        if self.control_session_factory is None:
            raise RuntimeError("Workspace connection writes require a control session")
        return self.control_session_factory


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


@router.get(
    "/llm-adapters",
    name="list_llm_adapters",
    response_model=LLMAdapterListResponse,
    responses=REST_ERROR_RESPONSES,
)
def list_llm_adapters(_actor: RestActorDep, services: ServicesDep):
    """Installed API specifications and the endpoints connections may use."""
    return {"data": services.llm_connections.catalog()}


def add_connection_routes(
    path: str, resolve_owner: Callable[..., ConnectionOwner], *, name: str
) -> None:
    OwnerDep = Annotated[ConnectionOwner, Depends(resolve_owner)]

    def authorize(owner: ConnectionOwner, services) -> None:
        # Workspace writes check the account and role on the control plane
        # and close that session before the Workspace session writes.
        if owner.scope == "workspace":
            if owner.control_session_factory is None:
                raise RuntimeError(
                    "Workspace connection writes require a control session"
                )
            with owner.control_session_factory() as control:
                services.llm_connections.authorize_write(
                    control,
                    scope=owner.scope,
                    owner_id=owner.owner_id,
                    actor=owner.actor,
                )

    def with_verifications(owner: ConnectionOwner, services, records: list[dict]):
        with owner.control() as control:
            latest = services.llm_verifications.latest(
                control,
                scope=owner.scope,
                owner_id=owner.owner_id,
                connection_ids=[record["id"] for record in records],
            )
        return [
            {**record, "verification": latest.get(record["id"])} for record in records
        ]

    def list_connections(owner: OwnerDep, services: ServicesDep):
        with owner.session_factory() as session:
            records = services.llm_connections.list(
                session, scope=owner.scope, owner_id=owner.owner_id
            )
        return {"data": with_verifications(owner, services, records)}

    def get_connection(connection_id: UUID, owner: OwnerDep, services: ServicesDep):
        with owner.session_factory() as session:
            record = services.llm_connections.get(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                connection_id=connection_id,
            )
        return {"data": with_verifications(owner, services, [record])[0]}

    def create_connection(
        request: LLMConnectionCreate,
        owner: OwnerDep,
        services: ServicesDep,
    ):
        authorize(owner, services)
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
        authorize(owner, services)
        with owner.session_factory.begin() as session:
            record = services.llm_connections.update(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                actor=owner.actor,
                connection_id=connection_id,
                request=request,
            )
        return {"data": with_verifications(owner, services, [record])[0]}

    def delete_connection(
        connection_id: UUID,
        expected_revision: Annotated[int, Query(ge=1)],
        owner: OwnerDep,
        services: ServicesDep,
    ):
        authorize(owner, services)
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

    def verify_connection(
        connection_id: UUID,
        request: LLMVerificationRequest,
        owner: OwnerDep,
        services: ServicesDep,
    ):
        """Queue a check of the stored setup; it never enables the connection."""
        authorize(owner, services)
        with owner.session_factory() as session:
            config_hash = services.llm_connections.active_target(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                connection_id=connection_id,
                expected_revision=request.expected_revision,
            )
        with owner.control.begin() as control:
            if owner.scope == "user":
                services.llm_connections.authorize_write(
                    control, scope="user", owner_id=owner.owner_id, actor=owner.actor
                )
            operation = services.llm_verifications.request(
                control,
                scope=owner.scope,
                owner_id=owner.owner_id,
                user_id=owner.actor.user_id,
                connection_id=connection_id,
                target="active",
                config_hash=config_hash,
            )
        return {"data": operation}

    def replace_credential(
        connection_id: UUID,
        request: LLMCredentialReplace,
        owner: OwnerDep,
        services: ServicesDep,
    ):
        """Verify a new credential; it replaces the active one only on success."""
        authorize(owner, services)
        with owner.control() as control:
            services.llm_verifications.ensure_idle(control, connection_id=connection_id)
        with owner.session_factory.begin() as session:
            config_hash = services.llm_connections.stage_candidate(
                session,
                scope=owner.scope,
                owner_id=owner.owner_id,
                actor=owner.actor,
                connection_id=connection_id,
                request=request,
            )
        with owner.control.begin() as control:
            operation = services.llm_verifications.request(
                control,
                scope=owner.scope,
                owner_id=owner.owner_id,
                user_id=owner.actor.user_id,
                connection_id=connection_id,
                target="candidate",
                config_hash=config_hash,
            )
        return {"data": operation}

    def get_verification(operation_id: UUID, owner: OwnerDep, services: ServicesDep):
        with owner.control() as control:
            operation = services.llm_verifications.get(
                control,
                scope=owner.scope,
                owner_id=owner.owner_id,
                operation_id=operation_id,
            )
        return {"data": operation}

    def cancel_verification(operation_id: UUID, owner: OwnerDep, services: ServicesDep):
        """Stop a verification; a provider call already sent may be billed."""
        with owner.control.begin() as control:
            services.llm_connections.authorize_write(
                control, scope=owner.scope, owner_id=owner.owner_id, actor=owner.actor
            )
            operation = services.llm_verifications.cancel(
                control,
                scope=owner.scope,
                owner_id=owner.owner_id,
                operation_id=operation_id,
            )
        return {"data": operation}

    item_path = f"{path}/{{connection_id}}"
    verification_path = f"{path.rsplit('/', 1)[0]}/llm-verifications/{{operation_id}}"
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
    for method, route_path, route_name, handler, status in (
        ("POST", f"{item_path}/verify", "verify", verify_connection, 202),
        (
            "PUT",
            f"{item_path}/credential",
            "replace_credential",
            replace_credential,
            202,
        ),
        ("GET", verification_path, "get_verification", get_verification, 200),
        (
            "POST",
            f"{verification_path}/cancel",
            "cancel_verification",
            cancel_verification,
            200,
        ),
    ):
        router.add_api_route(
            route_path,
            handler,
            methods=[method],
            name=f"{route_name}_{name}_llm_connection",
            status_code=status,
            response_model=LLMVerificationResponse,
            responses=REST_ERROR_RESPONSES,
        )


add_connection_routes("/me/llm-connections", account_owner, name="personal")
add_connection_routes(
    "/workspaces/{workspace_id}/llm-connections", workspace_owner, name="workspace"
)
