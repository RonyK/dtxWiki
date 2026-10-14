import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

import backend.services.workspace_graph_service as workspace_graph_module
from backend.models.context import (
    ContextCitation,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.read_errors import ReadValidationError
from backend.services.workspace_graph_service import WorkspaceGraphService

pytestmark = pytest.mark.integration

_BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _noncanonical_same_bytes(value: str) -> str:
    """Change only unused final sextet bits in an unpadded base64url value."""

    assert len(value) % 4 in {2, 3}
    unused_bits = 4 if len(value) % 4 == 2 else 2
    index = _BASE64URL_ALPHABET.index(value[-1])
    assert index % (1 << unused_bits) == 0
    alternative = _BASE64URL_ALPHABET[index + 1]
    mutated = value[:-1] + alternative
    padding = "=" * (-len(value) % 4)
    assert base64.urlsafe_b64decode(value + padding) == base64.urlsafe_b64decode(
        mutated + padding
    )
    return mutated


def _actor(role="viewer", actor_id="workspace-graph-actor"):
    return ActorContext(
        actor_kind="human",
        actor_id=actor_id,
        token_kind="session",
        scopes=(),
        workspace_role=role,
        role=role,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _page(session, workspace_id, slug, *, published=True):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        id=uuid4(),
        page_id=page.id,
        version_no=1,
        body_md=f"# {page.title}",
        content_hash=f"workspace-graph-{slug}",
        actor_type="human",
        actor_id=None,
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
    return page, version


def _link(
    session,
    workspace_id,
    source,
    source_version,
    target,
    *,
    occurrences=1,
    authored_at=None,
    link_syntax="wikilink",
):
    page_link = PageLink(
        workspace_id=workspace_id,
        source_page_id=source.id,
        source_version_id=source_version.id,
        link_syntax=link_syntax,
        target_slug=target.slug,
        target_page_id=target.id,
        occurrence_count=occurrences,
    )
    if authored_at is not None:
        page_link.created_at = authored_at
    session.add(page_link)
    projection = EdgeProjection(
        workspace_id=workspace_id,
        source_kind="context_page",
        source_id=source.id,
        target_kind="context_page",
        target_id=target.id,
        edge_kind="wikilink",
        provenance="curated",
        weight=float(occurrences),
        source_ref={"source_table": "page_links"},
    )
    session.add(projection)
    return projection


def _library(session, workspace_id, *, title="Visible source", restricted=False):
    item = LibraryItem(
        id=uuid4(),
        workspace_id=workspace_id,
        connector_type="file",
        uri=f"file:///{uuid4()}.md",
        title=title,
        trust_level="restricted" if restricted else "verified",
        lifecycle_status="active",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=item.workspace_id,
        id=uuid4(),
        library_item_id=item.id,
        version_no=1,
        content_hash=f"library-{item.id}",
        raw_storage_ref=f"library/{item.id}",
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    return item, version


def _citation(session, workspace_id, source, source_version, item_version):
    session.add(
        ContextCitation(
            workspace_id=item_version.workspace_id,
            target_kind="context_page_version",
            target_id=source_version.id,
            target_location={},
            library_item_version_id=item_version.id,
            location={},
            citation_type="raw_source",
            verification_status="verified",
            created_via="cite_tool",
            created_by_actor_type="human",
        )
    )
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=source.id,
            target_kind="library_item_version",
            target_id=item_version.id,
            edge_kind="citation_source",
            provenance="curated",
            weight=1.0,
            source_ref={"source_table": "context_citation"},
        )
    )


def _folder(session, workspace_id, title):
    node = WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        parent_id=None,
        sort_key=title,
        title=title,
        slug=title.casefold(),
        target_kind=None,
        target_id=None,
        primary_placement=True,
        visibility_status="visible",
    )
    session.add(node)
    return node


def _placement(
    session,
    workspace_id,
    page,
    parent,
    *,
    primary=True,
):
    node = WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="context_page",
        parent_id=parent.id if parent is not None else None,
        sort_key=page.slug,
        title=page.title,
        slug=page.slug,
        target_kind="context_page",
        target_id=page.id,
        primary_placement=primary,
        visibility_status="visible",
    )
    session.add(node)
    return node


def test_exact_workspace_scene_uses_published_authored_metrics_and_placements(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source_a, version_a = _page(session, workspace_id, "source-a")
        source_b, version_b = _page(session, workspace_id, "source-b")
        target, target_version = _page(session, workspace_id, "target")
        hidden_parent, _hidden_version = _page(
            session, workspace_id, "draft-parent", published=False
        )
        library, library_version = _library(session, workspace_id)
        folder_a = _folder(session, workspace_id, "Folder A")
        folder_b = _folder(session, workspace_id, "Folder B")
        _placement(session, workspace_id, source_a, folder_a)
        _placement(session, workspace_id, source_b, folder_a)
        _placement(session, workspace_id, target, folder_b)
        secondary = _placement(session, workspace_id, target, folder_a, primary=False)
        hidden_parent_node = _placement(session, workspace_id, hidden_parent, None)
        orphaned_visible, _orphaned_version = _page(
            session, workspace_id, "hidden-ancestor-child"
        )
        _placement(session, workspace_id, orphaned_visible, hidden_parent_node)
        _link(session, workspace_id, source_a, version_a, target, occurrences=7)
        _link(session, workspace_id, source_b, version_b, target, occurrences=3)
        _link(session, workspace_id, target, target_version, target, occurrences=9)
        _citation(session, workspace_id, source_a, version_a, library_version)

    service = WorkspaceGraphService(cursor_secret="workspace-graph-test-secret")
    with session_factory() as session:
        links = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=_actor(),
        )
        assert links.representation == "exact"
        assert links.completeness == "complete"
        assert links.counts.candidate_relations == 2
        target_node = next(
            node
            for node in links.nodes
            if node.grain == "document" and node.entity_id == target.id
        )
        assert target_node.reference_count == 2
        assert target_node.primary_folder_label == "Folder B"
        assert [
            placement.tree_node_id for placement in target_node.also_appears_in
        ] == [secondary.id]
        assert all(edge.cross_folder for edge in links.edges)
        orphan_node = next(
            node
            for node in links.nodes
            if node.grain == "document" and node.entity_id == orphaned_visible.id
        )
        assert orphan_node.primary_folder_label == "No visible placement"

        evidence = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="evidence"),
            actor=_actor(),
        )
        library_node = next(
            node
            for node in evidence.nodes
            if node.grain == "document" and node.entity_id == library.id
        )
        assert library_node.evidence_count == 1
        assert library_node.path == f"/library?target={library.id}"

        default_scene = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        )
        assert default_scene.completeness == "complete"
        assert default_scene.counts.candidate_relations == 3
        assert {edge.relation_kind for edge in default_scene.edges} == {
            "wikilink",
            "citation_source",
        }


