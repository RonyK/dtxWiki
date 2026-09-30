from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace


def edge_version(session: Session, workspace_id: UUID) -> int:
    return int(
        session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        or 0
    )


def bump_edge_version(session: Session, workspace_id: UUID) -> int:
    result = session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(edge_projection_version=Workspace.edge_projection_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("workspace not found while updating edge revision")
    session.flush()
    return edge_version(session, workspace_id)
