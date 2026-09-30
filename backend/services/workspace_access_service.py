from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update

from backend.models.base import utc_now
from backend.models.context import ContextPage
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent

WORKSPACE_VISIBILITIES = ("private", "team", "public")
WORKSPACE_JOIN_POLICIES = ("invite_only", "request")
_VISIBILITY_RANK = {value: index for index, value in enumerate(WORKSPACE_VISIBILITIES)}


class WorkspaceAccessServiceError(RuntimeError):
    error_code = "workspace_access_update_failed"


class WorkspaceAccessNotFoundError(WorkspaceAccessServiceError):
    error_code = "not_found"


class WorkspaceAccessForbiddenError(WorkspaceAccessServiceError):
    error_code = "workspace_role_insufficient"


class WorkspaceAccessConfirmationRequiredError(WorkspaceAccessServiceError):
    error_code = "workspace_access_confirmation_required"


class WorkspaceAccessValidationError(WorkspaceAccessServiceError):
    error_code = "validation_failed"


class WorkspaceAccessStaleError(WorkspaceAccessServiceError):
    error_code = "stale_base_version"


@dataclass(frozen=True)
class WorkspaceAccessView:
    workspace_id: UUID
    visibility: str
    join_policy: str
    pending_join_request_count: int
    public_page_count: int
    closed_join_request_count: int = 0
    cleared_public_page_count: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class WorkspaceAccessService:
    session_factory: object
    audit_service: AuditService = field(default_factory=AuditService)

    def get_access(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: UUID,
    ) -> WorkspaceAccessView:
        with self.session_factory() as session:
            workspace = self._require_workspace(session, workspace_id)
            self._require_active_owner(session, workspace_id, actor_user_id)
            return self._view(session, workspace)

    def update_access(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: UUID,
        visibility: str | None,
        join_policy: str | None,
        expected_updated_at: datetime,
        confirmed: bool,
        expected_pending_join_request_count: int | None = None,
        expected_public_page_count: int | None = None,
    ) -> WorkspaceAccessView:
        if visibility is not None and visibility not in WORKSPACE_VISIBILITIES:
            raise WorkspaceAccessValidationError("unsupported workspace visibility")
        if join_policy is not None and join_policy not in WORKSPACE_JOIN_POLICIES:
            raise WorkspaceAccessValidationError("unsupported workspace join policy")

        with self.session_factory.begin() as session:
            # SQLite needs an explicit writer intent before the Workspace and
            # grant-count snapshot; PostgreSQL uses the row lock below.
            acquire_write_intent(session)
            workspace = self._require_workspace(
                session,
                workspace_id,
                for_update=True,
            )
            self._require_active_owner(session, workspace_id, actor_user_id)
            if workspace.updated_at != expected_updated_at:
                raise WorkspaceAccessStaleError(
                    "workspace access settings changed; reload before saving"
                )
            # The `updated_at` guard above only covers the Workspace row
            # itself. The confirmation dialog also promises a specific
            # pending-join-request count and public-page count that live on
            # OTHER tables `Workspace.updated_at` never reflects — a join
            # request inserted or a page's `is_public` flipped between the
            # dialog opening and the save lands here without tripping the
            # guard and gets silently closed/cleared (WS-07). Check both
            # counts the same way, only when the caller sent them (older
            # clients omit them and keep the pre-WS-07 behavior).
            if expected_pending_join_request_count is not None:
                current_pending = int(
                    session.scalar(
                        select(func.count())
                        .select_from(WorkspaceJoinRequest)
                        .where(
                            WorkspaceJoinRequest.workspace_id == workspace_id,
                            WorkspaceJoinRequest.status == "pending",
                        )
                    )
                    or 0
                )
                if current_pending != expected_pending_join_request_count:
                    raise WorkspaceAccessStaleError(
                        "workspace access settings changed; reload before saving"
                    )
            if expected_public_page_count is not None:
                current_public_pages = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ContextPage)
                        .where(
                            ContextPage.workspace_id == workspace_id,
                            ContextPage.is_public.is_(True),
                        )
                    )
                    or 0
                )
                if current_public_pages != expected_public_page_count:
                    raise WorkspaceAccessStaleError(
                        "workspace access settings changed; reload before saving"
                    )
            next_visibility = visibility or workspace.visibility
            next_join_policy = join_policy or workspace.join_policy
            if next_visibility == "private":
                if join_policy == "request":
                    raise WorkspaceAccessValidationError(
                        "private workspaces cannot accept join requests"
                    )
                next_join_policy = "invite_only"

            changed = (
                next_visibility != workspace.visibility
                or next_join_policy != workspace.join_policy
            )
            if changed and not confirmed:
                raise WorkspaceAccessConfirmationRequiredError(
                    "confirm the workspace access change"
                )
            if not changed:
                return self._view(session, workspace)

            previous_visibility = workspace.visibility
            previous_join_policy = workspace.join_policy
            now = utc_now()
            is_visibility_downgrade = (
                _VISIBILITY_RANK[next_visibility]
                < _VISIBILITY_RANK[previous_visibility]
            )
            requests_disabled = (
                previous_join_policy == "request"
                and next_join_policy == "invite_only"
            )

            closed_join_request_count = 0
            if is_visibility_downgrade or requests_disabled:
                pending = list(
                    session.scalars(
                        select(WorkspaceJoinRequest)
                        .where(
                            WorkspaceJoinRequest.workspace_id == workspace_id,
                            WorkspaceJoinRequest.status == "pending",
                        )
                        .with_for_update()
                    )
                )
                close_reason = (
                    "visibility_downgrade"
                    if is_visibility_downgrade
                    else "join_requests_disabled"
                )
                for request in pending:
                    request.status = "closed"
                    request.decided_at = now
                    request.decided_by_user_id = actor_user_id
                    request.decision_note = None
                    request.closed_reason = close_reason
                    request.updated_at = now
                    self.audit_service.emit(
                        session=session,
                        workspace_id=workspace_id,
                        actor_kind="human",
                        actor_id=str(actor_user_id),
                        action="workspace.join_request.closed",
                        target_kind="workspace_join_request",
                        target_id=request.id,
                        payload={
                            "requester_user_id": str(request.requester_user_id),
                            "attempt_no": request.attempt_no,
                            "reason": close_reason,
                        },
                        event_summary="Workspace join request closed",
                        retention_class="sensitive",
                    )
                closed_join_request_count = len(pending)

            cleared_public_page_count = 0
            # Exposure scope is derived from Workspace visibility. Every
            # visibility transition therefore invalidates every existing
            # grant -- including team -> private and non-public -> public.
            # A later transition must never reinterpret or resurrect it.
            if previous_visibility != next_visibility:
                cleared_public_page_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ContextPage)
                        .where(
                            ContextPage.workspace_id == workspace_id,
                            ContextPage.is_public.is_(True),
                        )
                    )
                    or 0
                )
                if cleared_public_page_count:
                    session.execute(
                        update(ContextPage)
                        .where(
                            ContextPage.workspace_id == workspace_id,
                            ContextPage.is_public.is_(True),
                        )
                        .values(
                            is_public=False,
                            public_exposure_scope=None,
                            public_exposed_version_id=None,
                        )
                    )

            workspace.visibility = next_visibility
            workspace.join_policy = next_join_policy
            workspace.updated_at = now
            session.flush()
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="workspace.access.updated",
                target_kind="workspace",
                target_id=workspace_id,
                payload={
                    "previous_visibility": previous_visibility,
                    "visibility": next_visibility,
                    "previous_join_policy": previous_join_policy,
                    "join_policy": next_join_policy,
                    "closed_join_request_count": closed_join_request_count,
                    "cleared_public_page_count": cleared_public_page_count,
                },
                event_summary="Workspace access settings changed",
            )
            return self._view(
                session,
                workspace,
                closed_join_request_count=closed_join_request_count,
                cleared_public_page_count=cleared_public_page_count,
            )

    @staticmethod
    def _require_workspace(session, workspace_id: UUID, *, for_update: bool = False):
        statement = select(Workspace).where(Workspace.id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        workspace = session.scalar(statement)
        if workspace is None:
            raise WorkspaceAccessNotFoundError("workspace not found")
        return workspace

    @staticmethod
    def _require_active_owner(session, workspace_id: UUID, actor_user_id: UUID) -> None:
        actor = session.get(User, actor_user_id)
        membership = session.get(WorkspaceMembership, (actor_user_id, workspace_id))
        if (
            actor is None
            or actor.status != "active"
            or membership is None
            or membership.role != "owner"
        ):
            raise WorkspaceAccessForbiddenError("workspace owner role is required")

    @staticmethod
    def _view(
        session,
        workspace: Workspace,
        *,
        closed_join_request_count: int = 0,
        cleared_public_page_count: int = 0,
    ) -> WorkspaceAccessView:
        pending_count = int(
            session.scalar(
                select(func.count())
                .select_from(WorkspaceJoinRequest)
                .where(
                    WorkspaceJoinRequest.workspace_id == workspace.id,
                    WorkspaceJoinRequest.status == "pending",
                )
            )
            or 0
        )
        public_page_count = int(
            session.scalar(
                select(func.count())
                .select_from(ContextPage)
                .where(
                    ContextPage.workspace_id == workspace.id,
                    ContextPage.is_public.is_(True),
                )
            )
            or 0
        )
        return WorkspaceAccessView(
            workspace_id=workspace.id,
            visibility=workspace.visibility,
            join_policy=workspace.join_policy,
            pending_join_request_count=pending_count,
            public_page_count=public_page_count,
            closed_join_request_count=closed_join_request_count,
            cleared_public_page_count=cleared_public_page_count,
            updated_at=workspace.updated_at,
        )