def test_stale_or_draft_projection_rows_fail_closed(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, _published = _page(session, workspace_id, "published-source")
        target, _target_version = _page(session, workspace_id, "published-target")
        # No matching published-head PageLink exists: the projection is stale
        # or draft-derived and must not survive the authored intersection.
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=target.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1,
                source_ref={"adversary": "draft"},
            )
        )

    with session_factory() as session:
        scene = WorkspaceGraphService(cursor_secret="secret").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=_actor(),
        )
        assert scene.edges == []
        assert scene.counts.candidate_relations == 0


def test_context_url_relation_is_authored_and_archived_target_is_hidden(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, source_version = _page(session, workspace_id, "url-source")
        visible_target, _visible_version = _page(
            session,
            workspace_id,
            "url-visible-target",
        )
        archived_target, _archived_version = _page(
            session,
            workspace_id,
            "url-archived-target",
        )
        archived_target.lifecycle_status = "archived"
        _link(
            session,
            workspace_id,
            source,
            source_version,
            visible_target,
            link_syntax="context_url",
        )
        _link(
            session,
            workspace_id,
            source,
            source_version,
            archived_target,
            link_syntax="context_url",
        )

    with session_factory() as session:
        scene = WorkspaceGraphService(cursor_secret="url-link-secret").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=_actor(),
        )

    assert scene.counts.candidate_relations == 1
    assert {node.entity_id for node in scene.nodes if node.grain == "document"} == {
        source.id,
        visible_target.id,
    }
    assert len(scene.edges) == 1
    assert scene.edges[0].relation_kind == "wikilink"


def test_same_identity_projection_timestamp_cannot_influence_visible_scene(
    session_factory,
):
    old_time = datetime(2000, 1, 1, tzinfo=UTC)
    recent_time = datetime(2050, 1, 1, tzinfo=UTC)
    stale_projection_time = datetime(2099, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        old_source, old_source_version = _page(session, workspace_id, "old-source")
        old_target, old_target_version = _page(session, workspace_id, "old-target")
        recent_source, recent_source_version = _page(
            session, workspace_id, "recent-source"
        )
        recent_target, recent_target_version = _page(
            session, workspace_id, "recent-target"
        )
        old_source_version.created_at = old_time
        old_target_version.created_at = old_time
        recent_source_version.created_at = recent_time
        recent_target_version.created_at = recent_time
        stale = _link(
            session,
            workspace_id,
            old_source,
            old_source_version,
            old_target,
            authored_at=old_time,
        )
        stale.occurred_at = stale_projection_time
        recent = _link(
            session,
            workspace_id,
            recent_source,
            recent_source_version,
            recent_target,
            authored_at=recent_time,
        )
        recent.occurred_at = old_time

    service = WorkspaceGraphService(cursor_secret="published-occurrence-secret")
    with session_factory() as session:
        recently_connected = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                lens="links",
                quick_filters=["recently_connected"],
            ),
            actor=_actor(),
        )
        visible_ids = {
            node.entity_id
            for node in recently_connected.nodes
            if node.grain == "document"
        }
        assert visible_ids == {recent_source.id, recent_target.id}
        assert recently_connected.generated_at == recent_time

        stale_window = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                lens="links",
                time={
                    "basis": "relation_observed",
                    "from": stale_projection_time - timedelta(days=1),
                    "to": stale_projection_time + timedelta(days=1),
                },
            ),
            actor=_actor(),
        )
        assert stale_window.nodes == []
        assert stale_window.edges == []
        # Freshness describes the authored visible universe, not mutable
        # projection metadata, even when the selected time window is empty.
        assert stale_window.generated_at == recent_time


def test_malformed_context_and_library_head_pointers_fail_closed(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        valid_context, _valid_head = _page(session, workspace_id, "valid-context")
        superseded_context, superseded_head = _page(
            session, workspace_id, "superseded-context"
        )
        superseded_head.version_status = "superseded_snapshot"

        draft_pointer, _draft_pointer_published = _page(
            session, workspace_id, "draft-pointer"
        )
        draft_head = ContextPageVersion(
            workspace_id=draft_pointer.workspace_id,
            id=uuid4(),
            page_id=draft_pointer.id,
            version_no=2,
            body_md="# Draft pointer",
            content_hash="malformed-draft-pointer",
            actor_type="human",
            actor_id=None,
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(draft_head)
        session.flush()
        draft_pointer.current_published_version_id = draft_head.id

        foreign_pointer, _foreign_pointer_head = _page(
            session, workspace_id, "foreign-pointer"
        )
        foreign_owner, foreign_owner_head = _page(
            session, workspace_id, "foreign-owner"
        )
        foreign_pointer.current_published_version_id = foreign_owner_head.id

        valid_library, _valid_library_head = _library(
            session, workspace_id, title="Valid library"
        )
        cross_item, _cross_item_own_head = _library(
            session, workspace_id, title="Cross-item pointer"
        )
        donor_item, donor_head = _library(session, workspace_id, title="Donor item")
        cross_item.current_version_id = donor_head.id

        other_workspace = Workspace(
            id=uuid4(),
            slug="malformed-head-other",
            title="Malformed Head Other",
            name="Malformed Head Other",
        )
        session.add(other_workspace)
        session.flush()
        _foreign_item, foreign_library_head = _library(
            session,
            other_workspace.id,
            title="Foreign workspace item",
        )
        cross_workspace, _cross_workspace_own_head = _library(
            session,
            workspace_id,
            title="Cross-workspace pointer",
        )
        cross_workspace.current_version_id = foreign_library_head.id

    with session_factory() as session:
        scene = WorkspaceGraphService(cursor_secret="malformed-head-secret").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        )
        visible_ids = {
            node.entity_id for node in scene.nodes if node.grain == "document"
        }
        assert valid_context.id in visible_ids
        assert superseded_context.id in visible_ids
        assert foreign_owner.id in visible_ids
        assert valid_library.id in visible_ids
        assert donor_item.id in visible_ids
        assert draft_pointer.id not in visible_ids
        assert foreign_pointer.id not in visible_ids
        assert cross_item.id not in visible_ids
        assert cross_workspace.id not in visible_ids


def test_highly_referenced_metric_stays_pre_quick_filter_authoritative(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hub, _hub_version = _page(session, workspace_id, "reference-hub")
        for index in range(3):
            source, source_version = _page(
                session, workspace_id, f"reference-source-{index}"
            )
            _link(session, workspace_id, source, source_version, hub)

    with session_factory() as session:
        scene = WorkspaceGraphService(cursor_secret="metric-secret").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                lens="links",
                quick_filters=["highly_referenced"],
            ),
            actor=_actor(),
        )
        assert scene.counts.candidate_documents == 1
        assert scene.counts.candidate_relations == 0
        hub_node = next(
            node
            for node in scene.nodes
            if node.grain == "document" and node.entity_id == hub.id
        )
        assert hub_node.reference_count == 3
        assert scene.metric_basis.endswith("pre_quick.v1")


