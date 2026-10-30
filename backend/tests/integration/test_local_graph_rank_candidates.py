"""The Local Graph collects every relation kind and ranks it for its reader.

Raw EdgeProjection weights are in different units per kind, and the old
collector sorted by them: a page's backlinks (occurrence sums) pushed its tree
placements and citations out of the candidate set before the budget ever saw
them. ranked_projection_edges reads each kind on its own and ranks each row
from counts taken over what the reader can see.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.graph import GraphObservation
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.services import graph_service as graph_service_module
from backend.services.graph_query_service import GraphQueryService
from backend.services.graph_rank import (
    RankDenominators,
    rank_score,
    tree_weight,
    wikilink_weight,
)
from backend.services.graph_service import HYDRATORS, GraphService
from backend.tests.integration.test_graph_visibility_predicate_parity import (
    _legacy_item,
    _library_item,
    _unselected_version_artifact,
)
from backend.tests.integration.test_library_canonical_read_paths import (
    _seed_canonical_fixture,
)

pytestmark = pytest.mark.integration

REACHABLE = ("citation_page", "citation_source", "co_access", "tree", "wikilink")


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(role: str = "viewer") -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _page(session, workspace_id, *, title="Page", published=True, archived=False):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"rank-{uuid4()}",
        title=title,
        page_type="concept",
        lifecycle_status=(
            "archived" if archived else "published" if published else "draft"
        ),
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        id=uuid4(),
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}",
        content_hash=f"rank-{page.id}",
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


def _folder(session, workspace_id, *, parent_id=None, visible=True):
    folder = WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        parent_id=parent_id,
        sort_key=uuid4().hex,
        title="Folder",
        slug=f"rank-folder-{uuid4()}",
        target_kind=None,
        target_id=None,
        visibility_status="visible" if visible else "hidden",
    )
    session.add(folder)
    session.flush()
    return folder


def _edge(session, workspace_id, source, target, kind, weight=1.0, occurred_at=None):
    """``source``/``target`` are ``(kind, id)`` pairs or ORM rows."""

    def ref(value):
        if isinstance(value, tuple):
            return value
        if isinstance(value, WorkspaceTreeNode):
            return ("workspace_tree_node", value.id)
        return ("context_page", value.id)

    source_ref, target_ref = ref(source), ref(target)
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind=source_ref[0],
            source_id=source_ref[1],
            target_kind=target_ref[0],
            target_id=target_ref[1],
            edge_kind=kind,
            provenance="usage" if kind == "co_access" else "curated",
            weight=weight,
            occurred_at=occurred_at,
            source_ref={},
        )
    )


def _co_access(session, workspace_id, left, right, weight=3.0):
    """Stored once, in canonical UUID order, like the trail tick writes it."""

    first, second = sorted((left, right), key=lambda page: str(page.id))
    _edge(session, workspace_id, first, second, "co_access", weight)


def _ranked(session, workspace_id, seeds, *, role="viewer", edge_budget=150):
    service = GraphService()
    actor = _actor(role)
    return service.ranked_projection_edges(
        session,
        workspace_id,
        seeds=tuple(("context_page", seed) for seed in seeds),
        actor=actor,
        edge_kinds=REACHABLE,
        neighbor_kinds=tuple(sorted(HYDRATORS)),
        occurred_from=None,
        occurred_to=None,
        direction="both",
        edge_budget=edge_budget,
        now=datetime.now(UTC),
        hydration_context=service.hydration_context(session, workspace_id, actor),
    )


def _query(
    session,
    workspace_id,
    seeds,
    *,
    role="viewer",
    nodes=50,
    edges=150,
    direction="both",
    edge_kinds=(),
    usage=True,
    time: GraphTimeQuery | None = None,
):
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(
            seeds=[GraphSeed(kind="context_page", id=seed) for seed in seeds],
            edge_kinds=list(edge_kinds),
            direction=direction,
            budget=GraphBudget(nodes=nodes, edges=edges),
            time=time if time is not None else GraphTimeQuery(),
        ),
        actor=_actor(role),
        usage_signals_enabled=usage,
    )


def _scene(payload) -> list[tuple[str, str, float]]:
    return sorted(
        (edge.id, edge.edge_kind, round(edge.rank_score, 12)) for edge in payload.edges
    )


def _kinds(edges) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        counts[edge.edge_kind] = counts.get(edge.edge_kind, 0) + 1
    return counts


def test_every_reachable_kind_is_collected_beside_a_flood_of_links(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        for _ in range(300):
            _edge(
                session,
                workspace_id,
                _page(session, workspace_id),
                seed,
                "wikilink",
                2.0,
            )
        for _ in range(3):
            _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        for _ in range(2):
            _edge(
                session,
                workspace_id,
                seed,
                _page(session, workspace_id),
                "citation_page",
            )
        for _ in range(3):
            _co_access(session, workspace_id, seed, _page(session, workspace_id))
        seed_id = seed.id

    with session_factory() as session:
        ranked = _ranked(session, workspace_id, [seed_id])
        raw = GraphService().visible_projection_edges(
            session,
            workspace_id,
            kind="context_page",
            entity_id=seed_id,
            actor=_actor(),
            edge_kinds=REACHABLE,
            limit=201,
        )

    # Each (seed, kind, role) queue is hydrated up to one past the edge budget:
    # the 300 backlinks fill theirs, and no other kind waits behind them.
    assert _kinds(ranked) == {
        "wikilink": 151,
        "tree": 3,
        "citation_page": 2,
        "co_access": 3,
    }
    assert ranked.scan_capped is False
    assert all(0 < edge.rank_score < 1 for edge in ranked)
    # The raw-weight collector the Local Graph used before kept 201 links.
    assert "tree" not in _kinds(raw)


def test_backlinks_rank_by_the_backlinkers_visible_out_degree(session_factory):
    """One link from a focused page outranks two from a page linking 51."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        focused = _page(session, workspace_id, title="Focused")
        spread = _page(session, workspace_id, title="Spread")
        _edge(session, workspace_id, focused, seed, "wikilink", 1.0)
        _edge(session, workspace_id, spread, seed, "wikilink", 2.0)
        for _ in range(50):
            _edge(
                session, workspace_id, spread, _page(session, workspace_id), "wikilink"
            )
        seed_id, focused_id, spread_id = seed.id, focused.id, spread.id

    with session_factory() as session:
        ranked = {
            edge.source_id: edge for edge in _ranked(session, workspace_id, [seed_id])
        }

    now = datetime.now(UTC)
    assert ranked[focused_id].rank_score == pytest.approx(
        rank_score("wikilink", wikilink_weight(1.0, 1), None, now)
    )
    assert ranked[spread_id].rank_score == pytest.approx(
        rank_score("wikilink", wikilink_weight(2.0, 51), None, now)
    )
    assert ranked[focused_id].rank_score > ranked[spread_id].rank_score
    assert ranked[focused_id].weight < ranked[spread_id].weight  # raw stays raw


