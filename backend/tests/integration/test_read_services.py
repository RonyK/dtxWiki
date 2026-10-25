from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.base import Base
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.feedback import FeedbackItem
from backend.models.library import LibraryItem, LibraryItemTombstone
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.policy.auth import ActorContext
from backend.schemas.read import WorkspaceCreateRequest
from backend.schemas.tree import TreeFolderCreateRequest
from backend.scripts.seed_dev import (
    DEFAULT_AGENT_PAGE_SLUG,
    seed_default_workspace,
)
from backend.services.domain_read import DomainReadService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService
from backend.services.read_errors import (
    ReadNotFoundError,
    RejectedByPolicyError,
    ScopeDeniedError,
)
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import TreeService
from backend.services.workspace_read import WorkspaceReadService
from backend.services.workspace_write import WorkspaceWriteService


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _system_actor(workspace_id):
    return ActorContext(
        actor_kind="system",
        actor_id="test-system",
        scopes=("*",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def _viewer_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="viewer-user",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )


def test_restricted_library_tombstones_follow_actor_visibility():
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add_all(
            [
                LibraryItemTombstone(
                    workspace_id=workspace.id,
                    library_item_id=uuid4(),
                    uri="https://example.com/trusted-deleted",
                    title="Trusted deleted source",
                    trust_level_snapshot="trusted",
                ),
                LibraryItemTombstone(
                    workspace_id=workspace.id,
                    library_item_id=uuid4(),
                    uri="https://example.com/restricted-deleted",
                    title="Restricted deleted source",
                    trust_level_snapshot="restricted",
                ),
            ]
        )
        workspace_id = workspace.id

    with session_factory() as session:
        viewer_rows = library_service.list_tombstones(
            session,
            workspace_id,
            actor=_viewer_actor(workspace_id),
        )
        owner_rows = library_service.list_tombstones(
            session,
            workspace_id,
            actor=_system_actor(workspace_id),
        )

    assert {row["title"] for row in viewer_rows} == {"Trusted deleted source"}
    assert {row["title"] for row in owner_rows} == {
        "Trusted deleted source",
        "Restricted deleted source",
    }


def _editor_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="editor-user",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )


def test_restricted_library_item_direct_id_mutation_hidden_from_editor():
    # H-03: an editor cannot read a restricted Library item, so its references
    # lookup and delete must 404 for the editor exactly like GET, while an
    # owner/system actor still operates on it.
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        [item] = _seed_library_items(session, workspace.id, 1, trust_level="restricted")
        workspace_id = workspace.id
        item_id = item.id

    editor = _editor_actor(workspace_id)
    with session_factory.begin() as session:
        with pytest.raises(ReadNotFoundError):
            library_service.list_item_references(
                session, workspace_id, item_id, actor=editor
            )
        with pytest.raises(ReadNotFoundError):
            library_service.delete_library_item(
                session,
                workspace_id,
                item_id,
                actor_kind="human",
                actor_id="editor-user",
                actor=editor,
            )

    # An owner-level actor still resolves the references (no accidental lockout).
    with session_factory() as session:
        refs = library_service.list_item_references(
            session, workspace_id, item_id, actor=_system_actor(workspace_id)
        )
        assert refs == []


def test_sensitive_agent_tree_node_archive_hidden_from_editor():
    # H-03: an editor cannot read a sensitive AgentPage tree node (GET 404s),
    # so archiving it by direct id must 404 too, while an owner still can.
    from backend.services.tree_service import (
        TreeService,
        ensure_primary_tree_placement,
    )

    session_factory = make_session_factory()
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Sensitive Persona",
            slug="sensitive-persona",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(agent)
        session.flush()
        version = AgentPageVersion(
            workspace_id=agent.workspace_id,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        agent.current_active_version_id = version.id
        node = ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent.id,
            title=agent.title,
            section="agent",
            parent_id=None,
            default_parent_slug="agent",
            actor_kind="human",
            actor_id="owner",
        )
        workspace_id = workspace.id
        node_id = node.id

    with session_factory.begin() as session:
        with pytest.raises(ReadNotFoundError):
            tree_service.archive_node(
                session,
                workspace_id,
                node_id,
                actor_kind="human",
                actor_id="editor-user",
                actor=_editor_actor(workspace_id),
            )

    # An owner-level actor can still archive it.
    with session_factory.begin() as session:
        tree_service.archive_node(
            session,
            workspace_id,
            node_id,
            actor_kind="human",
            actor_id="owner",
            actor=_system_actor(workspace_id),
        )


def test_folder_archive_refused_when_subtree_has_hidden_node():
    # H-03 (cascade): archiving a folder cascades to its whole subtree. An editor
    # who cannot see a sensitive AgentPage node placed under a visible folder must
    # be refused, so the cascade cannot archive the hidden node.
    from backend.services.tree_service import (
        TreeService,
        create_tree_node,
        ensure_primary_tree_placement,
    )

    session_factory = make_session_factory()
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        agent_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "agent",
                WorkspaceTreeNode.slug == "agent",
            )
        )
        folder = create_tree_node(
            session,
            workspace_id=workspace.id,
            section="agent",
            node_kind="folder",
            parent_id=agent_root.id,
            title="Mixed Folder",
            slug="mixed-folder",
            actor_kind="human",
            actor_id="owner",
        )
        agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Hidden Under Folder",
            slug="hidden-under-folder",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(agent)
        session.flush()
        version = AgentPageVersion(
            workspace_id=agent.workspace_id,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Hidden",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        agent.current_active_version_id = version.id
        placed = ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent.id,
            title=agent.title,
            section="agent",
            parent_id=folder.id,
            default_parent_slug="agent",
            actor_kind="human",
            actor_id="owner",
        )
        placed.parent_id = folder.id
        session.flush()
        workspace_id = workspace.id
        folder_id = folder.id

    with session_factory.begin() as session:
        with pytest.raises(RejectedByPolicyError):
            tree_service.archive_node(
                session,
                workspace_id,
                folder_id,
                actor_kind="human",
                actor_id="editor-user",
                actor=_editor_actor(workspace_id),
            )

    # An owner can archive the whole subtree.
    with session_factory.begin() as session:
        tree_service.archive_node(
            session,
            workspace_id,
            folder_id,
            actor_kind="human",
            actor_id="owner",
            actor=_system_actor(workspace_id),
        )


def test_audit_tree_node_target_gated_for_editor():
    # M-01 (Codex follow-up): a tree audit event whose target is a sensitive
    # AgentPage node must be gated for an editor, so the row's summary/title/id
    # are redacted (not just the resolved model title).
    from backend.models.audit import AuditEvent
    from backend.services.activity_service import _audit_target_visible
    from backend.services.tree_service import ensure_primary_tree_placement

    session_factory = make_session_factory()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Audit Sensitive Persona",
            slug="audit-sensitive-persona",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(agent)
        session.flush()
        version = AgentPageVersion(
            workspace_id=agent.workspace_id,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        agent.current_active_version_id = version.id
        node = ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent.id,
            title=agent.title,
            section="agent",
            parent_id=None,
            default_parent_slug="agent",
            actor_kind="human",
            actor_id="owner",
        )
        event = AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="owner",
            action="tree.rename",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={"target_title": "Renamed Secret"},
        )
        session.add(event)
        session.flush()
        workspace_id = workspace.id

        assert (
            _audit_target_visible(session, event, _editor_actor(workspace_id)) is False
        )
        assert (
            _audit_target_visible(session, event, _system_actor(workspace_id)) is True
        )


