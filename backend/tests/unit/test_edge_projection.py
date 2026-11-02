"""Ownership validation must precede even an ORM-triggered autoflush."""

from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, select

from backend.models.context import EdgeProjection
from backend.models.workspace import Workspace
from backend.services.entity_ref import EntityKind
from backend.services.indexing.edge_projection import (
    EdgeProjectionScope,
    build_edge_row,
    reconcile_edge_projection_rows,
)


def test_expired_foreign_row_is_rejected_without_autoflush(session_factory):
    # One DB case is necessary: a fake Session cannot reproduce an expired
    # ORM attribute refreshing itself and flushing unrelated pending work.
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
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
