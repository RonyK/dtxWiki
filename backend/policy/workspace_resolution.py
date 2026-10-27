"""Workspace resolution sweep — Sprint_5 Phase_1.

Closes R-F6-6 by centralising actor/request workspace selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.trust_boundaries import Action, check_actor_can_access

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.policy.auth import ActorContext


class WorkspaceAmbiguityError(Exception):
    """No single workspace can be selected safely for the actor/request."""


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: UUID
    source: str


def resolve_workspace_for_actor(
    *,
    session: Session,
    actor: ActorContext,
    requested_workspace_id: UUID | None,
) -> WorkspaceContext:
    """Resolve the workspace context for an actor under the Sprint_5 priority.

    Priority, highest first:
      1. OAuth ``token_workspace_ids`` claim binding.
      2. Explicit request/path/query workspace id.
      3. A live, accessible persisted user default.
      4. A single remaining workspace membership.

    The function deliberately chooses a workspace only; access is checked by
    ``backend.policy.trust_boundaries.check_actor_can_access``.
    """

    actor_kind = getattr(actor, "actor_kind", None)
    if actor_kind == "system":
        if requested_workspace_id is not None:
            return WorkspaceContext(requested_workspace_id, "explicit")
        raise WorkspaceAmbiguityError("system actor requires an explicit workspace")

    if actor_kind == "mcp_client":
        raise WorkspaceAmbiguityError(
            "direct mcp_client actors are not supported; use PAT or OAuth"
        )

    token_workspace_ids = tuple(getattr(actor, "token_workspace_ids", ()) or ())
    if token_workspace_ids:
        token_set = set(token_workspace_ids)
        if requested_workspace_id is not None:
            if requested_workspace_id not in token_set:
                raise WorkspaceAmbiguityError("workspace token claim mismatch")
            source = "token_claim" if len(token_set) == 1 else "explicit"
            return WorkspaceContext(requested_workspace_id, source)
        if len(token_set) == 1:
            return WorkspaceContext(token_workspace_ids[0], "token_claim")
        raise WorkspaceAmbiguityError("workspace token claim is ambiguous")

    if requested_workspace_id is not None:
        return WorkspaceContext(requested_workspace_id, "explicit")

    user_id = getattr(actor, "user_id", None)
    if user_id is not None:
        user = session.get(User, user_id)
        if user is not None:
            default_workspace_id = user.default_workspace_id
            if _user_can_access_workspace(
                session,
                user_id=user_id,
                workspace_id=default_workspace_id,
            ):
                assert default_workspace_id is not None
                return WorkspaceContext(default_workspace_id, "user_default")
        membership_workspace_ids = list(
            session.scalars(
                select(WorkspaceMembership.workspace_id)
                .where(WorkspaceMembership.user_id == user_id)
                .order_by(WorkspaceMembership.workspace_id)
            ).all()
        )
        if len(membership_workspace_ids) == 1:
            return WorkspaceContext(membership_workspace_ids[0], "membership_single")
        if len(membership_workspace_ids) > 1:
            raise WorkspaceAmbiguityError("workspace picker selection is required")
        raise WorkspaceAmbiguityError("workspace discovery is required")

    raise WorkspaceAmbiguityError("non-user actor requires an explicit workspace")


def resolve_and_check(
    *,
    session: Session,
    actor: ActorContext,
    action: Action,
    requested_workspace_id: UUID | None = None,
) -> WorkspaceContext:
    """Phase_1 §11.1 helper — combine resolve + access check in one call.

    Phase_2 §3.10.1 contract: every MCP callsite invokes only this helper,
    never the underlying ``resolve_workspace_for_actor`` /
    ``check_actor_can_access`` pair directly. MCP HTTP may pass an explicit
    workspace id from JSON-RPC tool arguments; when absent the workspace is
    selected from token and user membership rules in
    ``resolve_workspace_for_actor`` and then gated by
    ``check_actor_can_access(action)``.

    Raises:
        WorkspaceAmbiguityError: no single workspace can be selected.
        PermissionError: actor not allowed for ``action`` on the resolved
            workspace (for example ``token_workspace_denied``,
            ``workspace_membership_missing``, or
            ``workspace_role_insufficient``).
    """

    workspace = resolve_workspace_for_actor(
        session=session,
        actor=actor,
        requested_workspace_id=requested_workspace_id,
    )
    check_actor_can_access(actor, workspace.workspace_id, action, session=session)
    return workspace


def _user_can_access_workspace(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID | None,
) -> bool:
    if workspace_id is None or session.get(Workspace, workspace_id) is None:
        return False
    return session.get(WorkspaceMembership, (user_id, workspace_id)) is not None


def workspace_existence_is_hidden(
    session: Session,
    *,
    actor: ActorContext,
    workspace_id: UUID,
) -> bool:
    """Return whether an exact denial must collapse to the uniform 404."""

    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return True
    if workspace.visibility != "private":
        return False
    user_id = getattr(actor, "user_id", None)
    return (
        user_id is None
        or session.get(
            WorkspaceMembership,
            (user_id, workspace_id),
        )
        is None
    )
