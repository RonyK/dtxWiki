from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from backend.models.agent import AgentPage
from backend.models.audit import AuditEvent
from backend.models.change import ChangeSet
from backend.models.context import ContextPage
from backend.models.library import LibraryItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.agent import AgentPageDraftRequest
from backend.schemas.context_write import (
    CitationInput,
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.schemas.tree import TreeFolderCreateRequest, TreeNodeUpdate
from backend.services.agent_page_service import AgentPageService
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_service import ReviewService
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import (
    TreeService,
    ensure_context_inbox_node,
    ensure_primary_tree_placement,
)

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _primary_node(session, workspace_id, target_kind, target_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.target_kind == target_kind,
            WorkspaceTreeNode.target_id == target_id,
            WorkspaceTreeNode.primary_placement.is_(True),
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )


def test_tree_node_id_is_opaque_across_workspaces_without_mutation(session_factory):
    service = TreeService()

    with session_factory.begin() as session:
        requested_workspace = _workspace(session)
        foreign_workspace = Workspace(
            slug="foreign-tree-node",
            title="Foreign Tree Node",
            name="Foreign Tree Node",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_node = WorkspaceTreeNode(
            workspace_id=foreign_workspace.id,
            section="context",
            node_kind="folder",
            parent_id=None,
            sort_key="0001",
            title="Foreign node title must stay opaque",
            slug="foreign-tree-node",
            visibility_status="visible",
        )
        session.add(foreign_node)
        session.flush()
        counts_before = (
            session.scalar(select(func.count()).select_from(ChangeSet)),
            session.scalar(select(func.count()).select_from(AuditEvent)),
        )

        with pytest.raises(ReadNotFoundError) as missing_error:
            service.update_node(
                session=session,
                workspace_id=requested_workspace.id,
                node_id=uuid4(),
                request=TreeNodeUpdate(display_name="Must not apply"),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        with pytest.raises(ReadNotFoundError) as foreign_error:
            service.update_node(
                session=session,
                workspace_id=requested_workspace.id,
                node_id=foreign_node.id,
                request=TreeNodeUpdate(display_name="Must not apply"),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        counts_after = (
            session.scalar(select(func.count()).select_from(ChangeSet)),
            session.scalar(select(func.count()).select_from(AuditEvent)),
        )

    assert str(foreign_error.value) == str(missing_error.value)
    assert str(foreign_workspace.id) not in str(foreign_error.value)
    assert foreign_node.title == "Foreign node title must stay opaque"
    assert counts_after == counts_before


def test_context_page_draft_creates_primary_tree_node(
    session_factory, media_installer_factory
):
    writer = ContextWriteService(media_installer_factory=media_installer_factory)
    reader = TreeReadService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/tree-context-source",
            title="Tree Context Source",
            raw_text="Tree placement source evidence.",
            actor_type="human",
        )
        result = writer.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Tree Context Draft",
                page_type="overview",
                body_markdown="# Tree Context Draft\n\nVerified.",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=registration.version.id,
                        snippet="Tree placement source evidence.",
                    )
                ],
                reason="create context tree placement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        node = _primary_node(session, workspace.id, "context_page", result.page_id)
        path = reader.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="context_page",
            target_id=result.page_id,
        )

    assert node is not None
    assert node.node_kind == "page_ref"
    assert node.section == "context"
    assert node.can_have_children is True
    assert node.parent_id is not None
    assert [entry.slug for entry in path] == [
        "context",
        "inbox",
        "tree-context-draft",
    ]


def test_missing_context_inbox_is_created_once_and_receives_mcp_drafts(
    session_factory,
):
    writer = ContextWriteService()

    with session_factory.begin() as session:
        workspace = Workspace(
            slug="missing-inbox",
            title="Missing Inbox",
            name="Missing Inbox",
        )
        session.add(workspace)
        session.flush()
        root = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            parent_id=None,
            sort_key="0001",
            title="Context",
            slug="context",
            is_system_node=True,
            can_have_children=True,
            primary_placement=True,
            visibility_status="visible",
            created_by_actor_type="system",
            created_by_actor_id=UUID(ACTOR_ID),
        )
        session.add(root)
        session.flush()

        created = writer.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Lazy Inbox Draft",
                page_type="overview",
                body_markdown="# Lazy Inbox Draft\n\nBody.",
                reason="verify lazy Inbox placement",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        first_inbox = ensure_context_inbox_node(session, workspace.id)
        second_inbox = ensure_context_inbox_node(session, workspace.id)
        placement = _primary_node(
            session,
            workspace.id,
            "context_page",
            created.page_id,
        )
        inbox_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceTreeNode)
            .where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.parent_id == root.id,
                WorkspaceTreeNode.node_kind == "folder",
                WorkspaceTreeNode.slug == "inbox",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )

    assert first_inbox.id == second_inbox.id
    assert first_inbox.title == "Inbox"
    assert first_inbox.is_system_node is True
    assert first_inbox.can_have_children is True
    assert inbox_count == 1
    assert placement is not None
    assert placement.parent_id == first_inbox.id