def test_audit_dangling_sensitive_target_fails_closed():
    # M-01 (Codex round-2): a deleted sensitivity-bearing target must fail CLOSED
    # — the stored event_summary/payload title still holds its snapshot name.
    from backend.models.audit import AuditEvent
    from backend.services.activity_service import _audit_target_visible

    session_factory = make_session_factory()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        missing_page_id = uuid4()
        event = AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="owner",
            action="context_page.updated",
            target_kind="context_page",
            target_id=missing_page_id,
            payload={"target_title": "Deleted Secret Page"},
        )
        session.add(event)
        session.flush()
        # Even an owner does not get a name we cannot re-authorize.
        assert (
            _audit_target_visible(session, event, _system_actor(workspace.id)) is False
        )


def test_workspace_read_service_lists_details_and_guide_payload():
    session_factory = make_session_factory()
    workspace_service = WorkspaceReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        listed = workspace_service.list_workspaces(session=session)
        detail = workspace_service.get_workspace(
            session=session,
            workspace_id=workspace.id,
        )
        guide = workspace_service.guide(
            session=session,
            workspace_id=workspace.id,
            detail_level="brief",
        )

    assert listed.current_workspace_id == workspace.id
    assert [item.slug for item in listed.workspaces] == ["default"]
    assert detail.library_policy["manual_approval_required"] is True
    assert detail.agent_injection_policy["bundle_ttl_default_seconds"] == 3600
    assert guide.workspace_summary["slug"] == "default"
    assert guide.library_policy_summary["manual_approval_required"] is True
    assert guide.available_tools == [
        "guide",
        "search",
        "fetch",
        "grep_wiki",
        "read",
        "list_agent_categories",
    ]
    assert guide.required_workflow == [
        "guide",
        "search or get_index",
        "fetch a search result, or use read_concept/read for direct navigation",
        "cite before a sourced write",
        (
            "Link to another dtxWiki document with the result's link_markdown. "
            "For a Context page input, pass its stable_url or page_id unchanged. "
            "Never use concept_path or resource_uri as a link."
        ),
        (
            "for an image: upload_image first; store data.markdown or "
            "data.logical_ref, never data.content_url or an /api/v1 transport URL; "
            "upload_image always requires an existing page_id and your own open "
            "editing_version_id, so create the page first for a new page, then "
            "upload and call update_context_edit; never fall back to data:"
        ),
        (
            "to write: create_context_page (new page) or begin_context_edit "
            "(existing page); then patch_context_edit (local changes) or "
            "update_context_edit (whole body); then submit_context_edit_for_review"
        ),
    ]
    assert guide.actor_context["workspace_id"] == str(workspace.id)
    # No actor was passed, so this reads as an internal call: not an AI channel,
    # so neither staging nor a review queue applies to it.
    assert guide.actor_context["my_writes_wait_for_human_review"] is False
    assert guide.actor_context["my_writes_stage_as_drafts"] is False
    assert guide.placement_parent_candidates
    assert all(
        {"node_id", "title", "depth", "path"} == set(candidate)
        for candidate in guide.placement_parent_candidates
    )
    assert "dtxwiki.agent_page.{slug}" in guide.available_prompts
    assert guide.recommended_agent_guidance_flow[0].startswith("Call guide first")
    assert any(
        "no placement goes to Inbox" in step
        for step in guide.recommended_agent_guidance_flow
    )


def test_workspace_provisioning_creates_one_canonical_context_inbox():
    session_factory = make_session_factory()

    with session_factory.begin() as session:
        user = User(
            email="inbox-owner@example.com",
            password_hash="not-used",
            display_name="Inbox Owner",
        )
        session.add(user)
        session.flush()
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(user.id),
            user_id=user.id,
            token_kind="session",
            provenance="browser_session",
            scopes=("*",),
            workspace_role="owner",
            role="owner",
        )
        created = WorkspaceWriteService().create_workspace(
            session=session,
            request=WorkspaceCreateRequest(
                title="Provisioned Inbox",
                slug="provisioned-inbox",
            ),
            actor=actor,
        )
        inbox_nodes = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == created.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "inbox",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        ).all()
        context_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == created.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )

    assert context_root is not None
    assert len(inbox_nodes) == 1
    assert inbox_nodes[0].parent_id == context_root.id
    assert inbox_nodes[0].title == "Inbox"
    assert inbox_nodes[0].is_system_node is True
    assert inbox_nodes[0].can_have_children is True


def test_guide_placement_candidates_include_real_parent_depth_and_path():
    session_factory = make_session_factory()
    workspace_service = WorkspaceReadService()
    tree_service = TreeService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        inbox = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "inbox",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        assert inbox is not None
        research = tree_service.create_folder(
            session=session,
            workspace_id=workspace.id,
            request=TreeFolderCreateRequest(
                parent_id=inbox.id,
                section="context",
                title="Research",
                slug="research",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
        )
        notes = tree_service.create_folder(
            session=session,
            workspace_id=workspace.id,
            request=TreeFolderCreateRequest(
                parent_id=research.id,
                section="context",
                title="Notes",
                slug="notes",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
        )
        guide = workspace_service.guide(
            session=session,
            workspace_id=workspace.id,
            detail_level="full",
        )
        candidates = {
            candidate["node_id"]: candidate
            for candidate in guide.placement_parent_candidates
        }
        context_root = next(
            candidate
            for candidate in guide.placement_parent_candidates
            if candidate["path"] == "/context"
        )

    assert context_root["depth"] == 0
    assert context_root["path"] == "/context"
    assert candidates[str(inbox.id)]["depth"] == 1
    assert candidates[str(inbox.id)]["path"] == "/context/inbox"
    assert candidates[str(research.id)]["depth"] == 2
    assert candidates[str(research.id)]["path"] == "/context/inbox/research"
    assert candidates[str(notes.id)]["depth"] == 3
    assert candidates[str(notes.id)]["path"] == ("/context/inbox/research/notes")


def test_tree_read_service_lists_tree_and_target_path():
    session_factory = make_session_factory()
    tree_service = TreeReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        nodes = tree_service.list_tree(
            session=session,
            workspace_id=workspace.id,
            section="agent",
        )
        path = tree_service.path_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent_page.id,
        )
        page_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "agent_page",
                WorkspaceTreeNode.target_id == agent_page.id,
            )
        )
        parent_id = tree_service.parent_node_id_for_target(
            session=session,
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent_page.id,
        )

    assert any(node.slug == "agent" for node in nodes)
    assert any(node.title == "Agent Guide" for node in nodes)
    assert [entry.title for entry in path] == [
        "Agent",
        "Agent Guide",
        "Default System Prompt",
    ]
    assert parent_id == page_node.parent_id
    assert page_node.can_have_children is False


