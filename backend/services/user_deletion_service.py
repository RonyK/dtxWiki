"""Retained account deletion for the single-database deployment profile.

Disabling credentials and scheduling destruction share one transaction. A
durable blob-purge intent survives the database cleanup so failed physical
deletion can be retried before the user identity is finally removed. Shared
workspace content and its append-only audit history remain workspace-owned.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from backend.context_media.application import DeleteUnusedMedia
from backend.context_media.domain import ActorIdentity, AssociationState
from backend.context_media.ports import ContextMediaRepository
from backend.domain.identity_policy import EmailPolicyError, normalize_email
from backend.models import (
    AgentPageVersion,
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
    MCPWriteIdempotency,
    OAuthCode,
    OAuthConsent,
    OAuthRefreshToken,
    PageLink,
    PasswordResetToken,
    ServerAdmin,
    User,
    UserFavorite,
    Workspace,
    WorkspaceCredentialGrant,
    WorkspaceJoinRequest,
    WorkspaceMembership,
    WorkspacePurgeGrant,
    WorkspaceTreeNode,
)
from backend.models.base import utc_now
from backend.models.oauth import OAuthAccessTokenJti
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
from backend.services.server_admin_service import is_server_admin
from backend.services.workspace_write import WorkspaceWriteService
from backend.utils.content_hash import content_hash

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 36500
_POLICY_KEY = "user_deletion_retention_days"
_BLOB_PENDING_PREFIX = "user_deletion_blob_pending:"
# All account deletions serialize the last-active-admin decision, including
# users who share no workspace. Workspace locks still protect owner changes.
_LIFECYCLE_LOCK = 0x44545855534552


class UserDeletionError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


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


def _require_admin(
    session: Session,
    user: User | None,
    expected_session_generation: int | None = None,
) -> None:
    if user is None or user.status != "active" or not is_server_admin(session, user.id):
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
                target_kind="server",
                target_id=actor_user_id,
                payload={"retention_days": days},
            )
        return days

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
            user, actor = self._lock_subject(session, user_id, actor_user_id)
            if admin_request:
                _require_admin(session, actor, expected_actor_session_generation)
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
            self._protect_owners(session, user)
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
            user.status = "deletion_pending"
            user.deletion_requested_at = requested_at
            user.deletion_retention_days = days
            user.purge_after = requested_at + timedelta(days=days)
            user.session_generation += 1
            self._revoke_access(session, user.id, requested_at)
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
                },
                retention_class="sensitive",
            )
            session.flush()
            return _record(user)

    @staticmethod
    def _lock_subject(
        session: Session, user_id: UUID, actor_user_id: UUID
    ) -> tuple[User, User | None]:
        # Follow media-intent -> workspace -> user, matching workspace deletion
        # and membership changes. Never hold a user lock while waiting on a
        # workspace another mutation may already have locked.
        personal_id = session.scalar(
            select(User.personal_workspace_id).where(User.id == user_id)
        )
        # Private drafts and uploads can survive removal of a membership.
        # Account-wide cleanup therefore takes the complete control roster,
        # including every media intent, before any workspace or user lock.
        workspace_ids = set(session.scalars(select(Workspace.id)).all())
        if personal_id is not None:
            workspace_ids.add(personal_id)
        for workspace_id in sorted(workspace_ids, key=lambda value: value.int):
            WorkspaceWriteService._lock_media_delete_intent(
                session=session, workspace_id=workspace_id
            )
        if workspace_ids:
            session.scalars(
                select(Workspace)
                .where(Workspace.id.in_(workspace_ids))
                .order_by(Workspace.id)
                .with_for_update()
            ).all()
        users = {
            user.id: user
            for user in session.scalars(
                select(User)
                .where(User.id.in_({user_id, actor_user_id}))
                .order_by(User.id)
                .with_for_update()
            )
        }
        user = users.get(user_id)
        if user is None:
            raise UserDeletionError(
                "user not found", error_code="user_deletion_not_found", status_code=404
            )
        if user.personal_workspace_id != personal_id:
            raise UserDeletionError(
                "personal workspace changed; retry account deletion",
                error_code="user_deletion_unavailable",
                status_code=503,
            )
        current_workspace_ids = set(
            session.scalars(
                select(WorkspaceMembership.workspace_id).where(
                    WorkspaceMembership.user_id == user_id
                )
            ).all()
        )
        if not current_workspace_ids <= workspace_ids:
            # A membership may have arrived before we acquired the user lock.
            # Retry the complete workspace-before-user lock sequence rather
            # than acquiring its workspace here in the reverse order.
            raise UserDeletionError(
                "workspace memberships changed; retry account deletion",
                error_code="user_deletion_unavailable",
                status_code=503,
            )
        return user, users.get(actor_user_id)

    @staticmethod
    def _protect_owners(session: Session, user: User) -> None:
        if user.status == "active" and is_server_admin(session, user.id):
            other_admin = session.scalar(
                select(User.id)
                .join(ServerAdmin, ServerAdmin.user_id == User.id)
                .where(User.id != user.id, User.status == "active")
                .limit(1)
            )
            if other_admin is None:
                raise UserDeletionError(
                    "the server must retain another active administrator",
                    error_code="user_deletion_last_server_admin",
                    status_code=409,
                )
        owned = session.scalars(
            select(WorkspaceMembership.workspace_id).where(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.role == "owner",
            )
        ).all()
        for workspace_id in owned:
            if workspace_id == user.personal_workspace_id:
                continue
            other_owner = session.scalar(
                select(User.id)
                .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.role == "owner",
                    User.id != user.id,
                    User.status == "active",
                )
                .limit(1)
            )
            if other_owner is None:
                raise UserDeletionError(
                    "transfer shared workspace ownership before deleting the account",
                    error_code="user_deletion_last_owner",
                    status_code=409,
                )

    @staticmethod
    def _revoke_access(session: Session, user_id: UUID, now: datetime) -> None:
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
        for workspace_id in session.scalars(select(Workspace.id)).all():
            session.execute(
                update_in_workspace(
                    WorkspaceCredentialGrant,
                    workspace_id=workspace_id,
                    conditions=(WorkspaceCredentialGrant.user_id == user_id,),
                    values={"revoked_at": now},
                )
            )
            session.execute(
                delete_in_workspace(
                    WorkspacePurgeGrant,
                    workspace_id=workspace_id,
                    conditions=(WorkspacePurgeGrant.user_id == user_id,),
                )
            )
            session.execute(
                update_in_workspace(
                    MCPClient,
                    workspace_id=workspace_id,
                    conditions=(MCPClient.issued_to_user_id == user_id,),
                    values={"status": "disabled"},
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
            except Exception:
                # The user and intent stay pending for a later bounded sweep.
                logger.exception(
                    "account deletion cleanup failed", extra={"user_id": str(user_id)}
                )
                # Move failed work behind untouched due users. A bounded
                # sweep must not let its first persistent failures starve
                # the rest of the deletion queue on every later tick.
                try:
                    with self.session_factory.begin() as session:
                        session.execute(
                            update(User)
                            .where(
                                User.id == user_id,
                                User.status == "deletion_pending",
                            )
                            .values(updated_at=utc_now())
                        )
                except Exception:
                    logger.exception(
                        "account deletion retry ordering update failed",
                        extra={"user_id": str(user_id)},
                    )
        return purged

    def _purge_one(
        self, user_id: UUID, blob_store_provider: object, now: datetime
    ) -> bool:
        intent_key = f"{_BLOB_PENDING_PREFIX}{user_id}"
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            if session.scalar(select(User.id).where(User.id == user_id)) is None:
                return False
            user, _ = self._lock_subject(session, user_id, user_id)
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
                if workspace_id is not None:
                    WorkspaceWriteService.purge_personal_workspace_for_deleted_user(
                        session=session, user=user
                    )
                self._cleanup_user_resources(session, user_id)
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
                        else None
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
            user, _ = self._lock_subject(session, user_id, user_id)
            intent = session.get(AppMetadata, intent_key)
            if user.status != "deletion_pending" or intent is None:
                raise RuntimeError("account deletion intent changed during cleanup")
            self._cleanup_user_resources(session, user_id)
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
        return True

    def _cleanup_user_resources(self, session: Session, user_id: UUID) -> None:
        # Shared content stays; personal preferences, connections and chat
        # projections are explicitly removed even when FK cascades are off.
        for workspace_id in session.scalars(select(Workspace.id)).all():
            self._cleanup_workspace_user_resources(
                session, user_id=user_id, workspace_id=workspace_id
            )
        token_ids = select(ApiToken.id).where(ApiToken.user_id == user_id)
        session.execute(
            delete(ApiTokenWorkspace).where(ApiTokenWorkspace.token_id.in_(token_ids))
        )
        session.execute(
            delete(EmailOutbox).where(EmailOutbox.recipient_user_id == user_id)
        )
        for model in (
            OAuthAccessTokenJti,
            OAuthCode,
            OAuthConsent,
            OAuthRefreshToken,
            PasswordResetToken,
            ApiToken,
            WorkspaceMembership,
            ServerAdmin,
        ):
            session.execute(delete(model).where(model.user_id == user_id))
        session.execute(
            delete(WorkspaceJoinRequest).where(
                WorkspaceJoinRequest.requester_user_id == user_id
            )
        )
        for model, column in (
            (WorkspaceJoinRequest, WorkspaceJoinRequest.decided_by_user_id),
            (PasswordResetToken, PasswordResetToken.requested_by_user_id),
            (Workspace, Workspace.owner_user_id),
        ):
            session.execute(
                update(model).where(column == user_id).values({column.key: None})
            )
        session.flush()

    def _cleanup_workspace_user_resources(
        self, session: Session, *, user_id: UUID, workspace_id: UUID
    ) -> None:
        self._scrub_private_context_versions(
            session, user_id=user_id, workspace_id=workspace_id
        )
        for model in (ContextPageVersion, AgentPageVersion):
            session.execute(
                update_in_workspace(
                    model,
                    workspace_id=workspace_id,
                    conditions=(model.actor_type == "human", model.actor_id == user_id),
                    values={"actor_display_name": None},
                )
            )
        chat_ids = select_in_workspace(
            McpChatSession,
            workspace_id=workspace_id,
            conditions=(McpChatSession.owner_user_id == user_id,),
        ).with_only_columns(McpChatSession.id)
        session.execute(
            update_in_workspace(
                ChangeSet,
                workspace_id=workspace_id,
                conditions=(ChangeSet.origin_chat_session_id.in_(chat_ids),),
                values={"origin_chat_session_id": None},
            )
        )
        for model in (McpChatArtifactLink, McpChatToolCallArtifact):
            session.execute(
                delete_in_workspace(
                    model,
                    workspace_id=workspace_id,
                    conditions=(model.chat_session_id.in_(chat_ids),),
                )
            )
        session.execute(
            delete_in_workspace(
                McpChatSession,
                workspace_id=workspace_id,
                conditions=(McpChatSession.owner_user_id == user_id,),
            )
        )
        for model in (WorkspaceCredentialGrant, WorkspacePurgeGrant, UserFavorite):
            session.execute(
                delete_in_workspace(
                    model,
                    workspace_id=workspace_id,
                    conditions=(model.user_id == user_id,),
                )
            )
        session.execute(
            delete_in_workspace(
                MCPWriteIdempotency,
                workspace_id=workspace_id,
                conditions=(
                    MCPWriteIdempotency.actor_kind == "human",
                    MCPWriteIdempotency.actor_id == str(user_id),
                ),
            )
        )
        for column in (MCPClient.created_by, MCPClient.issued_to_user_id):
            session.execute(
                update_in_workspace(
                    MCPClient,
                    workspace_id=workspace_id,
                    conditions=(column == user_id,),
                    values={column.key: None},
                )
            )

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
            raise RuntimeError("private_context_version_has_public_exposure")
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
                        and page_service._draft_still_open(session, page, version)
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
