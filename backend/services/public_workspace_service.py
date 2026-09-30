from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace


class PublicWorkspaceNotFoundError(RuntimeError):
    error_code = "not_found"


@dataclass(frozen=True)
class PublicWorkspaceProfile:
    workspace_id: UUID
    slug: str
    title: str
    description: str | None


def read_public_workspace_profile(
    session: Session,
    *,
    workspace_slug: str,
) -> PublicWorkspaceProfile:
    workspace = session.scalar(
        select(Workspace).where(
            Workspace.slug == workspace_slug,
            Workspace.visibility == "public",
        )
    )
    if workspace is None:
        raise PublicWorkspaceNotFoundError("workspace not found")
    return PublicWorkspaceProfile(
        workspace_id=workspace.id,
        slug=workspace.slug,
        title=workspace.title,
        description=workspace.description,
    )