def test_tree_read_children_count_excludes_subfolders():
    session_factory = make_session_factory()
    tree_service = TreeReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        parent = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            parent_id=None,
            sort_key="z001",
            title="Parent",
            slug="parent",
            target_kind=None,
            target_id=None,
            can_have_children=True,
        )
        session.add(parent)
        session.flush()
        session.add_all(
            [
                WorkspaceTreeNode(
                    workspace_id=workspace.id,
                    section="context",
                    node_kind="folder",
                    parent_id=parent.id,
                    sort_key="z002",
                    title="Empty Child Folder",
                    slug="empty-child-folder",
                    target_kind=None,
                    target_id=None,
                    can_have_children=True,
                ),
                WorkspaceTreeNode(
                    workspace_id=workspace.id,
                    section="context",
                    node_kind="page_ref",
                    parent_id=parent.id,
                    sort_key="z003",
                    title="Child Page",
                    slug="child-page",
                    target_kind="context_page",
                    target_id=uuid4(),
                    can_have_children=True,
                ),
            ]
        )
        session.flush()
        count = tree_service.children_count(
            session=session,
            workspace_id=workspace.id,
            parent_id=parent.id,
        )
        has_children = tree_service.has_children(
            session=session,
            workspace_id=workspace.id,
            parent_id=parent.id,
        )

    assert count == 1
    assert has_children is True


def test_tree_read_folder_only_branch_has_no_count_but_remains_expandable():
    session_factory = make_session_factory()
    tree_service = TreeReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        parent = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            parent_id=None,
            sort_key="z010",
            title="Folder Only",
            slug="folder-only",
            target_kind=None,
            target_id=None,
            can_have_children=True,
        )
        session.add(parent)
        session.flush()
        session.add(
            WorkspaceTreeNode(
                workspace_id=workspace.id,
                section="context",
                node_kind="folder",
                parent_id=parent.id,
                sort_key="z011",
                title="Nested Folder",
                slug="nested-folder",
                target_kind=None,
                target_id=None,
                can_have_children=True,
            )
        )
        session.flush()
        payload = tree_service.get_node(
            session=session,
            workspace_id=workspace.id,
            node_id=parent.id,
        )

    assert payload.children_count == 0
    assert payload.has_children is True


def test_domain_read_service_lists_library_item_versions_and_artifacts(
    media_installer_factory,
):
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/read-source",
            title="Read Source",
            format_hint="markdown",
            raw_text="# Source\n\nBody",
        )
        artifact = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        sources = read_service.list_library_items(
            session=session, workspace_id=workspace.id
        ).items
        detail = read_service.get_library_item(
            session=session,
            workspace_id=workspace.id,
            library_item_id=registration.library_item.id,
        )
        version_payload = read_service.get_library_item_version(
            session=session,
            workspace_id=workspace.id,
            version_id=registration.version.id,
        )
        artifact_payload = read_service.get_library_artifact(
            session=session,
            workspace_id=workspace.id,
            artifact_id=artifact.id,
        )

    assert [source.uri for source in sources] == ["https://example.com/read-source"]
    assert sources[0].processing_status == registration.version.processing_status
    assert detail.current_version.id == registration.version.id
    assert detail.versions[0].processing_status == "completed"
    # A-1: artifact_type is retired on the model; the payload field of the
    # same name is now sourced from `kind` (domain_read.py
    # _library_artifact_payload). This markdown-source extraction produces
    # media_type=text/markdown, which _artifact_kind_for_media_type maps to
    # kind="markdown".
    assert detail.artifacts[0].artifact_type == "markdown"
    assert version_payload.content_hash == registration.version.content_hash
    assert artifact_payload.index_text == "# Source\n\nBody"


def test_library_item_list_projects_processing_status_without_n_plus_one():
    session_factory = make_session_factory()
    library_service = LibraryService()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        expected: dict[object, str] = {}
        for index, processing_status in enumerate(("queued", "converting", "failed")):
            registration = library_service.register_library_item(
                session=session,
                workspace_id=workspace.id,
                canonical_uri=f"https://example.com/status-{index}",
                title=f"Status {index}",
                raw_text=f"status {index}",
            )
            registration.version.processing_status = processing_status
            expected[registration.library_item.id] = processing_status
        session.flush()

        select_count = 0

        def count_selects(*args):
            nonlocal select_count
            statement = args[2]
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            result = read_service.list_library_items(
                session=session,
                workspace_id=workspace.id,
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)

    assert {item.id: item.processing_status for item in result.items} == expected
    assert select_count == 2


def test_domain_read_library_hidden_when_archived(media_installer_factory):
    # Codex round-11/12: a direct GET of an archived/hidden library item (or its
    # version/artifact) must 404 like every other read lane, not re-serve the
    # stored content on a trust-only check.
    session_factory = make_session_factory()
    library_service = LibraryService()
    extraction_service = ExtractionService(
        media_installer_factory=media_installer_factory
    )
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/archived-read-source",
            title="Archived Read Source",
            format_hint="markdown",
            raw_text="# Source\n\nSensitive body",
        )
        artifact = extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        registration.library_item.lifecycle_status = "archived"
        session.flush()

        reads = (
            lambda: read_service.get_library_item(
                session=session,
                workspace_id=workspace.id,
                library_item_id=registration.library_item.id,
            ),
            lambda: read_service.get_library_item_version(
                session=session,
                workspace_id=workspace.id,
                version_id=registration.version.id,
            ),
            lambda: read_service.get_library_artifact(
                session=session,
                workspace_id=workspace.id,
                artifact_id=artifact.id,
            ),
        )
        for read in reads:
            with pytest.raises(ReadNotFoundError):
                read()


def test_domain_read_service_reads_agent_page_and_categories():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        categories = read_service.list_agent_categories(
            session=session,
            workspace_id=workspace.id,
        )
        pages = read_service.list_agent_pages(
            session=session,
            workspace_id=workspace.id,
            category="agent_guide",
            actor=_system_actor(workspace.id),
        ).items
        detail = read_service.read_agent_page(
            session=session,
            workspace_id=workspace.id,
            agent_page_id=agent_page.id,
            version_selector="active",
            actor=_system_actor(workspace.id),
        )
        draft_detail = read_service.read_agent_page(
            session=session,
            workspace_id=workspace.id,
            slug=DEFAULT_AGENT_PAGE_SLUG,
            version_selector="draft",
            actor=_system_actor(workspace.id),
        )

    assert categories[0].category == "agent_guide"
    assert categories[0].default_injection_policy == "always"
    assert [page.slug for page in pages] == [DEFAULT_AGENT_PAGE_SLUG]
    assert detail.body_markdown.startswith("# Default System Prompt")
    assert detail.version_info.version_status == "active_snapshot"
    assert [entry.title for entry in detail.tree_path] == [
        "Agent",
        "Agent Guide",
        "Default System Prompt",
    ]
    assert draft_detail.body_markdown is None
    assert draft_detail.version_info is None


