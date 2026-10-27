"""Retained account deletion for the single-database deployment profile.

Disabling credentials and scheduling destruction share one transaction. A
durable blob-purge intent survives the database cleanup so failed physical
deletion can be retried before the user identity is finally removed. Shared
workspace content and its append-only audit history remain workspace-owned.

Only the workspaces that hold the user's footprint are locked; the footprint
comes from the single attribution registry in ``backend.models.user_attribution``.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from backend.context_media.application import DeleteUnusedMedia
from backend.context_media.domain import ActorIdentity, AssociationState
from backend.context_media.ports import ContextMediaRepository
from backend.domain.identity_policy import EmailPolicyError, normalize_email
from backend.models import (
    ApiToken,
    ApiTokenWorkspace,
    AppMetadata,
    ChangeOperation,
    ChangeSet,
    ChunkEmbedding,
    ContentChunk,
    ContextMediaAsset,
    ContextMediaUsage,
    ContextPage,
    ContextPageVersion,
    EmailOutbox,
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
    MCPClient,
    OAuthConsent,
    PageLink,
    PasswordResetToken,
    ServerAdmin,
    User,
    Workspace,
    WorkspaceCredentialGrant,
    WorkspaceMembership,
    WorkspacePurgeGrant,
    WorkspaceTreeNode,
)
from backend.models.base import utc_now
from backend.models.user_attribution import (
    DELETED_USER_KEY_PREFIX,
    USER_ATTRIBUTIONS,
    UserAttribution,
)
from backend.repositories.context_media_orphans import context_media_orphan_clause
from backend.repositories.context_visibility import PRIVATE_VERSION_STATUSES
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    select_in_workspace,
    select_rows_in_workspace,
    update_in_workspace,
)
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.db_write_intent import acquire_write_intent
from backend.services.oauth_service import revoke_live_token_grant_families
from backend.services.workspace_membership_service import WorkspaceMembershipService
from backend.services.workspace_write import WorkspaceWriteService
from backend.utils.content_hash import content_hash

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 36500
_POLICY_KEY = "user_deletion_retention_days"
_POLICY_AUDIT_TARGET = uuid5(NAMESPACE_URL, f"dtxwiki:app-metadata:{_POLICY_KEY}")
_BLOB_PENDING_PREFIX = "user_deletion_blob_pending:"
# A request that authenticated before final cleanup cannot outlive this.
_DELETED_USER_MARKER_TTL = timedelta(days=7)
# All account deletions serialize the last-active-admin decision, including
# users who share no workspace. Workspace locks still protect owner changes.
_LIFECYCLE_LOCK = 0x44545855534552


class UserDeletionError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class _FootprintChanged(RuntimeError):
    """New footprint appeared before the user lock; the next sweep retries."""


def validate_retention_days(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RETENTION_DAYS:
        raise UserDeletionError(
            f"retention_days must be an integer from 0 to {MAX_RETENTION_DAYS}",
            error_code="user_deletion_invalid_retention",
            status_code=422,
        )
    return value


def _serialize_lifecycle(session: Session) -> None:
    acquire_write_intent(session)
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _LIFECYCLE_LOCK},
        )


def _policy(session: Session) -> int:
    row = session.get(AppMetadata, _POLICY_KEY)
    if row is None:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(row.value)
        if str(value) != row.value:
            raise ValueError("noncanonical policy")
        return validate_retention_days(value)
    except (ValueError, TypeError, UserDeletionError) as exc:
        raise UserDeletionError(
            "account deletion retention settings are unavailable",
            error_code="user_deletion_unavailable",
            status_code=503,
        ) from exc


def _lock_admin_grants(session: Session, user_ids: set[UUID]) -> set[UUID]:
    """Lock live server-admin grants (after the user rows) and return them."""

    if not user_ids:
        return set()
    return set(
        session.scalars(
            select(ServerAdmin.user_id)
            .where(ServerAdmin.user_id.in_(user_ids))
            .order_by(ServerAdmin.user_id)
            .with_for_update()
        )
    )


def _require_admin(
    session: Session,
    user: User | None,
    expected_session_generation: int | None = None,
    *,
    granted: set[UUID] | None = None,
) -> None:
    if granted is None and user is not None:
        granted = _lock_admin_grants(session, {user.id})
    if user is None or user.status != "active" or user.id not in (granted or set()):
        raise UserDeletionError(
            "an active server administrator is required",
            error_code="server_admin_required",
            status_code=403,
        )
    if (
        expected_session_generation is not None
        and user.session_generation != expected_session_generation
    ):
        raise UserDeletionError(
            "recent reauthentication is no longer valid",
            error_code="recent_reauthentication_required",
            status_code=401,
        )


def _record(user: User) -> dict:
    return {
        "user_id": user.id,
        "status": user.status,
        "requested_at": user.deletion_requested_at,
        "purge_after": user.purge_after,
        "retention_days": user.deletion_retention_days,
    }


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, UserDeletionError):
        return exc.error_code
    if isinstance(exc, OSError):
        return "user_deletion_blob_purge_failed"
    return "user_deletion_unexpected_error"


def _user_footprint(session: Session, user: User) -> set[UUID]:
    """Workspaces that still hold rows naming ``user`` (read without locks)."""

    workspace_ids: set[UUID] = set()
    for entry in USER_ATTRIBUTIONS:
        if entry.model is Workspace or not hasattr(entry.model, "workspace_id"):
            continue
        workspace_ids.update(
            session.scalars(
                select(entry.model.workspace_id)
                .where(*entry.pending(user.id))
                .distinct()
            )
        )
    if user.personal_workspace_id is not None:
        workspace_ids.add(user.personal_workspace_id)
    return workspace_ids


def _cleanup_page_ids(session: Session, user_id: UUID, workspace_id: UUID) -> set[UUID]:
    # The pages cleanup locks: those whose private drafts it repairs and those
    # whose orphaned uploads it reclaims.
    drafts = select_in_workspace(
        ContextPageVersion,
        workspace_id=workspace_id,
        conditions=(
            ContextPageVersion.created_by_actor_kind == "human",
            ContextPageVersion.created_by_actor_id == str(user_id),
            ContextPageVersion.version_status.in_(PRIVATE_VERSION_STATUSES),
        ),
    ).with_only_columns(ContextPageVersion.page_id)
    uploads = select_rows_in_workspace(
        ContextMediaAsset,
        workspace_id=workspace_id,
        conditions=(
            ContextMediaAsset.created_by_actor_kind == "human",
            ContextMediaAsset.created_by_actor_id == str(user_id),
            context_media_orphan_clause(),
        ),
    ).with_only_columns(ContextMediaAsset.page_id)
    return set(session.scalars(drafts)) | set(session.scalars(uploads))


def _owned_shared_workspace_ids(session: Session, user_id: UUID) -> set[UUID]:
    personal_id = session.scalar(
        select(User.personal_workspace_id).where(User.id == user_id)
    )
    return {
        workspace_id
        for workspace_id in session.scalars(
            select(WorkspaceMembership.workspace_id).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "owner",
            )
        )
        if workspace_id != personal_id
    }


def _has_other_active_owner(
    session: Session, workspace_id: UUID, user_id: UUID
) -> bool:
    return (
        session.scalar(
            select(User.id)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == "owner",
                User.id != user_id,
                User.status == "active",
            )
            .limit(1)
        )
        is not None
    )


@dataclass(frozen=True)
class UserDeletionService:
    session_factory: object
    credential_hasher: CredentialHasher = field(default_factory=CredentialHasher)
    audit_service: AuditService = field(default_factory=AuditService)
    context_media_repository_factory: (
        Callable[[Session], ContextMediaRepository] | None
    ) = None

    def get_policy(self) -> int:
        with self.session_factory() as session:
            return _policy(session)

    def set_policy(
        self,
        *,
        actor_user_id: UUID,
        retention_days: int,
        expected_actor_session_generation: int | None = None,
    ) -> int:
        days = validate_retention_days(retention_days)
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            actor = session.scalar(
                select(User).where(User.id == actor_user_id).with_for_update()
            )
            _require_admin(session, actor, expected_actor_session_generation)
            session.merge(AppMetadata(key=_POLICY_KEY, value=str(days)))
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="auth.user.deletion_policy_changed",
                target_kind="server_setting",
                target_id=_POLICY_AUDIT_TARGET,
                payload={"retention_days": days},
            )
        return days

    def deletion_preview(self, *, user_id: UUID) -> dict:
        """What a deletion of ``user_id`` would remove or hand over right now.

        Advisory only: ``request_deletion`` recomputes everything under locks.
        """

        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                raise UserDeletionError(
                    "user not found",
                    error_code="user_deletion_not_found",
                    status_code=404,
                )
            days = _policy(session)
            personal = None
            if user.personal_workspace_id is not None:
                workspace = session.get(Workspace, user.personal_workspace_id)
                if workspace is not None:
                    personal = {
                        "workspace_id": workspace.id,
                        "title": workspace.title,
                        "other_member_count": session.scalar(
                            select(func.count())
                            .select_from(WorkspaceMembership)
                            .where(
                                WorkspaceMembership.workspace_id == workspace.id,
                                WorkspaceMembership.user_id != user.id,
                            )
                        ),
                    }
            transferred = [
                {"workspace_id": workspace.id, "title": workspace.title}
                for workspace in session.scalars(
                    select(Workspace)
                    .where(
                        Workspace.id.in_(_owned_shared_workspace_ids(session, user.id))
                    )
                    .order_by(Workspace.title, Workspace.id)
                )
                if not _has_other_active_owner(session, workspace.id, user.id)
            ]
            recipients = session.scalar(
                select(func.count())
                .select_from(ServerAdmin)
                .join(User, User.id == ServerAdmin.user_id)
                .where(User.id != user.id, User.status == "active")
            )
        return {
            "retention_days": days,
            "personal_workspace": personal,
            "transferred_workspaces": transferred,
            "recipient_admin_count": recipients,
        }

    def request_deletion(
        self,
        *,
        user_id: UUID,
        actor_user_id: UUID,
        confirmation_email: str,
        current_password: str | None = None,
        expected_retention_days: int | None = None,
        admin_request: bool = False,
        expected_actor_session_generation: int | None = None,
    ) -> dict:
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            user, actor, owned, admin_ids, granted = self._lock_for_request(
                session, user_id, actor_user_id
            )
            if admin_request:
                _require_admin(
                    session,
                    actor,
                    expected_actor_session_generation,
                    granted=granted,
                )
            elif actor_user_id != user_id:
                raise UserDeletionError(
                    "self withdrawal requires the authenticated account",
                    error_code="server_admin_required",
                    status_code=403,
                )
            elif user.status not in {"active", "deletion_pending"}:
                raise UserDeletionError(
                    "the account is inactive",
                    error_code="invalid_credentials",
                    status_code=401,
                )
            try:
                confirmed_email = normalize_email(confirmation_email)
            except EmailPolicyError as exc:
                raise UserDeletionError(
                    "confirmation email does not match the account",
                    error_code="user_deletion_email_mismatch",
                    status_code=422,
                ) from exc
            if confirmed_email != user.email:
                raise UserDeletionError(
                    "confirmation email does not match the account",
                    error_code="user_deletion_email_mismatch",
                    status_code=422,
                )
            if not admin_request and not self.credential_hasher.verify_password(
                current_password or "", user.password_hash
            ):
                raise UserDeletionError(
                    "the current password is incorrect",
                    error_code="invalid_credentials",
                    status_code=401,
                )
            if user.status == "deletion_pending":
                return _record(user)
            recipients = sorted(
                (
                    admin_id
                    for admin_id in admin_ids
                    if admin_id != user.id
                    and admin_id in granted
                    and session.get(User, admin_id).status == "active"
                ),
                key=lambda value: value.int,
            )
            if user.status == "active" and user.id in granted and not recipients:
                raise UserDeletionError(
                    "the server must retain another active administrator",
                    error_code="user_deletion_last_server_admin",
                    status_code=409,
                )
            orphaned = [
                workspace
                for workspace in owned
                if not _has_other_active_owner(session, workspace.id, user.id)
            ]
            if orphaned and not recipients:
                raise UserDeletionError(
                    "no active server administrator can take over shared workspaces",
                    error_code="user_deletion_last_owner",
                    status_code=409,
                )
            days = _policy(session)
            if expected_retention_days is not None and (
                validate_retention_days(expected_retention_days) != days
            ):
                raise UserDeletionError(
                    "retention settings changed; review the deletion period again",
                    error_code="user_deletion_policy_changed",
                    status_code=409,
                )
            requested_at = utc_now()
            user.deletion_prior_status = user.status
            user.status = "deletion_pending"
            user.deletion_requested_at = requested_at
            user.deletion_retention_days = days
            user.purge_after = requested_at + timedelta(days=days)
            user.deletion_failure_count = None
            user.deletion_last_error = None
            user.session_generation += 1
            # The owner projection prefers active owners, so it must see the
            # departing user as inactive before it is recomputed below.
            session.flush()
            memberships = WorkspaceMembershipService(
                self.session_factory, audit_service=self.audit_service
            )
            orphaned_ids = {workspace.id for workspace in orphaned}
            for workspace in owned:
                if workspace.id in orphaned_ids:
                    memberships.transfer_orphaned_ownership(
                        session,
                        workspace=workspace,
                        departing_user_id=user.id,
                        recipient_user_ids=recipients,
                        actor_user_id=actor_user_id,
                    )
                else:
                    WorkspaceMembershipService._sync_owner_projection(
                        session, workspace
                    )
            self._revoke_access(session, user, requested_at)
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="auth.user.deletion_requested",
                target_kind="user",
                target_id=user.id,
                payload={
                    "retention_days": days,
                    "purge_after": user.purge_after.isoformat(),
                    "requested_via": "server_admin" if admin_request else "self",
                    "prior_status": user.deletion_prior_status,
                    "transferred_workspace_ids": sorted(
                        str(workspace_id) for workspace_id in orphaned_ids
                    ),
                },
                retention_class="sensitive",
            )
            session.flush()
            return _record(user)

    def restore_deletion(
        self,
        *,
        user_id: UUID,
        actor_user_id: UUID,
        expected_actor_session_generation: int | None = None,
    ) -> dict:
        """Cancel a pending deletion before final cleanup has started."""

        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            users = {
                user.id: user
                for user in session.scalars(
                    select(User)
                    .where(User.id.in_({user_id, actor_user_id}))
                    .order_by(User.id)
                    .with_for_update()
                )
            }
            _require_admin(
                session, users.get(actor_user_id), expected_actor_session_generation
            )
            user = users.get(user_id)
            if user is None:
                raise UserDeletionError(
                    "user not found",
                    error_code="user_deletion_not_found",
                    status_code=404,
                )
            if user.status != "deletion_pending":
                raise UserDeletionError(
                    "the account has no pending deletion",
                    error_code="user_deletion_not_pending",
                    status_code=409,
                )
            if session.get(AppMetadata, f"{_BLOB_PENDING_PREFIX}{user.id}") is not None:
                raise UserDeletionError(
                    "account cleanup has already started",
                    error_code="user_deletion_purge_started",
                    status_code=409,
                )
            purge_after = user.purge_after
            # Sessions, tokens and grants revoked by the request stay revoked;
            # the user signs in again. A missing prior status fails closed.
            user.status = (
                user.deletion_prior_status
                if user.deletion_prior_status in {"active", "disabled"}
                else "disabled"
            )
            user.deletion_requested_at = None
            user.purge_after = None
            user.deletion_retention_days = None
            user.deletion_prior_status = None
            user.deletion_failure_count = None
            user.deletion_last_error = None
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action="auth.user.deletion_cancelled",
                target_kind="user",
                target_id=user.id,
                payload={
                    "restored_status": user.status,
                    "purge_after": purge_after.isoformat() if purge_after else None,
                },
                retention_class="sensitive",
            )
            session.flush()
            return {"user_id": user.id, "status": user.status}

    @staticmethod
    def _lock_for_request(
        session: Session, user_id: UUID, actor_user_id: UUID
    ) -> tuple[User, User | None, list[Workspace], set[UUID], set[UUID]]:
        # Lock order matches membership changes: workspace, then users, then
        # live server-admin grants (as password recovery does). A deletion
        # request changes no content, so no media intent is taken.
        personal_id = session.scalar(
            select(User.personal_workspace_id).where(User.id == user_id)
        )
        owned_ids = _owned_shared_workspace_ids(session, user_id)
        admin_ids = set(session.scalars(select(ServerAdmin.user_id)))
        # NO KEY UPDATE still serializes with membership changes (FOR UPDATE)
        # without blocking ordinary inserts that reference the workspace.
        owned = (
            list(
                session.scalars(
                    select(Workspace)
                    .where(Workspace.id.in_(owned_ids))
                    .order_by(Workspace.id)
                    .with_for_update(key_share=True)
                )
            )
            if owned_ids
            else []
        )
        users = {
            user.id: user
            for user in session.scalars(
                select(User)
                .where(User.id.in_({user_id, actor_user_id, *admin_ids}))
                .order_by(User.id)
                .with_for_update()
            )
        }
        user = users.get(user_id)
        if user is None:
            raise UserDeletionError(
                "user not found", error_code="user_deletion_not_found", status_code=404
            )
        if (
            user.personal_workspace_id != personal_id
            or _owned_shared_workspace_ids(session, user_id) != owned_ids
        ):
            # An ownership arrived before we held the user lock. Retry the
            # whole workspace-before-user sequence rather than reversing it.
            raise UserDeletionError(
                "workspace ownership changed; retry account deletion",
                error_code="user_deletion_unavailable",
                status_code=503,
            )
        granted = _lock_admin_grants(session, admin_ids | {actor_user_id})
        return user, users.get(actor_user_id), owned, admin_ids, granted

    @staticmethod
    def _lock_for_purge(
        session: Session, user_id: UUID, *, final: bool
    ) -> tuple[User, set[UUID]]:
        # Media intent, then workspace, then the pages cleanup repairs (in
        # global id order, as history repair locks them) -- the order
        # workspace deletion and Context writers use -- but only where the
        # user still has a footprint, so unrelated workspaces keep writing.
        #
        # The user row itself is locked only by the final transaction, which
        # deletes it. Before that no lock is needed: the deletion request took
        # the row FOR UPDATE, so every attributed write that saw the account
        # active committed first, and later ones see it pending and are
        # refused. Not holding the row keeps a stale writer that waits in the
        # attribution guard from closing a cycle with the rows cleanup locks.
        user = session.get(User, user_id)
        personal_id = user.personal_workspace_id
        footprint = _user_footprint(session, user)
        ordered = sorted(footprint, key=lambda value: value.int)
        for workspace_id in ordered:
            WorkspaceWriteService._lock_media_delete_intent(
                session=session, workspace_id=workspace_id
            )
        for workspace_id in ordered:
            session.scalar(
                select(Workspace.id)
                .where(Workspace.id == workspace_id)
                .with_for_update(key_share=workspace_id != personal_id)
            )
        pages = {
            (page_id, workspace_id)
            for workspace_id in ordered
            for page_id in _cleanup_page_ids(session, user_id, workspace_id)
        }
        if personal_id in footprint:
            # Every page goes with the Personal workspace, including pages
            # other members wrote there; they join the same global order.
            pages |= {
                (page_id, personal_id)
                for page_id in session.scalars(
                    select_in_workspace(
                        ContextPage, workspace_id=personal_id, conditions=()
                    ).with_only_columns(ContextPage.id)
                )
            }
        for page_id, workspace_id in sorted(pages, key=lambda pair: pair[0].int):
            session.scalar(
                select_in_workspace(
                    ContextPage,
                    workspace_id=workspace_id,
                    conditions=(ContextPage.id == page_id,),
                )
                .with_only_columns(ContextPage.id)
                .with_for_update()
            )
        session.refresh(user, with_for_update=final)
        if (
            user.personal_workspace_id != personal_id
            or not _user_footprint(session, user) <= footprint
            or any(
                (page_id, workspace_id) not in pages
                for workspace_id in ordered
                for page_id in _cleanup_page_ids(session, user_id, workspace_id)
            )
        ):
            raise _FootprintChanged(str(user_id))
        return user, footprint

    @staticmethod
    def _revoke_access(session: Session, user: User, now: datetime) -> None:
        user_id = user.id
        for model in (ApiToken, PasswordResetToken):
            session.execute(
                update(model)
                .where(model.user_id == user_id, model.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        revoke_live_token_grant_families(session, user_id=user_id, revoked_at=now)
        token_ids = select(ApiToken.id).where(ApiToken.user_id == user_id)
        session.execute(
            update(ApiTokenWorkspace)
            .where(
                ApiTokenWorkspace.token_id.in_(token_ids),
                ApiTokenWorkspace.removed_at.is_(None),
            )
            .values(
                removed_at=now,
                revision=ApiTokenWorkspace.revision + 1,
                sync_state="pending",
            )
        )
        session.execute(
            update(OAuthConsent)
            .where(OAuthConsent.user_id == user_id, OAuthConsent.removed_at.is_(None))
            .values(
                removed_at=now,
                revision=OAuthConsent.revision + 1,
                sync_state="pending",
            )
        )
        # Keep the control-plane removal intent too: otherwise replica
        # reconciliation could revive a grant we just revoked locally.
        for model, condition, change in (
            (
                WorkspaceCredentialGrant,
                WorkspaceCredentialGrant.user_id == user_id,
                {"revoked_at": now},
            ),
            (WorkspacePurgeGrant, WorkspacePurgeGrant.user_id == user_id, None),
            (MCPClient, MCPClient.issued_to_user_id == user_id, {"status": "disabled"}),
        ):
            for workspace_id in set(
                session.scalars(select(model.workspace_id).where(condition).distinct())
            ):
                session.execute(
                    delete_in_workspace(
                        model, workspace_id=workspace_id, conditions=(condition,)
                    )
                    if change is None
                    else update_in_workspace(
                        model,
                        workspace_id=workspace_id,
                        conditions=(condition,),
                        values=change,
                    )
                )
        session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.recipient_user_id == user_id,
                EmailOutbox.status.in_(("queued", "sending", "failed")),
            )
            .values(
                status="failed",
                next_attempt_at=None,
                encrypted_token=None,
                last_error="account_deletion_pending",
            )
        )

    def purge_due(
        self,
        *,
        blob_store_provider: object,
        now: datetime | None = None,
        limit: int = 25,
    ) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        due_at = now or utc_now()
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        with self.session_factory() as session:
            user_ids = session.scalars(
                select(User.id)
                .where(User.status == "deletion_pending", User.purge_after <= due_at)
                .order_by(User.updated_at, User.purge_after, User.id)
                .limit(limit)
            ).all()
        purged = 0
        for user_id in user_ids:
            try:
                purged += int(self._purge_one(user_id, blob_store_provider, due_at))
            except _FootprintChanged:
                logger.info(
                    "account deletion footprint changed; retrying next sweep",
                    extra={"user_id": str(user_id)},
                )
            except Exception as exc:
                # The user and intent stay pending for a later bounded sweep.
                logger.exception(
                    "account deletion cleanup failed", extra={"user_id": str(user_id)}
                )
                # Record the failure for administrators and move failed work
                # behind untouched due users, so persistent failures cannot
                # starve the rest of the deletion queue on every later tick.
                try:
                    with self.session_factory.begin() as session:
                        session.execute(
                            update(User)
                            .where(
                                User.id == user_id,
                                User.status == "deletion_pending",
                            )
                            .values(
                                updated_at=utc_now(),
                                deletion_failure_count=(
                                    func.coalesce(User.deletion_failure_count, 0) + 1
                                ),
                                deletion_last_error=_failure_code(exc),
                            )
                        )
                except Exception:
                    logger.exception(
                        "account deletion retry ordering update failed",
                        extra={"user_id": str(user_id)},
                    )
        self._expire_deleted_user_markers()
        return purged

    def _expire_deleted_user_markers(self) -> None:
        cutoff = (utc_now() - _DELETED_USER_MARKER_TTL).isoformat()
        try:
            with self.session_factory.begin() as session:
                session.execute(
                    delete(AppMetadata).where(
                        AppMetadata.key.startswith(DELETED_USER_KEY_PREFIX),
                        AppMetadata.value < cutoff,
                    )
                )
        except Exception:
            logger.exception("expiring deleted-user markers failed")

    def _purge_one(
        self, user_id: UUID, blob_store_provider: object, now: datetime
    ) -> bool:
        intent_key = f"{_BLOB_PENDING_PREFIX}{user_id}"
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            if session.scalar(select(User.id).where(User.id == user_id)) is None:
                return False
            user, footprint = self._lock_for_purge(session, user_id, final=False)
            if (
                user.status != "deletion_pending"
                or user.purge_after is None
                or user.purge_after > now
            ):
                return False
            intent = session.get(AppMetadata, intent_key)
            workspace_id = (
                UUID(intent.value)
                if intent is not None and intent.value
                else user.personal_workspace_id
            )
            store = None
            if workspace_id is not None:
                store = blob_store_provider.store_for(workspace_id)
                if not callable(getattr(store, "purge_workspace", None)):
                    raise UserDeletionError(
                        "blob storage cannot reclaim personal workspace data",
                        error_code="user_deletion_unavailable",
                        status_code=503,
                    )
            if intent is None:
                # Insert the retry marker in the same commit that removes rows.
                session.add(
                    AppMetadata(
                        key=intent_key, value=str(workspace_id) if workspace_id else ""
                    )
                )
                footprint.discard(workspace_id)
                self._cleanup_user_resources(session, user_id, footprint)
                if workspace_id is not None:
                    # Last: deleting the Personal workspace nulls the user's
                    # unique pointer, which locks the user row, so nothing
                    # after it waits on rows a stale writer could still hold.
                    WorkspaceWriteService.purge_personal_workspace_for_deleted_user(
                        session=session, user=user
                    )
                self.audit_service.emit(
                    session=session,
                    workspace_id=None,
                    actor_kind="system",
                    actor_id="user_deletion_worker",
                    action="auth.user.deletion_data_purged",
                    target_kind="user",
                    target_id=user_id,
                    payload={
                        "personal_workspace_id": str(workspace_id)
                        if workspace_id
                        else None,
                        "cleaned_workspace_count": len(footprint),
                    },
                    retention_class="sensitive",
                )
        # Committed rows and durable intent precede irreversible file deletion.
        if workspace_id is not None:
            store.purge_workspace(workspace_id)
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            if session.scalar(select(User.id).where(User.id == user_id)) is None:
                return False
            # Normally empty: the first transaction already cleaned the
            # footprint, and new attributions to a pending user are refused.
            user, footprint = self._lock_for_purge(session, user_id, final=True)
            intent = session.get(AppMetadata, intent_key)
            if user.status != "deletion_pending" or intent is None:
                raise UserDeletionError(
                    "account deletion intent changed during cleanup",
                    error_code="user_deletion_intent_changed",
                    status_code=409,
                )
            self._cleanup_user_resources(session, user_id, footprint)
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="system",
                actor_id="user_deletion_worker",
                action="auth.user.deleted",
                target_kind="user",
                target_id=user_id,
                retention_class="sensitive",
            )
            session.execute(delete(User).where(User.id == user_id))
            session.delete(intent)
            session.add(
                AppMetadata(
                    key=f"{DELETED_USER_KEY_PREFIX}{user_id}",
                    value=utc_now().isoformat(),
                )
            )
        return True

    def _cleanup_user_resources(
        self, session: Session, user_id: UUID, workspace_ids: set[UUID]
    ) -> None:
        # Shared content stays; personal preferences, connections and chat
        # projections are explicitly removed even when FK cascades are off.
        for workspace_id in sorted(workspace_ids, key=lambda value: value.int):
            self._cleanup_workspace_user_resources(
                session, user_id=user_id, workspace_id=workspace_id
            )
        projected = list(
            session.scalars(select(Workspace).where(Workspace.owner_user_id == user_id))
        )
        for entry in USER_ATTRIBUTIONS:
            if entry.workspace_scoped:
                continue
            if entry.cleanup == "api_tokens":
                token_ids = select(ApiToken.id).where(*entry.match(user_id))
                session.execute(
                    delete(ApiTokenWorkspace).where(
                        ApiTokenWorkspace.token_id.in_(token_ids)
                    )
                )
                session.execute(delete(ApiToken).where(*entry.match(user_id)))
            elif entry.cleanup == "delete":
                session.execute(delete(entry.model).where(*entry.match(user_id)))
            elif entry.cleanup == "clear":
                for column in entry.columns:
                    session.execute(
                        update(entry.model)
                        .where(column == user_id)
                        .values({column.key: None})
                    )
            elif entry.cleanup == "owner_projection":
                # Memberships are gone by now; fall back to a remaining owner.
                for workspace in projected:
                    WorkspaceMembershipService._sync_owner_projection(
                        session, workspace
                    )
            else:
                raise ValueError(f"unknown identity cleanup {entry.cleanup!r}")
        session.flush()

    def _cleanup_workspace_user_resources(
        self, session: Session, *, user_id: UUID, workspace_id: UUID
    ) -> None:
        for entry in USER_ATTRIBUTIONS:
            if entry.workspace_scoped:
                self._cleanup_workspace_attribution(
                    session, entry, user_id=user_id, workspace_id=workspace_id
                )

    def _cleanup_workspace_attribution(
        self,
        session: Session,
        entry: UserAttribution,
        *,
        user_id: UUID,
        workspace_id: UUID,
    ) -> None:
        model = entry.model
        if entry.cleanup == "private_versions":
            self._scrub_private_context_versions(
                session, user_id=user_id, workspace_id=workspace_id
            )
        elif entry.cleanup == "author_name":
            session.execute(
                update_in_workspace(
                    model,
                    workspace_id=workspace_id,
                    conditions=entry.pending(user_id),
                    values={"actor_display_name": None},
                )
            )
        elif entry.cleanup == "chat_sessions":
            chat_ids = select_in_workspace(
                McpChatSession,
                workspace_id=workspace_id,
                conditions=entry.match(user_id),
            ).with_only_columns(McpChatSession.id)
            session.execute(
                update_in_workspace(
                    ChangeSet,
                    workspace_id=workspace_id,
                    conditions=(ChangeSet.origin_chat_session_id.in_(chat_ids),),
                    values={"origin_chat_session_id": None},
                )
            )
            for link_model in (McpChatArtifactLink, McpChatToolCallArtifact):
                session.execute(
                    delete_in_workspace(
                        link_model,
                        workspace_id=workspace_id,
                        conditions=(link_model.chat_session_id.in_(chat_ids),),
                    )
                )
            session.execute(
                delete_in_workspace(
                    McpChatSession,
                    workspace_id=workspace_id,
                    conditions=entry.match(user_id),
                )
            )
        elif entry.cleanup == "delete":
            session.execute(
                delete_in_workspace(
                    model, workspace_id=workspace_id, conditions=entry.match(user_id)
                )
            )
        elif entry.cleanup == "clear":
            for column in entry.columns:
                session.execute(
                    update_in_workspace(
                        model,
                        workspace_id=workspace_id,
                        conditions=(column == user_id,),
                        values={column.key: None},
                    )
                )
        elif entry.cleanup == "orphan_media":
            self._reclaim_orphan_media(
                session, user_id=user_id, workspace_id=workspace_id
            )
        else:
            raise ValueError(f"unknown workspace cleanup {entry.cleanup!r}")

    def _scrub_private_context_versions(
        self, session: Session, *, user_id: UUID, workspace_id: UUID
    ) -> None:
        from backend.services.mcp_chat_service import purge_previews_for_page
        from backend.services.page_service import PageService, sync_page_projections

        private_versions = session.scalars(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.created_by_actor_kind == "human",
                    ContextPageVersion.created_by_actor_id == str(user_id),
                    ContextPageVersion.version_status.in_(PRIVATE_VERSION_STATUSES),
                ),
            )
        ).all()
        private_ids = {version.id for version in private_versions}
        page_ids = {version.page_id for version in private_versions}
        pages = (
            session.scalars(
                select_in_workspace(
                    ContextPage,
                    workspace_id=workspace_id,
                    conditions=(ContextPage.id.in_(page_ids),),
                ).with_for_update()
            ).all()
            if page_ids
            else []
        )
        if any(
            page.current_published_version_id in private_ids
            or page.public_exposed_version_id in private_ids
            for page in pages
        ):
            raise UserDeletionError(
                "a private draft is published or publicly exposed",
                error_code="user_deletion_private_version_exposed",
                status_code=409,
            )
        for version in private_versions:
            version.version_status = "discarded"
            version.body_md = ""
            version.content_hash = content_hash("")
            version.nav_description = None
            version.block_map = None
            version.reconcile_manifest = None
            version.actor_display_name = None
            version.editing_revision += 1
            purge_previews_for_page(
                session, workspace_id, version.page_id, version_id=version.id
            )
        if private_ids:
            session.execute(
                delete_in_workspace(
                    ContextMediaUsage,
                    workspace_id=workspace_id,
                    conditions=(ContextMediaUsage.page_version_id.in_(private_ids),),
                )
            )
            session.execute(
                update_in_workspace(
                    ChangeOperation,
                    workspace_id=workspace_id,
                    conditions=(
                        ChangeOperation.diff_ref.in_(
                            [
                                f"context_page_version:{version_id}"
                                for version_id in private_ids
                            ]
                        ),
                    ),
                    values={"diff_summary": None},
                )
            )
            chunks = select_in_workspace(
                ContentChunk,
                workspace_id=workspace_id,
                conditions=(
                    ContentChunk.document_kind == "context_page",
                    ContentChunk.source_version_id.in_(private_ids),
                ),
            ).with_only_columns(ContentChunk.id)
            session.execute(
                delete_in_workspace(
                    ChunkEmbedding,
                    workspace_id=workspace_id,
                    conditions=(ChunkEmbedding.chunk_id.in_(chunks),),
                )
            )
            session.execute(
                delete_in_workspace(
                    ContentChunk,
                    workspace_id=workspace_id,
                    conditions=(
                        ContentChunk.document_kind == "context_page",
                        ContentChunk.source_version_id.in_(private_ids),
                    ),
                )
            )
            session.execute(
                delete_in_workspace(
                    PageLink,
                    workspace_id=workspace_id,
                    conditions=(PageLink.source_version_id.in_(private_ids),),
                )
            )
        page_service = PageService(self.session_factory)
        for page in pages:
            versions = session.scalars(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(ContextPageVersion.page_id == page.id,),
                ).order_by(ContextPageVersion.version_no.desc())
            ).all()
            if page.latest_draft_version_id in private_ids:
                page.latest_draft_version_id = next(
                    (
                        version.id
                        for version in versions
                        if version.version_status == "draft"
                        and version.graph_status == "linear"
                        and page_service.draft_still_open(session, page, version)
                    ),
                    None,
                )
            if page.current_version_id in private_ids:
                page.current_version_id = (
                    page.latest_draft_version_id or page.current_published_version_id
                )
            has_shared_history = any(
                version.version_status not in PRIVATE_VERSION_STATUSES
                for version in versions
            )
            if not has_shared_history and not any(
                version.version_status == "editing" for version in versions
            ):
                page.lifecycle_status = "archived"
                page.current_version_id = None
                page.title = "Deleted draft"
                page.slug = f"deleted-draft-{page.id.hex}"
                page.tags = []
                page.aliases = []
                page.placement_reason = None
                session.execute(
                    update_in_workspace(
                        WorkspaceTreeNode,
                        workspace_id=workspace_id,
                        conditions=(
                            WorkspaceTreeNode.target_kind == "context_page",
                            WorkspaceTreeNode.target_id == page.id,
                        ),
                        values={"title": page.title, "slug": page.slug},
                    )
                )
            elif page.lifecycle_status != "archived":
                page.lifecycle_status = (
                    "published" if page.current_published_version_id else "draft"
                )
            session.flush()
            sync_page_projections(session, page=page)
        session.flush()

    def _reclaim_orphan_media(
        self, session: Session, *, user_id: UUID, workspace_id: UUID
    ) -> None:
        # Reclaim only this user's unreachable associations. A surviving
        # shared/history usage keeps its asset and deduplicated blob intact.
        orphan_assets = session.scalars(
            select_rows_in_workspace(
                ContextMediaAsset,
                workspace_id=workspace_id,
                conditions=(
                    ContextMediaAsset.created_by_actor_kind == "human",
                    ContextMediaAsset.created_by_actor_id == str(user_id),
                    context_media_orphan_clause(),
                ),
            )
        ).all()
        if orphan_assets and self.context_media_repository_factory is None:
            raise UserDeletionError(
                "Context media cleanup is unavailable",
                error_code="user_deletion_unavailable",
                status_code=503,
            )
        if orphan_assets:
            repository = self.context_media_repository_factory(session)
            repository.lock_workspace_for_usage(workspace_id)
            for asset in orphan_assets:
                if asset.association_state != AssociationState.TOMBSTONED.value:
                    snapshot = repository.get_page_asset_usage(
                        workspace_id, asset.page_id, asset.id
                    )
                    if snapshot is None:
                        continue
                    DeleteUnusedMedia().execute(
                        repository,
                        workspace_id=workspace_id,
                        page_id=asset.page_id,
                        asset_id=asset.id,
                        usage_token=snapshot.usage_token,
                        actor=ActorIdentity("system", "user_deletion_worker"),
                    )
                repository.drain_gc_candidate(
                    workspace_id=workspace_id, asset_id=asset.id
                )
