"""Retained account deletion for the single-database deployment profile.

Disabling credentials and scheduling destruction share one transaction. A
workspace whose only admin is the departing account is held with it: closed to
members while the account is retained, then deleted with it unless an active
admin is appointed first. A durable blob-purge manifest survives the database
cleanup so failed physical deletion can be retried before the user identity is
finally removed. Other workspaces keep their shared content and append-only
audit history; the account's unsubmitted private edits there are deleted.

Only the workspaces that hold the user's footprint are locked; the footprint
comes from the single attribution registry in ``backend.models.user_attribution``.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from backend.domain.identity_policy import EmailPolicyError, normalize_email
from backend.models import (
    ApiToken,
    ApiTokenWorkspace,
    AppMetadata,
    ChangeOperation,
    ChunkEmbedding,
    ContentChunk,
    ContextMediaUsage,
    ContextPage,
    ContextPageVersion,
    EmailOutbox,
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
    USER_ATTRIBUTIONS,
    UserAttribution,
)
from backend.repositories.context_visibility import PRIVATE_VERSION_STATUSES
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    select_in_workspace,
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
# SQLite keeps no queue of waiting writers: a blocked writer's busy handler
# sleeps up to 100 ms between retries, so back-to-back deletion transactions
# would keep it waiting until the last one. A short pause after each held
# workspace lets other writes in (measured: 300 workspaces, other writes wait
# at most 0.06 s instead of the whole cleanup).
_SQLITE_WRITER_YIELD_SECONDS = 0.05
# All account deletions serialize the last-active-admin decision, including
# users who share no workspace. Workspace locks still protect owner changes.
_LIFECYCLE_LOCK = 0x44545855534552


class UserDeletionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        status_code: int,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code
        self.details = details


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
    return workspace_ids


def _private_draft_page_ids(
    session: Session, user_id: UUID, workspace_id: UUID
) -> set[UUID]:
    # The pages whose unsubmitted private edits cleanup deletes.
    return set(
        session.scalars(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.created_by_actor_kind == "human",
                    ContextPageVersion.created_by_actor_id == str(user_id),
                    ContextPageVersion.version_status.in_(PRIVATE_VERSION_STATUSES),
                ),
            ).with_only_columns(ContextPageVersion.page_id)
        )
    )


def _owned_workspace_ids(session: Session, user_id: UUID) -> set[UUID]:
    return set(
        session.scalars(
            select(WorkspaceMembership.workspace_id).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "owner",
            )
        )
    )


def _co_owner_ids(
    session: Session, workspace_ids: set[UUID], user_id: UUID
) -> set[UUID]:
    if not workspace_ids:
        return set()
    return set(
        session.scalars(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id.in_(workspace_ids),
                WorkspaceMembership.role == "owner",
                WorkspaceMembership.user_id != user_id,
            )
        )
    )


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


def _other_active_member_count(
    session: Session, workspace_id: UUID, user_id: UUID
) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(WorkspaceMembership)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id != user_id,
                User.status == "active",
            )
        )
        or 0
    )


def _sole_admin_workspaces(
    session: Session, workspaces: list[Workspace], user_id: UUID
) -> list[dict]:
    """Owned workspaces nobody else could manage, with their active members.

    A workspace already held for another account keeps that sponsor.
    """

    return [
        {
            "workspace_id": workspace.id,
            "title": workspace.title,
            "other_member_count": _other_active_member_count(
                session, workspace.id, user_id
            ),
        }
        for workspace in sorted(workspaces, key=lambda item: (item.title, item.id.int))
        if workspace.account_deletion_user_id is None
        and not _has_other_active_owner(session, workspace.id, user_id)
    ]


def _split_targets(
    sole_admin: list[dict], *, admin_request: bool
) -> tuple[list[dict], list[dict]]:
    """Targets held with the account, and workspaces blocking self-withdrawal.

    A member leaving cannot decide for other members: workspaces with other
    active members must be deleted (or given another admin) first. A server
    administrator approves every workspace that would be left unmanaged.
    """

    if admin_request:
        return sole_admin, []
    return (
        [item for item in sole_admin if item["other_member_count"] == 0],
        [item for item in sole_admin if item["other_member_count"] > 0],
    )


def _approved_key(items: list[dict]) -> set[tuple[UUID, int]]:
    return {
        (UUID(str(item["workspace_id"])), int(item["other_member_count"]))
        for item in items
    }


def _manifest_ids(value: str | None) -> list[UUID]:
    # Older retries stored one workspace id (or none) instead of a list.
    if not value:
        return []
    if value.startswith("["):
        return [UUID(item) for item in json.loads(value)]
    return [UUID(value)]


@dataclass(frozen=True)
class UserDeletionService:
    session_factory: object
    credential_hasher: CredentialHasher = field(default_factory=CredentialHasher)
    audit_service: AuditService = field(default_factory=AuditService)

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

    def deletion_preview(self, *, user_id: UUID, admin_request: bool = False) -> dict:
        """What a deletion of ``user_id`` would hold, or what blocks it, now.

        Advisory only: ``request_deletion`` recomputes everything under locks
        and requires the caller to approve exactly the targets shown here.
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
            owned_ids = _owned_workspace_ids(session, user.id)
            owned = (
                list(
                    session.scalars(
                        select(Workspace).where(Workspace.id.in_(owned_ids))
                    )
                )
                if owned_ids
                else []
            )
            targets, blocking = _split_targets(
                _sole_admin_workspaces(session, owned, user.id),
                admin_request=admin_request,
            )
        return {
            "retention_days": days,
            "target_workspaces": targets,
            "blocking_workspaces": blocking,
        }

    def _require_self_confirmation(
        self,
        user: User,
        current_password: str | None,
        verified_session_generation: int | None,
    ) -> None:
        # An account with a password confirms with it; one signed in only
        # through an external provider needs a recent account verification.
        if user.password_hash is not None:
            if not self.credential_hasher.verify_password(
                current_password or "", user.password_hash
            ):
                raise UserDeletionError(
                    "the current password is incorrect",
                    error_code="invalid_credentials",
                    status_code=401,
                )
        elif verified_session_generation != user.session_generation:
            raise UserDeletionError(
                "recent account verification is required",
                error_code="recent_reauthentication_required",
                status_code=401,
            )

    def request_deletion(
        self,
        *,
        user_id: UUID,
        actor_user_id: UUID,
        confirmation_email: str | None = None,
        current_password: str | None = None,
        expected_retention_days: int | None = None,
        admin_request: bool = False,
        expected_actor_session_generation: int | None = None,
        approved_workspaces: list[dict] | None = None,
    ) -> dict:
        """Hold ``user_id`` for deletion after its retention period.

        ``expected_actor_session_generation`` is the session generation a
        recent confirmation was verified against: the administrator's, or for
        self-withdrawal from an account without a password, the account's own.
        """

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
            # The account's email is typed to confirm; an account without one
            # is confirmed by its id alone.
            try:
                confirmed_email = (
                    None
                    if confirmation_email is None
                    else normalize_email(confirmation_email)
                )
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
            if not admin_request:
                self._require_self_confirmation(
                    user, current_password, expected_actor_session_generation
                )
            if user.status == "deletion_pending":
                return _record(user)
            other_admins = [
                admin_id
                for admin_id in admin_ids
                if admin_id != user.id
                and admin_id in granted
                and session.get(User, admin_id).status == "active"
            ]
            if user.status == "active" and user.id in granted and not other_admins:
                raise UserDeletionError(
                    "the server must retain another active administrator",
                    error_code="user_deletion_last_server_admin",
                    status_code=409,
                )
            targets, blocking = _split_targets(
                _sole_admin_workspaces(session, owned, user.id),
                admin_request=admin_request,
            )
            if blocking:
                raise UserDeletionError(
                    "delete these workspaces or appoint another admin first",
                    error_code="user_deletion_last_owner",
                    status_code=409,
                    details={
                        "blocking_workspaces": [
                            {**item, "workspace_id": str(item["workspace_id"])}
                            for item in blocking
                        ]
                    },
                )
            if _approved_key(approved_workspaces or []) != _approved_key(targets):
                raise UserDeletionError(
                    "the workspaces held with this account changed; review them again",
                    error_code="user_deletion_targets_changed",
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
            target_ids = {item["workspace_id"] for item in targets}
            for workspace in owned:
                if workspace.id in target_ids:
                    workspace.account_deletion_user_id = user.id
                WorkspaceMembershipService._sync_owner_projection(session, workspace)
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
                    "target_workspace_ids": sorted(
                        str(workspace_id) for workspace_id in target_ids
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
            # Workspaces before users, as membership changes lock them: the
            # account's own held workspaces, and held workspaces it co-owns
            # that an active restore gives an admin again.
            held_ids = set(
                session.scalars(
                    select(Workspace.id).where(
                        Workspace.account_deletion_user_id == user_id
                    )
                )
            ) | set(
                session.scalars(
                    select(Workspace.id)
                    .join(
                        WorkspaceMembership,
                        WorkspaceMembership.workspace_id == Workspace.id,
                    )
                    .where(
                        WorkspaceMembership.user_id == user_id,
                        WorkspaceMembership.role == "owner",
                        Workspace.account_deletion_user_id.is_not(None),
                    )
                )
            )
            held = (
                list(
                    session.scalars(
                        select(Workspace)
                        .where(Workspace.id.in_(held_ids))
                        .order_by(Workspace.id)
                        .with_for_update(key_share=True)
                    )
                )
                if held_ids
                else []
            )
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
            session.flush()
            released = []
            for workspace in held:
                if workspace.account_deletion_user_id == user.id:
                    workspace.account_deletion_user_id = None
                # An active owner reopens it (another account's hold too).
                WorkspaceMembershipService._sync_owner_projection(session, workspace)
                if workspace.account_deletion_user_id is None:
                    released.append(str(workspace.id))
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
                    "released_workspace_ids": sorted(released),
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
        owned_ids = _owned_workspace_ids(session, user_id)
        # Only a server administrator's own deletion needs the other grants:
        # the server must keep another active administrator.
        admin_ids = (
            set(session.scalars(select(ServerAdmin.user_id)))
            if session.get(ServerAdmin, user_id) is not None
            else set()
        )
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
        # Co-owners decide which workspaces are left without an admin, so
        # their status must not change while that is decided.
        co_owner_ids = _co_owner_ids(session, owned_ids, user_id)
        users = {
            user.id: user
            for user in session.scalars(
                select(User)
                .where(User.id.in_({user_id, actor_user_id, *admin_ids, *co_owner_ids}))
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
            _owned_workspace_ids(session, user_id) != owned_ids
            or _co_owner_ids(session, owned_ids, user_id) != co_owner_ids
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
    def _lock_for_purge(session: Session, user_id: UUID) -> tuple[User, set[UUID]]:
        # Media intent, then workspace, then the pages whose private drafts
        # cleanup deletes (in global id order, as history repair locks them)
        # -- the order workspace deletion and Context writers use -- but only
        # where the user has a footprint, so unrelated workspaces keep
        # writing. The user row comes last. Restore and other account
        # lifecycle changes are serialized by the lifecycle lock.
        user = session.get(User, user_id)
        footprint = _user_footprint(session, user)
        drafts = {
            workspace_id: _private_draft_page_ids(session, user_id, workspace_id)
            for workspace_id in footprint
        }
        ordered = sorted(footprint, key=lambda value: value.int)
        for workspace_id in ordered:
            if drafts[workspace_id]:
                WorkspaceWriteService._lock_media_delete_intent(
                    session=session, workspace_id=workspace_id
                )
        for workspace_id in ordered:
            session.scalar(
                select(Workspace.id)
                .where(Workspace.id == workspace_id)
                .with_for_update(key_share=True)
            )
        pages = {
            (page_id, workspace_id)
            for workspace_id, page_ids in drafts.items()
            for page_id in page_ids
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
        session.refresh(user, with_for_update=True)
        if not _user_footprint(session, user) <= footprint or any(
            (page_id, workspace_id) not in pages
            for workspace_id in footprint
            for page_id in _private_draft_page_ids(session, user_id, workspace_id)
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
        return purged

    def _purge_one(
        self, user_id: UUID, blob_store_provider: object, now: datetime
    ) -> bool:
        intent_key = f"{_BLOB_PENDING_PREFIX}{user_id}"
        # 1. The manifest names the held workspaces to delete. Once it exists
        #    the account can no longer be restored; later sweeps resume from it.
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            user = session.get(User, user_id)
            if (
                user is None
                or user.status != "deletion_pending"
                or user.purge_after is None
                or user.purge_after > now
            ):
                return False
            yield_writer = session.get_bind().dialect.name == "sqlite"
            intent = session.get(AppMetadata, intent_key)
            if intent is None:
                manifest = sorted(
                    session.scalars(
                        select(Workspace.id).where(
                            Workspace.account_deletion_user_id == user_id
                        )
                    ),
                    key=lambda value: value.int,
                )
            else:
                manifest = _manifest_ids(intent.value)
            stores = {}
            for workspace_id in manifest:
                store = blob_store_provider.store_for(workspace_id)
                if not callable(getattr(store, "purge_workspace", None)):
                    raise UserDeletionError(
                        "blob storage cannot reclaim deleted workspace data",
                        error_code="user_deletion_unavailable",
                        status_code=503,
                    )
                stores[workspace_id] = store
            if intent is None:
                session.add(
                    AppMetadata(
                        key=intent_key,
                        value=json.dumps(
                            [str(workspace_id) for workspace_id in manifest]
                        ),
                    )
                )
        # 2. One transaction per workspace: SQLite has a single writer for the
        #    whole database, so every other write waits only for one
        #    workspace's deletion, not all of them. Each workspace's files go
        #    right after its rows commit; a retry repeats that idempotently.
        deleted = []
        for workspace_id in manifest:
            if self._delete_held_workspace(user_id, workspace_id):
                stores[workspace_id].purge_workspace(workspace_id)
                deleted.append(workspace_id)
            if yield_writer:
                time.sleep(_SQLITE_WRITER_YIELD_SECONDS)
        # 3. The account's references in the remaining workspaces, then the
        #    identity itself.
        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            if session.scalar(select(User.id).where(User.id == user_id)) is None:
                return False
            user, footprint = self._lock_for_purge(session, user_id)
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
                action="auth.user.deletion_data_purged",
                target_kind="user",
                target_id=user_id,
                payload={
                    "deleted_workspace_ids": [
                        str(workspace_id) for workspace_id in deleted
                    ],
                    "kept_workspace_ids": [
                        str(workspace_id)
                        for workspace_id in manifest
                        if workspace_id not in deleted
                    ],
                    "cleaned_workspace_count": len(footprint),
                },
                retention_class="sensitive",
            )
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

    def _delete_held_workspace(self, user_id: UUID, workspace_id: UUID) -> bool:
        """Delete one held workspace; False if an admin was appointed first."""

        with self.session_factory.begin() as session:
            _serialize_lifecycle(session)
            user = session.get(User, user_id)
            if user is None or user.status != "deletion_pending":
                raise UserDeletionError(
                    "account deletion intent changed during cleanup",
                    error_code="user_deletion_intent_changed",
                    status_code=409,
                )
            return WorkspaceWriteService.purge_account_deletion_target(
                session=session, user=user, workspace_id=workspace_id
            )

    def _cleanup_user_resources(
        self, session: Session, user_id: UUID, workspace_ids: set[UUID]
    ) -> None:
        # Shared documents, submitted drafts, media and conversation history
        # stay. Unsubmitted private edits, credentials and preferences go, and
        # nullable account links are detached.
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