def test_domain_read_service_reads_context_page_and_change_history():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="read-context",
        title="Read Context",
        body_md="# Read Context\n\nBody",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        legacy_version = session.get(ContextPageVersion, page.version_id)
        legacy_version.actor_display_name = None

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        context_page = session.get(ContextPage, page.id)
        pages = read_service.list_context_pages(
            session=session,
            workspace_id=workspace.id,
        ).items
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
        )
        changes = read_service.list_changes(
            session=session,
            workspace_id=workspace.id,
            target_type="context_page",
            target_id=page.id,
            include_diff=True,
            actor=_system_actor(workspace.id),
        )

    assert [item.slug for item in pages] == ["read-context"]
    assert detail.body_markdown == "# Read Context\n\nBody"
    assert detail.version_info.content_hash == page.content_hash
    assert detail.published_version_info is not None
    assert detail.published_version_info.version_id == page.version_id
    assert detail.published_version_info.version_no == 1
    assert context_page.current_version_id == page.version_id
    assert changes.versions[0].version_id == page.version_id
    assert changes.versions[0].prior_relation == "root"
    assert changes.versions[0].prior_version_id is None
    assert changes.versions[0].actor_display_name == "Human editor"
    assert changes.changesets[0].status == "applied"
    assert changes.diffs[0]["target_type"] == "context_page"


def test_change_set_read_lane_hides_autosave_on_a_draft_version():
    # LEAK-02: the ONLY gate on change_set reads used to be
    # actor_can_read_audit_records, a capability check that says nothing about
    # the operations' TARGETS. The representative payload is an autosave
    # metadata_patch: it lands on a page that already has a visible PUBLISHED
    # head (so page-visibility alone is not enough -- a fix that only checked
    # the page would pass this page and still leak the draft's title), but the
    # diff_ref names the unpublished DRAFT version and diff_summary carries
    # its confidential title text.
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="leak02-autosave-target",
            title="Leak02 Autosave Target",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        published_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Published\n\nVisible body.",
            content_hash="leak02-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        session.add(published_version)
        session.flush()
        page.current_published_version_id = published_version.id
        page.current_version_id = published_version.id

        draft_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="# Draft\n\nSecret upcoming body.",
            content_hash="leak02-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="draft",
            parent_version_id=published_version.id,
        )
        session.add(draft_version)
        session.flush()
        page.latest_draft_version_id = draft_version.id
        page.current_version_id = draft_version.id

        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="page.autosave.draft",
            trigger_type="page_autosave",
            actor_type="human",
            status="applied",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="metadata_patch",
                diff_summary="title -> Confidential Northwind Draft Title",
                diff_ref=f"context_page_version:{draft_version.id}",
            )
        )
        workspace_id = workspace.id
        change_set_id = change_set.id
        page_id = page.id

    viewer = _viewer_actor(workspace_id)
    owner = _system_actor(workspace_id)

    with session_factory() as session:
        # A viewer 404s -- not 403 -- so existence is not disclosed either.
        with pytest.raises(ReadNotFoundError):
            read_service.read_target(
                session=session,
                workspace_id=workspace_id,
                target_type="change_set",
                target_id=change_set_id,
                actor=viewer,
            )
        with pytest.raises(ReadNotFoundError):
            read_service.fetch_target(
                session=session,
                workspace_id=workspace_id,
                entity_id=change_set_id,
                entity_type="change_set",
                actor=viewer,
            )

        # The page itself IS visible to the viewer (has a published head) --
        # proves this isn't just an existing page-visibility gate re-firing.
        read_service.read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=page_id,
            actor=viewer,
        )

        owner_detail = read_service.read_target(
            session=session,
            workspace_id=workspace_id,
            target_type="change_set",
            target_id=change_set_id,
            actor=owner,
        )
        assert owner_detail["reason"] == "page.autosave.draft"
        owner_fetch = read_service.fetch_target(
            session=session,
            workspace_id=workspace_id,
            entity_id=change_set_id,
            entity_type="change_set",
            actor=owner,
        )
        assert owner_fetch is not None
        assert "Confidential Northwind Draft Title" in owner_fetch["text"]


def test_change_set_visible_fails_closed_with_no_operations():
    # change_set_visible's own contract is "readable only if EVERY operation
    # is readable" -- vacuously true for an empty operation set. A change
    # set's `reason` can carry arbitrary free text (risk_notes, an autosave
    # label, etc) independent of any operation, so the predicate must not
    # default to True just because there is nothing (yet) to check against.
    # No current write path persists a ChangeSet without an operation in the
    # same transaction (every ChangeSet(...) call site immediately adds a
    # ChangeOperation before the caller's session can be read from again),
    # so this is a defensive fix for the predicate's own contract, not a
    # currently-reachable HTTP exploit -- it must hold regardless.
    from backend.services.actor_visible_read import change_set_visible

    session_factory = make_session_factory()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="acquire Northwind pricing at a steep discount",
            trigger_type="manual_edit",
            actor_type="human",
            status="applied",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        workspace_id = workspace.id
        change_set_id = change_set.id

    viewer = _viewer_actor(workspace_id)
    owner = _system_actor(workspace_id)

    with session_factory() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set_visible(session, change_set, None) is False
        assert change_set_visible(session, change_set, viewer) is False
        assert change_set_visible(session, change_set, owner) is False


