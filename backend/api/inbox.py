"""Inbox REST routes.

Every inbox exposes the same list and mark endpoints; only how the request
finds its inbox differs. A scope contributes one dependency that returns the
viewer and the session factory for its plane, and ``add_inbox_routes`` mounts
the shared handlers on it. The recipient is always the authenticated user;
nothing here creates messages, which only producers do in their own
transactions.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from backend.api.deps import (
    RestActorDep,
    ServicesDep,
    SessionFactoryDep,
    WorkspaceSessionFactoryDep,
    WorkspaceViewerAccessDep,
    read_service_error_response,
)
from backend.policy.auth import ActorContext
from backend.schemas.common import RESTErrorBody
from backend.schemas.inbox import (
    InboxMarkRequest,
    InboxMarkResponse,
    InboxPageResponse,
    InboxStateParam,
)
from backend.services.inbox import (
    DEFAULT_INBOX_CONFIG,
    AccountInbox,
    InboxScope,
    InboxViewer,
    WorkspaceInbox,
)
from backend.services.read_errors import ReadServiceError

router = APIRouter(tags=["inbox"])

REST_ERROR_RESPONSES = {
    status: {"model": RESTErrorBody} for status in (401, 403, 404, 422, 503)
}
# Page size bounds are REST contract, so they come from the defaults at import
# time rather than from a runtime override.
_LISTING = DEFAULT_INBOX_CONFIG.listing
LimitParam = Annotated[int, Query(ge=1, le=_LISTING.page_size_max)]


@dataclass(frozen=True)
class InboxRequest:
    viewer: InboxViewer
    session_factory: sessionmaker[Session]


def _viewer(
    actor: ActorContext, scope: InboxScope, role: str | None = None
) -> InboxViewer:
    if actor.user_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "human_session_required",
                "message": "An inbox belongs to a user.",
            },
        )
    return InboxViewer(actor, actor.user_id, scope, role=role)


def workspace_inbox(
    workspace_id: UUID,
    actor: RestActorDep,
    access: WorkspaceViewerAccessDep,
    session_factory: WorkspaceSessionFactoryDep,
) -> InboxRequest:
    return InboxRequest(
        _viewer(actor, WorkspaceInbox(workspace_id), access.role), session_factory
    )


def account_inbox(
    actor: RestActorDep, session_factory: SessionFactoryDep
) -> InboxRequest:
    return InboxRequest(_viewer(actor, AccountInbox()), session_factory)


def add_inbox_routes(
    path: str, resolve: Callable[..., InboxRequest], *, name: str
) -> None:
    """Mount the shared list and mark handlers for one kind of inbox."""

    InboxDep = Annotated[InboxRequest, Depends(resolve)]

    def list_items(
        inbox: InboxDep,
        services: ServicesDep,
        state: InboxStateParam = "all",
        cursor: str | None = None,
        limit: LimitParam = _LISTING.page_size_default,
    ):
        try:
            with inbox.session_factory() as session:
                page = services.inbox.list_items(
                    session, inbox.viewer, state=state, cursor=cursor, limit=limit
                )
        except ReadServiceError as exc:
            return read_service_error_response(exc)
        return {"data": asdict(page)}

    def mark_item(
        request_body: InboxMarkRequest, inbox: InboxDep, services: ServicesDep
    ):
        try:
            with inbox.session_factory.begin() as session:
                result = services.inbox.mark(
                    session,
                    inbox.viewer,
                    kind=request_body.kind,
                    item_key=request_body.item_key,
                    seen_revision=request_body.revision,
                    read=request_body.read,
                    archived=request_body.archived,
                )
        except ReadServiceError as exc:
            return read_service_error_response(exc)
        return {"data": asdict(result)}

    router.add_api_route(
        path,
        list_items,
        methods=["GET"],
        name=f"list_{name}_inbox",
        response_model=InboxPageResponse,
        responses=REST_ERROR_RESPONSES,
    )
    router.add_api_route(
        f"{path}/marks",
        mark_item,
        methods=["POST"],
        name=f"mark_{name}_inbox_item",
        response_model=InboxMarkResponse,
        responses=REST_ERROR_RESPONSES,
    )


add_inbox_routes("/workspaces/{workspace_id}/inbox", workspace_inbox, name="workspace")
add_inbox_routes("/users/me/inbox", account_inbox, name="account")