def test_context_mcp_draft_explicit_placement_wins(session_factory):
    writer = ContextWriteService()
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        inbox = ensure_context_inbox_node(session, workspace.id)
        folder = tree_service.create_folder(
            session=session,
            workspace_id=workspace.id,
            request=TreeFolderCreateRequest(
                parent_id=inbox.id,
                section="context",
                title="Selected Parent",
                slug="selected-parent",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        created = writer.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Explicitly Placed Draft",
                page_type="overview",
                body_markdown="# Explicitly Placed Draft\n\nBody.",
                reason="verify explicit placement wins",
                placement_parent_node_id=folder.id,
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        placement = _primary_node(
            session,
            workspace.id,
            "context_page",
            created.page_id,
        )
        assert placement is not None
        page = session.get(ContextPage, created.page_id)
        assert page is not None
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        # Approving publishes the draft, which is the path that re-asserts
        # placement. Calling publish_draft again afterwards would hit the
        # already-published replay branch, not the re-assert being tested.
        placement = _primary_node(
            session,
            workspace.id,
            "context_page",
            created.page_id,
        )

    assert placement is not None
    assert placement.parent_id == folder.id


def test_context_mcp_draft_invalid_placement_rolls_back_with_same_error(
    session_factory,
):
    writer = ContextWriteService()
    missing_parent_id = uuid4()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        with pytest.raises(ReadNotFoundError, match="tree node not found") as exc:
            writer.write_draft(
                session=session,
                workspace_id=workspace.id,
                request=WriteDraftRequest(
                    title="Invalid Placement Draft",
                    page_type="overview",
                    body_markdown="# Invalid Placement Draft\n\nBody.",
                    reason="verify invalid placement rollback",
                    placement_parent_node_id=missing_parent_id,
                ),
                actor_kind="mcp_client",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
        leaked_page_count = session.scalar(
            select(func.count())
            .select_from(ContextPage)
            .where(
                ContextPage.workspace_id == workspace.id,
                ContextPage.slug == "invalid-placement-draft",
            )
        )

    assert exc.value.error_code == "not_found"
    assert leaked_page_count == 0


def test_agent_page_draft_creates_category_primary_tree_node(session_factory):
    service = AgentPageService()
    reader = TreeReadService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Tree Persona",
                slug="tree-persona",
                body_markdown="# Tree Persona\n\nUse the tree.",
                reason="create agent tree placement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

        node = _primary_node(session, workspace.id, "agent_page", result.agent_page_id)
        path = reader.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=result.agent_page_id,
        )

    assert node is not None
    assert node.node_kind == "page_ref"
    assert node.section == "agent"
    assert node.can_have_children is True
    assert [entry.slug for entry in path] == ["agent", "persona", "tree-persona"]


def test_agent_page_draft_honors_requested_parent_node(session_factory):
    service = AgentPageService()
    tree_service = TreeService()
    reader = TreeReadService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        persona_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "agent",
                WorkspaceTreeNode.slug == "persona",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        assert persona_root is not None
        folder = tree_service.create_folder(
            session=session,
            workspace_id=workspace.id,
            request=TreeFolderCreateRequest(
                parent_id=persona_root.id,
                section="agent",
                title="Research Agents",
                slug="research-agents",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Nested Persona",
                slug="nested-persona",
                body_markdown="# Nested Persona\n\nUse the selected folder.",
                parent_node_id=folder.id,
                reason="create agent page under selected folder",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )

        node = _primary_node(session, workspace.id, "agent_page", result.agent_page_id)
        path = reader.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=result.agent_page_id,
        )

    assert node is not None
    assert node.parent_id == folder.id
    assert [entry.slug for entry in path] == [
        "agent",
        "persona",
        "research-agents",
        "nested-persona",
    ]


def test_tree_rename_of_context_page_node_syncs_page_title(
    session_factory, media_installer_factory
):
    writer = ContextWriteService(media_installer_factory=media_installer_factory)
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/tree-rename-context-source",
            title="Tree Rename Context Source",
            raw_text="Tree rename source evidence.",
            actor_type="human",
        )
        result = writer.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Tree Rename Draft",
                page_type="overview",
                body_markdown="# Tree Rename Draft\n\nVerified.",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=registration.version.id,
                        snippet="Tree rename source evidence.",
                    )
                ],
                reason="create context page for tree rename",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        node = _primary_node(session, workspace.id, "context_page", result.page_id)
        revision_before = session.scalar(
            select(Workspace.edge_projection_version).where(
                Workspace.id == workspace.id
            )
        )
        renamed = tree_service.update_node(
            session=session,
            workspace_id=workspace.id,
            node_id=node.id,
            request=TreeNodeUpdate(display_name="Renamed Context Label"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, result.page_id)
        assert renamed.title == "Renamed Context Label"
        assert page.title == "Renamed Context Label"
        assert (
            session.scalar(
                select(Workspace.edge_projection_version).where(
                    Workspace.id == workspace.id
                )
            )
            == revision_before
        )

        # The next write-path placement sync copies page.title back onto the
        # node; the rename must survive it instead of silently reverting.
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace.id,
            target_kind="context_page",
            target_id=result.page_id,
            title=page.title,
            slug=page.slug,
            section="context",
            parent_id=None,
            default_parent_slug="inbox",
            actor_kind="human",
            actor_id=ACTOR_ID,
            can_have_children=True,
        )
        refreshed = _primary_node(session, workspace.id, "context_page", result.page_id)

    assert refreshed.title == "Renamed Context Label"