def test_actor_visible_reads_keep_drafts_and_private_agent_content_from_viewers():
    session_factory = make_session_factory()
    read_service = DomainReadService()
    page_service = PageService(session_factory)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        published_page = ContextPage(
            workspace_id=workspace.id,
            slug="visibility-published",
            title="Visibility Published",
            page_type="overview",
            lifecycle_status="published",
        )
        draft_only_page = ContextPage(
            workspace_id=workspace.id,
            slug="visibility-draft-only",
            title="Visibility Draft Only",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add_all([published_page, draft_only_page])
        session.flush()
        published_version = ContextPageVersion(
            workspace_id=published_page.workspace_id,
            page_id=published_page.id,
            version_no=1,
            body_md="# Published\n\nViewer-visible body.",
            content_hash="a" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        pending_version = ContextPageVersion(
            workspace_id=published_page.workspace_id,
            page_id=published_page.id,
            version_no=2,
            body_md="# Pending\n\nPrivate pending body.",
            content_hash="b" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="draft",
        )
        draft_only_version = ContextPageVersion(
            workspace_id=draft_only_page.workspace_id,
            page_id=draft_only_page.id,
            version_no=1,
            body_md="# Draft only\n\nNever published.",
            content_hash="c" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="draft",
        )
        session.add_all([published_version, pending_version, draft_only_version])
        session.flush()
        published_page.current_version_id = pending_version.id
        published_page.current_published_version_id = published_version.id
        published_page.latest_draft_version_id = pending_version.id
        draft_only_page.current_version_id = draft_only_version.id
        draft_only_page.latest_draft_version_id = draft_only_version.id

        claim = ContextClaim(
            workspace_id=pending_version.workspace_id,
            page_version_id=pending_version.id,
            claim_text="Pending private claim",
            location_in_page={},
            claim_type="fact",
            confidence="high",
        )
        citation = ContextCitation(
            workspace_id=published_version.workspace_id,
            target_kind="context_page_version",
            target_id=pending_version.id,
            target_location={},
            cited_context_page_version_id=published_version.id,
            location={},
            snippet="Pending private citation",
            citation_type="context_page",
        )
        session.add_all([claim, citation])

        private_agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Private Persona",
            slug="private-persona",
            sensitivity="private",
            lifecycle_status="active",
        )
        public_agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Public Persona With Draft",
            slug="public-persona-with-draft",
            sensitivity="workspace",
            lifecycle_status="active",
        )
        session.add_all([private_agent, public_agent])
        session.flush()
        private_active = AgentPageVersion(
            workspace_id=private_agent.workspace_id,
            agent_page_id=private_agent.id,
            version_no=1,
            body_markdown="# Private persona",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        public_active = AgentPageVersion(
            workspace_id=public_agent.workspace_id,
            agent_page_id=public_agent.id,
            version_no=1,
            body_markdown="# Public active persona",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        public_draft = AgentPageVersion(
            workspace_id=public_agent.workspace_id,
            agent_page_id=public_agent.id,
            version_no=2,
            body_markdown="# Private pending persona",
            frontmatter={},
            actor_type="human",
            version_status="draft",
        )
        session.add_all([private_active, public_active, public_draft])
        session.flush()
        private_agent.current_active_version_id = private_active.id
        public_agent.current_active_version_id = public_active.id
        public_agent.latest_draft_version_id = public_draft.id
        workspace_id = workspace.id
        published_page_id = published_page.id
        draft_only_page_id = draft_only_page.id
        pending_version_id = pending_version.id
        claim_id = claim.id
        citation_id = citation.id
        public_agent_id = public_agent.id

    viewer = _viewer_actor(workspace_id)
    with session_factory() as session:
        context_rows = read_service.list_context_pages(
            session=session,
            workspace_id=workspace_id,
            actor=viewer,
        ).items
        assert {row.id for row in context_rows} == {published_page_id}
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=published_page_id,
            actor=viewer,
        )
        assert detail.body_markdown == "# Published\n\nViewer-visible body."
        for target_type, target_id in (
            ("context_page_version", pending_version_id),
            ("context_claim", claim_id),
            ("context_citation", citation_id),
        ):
            with pytest.raises(ReadNotFoundError):
                read_service.read_target(
                    session=session,
                    workspace_id=workspace_id,
                    target_type=target_type,
                    target_id=target_id,
                    actor=viewer,
                )
        with pytest.raises(ReadNotFoundError):
            read_service.read_context_page(
                session=session,
                workspace_id=workspace_id,
                page_id=draft_only_page_id,
                actor=viewer,
            )
        agent_rows = read_service.list_agent_pages(
            session=session,
            workspace_id=workspace_id,
            actor=viewer,
        ).items
        assert {row.title for row in agent_rows} == {"Public Persona With Draft"}
        with pytest.raises(ReadNotFoundError):
            read_service.read_agent_page(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=public_agent_id,
                version_selector="draft",
                actor=viewer,
            )

    legacy_rows = page_service.list_pages("default", actor=viewer).pages
    assert {row.id for row in legacy_rows} == {published_page_id}
    assert page_service.read_page(page_id=published_page_id, actor=viewer).body_md == (
        "# Published\n\nViewer-visible body."
    )
    assert [
        entry.version_id
        for entry in page_service.list_versions(
            published_page_id,
            actor=viewer,
        )
    ] == [published_version.id]


def test_history_limits_apply_after_visibility_and_deduplicate_changesets():
    session_factory = make_session_factory()
    read_service = DomainReadService()
    base_time = datetime(2026, 7, 1, tzinfo=UTC)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="bounded-visible-history",
            title="Bounded Visible History",
            page_type="overview",
            lifecycle_status="published",
        )
        agent = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Bounded Agent History",
            slug="bounded-agent-history",
            sensitivity="workspace",
            lifecycle_status="active",
        )
        session.add_all([page, agent])
        session.flush()

        published = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Published history",
            content_hash="1" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        active_agent = AgentPageVersion(
            workspace_id=agent.workspace_id,
            agent_page_id=agent.id,
            version_no=1,
            body_markdown="# Active agent history",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add_all([published, active_agent])
        session.flush()
        drafts = [
            ContextPageVersion(
                workspace_id=page.workspace_id,
                page_id=page.id,
                version_no=version_no,
                body_md=f"# Draft {version_no}",
                content_hash=str(version_no) * 64,
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="editor-user",
                version_status="draft",
            )
            for version_no in range(2, 5)
        ]
        agent_drafts = [
            AgentPageVersion(
                workspace_id=agent.workspace_id,
                agent_page_id=agent.id,
                version_no=version_no,
                body_markdown=f"# Agent draft {version_no}",
                frontmatter={},
                actor_type="human",
                version_status="draft",
            )
            for version_no in range(2, 5)
        ]
        session.add_all([*drafts, *agent_drafts])
        session.flush()
        page.current_version_id = drafts[-1].id
        page.current_published_version_id = published.id
        page.latest_draft_version_id = drafts[-1].id
        agent.current_active_version_id = active_agent.id
        agent.latest_draft_version_id = agent_drafts[-1].id

        applied = ChangeSet(
            workspace_id=workspace.id,
            reason="Visible applied change",
            trigger_type="manual",
            actor_type="human",
            status="applied",
            risk_level="low",
            created_at=base_time,
        )
        pending = [
            ChangeSet(
                workspace_id=workspace.id,
                reason=f"Hidden pending change {index}",
                trigger_type="manual",
                actor_type="human",
                status="pending_review",
                risk_level="low",
                created_at=base_time + timedelta(minutes=index),
            )
            for index in range(1, 4)
        ]
        session.add_all([applied, *pending])
        session.flush()
        session.add_all(
            [
                ChangeOperation(
                    workspace_id=applied.workspace_id,
                    change_set_id=applied.id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="update",
                    diff_summary="First operation",
                ),
                ChangeOperation(
                    workspace_id=applied.workspace_id,
                    change_set_id=applied.id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="update",
                    diff_summary="Second operation",
                ),
                *[
                    ChangeOperation(
                        workspace_id=change_set.workspace_id,
                        change_set_id=change_set.id,
                        target_type="context_page",
                        target_id=page.id,
                        operation="update",
                    )
                    for change_set in pending
                ],
            ]
        )
        workspace_id = workspace.id
        page_id = page.id
        agent_id = agent.id
        published_id = published.id
        active_agent_id = active_agent.id
        applied_id = applied.id

    viewer = _viewer_actor(workspace_id)
    page_versions = PageService(session_factory).list_versions(
        page_id,
        limit=1,
        actor=viewer,
    )
    with session_factory() as session:
        page_history = read_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=page_id,
            limit=1,
            actor=viewer,
        )
        agent_history = read_service.list_changes(
            session=session,
            workspace_id=workspace_id,
            target_type="agent_page",
            target_id=agent_id,
            limit=1,
            actor=viewer,
        )

    assert [entry.version_id for entry in page_versions] == [published_id]
    assert [entry.version_id for entry in page_history.versions] == [published_id]
    assert [entry.change_set_id for entry in page_history.changesets] == [applied_id]
    assert [entry.version_id for entry in agent_history.versions] == [active_agent_id]


