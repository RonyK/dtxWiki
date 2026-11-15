"""User attribution columns and their account-cleanup treatment.

The registry identifies workspaces that hold a user's footprint and how cleanup
treats those rows. The unit guard requires every foreign key to ``users.id`` to
appear here so new references cannot silently prevent account removal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import String, and_, cast, func, literal, or_
from sqlalchemy.orm import InstrumentedAttribute

from backend.models.agent import AgentPageVersion
from backend.models.api_token import ApiToken
from backend.models.audit import AuditEvent
from backend.models.auth_identity import AuthLoginTransaction, UserIdentity
from backend.models.auth_verification import (
    EmailConflictRequest,
    EmailReauthenticationChallenge,
)
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.favorite import UserFavorite
from backend.models.inbox import (
    AccountInboxEntry,
    WorkspaceInboxEntry,
    WorkspaceInboxRecipient,
)
from backend.models.mail import EmailOutbox
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.models.oauth import (
    OAuthAccessTokenJti,
    OAuthCode,
    OAuthConsent,
    OAuthRefreshToken,
)
from backend.models.password_reset import PasswordResetToken
from backend.models.review import ReviewItem
from backend.models.server_admin import ServerAdmin
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.models.workspace_purge_grant import WorkspacePurgeGrant


def _no_conditions() -> tuple:
    return ()


def _private_version_outstanding() -> tuple:
    """Unsubmitted sole-author bytes retain their existing deletion treatment.

    Draft visibility is shared, so creation alone is insufficient: a request
    or another contributor's persisted write keeps the shared content intact.
    """
    version = ContextPageVersion
    own_id = func.replace(version.created_by_actor_id, "-", "")
    version_id = func.replace(cast(version.id, String), "-", "")
    reference = literal("context_page_version:") + version_id
    from backend.repositories.audit_repo import select_workspace_audit_events
    from backend.repositories.workspace_scoped import select_in_workspace

    operation_sets = (
        select_in_workspace(
            ChangeOperation,
            workspace_id=version.workspace_id,
            conditions=(func.replace(ChangeOperation.diff_ref, "-", "") == reference,),
        )
        .with_only_columns(ChangeOperation.change_set_id)
        .correlate(ContextPageVersion)
    )
    submitted = (
        select_in_workspace(
            ReviewItem,
            workspace_id=version.workspace_id,
            conditions=(ReviewItem.change_set_id.in_(operation_sets),),
        )
        .with_only_columns(ReviewItem.id)
        .correlate(ContextPageVersion)
        .exists()
    )
    contributor = (
        select_in_workspace(
            ChangeSet,
            workspace_id=version.workspace_id,
            conditions=(
                ChangeSet.id.in_(operation_sets),
                ChangeSet.actor_id.is_not(None),
                or_(
                    ChangeSet.actor_type != version.created_by_actor_kind,
                    func.replace(cast(ChangeSet.actor_id, String), "-", "") != own_id,
                ),
            ),
        )
        .with_only_columns(ChangeSet.id)
        .correlate(ContextPageVersion)
        .exists()
    )
    authored_audit = (
        select_workspace_audit_events(
            version.workspace_id,
            conditions=(
                func.replace(AuditEvent.payload["version_id"].as_string(), "-", "")
                == version_id,
                AuditEvent.action.in_(
                    (
                        "context_page.draft.save",
                        "context_page.references.update",
                        "page.patch_context_edit",
                        "page.update",
                        "page.autosave.draft",
                        "page.autosave.overwrite",
                    )
                ),
                or_(
                    AuditEvent.actor_kind != version.created_by_actor_kind,
                    func.replace(AuditEvent.actor_id, "-", "") != own_id,
                ),
            ),
        )
        .with_only_columns(AuditEvent.id)
        .correlate(ContextPageVersion)
        .exists()
    )
    last_actor_is_other = or_(
        version.actor_type != version.created_by_actor_kind,
        and_(
            version.actor_id.is_not(None),
            func.replace(cast(version.actor_id, String), "-", "") != own_id,
        ),
    )
    return (
        or_(
            and_(
                version.version_status == "discarded",
                or_(
                    version.body_md != "",
                    version.actor_display_name.is_not(None),
                ),
            ),
            and_(
                version.version_status == "draft",
                version.commit_oid.is_(None),
                ~submitted,
                ~contributor,
                ~authored_audit,
                ~last_actor_is_other,
            ),
        ),
    )


@dataclass(frozen=True)
class UserAttribution:
    """One way a row names a user, and what account cleanup does with it."""

    columns: tuple[InstrumentedAttribute, ...]
    # delete | clear | private_versions | author_name | api_tokens |
    # owner_projection | preserve | retire_inbox_recipient
    cleanup: str
    workspace_scoped: bool
    # (attribute, value) that marks the column as a human user id.
    actor_kind: tuple[str, str] | None = None
    # The column stores ``str(user_id)`` rather than a UUID.
    as_text: bool = False
    # Extra conditions selecting rows cleanup still has to change; cleaned
    # rows may keep matching the attribution itself.
    outstanding: Callable[[], tuple] = _no_conditions

    @property
    def model(self) -> type:
        return self.columns[0].class_

    def match(self, user_id: UUID) -> tuple:
        value = str(user_id) if self.as_text else user_id
        conditions = [column == value for column in self.columns]
        named = conditions[0] if len(conditions) == 1 else or_(*conditions)
        if self.actor_kind is None:
            return (named,)
        kind_name, kind_value = self.actor_kind
        return (getattr(self.model, kind_name) == kind_value, named)

    def pending(self, user_id: UUID) -> tuple:
        return (*self.match(user_id), *self.outstanding())


USER_ATTRIBUTIONS: tuple[UserAttribution, ...] = (
    # Workspace plane, in cleanup order. Unsubmitted sole-author drafts go
    # first; submitted or collaboratively changed versions retain their bytes.
    UserAttribution(
        (ContextPageVersion.created_by_actor_id,),
        "private_versions",
        workspace_scoped=True,
        actor_kind=("created_by_actor_kind", "human"),
        as_text=True,
        outstanding=_private_version_outstanding,
    ),
    # Submitted shared changes retain their author UUID. They can produce a
    # later review result even after every other user footprint is gone.
    UserAttribution(
        (ChangeSet.actor_id,),
        "preserve",
        workspace_scoped=True,
        actor_kind=("actor_type", "human"),
    ),
    UserAttribution(
        (ContextPageVersion.actor_id,),
        "author_name",
        workspace_scoped=True,
        actor_kind=("actor_type", "human"),
        outstanding=lambda: (ContextPageVersion.actor_display_name.is_not(None),),
    ),
    UserAttribution(
        (AgentPageVersion.actor_id,),
        "author_name",
        workspace_scoped=True,
        actor_kind=("actor_type", "human"),
        outstanding=lambda: (AgentPageVersion.actor_display_name.is_not(None),),
    ),
    UserAttribution(
        (McpChatSession.owner_user_id,),
        "clear",
        workspace_scoped=True,
    ),
    UserAttribution(
        (WorkspaceCredentialGrant.user_id,),
        "delete",
        workspace_scoped=True,
    ),
    UserAttribution(
        (WorkspacePurgeGrant.user_id,),
        "delete",
        workspace_scoped=True,
    ),
    UserAttribution((UserFavorite.user_id,), "delete", workspace_scoped=True),
    UserAttribution(
        (MCPWriteIdempotency.actor_id,),
        "delete",
        workspace_scoped=True,
        actor_kind=("actor_kind", "human"),
        as_text=True,
    ),
    # Call history outlives its 24-hour retry window; only the identity goes.
    UserAttribution(
        (MCPToolCall.actor_id,),
        "clear",
        workspace_scoped=True,
        actor_kind=("actor_kind", "human"),
        as_text=True,
    ),
    UserAttribution(
        (MCPClient.created_by, MCPClient.issued_to_user_id),
        "clear",
        workspace_scoped=True,
    ),
    # Producers lock source rows before the recipient. Finish source cleanup
    # first, retire the recipient using that same lock, then remove messages.
    UserAttribution(
        (WorkspaceInboxRecipient.user_id,),
        "retire_inbox_recipient",
        workspace_scoped=True,
        outstanding=lambda: (WorkspaceInboxRecipient.deleted_at.is_(None),),
    ),
    UserAttribution((WorkspaceInboxEntry.user_id,), "delete", workspace_scoped=True),
    # Identity plane, in foreign-key-safe deletion order. Memberships are
    # workspace rows but leave with the identity, before the owner projection
    # is recomputed from the remaining owners.
    UserAttribution((EmailOutbox.recipient_user_id,), "delete", False),
    UserAttribution((AccountInboxEntry.user_id,), "delete", False),
    # External sign-in links, pending sign-ins and confirmations, and requests
    # to release this account's email leave with it.
    UserAttribution((UserIdentity.user_id,), "delete", False),
    UserAttribution((AuthLoginTransaction.user_id,), "delete", False),
    UserAttribution((EmailReauthenticationChallenge.user_id,), "delete", False),
    UserAttribution((EmailConflictRequest.target_user_id,), "delete", False),
    UserAttribution((OAuthAccessTokenJti.user_id,), "delete", False),
    UserAttribution((OAuthCode.user_id,), "delete", False),
    UserAttribution((OAuthConsent.user_id,), "delete", False),
    UserAttribution((OAuthRefreshToken.user_id,), "delete", False),
    UserAttribution((PasswordResetToken.user_id,), "delete", False),
    UserAttribution((ApiToken.user_id,), "api_tokens", False),
    UserAttribution((WorkspaceMembership.user_id,), "delete", False),
    UserAttribution((ServerAdmin.user_id,), "delete", False),
    UserAttribution((WorkspaceJoinRequest.requester_user_id,), "delete", False),
    UserAttribution((WorkspaceJoinRequest.decided_by_user_id,), "clear", False),
    UserAttribution((PasswordResetToken.requested_by_user_id,), "clear", False),
    UserAttribution((Workspace.account_deletion_user_id,), "clear", False),
    UserAttribution((Workspace.owner_user_id,), "owner_projection", False),
)