def test_authored_relations_do_not_require_edge_projection(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, source_version = _page(session, workspace_id, "authored-source")
        target, _target_version = _page(session, workspace_id, "authored-target")
        library, library_version = _library(
            session,
            workspace_id,
            title="Authored evidence",
        )
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=source.id,
                source_version_id=source_version.id,
                target_slug=target.slug,
                target_page_id=target.id,
                occurrence_count=1,
            )
        )
        session.add(
            ContextCitation(
                workspace_id=library_version.workspace_id,
                target_kind="context_page_version",
                target_id=source_version.id,
                target_location={},
                library_item_version_id=library_version.id,
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="cite_tool",
                created_by_actor_type="human",
            )
        )

    service = WorkspaceGraphService(cursor_secret="authored-no-projection-secret")
    with session_factory() as session:
        links = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=_actor(),
        )
        evidence = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="evidence"),
            actor=_actor(),
        )
        assert links.completeness == "complete"
        assert {(edge.source, edge.target) for edge in links.edges} == {
            (
                f"document:context:{source.id}",
                f"document:context:{target.id}",
            )
        }
        assert evidence.completeness == "complete"
        assert {(edge.source, edge.target) for edge in evidence.edges} == {
            (
                f"document:context:{source.id}",
                f"document:library:{library.id}",
            )
        }


def test_graph_revision_is_filter_and_lens_independent(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, source_version = _page(session, workspace_id, "revision-source")
        target, _target_version = _page(session, workspace_id, "revision-target")
        _library_item, library_version = _library(
            session,
            workspace_id,
            title="Revision evidence",
        )
        folder = _folder(session, workspace_id, "Revision folder")
        _placement(session, workspace_id, source, folder)
        _link(session, workspace_id, source, source_version, target)
        _citation(session, workspace_id, source, source_version, library_version)

    service = WorkspaceGraphService(cursor_secret="stable-revision-secret")
    requests = [
        WorkspaceGraphOverviewRequest(lens="links"),
        WorkspaceGraphOverviewRequest(
            lens="evidence",
            document_kinds=["library"],
        ),
        WorkspaceGraphOverviewRequest(
            lens="directory",
            document_kinds=["context"],
            search="revision source",
            folders={"include": [folder.id]},
            quick_filters=["orphans"],
        ),
    ]
    with session_factory() as session:
        revisions = {
            service.query(
                session,
                workspace_id,
                request=request,
                actor=_actor(),
            ).graph_revision
            for request in requests
        }
    assert len(revisions) == 1


def test_graph_revision_changes_for_head_and_all_placement_metadata(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="metadata-revision-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, head = _page(session, workspace_id, "metadata-revision")
        primary_folder = _folder(session, workspace_id, "Primary folder")
        secondary_folder = _folder(session, workspace_id, "Secondary folder")
        _placement(session, workspace_id, page, primary_folder)
        _placement(
            session,
            workspace_id,
            page,
            secondary_folder,
            primary=False,
        )

    with session_factory() as session:
        initial = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        ).graph_revision

    with session_factory.begin() as session:
        session.get(WorkspaceTreeNode, primary_folder.id).title = "Primary renamed"
    with session_factory() as session:
        after_primary = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        ).graph_revision
    assert after_primary != initial

    with session_factory.begin() as session:
        session.get(WorkspaceTreeNode, secondary_folder.id).title = "Secondary renamed"
    with session_factory() as session:
        after_secondary = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        ).graph_revision
    assert after_secondary != after_primary

    with session_factory.begin() as session:
        session.get(ContextPageVersion, head.id).created_at += timedelta(seconds=1)
    with session_factory() as session:
        after_head_freshness = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor(),
        ).graph_revision
    assert after_head_freshness != after_secondary