def test_actor_visible_read_rejects_restricted_library_artifact(
    media_installer_factory,
):
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/restricted-read",
            title="Restricted Read",
            raw_text="# Restricted\n\nPrivate source body.",
            trust_level="restricted",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        workspace_id = workspace.id
        artifact_id = artifact.id

    with session_factory() as session:
        with pytest.raises(RejectedByPolicyError):
            read_service.read_target(
                session=session,
                workspace_id=workspace_id,
                target_type="library_artifact",
                target_id=artifact_id,
                actor=_viewer_actor(workspace_id),
            )


def test_restricted_library_citation_is_uniform_not_found_for_viewer():
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/restricted-citation",
            title="Restricted Citation",
            raw_text="# Restricted citation source",
            trust_level="restricted",
        )
        page = ContextPage(
            workspace_id=workspace.id,
            slug="visible-citation-owner",
            title="Visible Citation Owner",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Visible citation owner",
            content_hash="f" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        citation = ContextCitation(
            workspace_id=workspace.id,
            target_kind="context_page_version",
            target_id=version.id,
            target_location={},
            library_item_version_id=registration.version.id,
            location={},
            snippet="Restricted source",
            citation_type="raw_source",
        )
        session.add(citation)
        session.flush()
        workspace_id = workspace.id
        citation_id = citation.id

    with session_factory() as session:
        with pytest.raises(ReadNotFoundError, match="context citation not found"):
            read_service.read_target(
                session=session,
                workspace_id=workspace_id,
                target_type="context_citation",
                target_id=citation_id,
                actor=_viewer_actor(workspace_id),
            )


def test_domain_read_service_rejects_missing_explicit_context_version():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="missing-version-context",
        title="Missing Version Context",
        body_md="# Missing Version Context\n\nBody",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        with pytest.raises(ReadNotFoundError):
            read_service.read_context_page(
                session=session,
                workspace_id=workspace.id,
                page_id=page.id,
                version_id=uuid4(),
            )


def test_domain_read_service_list_changes_does_not_leak_other_workspace_versions():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        default_workspace = seed_default_workspace(session)
        seed_default_workspace(session, slug="other", title="Other Workspace")
    other_page = PageService(session_factory).write_page(
        workspace_slug="other",
        slug="other-context",
        title="Other Context",
        body_md="# Other Context\n\nBody",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        with pytest.raises(ReadNotFoundError):
            read_service.list_changes(
                session=session,
                workspace_id=default_workspace.id,
                target_type="context_page",
                target_id=other_page.id,
                actor=_system_actor(default_workspace.id),
            )


def test_domain_read_direct_targets_are_opaque_across_workspaces(
    media_installer_factory,
):
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        requested = seed_default_workspace(session)
        foreign = seed_default_workspace(
            session,
            slug="direct-target-foreign",
            title="Direct Target Foreign",
        )
        foreign_page = _seed_context_pages(session, foreign.id, 1)[0]
        foreign_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == foreign_page.id
            )
        )
        foreign_claim = ContextClaim(
            workspace_id=foreign.id,
            page_version_id=foreign_version.id,
            claim_text="Foreign claim body must stay opaque",
        )
        session.add(foreign_claim)
        session.flush()
        foreign_citation = ContextCitation(
            workspace_id=foreign.id,
            target_kind="context_page_version",
            target_id=foreign_version.id,
            target_location={},
            cited_context_page_version_id=foreign_version.id,
            location={},
            snippet="Foreign citation snippet",
            citation_type="context_page",
        )
        foreign_feedback = FeedbackItem(
            workspace_id=foreign.id,
            target_type="context_page",
            target_id=foreign_page.id,
            body="Foreign feedback body must stay opaque",
        )
        session.add_all([foreign_citation, foreign_feedback])

        foreign_agent = _seed_agent_pages(session, foreign.id, 1)[0]
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=foreign.id,
            canonical_uri="https://example.com/direct-target-foreign",
            title="Foreign direct target",
            raw_text="# Foreign direct target",
        )
        foreign_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        session.flush()

        requested_id = requested.id
        target_ids = {
            "library_item": registration.library_item.id,
            "library_item_version": registration.version.id,
            "library_artifact": foreign_artifact.id,
            "agent_page": foreign_agent.id,
            "context_page_version": foreign_version.id,
            "context_claim": foreign_claim.id,
            "context_citation": foreign_citation.id,
            "feedback": foreign_feedback.id,
        }

    actor = _system_actor(requested_id)
    with session_factory() as session:
        reads = (
            (
                "source not found",
                lambda: read_service.require_visible_library_item(
                    session,
                    requested_id,
                    target_ids["library_item"],
                    actor=actor,
                ),
            ),
            (
                "source version not found",
                lambda: read_service.get_library_item_version(
                    session,
                    requested_id,
                    target_ids["library_item_version"],
                    actor=actor,
                ),
            ),
            (
                "source artifact not found",
                lambda: read_service.get_library_artifact(
                    session,
                    requested_id,
                    target_ids["library_artifact"],
                    actor=actor,
                ),
            ),
            (
                "agent page not found",
                lambda: read_service.read_agent_page(
                    session=session,
                    workspace_id=requested_id,
                    agent_page_id=target_ids["agent_page"],
                    actor=actor,
                ),
            ),
            *(
                (
                    f"{target_type.replace('_', ' ')} not found",
                    lambda target_type=target_type: read_service.read_target(
                        session=session,
                        workspace_id=requested_id,
                        target_type=target_type,
                        target_id=target_ids[target_type],
                        actor=actor,
                    ),
                )
                for target_type in (
                    "context_page_version",
                    "context_claim",
                    "context_citation",
                    "feedback",
                )
            ),
        )
        for message, read in reads:
            with pytest.raises(ReadNotFoundError, match=message):
                read()


