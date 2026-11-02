from __future__ import annotations

from hashlib import blake2b
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace


def _projection_lock_key(workspace_id: UUID) -> int:
    return int.from_bytes(
        blake2b(
            f"dtxwiki.edge-projection:{workspace_id}".encode(), digest_size=8
        ).digest(),
        "big",
        signed=True,
    )


def try_edge_projection_reader(session: Session, workspace_id: UUID) -> None:
    """Co-access never waits behind an administrative projection rebuild.

    Concurrent conversation writers share this advisory lock. A maintenance
    writer owns it exclusively; a view then defers only its derived work.
    No Workspace row is touched by this guard.
    """
    if session.get_bind().dialect.name == "postgresql" and not session.scalar(
        select(
            func.pg_try_advisory_xact_lock_shared(_projection_lock_key(workspace_id))
        )
    ):
        raise BlockingIOError("edge maintenance in progress; retry derived co-access")


def lock_edge_projection_workspace(
    session: Session, workspace_id: UUID, *, nowait: bool = False
) -> None:
    """Serialize projection writers before they read or mutate edge rows.

    A no-op UPDATE takes the workspace row lock on PostgreSQL and the writer
    lock on SQLite. Pending edge changes must not autoflush before that lock;
    acquiring it never changes the public projection revision.
    """
    with session.no_autoflush:
        if nowait and session.get_bind().dialect.name == "postgresql":
            if (
                session.scalar(
                    select(Workspace.id)
                    .where(Workspace.id == workspace_id)
                    .with_for_update(nowait=True)
                )
                is None
            ):
                raise ValueError("workspace not found while locking edge projection")
            return
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                select(func.pg_advisory_xact_lock(_projection_lock_key(workspace_id)))
            )
        result = session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                edge_projection_version=Workspace.edge_projection_version,
                updated_at=Workspace.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ValueError("workspace not found while locking edge projection")


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
        .values(
            edge_projection_version=Workspace.edge_projection_version + 1,
            updated_at=Workspace.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("workspace not found while updating edge revision")
    session.flush()
    return edge_version(session, workspace_id)