def test_relations_fail_closed_when_heads_advance_after_capture(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="captured-head-secret")
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, captured_source_head = _page(
            session,
            workspace_id,
            "captured-source",
        )
        old_target, _old_target_head = _page(
            session,
            workspace_id,
            "captured-old-target",
        )
        new_target, new_target_head = _page(
            session,
            workspace_id,
            "captured-new-target",
        )
        library, _captured_library_head = _library(
            session,
            workspace_id,
            title="Captured library head",
        )
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=source.id,
                source_version_id=captured_source_head.id,
                target_slug=old_target.slug,
                target_page_id=old_target.id,
                occurrence_count=1,
            )
        )
        session.flush()

        captured_documents = service._load_documents(session, workspace_id, actor)
        service._attach_tree_placements(session, workspace_id, captured_documents)

        captured_source_head.version_status = "superseded_snapshot"
        new_source_head = ContextPageVersion(
            workspace_id=source.workspace_id,
            id=uuid4(),
            page_id=source.id,
            version_no=2,
            body_md="# Concurrent source",
            content_hash="captured-source-v2",
            actor_type="human",
            actor_id=None,
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(new_source_head)
        session.flush()
        source.current_published_version_id = new_source_head.id
        source.current_version_id = new_source_head.id
        session.add_all(
            [
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=source.id,
                    source_version_id=new_source_head.id,
                    target_slug=new_target.slug,
                    target_page_id=new_target.id,
                    occurrence_count=1,
                ),
                ContextCitation(
                    workspace_id=workspace_id,
                    target_kind="context_page_version",
                    target_id=new_source_head.id,
                    target_location={},
                    cited_context_page_version_id=new_target_head.id,
                    location={},
                    citation_type="context_page",
                    verification_status="verified",
                    created_via="cite_tool",
                    created_by_actor_type="human",
                ),
            ]
        )

        new_library_head = LibraryItemVersion(
            workspace_id=library.workspace_id,
            id=uuid4(),
            library_item_id=library.id,
            version_no=2,
            content_hash="captured-library-v2",
            raw_storage_ref=f"library/{library.id}/v2",
            processing_status="ready",
        )
        session.add(new_library_head)
        session.flush()
        library.current_version_id = new_library_head.id
        # Even though this citation is attached to the captured Context head,
        # its concurrently advanced Library endpoint is outside the capture.
        session.add(
            ContextCitation(
                workspace_id=new_library_head.workspace_id,
                target_kind="context_page_version",
                target_id=captured_source_head.id,
                target_location={},
                library_item_version_id=new_library_head.id,
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="cite_tool",
                created_by_actor_type="human",
            )
        )
        session.flush()

        relations = service._load_relation_universe(
            session,
            workspace_id,
            captured_documents,
        )

    assert captured_documents[("context", source.id)].head_id == (
        captured_source_head.id
    )
    assert captured_documents[("library", library.id)].head_id != new_library_head.id
    assert relations == []

    with session_factory() as session:
        fresh_documents = service._load_documents(session, workspace_id, actor)
        fresh_relations = service._load_relation_universe(
            session,
            workspace_id,
            fresh_documents,
        )
    assert {
        (relation.kind, relation.source, relation.target)
        for relation in fresh_relations
    } == {
        (
            "wikilink",
            ("context", source.id),
            ("context", new_target.id),
        ),
        (
            "citation_page",
            ("context", source.id),
            ("context", new_target.id),
        ),
    }


def test_library_relation_target_converges_after_captured_head_advances(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="captured-library-head-secret")
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, source_head = _page(
            session,
            workspace_id,
            "stable-library-source",
        )
        library, captured_library_head = _library(
            session,
            workspace_id,
            title="Advancing library target",
        )
        captured_documents = service._load_documents(session, workspace_id, actor)

        new_library_head = LibraryItemVersion(
            workspace_id=library.workspace_id,
            id=uuid4(),
            library_item_id=library.id,
            version_no=2,
            content_hash="advancing-library-v2",
            raw_storage_ref=f"library/{library.id}/v2",
            processing_status="ready",
        )
        session.add(new_library_head)
        session.flush()
        library.current_version_id = new_library_head.id
        session.add(
            ContextCitation(
                workspace_id=workspace_id,
                target_kind="context_page_version",
                target_id=source_head.id,
                target_location={},
                library_item_version_id=new_library_head.id,
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="cite_tool",
                created_by_actor_type="human",
            )
        )
        session.flush()

        captured_relations = service._load_relation_universe(
            session,
            workspace_id,
            captured_documents,
        )

    assert captured_documents[("library", library.id)].head_id == (
        captured_library_head.id
    )
    assert captured_relations == []

    with session_factory() as session:
        fresh_documents = service._load_documents(session, workspace_id, actor)
        fresh_relations = service._load_relation_universe(
            session,
            workspace_id,
            fresh_documents,
        )
    assert {
        (relation.kind, relation.source, relation.target)
        for relation in fresh_relations
    } == {
        (
            "citation_source",
            ("context", source.id),
            ("library", library.id),
        )
    }


def test_folder_facets_survive_apply_reopen_and_add_another_folder(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="folder-facet-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        folder_a = _folder(session, workspace_id, "Folder A")
        folder_b = _folder(session, workspace_id, "Folder B")
        page_a, _head_a = _page(session, workspace_id, "folder-a-page")
        page_b, _head_b = _page(session, workspace_id, "folder-b-page")
        _placement(session, workspace_id, page_a, folder_a)
        _placement(session, workspace_id, page_b, folder_b)

    with session_factory() as session:
        applied_a = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(folders={"include": [folder_a.id]}),
            actor=_actor(),
        )
        facet_a = {item.value: item.count for item in applied_a.facet_counts.folders}
        assert facet_a == {str(folder_a.id): 1, str(folder_b.id): 1}
        assert applied_a.counts.candidate_documents == 1

        applied_a_and_b = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                folders={"include": [folder_a.id, folder_b.id]}
            ),
            actor=_actor(),
        )
        facet_a_and_b = {
            item.value: item.count for item in applied_a_and_b.facet_counts.folders
        }
        assert facet_a_and_b == facet_a
        assert applied_a_and_b.counts.candidate_documents == 2
        assert applied_a_and_b.graph_revision == applied_a.graph_revision


def test_cluster_id_is_cached_across_relation_materialization(
    session_factory,
    monkeypatch,
):
    service = WorkspaceGraphService(cursor_secret="cluster-cache-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source, source_head = _page(session, workspace_id, "cluster-cache-source")
        target, _target_head = _page(session, workspace_id, "cluster-cache-target")
        _link(session, workspace_id, source, source_head, target)

    with session_factory() as session:
        documents = service._load_documents(session, workspace_id, _actor())
        relations = service._load_relation_universe(
            session,
            workspace_id,
            documents,
        )

    groups = [
        workspace_graph_module._Group(
            tuple(sorted(documents, key=workspace_graph_module._document_key_sort)),
            cluster_kind="other_cluster",
        )
    ]
    original_group_id = workspace_graph_module._group_id
    group_id_calls = 0

    def counted_group_id(*args, **kwargs):
        nonlocal group_id_calls
        group_id_calls += 1
        return original_group_id(*args, **kwargs)

    monkeypatch.setattr(workspace_graph_module, "_group_id", counted_group_id)
    rendered = service._materialize_groups(
        groups,
        documents,
        relations * 500,
        omission_reasons=[],
    )

    assert len(rendered.edges) == 1
    assert rendered.edges[0].relation_count == 500
    assert group_id_calls == 2


def test_privacy_twin_is_exact_after_1025_invisible_documents_and_edges(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="privacy-twin-secret")
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        visible, _visible_version = _page(session, workspace_id, "visible")
    with session_factory() as session:
        before = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=actor,
        ).model_dump(mode="json")

    with session_factory.begin() as session:
        for index in range(1025):
            hidden, hidden_version = _page(
                session,
                workspace_id,
                f"hidden-{index:04d}",
                published=False,
            )
            _placement(session, workspace_id, hidden, None)
            session.add(
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=hidden.id,
                    source_version_id=hidden_version.id,
                    target_slug=visible.slug,
                    target_page_id=visible.id,
                    occurrence_count=1,
                )
            )
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=hidden.id,
                    target_kind="context_page",
                    target_id=visible.id,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=1,
                    source_ref={"hidden": True},
                )
            )

    with session_factory() as session:
        after = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=actor,
        ).model_dump(mode="json")
    assert after == before