def test_tree_rename_of_agent_page_node_syncs_agent_title(session_factory):
    service = AgentPageService()
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Rename Persona",
                slug="rename-persona",
                body_markdown="# Rename Persona\n\nUse the tree.",
                reason="create agent page for tree rename",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        node = _primary_node(session, workspace.id, "agent_page", result.agent_page_id)
        tree_service.update_node(
            session=session,
            workspace_id=workspace.id,
            node_id=node.id,
            request=TreeNodeUpdate(display_name="Renamed Persona Label"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        agent_page = session.get(AgentPage, result.agent_page_id)

        assert agent_page.title == "Renamed Persona Label"
        [indexed] = (
            session.execute(
                text(
                    "SELECT context_header, body FROM search_doc_fts "
                    "WHERE source_kind = 'agent_page' AND entity_id = :entity_id"
                ),
                {"entity_id": str(agent_page.id)},
            )
            .mappings()
            .all()
        )
        assert "Title: Renamed Persona Label" in indexed["context_header"]
        assert indexed["body"] == "# Rename Persona\n\nUse the tree."


def test_source_and_artifact_create_primary_tree_nodes(
    session_factory, media_installer_factory
):
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )
    reader = TreeReadService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/tree-source",
            title="Tree Source",
            raw_text="Tree source body.",
            actor_type="human",
        )
        artifact = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
            raw_text="Tree source body.",
            actor_type="human",
        )
        source = session.get(LibraryItem, registration.library_item.id)
        source_node = _primary_node(session, workspace.id, "library_item", source.id)
        artifact_node = _primary_node(
            session,
            workspace.id,
            "library_artifact",
            artifact.id,
        )
        source_path = reader.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="library_item",
            target_id=source.id,
        )
        artifact_path = reader.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="library_artifact",
            target_id=artifact.id,
        )

    assert source_node is not None
    assert source_node.node_kind == "library_item_ref"
    assert source_node.can_have_children is True
    assert artifact_node is not None
    assert artifact_node.node_kind == "library_artifact_ref"
    assert artifact_node.parent_id == source_node.id
    assert artifact_node.can_have_children is False
    assert [entry.slug for entry in source_path] == ["library", None]
    assert [entry.slug for entry in artifact_path] == ["library", None, None]
