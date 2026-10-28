"""Create an owned workspace with the default seed every creator shares.

Browser creates and the workspace a new account starts with both use this core,
so neither receives a different shape. It imports no session or auth module and
can therefore be called from account creation without an import cycle.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.repositories.workspace_repo import insert_workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.tree_service import ensure_context_inbox_node


def create_owned_workspace(
    session: Session,
    *,
    owner_user_id: UUID,
    slug: str,
    title: str,
) -> Workspace:
    # Insert before seeding: seeding is get-or-create by slug, so a competitor
    # that committed after the caller's availability check would otherwise be
    # adopted and re-owned instead of failing the UNIQUE slug.
    insert_workspace(session, slug=slug, title=title)
    workspace = seed_default_workspace(session, slug=slug, title=title)
    ensure_context_inbox_node(session, workspace.id)
    workspace.created_by_actor_id = owner_user_id
    workspace.owner_actor_id = owner_user_id
    grant_workspace_membership(
        session,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        role="owner",
    )
    session.flush()
    return workspace
