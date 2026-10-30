"""Workspace join requests, on the account inbox.

Derived on read from the request rows. An owner sees each pending request
for a workspace they own. A requester sees the approval or rejection of
their current attempt for ``WorkspaceJoinConfig.result_visible_for``; an
approval is shown only while they are still a member. A rejection is shown
only while the workspace is discoverable or they are a member. A new attempt
or a decision changes the revision; a cancelled or closed request leaves.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.services.inbox.base import (
    ComputedScan,
    InboxDisplay,
    InboxKind,
    InboxSnapshot,
    InboxViewer,
)
from backend.services.inbox.config import WorkspaceJoinConfig
from backend.services.inbox.links import workspace_path
from backend.services.inbox.scopes import AccountInbox
from backend.services.inbox.sources import ComputedSource
from backend.utils.uuid import parse_uuid_or_none

WORKSPACE_JOIN = "workspace_join"

PENDING = "pending"
RESULTS = ("approved", "rejected")


def _membership(user_id: UUID, *roles: str):
    """Whether the user is a member of the request's workspace, in a role."""

    conditions = [
        WorkspaceMembership.workspace_id == WorkspaceJoinRequest.workspace_id,
        WorkspaceMembership.user_id == user_id,
    ]
    if roles:
        conditions.append(WorkspaceMembership.role.in_(roles))
    return (
        select(WorkspaceMembership.user_id)
        .where(*conditions)
        .correlate(WorkspaceJoinRequest)
        .exists()
    )


def _event_at():
    return func.coalesce(
        WorkspaceJoinRequest.decided_at, WorkspaceJoinRequest.requested_at
    )


def _snapshot(request: WorkspaceJoinRequest) -> InboxSnapshot:
    event_at: datetime | None = (
        request.requested_at if request.status == PENDING else request.decided_at
    )
    return InboxSnapshot(
        f"{request.workspace_id}:{request.id}",
        f"{request.attempt_no}:{request.status}",
        event_at,
    )


class _JoinRequests:
    def __init__(self, config: WorkspaceJoinConfig) -> None:
        self.config = config

    def _visible(self, viewer: InboxViewer):
        """Requests this viewer has an item for: pending ones they own, and
        their own recent results."""

        owned_pending = and_(
            WorkspaceJoinRequest.status == PENDING,
            _membership(viewer.user_id, "owner"),
        )
        discoverable_workspace = (
            select(Workspace.id)
            .where(
                Workspace.id == WorkspaceJoinRequest.workspace_id,
                Workspace.visibility.in_(("team", "public")),
            )
            .correlate(WorkspaceJoinRequest)
            .exists()
        )
        own_result = and_(
            WorkspaceJoinRequest.requester_user_id == viewer.user_id,
            WorkspaceJoinRequest.status.in_(RESULTS),
            WorkspaceJoinRequest.decided_at
            > utc_now() - self.config.result_visible_for,
            or_(
                _membership(viewer.user_id),
                and_(
                    WorkspaceJoinRequest.status == "rejected",
                    discoverable_workspace,
                ),
            ),
        )
        return or_(owned_pending, own_result)

    def scan(self, session: Session, viewer: InboxViewer, limit: int) -> ComputedScan:
        visible = self._visible(viewer)
        total = session.scalar(
            select(func.count()).select_from(WorkspaceJoinRequest).where(visible)
        )
        requests = session.scalars(
            select(WorkspaceJoinRequest)
            .where(visible)
            .order_by(_event_at().desc(), WorkspaceJoinRequest.id.desc())
            .limit(limit)
        ).all()
        return ComputedScan([_snapshot(request) for request in requests], total or 0)

    def get(
        self, session: Session, viewer: InboxViewer, item_key: str
    ) -> InboxSnapshot | None:
        workspace_id, _, request_id = item_key.partition(":")
        request = session.scalar(
            select(WorkspaceJoinRequest).where(
                WorkspaceJoinRequest.id == parse_uuid_or_none(request_id),
                WorkspaceJoinRequest.workspace_id == parse_uuid_or_none(workspace_id),
                self._visible(viewer),
            )
        )
        return _snapshot(request) if request is not None else None

    def resolve(
        self, session: Session, viewer: InboxViewer, snapshots: Sequence[InboxSnapshot]
    ) -> Mapping[str, InboxDisplay]:
        request_ids = {
            request_id
            for snapshot in snapshots
            if (request_id := parse_uuid_or_none(snapshot.item_key.partition(":")[2]))
        }
        if not request_ids:
            return {}
        # A mark can read its source before another transaction changes access.
        # Apply eligibility in the same query that projects the current title.
        rows = session.execute(
            select(WorkspaceJoinRequest, Workspace.title, User.display_name)
            .join(Workspace, Workspace.id == WorkspaceJoinRequest.workspace_id)
            .join(User, User.id == WorkspaceJoinRequest.requester_user_id)
            .where(WorkspaceJoinRequest.id.in_(request_ids), self._visible(viewer))
        ).all()
        displays: dict[str, InboxDisplay] = {}
        for request, workspace_title, requester_name in rows:
            key = f"{request.workspace_id}:{request.id}"
            status = request.status
            displays[key] = InboxDisplay(
                title=workspace_title,
                href=(
                    workspace_path(request.workspace_id, "settings?section=members")
                    if status == PENDING
                    else workspace_path(request.workspace_id)
                    if status == "approved"
                    else None
                ),
                params={"status": status, "requester": requester_name},
            )
        return displays


def workspace_join_kind(config: WorkspaceJoinConfig) -> InboxKind:
    requests = _JoinRequests(config)
    return InboxKind(
        WORKSPACE_JOIN,
        AccountInbox.name,
        ComputedSource(scan=requests.scan, get=requests.get),
        requests.resolve,
        reopen_on_new_revision=True,
    )
