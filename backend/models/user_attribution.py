"""Every column that names a user, and the flush guard that protects them.

Account deletion needs three views of one set: which workspaces still hold a
user's footprint, how cleanup treats each kind of row, and which new rows must
not name an inactive or already deleted user. Keeping one registry means a new
attribution column cannot reach one of those views and miss the others; the
static guard in the unit tests requires every foreign key to ``users.id`` to
appear here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import event, inspect, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from backend.models.agent import AgentPageVersion
from backend.models.api_token import ApiToken
from backend.models.app_metadata import AppMetadata
from backend.models.context import ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.favorite import UserFavorite
from backend.models.mail import EmailOutbox
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.oauth import (
    OAuthAccessTokenJti,
    OAuthCode,
    OAuthConsent,
    OAuthRefreshToken,
)
from backend.models.password_reset import PasswordResetToken
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.models.workspace_purge_grant import WorkspacePurgeGrant

# Final account cleanup leaves this marker so a request that authenticated
# before the deletion cannot recreate the user's rows after the user is gone.
DELETED_USER_KEY_PREFIX = "user_deleted:"


class ActorInactiveError(RuntimeError):
    error_code = "account_inactive"


def _no_conditions() -> tuple:
    return ()


def _private_version_outstanding() -> tuple:
    from backend.repositories.context_visibility import PRIVATE_VERSION_STATUSES

    return (
        ContextPageVersion.version_status.in_(PRIVATE_VERSION_STATUSES),
        or_(
            ContextPageVersion.version_status == "editing",
            ContextPageVersion.body_md != "",
            ContextPageVersion.actor_display_name.is_not(None),
        ),
    )


def _orphan_media_outstanding() -> tuple:
    from backend.repositories.context_media_orphans import (
        context_media_orphan_clause,
    )

    return (context_media_orphan_clause(),)


@dataclass(frozen=True)
class UserAttribution:
    """One way a row names a user, and what account cleanup does with it."""

    columns: tuple[InstrumentedAttribute, ...]
    # delete | clear | private_versions | author_name | chat_sessions |
    # orphan_media | api_tokens | owner_projection
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

    def named_users(self, instance: object) -> set[UUID]:
        if self.actor_kind is not None:
            kind_name, kind_value = self.actor_kind
            if getattr(instance, kind_name, None) != kind_value:
                return set()
        named: set[UUID] = set()
        for column in self.columns:
            value = getattr(instance, column.key, None)
            if isinstance(value, str):
                try:
                    value = UUID(value)
                except ValueError:
                    continue
            if isinstance(value, UUID):
                named.add(value)
        return named


USER_ATTRIBUTIONS: tuple[UserAttribution, ...] = (
    # Workspace plane, in cleanup order.
    UserAttribution(
        (ContextPageVersion.created_by_actor_id,),
        "private_versions",
        workspace_scoped=True,
        actor_kind=("created_by_actor_kind", "human"),
        as_text=True,
        outstanding=_private_version_outstanding,
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
        "chat_sessions",
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
    UserAttribution(
        (MCPClient.created_by, MCPClient.issued_to_user_id),
        "clear",
        workspace_scoped=True,
    ),
    UserAttribution(
        (ContextMediaAsset.created_by_actor_id,),
        "orphan_media",
        workspace_scoped=True,
        actor_kind=("created_by_actor_kind", "human"),
        as_text=True,
        outstanding=_orphan_media_outstanding,
    ),
    # Identity plane, in foreign-key-safe deletion order. Memberships are
    # workspace rows but leave with the identity, before the owner projection
    # is recomputed from the remaining owners.
    UserAttribution((EmailOutbox.recipient_user_id,), "delete", False),
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
    UserAttribution((Workspace.owner_user_id,), "owner_projection", False),
)


def _references_users(column: InstrumentedAttribute) -> bool:
    return any(
        foreign_key.column.table.name == User.__tablename__
        for foreign_key in column.property.columns[0].foreign_keys
    )


# A foreign-key column already serializes with the user row (the FK check
# takes KEY SHARE) and cannot outlive it, and system repairs legitimately add
# such rows for suspended users. Bare attribution columns get the flush guard.
_GUARDED_BY_MODEL: dict[type, tuple[UserAttribution, ...]] = {}
for _entry in USER_ATTRIBUTIONS:
    if not any(_references_users(column) for column in _entry.columns):
        _GUARDED_BY_MODEL[_entry.model] = (
            *_GUARDED_BY_MODEL.get(_entry.model, ()),
            _entry,
        )

_USERS_TABLE_PRESENT: WeakKeyDictionary = WeakKeyDictionary()


def _users_table_present(connection) -> bool:
    # A physically split workspace shard has no identity tables; account
    # deletion is unavailable there, so the guard has nothing to protect.
    engine = connection.engine
    if _USERS_TABLE_PRESENT.get(engine):
        return True
    present = inspect(connection).has_table(User.__tablename__)
    if present:
        _USERS_TABLE_PRESENT[engine] = True
    return present


def _refuse_inactive_attributions(session: Session, _flush_context) -> None:
    # after_flush still lists the instances this flush inserted in
    # ``session.new``; raising here rolls those inserts back with the flush.
    named: set[UUID] = set()
    for instance in session.new:
        for entry in _GUARDED_BY_MODEL.get(type(instance), ()):
            named |= entry.named_users(instance)
    if not named:
        return
    connection = session.connection()
    if not _users_table_present(connection):
        return
    # The check runs after the INSERTs: this transaction already holds the
    # workspace foreign-key lock (and SQLite's write lock), so taking the user
    # row next keeps the workspace-then-user order that membership changes and
    # account deletion use. KEY SHARE conflicts with their FOR UPDATE on the
    # user, so either this insert commits first and cleanup sees it, or it
    # sees the committed deletion.
    rows = connection.execute(
        select(User.id, User.status)
        .where(User.id.in_(named))
        .with_for_update(read=True, key_share=True)
    ).all()
    refused = {row.id for row in rows if row.status != "active"}
    missing = named - {row.id for row in rows}
    if missing:
        markers = connection.execute(
            select(AppMetadata.key).where(
                AppMetadata.key.in_(
                    [f"{DELETED_USER_KEY_PREFIX}{user_id}" for user_id in missing]
                )
            )
        ).scalars()
        refused |= {UUID(key.removeprefix(DELETED_USER_KEY_PREFIX)) for key in markers}
    if refused:
        raise ActorInactiveError("the account is no longer active")


event.listen(Session, "after_flush", _refuse_inactive_attributions)
