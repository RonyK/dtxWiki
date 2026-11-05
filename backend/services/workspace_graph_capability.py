"""Whether the Workspace Knowledge Map is available to one actor right now.

The product switch is the owner's Settings choice on the control-plane
workspace row. ``graph_service_available`` is an operator break-glass that
can only turn the map off; it never turns it on for a workspace whose owner
left it off. Local (per-document) Graphs do not consult either switch.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace
from backend.policy.rbac import role_allows
from backend.schemas.workspace_graph import (
    WorkspaceGraphCapability,
    WorkspaceGraphMapCapability,
)
from backend.services.read_errors import ReadNotFoundError


def usage_relation_signals_enabled(
    control_session: Session,
    workspace_id: UUID,
) -> bool:
    """The owner's usage-signal switch. A missing workspace reads as off."""

    return bool(
        control_session.scalar(
            select(Workspace.usage_relation_signals_enabled).where(
                Workspace.id == workspace_id
            )
        )
    )


def workspace_graph_capability(
    control_session: Session,
    workspace_id: UUID,
    *,
    service_available: bool,
    actor_role: str,
) -> WorkspaceGraphCapability:
    row = control_session.execute(
        select(
            Workspace.workspace_graph_enabled,
            Workspace.usage_relation_signals_enabled,
        ).where(Workspace.id == workspace_id)
    ).one_or_none()
    if row is None:
        raise ReadNotFoundError("workspace not found")
    workspace_enabled, usage_signals_enabled = row
    # Members cannot use this experiment. For administrators the operator
    # switch takes precedence: an unavailable service cannot be fixed in Labs.
    if not role_allows(actor_role, "owner"):
        reason = "permission_denied"
    elif not service_available:
        reason = "service_unavailable"
    elif not workspace_enabled:
        reason = "disabled_by_workspace"
    else:
        reason = "enabled"
    return WorkspaceGraphCapability(
        workspace_map=WorkspaceGraphMapCapability(
            effective_enabled=reason == "enabled",
            workspace_enabled=workspace_enabled,
            service_available=service_available,
            reason=reason,
        ),
        usage_relation_signals_enabled=usage_signals_enabled,
        can_manage=role_allows(actor_role, "owner"),
    )
