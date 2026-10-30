"""Sprint-3 completion gate (design §13 proxies, §20.1 row 3).

1,000+ pages in a depth-3 folder tree: ① every index node render stays
within the ~7k-token budget (28k chars at 4 chars/token), ② golden pages
are reachable within 3 hops from the section root with each intermediate
folder present in its parent's render, ③ fanout truncation is honest.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.tree import TreeFolderCreateRequest
from backend.services.index_refs import format_index_ref, title_text
from backend.services.okf import build_workspace_index
from backend.services.okf_index import (
    GET_INDEX_NODE_TOKEN_BUDGET,
    build_routing_index,
    estimate_index_tokens,
)
from backend.services.tree_projection import TreeProjection, TreeProjectionService
from backend.services.tree_service import TreeService, create_tree_node

pytestmark = pytest.mark.integration

DEPTH_1_FOLDERS = 5
DEPTH_2_FOLDERS = 5
PAGES_PER_LEAF = 42  # 5*5*42 = 1,050 pages


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


@pytest.fixture(scope="module")
def scale_workspace(session_factory_module):
    session_factory = session_factory_module
    tree = TreeService()
    golden: list[tuple[UUID, str, list[str]]] = []
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        for d1 in range(DEPTH_1_FOLDERS):
            level1 = tree.create_folder(
                session=session,
                workspace_id=workspace_id,
                request=TreeFolderCreateRequest(
                    parent_id=root.id,
                    section="context",
                    title=f"Domain {d1}",
                    slug=f"domain-{d1}",
                ),
                actor_kind="human",
                actor_id="tester",
            )
            for d2 in range(DEPTH_2_FOLDERS):
                level2 = tree.create_folder(
                    session=session,
                    workspace_id=workspace_id,
                    request=TreeFolderCreateRequest(
                        parent_id=level1.id,
                        section="context",
                        title=f"Area {d1}-{d2}",
                        slug=f"area-{d1}-{d2}",
                    ),
                    actor_kind="human",
                    actor_id="tester",
                )
                for p in range(PAGES_PER_LEAF):
                    slug = f"doc-{d1}-{d2}-{p:02d}"
                    page = ContextPage(
                        workspace_id=workspace_id,
                        slug=slug,
                        title=f"Doc {d1}-{d2}-{p:02d}",
                        page_type="concept",
                        lifecycle_status="published",
                    )
                    session.add(page)
                    session.flush()
                    version = ContextPageVersion(
                        workspace_id=workspace_id,
                        page_id=page.id,
                        version_no=1,
                        body_md=(
                            f"# Doc {d1}-{d2}-{p:02d}\n\n"
                            f"Reference document {p:02d} for area {d1}-{d2} "
                            "covering operational procedures in detail."
                        ),
                        content_hash=f"{slug}-1",
                        actor_type="human",
                        created_by_actor_kind="human",
                        created_by_actor_id="tester",
                        version_status="published_snapshot",
                        nav_description=(
                            f"Reference document {p:02d} for area {d1}-{d2} "
                            "covering operational procedures in detail."
                        ),
                    )
                    session.add(version)
                    session.flush()
                    page.current_published_version_id = version.id
                    page.current_version_id = version.id
                    create_tree_node(
                        session,
                        workspace_id=workspace_id,
                        section="context",
                        node_kind="page_ref",
                        parent_id=level2.id,
                        title=page.title,
                        target_kind="context_page",
                        target_id=page.id,
                        can_have_children=False,
                        primary_placement=True,
                        actor_kind="human",
                        actor_id="tester",
                        sort_key=f"{p + 1:04d}",
                    )
                    if p == 0:
                        golden.append(
                            (
                                page.id,
                                page.title,
                                [
                                    "/context",
                                    format_index_ref("folder", level1.id),
                                    format_index_ref("folder", level2.id),
                                ],
                            )
                        )
        # name index for golden pages only (bulk seeding skips projections
        # for speed; the gate needs structure, not search)
        return {"workspace_id": workspace_id, "golden": golden}


@pytest.fixture(scope="module")
def session_factory_module():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.models.base import Base
    from backend.scripts.seed_dev import seed_default_workspace

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_default_workspace(session)
    return factory


def test_index_token_budget_per_node(session_factory_module, scale_workspace):
    """§13-①: root + every folder node render <= the 7k-token proxy."""

    session_factory = session_factory_module
    with session_factory() as session:
        workspace_id = scale_workspace["workspace_id"]
        renders = {
            "root": build_workspace_index(
                session, workspace_id, actor=None, workspace_slug="default"
            )
        }
        projection = TreeProjectionService().load(
            session, workspace_id, None, sections=("context",)
        )
        for node in projection.nodes.values():
            if node.node_kind != "folder" or not projection.is_visible(node.id):
                continue
            renders[str(node.id)] = build_workspace_index(
                session,
                workspace_id,
                actor=None,
                workspace_slug="default",
                node=str(node.id),
            )
    for name, render in renders.items():
        estimated = estimate_index_tokens(render)
        assert estimated <= GET_INDEX_NODE_TOKEN_BUDGET, (
            f"node {name} estimates {estimated} tokens > {GET_INDEX_NODE_TOKEN_BUDGET}"
        )
    # sanity: this is genuinely the routing mode over 1,000+ pages
    assert "](dtxwiki://index/folder/" in renders["root"]

    # One pure projection covers a different budget boundary: a maximum-size
    # Unicode folder title grows the header beyond the old 64-token reserve.
    # The English scale fixture above cannot expose this; no extra DB case is
    # needed to prove that the complete response, including its tail, fits.
    root_id, folder_id = uuid4(), uuid4()

    def folder(node_id, title, slug, parent_id, summary="x"):
        return WorkspaceTreeNode(
            id=node_id, workspace_id=workspace_id, section="context",
            node_kind="folder", title=title, slug=slug, parent_id=parent_id,
            sort_key=slug, nav_summary=summary,
        )

    root = folder(root_id, "Context", "context", None)
    target = folder(folder_id, "한" * 80, "r", root_id)
    children = [
        folder(uuid4(), "나" * 80, f"folder-{i}", folder_id, "글" * 132 + "a" * 28)
        for i in range(50)
    ]
    nodes = {node.id: node for node in [root, target, *children]}
    projection = TreeProjection(nodes, set(nodes))
    projection._children = {root_id: [target], folder_id: children}
    document = build_routing_index(
        None, workspace_id, actor=None, workspace_slug="default",
        node=format_index_ref("folder", folder_id), projection=projection,
    )
    assert estimate_index_tokens(document.markdown) <= GET_INDEX_NODE_TOKEN_BUDGET
    assert document.overflow == len(children) - len(document.entries)
    assert f"\n\n…and {document.overflow} more" in document.markdown


def test_golden_pages_reachable_within_three_hops(
    session_factory_module, scale_workspace
):
    """§13-②: each golden page's placement is <=3 hops from the section root
    AND every intermediate folder line is present in its parent's render."""

    session_factory = session_factory_module
    with session_factory() as session:
        workspace_id = scale_workspace["workspace_id"]
        renders: dict[str, str] = {}

        def render(node_ref):
            # No writes happen between golden paths, so ancestors render once.
            if node_ref not in renders:
                renders[node_ref] = build_workspace_index(
                    session,
                    workspace_id,
                    actor=None,
                    workspace_slug="default",
                    node=node_ref,
                )
            return renders[node_ref]

        for page_id, title, hops in scale_workspace["golden"]:
            assert len(hops) <= 3
            # each hop's folder appears in the parent hop's render
            for parent_ref, child_ref in zip(hops, hops[1:], strict=False):
                parent_render = render(parent_ref)
                assert f"]({child_ref})" in parent_render, (
                    f"{child_ref} missing from {parent_ref} render"
                )
            # the page itself appears in the leaf render
            leaf_render = render(hops[-1])
            page_ref = format_index_ref("context", page_id)
            assert f"- [{title_text(title)}]({page_ref}) — " in leaf_render