def test_citation_visibility_and_agent_history_reject_foreign_workspace_targets():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        requested = seed_default_workspace(session)
        foreign = seed_default_workspace(
            session,
            slug="nested-target-foreign",
            title="Nested Target Foreign",
        )
        source_page = _seed_context_pages(session, requested.id, 1)[0]
        source_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == source_page.id
            )
        )
        foreign_page = _seed_context_pages(session, foreign.id, 1)[0]
        foreign_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == foreign_page.id
            )
        )
        foreign_claim = ContextClaim(
            workspace_id=foreign.id,
            page_version_id=foreign_version.id,
            claim_text="Foreign nested claim",
        )
        session.add(foreign_claim)
        session.flush()
        citations = (
            ContextCitation(
                workspace_id=requested.id,
                target_kind="context_claim",
                target_id=foreign_claim.id,
                target_location={},
                cited_context_page_version_id=source_version.id,
                location={},
                citation_type="context_page",
            ),
            ContextCitation(
                workspace_id=requested.id,
                target_kind="context_page_version",
                target_id=foreign_version.id,
                target_location={},
                cited_context_page_version_id=source_version.id,
                location={},
                citation_type="context_page",
            ),
        )
        session.add_all(citations)
        foreign_agent = _seed_agent_pages(session, foreign.id, 1)[0]
        session.flush()

        requested_id = requested.id
        citation_ids = tuple(citation.id for citation in citations)
        foreign_agent_id = foreign_agent.id

    actor = _system_actor(requested_id)
    with session_factory() as session:
        for citation_id in citation_ids:
            with pytest.raises(
                ReadNotFoundError,
                match="context citation not found",
            ):
                read_service.read_target(
                    session=session,
                    workspace_id=requested_id,
                    target_type="context_citation",
                    target_id=citation_id,
                    actor=actor,
                )

        with pytest.raises(ReadNotFoundError, match="target not found"):
            read_service.list_changes(
                session=session,
                workspace_id=requested_id,
                target_type="agent_page",
                target_id=foreign_agent_id,
                actor=actor,
            )


def test_context_version_review_metadata_scopes_to_caller_workspace():
    # LEAK-05: the shared helper's WHERE only self-joins
    # ReviewItem.workspace_id == ChangeSet.workspace_id -- that is a
    # self-consistency check of the JOIN, not a caller-scope check. Without
    # an explicit workspace_id pin, a ChangeOperation belonging to a
    # DIFFERENT workspace that (abnormally) names this workspace's page/
    # version can still surface that other workspace's ReviewItem
    # disposition here. No normal write path produces such a row today (it
    # is latent per the original report); this test seeds the anomaly
    # directly, as its own docstring-equivalent comment must say, to pin a
    # regression oracle for the fix rather than claim it's live.
    from backend.models.review import ReviewItem
    from backend.services.context_review_disposition import (
        context_version_review_metadata,
    )

    session_factory = make_session_factory()

    with session_factory.begin() as session:
        workspace_a = seed_default_workspace(session)
        workspace_b = seed_default_workspace(
            session, slug="leak05-workspace-b", title="Leak05 Workspace B"
        )

        page_a = ContextPage(
            workspace_id=workspace_a.id,
            slug="leak05-page-a",
            title="Leak05 Page A",
            page_type="overview",
        )
        session.add(page_a)
        session.flush()
        version_a = ContextPageVersion(
            workspace_id=page_a.workspace_id,
            page_id=page_a.id,
            version_no=1,
            body_md="# A\n\nBody.",
            content_hash="leak05-a-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(version_a)
        session.flush()
        page_a.current_published_version_id = version_a.id
        page_a.current_version_id = version_a.id

        # Anomalous cross-workspace row: workspace B's ChangeSet/
        # ChangeOperation names workspace A's version via diff_ref. Seeded
        # directly -- no normal write path produces this.
        change_set_b = ChangeSet(
            workspace_id=workspace_b.id,
            reason="cross-workspace anomaly",
            trigger_type="context_page_publish",
            actor_type="human",
            status="applied",
            risk_level="low",
        )
        session.add(change_set_b)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=change_set_b.workspace_id,
                change_set_id=change_set_b.id,
                target_type="context_page",
                target_id=page_a.id,
                operation="publish",
                diff_ref=f"context_page_version:{version_a.id}",
            )
        )
        session.add(
            ReviewItem(
                workspace_id=workspace_b.id,
                change_set_id=change_set_b.id,
                review_type="context_update",
                status="rejected",
                risk_level="low",
            )
        )
        workspace_a_id = workspace_a.id
        version_a_id = version_a.id

    with session_factory() as session:
        version = session.get(ContextPageVersion, version_a_id)
        scoped = context_version_review_metadata(
            session, [version], workspace_id=workspace_a_id
        )

    assert scoped[version_a_id].disposition == "none"
    assert scoped[version_a_id].review_item_id is None


