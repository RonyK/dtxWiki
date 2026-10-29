from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import exists, func, or_, select

from backend.domain.input_policy import (
    validate_text,
)
from backend.models.auth_identity import UserIdentity
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.audit_service import AuditService


@dataclass(frozen=True)
class AdminUserWorkspace:
    workspace_id: UUID
    slug: str
    title: str
    role: str
    # Held with this user's pending account deletion.
    deletion_held: bool = False


@dataclass(frozen=True)
class AdminUserRecord:
    user: User
    workspaces: list[AdminUserWorkspace]
    identities: list[UserIdentity]


@dataclass(frozen=True)
class AdminUserSearchResult:
    items: list[AdminUserRecord]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class AdminUserRecoveryService:
    session_factory: object
    audit_service: AuditService = field(default_factory=AuditService)

    def search_users(
        self,
        *,
        actor_user_id: UUID,
        query: str,
        limit: int = 25,
        offset: int = 0,
    ) -> AdminUserSearchResult:
        validate_text(query, field="query", max_length=320)
        normalized = unicodedata.normalize("NFKC", query).strip().casefold()
        pattern = f"%{normalized}%"
        try:
            exact_id = UUID(normalized)
        except ValueError:
            exact_id = None
        with self.session_factory.begin() as session:
            membership_match = exists(
                select(WorkspaceMembership.user_id)
                .join(
                    Workspace,
                    Workspace.id == WorkspaceMembership.workspace_id,
                )
                .where(
                    WorkspaceMembership.user_id == User.id,
                    or_(
                        func.lower(Workspace.slug).like(pattern),
                        func.lower(Workspace.title).like(pattern),
                    ),
                )
            )
            filters = ()
            if normalized:
                filters = (
                    or_(
                        func.lower(User.email).like(pattern),
                        func.lower(User.display_name).like(pattern),
                        membership_match,
                        User.id == exact_id if exact_id is not None else False,
                    ),
                )
            total = int(
                session.scalar(select(func.count()).select_from(User).where(*filters))
                or 0
            )
            users = list(
                session.scalars(
                    select(User)
                    .where(*filters)
                    .order_by(func.lower(User.email), User.id)
                    .offset(offset)
                    .limit(limit)
                ).all()
            )
            records: list[AdminUserRecord] = []
            for user in users:
                memberships = list(
                    session.execute(
                        select(WorkspaceMembership, Workspace)
                        .join(
                            Workspace,
                            Workspace.id == WorkspaceMembership.workspace_id,
                        )
                        .where(WorkspaceMembership.user_id == user.id)
                        .order_by(func.lower(Workspace.title), Workspace.id)
                    ).all()
                )
                records.append(
                    AdminUserRecord(
                        user=user,
                        identities=list(
                            session.scalars(
                                select(UserIdentity)
                                .where(UserIdentity.user_id == user.id)
                                .order_by(UserIdentity.provider_slug)
                            )
                        ),
                        workspaces=[
                            AdminUserWorkspace(
                                workspace_id=workspace.id,
                                slug=workspace.slug,
                                title=workspace.title,
                                role=membership.role,
                                deletion_held=(
                                    workspace.account_deletion_user_id == user.id
                                ),
                            )
                            for membership, workspace in memberships
                        ],
                    )
                )
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="admin.users.searched",
                target_kind="user",
                target_id=actor_user_id,
                payload={
                    "query_present": bool(normalized),
                    "result_count": len(records),
                    "limit": limit,
                    "offset": offset,
                },
                event_summary="A server admin searched the protected user directory",
                retention_class="sensitive",
            )
            return AdminUserSearchResult(
                items=records,
                total=total,
                limit=limit,
                offset=offset,
            )
