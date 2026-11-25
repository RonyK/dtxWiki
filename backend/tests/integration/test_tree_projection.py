"""B-7 shared tree projection + REST tree actor filter (Sprint-3 C1-T6, A-9)."""

from uuid import uuid4

import pytest
from sqlalchemy import event, select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.tree import TreeFolderCreateRequest
from backend.services.okf import build_workspace_index
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import TreeService, ensure_primary_tree_placement

pytestmark = pytest.mark.integration


def _actor(role):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _seed_draft_only_page(session, workspace_id):
    page = ContextPage(
        workspace_id=workspace_id,
        slug="a9-draft-only",
        title="A9 Draft Only",
        page_type="concept",
        lifecycle_status="draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="# Draft\n\nDraft body.",
        content_hash="a9-draft-1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.latest_draft_version_id = version.id
    ensure_primary_tree_placement(
        session=session,
        workspace_id=workspace_id,
        target_kind="context_page",
        target_id=page.id,
        title=page.title,
        section="context",
        parent_id=None,
        default_parent_slug="inbox",
        actor_kind="human",
        actor_id="tester",
    )
    return page


def _seed_restricted_library_item(session, workspace_id):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="url",
        format_hint="html",
        uri="https://example.com/a9-secret",
        title="A9 Secret Source",
        metadata_json={},
        trust_level="restricted",
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    ensure_primary_tree_placement(
        session=session,
        workspace_id=workspace_id,
        target_kind="library_item",
        target_id=item.id,
        title=item.title,
        section="library",
        parent_id=None,
        default_parent_slug="library",
        actor_kind="human",
        actor_id="tester",
    )
    return item


def _seed_agent_page(
    session,
    workspace_id,
    *,
    title,
    slug,
    category="persona",
    sensitivity="workspace",
    active,
):
    page = AgentPage(
        workspace_id=workspace_id,
        category=category,
        title=title,
        slug=slug,
        sensitivity=sensitivity,
        lifecycle_status="active" if active else "draft",
    )
    session.add(page)
    session.flush()
    version = AgentPageVersion(
        workspace_id=workspace_id,
        agent_page_id=page.id,
        version_no=1,
        body_markdown=f"# {title}\n\nBody.",
        actor_type="human",
        version_status="active_snapshot" if active else "draft",
    )
    session.add(version)
    session.flush()
    if active:
        page.current_active_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    ensure_primary_tree_placement(
        session=session,
        workspace_id=workspace_id,
        target_kind="agent_page",
        target_id=page.id,
        title=page.title,
        slug=page.slug,
        section="agent",
        parent_id=None,
        default_parent_slug=category,
        actor_kind="human",
        actor_id="tester",
        can_have_children=True,
    )
    return page


def test_viewer_tree_omits_restricted_and_draft_only_nodes(session_factory):
    """A-9 repro: before the fix the REST tree served every node title to any
    viewer — restricted Library titles and draft-only pages included."""

    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_draft_only_page(session, workspace_id)
        _seed_restricted_library_item(session, workspace_id)

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_titles = {
            node.title
            for node in reader.list_tree_data(
                session=session, workspace_id=workspace_id, actor=_actor("viewer")
            ).nodes
        }
        editor_titles = {
            node.title
            for node in reader.list_tree_data(
                session=session, workspace_id=workspace_id, actor=_actor("editor")
            ).nodes
        }

    assert "A9 Draft Only" not in viewer_titles
    assert "A9 Secret Source" not in viewer_titles
    assert "A9 Draft Only" in editor_titles
    # restricted library visibility is scope-gated, not role-gated: an editor
    # without the restricted-read grant must not see it either
    assert "A9 Secret Source" not in editor_titles


def test_tree_and_index_expose_identical_visible_sets(session_factory):
    """Cross-surface parity: get_index and the REST tree agree per actor."""

    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_draft_only_page(session, workspace_id)

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        for role in ("viewer", "editor"):
            actor = _actor(role)
            index = build_workspace_index(
                session, workspace_id, actor=actor, workspace_slug="default"
            )
            tree_page_titles = {
                node.title
                for node in reader.list_tree_data(
                    session=session, workspace_id=workspace_id, actor=actor
                ).nodes
                if node.target_kind == "context_page"
            }
            assert ("A9 Draft Only" in index) == (
                "A9 Draft Only" in tree_page_titles
            ), role


def test_agent_tree_applies_active_draft_and_sensitive_category_gates(
    session_factory,
):
    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_agent_page(
            session,
            workspace_id,
            title="Public Persona",
            slug="public-persona",
            active=True,
        )
        _seed_agent_page(
            session,
            workspace_id,
            title="Draft Persona",
            slug="draft-persona",
            active=False,
        )
        _seed_agent_page(
            session,
            workspace_id,
            title="Owner Memory",
            slug="owner-memory",
            category="memory",
            active=True,
        )
        _seed_agent_page(
            session,
            workspace_id,
            title="Private Persona",
            slug="private-persona",
            sensitivity="private",
            active=True,
        )

    def titles_for(role):
        with session_factory() as session:
            workspace_id = _workspace_id(session)
            return {
                node.title
                for node in reader.list_tree_data(
                    session=session,
                    workspace_id=workspace_id,
                    actor=_actor(role),
                ).nodes
            }

    viewer = titles_for("viewer")
    editor = titles_for("editor")
    owner = titles_for("owner")
    assert "Public Persona" in viewer
    assert "Draft Persona" not in viewer
    assert "Owner Memory" not in viewer
    assert "Private Persona" not in viewer
    assert "Draft Persona" in editor
    assert "Owner Memory" not in editor
    assert "Private Persona" not in editor
    assert {
        "Public Persona",
        "Draft Persona",
        "Owner Memory",
        "Private Persona",
    } <= owner


def test_tree_payload_batches_target_metadata(session_factory, page_service):
    context = page_service.write_page(
        workspace_slug="default",
        slug="tree-target-meta",
        title="Tree Target Meta",
        body_md="# Tree Target Meta\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        agent = _seed_agent_page(
            session,
            workspace_id,
            title="Metadata Persona",
            slug="metadata-persona",
            active=True,
        )

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        nodes = TreeReadService().list_tree_data(
            session=session,
            workspace_id=workspace_id,
            actor=_actor("owner"),
        ).nodes
    by_target = {node.target_id: node for node in nodes if node.target_id}
    assert by_target[context.id].target_meta == {
        "page_type": "overview",
        "lifecycle_status": "published",
    }
    assert by_target[agent.id].target_meta == {
        "category": "persona",
        "injection_policy": "task_matched",
        "lifecycle_status": "active",
    }


def test_list_tree_query_count_is_bounded(session_factory):
    """N+1 guard: listing a ~50-node tree must stay within a handful of
    statements (reverting to per-node count queries fails this)."""

    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        for i in range(50):
            session.add(
                WorkspaceTreeNode(
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=root.id,
                    sort_key=f"{i + 100:04d}",
                    title=f"Bulk {i}",
                    slug=f"bulk-{i}",
                    is_system_node=False,
                    can_have_children=True,
                    primary_placement=False,
                    visibility_status="visible",
                )
            )
        session.flush()

    statements: list[str] = []
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        engine = session.get_bind()

        def _count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            reader.list_tree_data(
                session=session, workspace_id=workspace_id, actor=_actor("editor")
            )
        finally:
            event.remove(engine, "before_cursor_execute", _count)

    assert len(statements) <= 8, f"{len(statements)} statements: {statements[:10]}"


def test_tree_write_uses_incremental_edge_refresh(session_factory, monkeypatch):
    from backend.services.indexing import edge_registry

    def reject_full_rebuild(*_args, **_kwargs):
        raise AssertionError("tree write must not rebuild the workspace projection")

    monkeypatch.setattr(
        edge_registry, "refresh_tree_edge_projection", reject_full_rebuild
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        created = TreeService().create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id,
                section="context",
                title="Incremental Edge Folder",
            ),
            actor_kind="human",
            actor_id="tester",
        )

    assert created.title == "Incremental Edge Folder"
