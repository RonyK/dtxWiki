"""Screens an Inbox item opens, in one place.

Workspace screens use the workspace-scoped routes the frontend defines in
``frontend/src/shared/lib/workspaceRoutes.ts``, so a link never acts on
whichever workspace the browser has selected.
"""

from uuid import UUID

ACCESS_KEYS_PATH = "/account/integrations"


def workspace_path(workspace_id: UUID, child: str = "") -> str:
    base = f"/workspaces/{workspace_id}"
    return f"{base}/{child}" if child else base


def review_path(workspace_id: UUID, review_id: UUID) -> str:
    return workspace_path(workspace_id, f"review/{review_id}")
