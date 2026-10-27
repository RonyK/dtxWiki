from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.orm import Session

from backend.domain.input_policy import (
    validate_text,
)
from backend.models.base import utc_now
from backend.models.normalized_keys import normalize_search_key
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.policy.rbac import normalize_role
from backend.services.audit_service import AuditService
from backend.services.oauth_service import revoke_workspace_oauth_consent
from backend.services.session_service import InvalidCredentialsError, normalize_email

FIRST_SHARE_WARNING_CODE = "workspace_first_share_confirmation_required"
FIRST_SHARE_WARNING = (
    "Adding the first non-owner shares the whole workspace through role-based "
    "access. Confirm the share and consider renaming a default Personal Workspace."
)
_SQLITE_WRITE_RESERVATION_KEY = "workspace_membership_sqlite_write_reservation"


class WorkspaceMembershipServiceError(RuntimeError):
    error_code = "workspace_membership_error"


class WorkspaceMembershipNotFoundError(WorkspaceMembershipServiceError):
    error_code = "not_found"


class WorkspaceMemberNotFoundError(WorkspaceMembershipNotFoundError):
    error_code = "workspace_member_not_found"


class WorkspaceMembershipForbiddenError(WorkspaceMembershipServiceError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class WorkspaceMembershipConflictError(WorkspaceMembershipServiceError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class WorkspaceMembershipValidationError(WorkspaceMembershipServiceError):
    error_code = "workspace_role_invalid"


class WorkspaceMemberCursorInvalidError(WorkspaceMembershipServiceError):
    error_code = "validation_failed"


class WorkspaceMemberInactiveError(WorkspaceMembershipConflictError):
    def __init__(self) -> None:
        super().__init__(
            "inactive users cannot be added to a workspace",
            error_code="workspace_member_inactive",
        )


class WorkspaceFirstShareConfirmationRequiredError(WorkspaceMembershipConflictError):
    def __init__(self) -> None:
        super().__init__(FIRST_SHARE_WARNING, error_code=FIRST_SHARE_WARNING_CODE)


class WorkspacePersonalOwnerLockedError(WorkspaceMembershipConflictError):
    def __init__(self) -> None:
        super().__init__(
            "a Personal Workspace owner cannot be demoted or removed; "
            "delete the workspace instead",
            error_code="personal_workspace_owner_locked",
        )


class WorkspaceLastOwnerLockedError(WorkspaceMembershipConflictError):
    def __init__(self) -> None:
        super().__init__(
            "the workspace must retain another active owner",
            error_code="workspace_last_owner_required",
        )


@dataclass(frozen=True)
class WorkspaceMemberView:
    user_id: UUID
    email: str
    display_name: str
    status: str
    workspace_id: UUID
    workspace_slug: str
    role: str
    joined_at: datetime
    is_personal_anchor_owner: bool


@dataclass(frozen=True)
class WorkspaceMemberPage:
    items: list[WorkspaceMemberView]
    total: int
    limit: int
    offset: int
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True)
class WorkspaceMemberMutation:
    member: WorkspaceMemberView
    created: bool = False
    first_share: bool = False
    member_count: int = 0


@dataclass(frozen=True)
class WorkspaceMemberRemoval:
    removed_user_id: UUID
    previous_role: str
    default_workspace_id: UUID | None
    fallback_workspace_id: UUID | None
    member_count: int


@dataclass(frozen=True)
class WorkspaceMembershipService:
    """The single mutation seam for workspace membership administration.

    Browser callers always supply ``actor_user_id`` and are authorized from the
    live membership row. ``None`` is reserved for the existing OS-operator CLI;
    it is deliberately identified as a system actor in audit events.
    """

    session_factory: object
    audit_service: AuditService = field(default_factory=AuditService)

    def list_members(
        self,
        *,
        workspace_slug: str,
        actor_user_id: UUID | None = None,
    ) -> list[WorkspaceMemberView]:
        """Compatibility wrapper used by the existing operator CLI."""

        items: list[WorkspaceMemberView] = []
        cursor = None
        while True:
            page = self.list_members_page(
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                limit=100,
                cursor=cursor,
            )
            items.extend(page.items)
            if page.next_cursor is None:
                return items
            cursor = page.next_cursor

    def list_members_page(
        self,
        *,
        actor_user_id: UUID | None,
        workspace_id: UUID | None = None,
        workspace_slug: str | None = None,
        limit: int = 25,
        offset: int | None = None,
        query: str | None = None,
        role: str | None = None,
        cursor: str | None = None,
        server_admin: bool = False,
    ) -> WorkspaceMemberPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if offset is not None and offset < 0:
            raise ValueError("offset must be non-negative")
        resolved_offset = offset or 0
        normalized_query = _normalize_member_query(query or "")
        normalized_role = None
        if role is not None:
            if role not in {"viewer", "editor", "owner"}:
                raise WorkspaceMembershipValidationError(
                    "workspace role must be viewer, editor, or owner"
                )
            normalized_role = role
        if cursor is not None and offset is not None:
            raise WorkspaceMemberCursorInvalidError(
                "cursor and offset cannot be combined"
            )
        after = (
            _decode_member_cursor(cursor, normalized_query, normalized_role)
            if cursor is not None
            else None
        )
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(
                session,
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
            )
            self._require_manager(
                session,
                workspace,
                actor_user_id,
                server_admin=server_admin,
                for_update=False,
            )
            predicates = [WorkspaceMembership.workspace_id == workspace.id]
            if normalized_role is not None:
                predicates.append(WorkspaceMembership.role == normalized_role)
            if normalized_query:
                predicates.append(
                    User.member_search_key.contains(
                        normalized_query,
                        autoescape=True,
                    )
                )

            total = (
                session.scalar(
                    select(func.count())
                    .select_from(User)
                    .join(
                        WorkspaceMembership,
                        WorkspaceMembership.user_id == User.id,
                    )
                    .where(*predicates)
                )
                or 0
            )
            page_predicates = list(predicates)
            if after is not None:
                joined_after, user_after = after
                page_predicates.append(
                    or_(
                        WorkspaceMembership.joined_at > joined_after,
                        and_(
                            WorkspaceMembership.joined_at == joined_after,
                            User.id > user_after,
                        ),
                    )
                )
            statement = (
                select(User, WorkspaceMembership)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.user_id == User.id,
                )
                .where(*page_predicates)
                .order_by(WorkspaceMembership.joined_at, User.id)
                .limit(limit + 1)
            )
            if cursor is None and resolved_offset:
                statement = statement.offset(resolved_offset)
            page_rows = list(session.execute(statement).all())
            selected = page_rows[:limit]
            next_cursor = None
            if len(page_rows) > limit and selected:
                last_user, last_membership = selected[-1]
                next_cursor = _encode_member_cursor(
                    normalized_query,
                    normalized_role,
                    (last_membership.joined_at.isoformat(), last_user.id.hex),
                )
            items = [
                self._view(workspace, user, membership) for user, membership in selected
            ]
            return WorkspaceMemberPage(
                items=items,
                total=total,
                limit=limit,
                offset=resolved_offset,
                next_cursor=next_cursor,
            )

    def add_member(
        self,
        *,
        workspace_slug: str,
        email: str,
        role: str,
        actor_user_id: UUID | None = None,
    ) -> WorkspaceMemberView:
        """Compatibility wrapper used by the existing operator CLI."""

        try:
            return self.add_member_with_metadata(
                workspace_slug=workspace_slug,
                email=email,
                role=role,
                actor_user_id=actor_user_id,
                confirm_first_share=True,
            ).member
        except WorkspaceMembershipConflictError as exc:
            if exc.error_code != "workspace_membership_role_conflict":
                raise
        normalized_email = normalize_email(email)
        with self.session_factory() as session:
            workspace = self._require_workspace(
                session,
                workspace_slug=workspace_slug,
            )
            user = session.scalar(select(User).where(User.email == normalized_email))
            if user is None:
                raise InvalidCredentialsError("user not found")
            workspace_id = workspace.id
            user_id = user.id
        return self.change_member_role(
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            target_user_id=user_id,
            role=role,
        ).member

    def add_member_with_metadata(
        self,
        *,
        actor_user_id: UUID | None,
        email: str,
        role: str = "viewer",
        confirm_first_share: bool = False,
        workspace_id: UUID | None = None,
        workspace_slug: str | None = None,
        server_admin: bool = False,
    ) -> WorkspaceMemberMutation:
        normalized_email = normalize_email(email)
        normalized_role = _normalize_requested_role(role)
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(
                session,
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
                for_update=True,
            )
            self._require_manager(
                session, workspace, actor_user_id, server_admin=server_admin
            )
            user = session.scalar(
                select(User).where(User.email == normalized_email).with_for_update()
            )
            if user is None:
                raise InvalidCredentialsError("user not found")
            if user.status != "active":
                raise WorkspaceMemberInactiveError()

            existing = session.get(WorkspaceMembership, (user.id, workspace.id))
            if existing is not None:
                if normalize_role(existing.role) != normalized_role:
                    raise WorkspaceMembershipConflictError(
                        "the user already has a different workspace role; "
                        "use role change",
                        error_code="workspace_membership_role_conflict",
                    )
                self._sync_owner_projection(session, workspace)
                return WorkspaceMemberMutation(
                    member=self._view(workspace, user, existing),
                    created=False,
                    member_count=self._member_count(session, workspace.id),
                )

            if normalized_role == "owner":
                raise WorkspaceMembershipValidationError(
                    "new members may be added only as viewer or editor; "
                    "promote an existing member to owner separately"
                )

            first_share = normalized_role != "owner" and not self._has_non_owner(
                session, workspace.id
            )
            if first_share and not confirm_first_share:
                raise WorkspaceFirstShareConfirmationRequiredError()

            membership = WorkspaceMembership(
                user_id=user.id,
                workspace_id=workspace.id,
                role=normalized_role,
            )
            session.add(membership)
            session.flush()
            self._close_pending_join_request(
                session,
                workspace_id=workspace.id,
                requester_user_id=user.id,
                actor_user_id=actor_user_id,
            )
            self._sync_owner_projection(session, workspace)
            self._emit_membership_audit(
                session,
                workspace_id=workspace.id,
                actor_user_id=actor_user_id,
                action="workspace.member.added",
                target_user_id=user.id,
                old_role=None,
                new_role=normalized_role,
                extra={"first_share": first_share},
            )
            return WorkspaceMemberMutation(
                member=self._view(workspace, user, membership),
                created=True,
                first_share=first_share,
                member_count=self._member_count(session, workspace.id),
            )

    def change_member_role(
        self,
        *,
        actor_user_id: UUID | None,
        workspace_id: UUID,
        target_user_id: UUID,
        role: str,
        server_admin: bool = False,
    ) -> WorkspaceMemberMutation:
        normalized_role = _normalize_requested_role(role)
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(
                session,
                workspace_id=workspace_id,
                for_update=True,
            )
            self._require_manager(
                session, workspace, actor_user_id, server_admin=server_admin
            )
            target, membership = self._require_member(
                session, workspace.id, target_user_id, for_update=True
            )
            old_role = normalize_role(membership.role)
            if old_role == normalized_role:
                self._sync_owner_projection(session, workspace)
                return WorkspaceMemberMutation(
                    member=self._view(workspace, target, membership),
                    member_count=self._member_count(session, workspace.id),
                )
            if old_role == "owner" and normalized_role != "owner":
                self._protect_owner_removal(session, workspace, target)

            membership.role = normalized_role
            session.flush()
            self._sync_owner_projection(session, workspace)
            self._emit_membership_audit(
                session,
                workspace_id=workspace.id,
                actor_user_id=actor_user_id,
                action="workspace.member.role_changed",
                target_user_id=target.id,
                old_role=old_role,
                new_role=normalized_role,
            )
            return WorkspaceMemberMutation(
                member=self._view(workspace, target, membership),
                member_count=self._member_count(session, workspace.id),
            )

    def remove_member(self, *, workspace_slug: str, email: str) -> bool:
        """Compatibility wrapper used by the existing operator CLI."""

        normalized_email = normalize_email(email)
        with self.session_factory() as session:
            workspace = self._require_workspace(session, workspace_slug=workspace_slug)
            user = session.scalar(select(User).where(User.email == normalized_email))
            if user is None:
                raise InvalidCredentialsError("user not found")
            membership = session.get(WorkspaceMembership, (user.id, workspace.id))
            if membership is None:
                return False
        self.remove_member_by_id(
            actor_user_id=None,
            workspace_id=workspace.id,
            target_user_id=user.id,
        )
        return True

    def remove_member_by_id(
        self,
        *,
        actor_user_id: UUID | None,
        workspace_id: UUID,
        target_user_id: UUID,
    ) -> WorkspaceMemberRemoval:
        with self.session_factory.begin() as session:
            workspace = self._require_workspace(
                session,
                workspace_id=workspace_id,
                for_update=True,
            )
            is_self_remove = (
                actor_user_id is not None and actor_user_id == target_user_id
            )
            if not is_self_remove:
                # Authorize before resolving another user's membership so a
                # non-owner cannot use 403/404 differences as an enumeration
                # oracle.
                self._require_owner(session, workspace.id, actor_user_id)
            target, membership = self._require_member(
                session, workspace.id, target_user_id, for_update=True
            )
            if self._is_personal_anchor(workspace, target):
                raise WorkspacePersonalOwnerLockedError()
            old_role = normalize_role(membership.role)
            if old_role == "owner":
                self._protect_owner_removal(session, workspace, target)

            fallback_workspace_id = self._resolve_accessible_fallback(
                session, target, workspace.id
            )
            target.default_workspace_id = fallback_workspace_id

            session.delete(membership)
            session.flush()
            revoke_workspace_oauth_consent(
                session,
                user_id=target.id,
                workspace_id=workspace.id,
            )
            self._sync_owner_projection(session, workspace)
            self._emit_membership_audit(
                session,
                workspace_id=workspace.id,
                actor_user_id=actor_user_id,
                action="workspace.member.removed",
                target_user_id=target.id,
                old_role=old_role,
                new_role=None,
                extra={"self_remove": is_self_remove},
            )
            return WorkspaceMemberRemoval(
                removed_user_id=target.id,
                previous_role=old_role,
                default_workspace_id=fallback_workspace_id,
                fallback_workspace_id=fallback_workspace_id,
                member_count=self._member_count(session, workspace.id),
            )

    def _close_pending_join_request(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        requester_user_id: UUID,
        actor_user_id: UUID | None,
    ) -> None:
        """Close a pending join request a direct add just superseded (WS-06).

        Mirrors ``WorkspaceJoinService._decide_join_request_once``'s
        already-a-member branch so both entry points into membership leave the
        join request in the same terminal state: without this, an owner who
        directly adds an already-requesting user leaves that request stuck at
        ``pending`` forever (it never shows up again in the owner's queue, but
        it also never lets the requester see themselves as approved).
        """
        now = utc_now()
        closed_request_id = session.execute(
            update(WorkspaceJoinRequest)
            .where(
                WorkspaceJoinRequest.workspace_id == workspace_id,
                WorkspaceJoinRequest.requester_user_id == requester_user_id,
                WorkspaceJoinRequest.status == "pending",
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
            .returning(WorkspaceJoinRequest.id)
        ).scalar_one_or_none()
        if closed_request_id is not None:
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind="human" if actor_user_id is not None else "system",
                actor_id=(
                    str(actor_user_id)
                    if actor_user_id is not None
                    else "cli:workspace-members"
                ),
                action="workspace.join_request.closed",
                target_kind="workspace_join_request",
                target_id=closed_request_id,
                payload={
                    "requester_user_id": str(requester_user_id),
                    "reason": "membership_already_exists",
                },
                event_summary="Workspace join request closed",
                retention_class="sensitive",
            )

    def _require_workspace(
        self,
        session: Session,
        *,
        workspace_id: UUID | None = None,
        workspace_slug: str | None = None,
        for_update: bool = False,
    ) -> Workspace:
        if (workspace_id is None) == (workspace_slug is None):
            raise ValueError("exactly one workspace identifier is required")
        if for_update:
            self._reserve_sqlite_write_transaction(session)
        statement = select(Workspace)
        if workspace_id is not None:
            statement = statement.where(Workspace.id == workspace_id)
        else:
            statement = statement.where(Workspace.slug == workspace_slug)
        if for_update:
            statement = statement.with_for_update()
        workspace = session.scalar(statement)
        if workspace is None:
            raise WorkspaceMembershipNotFoundError("workspace not found")
        return workspace

    def _require_manager(
        self,
        session: Session,
        workspace: Workspace,
        actor_user_id: UUID | None,
        *,
        server_admin: bool,
        for_update: bool = True,
    ) -> None:
        if not server_admin:
            self._require_owner(session, workspace.id, actor_user_id)
            return
        # A server administrator manages only a workspace held for an
        # account's deletion, to appoint the admin that reopens it. Ordinary
        # workspaces stay owner-managed. Users, then grants, after the
        # workspace row -- the order account deletion and recovery use.
        actor_query = select(User).where(User.id == actor_user_id)
        grant_query = select(ServerAdmin.user_id).where(
            ServerAdmin.user_id == actor_user_id
        )
        if for_update:
            actor_query = actor_query.with_for_update()
            grant_query = grant_query.with_for_update()
        actor = session.scalar(actor_query) if actor_user_id is not None else None
        granted = session.scalar(grant_query) if actor is not None else None
        if actor is None or actor.status != "active" or granted is None:
            raise WorkspaceMembershipForbiddenError(
                "an active server administrator is required",
                error_code="server_admin_required",
            )
        if workspace.account_deletion_user_id is None:
            raise WorkspaceMembershipConflictError(
                "only a workspace held for account deletion is managed here",
                error_code="workspace_not_deletion_pending",
            )

    def _require_owner(
        self,
        session: Session,
        workspace_id: UUID,
        actor_user_id: UUID | None,
    ) -> None:
        if actor_user_id is None:
            return
        actor = session.get(User, actor_user_id)
        membership = session.get(WorkspaceMembership, (actor_user_id, workspace_id))
        if actor is None or membership is None:
            raise WorkspaceMembershipForbiddenError(
                "workspace membership is required",
                error_code="workspace_membership_missing",
            )
        if actor.status != "active" or normalize_role(membership.role) != "owner":
            raise WorkspaceMembershipForbiddenError(
                "workspace owner role is required",
                error_code="workspace_role_insufficient",
            )

    def _require_member(
        self,
        session: Session,
        workspace_id: UUID,
        user_id: UUID,
        *,
        for_update: bool,
    ) -> tuple[User, WorkspaceMembership]:
        if for_update:
            self._reserve_sqlite_write_transaction(session)
        statement = (
            select(User, WorkspaceMembership)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                User.id == user_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = session.execute(statement).one_or_none()
        if row is None:
            raise WorkspaceMemberNotFoundError("workspace member not found")
        return row[0], row[1]

    @staticmethod
    def _reserve_sqlite_write_transaction(session: Session) -> None:
        """Reserve SQLite's single writer before mutation authorization reads.

        PostgreSQL keeps using the row-level ``FOR UPDATE`` clauses below. SQLite
        ignores those clauses, and pysqlite otherwise delays ``BEGIN IMMEDIATE``
        until the first DML statement, after the last-owner check has already
        raced. The Session transaction identity makes repeated calls from the
        workspace and member locking helpers idempotent without a process lock.
        """

        if session.get_bind().dialect.name != "sqlite":
            return
        transaction = session.get_transaction()
        if transaction is None:
            raise RuntimeError("membership mutation requires an active transaction")
        if session.info.get(_SQLITE_WRITE_RESERVATION_KEY) is transaction:
            return

        connection = session.connection()
        driver_connection = connection.connection.driver_connection
        if not driver_connection.in_transaction:
            session.execute(text("BEGIN IMMEDIATE"))
        session.info[_SQLITE_WRITE_RESERVATION_KEY] = transaction

    def _protect_owner_removal(
        self,
        session: Session,
        workspace: Workspace,
        target: User,
    ) -> None:
        if self._is_personal_anchor(workspace, target):
            raise WorkspacePersonalOwnerLockedError()
        if not self._has_other_active_owner(session, workspace.id, target.id):
            raise WorkspaceLastOwnerLockedError()

    @staticmethod
    def _is_personal_anchor(workspace: Workspace, user: User) -> bool:
        return user.personal_workspace_id == workspace.id

    @staticmethod
    def _has_non_owner(session: Session, workspace_id: UUID) -> bool:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.role != "owner",
                )
            )
        )

    @staticmethod
    def _has_other_active_owner(
        session: Session,
        workspace_id: UUID,
        excluded_user_id: UUID,
    ) -> bool:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .join(User, User.id == WorkspaceMembership.user_id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.role == "owner",
                    WorkspaceMembership.user_id != excluded_user_id,
                    User.status == "active",
                )
            )
        )

    @staticmethod
    def _member_count(session: Session, workspace_id: UUID) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(WorkspaceMembership.workspace_id == workspace_id)
            )
            or 0
        )

    @staticmethod
    def _resolve_accessible_fallback(
        session: Session,
        user: User,
        removed_workspace_id: UUID,
    ) -> UUID | None:
        """Choose the next landing workspace without requiring a Personal anchor."""

        for candidate_id in (
            user.default_workspace_id,
            user.personal_workspace_id,
        ):
            if candidate_id is None or candidate_id == removed_workspace_id:
                continue
            if session.get(Workspace, candidate_id) is None:
                continue
            if session.get(WorkspaceMembership, (user.id, candidate_id)) is not None:
                return candidate_id

        return session.scalar(
            select(WorkspaceMembership.workspace_id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.workspace_id != removed_workspace_id,
            )
            .order_by(Workspace.created_at, Workspace.slug, Workspace.id)
            .limit(1)
        )

    @staticmethod
    def _sync_owner_projection(session: Session, workspace: Workspace) -> None:
        owner_id = session.scalar(
            select(WorkspaceMembership.user_id)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.role == "owner",
            )
            .order_by(
                case((User.status == "active", 0), else_=1),
                WorkspaceMembership.joined_at,
                WorkspaceMembership.user_id,
            )
            .limit(1)
        )
        workspace.owner_user_id = owner_id
        if workspace.account_deletion_user_id is not None and owner_id is not None:
            # Every path that leaves an active admin ends here, so this is
            # where a workspace held for account deletion is reopened.
            owner = session.get(User, owner_id)
            if owner is not None and owner.status == "active":
                workspace.account_deletion_user_id = None
        session.flush()

    @staticmethod
    def _view(
        workspace: Workspace,
        user: User,
        membership: WorkspaceMembership,
    ) -> WorkspaceMemberView:
        return WorkspaceMemberView(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            role=normalize_role(membership.role),
            joined_at=membership.joined_at,
            is_personal_anchor_owner=(user.personal_workspace_id == workspace.id),
        )

    def _emit_membership_audit(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        actor_user_id: UUID | None,
        action: str,
        target_user_id: UUID,
        old_role: str | None,
        new_role: str | None,
        extra: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "workspace_id": str(workspace_id),
            "target_user_id": str(target_user_id),
            "old_role": old_role,
            "new_role": new_role,
        }
        payload.update(extra or {})
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="human" if actor_user_id is not None else "system",
            actor_id=(
                str(actor_user_id)
                if actor_user_id is not None
                else "cli:workspace-members"
            ),
            action=action,
            target_kind="workspace_membership",
            target_id=target_user_id,
            payload=payload,
            event_summary=action,
            retention_class="sensitive",
        )


