from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.favorite import UserFavorite
from backend.models.library import LibraryItem
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.favorite_service import set_favorite
from backend.services.read_errors import ReadNotFoundError, ScopeDeniedError
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import ensure_primary_tree_placement
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration
CONTENT_TIME = datetime(2026, 1, 2, 3, tzinfo=UTC)
REVIEW_TIME = CONTENT_TIME - timedelta(days=10)


def _actor(role="owner", user_id=None):
    principal = user_id or uuid4()
    return ActorContext(
        actor_kind="human",
        actor_id=str(principal),
        user_id=principal,
        workspace_role=role,
        role=role,
        token_kind="session",
    )


def _workspace(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _target(session, workspace_id, kind, *, review=False):
    if kind == "context_page":
        target = ContextPage(
            workspace_id=workspace_id,
            title="Context",
            slug=str(uuid4()),
            lifecycle_status="published",
            content_changed_at=CONTENT_TIME,
        )
        session.add(target)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=target.id,
            body_md="published",
            content_hash="published",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="test",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        target.current_version_id = target.current_published_version_id = version.id
        if review:
            draft = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=target.id,
                version_no=2,
                body_md="new draft",
                content_hash="draft",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="test",
                version_status="draft",
                parent_version_id=version.id,
            )
            session.add(draft)
            session.flush()
            target.latest_draft_version_id = target.current_version_id = draft.id
    elif kind == "agent_page":
        target = AgentPage(
            workspace_id=workspace_id,
            title="Agent",
            slug=str(uuid4()),
            category="skill",
            lifecycle_status="active",
            content_changed_at=CONTENT_TIME,
        )
        session.add(target)
        session.flush()
        version = AgentPageVersion(
            workspace_id=workspace_id,
            agent_page_id=target.id,
            body_markdown="active",
            actor_type="human",
            version_status="active_snapshot",
            version_no=1,
        )
        session.add(version)
        session.flush()
        target.current_active_version_id = version.id
        if review:
            draft = AgentPageVersion(
                workspace_id=workspace_id,
                agent_page_id=target.id,
                version_no=2,
                body_markdown="draft",
                actor_type="human",
                version_status="draft",
            )
            session.add(draft)
            session.flush()
            target.latest_draft_version_id = draft.id
    else:
        target = LibraryItem(
            workspace_id=workspace_id,
            title="Library",
            uri=f"https://example.test/{uuid4()}",
            connector_type="web",
            lifecycle_status="ready",
            content_changed_at=CONTENT_TIME,
        )
        session.add(target)
        session.flush()
    node = ensure_primary_tree_placement(
        session=session,
        workspace_id=workspace_id,
        target_kind=kind,
        target_id=target.id,
        title=target.title,
        section=kind.split("_")[0],
        parent_id=None,
        default_parent_slug="inbox"
        if kind == "context_page"
        else "library"
        if kind == "library_item"
        else "skill",
        actor_kind="human",
        actor_id="test",
    )
    if review:
        change = ChangeSet(
            workspace_id=workspace_id,
            trigger_type="test",
            actor_type="human",
            status="needs_review",
        )
        session.add(change)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=workspace_id,
                change_set_id=change.id,
                target_type=kind,
                target_id=target.id,
                operation="update_draft",
                diff_ref=f"{kind}_version:{draft.id}",
            )
        )
        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                change_set_id=change.id,
                review_type="context_update"
                if kind == "context_page"
                else "agent_page_activation",
                status="pending",
                created_at=REVIEW_TIME,
            )
        )
    session.flush()
    return target, node


@pytest.mark.parametrize("kind", ["context_page", "library_item", "agent_page"])
def test_viewer_favorites_are_private_durable_and_follow_targets(session_factory, kind):
    first, second = _actor("viewer"), _actor("viewer")
    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace(session)
        target, node = _target(session, workspace_id, kind)
        target_id, node_id = target.id, node.id
        set_favorite(
            session,
            workspace_id=workspace_id,
            actor=first,
            target_kind=kind,
            target_id=target.id,
            is_favorite=True,
        )
        set_favorite(
            session,
            workspace_id=workspace_id,
            actor=first,
            target_kind=kind,
            target_id=target.id,
            is_favorite=True,
        )
        # Preferences are attached to identity, independently of placement/name.
        node.title = "Renamed"
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == node.section,
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        node.parent_id = root.id
        alias = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section=node.section,
            node_kind="entity_ref",
            target_kind=kind,
            target_id=target_id,
            parent_id=root.id,
            title="Second placement",
            sort_key="ZZZ",
            is_system_node=False,
            can_have_children=True,
            primary_placement=False,
            visibility_status="visible",
        )
        session.add(alias)
    with session_factory() as session:
        assert reader.get_node(session, workspace_id, node_id, actor=first).is_favorite
        assert not reader.get_node(
            session, workspace_id, node_id, actor=second
        ).is_favorite
        listed = reader.list_tree(session, workspace_id, actor=first)
        assert next(node for node in listed if node.id == node_id).is_favorite
        placements = [node for node in listed if node.target_id == target_id]
        assert len(placements) == 2 and all(node.is_favorite for node in placements)
        assert (
            reader.get_node(
                session, workspace_id, node_id, actor=first
            ).content_changed_at
            == CONTENT_TIME
        )
        assert len(session.scalars(select(UserFavorite)).all()) == 1
    with session_factory.begin() as session:
        set_favorite(
            session,
            workspace_id=workspace_id,
            actor=first,
            target_kind=kind,
            target_id=target_id,
            is_favorite=False,
        )
    with session_factory() as session:
        assert not reader.get_node(
            session, workspace_id, node_id, actor=first
        ).is_favorite


