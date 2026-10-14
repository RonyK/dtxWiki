"""Edge-projection registry (G0 §19.5.4): completeness, vocabulary, rebuild."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.models.base import Base
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
    PageProvenance,
)
from backend.models.graph import GraphObservation
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.library_discovery import LibrarySyncRun
from backend.models.mcp_chat import McpChatArtifactKind
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.services.entity_ref import EntityKind
from backend.services.graph_service import HYDRATORS
from backend.services.indexing.edge_registry import (
    DEFERRED_EDGE_RELATIONS,
    EDGE_BEARING_RELATIONS,
    EDGE_EXTRACTORS,
    EDGE_RELATION_SOURCES,
    rebuild_edge_projection,
    reconcile_wikilink_edge_projection,
    refresh_page_edge_projection,
)

pytestmark = pytest.mark.integration


def test_every_edge_bearing_relation_has_an_extractor():
    """§19.5.4 completeness: the roster and the registry must not drift.

    A new edge-bearing relation must either register an extractor or be
    explicitly deferred with a design reference.
    """

    covered = set(EDGE_EXTRACTORS) | set(DEFERRED_EDGE_RELATIONS)
    assert set(EDGE_BEARING_RELATIONS) == covered
    assert not (set(EDGE_EXTRACTORS) & set(DEFERRED_EDGE_RELATIONS))
    for edge_kind, reason in DEFERRED_EDGE_RELATIONS.items():
        assert reason.strip(), f"deferred relation {edge_kind} needs a reason"
    assert sum(map(len, EDGE_BEARING_RELATIONS.values())) == len(EDGE_RELATION_SOURCES)


_GRAPH_FK_TARGET_TABLES = {
    "agent_input_bundle",
    "agent_page",
    "agent_page_version",
    "ask_session",
    "ask_turn",
    "audit_event",
    "change_set",
    "context_ingress_proposal",
    "context_page",
    "context_page_version",
    "job",
    "library_artifact",
    "library_discovery_run",
    "library_item",
    "library_item_version",
    "library_sync_run",
    "mcp_chat_session",
    "mcp_tool_call",
    "review_item",
    "workspace_tree_node",
}
_NON_EDGE_RELATION_SOURCES = {
    # Mutable head pointers and rebuildable lookup/index rows are storage
    # mechanics, not knowledge edges.
    "agent_page.current_active_version_id->agent_page_version.id",
    "agent_page.latest_draft_version_id->agent_page_version.id",
    "context_page.current_published_version_id->context_page_version.id",
    "context_page.latest_draft_version_id->context_page_version.id",
    # The exact public-exposure snapshot is another mutable delivery pointer.
    # Its composite FK proves that the exposed version belongs to this page;
    # neither component is a semantic knowledge edge.
    "context_page.public_exposed_version_id->context_page_version.id",
    "context_page.id->context_page_version.page_id",
    "library_item.current_version_id->library_item_version.id",
    "library_item.current_ready_version_id->library_item_version.id",
    # Composite FK component that proves the ready version belongs to the item;
    # it is an integrity guard for the pointer above, not a second graph edge.
    "library_item.id->library_item_version.library_item_id",
    # Conversion and preview_generation share one physical Run/Attempt table,
    # stage-discriminated -- same "processing bookkeeping for one exact
    # version, not a knowledge edge" reasoning as the two per-stage tables
    # (library_conversion_run/attempt, library_preview_run/attempt) this
    # replaces.
    "library_run.library_item_version_id->library_item_version.id",
    "library_attempt.library_item_version_id->library_item_version.id",
    "library_projection_outbox.library_item_version_id->library_item_version.id",
    "page_name_index.page_id->context_page.id",
    "page_name_index.source_version_id->context_page_version.id",
    # Upload bindings are delivery-authorization metadata, not semantic
    # knowledge relations. They control when a content-addressed file becomes
    # visible with a published Context version.
    "upload_asset.page_id_hint->context_page.id",
    "upload_asset_binding.page_id->context_page.id",
    "upload_asset_binding.page_version_id->context_page_version.id",
    # Context media rows are attachment ownership and exact-version usage
    # evidence. Markdown reference extraction remains the semantic graph
    # authority, so projecting these integrity FKs would duplicate page edges.
    "context_media_asset.workspace_id->context_page.workspace_id",
    "context_media_asset.page_id->context_page.id",
    # P2.3 page-first creation: ContextMediaSession's page-less "session-bound"
    # ownership shape is gone -- every asset is page-owned via ContextPage
    # directly (classified above) AND now also carries the exact
    # ContextPageVersion it was created for (mcp_chat_service.py's
    # media_created_for_version join uses this to detect a private "editing"
    # row), same non-edge reasoning as the ContextPage FKs above.
    "context_media_asset.created_for_version_id->context_page_version.id",
    "context_media_asset.page_id->context_page_version.page_id",
    "context_media_asset.workspace_id->context_page_version.workspace_id",
    "context_media_usage.page_id->context_page_version.page_id",
    "context_media_usage.page_version_id->context_page_version.id",
    "content_chunk.document_kind+document_id",
    "edge_projection.source_kind+source_id",
    "edge_projection.target_kind+target_id",
    "lint_finding.target_type+target_id",
    # M0 queue rows point at maintenance targets — operational state, not
    # knowledge relations (design §17.3.1-c).
    "client_delegated_task.target_kind+target_id",
    # A-1: preview_generation's input-provenance field (which immutable
    # conversion artifact an attempt consumed) now lives on the 1:1
    # LibraryAttemptPreviewDetail extension table rather than directly on the
    # (now shared) attempt row -- same "pipeline provenance, again not a
    # knowledge edge" reasoning as before. library_run/library_attempt above
    # already cover this stage's library_item_version_id bookkeeping FKs, and
    # library_preview_artifact is now literally library_artifact (already
    # classified elsewhere), so neither needs a line of its own here anymore.
    "library_attempt_preview_detail.input_artifact_id->library_artifact.id",
    # The six new 1:1 *ConversionDetail/*PreviewDetail extension tables (one
    # each for LibraryRun/LibraryAttempt/LibraryArtifact) are the other half
    # of their parent row, split out only to keep stage-specific columns off
    # the shared table -- not a second knowledge edge between two different
    # entities. Only library_artifact_*_detail shows up here at all (the
    # other four point at library_run/library_attempt, neither of which is a
    # graph-relevant target table) because their parent, library_artifact, is.
    "library_artifact_conversion_detail.artifact_id->library_artifact.id",
    "library_artifact_conversion_detail.library_item_version_id->library_artifact.library_item_version_id",
    "library_artifact_conversion_detail.stage->library_artifact.stage",
    "library_artifact_conversion_detail.workspace_id->library_artifact.workspace_id",
    "library_artifact_preview_detail.artifact_id->library_artifact.id",
    "library_artifact_preview_detail.library_item_version_id->library_artifact.library_item_version_id",
    "library_artifact_preview_detail.stage->library_artifact.stage",
    "library_artifact_preview_detail.workspace_id->library_artifact.workspace_id",
    # Another mutable head pointer, like current_version_id/current_ready_version_id.
    "library_item.current_preview_version_id->library_item_version.id",
    # The semantic overlay is editor-authored Markdown layered over one exact
    # base artifact, so these FKs are ownership and integrity fences on that
    # text. Markdown reference extraction stays the semantic graph authority —
    # the same reasoning applied to context media above.
    "library_semantic_overlay.library_item_version_id->library_item_version.id",
    "library_semantic_overlay.base_artifact_id->library_artifact.id",
    # Composite FK component proving the base artifact belongs to this version.
    "library_semantic_overlay.library_item_version_id->library_artifact.library_item_version_id",
    # Block Provenance redesign (design doc section 9): the read ledger's
    # mutable "last version this conversation read" pointer, used only to
    # auto-fill patch_draft's base when a caller omits it. The knowledge edge
    # itself is the semantic link already carried by
    # mcp_chat_artifact_link.resource_kind+resource_id+verb (REFERENCED for a
    # read, EDITED for a patch). This FK is bookkeeping for that link's
    # freshness, not a second relation.
    "mcp_chat_artifact_link.last_read_version_id->context_page_version.id",
    # Every composite FK enforcing "child.workspace_id == parent.
    # workspace_id" contributes a workspace_id->workspace_id column pair
    # alongside its real local-id->id pair (already classified above or in
    # EDGE_RELATION_SOURCES). That pair is the same kind of integrity-guard
    # component the comments above already call out for other composite
    # FKs -- it proves tenancy, not a second knowledge edge between the two
    # rows.
    "context_page_version.workspace_id->context_page.workspace_id",
    "page_links.workspace_id->context_page.workspace_id",
    "page_name_index.workspace_id->context_page.workspace_id",
    "page_provenance.workspace_id->context_page.workspace_id",
    "upload_asset.workspace_id->context_page.workspace_id",
    "upload_asset_binding.workspace_id->context_page.workspace_id",
    "ask_turn.workspace_id->context_page.workspace_id",
    "library_artifact.workspace_id->library_item_version.workspace_id",
    "library_run.workspace_id->library_item_version.workspace_id",
    "library_attempt.workspace_id->library_item_version.workspace_id",
    "library_projection_outbox.workspace_id->library_item_version.workspace_id",
    "library_semantic_overlay.workspace_id->library_item_version.workspace_id",
    "library_item_version.workspace_id->library_item.workspace_id",
    "library_discovery_candidate.workspace_id->library_item.workspace_id",
    "library_sync_run.workspace_id->library_item.workspace_id",
    "proposal_source_item.workspace_id->library_item.workspace_id",
    "library_discovery_run.workspace_id->job.workspace_id",
    "library_sync_run.workspace_id->job.workspace_id",
    "change_set.workspace_id->mcp_chat_session.workspace_id",
    "mcp_chat_artifact_link.workspace_id->mcp_chat_session.workspace_id",
    "mcp_chat_tool_call_artifact.workspace_id->mcp_chat_session.workspace_id",
    "mcp_chat_artifact_link.workspace_id->mcp_tool_call.workspace_id",
    "mcp_chat_artifact_link.workspace_id->context_page_version.workspace_id",
    "mcp_chat_tool_call_artifact.workspace_id->mcp_tool_call.workspace_id",
    "change_operation.workspace_id->change_set.workspace_id",
    "context_ingress_proposal.workspace_id->change_set.workspace_id",
    "feedback_item.workspace_id->change_set.workspace_id",
    "mcp_tool_call.workspace_id->change_set.workspace_id",
    "review_item.workspace_id->change_set.workspace_id",
    "review_decision.workspace_id->review_item.workspace_id",
    "agent_page_version.workspace_id->agent_page.workspace_id",
    "agent_page.workspace_id->agent_page_version.workspace_id",
    "library_discovery_candidate.workspace_id->library_discovery_run.workspace_id",
    "library_item_version.workspace_id->library_sync_run.workspace_id",
    "proposal_source_item.workspace_id->context_ingress_proposal.workspace_id",
    "ask_session.workspace_id->workspace_tree_node.workspace_id",
    "ask_turn.workspace_id->workspace_tree_node.workspace_id",
    "context_ingress_proposal.workspace_id->workspace_tree_node.workspace_id",
    "workspace_tree_node.workspace_id->workspace_tree_node.workspace_id",
    "ask_turn.workspace_id->ask_session.workspace_id",
    "ask_turn_citation.workspace_id->ask_turn.workspace_id",
    "context_ingress_proposal.workspace_id->ask_turn.workspace_id",
    "context_citation.workspace_id->library_artifact.workspace_id",
    "library_attempt_preview_detail.workspace_id->library_artifact.workspace_id",
    "agent_input_bundle_item.workspace_id->library_artifact.workspace_id",
    "context_citation.workspace_id->context_page_version.workspace_id",
    "context_claim.workspace_id->context_page_version.workspace_id",
    "agent_input_bundle_item.workspace_id->context_page_version.workspace_id",
    "context_page_version.workspace_id->context_page_version.workspace_id",
    "context_citation.workspace_id->library_item_version.workspace_id",
    "library_item_version.workspace_id->library_item_version.workspace_id",
    "agent_input_bundle_item.workspace_id->agent_input_bundle.workspace_id",
    "ask_turn.workspace_id->agent_input_bundle.workspace_id",
    "mcp_tool_call.workspace_id->agent_input_bundle.workspace_id",
    "agent_input_bundle_item.workspace_id->agent_page_version.workspace_id",
    "page_links.workspace_id->context_page_version.workspace_id",
    "page_name_index.workspace_id->context_page_version.workspace_id",
    "upload_asset_binding.workspace_id->context_page_version.workspace_id",
    "context_page.workspace_id->context_page_version.workspace_id",
    "library_semantic_overlay.workspace_id->library_artifact.workspace_id",
    "library_item.workspace_id->library_item_version.workspace_id",
    "context_media_usage.workspace_id->context_page_version.workspace_id",
    "mcp_tool_call.workspace_id->audit_event.workspace_id",
}


def _metadata_edge_candidates() -> set[str]:
    candidates: set[str] = set()
    for table in Base.metadata.tables.values():
        columns = set(table.c.keys())
        for column in table.c:
            for foreign_key in column.foreign_keys:
                target_table = foreign_key.column.table.name
                if target_table in _GRAPH_FK_TARGET_TABLES:
                    candidates.add(
                        f"{table.name}.{column.name}->"
                        f"{target_table}.{foreign_key.column.name}"
                    )
        for prefix in ("artifact", "document", "resource", "source", "target"):
            id_column = f"{prefix}_id"
            kind_column = (
                f"{prefix}_kind" if f"{prefix}_kind" in columns else f"{prefix}_type"
            )
            if id_column in columns and kind_column in columns:
                candidates.add(f"{table.name}.{kind_column}+{id_column}")

    review_columns = Base.metadata.tables["review_decision"].c
    if {
        "audience_kind",
        "audience_user_id",
    } <= set(review_columns.keys()):
        candidates.add("review_decision.audience_kind+audience_user_id")
    return candidates


def test_metadata_edge_candidates_are_registered_or_explicitly_non_edges():
    """A new polymorphic pair or domain FK must make an explicit roster choice."""

    candidates = _metadata_edge_candidates()
    assert candidates == set(EDGE_RELATION_SOURCES) | _NON_EDGE_RELATION_SOURCES


def test_mcp_chat_resource_kinds_align_with_entity_kind_vocabulary():
    """G0-3: kind strings across surfaces come from one vocabulary."""

    entity_values = {kind.value for kind in EntityKind}
    assert {kind.value for kind in McpChatArtifactKind} <= entity_values


def _seed_page(session, workspace_id, slug):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=slug.title(),
        page_type="concept",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {slug}",
        content_hash=f"{slug}-v1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    page.current_version_id = version.id
    return page, version


def test_page_edge_refresh_fixed_point_preserves_projection_state(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        source, source_version = _seed_page(
            session,
            workspace.id,
            "fixed-point-source",
        )
        target, _target_version = _seed_page(
            session,
            workspace.id,
            "fixed-point-target",
        )
        session.add(
            PageLink(
                workspace_id=workspace.id,
                source_page_id=source.id,
                source_version_id=source_version.id,
                target_slug=target.slug,
                target_page_id=target.id,
                occurrence_count=2,
            )
        )
        refresh_page_edge_projection(session, workspace.id, source.id)
        edge = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace.id,
                EdgeProjection.source_id == source.id,
                EdgeProjection.target_id == target.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
        assert edge is not None
        session.flush()
        workspace_id = workspace.id
        source_id = source.id
        edge_id = edge.id
        fixed_point_state = (
            edge.id,
            edge.created_at,
            edge.last_seen_at,
        )
        revision = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        observation_count = len(
            session.scalars(
                select(GraphObservation.id).where(
                    GraphObservation.workspace_id == workspace_id
                )
            ).all()
        )

    with session_factory.begin() as session:
        refresh_page_edge_projection(session, workspace_id, source_id)

    with session_factory() as session:
        edge = session.get(EdgeProjection, edge_id)
        assert edge is not None
        assert (edge.id, edge.created_at, edge.last_seen_at) == fixed_point_state
        assert (
            session.scalar(
                select(Workspace.edge_projection_version).where(
                    Workspace.id == workspace_id
                )
            )
            == revision
        )
        assert (
            len(
                session.scalars(
                    select(GraphObservation.id).where(
                        GraphObservation.workspace_id == workspace_id
                    )
                ).all()
            )
            == observation_count
        )


def test_page_edge_refresh_repairs_semantic_drift_in_place(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        source, source_version = _seed_page(
            session,
            workspace.id,
            "semantic-drift-source",
        )
        target, _target_version = _seed_page(
            session,
            workspace.id,
            "semantic-drift-target",
        )
        session.add(
            PageLink(
                workspace_id=workspace.id,
                source_page_id=source.id,
                source_version_id=source_version.id,
                target_slug=target.slug,
                target_page_id=target.id,
                occurrence_count=3,
            )
        )
        refresh_page_edge_projection(session, workspace.id, source.id)
        edge = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace.id,
                EdgeProjection.source_id == source.id,
                EdgeProjection.target_id == target.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
        assert edge is not None
        session.flush()
        workspace_id = workspace.id
        source_id = source.id
        edge_id = edge.id
        edge_created_at = edge.created_at
        wrong_occurred_at = source_version.created_at

    with session_factory.begin() as session:
        edge = session.get(EdgeProjection, edge_id)
        assert edge is not None
        edge.provenance = "usage"
        edge.source_ref = {"table": "wrong_source"}
        edge.occurred_at = wrong_occurred_at

    with session_factory() as session:
        revision_before = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        observation_count = len(
            session.scalars(
                select(GraphObservation.id).where(
                    GraphObservation.workspace_id == workspace_id
                )
            ).all()
        )

    with session_factory.begin() as session:
        refresh_page_edge_projection(session, workspace_id, source_id)

    with session_factory() as session:
        edge = session.get(EdgeProjection, edge_id)
        assert edge is not None
        assert edge.created_at == edge_created_at
        assert edge.provenance == "curated"
        assert edge.weight == 3.0
        assert edge.occurred_at is None
        assert edge.source_ref == {"table": "page_links"}
        assert (
            session.scalar(
                select(Workspace.edge_projection_version).where(
                    Workspace.id == workspace_id
                )
            )
            == revision_before + 1
        )
        assert (
            len(
                session.scalars(
                    select(GraphObservation.id).where(
                        GraphObservation.workspace_id == workspace_id
                    )
                ).all()
            )
            == observation_count
        )


def test_page_edge_refresh_updates_weight_in_place_without_identity_observation(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        source, source_version = _seed_page(
            session,
            workspace.id,
            "weight-change-source",
        )
        target, _target_version = _seed_page(
            session,
            workspace.id,
            "weight-change-target",
        )
        link = PageLink(
            workspace_id=workspace.id,
            source_page_id=source.id,
            source_version_id=source_version.id,
            target_slug=target.slug,
            target_page_id=target.id,
            occurrence_count=1,
        )
        session.add(link)
        refresh_page_edge_projection(session, workspace.id, source.id)
        edge = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace.id,
                EdgeProjection.source_id == source.id,
                EdgeProjection.target_id == target.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
        assert edge is not None
        session.flush()
        workspace_id = workspace.id
        source_id = source.id
        link_id = link.id
        edge_id = edge.id
        edge_created_at = edge.created_at

    with session_factory.begin() as session:
        link = session.get(PageLink, link_id)
        assert link is not None
        link.occurrence_count = 5

    with session_factory() as session:
        revision_before = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace_id
            )
        )
        observation_count = len(
            session.scalars(
                select(GraphObservation.id).where(
                    GraphObservation.workspace_id == workspace_id
                )
            ).all()
        )

    with session_factory.begin() as session:
        refresh_page_edge_projection(session, workspace_id, source_id)

    with session_factory() as session:
        edge = session.get(EdgeProjection, edge_id)
        assert edge is not None
        assert edge.created_at == edge_created_at
        assert edge.weight == 5.0
        assert (
            session.scalar(
                select(Workspace.edge_projection_version).where(
                    Workspace.id == workspace_id
                )
            )
            == revision_before + 1
        )
        assert (
            len(
                session.scalars(
                    select(GraphObservation.id).where(
                        GraphObservation.workspace_id == workspace_id
                    )
                ).all()
            )
            == observation_count
        )


def test_narrow_wikilink_reconcile_never_mutates_sources_outside_lock_set(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        locked_source, locked_version = _seed_page(
            session,
            workspace.id,
            "locked-source",
        )
        outside_stale, _outside_stale_version = _seed_page(
            session,
            workspace.id,
            "outside-stale",
        )
        outside_changed, outside_changed_version = _seed_page(
            session,
            workspace.id,
            "outside-changed",
        )
        target, _target_version = _seed_page(
            session,
            workspace.id,
            "narrow-target",
        )
        session.add_all(
            [
                PageLink(
                    workspace_id=workspace.id,
                    source_page_id=locked_source.id,
                    source_version_id=locked_version.id,
                    target_slug=target.slug,
                    target_page_id=target.id,
                    occurrence_count=2,
                ),
                PageLink(
                    workspace_id=workspace.id,
                    source_page_id=outside_changed.id,
                    source_version_id=outside_changed_version.id,
                    target_slug=target.slug,
                    target_page_id=target.id,
                    occurrence_count=7,
                ),
            ]
        )
        locked_edge = EdgeProjection(
            workspace_id=workspace.id,
            source_kind="context_page",
            source_id=locked_source.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=1.0,
            source_ref={"marker": "locked-before"},
        )
        outside_stale_edge = EdgeProjection(
            workspace_id=workspace.id,
            source_kind="context_page",
            source_id=outside_stale.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=13.0,
            source_ref={"marker": "outside-stale"},
        )
        outside_changed_edge = EdgeProjection(
            workspace_id=workspace.id,
            source_kind="context_page",
            source_id=outside_changed.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            provenance="usage",
            weight=99.0,
            source_ref={"marker": "outside-changed"},
        )
        session.add_all([locked_edge, outside_stale_edge, outside_changed_edge])
        session.flush()
        workspace_id = workspace.id
        locked_source_id = locked_source.id
        locked_edge_id = locked_edge.id
        outside_edge_ids = {
            outside_stale_edge.id,
            outside_changed_edge.id,
        }

    with session_factory() as session:
        outside_before = _edge_projection_snapshots(session, outside_edge_ids)

    with session_factory.begin() as session:
        delta = reconcile_wikilink_edge_projection(
            session,
            workspace_id,
            source_page_ids=frozenset({locked_source_id}),
        )

    assert delta.desired_count == 1
    assert delta.inserted == 0
    assert delta.updated == 1
    assert delta.deleted == 0
    assert delta.affected_source_ids == frozenset({locked_source_id})
    with session_factory() as session:
        locked_after = session.get(EdgeProjection, locked_edge_id)
        assert locked_after is not None
        assert locked_after.weight == 2.0
        assert _edge_projection_snapshots(session, outside_edge_ids) == outside_before


def _edge_projection_snapshots(session, edge_ids):
    rows = session.scalars(
        select(EdgeProjection)
        .where(EdgeProjection.id.in_(edge_ids))
        .order_by(EdgeProjection.id)
    ).all()
    return [
        (
            edge.id,
            edge.source_kind,
            edge.source_id,
            edge.target_kind,
            edge.target_id,
            edge.edge_kind,
            edge.provenance,
            edge.weight,
            edge.occurred_at,
            edge.source_ref,
            edge.created_at,
            edge.last_seen_at,
        )
        for edge in rows
    ]


def _seed_every_relation(session):
    """One row for each relation the full rebuild projects, beside the tree."""

    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    node = session.scalar(
        select(WorkspaceTreeNode).where(WorkspaceTreeNode.workspace_id == workspace.id)
    )
    page_a, version_a = _seed_page(session, workspace.id, "edge-source")
    page_b, _ = _seed_page(session, workspace.id, "edge-target")

    session.add(
        PageLink(
            workspace_id=workspace.id,
            source_page_id=page_a.id,
            source_version_id=version_a.id,
            target_slug=page_b.slug,
            target_page_id=page_b.id,
        )
    )

    ask_session = AskSession(
        workspace_id=workspace.id,
        title="Edges",
        actor_kind="human",
        placement_tree_node_id=node.id,
    )
    session.add(ask_session)
    session.flush()
    turn = AskTurn(
        workspace_id=ask_session.workspace_id,
        ask_session_id=ask_session.id,
        turn_no=1,
        user_message="q",
        assistant_message="a",
        promoted_context_page_id=page_a.id,
    )
    session.add(turn)
    session.flush()
    session.add(
        AskTurnCitation(
            workspace_id=turn.workspace_id,
            ask_turn_id=turn.id,
            target_kind="context_page",
            target_id=page_b.id,
            rank=0,
        )
    )
    session.add(
        PageProvenance(
            workspace_id=workspace.id,
            page_id=page_a.id,
            source_kind="ask_turn",
            source_id=turn.id,
        )
    )

    item = LibraryItem(
        workspace_id=workspace.id,
        connector_type="url",
        format_hint="html",
        uri="https://example.com/edges",
        metadata_json={},
        trust_level="normal",
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    sync_run = LibrarySyncRun(
        workspace_id=workspace.id,
        library_item_id=item.id,
        trigger_type="manual",
        status="completed",
    )
    session.add(sync_run)
    session.flush()
    session.add(
        LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_no=1,
            content_hash="hash",
            raw_storage_ref="ref",
            processing_status="ready",
            sync_run_id=sync_run.id,
        )
    )
    session.flush()
    return SimpleNamespace(
        workspace=workspace,
        page_a=page_a,
        version_a=version_a,
        page_b=page_b,
        turn=turn,
        sync_run=sync_run,
    )


def test_rebuild_edge_projection_projects_all_relations_idempotently(
    session_factory,
):
    with session_factory.begin() as session:
        seeded = _seed_every_relation(session)
        workspace, page_a, page_b = seeded.workspace, seeded.page_a, seeded.page_b
        turn, sync_run = seeded.turn, seeded.sync_run

        counts = rebuild_edge_projection(
            session, workspace.id, usage_signals_enabled=True
        )
        assert {
            key: counts[key]
            for key in (
                "ask_citation",
                "promoted_from",
                "page_provenance",
                "library_sync_version",
                "wikilink",
            )
        } == {
            "ask_citation": 1,
            "promoted_from": 1,
            "page_provenance": 1,
            "library_sync_version": 1,
            "wikilink": 1,
        }
        # G1 kinds: the fixture tree (roots + placements) projects hierarchy
        # edges; no citations or version parents were seeded.
        assert counts["tree"] > 0
        assert counts["citation_page"] == 0
        assert counts["citation_source"] == 0
        assert counts["version_parent"] == 0

        # Idempotent: a second rebuild replaces, never duplicates.
        counts_again = rebuild_edge_projection(
            session, workspace.id, usage_signals_enabled=True
        )
        assert counts_again == counts
        edges = session.scalars(
            select(EdgeProjection).where(EdgeProjection.workspace_id == workspace.id)
        ).all()
        assert len(edges) == 5 + counts["tree"]
        by_kind = {edge.edge_kind: edge for edge in edges}
        assert by_kind["wikilink"].source_id == page_a.id
        assert by_kind["wikilink"].target_id == page_b.id
        assert by_kind["ask_citation"].source_id == turn.id
        assert by_kind["ask_citation"].target_kind == "context_page"
        assert by_kind["promoted_from"].source_id == page_a.id
        assert by_kind["page_provenance"].target_id == turn.id
        assert by_kind["library_sync_version"].target_id == sync_run.id


HALF_LIFE_KINDS = (
    "version_parent",
    "version_merge",
    "ask_citation",
    "library_sync_version",
)


def test_half_life_kinds_never_join_a_page_and_a_hydratable_neighbor(
    session_factory,
):
    """Guard: the Local Graph never ranks a relation that decays with time.

    The ranked collector reads a row only when both of its ends hydrate (a
    seed is a hydrated entity too), and no kind whose rank halves with age has
    such a row: they link page versions, Ask turns and sync runs. The
    collector keeps an overflowing partition's rows by raw weight, which for
    these kinds is not rank order. When F09 gives ask_turn a hydrator,
    ask_citation joins an Ask turn to a page and this fails; the partition
    order has to change with it.
    """

    with session_factory.begin() as session:
        seeded = _seed_every_relation(session)
        workspace = seeded.workspace
        # Lineage for both version families.
        child = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=seeded.page_a.id,
            version_no=2,
            body_md="# edge-source v2",
            content_hash="edge-source-v2",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=seeded.version_a.id,
        )
        session.add(child)
        session.flush()
        merged = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=seeded.page_a.id,
            version_no=3,
            body_md="# edge-source v3",
            content_hash="edge-source-v3",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            parent_version_id=seeded.version_a.id,
            merged_into_version_id=child.id,
        )
        session.add(merged)
        session.flush()

        rebuild_edge_projection(session, workspace.id, usage_signals_enabled=True)
        rows = session.scalars(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace.id,
                EdgeProjection.edge_kind.in_(HALF_LIFE_KINDS),
            )
        ).all()

    assert {row.edge_kind for row in rows} == set(HALF_LIFE_KINDS)
    for row in rows:
        assert not (row.source_kind in HYDRATORS and row.target_kind in HYDRATORS), (
            row.edge_kind,
            row.source_kind,
            row.target_kind,
        )