def test_dense_component_suppresses_infeasible_expansion_without_error(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="dense-expansion-secret")
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pages = [_page(session, workspace_id, f"dense-{index}") for index in range(6)]
        for source, source_head in pages:
            for target, _target_head in pages:
                if source.id == target.id:
                    continue
                session.add(
                    PageLink(
                        workspace_id=workspace_id,
                        source_page_id=source.id,
                        source_version_id=source_head.id,
                        target_slug=target.slug,
                        target_page_id=target.id,
                        occurrence_count=1,
                    )
                )

    request = WorkspaceGraphOverviewRequest(budget={"documents": 3, "relations": 1})
    with session_factory() as session:
        base = service.query(
            session,
            workspace_id,
            request=request,
            actor=actor,
        )
        cluster = next(node for node in base.nodes if node.grain == "cluster")
        assert len(base.nodes) <= 3
        assert len(base.edges) <= 1
        assert base.expansion_cursor is None
        assert base.capabilities.expand is False

        # A client may still submit the visible cluster id without a cursor.
        # The server must return the bounded aggregate and terminate, not turn
        # the infeasible reveal into a 4xx/5xx expansion loop.
        suppressed = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 3, "relations": 1},
                expansion={"cluster_id": cluster.id},
            ),
            actor=actor,
        )
        assert len(suppressed.nodes) <= 3
        assert len(suppressed.edges) <= 1
        assert suppressed.expansion_cursor is None
        assert suppressed.capabilities.expand is False
        assert sum(
            1 if node.grain == "document" else node.member_count
            for node in suppressed.nodes
        ) == len(pages)


def test_default_budget_expansion_adapts_reveal_window_to_relation_cap():
    service = WorkspaceGraphService(cursor_secret="default-expansion-secret")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    keys = sorted(
        [("context", uuid4()) for _index in range(251)],
        key=workspace_graph_module._document_key_sort,
    )
    documents = {
        key: workspace_graph_module._Document(
            key=key,
            entity_id=key[1],
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={key[1]}",
            head_id=uuid4(),
            updated_at=now,
        )
        for index, key in enumerate(keys)
    }
    # The first 40 members form a complete directed component. Full document
    # capacity would map 1,560 identities; the default 750-relation cap allows
    # exactly 26 exact prefix members plus the residual aggregate (702 edges).
    dense_keys = keys[:40]
    relations = [
        workspace_graph_module._Relation(
            source=source,
            target=target,
            kind="wikilink",
            layer="knowledge",
            occurred_at=now,
        )
        for source in dense_keys
        for target in dense_keys
        if source != target
    ]
    cluster = workspace_graph_module._Group(
        tuple(keys),
        cluster_kind="link_cluster",
    )

    candidate = service._expansion_candidate(
        [cluster],
        cluster,
        documents,
        relations,
        offset=0,
        document_budget=250,
        relation_budget=750,
        pinned=None,
    )

    assert candidate is not None
    expanded, revealed_count = candidate
    assert revealed_count == 26
    assert sum(group.is_document for group in expanded) == 26
    assert len(expanded) <= 250
    assert workspace_graph_module._mapped_relation_count(expanded, relations) <= 750


def test_aggregate_cursor_round_trip_is_bounded_bound_and_tamper_evident(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="cursor-secret")
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        for index in range(6):
            _page(session, workspace_id, f"isolated-{index}")

    request = WorkspaceGraphOverviewRequest(budget={"documents": 3, "relations": 10})
    with session_factory() as session:
        base = service.query(
            session,
            workspace_id,
            request=request,
            actor=actor,
        )
        assert base.representation == "aggregated"
        assert len(base.nodes) <= 3
        assert (
            sum(
                1 if node.grain == "document" else node.member_count
                for node in base.nodes
            )
            == 6
        )
        cluster = next(node for node in base.nodes if node.grain == "cluster")
        assert base.expansion_cursor is not None

        base_document_ids = {
            node.entity_id for node in base.nodes if node.grain == "document"
        }
        cursor = base.expansion_cursor
        revealed_cluster_members = set()
        for _index in range(cluster.member_count):
            assert cursor is not None
            expanded = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget={"documents": 3, "relations": 10},
                    expansion={"cluster_id": cluster.id, "cursor": cursor},
                ),
                actor=actor,
            )
            assert len(expanded.nodes) <= 3
            assert len(expanded.edges) <= 10
            assert (
                sum(
                    1 if node.grain == "document" else node.member_count
                    for node in expanded.nodes
                )
                == 6
            )
            revealed_cluster_members.update(
                node.entity_id
                for node in expanded.nodes
                if node.grain == "document" and node.entity_id not in base_document_ids
            )
            cursor = expanded.expansion_cursor
        assert len(revealed_cluster_members) == cluster.member_count
        assert cursor is None

        body, signature = base.expansion_cursor.split(".")
        equivalent_noncanonical_signature = _noncanonical_same_bytes(signature)
        bit_flipped_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered_variants = [
            # Same decoded signature bytes, different non-canonical text: this
            # is the regression a permissive base64url decoder accepted.
            f"{body}.{equivalent_noncanonical_signature}",
            # Padding is forbidden by the unpadded cursor wire format.
            f"{body}=.{signature}",
            f"{body}.{signature}=",
            # Standard base64 alphabet and whitespace are not base64url.
            f"{body}.{signature[:-1]}+",
            f"{body}.{signature[:-1]} ",
            # Canonical encoding with different bytes must fail HMAC.
            f"{body}.{bit_flipped_signature}",
        ]
        for tampered in tampered_variants:
            with pytest.raises(ReadValidationError, match="tampered"):
                service.query(
                    session,
                    workspace_id,
                    request=WorkspaceGraphOverviewRequest(
                        budget={"documents": 3, "relations": 10},
                        expansion={"cluster_id": cluster.id, "cursor": tampered},
                    ),
                    actor=actor,
                )
        with pytest.raises(ReadValidationError, match="actor"):
            service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget={"documents": 3, "relations": 10},
                    expansion={
                        "cluster_id": cluster.id,
                        "cursor": base.expansion_cursor,
                    },
                ),
                actor=_actor(actor_id="different-actor"),
            )


