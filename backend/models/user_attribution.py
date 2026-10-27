"""User attribution columns and their account-cleanup treatment.

The registry identifies workspaces that hold a user's footprint and how cleanup
treats those rows. The unit guard requires every foreign key to ``users.id`` to
appear here so new references cannot silently prevent account removal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import InstrumentedAttribute

from backend.models.agent import AgentPageVersion
from backend.models.api_token import ApiToken
from backend.models.context import ContextPageVersion
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
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.models.workspace_purge_grant import WorkspacePurgeGrant


def _no_conditions() -> tuple:
    return ()


@dataclass(frozen=True)
class UserAttribution:
    """One way a row names a user, and what account cleanup does with it."""

    columns: tuple[InstrumentedAttribute, ...]
    # delete | clear | author_name | api_tokens | owner_projection
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
    # Workspace plane, in cleanup order.
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
    UserAttribution(
        (MCPClient.created_by, MCPClient.issued_to_user_id),
        "clear",
        workspace_scoped=True,
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
