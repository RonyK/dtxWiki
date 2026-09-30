from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace


def get_workspace_by_slug(session: Session, slug: str) -> Workspace | None:
    return session.scalar(select(Workspace).where(Workspace.slug == slug))


def insert_workspace(session: Session, slug: str, title: str) -> Workspace:
    workspace = Workspace(slug=slug, title=title, name=title)
    session.add(workspace)
    session.flush()
    return workspace
