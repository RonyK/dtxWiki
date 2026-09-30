from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import get_settings
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.repositories.workspace_repo import get_workspace_by_slug, insert_workspace

DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_TITLE = "Default Workspace"
DEFAULT_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000010")
DEFAULT_AGENT_PAGE_SLUG = "default-system-prompt"
DEFAULT_AGENT_PAGE_TITLE = "Default System Prompt"
DEFAULT_AGENT_PAGE_BODY = (
    "# Default System Prompt\n\n"
    "You maintain a source-grounded knowledge base. Always cite sources for "
    "factual claims."
)
SECTION_ROOTS = [
    ("agent", "agent", "Agent", "0001", True, True),
    ("context", "context", "Context", "0002", True, True),
    ("library", "library", "Library", "0003", True, True),
    ("ask", "ask", "Ask", "0004", False, False),
]
AGENT_CATEGORY_ROOTS = [
    ("agent_guide", "Agent Guide"),
    ("persona", "Persona"),
    ("skill", "Skill"),
    ("memory", "Memory"),
]


def seed_default_workspace(
    session: Session,
    slug: str = DEFAULT_WORKSPACE_SLUG,
    title: str = DEFAULT_WORKSPACE_TITLE,
) -> Workspace:
    workspace = get_workspace_by_slug(session, slug)
    if workspace is None:
        workspace = insert_workspace(session, slug=slug, title=title)
    if workspace.created_by_actor_id is None:
        workspace.created_by_actor_id = DEFAULT_ACTOR_ID
    if workspace.owner_actor_id is None:
        workspace.owner_actor_id = DEFAULT_ACTOR_ID
    if session.get(WorkspaceMediaWriteIntent, workspace.id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace.id,
                state="open",
                delete_generation=0,
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
    agent_page = seed_default_agent_page(session, workspace)
    seed_default_tree(session, workspace, agent_page)
    from backend.services.indexing.fts5 import sync_agent_page_doc_sources

    sync_agent_page_doc_sources(session, agent_page)
    return workspace


def seed_default_agent_page(session: Session, workspace: Workspace) -> AgentPage:
    for agent_page in session.query(AgentPage).filter_by(
        workspace_id=workspace.id,
        slug=DEFAULT_AGENT_PAGE_SLUG,
    ):
        return agent_page

    agent_page = AgentPage(
        workspace_id=workspace.id,
        category="agent_guide",
        title=DEFAULT_AGENT_PAGE_TITLE,
        slug=DEFAULT_AGENT_PAGE_SLUG,
        lifecycle_status="active",
        injection_policy="always",
        priority=1,
        sensitivity="workspace",
        created_by_actor_type="system",
        created_by_actor_id=DEFAULT_ACTOR_ID,
    )
    session.add(agent_page)
    session.flush()
    version = AgentPageVersion(
        workspace_id=agent_page.workspace_id,
        agent_page_id=agent_page.id,
        version_no=1,
        body_markdown=DEFAULT_AGENT_PAGE_BODY,
        actor_type="system",
        actor_id=DEFAULT_ACTOR_ID,
        version_status="active_snapshot",
        activation_reason="default seed",
    )
    session.add(version)
    session.flush()
    agent_page.current_active_version_id = version.id
    return agent_page


def seed_default_tree(
    session: Session,
    workspace: Workspace,
    default_agent_page: AgentPage,
) -> None:
    section_roots: dict[str, WorkspaceTreeNode] = {}
    for (
        section,
        slug,
        title,
        sort_key,
        allow_folder,
        allow_page_parent,
    ) in SECTION_ROOTS:
        section_roots[section] = ensure_tree_node(
            session=session,
            workspace=workspace,
            section=section,
            slug=slug,
            title=title,
            sort_key=sort_key,
            allow_children=True,
        )
        ensure_tree_policy(
            session=session,
            workspace=workspace,
            section=section,
            allow_user_folder_create=allow_folder,
            allow_page_as_parent=allow_page_parent,
            fixed_root_node_ids=[section_roots[section].id],
        )

    agent_fixed_roots = [section_roots["agent"].id]
    agent_category_roots: dict[str, WorkspaceTreeNode] = {}
    for index, (slug, title) in enumerate(AGENT_CATEGORY_ROOTS, start=1):
        node = ensure_tree_node(
            session=session,
            workspace=workspace,
            section="agent",
            slug=slug,
            title=title,
            sort_key=f"{index:04d}",
            parent_id=section_roots["agent"].id,
            allow_children=True,
        )
        agent_category_roots[slug] = node
        agent_fixed_roots.append(node.id)

    # Workspace provisioning and lazy MCP placement share one canonical,
    # concurrency-safe Inbox ensure path.
    from backend.services.tree_service import ensure_context_inbox_node

    context_inbox = ensure_context_inbox_node(session, workspace.id)
    ensure_tree_node(
        session=session,
        workspace=workspace,
        section="context",
        slug="ask_captures",
        title="Ask Captures",
        sort_key="0001",
        parent_id=context_inbox.id,
        allow_children=True,
    )
    ensure_tree_node(
        session=session,
        workspace=workspace,
        section="agent",
        slug="memory_candidates",
        title="Memory Candidates",
        sort_key="0001",
        parent_id=agent_category_roots["memory"].id,
        allow_children=True,
    )
    ensure_default_agent_page_placement(
        session=session,
        workspace=workspace,
        agent_page=default_agent_page,
        parent_id=agent_category_roots["agent_guide"].id,
    )

    update_tree_policy_fixed_roots(
        session=session,
        workspace=workspace,
        section="agent",
        fixed_root_node_ids=agent_fixed_roots,
    )


def ensure_tree_node(
    session: Session,
    workspace: Workspace,
    section: str,
    slug: str,
    title: str,
    sort_key: str,
    parent_id: UUID | None = None,
    allow_children: bool = True,
) -> WorkspaceTreeNode:
    node = (
        session.query(WorkspaceTreeNode)
        .filter_by(
            workspace_id=workspace.id,
            section=section,
            parent_id=parent_id,
            slug=slug,
        )
        .first()
    )
    if (
        node is None
        and section == "library"
        and parent_id is None
        and slug == "library"
    ):
        node = (
            session.query(WorkspaceTreeNode)
            .filter_by(
                workspace_id=workspace.id,
                section="library",
                parent_id=None,
                slug="library",
            )
            .first()
        )
    if node is not None:
        node.slug = slug
        node.title = title
        node.sort_key = sort_key
        node.can_have_children = allow_children
        session.add(node)
        session.flush()
        return node
    node = WorkspaceTreeNode(
        workspace_id=workspace.id,
        section=section,
        node_kind="folder",
        parent_id=parent_id,
        sort_key=sort_key,
        title=title,
        slug=slug,
        is_system_node=True,
        can_have_children=allow_children,
        primary_placement=True,
        visibility_status="visible",
        created_by_actor_type="system",
        created_by_actor_id=DEFAULT_ACTOR_ID,
    )
    session.add(node)
    session.flush()
    return node


def ensure_default_agent_page_placement(
    session: Session,
    workspace: Workspace,
    agent_page: AgentPage,
    parent_id: UUID,
) -> WorkspaceTreeNode:
    node = (
        session.query(WorkspaceTreeNode)
        .filter_by(
            workspace_id=workspace.id,
            target_kind="agent_page",
            target_id=agent_page.id,
            primary_placement=True,
        )
        .first()
    )
    if node is not None:
        return node
    node = WorkspaceTreeNode(
        workspace_id=workspace.id,
        section="agent",
        node_kind="page_ref",
        parent_id=parent_id,
        sort_key="0001",
        title=agent_page.title,
        target_kind="agent_page",
        target_id=agent_page.id,
        is_system_node=False,
        can_have_children=False,
        primary_placement=True,
        visibility_status="visible",
        created_by_actor_type="system",
        created_by_actor_id=DEFAULT_ACTOR_ID,
    )
    session.add(node)
    session.flush()
    return node


def ensure_tree_policy(
    session: Session,
    workspace: Workspace,
    section: str,
    allow_user_folder_create: bool,
    allow_page_as_parent: bool,
    fixed_root_node_ids: list[UUID],
) -> TreePolicy:
    policy = session.get(TreePolicy, (workspace.id, section))
    fixed_ids = [str(root_id) for root_id in fixed_root_node_ids]
    if policy is None:
        policy = TreePolicy(
            workspace_id=workspace.id,
            section=section,
            allow_user_folder_create=allow_user_folder_create,
            allow_page_as_parent=allow_page_as_parent,
            fixed_root_node_ids=fixed_ids,
        )
        session.add(policy)
        session.flush()
        return policy
    policy.allow_user_folder_create = allow_user_folder_create
    policy.allow_page_as_parent = allow_page_as_parent
    policy.fixed_root_node_ids = fixed_ids
    return policy


def update_tree_policy_fixed_roots(
    session: Session,
    workspace: Workspace,
    section: str,
    fixed_root_node_ids: list[UUID],
) -> None:
    policy = session.get(TreePolicy, (workspace.id, section))
    if policy is not None:
        policy.fixed_root_node_ids = [str(root_id) for root_id in fixed_root_node_ids]


def main() -> None:
    settings = get_settings()
    session_factory = make_session_factory_from_settings(settings)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(
            session,
            slug=DEFAULT_WORKSPACE_SLUG,
            title=DEFAULT_WORKSPACE_TITLE,
        )
        print(f"seeded workspace {workspace.slug}")


if __name__ == "__main__":
    main()
