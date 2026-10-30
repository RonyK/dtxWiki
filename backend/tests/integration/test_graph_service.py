"""GraphService.neighbors + write-through refresh + perf budget (C2-T5/T6)."""

import os
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.entity_ref import EntityKind
from backend.services.graph_service import (
    GRAPH_TREE_HYDRATION_ANCESTOR_DEPTH_LIMIT,
    HYDRATORS,
    VISIBLE_PROJECTION_PAGE_LIMIT,
    GraphService,
)
from backend.services.indexing.edge_registry import (
    EDGE_EXTRACTORS,
    refresh_tree_edge_projection,
)
from backend.services.page_service import PageService
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_service import ensure_primary_tree_placement

pytestmark = pytest.mark.integration


def _actor(role="editor"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _publish(page_service: PageService, slug, title, body):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=body,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )


def _seed_context_page(
    session,
    workspace_id,
    *,
    slug: str,
    title: str,
    published: bool = True,
):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}\n\nBody.",
        content_hash=f"graph-{slug}",
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


def _edge(session, workspace_id, source, target, edge_kind, weight=1.0):
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=source.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind=edge_kind,
            provenance="curated",
            weight=weight,
            source_ref={},
        )
    )


def test_extracted_kinds_all_have_hydrators_or_are_version_lineage():
    """19.5.4-b: every kind an extractor can emit either hydrates (and is
    returnable) or is a deliberate label-only lineage kind."""

    emitted_kinds = {
        EntityKind.CONTEXT_PAGE.value,
        EntityKind.LIBRARY_ITEM.value,
        EntityKind.LIBRARY_ARTIFACT.value,
        EntityKind.LIBRARY_ITEM_VERSION.value,
        EntityKind.LIBRARY_SYNC_RUN.value,
        EntityKind.ASK_TURN.value,
        EntityKind.WORKSPACE_TREE_NODE.value,
        EntityKind.CONTEXT_PAGE_VERSION.value,
    }
    label_only = {
        # citation_source targets hydrate as their parent Library item so
        # the read_concept citations block can actually name sources (5.5).
        EntityKind.LIBRARY_SYNC_RUN.value,
        EntityKind.ASK_TURN.value,
        EntityKind.CONTEXT_PAGE_VERSION.value,
        EntityKind.AGENT_PAGE.value,
    }
    for kind in emitted_kinds - label_only:
        assert kind in HYDRATORS, f"{kind} emitted but not hydratable"
    assert set(EDGE_EXTRACTORS)  # registry non-empty sanity


def test_agent_page_neighbors_are_hydrated_and_owner_gated(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="agent-neighbor-seed",
            title="Agent Neighbor Seed",
        )
        agents = [
            AgentPage(
                workspace_id=workspace_id,
                category="persona",
                title="Visible Persona",
                slug="visible-persona",
                lifecycle_status="active",
                sensitivity="workspace",
            ),
            AgentPage(
                workspace_id=workspace_id,
                category="memory",
                title="Owner Memory",
                slug="owner-memory",
                lifecycle_status="active",
                sensitivity="private",
            ),
        ]
        session.add_all(agents)
        session.flush()
        for index, page in enumerate(agents, start=1):
            version = AgentPageVersion(
                workspace_id=workspace_id,
                agent_page_id=page.id,
                version_no=1,
                body_markdown=f"# Agent {index}",
                frontmatter={},
                actor_type="human",
                version_status="active",
            )
            session.add(version)
            session.flush()
            page.current_active_version_id = version.id
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="agent_page",
                    target_id=page.id,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                )
            )
        session.flush()

        viewer = GraphService().neighbors(
            session,
            workspace_id,
            kind="context_page",
            entity_id=seed.id,
            actor=_actor("viewer"),
            direction="out",
        )
        owner = GraphService().neighbors(
            session,
            workspace_id,
            kind="context_page",
            entity_id=seed.id,
            actor=_actor("owner"),
            direction="out",
        )

    assert {(item.kind, item.id) for item in viewer} == {("agent_page", agents[0].id)}
    assert {(item.kind, item.id) for item in owner} == {
        ("agent_page", page.id) for page in agents
    }