def _aggregated_focus_workspace(session_factory, count=251):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page_ids = [
            _page(session, workspace_id, f"focus-{index:03d}")[0].id
            for index in range(count)
        ]
        draft_id = _page(session, workspace_id, "focus-draft", published=False)[0].id
    return workspace_id, page_ids, draft_id


def test_focus_document_is_pinned_when_the_scene_is_aggregated(session_factory):
    """A Local-to-Workspace deep link selects its document in a capped scene."""

    service = WorkspaceGraphService(cursor_secret="focus-pin-secret")
    actor = _actor()
    workspace_id, page_ids, _draft_id = _aggregated_focus_workspace(session_factory)

    with session_factory() as session:
        baseline = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=actor,
        )
        rendered = {
            node.entity_id for node in baseline.nodes if node.grain == "document"
        }
        hidden = [page_id for page_id in page_ids if page_id not in rendered]
        assert baseline.representation == "aggregated"
        assert hidden

        for focus_id in (hidden[0], hidden[-1], page_ids[0]):
            scene = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(focus_entity_id=focus_id),
                actor=actor,
            )
            focused = next(
                node
                for node in scene.nodes
                if node.grain == "document" and node.entity_id == focus_id
            )
            assert scene.focus is not None
            assert scene.focus.status == "document"
            assert scene.focus.node_id == focused.id
            assert scene.counts.candidate_documents == len(page_ids)
            assert len(scene.nodes) <= 250
            assert len(scene.edges) <= 750


def test_focus_falls_back_to_its_cluster_when_no_slot_can_be_reserved(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="focus-cluster-secret")
    workspace_id, page_ids, _draft_id = _aggregated_focus_workspace(
        session_factory,
        count=5,
    )

    with session_factory() as session:
        scene = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 1, "relations": 1},
                focus_entity_id=page_ids[2],
            ),
            actor=_actor(),
        )

    (cluster,) = scene.nodes
    assert cluster.grain == "cluster"
    assert scene.focus is not None
    assert scene.focus.status == "cluster"
    assert scene.focus.node_id == cluster.id


def test_unavailable_focus_does_not_distinguish_unpublished_from_missing(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="focus-unavailable-secret")
    actor = _actor()
    workspace_id, _page_ids, draft_id = _aggregated_focus_workspace(
        session_factory,
        count=5,
    )

    with session_factory() as session:
        scenes = [
            service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(focus_entity_id=focus_id),
                actor=actor,
            )
            for focus_id in (draft_id, uuid4())
        ]
        unfocused = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=actor,
        )

    for scene in scenes:
        assert scene.focus is not None
        assert scene.focus.status == "unavailable"
        assert scene.focus.node_id is None
        assert scene.nodes == unfocused.nodes
        assert scene.edges == unfocused.edges
    assert unfocused.focus is None


def test_focus_is_pinned_when_the_relation_budget_forces_aggregation(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="focus-relation-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pages = [_page(session, workspace_id, f"chain-{index}") for index in range(12)]
        links = zip(pages, pages[1:], strict=False)
        for (source, source_head), (target, _head) in links:
            session.add(
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=source.id,
                    source_version_id=source_head.id,
                    target_slug=target.slug,
                    target_page_id=target.id,
                    occurrence_count=1,
                )
            )
    focus_id = pages[6][0].id

    with session_factory() as session:
        scene = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(
                budget={"documents": 250, "relations": 4},
                focus_entity_id=focus_id,
            ),
            actor=_actor(),
        )

    assert scene.representation == "aggregated"
    assert "relation_budget" in scene.omission_reasons
    assert scene.focus is not None
    assert scene.focus.status == "document"
    assert any(
        node.id == scene.focus.node_id and node.entity_id == focus_id
        for node in scene.nodes
        if node.grain == "document"
    )
    assert len(scene.edges) <= 4


def test_cluster_expansion_keeps_the_pinned_focus(session_factory):
    """Expanding a cluster never merges the pinned focus back into an aggregate.

    With eight unlinked pages and three slots the focused scene is one kept
    document, one cluster, and the focus. Expanding the cluster frees a slot
    by displacing another group, and which one it picks follows the members'
    digests, so every page takes a turn as the focus: a pass that only tried
    one page would pass or fail with the random page ids.
    """

    service = WorkspaceGraphService(cursor_secret="focus-expansion-secret")
    actor = _actor()
    workspace_id, page_ids, _draft_id = _aggregated_focus_workspace(
        session_factory,
        count=8,
    )
    budget = {"documents": 3, "relations": 10}

    with session_factory() as session:
        for focus_id in page_ids:
            base = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget=budget,
                    focus_entity_id=focus_id,
                ),
                actor=actor,
            )
            assert base.focus is not None
            assert base.focus.status == "document"
            cluster = next(node for node in base.nodes if node.grain == "cluster")
            assert base.expansion_cursor is not None

            expanded = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget=budget,
                    focus_entity_id=focus_id,
                    expansion={
                        "cluster_id": cluster.id,
                        "cursor": base.expansion_cursor,
                    },
                ),
                actor=actor,
            )

            # A cursor minted for the focused scene does not validate on a
            # scene grouped without the pin.
            with pytest.raises(ReadValidationError):
                service.query(
                    session,
                    workspace_id,
                    request=WorkspaceGraphOverviewRequest(
                        budget=budget,
                        expansion={
                            "cluster_id": cluster.id,
                            "cursor": base.expansion_cursor,
                        },
                    ),
                    actor=actor,
                )

            assert expanded.focus is not None
            assert expanded.focus.status == "document", focus_id
            assert any(
                node.id == expanded.focus.node_id and node.entity_id == focus_id
                for node in expanded.nodes
            )
            assert len(expanded.nodes) <= 3