def _seed_context_pages(session, workspace_id, count):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    pages = []
    for index in range(count):
        page = ContextPage(
            workspace_id=workspace_id,
            slug=f"keyset-context-{index:03d}",
            title=f"Keyset Context {index:03d}",
            page_type="overview",
            lifecycle_status="published",
            updated_at=base + timedelta(minutes=index),
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            created_at=base + timedelta(minutes=index),
            body_md=f"# {page.title}\n\nPublished body.",
            content_hash=f"keyset-context-{index:03d}",
            actor_type="system",
            actor_id=None,
            created_by_actor_kind="system",
            created_by_actor_id="test-system",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        pages.append(page)
    session.flush()
    return pages


def _seed_agent_pages(session, workspace_id, count):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    pages = []
    for index in range(count):
        page = AgentPage(
            workspace_id=workspace_id,
            category="skill",
            title=f"Keyset Agent {index:03d}",
            slug=f"keyset-agent-{index:03d}",
            lifecycle_status="active",
            priority=index,
            updated_at=base + timedelta(minutes=index),
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown=f"# {page.title}\n\nActive body.",
            frontmatter={},
            actor_type="system",
            actor_id=None,
            version_status="active",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        pages.append(page)
    session.flush()
    return pages


def _seed_library_items(session, workspace_id, count, *, trust_level="trusted"):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    items = []
    for index in range(count):
        item = LibraryItem(
            workspace_id=workspace_id,
            connector_type="upload",
            format_hint="markdown",
            uri=f"https://example.com/keyset-{trust_level}-{index:03d}",
            title=f"Keyset Library {index:03d}",
            trust_level=trust_level,
            lifecycle_status="registered",
            updated_at=base + timedelta(minutes=index),
        )
        session.add(item)
        items.append(item)
    session.flush()
    return items


def test_list_context_pages_keyset_paginates_recency_without_overlap():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        _seed_context_pages(session, workspace.id, 25)

    with session_factory() as session:
        first = read_service.list_context_pages(
            session=session, workspace_id=workspace.id, limit=10
        )
        assert len(first.items) == 10
        assert first.total == 25
        assert first.has_more is True
        assert first.next_cursor is not None
        # Recency order: newest published-version instant first.
        timestamps = [item.updated_at for item in first.items]
        assert timestamps == sorted(timestamps, reverse=True)

        second = read_service.list_context_pages(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor=first.next_cursor,
        )
        assert len(second.items) == 10
        assert second.has_more is True

        third = read_service.list_context_pages(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor=second.next_cursor,
        )
        assert len(third.items) == 5
        assert third.has_more is False
        assert third.next_cursor is None

    seen = [item.id for item in first.items + second.items + third.items]
    assert len(seen) == len(set(seen)) == 25


def test_list_context_pages_filters_unpublished_heads_and_hides_archived_by_default():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)

        def add_page(
            slug: str,
            *,
            lifecycle_status: str,
            published: bool,
            latest_draft: bool,
            legacy_draft_head: bool = False,
        ) -> ContextPage:
            page = ContextPage(
                workspace_id=workspace.id,
                slug=slug,
                title=slug.replace("-", " ").title(),
                page_type="overview",
                lifecycle_status=lifecycle_status,
            )
            session.add(page)
            session.flush()
            published_version = None
            if published:
                published_version = ContextPageVersion(
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    version_no=1,
                    body_md=f"# {page.title}\n\nPublished.",
                    content_hash=f"{slug}-published",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="filter-test",
                    version_status="published_snapshot",
                )
                session.add(published_version)
                session.flush()
                page.current_version_id = published_version.id
                page.current_published_version_id = published_version.id
            if latest_draft or legacy_draft_head:
                draft_version = ContextPageVersion(
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    version_no=2 if published else 1,
                    body_md=f"# {page.title}\n\nUnpublished.",
                    content_hash=f"{slug}-draft",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="filter-test",
                    version_status="draft",
                )
                session.add(draft_version)
                session.flush()
                page.current_version_id = draft_version.id
                if latest_draft:
                    page.latest_draft_version_id = draft_version.id
            return page

        published_only = add_page(
            "published-only",
            lifecycle_status="published",
            published=True,
            latest_draft=False,
        )
        published_with_draft = add_page(
            "published-with-draft",
            lifecycle_status="published",
            published=True,
            latest_draft=True,
        )
        legacy_draft_head = add_page(
            "legacy-draft-head",
            lifecycle_status="draft",
            published=False,
            latest_draft=False,
            legacy_draft_head=True,
        )
        archived_with_draft = add_page(
            "archived-with-draft",
            lifecycle_status="archived",
            published=True,
            latest_draft=True,
        )
        workspace_id = workspace.id

    actor = _system_actor(workspace_id)
    with session_factory() as session:
        with_drafts = read_service.list_context_pages(
            session=session,
            workspace_id=workspace_id,
            has_draft=True,
            actor=actor,
        )
        without_drafts = read_service.list_context_pages(
            session=session,
            workspace_id=workspace_id,
            has_draft=False,
            actor=actor,
        )
        archived = read_service.list_context_pages(
            session=session,
            workspace_id=workspace_id,
            status="archived",
            has_draft=True,
            actor=actor,
        )
        with pytest.raises(ReadNotFoundError):
            read_service.read_context_page(
                session=session,
                workspace_id=workspace_id,
                page_id=archived_with_draft.id,
                actor=actor,
            )
        for requested in (True, False):
            with pytest.raises(ScopeDeniedError):
                read_service.list_context_pages(
                    session=session,
                    workspace_id=workspace_id,
                    has_draft=requested,
                    actor=_viewer_actor(workspace_id),
                )

    assert {item.id for item in with_drafts.items} == {
        published_with_draft.id,
        legacy_draft_head.id,
    }
    assert with_drafts.total == len(with_drafts.items) == 2
    assert {item.id for item in without_drafts.items} == {published_only.id}
    assert without_drafts.total == len(without_drafts.items) == 1
    assert {item.id for item in archived.items} == {archived_with_draft.id}
    assert archived.total == len(archived.items) == 1


def test_list_context_pages_garbled_cursor_restarts_at_first_page():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        _seed_context_pages(session, workspace.id, 15)

    with session_factory() as session:
        first = read_service.list_context_pages(
            session=session, workspace_id=workspace.id, limit=10
        )
        garbled = read_service.list_context_pages(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor="not-a-valid-cursor!!!",
        )

    assert [item.id for item in garbled.items] == [item.id for item in first.items]
    assert garbled.total == 15


def test_list_agent_pages_orders_by_recency_keyset():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        _seed_agent_pages(session, workspace.id, 12)

    with session_factory() as session:
        first = read_service.list_agent_pages(
            session=session, workspace_id=workspace.id, limit=10
        )
        timestamps = [item.updated_at for item in first.items]
        assert timestamps == sorted(timestamps, reverse=True)
        assert first.has_more is True
        assert first.next_cursor is not None

        second = read_service.list_agent_pages(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor=first.next_cursor,
        )

    first_ids = {item.id for item in first.items}
    second_ids = {item.id for item in second.items}
    assert first_ids.isdisjoint(second_ids)
    assert second.has_more is False
    assert second.next_cursor is None


def test_list_agent_pages_hides_archived_by_default():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        active, archived = _seed_agent_pages(session, workspace.id, 2)
        archived.lifecycle_status = "archived"
        workspace_id = workspace.id

    actor = _system_actor(workspace_id)
    with session_factory() as session:
        current = read_service.list_agent_pages(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )
        archived_only = read_service.list_agent_pages(
            session=session,
            workspace_id=workspace_id,
            lifecycle_status="archived",
            actor=actor,
        )
        with pytest.raises(ReadNotFoundError):
            read_service.read_agent_page(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=archived.id,
                actor=actor,
            )

    current_ids = {item.id for item in current.items}
    assert active.id in current_ids
    assert archived.id not in current_ids
    assert current.total == len(current.items)
    assert {item.id for item in archived_only.items} == {archived.id}
    assert archived_only.total == 1


def test_list_library_items_keyset_total_and_has_more():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        _seed_library_items(session, workspace.id, 14)

    with session_factory() as session:
        first = read_service.list_library_items(
            session=session, workspace_id=workspace.id, limit=10
        )
        assert first.total == 14
        assert first.has_more is True
        second = read_service.list_library_items(
            session=session,
            workspace_id=workspace.id,
            limit=10,
            cursor=first.next_cursor,
        )

    assert len(second.items) == 4
    assert second.has_more is False
    assert second.next_cursor is None


def test_list_library_items_restricted_total_excludes_hidden_count():
    session_factory = make_session_factory()
    read_service = DomainReadService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        _seed_library_items(session, workspace.id, 4, trust_level="trusted")
        _seed_library_items(session, workspace.id, 6, trust_level="restricted")

    with session_factory() as session:
        unfiltered = read_service.list_library_items(
            session=session, workspace_id=workspace.id, limit=50
        )
        filtered = read_service.list_library_items(
            session=session,
            workspace_id=workspace.id,
            limit=50,
            exclude_trust_levels={"restricted"},
        )

    # An authorized actor sees the full total; a restricted actor's total is
    # the permission-filtered count, never the unfiltered aggregate, so the
    # hidden restricted-item count cannot be inferred.
    assert unfiltered.total == 10
    assert filtered.total == 4
    assert all(item.trust_level == "trusted" for item in filtered.items)
    assert len(filtered.items) == 4
