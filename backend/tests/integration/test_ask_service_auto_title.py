from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.ask import AskSession, AskTurn, ContextIngressProposal
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.schemas.context_ingress import (
    AskTurnPromoteRequest,
    ContextIngressAcceptRequest,
)
from backend.schemas.read import RankedResult, SearchPayload
from backend.services.ask_service import AskService
from backend.services.context_ingress_service import ContextIngressService
from backend.services.read_errors import ReadNotFoundError

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


class FakeSearchService:
    def __init__(self, context_page_id: UUID | None = None) -> None:
        self.context_page_id = context_page_id

    def search(self, **kwargs) -> SearchPayload:
        if kwargs["scope"] != "context_page" or self.context_page_id is None:
            return SearchPayload(
                ranked_results=[],
                supported_retrieval_modes=["keyword"],
                permission_note=None,
                next_cursor=None,
            )
        return SearchPayload(
            ranked_results=[
                RankedResult(
                    id=self.context_page_id,
                    type="context_page",
                    title="Deployment Plan",
                    snippet="Rollout guidance",
                    score=1.0,
                    citation_available=True,
                )
            ],
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
        )


def _default_workspace(session) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace


def _seed_context_page(session, workspace_id: UUID, title: str) -> ContextPage:
    # Published page: only a broadly-visible (published) page title may anchor
    # the auto-generated session title (H-01 — list_sessions is workspace-wide).
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug="deployment-plan",
        title=title,
        aliases=[],
        tags=[],
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}\n\nPublished body.",
        content_hash="auto-title-published-v1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id=ACTOR_ID,
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    return page


def test_ask_session_without_context_parent_defaults_to_inbox(session_factory):
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        inbox = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "inbox",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        assert inbox is not None

        detail = AskService(search_service=FakeSearchService()).create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )

    assert detail.default_context_parent_tree_node_id == inbox.id


