"""The SQL visibility predicate admits exactly what the hydrators admit.

Relation ranks are divided by counts taken in SQL (a page's visible links, a
folder's visible children). A count is only invariant to what the reader
cannot see if the SQL predicate and the hydrators agree on every row; where
the predicate admitted more, a hidden relation could move a visible score.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.library import (
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.graph_service import (
    HYDRATORS,
    GraphService,
    graph_neighbor_visibility_predicate,
)
from backend.tests.integration.test_library_canonical_read_paths import (
    _artifact,
    _attempt,
    _seed_canonical_fixture,
    _seed_graph_page,
)

pytestmark = pytest.mark.integration

ROLES = ("viewer", "editor", "owner")


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(workspace_id: UUID, role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
    )


def _context_page(session, workspace_id, *, published: bool, archived=False):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=f"parity-{uuid4()}",
        title="Parity Page",
        page_type="concept",
        lifecycle_status="archived"
        if archived
        else ("published" if published else "draft"),
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="# Parity Page",
        content_hash=f"parity-{page.id}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    return page


def _library_item(session, workspace_id, **overrides) -> LibraryItem:
    values = {
        "workspace_id": workspace_id,
        "connector_type": "upload",
        "format_hint": "pdf",
        "uri": f"upload://parity-{uuid4()}.pdf",
        "title": "Parity Source",
        "trust_level": "trusted",
        "lifecycle_status": "ready",
    }
    values.update(overrides)
    item = LibraryItem(**values)
    session.add(item)
    session.flush()
    return item


def _legacy_item(session, workspace_id, *, processing_status: str):
    """A pre-0005 item: no ready pointer, only the legacy current pointer."""

    item = _library_item(session, workspace_id)
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash=uuid4().hex * 2,
        raw_storage_ref="inline://legacy",
        processing_status=processing_status,
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    session.flush()
    return item, version


def _unselected_version_artifact(session, workspace_id):
    """A ready version whose conversion never selected an attempt."""

    item = _library_item(session, workspace_id)
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash=uuid4().hex * 2,
        raw_storage_ref="inline://no-selection",
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    item.current_ready_version_id = version.id
    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test-v1",
        output_schema_version="1.0",
        idempotency_key=f"parity:{version.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = _attempt(run=run, version=version, chain_index=0, converter_id="x")
    session.add(attempt)
    session.flush()
    artifact = _artifact(
        session, version=version, attempt=attempt, text="# X", digest_char="9"
    )
    session.add(artifact)
    session.flush()
    return artifact


def _targets(session, workspace_id) -> list[tuple[str, UUID]]:
    canonical = _seed_canonical_fixture(session, workspace_id)
    restricted = _library_item(session, workspace_id, trust_level="restricted")
    archived = _library_item(session, workspace_id, lifecycle_status="archived")
    hidden = _library_item(session, workspace_id, lifecycle_status="hidden")
    _legacy_ready_item, legacy_completed = _legacy_item(
        session, workspace_id, processing_status="completed"
    )
    _legacy_pending_item, legacy_pending = _legacy_item(
        session, workspace_id, processing_status="converting"
    )
    no_selection_artifact = _unselected_version_artifact(session, workspace_id)
    return [
        ("context_page", _context_page(session, workspace_id, published=True).id),
        ("context_page", _context_page(session, workspace_id, published=False).id),
        (
            "context_page",
            _context_page(session, workspace_id, published=True, archived=True).id,
        ),
        ("library_item", canonical.item.id),
        ("library_item", restricted.id),
        ("library_item", archived.id),
        ("library_item", hidden.id),
        ("library_item_version", canonical.ready_version.id),
        ("library_item_version", canonical.pending_version.id),
        ("library_item_version", legacy_completed.id),
        ("library_item_version", legacy_pending.id),
        ("library_artifact", canonical.selected_artifact.id),
        ("library_artifact", canonical.unselected_artifact.id),
        ("library_artifact", no_selection_artifact.id),
    ]


def test_sql_predicate_admits_exactly_what_hydrators_admit(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_graph_page(session, workspace_id)
        targets = _targets(session, workspace_id)
        for target_kind, target_id in targets:
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind=target_kind,
                    target_id=target_id,
                    edge_kind="citation_source",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                )
            )
        seed_id = seed.id

    service = GraphService()
    for role in ROLES:
        actor = _actor(workspace_id, role)
        with session_factory() as session:
            admitted_by_sql = set(
                session.execute(
                    select(EdgeProjection.target_kind, EdgeProjection.target_id).where(
                        EdgeProjection.workspace_id == workspace_id,
                        EdgeProjection.source_id == seed_id,
                        graph_neighbor_visibility_predicate(
                            EdgeProjection.target_kind,
                            EdgeProjection.target_id,
                            workspace_id=workspace_id,
                            actor=actor,
                        ),
                    )
                ).all()
            )
            context = service.hydration_context(session, workspace_id, actor)
            admitted_by_hydrators = {
                (kind, entity_id)
                for kind in {kind for kind, _ in targets}
                for entity_id in HYDRATORS[kind](
                    context, [target for k, target in targets if k == kind]
                )
            }

        assert admitted_by_sql == admitted_by_hydrators, role


def test_tree_sql_predicate_matches_target_and_ancestor_hydration(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_graph_page(session, workspace_id)
        draft = _context_page(session, workspace_id, published=False)
        published = _context_page(session, workspace_id, published=True)

        def tree_node(
            node_kind: str,
            *,
            parent_id: UUID | None = None,
            target_id: UUID | None = None,
            visible: bool = True,
        ) -> WorkspaceTreeNode:
            node = WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind=node_kind,
                parent_id=parent_id,
                sort_key=uuid4().hex,
                title=f"Tree {uuid4().hex}",
                slug=f"parity-tree-{uuid4().hex}",
                target_kind="context_page" if target_id else None,
                target_id=target_id,
                primary_placement=False,
                visibility_status="visible" if visible else "hidden",
            )
            session.add(node)
            session.flush()
            return node

        root = tree_node("folder")
        published_ref = tree_node("page_ref", parent_id=root.id, target_id=published.id)
        draft_ref = tree_node("page_ref", parent_id=root.id, target_id=draft.id)
        child_of_draft = tree_node("folder", parent_id=draft_ref.id)
        hidden = tree_node("folder", visible=False)
        child_of_hidden = tree_node("folder", parent_id=hidden.id)
        targets = (
            root.id,
            published_ref.id,
            draft_ref.id,
            child_of_draft.id,
            hidden.id,
            child_of_hidden.id,
        )
        for target_id in targets:
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="workspace_tree_node",
                    target_id=target_id,
                    edge_kind="tree",
                    provenance="derived",
                    weight=1.0,
                    source_ref={},
                )
            )
        seed_id = seed.id

    service = GraphService()
    for role in ROLES:
        actor = _actor(workspace_id, role)
        with session_factory() as session:
            admitted_by_sql = set(
                session.scalars(
                    select(EdgeProjection.target_id).where(
                        EdgeProjection.workspace_id == workspace_id,
                        EdgeProjection.source_id == seed_id,
                        graph_neighbor_visibility_predicate(
                            EdgeProjection.target_kind,
                            EdgeProjection.target_id,
                            workspace_id=workspace_id,
                            actor=actor,
                            allow_tree_fallback=True,
                        ),
                    )
                ).all()
            )
            context = service.hydration_context(session, workspace_id, actor)
            admitted_by_hydrator = set(
                HYDRATORS["workspace_tree_node"](context, list(targets))
            )

        assert admitted_by_sql == admitted_by_hydrator, role
