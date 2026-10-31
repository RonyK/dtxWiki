"""Gate 3: a production-shaped page shows its links, its folder and its sources.

Everything is written through the paths production uses -- page writes, the
tree service and the citation write-through seam -- and read with the request
the Local Graph screen sends. Before F08 the seed's 60 two-occurrence
backlinks filled every node slot and its folder and source never appeared.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextCitation, ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.schemas.tree import TreeFolderCreateRequest
from backend.services.graph_query_service import GraphQueryService
from backend.services.indexing.edge_registry import refresh_page_edge_projection
from backend.services.tree_service import TreeService
from backend.tests.integration.test_library_canonical_read_paths import (
    _seed_canonical_fixture,
)

pytestmark = pytest.mark.integration

BACKLINKERS = 60
SIBLINGS = 5


def _write(page_service, *, slug, title, body, parent_id=None):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=body,
        actor_kind="human",
        actor_id="mixed-scene-test",
        actor_provenance="browser_session",
        placement_parent_node_id=parent_id,
    )


def _viewer(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )


def test_production_shaped_page_shows_links_tree_and_citations(
    session_factory, page_service
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        folder = TreeService().create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id,
                section="context",
                title="Mixed Scene Folder",
                slug="mixed-scene-folder",
            ),
            actor_kind="human",
            actor_id="mixed-scene-test",
        )
        folder_id = folder.id

    seed = _write(
        page_service,
        slug="mixed-seed",
        title="Mixed Seed",
        body="# Mixed Seed\n\nThe page the others cite.",
        parent_id=folder_id,
    )
    for index in range(SIBLINGS):
        _write(
            page_service,
            slug=f"mixed-sibling-{index}",
            title=f"Mixed Sibling {index}",
            body=f"# Mixed Sibling {index}\n\nA neighbour in the folder.",
            parent_id=folder_id,
        )
    for index in range(BACKLINKERS):
        _write(
            page_service,
            slug=f"mixed-backlinker-{index}",
            title=f"Mixed Backlinker {index}",
            body=(
                f"# Mixed Backlinker {index}\n\n"
                "See [[mixed-seed]], and [[mixed-seed]] again."
            ),
        )

    with session_factory.begin() as session:
        seed_page = session.scalar(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.slug == "mixed-seed",
            )
        )
        canonical = _seed_canonical_fixture(session, workspace_id)
        session.add(
            ContextCitation(
                workspace_id=workspace_id,
                target_kind="context_page_version",
                target_id=seed.version_id,
                target_location={},
                library_item_version_id=canonical.ready_version.id,
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="manual_input",
                created_by_actor_type="human",
            )
        )
        session.flush()
        # The publish write-through seam for a page's citations.
        refresh_page_edge_projection(session, workspace_id, seed_page.id)
        seed_id = seed_page.id

    with session_factory() as session:
        payload = GraphQueryService().query(
            session,
            workspace_id,
            request=GraphQueryRequest(
                seeds=[GraphSeed(kind="context_page", id=seed_id)],
                preset="all",
                direction="both",
                time=GraphTimeQuery(mode="appearance"),
                budget=GraphBudget(nodes=50, edges=150),
            ),
            actor=_viewer(workspace_id),
            usage_signals_enabled=True,
        )

    kinds = {edge.edge_kind for edge in payload.edges}
    assert {"wikilink", "tree", "citation_source"} <= kinds, sorted(kinds)
    links = [edge for edge in payload.edges if edge.edge_kind == "wikilink"]
    assert links and all(edge.weight == 2 for edge in links)
    assert len(payload.nodes) == 50
