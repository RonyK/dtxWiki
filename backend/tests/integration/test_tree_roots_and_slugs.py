"""B-8: policy-based roots, unified node creation, visible-folder slug
uniqueness, collision-free sort keys (Sprint-3 C1-T5)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.tree import TreeFolderCreateRequest
from backend.services.tree_service import (
    TreeService,
    resolve_tree_parent,
    section_root_node,
)

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _context_root(session, workspace_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "context",
        )
    )


def _folder(service, session, workspace_id, parent_id, title, slug=None):
    return service.create_folder(
        session=session,
        workspace_id=workspace_id,
        request=TreeFolderCreateRequest(
            parent_id=parent_id, section="context", title=title, slug=slug
        ),
        actor_kind="human",
        actor_id="tester",
    )


def test_section_root_consumes_fixed_root_node_ids(session_factory):
    """Repointing the policy root moves default placements (reverting to the
    slug convention fails this)."""

    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        alt_root = _folder(service, session, workspace_id, root.id, "Alt Root")
        policy = session.get(TreePolicy, (workspace_id, "context"))
        policy.fixed_root_node_ids = [str(alt_root.id)]
        session.flush()

        resolved = section_root_node(session, workspace_id, "context")
        assert resolved.id == alt_root.id

        # default placement (no parent_id, no default slug) lands under it
        parent = resolve_tree_parent(
            session=session,
            workspace_id=workspace_id,
            section="context",
            parent_id=None,
        )
        assert parent.id == alt_root.id


def test_section_root_falls_back_to_slug_convention(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        policy = session.get(TreePolicy, (workspace_id, "context"))
        policy.fixed_root_node_ids = []
        session.flush()
        resolved = section_root_node(session, workspace_id, "context")
        assert resolved.slug == "context"


def test_visible_folder_slug_unique_allows_recreate_after_archive(
    session_factory,
):
    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        first = _folder(service, session, workspace_id, root.id, "Dup", slug="dup")

    # duplicate visible slug under the same parent -> rejected by DB
    # (own transaction: the IntegrityError rolls back only this block)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            workspace_id = _workspace_id(session)
            root = _context_root(session, workspace_id)
            node = WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                parent_id=root.id,
                sort_key="9999",
                title="Dup 2",
                slug="dup",
                is_system_node=False,
                can_have_children=True,
                primary_placement=False,
                visibility_status="visible",
            )
            session.add(node)
            session.flush()

    service2 = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        service2.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=first.id,
            actor_kind="human",
            actor_id="tester",
        )
        # archived slug frees the name for a fresh visible folder
        recreated = _folder(
            service2,
            session,
            workspace_id,
            _context_root(session, workspace_id).id,
            "Dup",
            slug="dup",
        )
        assert recreated.id != first.id


def test_sort_key_never_collides_after_archive(session_factory):
    """Repro: siblings 0001..0003, archive 0002, add a sibling — the count
    -based allocator reused 0003; max+1 must not."""

    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        a = _folder(service, session, workspace_id, root.id, "SK A")
        b = _folder(service, session, workspace_id, root.id, "SK B")
        c = _folder(service, session, workspace_id, root.id, "SK C")
        service.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=b.id,
            actor_kind="human",
            actor_id="tester",
        )
        d = _folder(service, session, workspace_id, root.id, "SK D")
        visible_keys = session.scalars(
            select(WorkspaceTreeNode.sort_key).where(
                WorkspaceTreeNode.parent_id == root.id,
                WorkspaceTreeNode.visibility_status == "visible",
                WorkspaceTreeNode.id.in_([a.id, c.id, d.id]),
            )
        ).all()
        assert len(visible_keys) == len(set(visible_keys))

@pytest.mark.parametrize(
    ("title", "slug", "expected_slug"),
    [
        ("\uc6b4\uc601 \ubb38\uc11c", None, "\uc6b4\uc601-\ubb38\uc11c"),
        ("Operations", "\uc6b4\uc601-\ud3f4\ub354", "\uc6b4\uc601-\ud3f4\ub354"),
    ],
    ids=["unicode-title-derived", "explicit-unicode-slug"],
)
def test_folder_unicode_slug_contract_is_consistent_end_to_end(
    session_factory, title, slug, expected_slug
):
    service = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        created = _folder(service, session, workspace_id, root.id, title, slug=slug)
        node_id = created.id
        assert created.slug == expected_slug

    with session_factory() as session:
        stored = session.get(WorkspaceTreeNode, node_id)
        assert stored.slug == expected_slug
        assert stored.title == title