@pytest.mark.parametrize("document_kind", ["context", "library", "library_uri"])
def test_database_long_titles_do_not_fail_the_whole_map(session_factory, document_kind):
    service = WorkspaceGraphService(cursor_secret="long-title-secret")
    label = "긴" * 201
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        ordinary, _head = _page(session, workspace_id, "ordinary-long-label-neighbor")
        if document_kind == "context":
            document, _head = _page(session, workspace_id, "long-label-context")
            document.title = label
        else:
            document, _head = _library(session, workspace_id, title=label)
            if document_kind == "library_uri":
                label = "https://example.test/" + "u" * 240
                document.title = None
                document.uri = label
        document_id = document.id
        ordinary_id = ordinary.id

    with session_factory() as session:
        payload = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(),
            actor=_actor("viewer"),
        )
    nodes = {node.entity_id: node for node in payload.nodes}
    assert set(nodes) == {document_id, ordinary_id}
    assert nodes[document_id].label == label[:200]
    assert payload.completeness == "complete"


def test_hidden_restricted_library_changes_do_not_affect_aggregated_viewer_map(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="aggregated-cohort-secret")
    request = WorkspaceGraphOverviewRequest(
        lens="compare", budget={"documents": 2, "relations": 10}
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pages = [
            _page(session, workspace_id, f"aggregate-visible-{index}")
            for index in range(4)
        ]
        for (source, head), (target, _target_head) in zip(
            pages, pages[1:], strict=False
        ):
            _link(session, workspace_id, source, head, target)
        item, item_head = _library(
            session, workspace_id, title="Hidden restricted label", restricted=True
        )
        _citation(session, workspace_id, pages[0][0], pages[0][1], item_head)
        session.flush()
        before_viewer = service.query(
            session, workspace_id, request=request, actor=_actor("viewer")
        )
        before_owner = service.query(
            session, workspace_id, request=request, actor=_actor("owner")
        )
        assert before_viewer.representation == "aggregated"
        assert before_owner.counts.candidate_documents == 5
        assert before_viewer.counts.candidate_documents == 4
        item.title = "RESTRICTED SENTINEL " + "s" * 240
        item_head.updated_at += timedelta(days=1)
        session.flush()
        after_viewer = service.query(
            session, workspace_id, request=request, actor=_actor("viewer")
        )
        after_owner = service.query(
            session, workspace_id, request=request, actor=_actor("owner")
        )

    assert after_viewer.model_dump_json() == before_viewer.model_dump_json()
    assert "RESTRICTED SENTINEL" not in after_viewer.model_dump_json()
    assert before_owner.graph_revision != after_owner.graph_revision


@pytest.mark.parametrize(
    "document_count,relation_count", [(2_000, 6_000), (2_001, 6_000), (2_000, 6_001)]
)
def test_map_api_keeps_meaningful_scenes_at_both_2k_boundaries(
    page_service, document_count, relation_count
):
    from uuid import UUID

    from backend.tests.integration.test_workspace_graph_api import _client

    client, workspace_text = _client(page_service, enabled=True)
    workspace_id = UUID(workspace_text)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with page_service.session_factory.begin() as session:
        pages = [
            ContextPage(
                id=UUID(int=index + 1),
                workspace_id=workspace_id,
                slug=f"boundary-doc-{index}",
                title=f"Boundary document {index}",
                page_type="concept",
                lifecycle_status="published",
            )
            for index in range(document_count)
        ]
        session.add_all(pages)
        session.flush()
        heads = [
            ContextPageVersion(
                id=UUID(int=document_count + index + 1),
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md="Boundary document",
                content_hash=hashlib.sha256(b"Boundary document").hexdigest(),
                actor_type="human",
                actor_id=None,
                created_by_actor_kind="human",
                created_by_actor_id="boundary-test",
                version_status="published_snapshot",
                created_at=now,
            )
            for index, page in enumerate(pages)
        ]
        session.add_all(heads)
        session.flush()
        for page, head in zip(pages, heads, strict=True):
            page.current_published_version_id = head.id
            page.current_version_id = head.id
        session.add_all(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=pages[index % document_count].id,
                source_version_id=heads[index % document_count].id,
                target_slug=pages[
                    (index % document_count + index // document_count + 1)
                    % document_count
                ].slug,
                target_page_id=pages[
                    (index % document_count + index // document_count + 1)
                    % document_count
                ].id,
                link_syntax="wikilink",
                occurrence_count=1,
                created_at=now,
            )
            for index in range(relation_count)
        )

    route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
    body = {"budget": {"documents": 2_000, "relations": 6_000}}
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    scene = response.json()["data"]
    assert scene["counts"]["candidate_documents"] == document_count
    assert scene["counts"]["candidate_relations"] == relation_count
    assert scene["counts"]["rendered_documents"] >= 1_990
    assert len(scene["nodes"]) <= 2_000
    assert 5_800 < len(scene["edges"]) <= 6_000
    assert sum(edge["relation_count"] for edge in scene["edges"]) == relation_count
    assert (
        sum(
            1 if node["grain"] == "document" else node["member_count"]
            for node in scene["nodes"]
        )
        == document_count
    )

    # A valid document title beyond the map's wire-label bound must remain
    # visible through this same public route instead of turning the map 500.
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, UUID(int=1))
        page.title = "긴" * 201
    long_title_response = client.post(route, json=body)
    assert long_title_response.status_code == 200, long_title_response.text
    assert (
        long_title_response.json()["data"]["counts"]["candidate_documents"]
        == document_count
    )
    long_node = next(
        node
        for node in long_title_response.json()["data"]["nodes"]
        if node["grain"] == "document" and node["entity_id"] == str(UUID(int=1))
    )
    assert long_node["label"] == "긴" * 200


@pytest.mark.parametrize("search", ["target", "context", "library", "on", "text"])
def test_map_search_does_not_match_navigation_route_words(session_factory, search):
    service = WorkspaceGraphService(cursor_secret="search-route-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _page(session, workspace_id, "alpha")
        _page(session, workspace_id, "beta")
        _library(session, workspace_id, title="Paper")
    with session_factory() as session:
        payload = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(search=search),
            actor=_actor(),
        )
    assert payload.counts.candidate_documents == 0
    assert payload.nodes == []


def test_map_search_matches_full_titles_and_primary_visible_placement_path(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="search-visible-path-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        folder = _folder(session, workspace_id, "Research Delta")
        page, _head = _page(session, workspace_id, "alpha")
        item, _head = _library(session, workspace_id, title="Target Context Library")
        _placement(session, workspace_id, page, folder)
        page.title = "A" * 200 + "FullTitleTail"
        page_id, item_id = page.id, item.id
    with session_factory() as session:
        for search, expected in (
            ("research delta", {page_id}),
            ("Alpha", {page_id}),
            ("FullTitleTail", {page_id}),
            ("target", {item_id}),
            ("context", {item_id}),
            ("library", {item_id}),
        ):
            payload = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(search=search),
                actor=_actor(),
            )
            assert {node.entity_id for node in payload.nodes} == expected, search
            if search == "FullTitleTail":
                assert payload.nodes[0].label == "A" * 200


def test_map_search_ignores_navigation_uuid_but_matches_uuid_in_real_title(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="search-uuid-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, _head = _page(session, workspace_id, "alpha")
        page_id = page.id
    request = WorkspaceGraphOverviewRequest(search=str(page_id))
    with session_factory() as session:
        before = service.query(session, workspace_id, request=request, actor=_actor())
    assert before.nodes == []
    with session_factory.begin() as session:
        session.get(ContextPage, page_id).title = f"A real identifier {page_id}"
    with session_factory() as session:
        after = service.query(session, workspace_id, request=request, actor=_actor())
    assert {node.entity_id for node in after.nodes} == {page_id}


def test_map_search_uses_no_hidden_ancestor_or_secondary_placement_metadata(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="search-placement-privacy-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        visible_folder = _folder(session, workspace_id, "Visible Primary")
        secondary_folder = _folder(session, workspace_id, "Secondary Secret Marker")
        hidden_folder = _folder(session, workspace_id, "Hidden Ancestor Marker")
        hidden_folder.visibility_status = "hidden"
        primary_page, _head = _page(session, workspace_id, "alpha")
        hidden_chain_page, _head = _page(session, workspace_id, "beta")
        _placement(session, workspace_id, primary_page, visible_folder)
        _placement(session, workspace_id, primary_page, secondary_folder, primary=False)
        _placement(session, workspace_id, hidden_chain_page, hidden_folder)
        primary_id = primary_page.id
    with session_factory() as session:
        for search in ("Secondary Secret Marker", "Hidden Ancestor Marker"):
            payload = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(search=search),
                actor=_actor(),
            )
            assert payload.nodes == [], search
            assert payload.facet_counts.folders == []
        visible = service.query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(search="Visible Primary"),
            actor=_actor(),
        )
    assert {node.entity_id for node in visible.nodes} == {primary_id}


def _unplaced_folder_fixture(session, workspace_id):
    ancestor = _folder(session, workspace_id, "Visible ancestor")
    nearest = _folder(session, workspace_id, "Visible nearest")
    nearest.parent_id = ancestor.id
    hidden = _folder(session, workspace_id, "Hidden folder")
    hidden.visibility_status = "hidden"
    pages = {
        name: _page(session, workspace_id, f"unplaced-{name}")[0]
        for name in ("folder", "root", "none", "hidden")
    }
    _placement(session, workspace_id, pages["folder"], nearest)
    _placement(session, workspace_id, pages["root"], None)
    hidden_placement = _placement(session, workspace_id, pages["hidden"], hidden)
    return {
        "ancestor": ancestor.id,
        "nearest": nearest.id,
        "hidden_folder": hidden.id,
        "hidden_placement": hidden_placement.id,
        "pages": {name: page.id for name, page in pages.items()},
    }


def test_unplaced_filter_matches_root_missing_and_hidden_primary_folders(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="unplaced-visible-secret")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        fixture = _unplaced_folder_fixture(session, workspace_id)
    folder_page = fixture["pages"]["folder"]
    unplaced = set(fixture["pages"].values()) - {folder_page}
    controls = (
        ({"include": ["unplaced"]}, unplaced),
        ({"exclude": ["unplaced"]}, {folder_page}),
        ({"only": ["unplaced"]}, unplaced),
        ({"include": [fixture["ancestor"]]}, {folder_page}),
        ({"include": [fixture["nearest"], "unplaced"]}, set(fixture["pages"].values())),
        ({"only": [fixture["ancestor"], "unplaced"]}, set(fixture["pages"].values())),
        ({"include": [fixture["ancestor"]], "exclude": ["unplaced"]}, {folder_page}),
        ({"include": ["unplaced"], "exclude": [fixture["nearest"]]}, unplaced),
    )
    with session_factory() as session:
        for folders, expected in controls:
            payload = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(folders=folders),
                actor=_actor(),
            )
            assert {node.entity_id for node in payload.nodes} == expected, folders
            assert {
                facet.value: facet.count for facet in payload.facet_counts.folders
            } == {
                str(fixture["nearest"]): 1,
                "unplaced": 3,
            }