def _normalize_member_query(value: str) -> str:
    validate_text(value, field="query", max_length=320)
    return normalize_search_key(value)


def _normalize_requested_role(role: str) -> str:
    if role not in {"viewer", "editor", "owner"}:
        raise WorkspaceMembershipValidationError(
            "workspace role must be viewer, editor, or owner"
        )
    return role


def _encode_member_cursor(
    query: str,
    role: str | None,
    sort_key: tuple[str, str],
) -> str:
    filters = f"{query}\0{role or ''}".encode()
    payload = {
        "v": 1,
        "f": hashlib.sha256(filters).hexdigest(),
        "joined": sort_key[0],
        "id": sort_key[1],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_member_cursor(
    cursor: str,
    query: str,
    role: str | None,
) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if set(payload) != {"v", "f", "joined", "id"} or payload["v"] != 1:
            raise ValueError("unsupported member cursor")
        expected = hashlib.sha256(f"{query}\0{role or ''}".encode()).hexdigest()
        if payload["f"] != expected:
            raise ValueError("member cursor does not match filters")
        joined = payload["joined"]
        user_hex = payload["id"]
        if not isinstance(joined, str) or not isinstance(user_hex, str):
            raise ValueError("invalid member cursor values")
        if UUID(hex=user_hex).hex != user_hex:
            raise ValueError("invalid member cursor user")
        return datetime.fromisoformat(joined), UUID(hex=user_hex)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise WorkspaceMemberCursorInvalidError(
            "invalid workspace member cursor"
        ) from exc