def test_co_access_reaches_legacy_direct_and_panel_reads(session_factory):
    """co_access is an ordinary edge kind: no gate of its own.

    It used to be dropped unconditionally for a k-anonymity threshold that a
    workspace of a handful of named teammates can never satisfy, which only
    guaranteed the relation stayed empty. What has to hold is access control,
    and hydration enforces that for every edge kind alike.
    """

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="legacy-co-access-seed",
            title="Legacy Co-access Seed",
        )
        target = _seed_context_page(
            session,
            workspace_id,
            slug="legacy-co-access-target",
            title="Legacy Co-access Target",
        )
        _edge(session, workspace_id, seed, target, "co_access")
        seed_id = seed.id
        target_id = target.id

    with session_factory() as session:
        graph = GraphService()
        direct = graph.neighbors(
            session,
            workspace_id,
            kind="context_page",
            entity_id=seed_id,
            actor=_actor("owner"),
            edge_kinds=("co_access",),
            direction="both",
        )
        panel = graph.context_page_neighbors_panel(
            session,
            workspace_id,
            page_id=seed_id,
            actor=_actor("owner"),
        )

    assert [edge.id for edge in direct] == [target_id]
    assert direct[0].edge_kind == "co_access"
    assert [item.id for item in panel.co_access] == [target_id]


def test_publish_refreshes_wikilink_neighbors_without_reindex(
    session_factory, page_service: PageService
):
    """Write-through freshness: publishing a page with new [[links]] updates
    neighbors immediately (reverting to rebuild-only fails this)."""

    target = _publish(page_service, "wt-target", "WT Target", "# WT Target\n\nBody.")
    source = _publish(
        page_service,
        "wt-source",
        "WT Source",
        "# WT Source\n\nSee [[wt-target]].",
    )
    graph = GraphService()
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        out = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=source.id,
            actor=_actor(),
            edge_kinds=("wikilink",),
            direction="out",
        )
        assert [n.title for n in out] == ["WT Target"]
        backlinks = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=target.id,
            actor=_actor(),
            edge_kinds=("wikilink",),
            direction="in",
        )
        assert [n.title for n in backlinks] == ["WT Source"]


def test_viewer_never_receives_invisible_targets(
    session_factory, page_service: PageService
):
    from backend.models.context import ContextPage, ContextPageVersion
    from backend.services.indexing.edge_registry import rebuild_edge_projection

    # draft-only page linked from a published page
    published = _publish(
        page_service,
        "vis-source",
        "Vis Source",
        "# Vis Source\n\nSee [[vis-draft]].",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        draft = ContextPage(
            workspace_id=workspace_id,
            slug="vis-draft",
            title="Vis Draft",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(draft)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=draft.id,
            version_no=1,
            body_md="# Vis Draft\n\nDraft.",
            content_hash="vis-draft-1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(version)
        session.flush()
        draft.current_version_id = version.id
        draft.latest_draft_version_id = version.id
        draft_id = draft.id
        # resolve the pending wikilink target and rebuild edges
        from backend.services.page_service import refresh_page_links

        source_page = session.get(ContextPage, published.id)
        head = session.get(ContextPageVersion, source_page.current_published_version_id)
        refresh_page_links(session, page=source_page, version=head)
        rebuild_edge_projection(session, workspace_id, usage_signals_enabled=True)

    graph = GraphService()
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_out = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=published.id,
            actor=_actor("viewer"),
            edge_kinds=("wikilink",),
            direction="out",
        )
        assert all(n.id != draft_id for n in viewer_out)
        editor_out = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=published.id,
            actor=_actor("editor"),
            edge_kinds=("wikilink",),
            direction="out",
        )
        assert any(n.id == draft_id for n in editor_out)


