"""Ownership validation must precede even an ORM-triggered autoflush."""

from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, select

from backend.models.context import EdgeProjection
from backend.models.workspace import Workspace
from backend.services.entity_ref import EntityKind
from backend.services.graph_cache import lock_edge_projection_workspace
from backend.services.indexing.edge_projection import (
    EdgeProjectionScope,
    build_edge_row,
    reconcile_edge_projection_rows,
)


def test_expired_foreign_row_is_rejected_without_autoflush(session_factory):
    # Budget exception: a fake Session cannot reproduce an expired ORM
    # attribute refreshing itself and flushing unrelated pending work.
    with session_factory() as session:
        workspace_id, revision_before, updated_before = session.execute(
            select(
                Workspace.id,
                Workspace.edge_projection_version,
                Workspace.updated_at,
            ).where(Workspace.slug == "default")
        ).one()
        foreign_workspace = Workspace(
            slug="projection-foreign", name="projection-foreign", title="Foreign"
        )
        session.add(foreign_workspace)
        session.flush()
        foreign = EdgeProjection(
            **build_edge_row(
                workspace_id=foreign_workspace.id,
                source_kind=EntityKind.CONTEXT_PAGE,
                source_id=uuid4(),
                target_kind=EntityKind.CONTEXT_PAGE,
                target_id=uuid4(),
                edge_kind="co_access",
                provenance="usage",
                source_table="test",
            )
        )
        session.add(foreign)
        session.commit()
        session.expire(foreign)
        pending = EdgeProjection(
            **build_edge_row(
                workspace_id=workspace_id,
                source_kind=EntityKind.CONTEXT_PAGE,
                source_id=uuid4(),
                target_kind=EntityKind.CONTEXT_PAGE,
                target_id=uuid4(),
                edge_kind="co_access",
                provenance="usage",
                source_table="test",
            )
        )
        session.add(pending)
        flushes = []

        def before_flush(*_args):
            flushes.append(True)

        event.listen(session, "before_flush", before_flush)
        try:
            # A producer's workspace lock must precede autoflush, while keeping
            # both its public revision and ordinary workspace timestamp intact.
            lock_edge_projection_workspace(session, workspace_id)
            assert flushes == []
            assert inspect(pending).pending
            assert session.connection().execute(
                select(Workspace.edge_projection_version, Workspace.updated_at).where(
                    Workspace.id == workspace_id
                )
            ).one() == (revision_before, updated_before)
            with pytest.raises(ValueError, match="outside the producer"):
                reconcile_edge_projection_rows(
                    session,
                    scope=EdgeProjectionScope(workspace_id, frozenset({"co_access"})),
                    existing_rows=[foreign],
                    desired_rows=[],
                )
            assert flushes == []
            assert inspect(pending).pending
            assert not inspect(foreign).deleted
        finally:
            event.remove(session, "before_flush", before_flush)
            session.rollback()
