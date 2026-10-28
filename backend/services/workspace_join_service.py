from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.domain.input_policy import (
    validate_text,
)
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.services.audit_service import AuditService

JOIN_REQUEST_STATUSES = ("pending", "approved", "rejected", "cancelled", "closed")

# event_summary for each join-request audit action, keyed by the same string
# passed as `action=` to _emit_audit. Without this, event_summary defaulted
# to the raw action string ("workspace.join_request.requested"), which the
# activity feed then humanized into "Workspace.join request.requested" --
# every join-request row rendered as the same nonsense title (UI-08).
_JOIN_EVENT_SUMMARIES: dict[str, str] = {
    "workspace.join_request.requested": "Workspace join request submitted",
    "workspace.join_request.cancelled": "Workspace join request cancelled",
    "workspace.join_request.closed": "Workspace join request closed",
    "workspace.join_request.approved": "Workspace join request approved",
    "workspace.join_request.rejected": "Workspace join request rejected",
}


class WorkspaceJoinServiceError(RuntimeError):
    error_code = "workspace_join_request_failed"


class WorkspaceJoinNotFoundError(WorkspaceJoinServiceError):
    error_code = "not_found"


class WorkspaceJoinRequestNotFoundError(WorkspaceJoinNotFoundError):
    error_code = "workspace_join_request_not_found"


class WorkspaceJoinForbiddenError(WorkspaceJoinServiceError):
    error_code = "workspace_role_insufficient"


class WorkspaceJoinNotAllowedError(WorkspaceJoinServiceError):
    error_code = "workspace_join_not_allowed"


class WorkspaceJoinDeletionPendingError(WorkspaceJoinNotAllowedError):
    error_code = "workspace_deletion_pending"


class WorkspaceJoinRequestStaleError(WorkspaceJoinServiceError):
    error_code = "workspace_join_request_stale"


class WorkspaceJoinRequestTerminalError(WorkspaceJoinServiceError):
    error_code = "workspace_join_request_terminal"


class WorkspaceJoinRequesterInactiveError(WorkspaceJoinServiceError):
    error_code = "workspace_join_request_requester_inactive"


@dataclass(frozen=True)
class WorkspaceJoinRequestView:
    request_id: UUID
    workspace_id: UUID
    requester_user_id: UUID
    requester_email: str | None
    requester_display_name: str
    status: str
    attempt_no: int
    requested_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    decided_by_user_id: UUID | None
    decision_note: str | None
    closed_reason: str | None


@dataclass(frozen=True)
class WorkspaceJoinMutation:
    request: WorkspaceJoinRequestView
    new_attempt: bool
    membership_created: bool = False


@dataclass(frozen=True)
class WorkspaceJoinRequestPage:
    items: list[WorkspaceJoinRequestView]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True)
