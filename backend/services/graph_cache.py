from __future__ import annotations

from hashlib import blake2b
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace
from backend.services.db_write_intent import acquire_write_intent


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


def try_lock_edge_projection_workspace(session: Session, workspace_id: UUID) -> None:
    """Acquire maintenance guards without waiting behind page-first writers.

    Callers must also acquire their page locks with NOWAIT. A busy guard or
    page aborts the caller's entire transaction so the worker can retry it;
    retaining partial repairs or waiting while holding these guards can
    deadlock ordinary page writers, sharing, or projection rebuilds.
    """
    acquire_write_intent(session)
    with session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql" and not session.scalar(
            select(func.pg_try_advisory_xact_lock(_projection_lock_key(workspace_id)))
        ):
            raise BlockingIOError("edge projection busy; retry maintenance transaction")
        workspace = session.scalar(
            select(Workspace.id)
            .where(Workspace.id == workspace_id)
            # NO KEY UPDATE protects policy fields while permitting FK checks.
            .with_for_update(key_share=True, nowait=True)
        )
    if workspace is None:
        raise ValueError("workspace not found while locking edge projection")


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
        if session.get_bind().dialect.name == "postgresql":
            # Policy/publication callers may already own this Workspace row.
            # Taking the maintenance barrier first would invert their order.
            # Incremental readers retain shared-barrier + row NOWAIT behavior.
            session.execute(
                select(func.pg_advisory_xact_lock(_projection_lock_key(workspace_id)))
            )


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