def test_ask_session_id_is_opaque_across_workspaces(session_factory):
    service = AskService(search_service=FakeSearchService())

    with session_factory.begin() as session:
        requested_workspace = _default_workspace(session)
        foreign_workspace = Workspace(
            slug="foreign-ask-session",
            title="Foreign Ask Session",
            name="Foreign Ask Session",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_node = WorkspaceTreeNode(
            workspace_id=foreign_workspace.id,
            section="ask",
            node_kind="folder",
            sort_key="0000",
            title="Foreign Ask Session",
            slug="foreign-ask-session-node",
            visibility_status="visible",
        )
        session.add(foreign_node)
        session.flush()
        foreign_session = AskSession(
            workspace_id=foreign_workspace.id,
            title="Foreign session title must stay opaque",
            actor_kind="human",
            placement_tree_node_id=foreign_node.id,
        )
        session.add(foreign_session)
        session.flush()

        with pytest.raises(ReadNotFoundError) as missing_error:
            service.get_session(
                session=session,
                workspace_id=requested_workspace.id,
                ask_session_id=uuid4(),
            )
        with pytest.raises(ReadNotFoundError) as foreign_error:
            service.get_session(
                session=session,
                workspace_id=requested_workspace.id,
                ask_session_id=foreign_session.id,
            )

    assert str(foreign_error.value) == str(missing_error.value)
    assert str(foreign_workspace.id) not in str(foreign_error.value)


def test_ask_ingress_without_placement_creates_page_in_inbox(session_factory):
    ask_service = AskService(search_service=FakeSearchService())
    ingress_service = ContextIngressService()

    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        ask_detail = ask_service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        inbox_id = ask_detail.default_context_parent_tree_node_id
        assert inbox_id is not None
        turn = AskTurn(
            workspace_id=workspace.id,
            ask_session_id=ask_detail.id,
            turn_no=1,
            user_message="Capture this answer",
            assistant_message="# Captured Answer\n\nBody.",
            topic_tags=[],
            recommended_placement_tree_node_id=inbox_id,
            placement_confidence="medium",
            promotion_state="not_promoted",
            retrieval_trace=[],
            tried_lanes=[],
            fallback_disclosure=False,
            warning_codes=[],
        )
        session.add(turn)
        session.flush()
        proposal = ingress_service.promote_ask_turn(
            session=session,
            workspace_id=workspace.id,
            ask_turn_id=turn.id,
            request=AskTurnPromoteRequest(
                save_mode="create_page",
                page_type="overview",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        accepted = ingress_service.accept_proposal(
            session=session,
            workspace_id=workspace.id,
            proposal_id=proposal.id,
            request=ContextIngressAcceptRequest(
                final_save_mode="create_page",
                final_parent_node_id=proposal.proposed_parent_tree_node_id,
                final_title="Captured Answer",
                final_page_type="overview",
                review_required=False,
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        placement = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == accepted.context_page_id,
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )

    assert proposal.proposed_parent_tree_node_id == inbox_id
    assert placement is not None
    assert placement.parent_id == inbox_id


def test_promote_ask_turn_hides_foreign_turn_without_mutation(session_factory):
    ingress_service = ContextIngressService()

    with session_factory.begin() as session:
        requested_workspace = _default_workspace(session)
        foreign_workspace = Workspace(
            slug="ask-ingress-foreign",
            title="Ask ingress foreign",
            name="Ask ingress foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_parent = WorkspaceTreeNode(
            workspace_id=foreign_workspace.id,
            section="context",
            node_kind="folder",
            sort_key="0000",
            title="Context",
            slug="context",
            is_system_node=True,
        )
        session.add(foreign_parent)
        session.flush()
        foreign_session = AskSession(
            workspace_id=foreign_workspace.id,
            title="Foreign session",
            actor_kind="human",
            default_context_parent_tree_node_id=foreign_parent.id,
            placement_tree_node_id=foreign_parent.id,
        )
        session.add(foreign_session)
        session.flush()
        foreign_turn = AskTurn(
            workspace_id=foreign_workspace.id,
            ask_session_id=foreign_session.id,
            turn_no=1,
            user_message="Keep this foreign",
            assistant_message="Foreign answer",
        )
        session.add(foreign_turn)
        session.flush()

        with pytest.raises(ReadNotFoundError) as denied:
            ingress_service.promote_ask_turn(
                session=session,
                workspace_id=requested_workspace.id,
                ask_turn_id=foreign_turn.id,
                request=AskTurnPromoteRequest(
                    save_mode="create_page",
                    page_type="overview",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        created_proposal = session.scalar(
            select(ContextIngressProposal.id).where(
                ContextIngressProposal.source_ask_turn_id == foreign_turn.id
            )
        )

    assert denied.value.error_code == "not_found"
    assert foreign_turn.promotion_state == "not_promoted"
    assert created_proposal is None


def test_ask_auto_title_uses_intent_then_repeated_context_anchor(session_factory):
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        page = _seed_context_page(session, workspace.id, "Deployment Plan")
        service = AskService(search_service=FakeSearchService(page.id))
        detail = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        first, warnings = service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=detail.id,
            request=AskTurnCreate(
                user_message=(
                    "How should we roll out dashboard activity filters tomorrow?"
                )
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        ask_session = session.get(AskSession, detail.id)
        assert ask_session is not None
        assert warnings == []
        assert first.turn_no == 1
        assert (
            ask_session.title == "How should we roll out dashboard activity filters..."
        )

        service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=detail.id,
            request=AskTurnCreate(user_message="What are the follow-up risks?"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

        node = session.get(WorkspaceTreeNode, ask_session.placement_tree_node_id)
        assert ask_session.title == (
            "Deployment Plan: How should we roll out dashboard activity filters..."
        )
        assert node is not None
        assert node.title == ask_session.title


def test_ask_auto_title_preserves_user_supplied_title(session_factory, monkeypatch):
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        page = _seed_context_page(session, workspace.id, "Deployment Plan")
        service = AskService(search_service=FakeSearchService(page.id))
        detail = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(title="Incident analysis"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        original_lock = service._lock_session_for_turn_insert
        locked_session_ids = []

        def tracked_lock(*, session, ask_session_id):
            locked_session_ids.append(ask_session_id)
            return original_lock(
                session=session,
                ask_session_id=ask_session_id,
            )

        monkeypatch.setattr(
            service,
            "_lock_session_for_turn_insert",
            tracked_lock,
        )
        service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=detail.id,
            request=AskTurnCreate(user_message="How should we investigate this?"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

        ask_session = session.get(AskSession, detail.id)
        assert ask_session is not None
        assert ask_session.title == "Incident analysis"
        assert ask_session.title_user_edited is True
        assert locked_session_ids == [detail.id]


def test_ask_auto_title_uses_topic_anchor_when_context_anchor_absent(session_factory):
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        service = AskService(search_service=FakeSearchService())
        detail = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(default_topic_tags=["Retention"]),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=detail.id,
            request=AskTurnCreate(user_message="Evaluate retention policy"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

        ask_session = session.get(AskSession, detail.id)
        assert ask_session is not None
        assert ask_session.title == "Retention: Evaluate retention policy"


def test_ask_auto_title_space_boundary_and_long_anchor_keep_intent(session_factory):
    with session_factory.begin() as session:
        workspace = _default_workspace(session)
        page = _seed_context_page(session, workspace.id, "a" * 254)
        for anchor_id, question in (
            (None, "a" * 95 + " bbb ccc"),
            (page.id, "Explain the deployment"),
        ):
            service = AskService(search_service=FakeSearchService(anchor_id))
            detail = service.create_session(
                session=session,
                workspace_id=workspace.id,
                request=AskSessionCreate(),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
            service.create_turn(
                session=session,
                workspace_id=workspace.id,
                ask_session_id=detail.id,
                request=AskTurnCreate(user_message=question),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
            if anchor_id is not None:
                service.create_turn(
                    session=session,
                    workspace_id=workspace.id,
                    ask_session_id=detail.id,
                    request=AskTurnCreate(user_message="What follows this deployment?"),
                    actor_kind="human",
                    actor_id=ACTOR_ID,
                )
            stored = session.get(AskSession, detail.id)
            node = session.get(WorkspaceTreeNode, stored.placement_tree_node_id)
            assert 1 <= len(stored.title) <= 256
            assert stored.title == stored.title.strip() == node.title
            if anchor_id is None:
                assert stored.title == "a" * 95
            else:
                assert stored.title.endswith(": Explain the deployment")