def test_context_page_neighbors_panel_sections_limits_and_visibility(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="panel-seed",
            title="Panel Seed",
        )
        hidden = _seed_context_page(
            session,
            workspace_id,
            slug="panel-hidden",
            title="Panel Hidden",
            published=False,
        )
        archived = _seed_context_page(
            session,
            workspace_id,
            slug="panel-archived",
            title="Panel Archived",
        )
        archived.lifecycle_status = "archived"
        for index in range(12):
            inbound = _seed_context_page(
                session,
                workspace_id,
                slug=f"panel-in-{index}",
                title=f"Panel In {index}",
            )
            outbound = _seed_context_page(
                session,
                workspace_id,
                slug=f"panel-out-{index}",
                title=f"Panel Out {index}",
            )
            _edge(session, workspace_id, inbound, seed, "wikilink", 20 - index)
            _edge(session, workspace_id, seed, outbound, "wikilink", 20 - index)
        for index in range(7):
            citation = _seed_context_page(
                session,
                workspace_id,
                slug=f"panel-citation-{index}",
                title=f"Panel Citation {index}",
            )
            _edge(
                session,
                workspace_id,
                seed,
                citation,
                "citation_page",
                20 - index,
            )
        _edge(session, workspace_id, seed, hidden, "wikilink", 100)
        _edge(session, workspace_id, seed, archived, "wikilink", 101)
        seed_id = seed.id
        hidden_id = hidden.id
        archived_id = archived.id

    with session_factory() as session:
        graph = GraphService()
        viewer = graph.context_page_neighbors_panel(
            session,
            workspace_id,
            page_id=seed_id,
            actor=_actor("viewer"),
        )
        editor = graph.context_page_neighbors_panel(
            session,
            workspace_id,
            page_id=seed_id,
            actor=_actor("editor"),
        )

        assert len(viewer.backlinks) == 10
        assert len(viewer.outbound) == 10
        assert len(viewer.citations) == 5
        assert viewer.truncated is True
        assert viewer.warnings == ["graph_projection_source_bounded"]
        assert {item.edge_kind for item in viewer.backlinks} == {"wikilink"}
        assert {item.direction for item in viewer.backlinks} == {"in"}
        assert {item.direction for item in viewer.outbound} == {"out"}
        assert {item.edge_kind for item in viewer.citations} == {"citation_page"}
        assert hidden_id not in {item.id for item in viewer.outbound}
        assert hidden_id in {item.id for item in editor.outbound}
        assert archived_id not in {item.id for item in viewer.outbound}
        assert archived_id not in {item.id for item in editor.outbound}

        with pytest.raises(ReadNotFoundError):
            graph.context_page_neighbors_panel(
                session,
                workspace_id,
                page_id=hidden_id,
                actor=_actor("viewer"),
            )
        with pytest.raises(ReadNotFoundError):
            graph.context_page_neighbors_panel(
                session,
                uuid4(),
                page_id=seed_id,
                actor=_actor("editor"),
            )