def test_favorites_fail_closed_for_inaccessible_targets_and_nonusers(session_factory):
    actor = _actor("viewer")
    with session_factory.begin() as session:
        workspace_id = _workspace(session)
        item, _ = _target(session, workspace_id, "library_item")
        item.trust_level = "restricted"
        session.flush()
        with pytest.raises(ReadNotFoundError):
            set_favorite(
                session,
                workspace_id=workspace_id,
                actor=actor,
                target_kind="library_item",
                target_id=item.id,
                is_favorite=True,
            )
        with pytest.raises(ReadNotFoundError):
            set_favorite(
                session,
                workspace_id=uuid4(),
                actor=actor,
                target_kind="library_item",
                target_id=item.id,
                is_favorite=False,
            )
        with pytest.raises(ScopeDeniedError):
            set_favorite(
                session,
                workspace_id=workspace_id,
                actor=replace(actor, user_id=None),
                target_kind="library_item",
                target_id=item.id,
                is_favorite=True,
            )
        assert not session.scalars(select(UserFavorite)).all()


@pytest.mark.parametrize("kind", ["context_page", "agent_page"])
def test_only_current_authorized_reviews_are_exposed_without_changing_content_time(
    session_factory, kind
):
    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace(session)
        target, node = _target(session, workspace_id, kind, review=True)
        node_id = node.id
    with session_factory.begin() as session:
        owner = reader.get_node(session, workspace_id, node_id, actor=_actor())
        viewer = reader.get_node(session, workspace_id, node_id, actor=_actor("viewer"))
        assert owner.review_status == "pending"
        assert owner.review_requested_at == REVIEW_TIME
        assert owner.content_changed_at == CONTENT_TIME
        assert viewer.review_status is None and viewer.review_requested_at is None
        review = session.scalar(
            select(ReviewItem).where(ReviewItem.workspace_id == workspace_id)
        )
        review.status = "changes_requested"
    with session_factory.begin() as session:
        assert (
            reader.get_node(
                session, workspace_id, node_id, actor=_actor()
            ).review_status
            == "changes_requested"
        )
        target_model = ContextPage if kind == "context_page" else AgentPage
        target = session.get(target_model, target.id)
        target.latest_draft_version_id = None
    with session_factory() as session:
        stale = reader.get_node(session, workspace_id, node_id, actor=_actor())
        assert stale.review_status is None and stale.review_requested_at is None
        assert stale.content_changed_at == CONTENT_TIME


def test_tree_metadata_query_budget_does_not_scale_with_target_count(session_factory):
    reader, actor = TreeReadService(), _actor()
    with session_factory.begin() as session:
        workspace_id = _workspace(session)
        for kind in ("context_page", "agent_page", "library_item"):
            _target(session, workspace_id, kind, review=kind != "library_item")

    def query_count():
        statements = []
        with session_factory() as session:
            engine = session.get_bind()

            def record(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", record)
            try:
                reader.list_tree_data(session, workspace_id, actor=actor)
            finally:
                event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    small = query_count()
    with session_factory.begin() as session:
        for _ in range(15):
            for kind in ("context_page", "agent_page", "library_item"):
                _target(session, workspace_id, kind, review=kind != "library_item")
    assert query_count() == small


def test_rest_viewer_can_favorite_but_cannot_name_unsupported_targets(page_service):
    with TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    ) as client:
        workspace_id = authenticate_test_client_in_workspace(
            client, page_service.session_factory, role="viewer"
        )
        with page_service.session_factory.begin() as session:
            item, node = _target(session, _workspace(session), "library_item")
            item_id, node_id = item.id, node.id
        base = f"/api/v1/workspaces/{workspace_id}"
        response = client.put(
            f"{base}/favorites/library_item/{item_id}", json={"is_favorite": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "target_kind": "library_item",
            "target_id": str(item_id),
            "is_favorite": True,
        }
        assert client.get(f"{base}/tree/nodes/{node_id}").json()["data"]["is_favorite"]
        assert (
            client.put(
                f"{base}/favorites/folder/{node_id}", json={"is_favorite": True}
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"{base}/favorites/library_item/{uuid4()}", json={"is_favorite": True}
            ).status_code
            == 404
        )