def test_unplaced_filter_hidden_and_foreign_folder_twin_remains_byte_exact(
    session_factory,
):
    service = WorkspaceGraphService(cursor_secret="unplaced-privacy-secret")
    request = WorkspaceGraphOverviewRequest(folders={"only": ["unplaced"]})
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        fixture = _unplaced_folder_fixture(session, workspace_id)
        session.flush()
        before = service.query(session, workspace_id, request=request, actor=_actor())
        session.get(
            WorkspaceTreeNode, fixture["hidden_folder"]
        ).title = "HIDDEN FOLDER SENTINEL"
        session.get(
            WorkspaceTreeNode, fixture["hidden_placement"]
        ).title = "HIDDEN PLACEMENT SENTINEL"
        _library(session, workspace_id, title="RESTRICTED SENTINEL", restricted=True)
        foreign_workspace = Workspace(
            id=uuid4(), slug="unplaced-foreign", title="Foreign", name="Foreign"
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_folder = _folder(
            session, foreign_workspace.id, "FOREIGN FOLDER SENTINEL"
        )
        foreign_page, _head = _page(session, foreign_workspace.id, "foreign-page")
        _placement(session, foreign_workspace.id, foreign_page, foreign_folder)
        session.flush()
        after = service.query(session, workspace_id, request=request, actor=_actor())
        for foreign_or_hidden in (foreign_folder.id, fixture["hidden_folder"]):
            unavailable = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    folders={"only": [foreign_or_hidden]}
                ),
                actor=_actor(),
            )
            assert unavailable.nodes == []
            mixed = service.query(
                session,
                workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    folders={"only": ["unplaced", foreign_or_hidden]}
                ),
                actor=_actor(),
            )
            assert mixed.model_dump_json() == after.model_dump_json()
    assert after.model_dump_json() == before.model_dump_json()
    assert "SENTINEL" not in after.model_dump_json()