def _hidden_fixture(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        backlinker = _page(session, workspace_id, title="Backlinker")
        _edge(session, workspace_id, backlinker, seed, "wikilink", 1.0)
        for _ in range(3):
            _edge(
                session,
                workspace_id,
                backlinker,
                _page(session, workspace_id),
                "wikilink",
            )
        folder = _folder(session, workspace_id)
        _edge(session, workspace_id, folder, seed, "tree")
        for _ in range(2):
            _edge(session, workspace_id, folder, _page(session, workspace_id), "tree")
        for _ in range(2):
            _edge(
                session,
                workspace_id,
                seed,
                _page(session, workspace_id),
                "wikilink",
                2.0,
            )
        # Its citation total divides a visible edge, so hidden citations of
        # the seed would show as a changed rank.
        _edge(
            session,
            workspace_id,
            seed,
            _page(session, workspace_id),
            "citation_page",
            2.0,
        )
        return workspace_id, seed.id, backlinker.id, folder.id


def _add_draft_target_of_backlinker(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    draft = _page(session, workspace_id, published=False)
    _edge(session, workspace_id, ("context_page", backlinker_id), draft, "wikilink")


def _add_archived_target_of_backlinker(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    # The backlinker's out-degree divides its backlink to the seed.
    archived = _page(session, workspace_id, archived=True)
    _edge(session, workspace_id, ("context_page", backlinker_id), archived, "wikilink")


def _add_hidden_library_citations_of_seed(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    _pending_item, pending = _legacy_item(
        session, workspace_id, processing_status="converting"
    )
    unselected = _unselected_version_artifact(session, workspace_id)
    for target in (
        ("library_item_version", pending.id),
        ("library_artifact", unselected.id),
    ):
        _edge(
            session,
            workspace_id,
            ("context_page", seed_id),
            target,
            "citation_source",
            4.0,
        )


def _add_restricted_citation_of_seed(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    restricted = _library_item(session, workspace_id, trust_level="restricted")
    _edge(
        session,
        workspace_id,
        ("context_page", seed_id),
        ("library_item", restricted.id),
        "citation_source",
        4.0,
    )


def _add_draft_page_in_seed_folder(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    draft = _page(session, workspace_id, published=False)
    _edge(session, workspace_id, ("workspace_tree_node", folder_id), draft, "tree")


def _add_hidden_child_folder(session, workspace_id, seed_id, backlinker_id, folder_id):
    hidden = _folder(session, workspace_id, parent_id=folder_id, visible=False)
    _edge(session, workspace_id, ("workspace_tree_node", folder_id), hidden, "tree")


def _add_many_draft_backlinkers(
    session, workspace_id, seed_id, backlinker_id, folder_id
):
    for _ in range(1_025):
        draft = _page(session, workspace_id, published=False)
        _edge(session, workspace_id, draft, ("context_page", seed_id), "wikilink", 9.0)


@pytest.mark.parametrize(
    ("addition", "owner_sees_it"),
    [
        pytest.param(
            _add_draft_target_of_backlinker, True, id="draft-target-of-backlinker"
        ),
        pytest.param(
            _add_archived_target_of_backlinker,
            False,
            id="archived-target-of-backlinker",
        ),
        pytest.param(
            _add_hidden_library_citations_of_seed,
            False,
            id="pending-version-and-unselected-artifact-citations",
        ),
        pytest.param(
            _add_restricted_citation_of_seed, None, id="restricted-library-citation"
        ),
        pytest.param(
            _add_draft_page_in_seed_folder, True, id="draft-page-in-seed-folder"
        ),
        pytest.param(_add_hidden_child_folder, False, id="hidden-child-folder"),
        pytest.param(_add_many_draft_backlinkers, True, id="many-draft-backlinkers"),
    ],
)
def test_hidden_relations_do_not_move_scores_selection_or_order(
    session_factory, addition, owner_sees_it
):
    workspace_id, seed_id, backlinker_id, folder_id = _hidden_fixture(session_factory)
    with session_factory() as session:
        viewer_before = _scene(
            _query(session, workspace_id, [seed_id], nodes=6, edges=5)
        )
        owner_before = _scene(_query(session, workspace_id, [seed_id], role="owner"))
    with session_factory.begin() as session:
        addition(session, workspace_id, seed_id, backlinker_id, folder_id)
    with session_factory() as session:
        viewer_after = _scene(
            _query(session, workspace_id, [seed_id], nodes=6, edges=5)
        )
        owner_after = _scene(_query(session, workspace_id, [seed_id], role="owner"))

    assert viewer_after == viewer_before
    # The owner can read drafts, so the same addition is a live count for them.
    if owner_sees_it is not None:
        assert (owner_after != owner_before) is owner_sees_it


def test_hidden_tree_rows_cannot_change_local_scene_or_completeness(
    session_factory, monkeypatch
):
    fixed = datetime(2026, 9, 28, tzinfo=UTC)
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: fixed)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Visible Seed")
        visible_folder = _folder(session, workspace_id)
        _edge(session, workspace_id, visible_folder, seed, "tree")
        session.flush()
        before = _query(
            session, workspace_id, [seed.id], edge_kinds=("tree",)
        ).model_dump_json()
        for index in range(1_025):
            hidden = WorkspaceTreeNode(
                id=uuid4(),
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                sort_key=f"hidden-{index}",
                title=f"PRIVACY SENTINEL {index}",
                slug=f"hidden-local-folder-{index}-{uuid4().hex}",
                visibility_status="hidden",
            )
            session.add(hidden)
            _edge(session, workspace_id, hidden, seed, "tree", 9.0)
        session.flush()
        after = _query(
            session, workspace_id, [seed.id], edge_kinds=("tree",)
        ).model_dump_json()

    assert after == before
    assert "PRIVACY SENTINEL" not in after


@pytest.mark.parametrize("hidden_by", ("draft_target", "hidden_ancestor"))
def test_tree_target_or_ancestor_cannot_spend_local_raw_cap(
    session_factory, monkeypatch, hidden_by
):
    fixed = datetime(2026, 9, 28, tzinfo=UTC)
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: fixed)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Visible Seed")
        visible_folder = _folder(session, workspace_id)
        _edge(session, workspace_id, visible_folder, seed, "tree")
        session.flush()
        before = _query(
            session, workspace_id, [seed.id], edge_kinds=("tree",)
        ).model_dump_json()

        hidden_parent = (
            _folder(session, workspace_id, visible=False)
            if hidden_by == "hidden_ancestor"
            else None
        )
        draft_target = (
            _page(session, workspace_id, published=False)
            if hidden_by == "draft_target"
            else None
        )
        for index in range(1_025):
            hidden = WorkspaceTreeNode(
                id=uuid4(),
                workspace_id=workspace_id,
                section="context",
                node_kind="page_ref" if draft_target else "folder",
                parent_id=hidden_parent.id if hidden_parent else None,
                sort_key=f"hidden-{index}",
                title=f"PRIVACY SENTINEL {index}",
                slug=f"hidden-tree-ref-{index}-{uuid4().hex}",
                target_kind="context_page" if draft_target else None,
                target_id=draft_target.id if draft_target else None,
                primary_placement=False,
                visibility_status="visible",
            )
            session.add(hidden)
            _edge(session, workspace_id, hidden, seed, "tree", 9.0)
        session.flush()
        after = _query(
            session, workspace_id, [seed.id], edge_kinds=("tree",)
        ).model_dump_json()

    assert after == before
    assert "PRIVACY SENTINEL" not in after


def test_hidden_tree_observations_cannot_spend_observation_raw_cap(
    session_factory, monkeypatch
):
    fixed = datetime(2026, 9, 28, tzinfo=UTC)
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: fixed)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Visible Seed")
        visible_folder = _folder(session, workspace_id)
        session.add(
            GraphObservation(
                workspace_id=workspace_id,
                operation="edge_created",
                workspace_sequence=1,
                source_kind="workspace_tree_node",
                source_id=visible_folder.id,
                target_kind="context_page",
                target_id=seed.id,
                edge_kind="tree",
                occurred_at=fixed - timedelta(days=2),
                idempotency_key=f"{1:064x}",
            )
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1
        session.flush()
        before = _query(session, workspace_id, [seed.id], edge_kinds=("tree",))

        draft = _page(session, workspace_id, published=False)
        hidden_ref = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="page_ref",
            sort_key="hidden-observation",
            title="PRIVACY SENTINEL",
            slug=f"hidden-observation-{uuid4().hex}",
            target_kind="context_page",
            target_id=draft.id,
            visibility_status="visible",
        )
        session.add(hidden_ref)
        for index in range(1_025):
            sequence = index + 2
            session.add(
                GraphObservation(
                    workspace_id=workspace_id,
                    operation="edge_created",
                    workspace_sequence=sequence,
                    source_kind="workspace_tree_node",
                    source_id=hidden_ref.id,
                    target_kind="context_page",
                    target_id=seed.id,
                    edge_kind="tree",
                    occurred_at=fixed - timedelta(days=1, seconds=index),
                    idempotency_key=f"{sequence:064x}",
                )
            )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1_026
        session.flush()
        after = _query(session, workspace_id, [seed.id], edge_kinds=("tree",))

    assert after.nodes == before.nodes
    assert after.edges == before.edges
    assert after.completeness == before.completeness
    assert after.truncated == before.truncated
    assert after.warnings == before.warnings
    assert "PRIVACY SENTINEL" not in after.model_dump_json()


def test_denominators_equal_counting_what_hydrators_admit(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source = _page(session, workspace_id, title="Source")
        for _ in range(3):
            _edge(
                session, workspace_id, source, _page(session, workspace_id), "wikilink"
            )
        _edge(
            session,
            workspace_id,
            source,
            _page(session, workspace_id, published=False),
            "wikilink",
        )
        _edge(
            session,
            workspace_id,
            source,
            _page(session, workspace_id, archived=True),
            "wikilink",
        )
        _edge(
            session,
            workspace_id,
            source,
            _page(session, workspace_id),
            "citation_page",
            2.0,
        )
        _edge(
            session,
            workspace_id,
            source,
            _page(session, workspace_id),
            "citation_page",
            3.0,
        )
        _edge(
            session,
            workspace_id,
            source,
            _page(session, workspace_id, published=False),
            "citation_page",
            7.0,
        )
        # Every Library shape: one citation the reader can see, three not.
        canonical = _seed_canonical_fixture(session, workspace_id)
        _edge(
            session,
            workspace_id,
            source,
            ("library_item", canonical.item.id),
            "citation_source",
        )
        _pending_item, pending = _legacy_item(
            session, workspace_id, processing_status="converting"
        )
        restricted = _library_item(session, workspace_id, trust_level="restricted")
        for target in (
            ("library_item_version", pending.id),
            (
                "library_artifact",
                _unselected_version_artifact(session, workspace_id).id,
            ),
            ("library_item", restricted.id),
        ):
            _edge(session, workspace_id, source, target, "citation_source", 7.0)
        folder = _folder(session, workspace_id)
        for _ in range(2):
            _edge(session, workspace_id, folder, _page(session, workspace_id), "tree")
        _edge(
            session,
            workspace_id,
            folder,
            _page(session, workspace_id, published=False),
            "tree",
        )
        _edge(
            session,
            workspace_id,
            folder,
            _folder(session, workspace_id, parent_id=folder.id),
            "tree",
        )
        _edge(
            session,
            workspace_id,
            folder,
            _folder(session, workspace_id, parent_id=folder.id, visible=False),
            "tree",
        )
        source_id, folder_id = source.id, folder.id

    service = GraphService()
    actor = _actor()
    with session_factory() as session:
        counts = service.relation_rank_denominators(
            session,
            workspace_id,
            actor=actor,
            sources=(
                ("context_page", source_id),
                ("workspace_tree_node", folder_id),
            ),
        )
        # The same counts, by hydrating every out-row the way the scene does.
        context = service.hydration_context(session, workspace_id, actor)
        rows = session.execute(
            select(
                EdgeProjection.source_id,
                EdgeProjection.target_kind,
                EdgeProjection.target_id,
                EdgeProjection.edge_kind,
                EdgeProjection.weight,
            ).where(EdgeProjection.source_id.in_((source_id, folder_id)))
        ).all()
        admitted = {
            (kind, entity_id)
            for kind in {row.target_kind for row in rows}
            for entity_id in HYDRATORS[kind](
                context, [row.target_id for row in rows if row.target_kind == kind]
            )
        }

    hydrated_links = sum(
        1
        for row in rows
        if row.edge_kind == "wikilink" and (row.target_kind, row.target_id) in admitted
    )
    hydrated_citations = sum(
        row.weight
        for row in rows
        if row.edge_kind in ("citation_page", "citation_source")
        and (row.target_kind, row.target_id) in admitted
    )
    hydrated_children = sum(
        1
        for row in rows
        if row.edge_kind == "tree" and (row.target_kind, row.target_id) in admitted
    )
    assert counts[("context_page", source_id)] == RankDenominators(
        wikilink_out_degree=hydrated_links, citation_total=hydrated_citations
    )
    assert counts[("workspace_tree_node", folder_id)] == RankDenominators(
        tree_children=hydrated_children
    )
    assert (hydrated_links, hydrated_citations, hydrated_children) == (3, 6.0, 3)


def test_windowed_hydration_selects_what_full_hydration_selects(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        other_seed = _page(session, workspace_id, title="Other Seed")
        _edge(session, workspace_id, seed, other_seed, "wikilink", 4.0)
        # A seed heads the hub's backlink queue; it is hydrated anyway and
        # must not take a window slot from the queue's other rows.
        _edge(session, workspace_id, other_seed, seed, "wikilink", 9.0)
        # Rank order is not raw-weight order: a backlinker's out-degree
        # divides its weight, and equal ranks go newest first.
        sinks = [_page(session, workspace_id) for _ in range(4)]
        dated = datetime(2026, 9, 1, tzinfo=UTC)
        for index in range(120):
            backlinker = _page(session, workspace_id)
            _edge(
                session,
                workspace_id,
                backlinker,
                seed,
                "wikilink",
                1.0 + index % 5,
                occurred_at=dated - timedelta(days=index % 3),
            )
            for sink in sinks[: index % 4]:
                _edge(session, workspace_id, backlinker, sink, "wikilink")
            if index < 20:
                _edge(session, workspace_id, seed, backlinker, "wikilink", 1.0)
        # A placement the hydrator rejects (hidden ancestor) and links to
        # tree-node ids that do not exist.
        hidden_parent = _folder(session, workspace_id, visible=False)
        _edge(
            session,
            workspace_id,
            _folder(session, workspace_id, parent_id=hidden_parent.id),
            seed,
            "tree",
        )
        _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        for _ in range(5):
            _edge(
                session,
                workspace_id,
                seed,
                ("workspace_tree_node", uuid4()),
                "wikilink",
                9.0,
            )
        seed_ids = [seed.id, other_seed.id]

    hydrated_ids: list[UUID] = []
    real_context_hydrator = HYDRATORS["context_page"]

    def counting_hydrator(context, ids):
        hydrated_ids.extend(ids)
        return real_context_hydrator(context, ids)

    # direction="out" leaves only the seed's links, whose window the five
    # unhydratable links fill first: only a second round can reach the rest.
    cases = [
        (nodes, edges, seeds, direction)
        for nodes, edges in ((2, 1), (10, 10), (50, 150), (250, 750))
        for seeds in ([seed_ids[0]], seed_ids)
        for direction in ("both", "out")
    ]
    for nodes, edges, seeds, direction in cases:
        monkeypatch.setitem(HYDRATORS, "context_page", counting_hydrator)
        hydrated_ids.clear()
        with session_factory() as session:
            windowed = _query(
                session,
                workspace_id,
                seeds,
                nodes=nodes,
                edges=edges,
                direction=direction,
            )
        windowed_hydrated = len(hydrated_ids)
        monkeypatch.setattr(
            graph_service_module, "_hydration_window", lambda _budget: 10**9
        )
        with session_factory() as session:
            full = _query(
                session,
                workspace_id,
                seeds,
                nodes=nodes,
                edges=edges,
                direction=direction,
            )
        monkeypatch.undo()

        case = (nodes, edges, len(seeds), direction)
        assert _scene(windowed) == _scene(full), case
        assert [node.id for node in windowed.nodes] == [
            node.id for node in full.nodes
        ], case
        assert windowed.completeness == full.completeness, case
        assert windowed.warnings == full.warnings, case
        if edges <= 10 and direction == "both":
            # 120 backlinks, but only the queue's window was hydrated.
            assert windowed_hydrated < 60, case
        if edges == 10 and direction == "out" and len(seeds) == 1:
            # The first window held 5 unhydratable links and 6 visible ones;
            # the node budget admits 9 neighbours.
            # Induced relations between selected neighbours can fill the last
            # edge slot even when only nine node-costing spokes are admitted.
            assert len(full.edges) == 10, case


@pytest.mark.parametrize(
    ("backlinks", "completeness", "warnings"),
    [
        (300, "capped", ["graph_budget_applied"]),
        (1_024, "capped", ["graph_budget_applied"]),
        (
            1_025,
            "bounded_sample",
            ["graph_projection_source_bounded", "graph_budget_applied"],
        ),
    ],
)
def test_only_a_partition_past_its_row_budget_makes_a_bounded_sample(
    session_factory, backlinks, completeness, warnings
):
    """A hub is truncated by the budget, not sampled, until one kind overflows."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Hub")
        for _ in range(backlinks):
            _edge(session, workspace_id, _page(session, workspace_id), seed, "wikilink")
        seed_id = seed.id

    with session_factory() as session:
        payload = _query(session, workspace_id, [seed_id])

    assert payload.completeness == completeness
    assert payload.warnings == warnings


@pytest.mark.parametrize("direction", ["in", "out"])
def test_read_together_partners_come_from_both_stored_orientations(
    session_factory, direction
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        partners = [_page(session, workspace_id) for _ in range(6)]
        for partner in partners:
            _co_access(session, workspace_id, seed, partner)
        seed_id = seed.id
        partner_ids = {partner.id for partner in partners}

    with session_factory() as session:
        payload = _query(
            session,
            workspace_id,
            [seed_id],
            direction=direction,
            edge_kinds=["co_access"],
        )

    neighbours = {node.entity_id for node in payload.nodes if node.entity_id != seed_id}
    assert neighbours == partner_ids


def test_switching_usage_signals_off_keeps_co_access_out_of_the_sql(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        _co_access(session, workspace_id, seed, _page(session, workspace_id))
        _edge(session, workspace_id, seed, _page(session, workspace_id), "wikilink")
        seed_id = seed.id

    captured: list[tuple] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, _statement, parameters, context, _many):
            if context.execution_options.get("graph_rank_phase") == "ranked_candidates":
                captured.append(tuple(parameters))

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = _query(session, workspace_id, [seed_id], usage=False)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert captured
    assert all("co_access" not in parameters for parameters in captured)
    assert _kinds(payload.edges) == {"wikilink": 1}
    assert "usage_relation_signals_disabled" in payload.warnings


def test_observed_relations_are_ranked_like_current_ones(session_factory):
    occurred_at = datetime.now(UTC) - timedelta(days=1)
    observation_id = uuid4()
    time = GraphTimeQuery(from_=occurred_at, to=occurred_at)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        observed_target = _page(session, workspace_id, title="Observed")
        for _ in range(2):
            _edge(session, workspace_id, seed, _page(session, workspace_id), "wikilink")
        # Current authored evidence needs a live projection for admission.
        # Its undated projection defers to the ledger in this dated query.
        _edge(session, workspace_id, seed, observed_target, "wikilink")
        session.add(
            GraphObservation(
                id=observation_id,
                workspace_id=workspace_id,
                operation="edge_created",
                workspace_sequence=1,
                source_kind="context_page",
                source_id=seed.id,
                target_kind="context_page",
                target_id=observed_target.id,
                edge_kind="wikilink",
                occurred_at=occurred_at,
                idempotency_key=f"{1:064x}",
            )
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1
        seed_id, observed_id = seed.id, observed_target.id

    with session_factory() as session:
        payload = _query(session, workspace_id, [seed_id], time=time)

    observed = [edge for edge in payload.edges if edge.provenance == "observed"]
    assert len(observed) == 1
    target = next(node for node in payload.nodes if node.id == observed[0].target)
    assert target.entity_id == observed_id
    # The seed's visible out-degree (3 current links) divides it, as for any
    # current link of the seed. Rank denominators ignore the date filter.
    assert observed[0].rank_score == pytest.approx(
        rank_score(
            "wikilink", wikilink_weight(observed[0].weight, 3), None, occurred_at
        )
    )

    # Rebuilding away that projection must not let immutable evidence revive
    # its relation or target in the current scene.
    with session_factory.begin() as session:
        deleted = session.execute(
            delete(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == "context_page",
                EdgeProjection.source_id == seed_id,
                EdgeProjection.target_kind == "context_page",
                EdgeProjection.target_id == observed_id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
        assert deleted.rowcount == 1
    with session_factory() as session:
        remaining = session.get(GraphObservation, observation_id)
        assert remaining is not None
        assert remaining.operation == "edge_created"
        assert remaining.source_id == seed_id
        assert remaining.target_id == observed_id
        assert remaining.occurred_at == occurred_at
        rejected = _query(session, workspace_id, [seed_id], time=time)

    assert rejected.edges == []
    assert {node.entity_id for node in rejected.nodes} == {seed_id}


def test_observed_duplicate_takes_the_current_relation_rank(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        target = _page(session, workspace_id, title="Target")
        _edge(session, workspace_id, seed, target, "wikilink", 3.0)
        seed_id, target_id = seed.id, target.id

    service = GraphQueryService()
    with session_factory() as session:
        payload = _query(session, workspace_id, [seed_id])
        projected = [candidate for candidate in _candidates_from(payload, priority=0)]
        # The observation recorded a different weight; the current
        # relation's rank still wins, and no denominator is fetched for it.
        observed = [
            replace(
                _replace_provenance(candidate, "observed", priority=1, rank=0.0),
                edge=candidate.edge.model_copy(
                    update={"provenance": "observed", "rank_score": 0.0, "weight": 40.0}
                ),
            )
            for candidate in projected
        ]
        fetched = []
        monkeypatch.setattr(
            service.graph_service,
            "relation_rank_denominators",
            lambda *args, **kwargs: fetched.append(kwargs) or {},
        )
        ranked = service._ranked_observations(
            session,
            workspace_id,
            actor=_actor(),
            observed=observed,
            projected=projected,
            now=datetime.now(UTC),
        )

    assert [candidate.edge.rank_score for candidate in ranked] == [
        candidate.edge.rank_score for candidate in projected
    ]
    assert fetched == []
    assert target_id in {node.entity_id for node in payload.nodes}


def _candidates_from(payload, *, priority):
    from backend.services.graph_query_service import _EdgeCandidate

    nodes = {node.id: node for node in payload.nodes}
    return [
        _EdgeCandidate(
            edge=edge,
            nodes=(nodes[edge.source], nodes[edge.target]),
            priority=priority,
            semantic_identity=(
                edge.source,
                edge.target,
                edge.edge_kind,
                edge.action_kind,
            ),
            anchor=edge.source,
            role="out",
        )
        for edge in payload.edges
    ]


def _replace_provenance(candidate, provenance, *, priority, rank):
    from dataclasses import replace

    return replace(
        candidate,
        edge=candidate.edge.model_copy(
            update={"provenance": provenance, "rank_score": rank}
        ),
        priority=priority,
    )


def test_edge_ids_and_ranks_survive_a_projection_rebuild(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        for index in range(12):
            _edge(
                session,
                workspace_id,
                _page(session, workspace_id),
                seed,
                "wikilink",
                1.0 + index % 3,
            )
        _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        seed_id = seed.id

    with session_factory() as session:
        before = _scene(_query(session, workspace_id, [seed_id], nodes=6, edges=5))
    with session_factory.begin() as session:
        rows = session.scalars(
            select(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        ).all()
        copies = [
            {
                column.key: getattr(row, column.key)
                for column in EdgeProjection.__table__.columns
                if column.key not in {"id", "created_at"}
            }
            for row in rows
        ]
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        session.flush()
        for values in reversed(copies):
            session.add(EdgeProjection(**values))
    with session_factory() as session:
        after = _scene(_query(session, workspace_id, [seed_id], nodes=6, edges=5))

    assert after == before


def test_tree_rank_is_the_placement_share_of_its_folder(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        folder = _folder(session, workspace_id)
        _edge(session, workspace_id, folder, seed, "tree")
        for _ in range(9):
            _edge(session, workspace_id, folder, _page(session, workspace_id), "tree")
        seed_id = seed.id

    with session_factory() as session:
        (edge,) = [
            edge
            for edge in _ranked(session, workspace_id, [seed_id])
            if edge.edge_kind == "tree"
        ]

    assert edge.rank_score == pytest.approx(
        rank_score("tree", tree_weight(9), None, datetime.now(UTC))
    )
    assert tree_weight(9) == pytest.approx(1 / math.log2(11))


def _denominators(session_factory, workspace_id, sources, *, role="viewer"):
    service = GraphService()
    with session_factory() as session:
        return service.relation_rank_denominators(
            session, workspace_id, actor=_actor(role), sources=tuple(sources)
        )


def test_denominators_saturate_at_the_row_cap_and_ignore_hidden_rows(
    session_factory, monkeypatch
):
    monkeypatch.setattr(graph_service_module, "RANK_DENOMINATOR_ROW_CAP", 8)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        linker = _page(session, workspace_id, title="Index")
        for _ in range(12):
            _edge(
                session, workspace_id, linker, _page(session, workspace_id), "wikilink"
            )
        citer = _page(session, workspace_id, title="Citer")
        for _ in range(12):
            _edge(
                session,
                workspace_id,
                citer,
                _page(session, workspace_id),
                "citation_page",
                2.0,
            )
        # Three rows fit under the cap, but they carry 15 citation instances.
        heavy = _page(session, workspace_id, title="Few heavy citations")
        for _ in range(3):
            _edge(
                session,
                workspace_id,
                heavy,
                _page(session, workspace_id),
                "citation_page",
                5.0,
            )
        few = _page(session, workspace_id, title="Few links")
        for _ in range(5):
            _edge(session, workspace_id, few, _page(session, workspace_id), "wikilink")
        inbox = _folder(session, workspace_id)
        for _ in range(12):
            _edge(session, workspace_id, inbox, _page(session, workspace_id), "tree")
        sparse = _folder(session, workspace_id)
        for _ in range(3):
            _edge(session, workspace_id, sparse, _page(session, workspace_id), "tree")
        ids = {
            "linker": linker.id,
            "citer": citer.id,
            "few": few.id,
            "heavy": heavy.id,
            "inbox": inbox.id,
            "sparse": sparse.id,
        }
    sources = [
        ("context_page", ids["linker"]),
        ("context_page", ids["citer"]),
        ("context_page", ids["few"]),
        ("context_page", ids["heavy"]),
        ("workspace_tree_node", ids["inbox"]),
        ("workspace_tree_node", ids["sparse"]),
    ]

    before = _denominators(session_factory, workspace_id, sources)

    assert before[("context_page", ids["linker"])].wikilink_out_degree == 8
    assert before[("context_page", ids["citer"])].citation_total == 8.0
    assert before[("context_page", ids["few"])].wikilink_out_degree == 5
    assert before[("context_page", ids["heavy"])].citation_total == 8.0
    assert before[("workspace_tree_node", ids["inbox"])].tree_children == 8
    assert before[("workspace_tree_node", ids["sparse"])].tree_children == 3

    # Rows the viewer cannot see push the small sources past the cap, onto
    # the capped statement; their counts stay what the viewer can see.
    with session_factory.begin() as session:
        for _ in range(20):
            _edge(
                session,
                workspace_id,
                ("context_page", ids["few"]),
                _page(session, workspace_id, published=False),
                "wikilink",
            )
            _edge(
                session,
                workspace_id,
                ("workspace_tree_node", ids["sparse"]),
                _page(session, workspace_id, published=False),
                "tree",
            )
    after = _denominators(session_factory, workspace_id, sources)

    assert after == before
    # The owner sees the drafts: the same rows are live counts for them.
    owner = _denominators(session_factory, workspace_id, sources, role="owner")
    assert owner[("context_page", ids["few"])].wikilink_out_degree == 8
    assert owner[("workspace_tree_node", ids["sparse"])].tree_children == 8


def test_windowed_hydration_counts_only_the_hydrated_prefix(
    session_factory, monkeypatch
):
    """Rows past an unhydrated one do not fill a queue's window.

    The out-queue's two strongest links point at tree nodes the hydrator
    rejects, and its later rows D0 and D1 are hydrated by the backlink queue.
    Counting them would skip the second round, so the strongest visible
    outbound link N would never be hydrated.
    """

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        for _ in range(2):
            _edge(
                session,
                workspace_id,
                seed,
                ("workspace_tree_node", uuid4()),
                "wikilink",
                9.0,
            )
        strongest = _page(session, workspace_id, title="N")
        _edge(session, workspace_id, seed, strongest, "wikilink", 4.0)
        for _ in range(2):
            partner = _page(session, workspace_id)
            _edge(session, workspace_id, seed, partner, "wikilink", 3.0)
            _edge(session, workspace_id, partner, seed, "wikilink", 1.0)
        seed_id, strongest_id = seed.id, strongest.id

    with session_factory() as session:
        windowed = _query(session, workspace_id, [seed_id], nodes=50, edges=1)
    monkeypatch.setattr(
        graph_service_module, "_hydration_window", lambda _budget: 10**9
    )
    with session_factory() as session:
        full = _query(session, workspace_id, [seed_id], nodes=50, edges=1)

    assert _scene(windowed) == _scene(full)
    assert strongest_id in {node.entity_id for node in windowed.nodes}


def test_ranked_collection_refuses_a_hydration_context_of_another_reader(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed_id = _page(session, workspace_id).id
    service = GraphService()
    with session_factory() as session:
        other = service.hydration_context(session, workspace_id, _actor("owner"))
        with pytest.raises(ValueError, match="graph read scope"):
            service.ranked_projection_edges(
                session,
                workspace_id,
                seeds=(("context_page", seed_id),),
                actor=_actor("viewer"),
                edge_kinds=REACHABLE,
                neighbor_kinds=tuple(sorted(HYDRATORS)),
                occurred_from=None,
                occurred_to=None,
                direction="both",
                edge_budget=150,
                now=datetime.now(UTC),
                hydration_context=other,
            )


def _published_pages(session, workspace_id, count):
    """Many visible pages in two flushes."""

    pages = [
        ContextPage(
            id=uuid4(),
            workspace_id=workspace_id,
            slug=f"rank-bulk-{uuid4()}",
            title="Bulk",
            page_type="concept",
            lifecycle_status="published",
        )
        for _ in range(count)
    ]
    session.add_all(pages)
    session.flush()
    versions = [
        ContextPageVersion(
            workspace_id=workspace_id,
            id=uuid4(),
            page_id=page.id,
            version_no=1,
            body_md="# Bulk",
            content_hash=f"rank-bulk-{page.id}",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        for page in pages
    ]
    session.add_all(versions)
    session.flush()
    for page, version in zip(pages, versions, strict=True):
        page.current_version_id = version.id
        page.current_published_version_id = version.id
    return pages


def _rebuild_projection(session_factory, workspace_id):
    """Recreate every row with new ids, in reverse order."""

    with session_factory.begin() as session:
        rows = session.scalars(
            select(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        ).all()
        copies = [
            {
                column.key: getattr(row, column.key)
                for column in EdgeProjection.__table__.columns
                if column.key not in {"id", "created_at"}
            }
            for row in rows
        ]
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        session.flush()
        for values in reversed(copies):
            session.add(EdgeProjection(id=uuid4(), **values))


def test_a_full_partition_of_one_kind_leaves_every_other_kind(session_factory):
    """The window is partitioned by kind: 1,100 heavier links fill theirs,
    and the tree placements and citations beside them still arrive."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        for backlinker in _published_pages(session, workspace_id, 1_100):
            _edge(session, workspace_id, backlinker, seed, "wikilink", 2.0)
        for _ in range(3):
            _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        for _ in range(2):
            _edge(
                session,
                workspace_id,
                seed,
                _page(session, workspace_id),
                "citation_page",
            )
        seed_id = seed.id

    with session_factory() as session:
        ranked = _ranked(session, workspace_id, [seed_id], edge_budget=1_100)
    counts = _kinds(ranked)

    assert ranked.scan_capped is True
    assert counts["tree"] == 3
    assert counts["citation_page"] == 2
    assert counts["wikilink"] == 1_024

    # Which 1,024 links the full partition keeps survives new row ids.
    before = sorted((edge.source_id, edge.rank_score) for edge in ranked)
    _rebuild_projection(session_factory, workspace_id)
    with session_factory() as session:
        after = _ranked(session, workspace_id, [seed_id], edge_budget=1_100)
    assert sorted((edge.source_id, edge.rank_score) for edge in after) == before


def test_each_seed_reads_its_share_of_the_partition_budget(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first = _page(session, workspace_id, title="First")
        second = _page(session, workspace_id, title="Second")
        for backlinker in _published_pages(session, workspace_id, 600):
            _edge(session, workspace_id, backlinker, first, "wikilink")
            _edge(session, workspace_id, backlinker, second, "wikilink")
        seed_ids = [first.id, second.id]

    with session_factory() as session:
        alone = _ranked(session, workspace_id, seed_ids[:1], edge_budget=750)
        together = _ranked(session, workspace_id, seed_ids, edge_budget=750)

    # 600 fits one seed's 1,024 rows, not each of two seeds' 512.
    assert alone.scan_capped is False
    assert together.scan_capped is True
    assert together.raw_scanned <= 2 * 2 * (1_024 // 2 + 1)


def test_the_tree_lookup_bound_alone_makes_a_bounded_sample(
    session_factory, monkeypatch
):
    """No partition overflows; the request-wide tree lookup budget does."""

    monkeypatch.setattr(graph_service_module, "GRAPH_TREE_HYDRATION_NODE_LIMIT", 32)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        for _ in range(50):
            _edge(
                session,
                workspace_id,
                seed,
                _folder(session, workspace_id),
                "wikilink",
                9.0,
            )
        for _ in range(100):
            _edge(session, workspace_id, _folder(session, workspace_id), seed, "tree")
        seed_id = seed.id

    with session_factory() as session:
        ranked = _ranked(session, workspace_id, [seed_id])
        payload = _query(session, workspace_id, [seed_id])

    assert ranked.raw_scanned < 1_024 * 2
    assert ranked.scan_capped is True
    assert payload.completeness == "bounded_sample"
    assert "graph_projection_source_bounded" in payload.warnings


def test_windowed_hydration_breaks_equal_ranks_by_date_like_the_budget(
    session_factory, monkeypatch
):
    """Read-together partners of equal weight: the newest come first, and
    their dates run against their ids, so a collector that broke the tie by
    id alone would hydrate the wrong window."""

    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, title="Seed")
        partners = sorted(
            (_page(session, workspace_id) for _ in range(12)), key=lambda page: page.id
        )
        for index, partner in enumerate(partners):
            first, second = sorted((seed, partner), key=lambda page: str(page.id))
            _edge(
                session,
                workspace_id,
                first,
                second,
                "co_access",
                3.0,
                occurred_at=now - timedelta(days=30 - index),
            )
        seed_id = seed.id
        newest = {partner.id for partner in partners[-5:]}

    with session_factory() as session:
        windowed = _query(session, workspace_id, [seed_id], nodes=6, edges=5)
    monkeypatch.setattr(
        graph_service_module, "_hydration_window", lambda _budget: 10**9
    )
    with session_factory() as session:
        full = _query(session, workspace_id, [seed_id], nodes=6, edges=5)

    assert _scene(windowed) == _scene(full)
    shown = {node.entity_id for node in windowed.nodes if node.entity_id != seed_id}
    assert shown == newest


def test_reused_statements_answer_each_reader_for_themselves(
    session_factory, monkeypatch
):
    """Statements are reused across requests; a reader never gets another's."""

    workspace_id, seed_id, backlinker_id, folder_id = _hidden_fixture(session_factory)
    with session_factory.begin() as session:
        _add_draft_target_of_backlinker(
            session, workspace_id, seed_id, backlinker_id, folder_id
        )

    def fresh_caches():
        monkeypatch.setattr(graph_service_module, "_DENOMINATOR_STATEMENTS", {})
        monkeypatch.setattr(graph_service_module, "_PARTITION_STATEMENTS", {})

    def scene(role):
        with session_factory() as session:
            return _scene(_query(session, workspace_id, [seed_id], role=role))

    fresh_caches()
    viewer_alone = scene("viewer")
    fresh_caches()
    owner_alone = scene("owner")
    fresh_caches()
    owner_first = scene("owner")
    viewer_after_owner = scene("viewer")
    owner_after_viewer = scene("owner")

    assert viewer_after_owner == viewer_alone
    assert owner_first == owner_after_viewer == owner_alone
    # The owner reads the draft, so the two scenes really differ.
    assert owner_alone != viewer_alone
    # Two orientations per signature: both viewer requests shared theirs, and
    # each owner (a new actor every call here) read drafts under their own.
    assert len(graph_service_module._PARTITION_STATEMENTS) == 2 * 3