def test_context_pages_graph_context_batches_queries_across_many_seeds(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeds = []
        targets = []
        for index in range(20):
            seed = _seed_context_page(
                session,
                workspace_id,
                slug=f"batch-seed-{index}",
                title=f"Batch Seed {index}",
            )
            target = _seed_context_page(
                session,
                workspace_id,
                slug=f"batch-target-{index}",
                title=f"Batch Target {index}",
            )
            _edge(session, workspace_id, seed, target, "wikilink")
            seeds.append(seed.id)
            targets.append(target.id)

    with session_factory() as session:
        statements = 0

        def count_statement(*_args):
            nonlocal statements
            statements += 1

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            contexts = GraphService().context_pages_graph_context(
                session,
                workspace_id,
                page_ids=seeds,
                actor=_actor("viewer"),
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)

    assert statements <= 4
    assert set(contexts) == set(seeds)
    assert {
        page_id: [entry.id for entry in contexts[page_id]] for page_id in seeds
    } == {
        seed_id: [target_id] for seed_id, target_id in zip(seeds, targets, strict=True)
    }


def test_context_pages_graph_context_reserves_budget_for_citations(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="citation-budget-seed",
            title="Citation Budget Seed",
        )
        for index in range(70):
            target = _seed_context_page(
                session,
                workspace_id,
                slug=f"citation-budget-link-{index}",
                title=f"Citation Budget Link {index}",
            )
            _edge(session, workspace_id, seed, target, "wikilink")
        citation = _seed_context_page(
            session,
            workspace_id,
            slug="citation-budget-source",
            title="Citation Budget Source",
        )
        _edge(session, workspace_id, seed, citation, "citation_page")
        seed_id = seed.id
        citation_id = citation.id

    with session_factory() as session:
        contexts = GraphService().context_pages_graph_context(
            session,
            workspace_id,
            page_ids=[seed_id],
            actor=_actor("viewer"),
        )

    assert citation_id in {entry.id for entry in contexts[seed_id]}


def test_neighbors_panel_hydrates_library_artifact_citation_to_item_path(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="source-citation-seed",
            title="Source Citation Seed",
        )
        item = LibraryItem(
            workspace_id=workspace_id,
            connector_type="upload",
            uri="upload://runbook.pdf",
            title="Deployment Runbook",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_no=1,
            content_hash="source-citation-v1",
            raw_storage_ref="inline://runbook",
            processing_status="completed",
        )
        session.add(version)
        session.flush()
        item.current_version_id = version.id
        item.current_ready_version_id = version.id
        # session_factory (backend/tests/conftest.py) uses a bare
        # create_engine() with no PRAGMA foreign_keys=ON, so run_id/
        # attempt_id/blob_id need not reference other real rows.
        run = LibraryRun(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key=f"test:{version.id}",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id="test.provider",
            provider_version="1",
            provider_spi_version="1",
            config_hash="test",
            input_sha256="0" * 64,
            output_schema_version="1",
            status="completed",
            chain_index=0,
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()
        artifact = LibraryArtifact(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=workspace_id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="text",
            media_type="text/plain",
            content_sha256="a" * 64,
            byte_size=len("Runbook body"),
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            index_text="Runbook body",
        )
        session.add(artifact)
        session.flush()
        # A-1: GraphService._artifact_belongs_to_selected_attempt requires
        # the version's selected attempt to be this artifact's attempt --
        # the retired nullable-attempt shape no longer makes an artifact
        # belong "by default".
        version.selected_conversion_attempt_id = attempt.id
        session.add(version)
        session.flush()
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed.id,
                target_kind="library_artifact",
                target_id=artifact.id,
                edge_kind="citation_source",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        seed_id = seed.id
        artifact_id = artifact.id
        item_id = item.id

    with session_factory() as session:
        panel = GraphService().context_page_neighbors_panel(
            session,
            workspace_id,
            page_id=seed_id,
            actor=_actor("viewer"),
        )

    assert len(panel.citations) == 1
    citation = panel.citations[0]
    assert citation.kind == "library_artifact"
    assert citation.id == artifact_id
    assert citation.title == "Deployment Runbook"
    assert citation.path == f"/library/{item_id}.md"


def test_tree_neighbor_does_not_name_hidden_parent(session_factory, page_service):
    child = _publish(
        page_service,
        "visible-child",
        "Visible Child",
        "# Visible Child\n\nPublished.",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        parent = ContextPage(
            workspace_id=workspace_id,
            slug="secret-parent",
            title="Secret Parent",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(parent)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=parent.id,
            version_no=1,
            body_md="# Secret Parent\n\nDraft.",
            content_hash="secret-parent-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(version)
        session.flush()
        parent.current_version_id = version.id
        parent.latest_draft_version_id = version.id
        parent_node = ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=parent.id,
            title=parent.title,
            slug=parent.slug,
            section="context",
            parent_id=None,
            default_parent_slug="context",
            actor_kind="human",
            actor_id="tester",
            can_have_children=True,
        )
        child_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == child.id,
            )
        )
        child_node.parent_id = parent_node.id
        refresh_tree_edge_projection(session, workspace_id)

    graph = GraphService()
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_parents = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=child.id,
            actor=_actor("viewer"),
            edge_kinds=("tree",),
            direction="in",
        )
        editor_parents = graph.neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=child.id,
            actor=_actor("editor"),
            edge_kinds=("tree",),
            direction="in",
        )

    assert all(parent.title != "Secret Parent" for parent in viewer_parents)
    assert [parent.title for parent in editor_parents] == ["Secret Parent"]