def test_fanout_truncation_is_honest(session_factory_module, scale_workspace):
    session_factory = session_factory_module
    with session_factory() as session:
        workspace_id = scale_workspace["workspace_id"]
        # root of context: 5 folders + inbox etc. -> no truncation
        # leaf area: 42 pages -> under fanout, no truncation
        leaf = build_workspace_index(
            session,
            workspace_id,
            actor=None,
            workspace_slug="default",
            node="/context/domain-0/area-0-0",
        )
        assert leaf.count("](dtxwiki://index/context/") == PAGES_PER_LEAF
        assert "…and" not in leaf


def test_multilingual_summaries_are_truncated_to_token_budget(
    session_factory_module,
    scale_workspace,
):
    with session_factory_module.begin() as session:
        workspace_id = scale_workspace["workspace_id"]
        projection = TreeProjectionService().load(
            session, workspace_id, None, sections=("context",)
        )
        leaf = next(
            node for node in projection.nodes.values() if node.slug == "area-0-0"
        )
        page_ids = [
            node.target_id
            for node in projection.visible_children(leaf.id)
            if node.target_kind == "context_page"
        ]
        version_ids = session.scalars(
            select(ContextPage.current_published_version_id).where(
                ContextPage.id.in_(page_ids)
            )
        ).all()
        originals = {}
        for version in session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.id.in_(version_ids))
        ).all():
            originals[version.id] = version.nav_description
            version.nav_description = "\uac00" * 160

    try:
        with session_factory_module() as session:
            render = build_workspace_index(
                session,
                scale_workspace["workspace_id"],
                actor=None,
                workspace_slug="default",
                node="/context/domain-0/area-0-0",
            )
    finally:
        # The fixture is module-scoped: undo the mutation so the other tests
        # do not depend on definition order.
        with session_factory_module.begin() as session:
            for version_id, description in originals.items():
                session.get(
                    ContextPageVersion, version_id
                ).nav_description = description

    assert estimate_index_tokens(render) <= GET_INDEX_NODE_TOKEN_BUDGET
    assert "…and" in render
