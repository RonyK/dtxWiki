from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from backend.app.container import DatabaseProvider, ServiceRegistry
from backend.app.errors import build_rest_error_response
from backend.app.state import (
    get_ask_service_from_app,
    get_container_from_app,
    get_search_service_from_app,
    get_session_factory_from_app,
)
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspaceAccess,
    WorkspacePlaneUnavailableError,
    require_workspace_role,
)
from backend.policy.rest_scopes import (
    defer_token_scope_until_target,
    require_token_scope,
)
from backend.services.ask_service import AskService
from backend.services.read_errors import (
    BundleExpiredError,
    ChatOwnerUnresolvableError,
    DuplicateSlugError,
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
    RejectedByPolicyError,
    ReviewAlreadyClaimedError,
    ScopeDeniedError,
    StaleBaseVersionError,
    UnsupportedRetrievalModeError,
)
from backend.services.search_service import SearchService


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return get_session_factory_from_app(request)


def get_search_service(request: Request):
    return get_search_service_from_app(request)


def get_ask_service(request: Request):
    return get_ask_service_from_app(request)


def get_services(request: Request) -> ServiceRegistry:
    return get_container_from_app(request).get_services()


def get_workspace_session_factory(
    request: Request, workspace_id: UUID
) -> sessionmaker[Session]:
    """R5' web hot path: workspace-scoped routes resolve their session
    factory through DatabaseProvider.session_for(workspace_id) so a cloud
    overlay can shard by workspace. The local provider returns the single
    control factory (seam-only, design 18.13.11)."""
    return get_container_from_app(request).db.session_for(workspace_id)


def get_database_provider(request: Request) -> DatabaseProvider:
    """The full provider, not one workspace_id pre-bound.

    WorkspaceSessionFactoryDep above resolves a single session_for(workspace_id)
    from a path/query workspace_id -- fine for a route scoped to one
    workspace. A route that fans out across the whole workspace roster itself
    (the server-admin job listing/summary) needs to call session_for(workspace_id)
    once per roster entry, so it needs the provider, not one bound factory.
    """
    return get_container_from_app(request).db


RestActorDep = Annotated[ActorContext, Depends(require_token_scope)]
# Routes whose target Workspace is known only after a slug/page lookup cannot
# authorize against the actor's default/origin scope here. Their resolver must
# narrow the actor and call require_effective_token_scope before domain access.
TargetResolvedRestActorDep = Annotated[
    ActorContext, Depends(defer_token_scope_until_target)
]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
WorkspaceSessionFactoryDep = Annotated[
    sessionmaker[Session], Depends(get_workspace_session_factory)
]
DatabaseProviderDep = Annotated[DatabaseProvider, Depends(get_database_provider)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
AskServiceDep = Annotated[AskService, Depends(get_ask_service)]
ServicesDep = Annotated[ServiceRegistry, Depends(get_services)]


def workspace_access(required_role: str):
    """Build a control-plane workspace role dependency.

    The check completes before a workspace data-plane session is opened. This
    matters for split providers and also avoids nested SQLite IMMEDIATE
    transactions in the single-database profile.
    """

    def check_workspace_access(
        workspace_id: UUID,
        actor: RestActorDep,
        session_factory: SessionFactoryDep,
        db: DatabaseProviderDep,
    ) -> WorkspaceAccess:
        try:
            with session_factory() as session:
                return require_workspace_role(
                    session=session,
                    actor=actor,
                    workspace_id=workspace_id,
                    required_role=required_role,
                    # MCPClient is workspace-plane data; session_factory
                    # above is the control plane, so the disable check needs
                    # its own session on the target workspace's own shard.
                    workspace_session_factory=db.session_for(workspace_id),
                )
        except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": exc.error_code,
                    "message": str(exc),
                },
            ) from exc
        except RejectedByPolicyError as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "error_code": exc.error_code,
                    "message": str(exc) or "workspace access denied",
                },
            ) from exc

    return check_workspace_access


WorkspaceViewerAccessDep = Annotated[
    WorkspaceAccess, Depends(workspace_access("viewer"))
]
WorkspaceEditorAccessDep = Annotated[
    WorkspaceAccess, Depends(workspace_access("editor"))
]
WorkspaceOwnerAccessDep = Annotated[
    WorkspaceAccess, Depends(workspace_access("owner"))
]
WorkspaceAdminAccessDep = Annotated[
    WorkspaceAccess, Depends(workspace_access("admin"))
]


def read_service_error_response(exc: ReadServiceError) -> JSONResponse:
    candidate_payloads = getattr(exc, "candidate_payloads", None)
    extra = (
        {"candidates": candidate_payloads()}
        if callable(candidate_payloads)
        else None
    )
    # A stale-base refusal names the bytes the caller must reconcile against,
    # using the same keys the page routes already emit for PageConflictError
    # (api/pages.py) so both surfaces speak one conflict-body shape.
    if isinstance(exc, StaleBaseVersionError) and exc.latest_version_id is not None:
        extra = {
            **(extra or {}),
            "server_version_id": str(exc.latest_version_id),
            "server_content_hash": exc.latest_content_hash,
        }
    page_id = getattr(exc, "page_id", None)
    if page_id is not None:
        extra = {**(extra or {}), "page_id": str(page_id)}
    # A refused write that already knows what is wrong says so on the way out,
    # so the caller renders the defect instead of guessing at the refusal.
    warnings = list(getattr(exc, "warnings", None) or [])
    return build_rest_error_response(
        error_code=exc.error_code,
        message=str(exc) or exc.error_code,
        status_code=status_code_for_read_error(exc),
        warnings=warnings or None,
        extra=extra,
    )


def status_code_for_read_error(exc: ReadServiceError) -> int:
    if exc.error_code in {
        "legacy_media_cutover_conflict",
        "stale_context_draft",
        "idempotency_key_conflict",
    }:
        return 409
    if exc.error_code == "operation_outcome_unknown":
        return 404
    if isinstance(exc, ReadNotFoundError):
        return 404
    if isinstance(exc, ScopeDeniedError | RejectedByPolicyError):
        return 403
    if isinstance(exc, BundleExpiredError):
        return 410
    if isinstance(exc, DuplicateSlugError):
        return 409
    if isinstance(exc, ReadValidationError):
        return 422
    if isinstance(exc, StaleBaseVersionError):
        return 409
    if isinstance(exc, UnsupportedRetrievalModeError):
        return 422
    if isinstance(exc, ChatOwnerUnresolvableError):
        return 422
    if isinstance(exc, ReviewAlreadyClaimedError):
        return 409
    if exc.error_code in {"disambiguation_required", "page_name_occupied"}:
        return 409
    if exc.error_code == "library_original_unavailable":
        return 404
    return 500
