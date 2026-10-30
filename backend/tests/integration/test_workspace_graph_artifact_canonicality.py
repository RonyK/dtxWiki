"""Retained conversion attempts must not enter the current document map."""

from dataclasses import replace

import pytest
from sqlalchemy import select, text

from backend.models.context import ContextCitation, ContextClaim
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.graph_service import GraphService
from backend.services.tree_projection import TreeProjectionService
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.integration.test_library_canonical_read_paths import (
    _actor,
    _seed_canonical_fixture,
    _seed_graph_page,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("selected_attempt", ["replacement", None])
@pytest.mark.parametrize("citation_target", ["context_page_version", "context_claim"])
def test_retained_attempt_placements_and_citations_leave_map_payload_exact(
    session_factory, selected_attempt, citation_target
):
    # This fixture uses real attempts, artifacts and media bindings. Turn on
    # SQLite FK enforcement so hidden evidence is valid retained data, rather
    # than a malformed pointer whose rejection would mask the attempt rule.
    with session_factory.kw["bind"].connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seeded = _seed_canonical_fixture(session, workspace_id)
        page = _seed_graph_page(session, workspace_id)
        # Simulate reconversion of the same ready source: its old attempt and
        # artifact remain stored after another attempt is selected, or after
        # the selection is cleared. The pending newer version is not the head.
        replacement = seeded.unselected_artifact
        seeded.ready_version.selected_conversion_attempt_id = (
            replacement.attempt_id if selected_attempt == "replacement" else None
        )
        session.add(
            WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="library",
                node_kind="library_item_ref",
                sort_key="primary",
                title="Canonical Library item",
                target_kind="library_item",
                target_id=seeded.item.id,
                primary_placement=True,
                visibility_status="visible",
            )
        )
        citation_target_id = page.current_published_version_id
        if citation_target == "context_claim":
            claim = ContextClaim(
                workspace_id=workspace_id,
                page_version_id=page.current_published_version_id,
                claim_text="Current published claim",
            )
            session.add(claim)
            session.flush()
            citation_target_id = claim.id

        def add_artifact_evidence(artifact, title):
            placement = WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="library",
                node_kind="library_artifact_ref",
                sort_key=title,
                title=title,
                target_kind="library_artifact",
                target_id=artifact.id,
                primary_placement=False,
                visibility_status="visible",
            )
            session.add_all(
                [
                    placement,
                    ContextCitation(
                        workspace_id=workspace_id,
                        target_kind=citation_target,
                        target_id=citation_target_id,
                        library_artifact_id=artifact.id,
                        citation_type="derived_artifact",
                        verification_status="verified",
                    ),
                ]
            )
            session.flush()
            return placement

        eligible_placement = None
        if selected_attempt == "replacement":
            eligible_placement = add_artifact_evidence(
                replacement, "Selected replacement artifact"
            )
        session.flush()
        service = WorkspaceGraphService(cursor_secret="artifact-canonicality-test")
        request = WorkspaceGraphOverviewRequest()
        actors = [
            replace(_actor(workspace_id), role=role, workspace_role=role)
            for role in ("viewer", "editor", "owner")
        ]
        baseline = {
            actor.role: service.query(
                session, workspace_id, request=request, actor=actor
            )
            for actor in actors
        }
        for scene in baseline.values():
            library_node = next(
                node
                for node in scene.nodes
                if node.grain == "document" and node.entity_id == seeded.item.id
            )
            assert [ref.tree_node_id for ref in library_node.also_appears_in] == (
                [eligible_placement.id] if eligible_placement is not None else []
            )
            citations = [
                edge for edge in scene.edges if edge.relation_kind == "citation_source"
            ]
            assert len(citations) == (1 if eligible_placement is not None else 0)
            if citations:
                assert citations[0].occurrence_weight == 1

        hidden_artifacts = [seeded.selected_artifact]
        if selected_attempt is None:
            hidden_artifacts.append(replacement)
        hidden_placements = [
            add_artifact_evidence(artifact, f"UNSELECTED ATTEMPT {index}")
            for index, artifact in enumerate(hidden_artifacts)
        ]
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        for actor in actors:
            # Independently establish the existing ordinary-read decision.
            tree = TreeProjectionService().load(session, workspace_id, actor)
            assert all(node.id not in tree.visible_ids for node in hidden_placements)
            assert (
                GraphService().hydrate_entities(
                    session,
                    workspace_id,
                    entities=[
                        ("library_artifact", artifact.id)
                        for artifact in hidden_artifacts
                    ],
                    actor=actor,
                )
                == {}
            )
            after = service.query(session, workspace_id, request=request, actor=actor)
            # Includes labels/paths, counts, occurrence weights, revision and
            # freshness. Invisible retained rows must not affect any of them.
            assert after.model_dump_json() == baseline[actor.role].model_dump_json()