class WorkspaceJoinService:
    session_factory: object
    audit_service: AuditService = field(default_factory=AuditService)

    def request_join(
        self,
        *,
        workspace_id: UUID,
        requester_user_id: UUID,
    ) -> WorkspaceJoinMutation:
        try:
            return self._request_join_once(
                workspace_id=workspace_id,
                requester_user_id=requester_user_id,
            )
        except IntegrityError:
            # Two first requests can both observe no row before the unique
            # insert. The loser returns the winner's pending attempt instead
            # of surfacing a database race. Other integrity failures retain
            # their original failure after one authoritative re-read.
            with self.session_factory() as session:
                request = session.scalar(
                    select(WorkspaceJoinRequest).where(
                        WorkspaceJoinRequest.workspace_id == workspace_id,
                        WorkspaceJoinRequest.requester_user_id == requester_user_id,
                    )
                )
                requester = session.get(User, requester_user_id)
                if (
                    request is not None
                    and request.status == "pending"
                    and requester is not None
                    and requester.status == "active"
                ):
                    return WorkspaceJoinMutation(
                        request=self._view(request, requester),
                        new_attempt=False,
                    )
            raise

    def _request_join_once(
        self,
        *,
        workspace_id: UUID,
        requester_user_id: UUID,
    ) -> WorkspaceJoinMutation:
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(session, workspace_id, for_update=True)
            requester = self._require_active_requester(
                session,
                requester_user_id,
                for_update=True,
            )
            self._require_request_policy(workspace)
            if workspace.account_deletion_user_id is not None:
                # Checked before the pending-request reply below, so a retry
                # cannot keep an old request looking accepted.
                raise WorkspaceJoinDeletionPendingError(
                    "this workspace is waiting to be deleted and accepts no members"
                )
            if (
                session.get(
                    WorkspaceMembership,
                    (requester_user_id, workspace_id),
                )
                is not None
            ):
                raise WorkspaceJoinNotAllowedError(
                    "workspace members cannot request to join again"
                )

            request = session.scalar(
                select(WorkspaceJoinRequest)
                .where(
                    WorkspaceJoinRequest.workspace_id == workspace_id,
                    WorkspaceJoinRequest.requester_user_id == requester_user_id,
                )
                .with_for_update()
            )
            if request is not None and request.status == "pending":
                return WorkspaceJoinMutation(
                    request=self._view(request, requester),
                    new_attempt=False,
                )

            now = utc_now()
            if request is None:
                request = WorkspaceJoinRequest(
                    workspace_id=workspace_id,
                    requester_user_id=requester_user_id,
                    status="pending",
                    attempt_no=1,
                    requested_at=now,
                    updated_at=now,
                )
                session.add(request)
            else:
                previous_status = request.status
                previous_attempt = request.attempt_no
                claimed = session.execute(
                    update(WorkspaceJoinRequest)
                    .where(
                        WorkspaceJoinRequest.id == request.id,
                        WorkspaceJoinRequest.status == previous_status,
                        WorkspaceJoinRequest.attempt_no == previous_attempt,
                    )
                    .values(
                        status="pending",
                        attempt_no=WorkspaceJoinRequest.attempt_no + 1,
                        requested_at=now,
                        updated_at=now,
                        decided_at=None,
                        decided_by_user_id=None,
                        decision_note=None,
                        closed_reason=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    session.refresh(request)
                    if request.status == "pending":
                        return WorkspaceJoinMutation(
                            request=self._view(request, requester),
                            new_attempt=False,
                        )
                    raise WorkspaceJoinRequestStaleError("join request changed; retry")
            session.flush()
            session.refresh(request)
            self._emit_audit(
                session,
                request=request,
                actor_user_id=requester_user_id,
                action="workspace.join_request.requested",
                payload={"attempt_no": request.attempt_no},
                requester_display_name=requester.display_name,
            )
            return WorkspaceJoinMutation(
                request=self._view(request, requester),
                new_attempt=True,
            )

    def cancel_join(
        self,
        *,
        workspace_id: UUID,
        requester_user_id: UUID,
    ) -> WorkspaceJoinMutation:
        with self.session_factory.begin() as session:
            # Workspace -> User -> WorkspaceJoinRequest, the same order
            # `_request_join_once` and `_decide_join_request_once` take. Locking
            # the requester first would let a concurrent request and cancel for
            # one pair hold each other's next row and deadlock on PostgreSQL.
            # Resolving the workspace and its policy first also keeps a
            # non-requestable workspace masked before any requester state is
            # disclosed.
            workspace = self._require_workspace(
                session,
                workspace_id,
                for_update=True,
            )
            self._require_request_policy(workspace)
            requester = self._require_active_requester(
                session,
                requester_user_id,
                for_update=True,
            )
            request = session.scalar(
                select(WorkspaceJoinRequest)
                .where(
                    WorkspaceJoinRequest.workspace_id == workspace_id,
                    WorkspaceJoinRequest.requester_user_id == requester_user_id,
                )
                .with_for_update()
            )
            if request is None:
                raise WorkspaceJoinRequestNotFoundError("join request not found")
            if request.status == "cancelled":
                return WorkspaceJoinMutation(
                    request=self._view(request, requester),
                    new_attempt=False,
                )
            if request.status != "pending":
                raise WorkspaceJoinRequestTerminalError(
                    "only the current pending request can be cancelled"
                )
            now = utc_now()
            request.status = "cancelled"
            request.updated_at = now
            request.decided_at = now
            request.decided_by_user_id = requester_user_id
            request.decision_note = None
            request.closed_reason = None
            session.flush()
            self._emit_audit(
                session,
                request=request,
                actor_user_id=requester_user_id,
                action="workspace.join_request.cancelled",
                payload={"attempt_no": request.attempt_no},
                requester_display_name=requester.display_name,
            )
            return WorkspaceJoinMutation(
                request=self._view(request, requester),
                new_attempt=False,
            )

    def list_join_requests(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: UUID,
        status: str | None = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> WorkspaceJoinRequestPage:
        if status is not None and status not in JOIN_REQUEST_STATUSES:
            raise ValueError("invalid join request status")
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid join request pagination")
        with self.session_factory() as session:
            self._require_workspace(session, workspace_id)
            self._require_active_owner(session, workspace_id, actor_user_id)
            predicate = [WorkspaceJoinRequest.workspace_id == workspace_id]
            if status is not None:
                predicate.append(WorkspaceJoinRequest.status == status)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(WorkspaceJoinRequest)
                    .where(*predicate)
                )
                or 0
            )
            rows = session.execute(
                select(WorkspaceJoinRequest, User)
                .join(User, User.id == WorkspaceJoinRequest.requester_user_id)
                .where(*predicate)
                .order_by(
                    WorkspaceJoinRequest.requested_at.desc(),
                    WorkspaceJoinRequest.id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return WorkspaceJoinRequestPage(
                items=[self._view(request, requester) for request, requester in rows],
                total=total,
                limit=limit,
                offset=offset,
            )

    def decide_join_request(
        self,
        *,
        workspace_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        decision: str,
        expected_attempt_no: int,
        decision_note: str | None = None,
    ) -> WorkspaceJoinMutation:
        if decision_note is not None:
            validate_text(decision_note, field="decision_note", max_length=500)
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        try:
            return self._decide_join_request_once(
                workspace_id=workspace_id,
                request_id=request_id,
                actor_user_id=actor_user_id,
                decision=decision,
                expected_attempt_no=expected_attempt_no,
                decision_note=decision_note,
            )
        except IntegrityError as exc:
            # A direct invitation can race an approval. Never overwrite its
            # role and never expose the database unique error. Retry once; the
            # existing-membership branch atomically closes the pending request.
            try:
                return self._decide_join_request_once(
                    workspace_id=workspace_id,
                    request_id=request_id,
                    actor_user_id=actor_user_id,
                    decision=decision,
                    expected_attempt_no=expected_attempt_no,
                    decision_note=decision_note,
                )
            except WorkspaceJoinServiceError:
                raise
            except IntegrityError as retry_exc:
                raise retry_exc from exc

    def _decide_join_request_once(
        self,
        *,
        workspace_id: UUID,
        request_id: UUID,
        actor_user_id: UUID,
        decision: str,
        expected_attempt_no: int,
        decision_note: str | None,
    ) -> WorkspaceJoinMutation:
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(session, workspace_id, for_update=True)
            self._require_active_owner(session, workspace_id, actor_user_id)
            request = session.scalar(
                select(WorkspaceJoinRequest)
                .where(
                    WorkspaceJoinRequest.id == request_id,
                    WorkspaceJoinRequest.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if request is None:
                raise WorkspaceJoinRequestNotFoundError("join request not found")
            if request.attempt_no != expected_attempt_no:
                raise WorkspaceJoinRequestStaleError("join request attempt is stale")
            if request.status != "pending":
                raise WorkspaceJoinRequestTerminalError(
                    "join request is already terminal"
                )
            self._require_request_policy(workspace)
            requester = self._require_active_requester(
                session,
                request.requester_user_id,
                for_update=True,
            )
            existing_membership = session.get(
                WorkspaceMembership,
                (request.requester_user_id, workspace_id),
            )
            if existing_membership is not None:
                now = utc_now()
                claimed = session.execute(
                    update(WorkspaceJoinRequest)
                    .where(
                        WorkspaceJoinRequest.id == request_id,
                        WorkspaceJoinRequest.workspace_id == workspace_id,
                        WorkspaceJoinRequest.status == "pending",
                        WorkspaceJoinRequest.attempt_no == expected_attempt_no,
                    )
                    .values(
                        status="closed",
                        updated_at=now,
                        decided_at=now,
                        decided_by_user_id=actor_user_id,
                        decision_note=None,
                        closed_reason="membership_already_exists",
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    raise WorkspaceJoinRequestTerminalError(
                        "join request is already terminal"
                    )
                session.flush()
                session.refresh(request)
                self._emit_audit(
                    session,
                    request=request,
                    actor_user_id=actor_user_id,
                    action="workspace.join_request.closed",
                    payload={
                        "attempt_no": request.attempt_no,
                        "reason": "membership_already_exists",
                        "existing_role": existing_membership.role,
                    },
                    requester_display_name=requester.display_name,
                )
                return WorkspaceJoinMutation(
                    request=self._view(request, requester),
                    new_attempt=False,
                    membership_created=False,
                )

            membership_created = decision == "approve"
            now = utc_now()
            claimed = session.execute(
                update(WorkspaceJoinRequest)
                .where(
                    WorkspaceJoinRequest.id == request_id,
                    WorkspaceJoinRequest.workspace_id == workspace_id,
                    WorkspaceJoinRequest.status == "pending",
                    WorkspaceJoinRequest.attempt_no == expected_attempt_no,
                )
                .values(
                    status="approved" if membership_created else "rejected",
                    updated_at=now,
                    decided_at=now,
                    decided_by_user_id=actor_user_id,
                    decision_note=decision_note,
                    closed_reason=None,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise WorkspaceJoinRequestStaleError("join request attempt is stale")
            if membership_created:
                session.add(
                    WorkspaceMembership(
                        user_id=request.requester_user_id,
                        workspace_id=workspace_id,
                        role="viewer",
                    )
                )
            session.flush()
            session.refresh(request)
            self._emit_audit(
                session,
                request=request,
                actor_user_id=actor_user_id,
                action=(
                    "workspace.join_request.approved"
                    if membership_created
                    else "workspace.join_request.rejected"
                ),
                payload={
                    "attempt_no": request.attempt_no,
                    "granted_role": "viewer" if membership_created else None,
                },
                requester_display_name=requester.display_name,
            )
            return WorkspaceJoinMutation(
                request=self._view(request, requester),
                new_attempt=False,
                membership_created=membership_created,
            )

    @staticmethod
    def _require_workspace(session, workspace_id: UUID, *, for_update: bool = False):
        statement = select(Workspace).where(Workspace.id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        workspace = session.scalar(statement)
        if workspace is None:
            raise WorkspaceJoinNotFoundError("workspace not found")
        return workspace

    @staticmethod
    def _require_request_policy(workspace: Workspace) -> None:
        if workspace.visibility == "private":
            # Exact joins must not disclose that a private workspace exists.
            raise WorkspaceJoinNotFoundError("workspace not found")
        if workspace.join_policy != "request":
            raise WorkspaceJoinNotAllowedError(
                "this workspace does not accept join requests"
            )

    @staticmethod
    def _require_active_requester(
        session,
        requester_user_id: UUID,
        *,
        for_update: bool,
    ) -> User:
        statement = select(User).where(User.id == requester_user_id)
        if for_update:
            statement = statement.with_for_update()
        requester = session.scalar(statement)
        if requester is None or requester.status != "active":
            raise WorkspaceJoinRequesterInactiveError("join requester is not active")
        return requester

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
            raise WorkspaceJoinForbiddenError("workspace owner role is required")

    @staticmethod
    def _view(
        request: WorkspaceJoinRequest,
        requester: User,
    ) -> WorkspaceJoinRequestView:
        return WorkspaceJoinRequestView(
            request_id=request.id,
            workspace_id=request.workspace_id,
            requester_user_id=request.requester_user_id,
            requester_email=requester.email,
            requester_display_name=requester.display_name,
            status=request.status,
            attempt_no=request.attempt_no,
            requested_at=request.requested_at,
            updated_at=request.updated_at,
            decided_at=request.decided_at,
            decided_by_user_id=request.decided_by_user_id,
            decision_note=request.decision_note,
            closed_reason=request.closed_reason,
        )

    def _emit_audit(
        self,
        session,
        *,
        request: WorkspaceJoinRequest,
        actor_user_id: UUID,
        action: str,
        payload: dict[str, object],
        requester_display_name: str,
    ) -> None:
        self.audit_service.emit(
            session=session,
            workspace_id=request.workspace_id,
            actor_kind="human",
            actor_id=str(actor_user_id),
            action=action,
            target_kind="workspace_join_request",
            target_id=request.id,
            payload={
                "requester_user_id": str(request.requester_user_id),
                # target_title_for_audit_event / _audit_payload_title reads
                # this key first, so the activity feed can show who the
                # request is from instead of a repeated generic title.
                "target_title": requester_display_name,
                **payload,
            },
            event_summary=_JOIN_EVENT_SUMMARIES[action],
            retention_class="sensitive",
        )