def test_too_deep_tree_neighbor_is_filtered_before_hydration(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="bounded-tree-ancestor-seed",
            title="Bounded Tree Ancestor Seed",
        )
        parent_id = None
        leaf_id = None
        for depth in range(GRAPH_TREE_HYDRATION_ANCESTOR_DEPTH_LIMIT + 2):
            leaf_id = uuid4()
            session.add(
                WorkspaceTreeNode(
                    id=leaf_id,
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=parent_id,
                    sort_key=f"{depth:04d}",
                    title=f"Bounded ancestor {depth}",
                    slug=f"bounded-ancestor-{depth}",
                    target_kind=None,
                    target_id=None,
                    visibility_status="visible",
                )
            )
            session.flush()
            parent_id = leaf_id
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed.id,
                target_kind="workspace_tree_node",
                target_id=leaf_id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        seed_id = seed.id

    statements: list[str] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            panel = GraphService().context_page_neighbors_panel(
                session,
                workspace_id,
                page_id=seed_id,
                actor=_actor("viewer"),
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert panel.outbound == []
    assert panel.truncated is False
    assert panel.warnings == []
    tree_queries = [
        statement.lower()
        for statement in statements
        if statement.lstrip().lower().startswith("select workspace_tree_node.")
        and "from workspace_tree_node" in statement.lower()
    ]
    assert tree_queries == []
    assert all(
        "order by workspace_tree_node.section" not in statement
        for statement in tree_queries
    )


def test_neighbors_sql_prefilter_refills_after_1025_hidden_high_weight_rows(
    session_factory, page_service
):
    visible = _publish(
        page_service,
        "visible-after-hidden",
        "Visible After Hidden",
        "# Visible\n\nPublished.",
    )
    hub = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for index in range(1_025):
            page = ContextPage(
                id=uuid4(),
                workspace_id=workspace_id,
                slug=f"hidden-neighbor-{index}",
                title=f"Hidden Neighbor {index}",
                page_type="concept",
                lifecycle_status="draft",
            )
            session.add(page)
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md="# Hidden\n\nDraft.",
                content_hash=f"hidden-neighbor-{index}",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="tester",
                version_status="draft",
            )
            session.add(version)
            session.flush()
            page.current_version_id = version.id
            page.latest_draft_version_id = version.id
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=hub,
                    target_kind="context_page",
                    target_id=page.id,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=float(2_000 - index),
                    source_ref={},
                )
            )
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=hub,
                target_kind="context_page",
                target_id=visible.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )

    statements: list[str] = []
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            neighbors = GraphService().neighbors(
                session,
                workspace_id,
                kind="context_page",
                entity_id=hub,
                actor=_actor("viewer"),
                edge_kinds=("wikilink",),
                direction="out",
                limit=1,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert [neighbor.title for neighbor in neighbors] == ["Visible After Hidden"]
    assert neighbors.scan_capped is False
    edge_queries = [
        statement for statement in statements if "FROM edge_projection" in statement
    ]
    assert len(edge_queries) == 1


def test_neighbors_ignores_nonexistent_tree_rows_before_scan_cap(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="bounded-tree-fallback-seed",
            title="Bounded Tree Fallback Seed",
        )
        visible = _seed_context_page(
            session,
            workspace_id,
            slug="bounded-tree-fallback-visible",
            title="Bounded Tree Fallback Visible",
        )
        session.add_all(
            [
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="workspace_tree_node",
                    target_id=uuid4(),
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=float(2_000 - index),
                    source_ref={},
                )
                for index in range(1_025)
            ]
        )
        _edge(session, workspace_id, seed, visible, "wikilink", 1.0)
        seed_id = seed.id
        visible_id = visible.id

    statements: list[str] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            panel = GraphService().context_page_neighbors_panel(
                session,
                workspace_id,
                page_id=seed_id,
                actor=_actor("viewer"),
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert [entry.id for entry in panel.outbound] == [visible_id]
    assert panel.truncated is False
    assert panel.warnings == []
    edge_queries = [
        statement for statement in statements if "FROM edge_projection" in statement
    ]
    # One outbound read plus at most one empty query for each other legacy
    # panel section. Stale tree ids do not spend the scan cap.
    assert len(edge_queries) <= VISIBLE_PROJECTION_PAGE_LIMIT + 4


def test_batched_graph_context_prefilters_1025_hidden_edges(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="batch-hidden-seed",
            title="Batch Hidden Seed",
        )
        visible = _seed_context_page(
            session,
            workspace_id,
            slug="batch-visible-after-hidden",
            title="Batch Visible After Hidden",
        )
        for index in range(1_025):
            hidden = ContextPage(
                id=uuid4(),
                workspace_id=workspace_id,
                slug=f"batch-hidden-target-{index}",
                title=f"Batch Hidden Target {index}",
                page_type="concept",
                lifecycle_status="draft",
            )
            session.add(hidden)
            _edge(
                session,
                workspace_id,
                seed,
                hidden,
                "wikilink",
                float(1_000 - index),
            )
        _edge(session, workspace_id, seed, visible, "wikilink", 1.0)
        seed_id = seed.id
        visible_id = visible.id

    with session_factory() as session:
        contexts = GraphService().context_pages_graph_context(
            session,
            workspace_id,
            page_ids=[seed_id],
            actor=_actor("viewer"),
            limit=1,
        )

    assert [entry.id for entry in contexts[seed_id]] == [visible_id]


def test_ppr_hidden_bridge_cannot_change_viewer_topology(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_context_page(
            session,
            workspace_id,
            slug="visible-ppr-seed",
            title="Visible PPR Seed",
        )
        direct = _seed_context_page(
            session,
            workspace_id,
            slug="visible-ppr-direct",
            title="Visible PPR Direct",
        )
        hidden_bridge = _seed_context_page(
            session,
            workspace_id,
            slug="hidden-ppr-bridge",
            title="Hidden PPR Bridge",
            published=False,
        )
        bridged = _seed_context_page(
            session,
            workspace_id,
            slug="visible-ppr-bridged",
            title="Visible PPR Bridged",
        )
        _edge(session, workspace_id, seed, direct, "wikilink", 1.0)
        _edge(session, workspace_id, seed, hidden_bridge, "wikilink", 100.0)
        _edge(session, workspace_id, hidden_bridge, bridged, "wikilink", 100.0)
        seed_id = seed.id
        direct_id = direct.id
        hidden_id = hidden_bridge.id
        bridged_id = bridged.id

    with session_factory() as session:
        graph = GraphService()
        viewer = graph.ppr(
            session,
            workspace_id,
            seeds=[("context_page", seed_id, 1.0)],
            actor=_actor("viewer"),
            top_k=10,
        )
        editor = graph.ppr(
            session,
            workspace_id,
            seeds=[("context_page", seed_id, 1.0)],
            actor=_actor("editor"),
            top_k=10,
        )

    viewer_ids = {result.id for result in viewer}
    editor_ids = {result.id for result in editor}
    assert direct_id in viewer_ids
    assert hidden_id not in viewer_ids
    assert bridged_id not in viewer_ids
    assert {hidden_id, bridged_id} <= editor_ids


@contextmanager
def _captured_statements(session):
    """Every statement the engine runs inside the block, with its parameters."""

    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _bound_limit(statement: str, parameters: tuple) -> int | None:
    """The LIMIT bound into a captured SQLite statement, or None without one."""

    if statement.rstrip().endswith("LIMIT ? OFFSET ?"):
        return parameters[-2]
    return None


def _seed_hub_graph(session_factory):
    """A 10K-edge hub with over 512 visible neighbors in each direction.

    Hidden drafts outrank the published endpoints; dangling endpoints are also
    present. SQL must exclude both before applying its candidate-window limit.
    Published rows are inserted last so only an ordered read yields the right
    window. Returns visible endpoints by direction, heaviest first.
    """

    hub = uuid4()
    visible: dict[str, list[tuple]] = {"out": [], "in": []}
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)

        def edge(source, target, weight):
            return {
                "workspace_id": workspace_id,
                "source_kind": "context_page",
                "source_id": source,
                "target_kind": "context_page",
                "target_id": target,
                "edge_kind": "wikilink",
                "provenance": "curated",
                "weight": weight,
                "source_ref": {},
            }

        rows = [edge(uuid4(), uuid4(), float(index % 7 + 1)) for index in range(8_010)]
        rows += [edge(hub, uuid4(), float(index % 7 + 1)) for index in range(390)]
        rows += [edge(uuid4(), hub, float(index % 7 + 1)) for index in range(390)]
        pages, versions = [], []
        weight = 10_000.0
        for direction in ("out", "in"):
            for published, count in ((False, 5), (True, 600)):
                for index in range(count):
                    state = "published" if published else "draft"
                    slug = f"hub-{direction}-{state}-{index}"
                    page = ContextPage(
                        id=uuid4(),
                        workspace_id=workspace_id,
                        slug=slug,
                        title=f"Hub {direction} {state} {index}",
                        page_type="concept",
                        lifecycle_status=state,
                    )
                    version = ContextPageVersion(
                        id=uuid4(),
                        workspace_id=workspace_id,
                        page_id=page.id,
                        version_no=1,
                        body_md=f"# {page.title}",
                        content_hash=f"graph-{slug}",
                        actor_type="human",
                        created_by_actor_kind="human",
                        created_by_actor_id="tester",
                        version_status="published_snapshot" if published else "draft",
                    )
                    pages.append(page)
                    versions.append(version)
                    rows.append(
                        edge(hub, page.id, weight)
                        if direction == "out"
                        else edge(page.id, hub, weight)
                    )
                    if published:
                        visible[direction].append((page.id, weight))
                    weight -= 1.0
        session.add_all(pages)
        session.flush()
        session.add_all(versions)
        session.flush()
        for page, version in zip(pages, versions, strict=True):
            page.current_version_id = version.id
            if page.lifecycle_status == "published":
                page.current_published_version_id = version.id
            else:
                page.latest_draft_version_id = version.id
        assert len(rows) == 10_000
        session.execute(insert(EdgeProjection), rows)
    return hub, visible


# neighbors() asks for one extra visible result to prove result_capped, so its
# raw candidate window is min(512, max(128, (limit + 1) * 8)).
@pytest.mark.parametrize(
    ("direction", "limit", "budgets"),
    [
        pytest.param("out", 50, {"out": 408}, id="out"),
        pytest.param("in", 50, {"in": 408}, id="in"),
        pytest.param("both", 50, {"out": 408, "in": 408}, id="both"),
        pytest.param("out", 1, {"out": 128}, id="floor"),
        pytest.param("out", 100, {"out": 512}, id="cap"),
    ],
)
def test_neighbors_work_is_bounded_on_a_10k_edge_graph(
    session_factory, monkeypatch, direction, limit, budgets
):
    """19.5.7 budget, asserted as counted work rather than elapsed time.

    Each direction reads one exact candidate window after SQL visibility
    filtering, then hydrates that window in one batch. The extra result proves
    the output cap while the 128 floor and 512 cap bound database work. Hidden
    and dangling endpoints never spend hydration budget. These are counted on
    the actual call so missing limits or per-edge lookups fail deterministically.
    """

    hub, visible = _seed_hub_graph(session_factory)

    hydrated: list[tuple] = []
    hydrate_context_pages = HYDRATORS[EntityKind.CONTEXT_PAGE.value]

    def spy_hydrate(ctx, ids):
        hydrated.append(tuple(ids))
        return hydrate_context_pages(ctx, ids)

    monkeypatch.setitem(HYDRATORS, EntityKind.CONTEXT_PAGE.value, spy_hydrate)

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        with _captured_statements(session) as statements:
            neighbors = GraphService().neighbors(
                session,
                workspace_id,
                kind="context_page",
                entity_id=hub,
                actor=_actor("viewer"),
                edge_kinds=("wikilink",),
                direction=direction,
                limit=limit,
            )

    edge_reads: dict[str, int | None] = {}
    for statement, parameters in statements:
        if "FROM edge_projection" in statement:
            side = "out" if "edge_projection.source_id = ?" in statement else "in"
            assert side not in edge_reads, f"{side} edges were read twice"
            edge_reads[side] = _bound_limit(statement, parameters)
    # One read per direction, each bound by the hard cap rather than the degree.
    assert edge_reads == budgets, "edge reads must be one per direction, LIMIT-bound"
    # Each visible degree beats the cap. Match exact ids as well as counts so
    # hidden/dangling rows cannot consume the SQL window before hydration.
    expected_hydration = [
        tuple(page_id for page_id, _weight in visible[side][:budget])
        for side, budget in budgets.items()
    ]
    assert hydrated == expected_hydration, "hydration must be one visible capped batch"
    # Edge read + hydration read per direction, nothing else.
    assert len(statements) == 2 * len(budgets), "unexpected extra statements"

    assert neighbors.result_capped is True
    assert neighbors.scan_capped is False
    # The viewer gets the published pages only, heaviest first.
    expected = sorted(
        (
            (page_id, side, weight)
            for side in budgets
            for page_id, weight in visible[side]
        ),
        key=lambda item: -item[2],
    )[:limit]
    assert [(n.id, n.direction) for n in neighbors] == [
        (page_id, side) for page_id, side, _weight in expected
    ]


@pytest.fixture
def benchmark_lane(request):
    """Gate for wall-clock budgets: opt-in, and never beside xdist workers.

    A latency measured on a shared runner proves nothing about the code, so the
    functional lanes assert counted work and the budgets run only when asked
    for, one process at a time: ``make test-backend-benchmarks``.
    """

    if os.environ.get("DTXWIKI_TEST_BENCHMARKS") != "1":
        pytest.skip("wall-clock budget; run `make test-backend-benchmarks`")
    if hasattr(request.config, "workerinput"):
        pytest.fail(
            "DTXWIKI_TEST_BENCHMARKS=1 under pytest-xdist: a budget timed beside "
            "other workers is not a measurement; run the lane without -n, as "
            "`make test-backend-benchmarks` does",
            pytrace=False,
        )


def test_neighbors_latency_benchmark_10k_edges(session_factory, benchmark_lane):
    """19.5.7 budget: median neighbors() call <= 10ms on a 10k-edge fixture."""

    graph = GraphService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hub = uuid4()
        rows = []
        for i in range(10_000):
            rows.append(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=hub if i % 100 == 0 else uuid4(),
                    target_kind="context_page",
                    target_id=uuid4(),
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=float(i % 7 + 1),
                    source_ref={},
                )
            )
        session.add_all(rows)

    timings = []
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        for _ in range(5):
            start = time.perf_counter()
            graph.neighbors(
                session,
                workspace_id,
                kind="context_page",
                entity_id=hub,
                actor=_actor(),
                edge_kinds=("wikilink",),
                direction="out",
            )
            timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    median = timings[len(timings) // 2]
    assert median <= 10.0, f"median {median:.2f}ms > 10ms budget"


def test_neighbors_pushes_hub_limit_into_edge_query(session_factory):
    hub = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add_all(
            [
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=hub,
                    target_kind="context_page",
                    target_id=uuid4(),
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=float(index),
                    source_ref={},
                )
                for index in range(1_000)
            ]
        )

    statements: list[str] = []
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            GraphService().neighbors(
                session,
                workspace_id,
                kind="context_page",
                entity_id=hub,
                actor=_actor(),
                edge_kinds=("wikilink",),
                direction="out",
                limit=5,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    edge_queries = [
        statement
        for statement in statements
        if "FROM edge_projection" in statement
        and "edge_projection.source_id" in statement
    ]
    assert edge_queries
    assert all("LIMIT" in statement.upper() for statement in edge_queries)
