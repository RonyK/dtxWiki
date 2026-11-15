"""MCP review-surface contract tests (item 11-c / T15b).

Dispatch-based, mirroring test_context_tools.py. Exercises the AI principal
mapping (P = conversation owner), the audience visibility, the soft-claim
conflict/expiry, idempotent retries, and the structural no-self-approval
guarantee (no MCP tool can move a review to approved).
"""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp import review_tools
from backend.mcp.protocol.tool_descriptors import TOOL_META, list_dtxwiki_tools
from backend.mcp.router import TOOL_HANDLERS, dispatch
from backend.mcp.scopes import TOOL_REQUIRED_ROLES, TOOL_REQUIRED_SCOPES
from backend.models.base import utc_now
from backend.models.mcp_chat import McpChatSession
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import ReviewDecisionRequest, WriteDraftRequest
from backend.schemas.mcp.review_tools import ListRequestedChangesPayload
from backend.services.context_write_service import ContextWriteService
from backend.services.review_service import ReviewService

REVIEW_TOOL_NAMES = ("list_requested_changes",)


@pytest.fixture
def editor_actor(mcp_default_actor):
    # An MCP token carrying the review scopes; resolve_target_actor elevates the
    # mcp_client to owner inside its own (default) workspace, satisfying editor.
    return mcp_default_actor(scopes=("review-read", "draft-write", "review-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _make_user(session, email: str, workspace_id, role: str = "editor") -> User:
    user = User(
        email=email,
        password_hash="x",
        display_name=email.split("@", 1)[0],
    )
    session.add(user)
    session.flush()
    grant_workspace_membership(
        session, user_id=user.id, workspace_id=workspace_id, role=role
    )
    return user


def _seed_chat_session(session, workspace_id, token: str, owner_id):
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace_id)
    )
    chat = McpChatSession(
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=token,
        owner_user_id=owner_id,
        started_at=utc_now(),
        last_activity_at=utc_now(),
        tool_call_count=1,
        write_count=1,
        error_count=0,
        title_user_edited=False,
    )
    session.add(chat)
    session.flush()
    return chat


def _seed_requested_change(
    session,
    workspace_id,
    title: str,
    *,
    reviewer_id,
    audience_kind: str | None,
):
    """Create a draft, then request_changes routed per the given audience.

    For audience_kind='requester' the audience_user_id is snapshotted to the
    reviewer making the request, so pass reviewer_id = the intended principal.
    """
    draft = ContextWriteService().write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown=f"# {title}\n\nBody.",
            reason="seed requested change",
        ),
        actor_kind="human",
        actor_id=str(reviewer_id),
        actor_role="owner",
    )
    review = session.get(ReviewItem, draft.review_item_id)
    ReviewService().apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review.id,
        request=ReviewDecisionRequest(
            decision_type="request_changes",
            decision_reason="Add a citation.",
            audience_kind=audience_kind,
        ),
        actor_kind="human",
        actor_id=str(reviewer_id),
        actor_role="owner",
        actor_provenance="browser_session",
    )
    return review, draft


# --------------------------------------------------------------------------
# Registry coverage — the read tool must appear in all five registries.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", REVIEW_TOOL_NAMES)
def test_review_tools_registered_in_all_registries(tool_name):
    listed = {tool.name for tool in list_dtxwiki_tools()}
    assert tool_name in TOOL_HANDLERS
    assert tool_name in TOOL_REQUIRED_SCOPES
    assert tool_name in TOOL_REQUIRED_ROLES
    assert tool_name in TOOL_META
    assert tool_name in listed


def test_no_mcp_tool_can_approve_a_review():
    """Review decisions remain outside the MCP tool surface."""
    assert {"approve", "reject", "request_changes", "decide_review"}.isdisjoint(
        TOOL_HANDLERS
    )
    assert "submit_revision_for_review" not in TOOL_HANDLERS
    assert TOOL_META["list_requested_changes"].read_only is True


# --------------------------------------------------------------------------
# Visibility / principal mapping.
# --------------------------------------------------------------------------


def test_list_requested_changes_shows_request_to_owner_principal(
    page_service, editor_actor
):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "rt-owner@example.com", workspace_id)
        reviewer = _make_user(session, "rt-reviewer@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        # Routed to `reviewer` (not the owner) — must NOT appear for the owner.
        _seed_requested_change(
            session,
            workspace_id,
            "Reviewer Routed Page",
            reviewer_id=reviewer.id,
            audience_kind="requester",
        )
        # requester routes to the actor making the request; route to the owner
        # by having the OWNER be the reviewer who self-requests.
        review_self, _ = _seed_requested_change(
            session,
            workspace_id,
            "Self Routed Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        self_review_id = review_self.id

    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id)},
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    ids = {item.review_item_id for item in response.data.items}
    # The owner sees the request they routed to themselves...
    assert self_review_id in ids
    # ...the reason (fix instruction) and evidence are projected.
    item = next(i for i in response.data.items if i.review_item_id == self_review_id)
    assert item.reason == "Add a citation."
    assert item.audience_kind == "requester"
    assert item.target is not None
    assert item.target.stable_url == f"/p/{workspace_id}/{item.target.target_id}"
    assert item.target.link_markdown == (
        f"[Self Routed Page]({item.target.stable_url})"
    )
    assert item.evidence is not None
    assert item.evidence.operations[0].document.reader_href == (
        f"/workspaces/{workspace_id}/context/pages/{item.target.target_id}"
    )


def test_requested_change_version_link_uses_authorized_document_id(monkeypatch):
    """A version UUID identifies evidence, while its link must open the document."""
    workspace_id, page_id, version_id, user_id = (uuid4() for _ in range(4))
    data = ListRequestedChangesPayload.model_validate(
        {
            "items": [
                {
                    "review_item_id": uuid4(),
                    "change_set_id": uuid4(),
                    "target": {
                        "target_type": "context_page_version",
                        "target_id": version_id,
                    },
                    "evidence": {
                        "operations": [
                            {
                                "operation_id": uuid4(),
                                "target_type": "context_page_version",
                                "target_id": version_id,
                                "operation": "update",
                                "document": {
                                    "document_kind": "context",
                                    "document_id": page_id,
                                    "title": "Reviewed document",
                                    "can_open": True,
                                },
                            }
                        ]
                    },
                    "claim": {"claimed": False, "claimed_by_me": False},
                }
            ]
        }
    )
    monkeypatch.setattr(
        review_tools, "resolve_effective_principal", lambda *a, **k: user_id
    )
    monkeypatch.setattr(review_tools, "_principal_role", lambda *a, **k: "owner")
    ctx = SimpleNamespace(
        actor=ActorContext(actor_kind="human", actor_id=str(user_id), user_id=user_id),
        workspace_id=workspace_id,
        conversation_token=None,
        session_factory=lambda: nullcontext(None),
        services=SimpleNamespace(
            review=SimpleNamespace(
                list_requested_changes_for_principal=lambda **k: data
            )
        ),
    )
    result = review_tools.list_requested_changes({"workspace_id": workspace_id}, ctx)
    assert result.envelope.result_status == "succeeded"
    target = result.data.items[0].target
    assert target.target_id == version_id
    assert target.stable_url == f"/p/{workspace_id}/{page_id}"
    assert target.link_markdown == f"[Reviewed document](/p/{workspace_id}/{page_id})"


def test_list_requested_changes_owner_less_caller_is_empty(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        reviewer = _make_user(session, "rt-ownerless@example.com", workspace_id)
        # Chat session with NO owner → no audience principal.
        _seed_chat_session(session, workspace_id, token, None)
        _seed_requested_change(
            session,
            workspace_id,
            "Broadcast Page",
            reviewer_id=reviewer.id,
            audience_kind="workspace_editors",
        )

    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id)},
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.items == []


def test_list_requested_changes_requires_review_read_scope(
    page_service, mcp_default_actor
):
    workspace_id = _workspace_id(page_service)
    no_scope_actor = mcp_default_actor(scopes=("guide", "search", "read"))
    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id)},
        actor=no_scope_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
    )
    assert response.envelope.result_status == "rejected_by_policy"
    assert response.envelope.error_code == "scope_denied"


# --------------------------------------------------------------------------
# submit_revision_for_review.
# --------------------------------------------------------------------------


def test_list_requested_changes_resolves_principal_on_first_call(
    page_service, mcp_default_actor
):
    """P2: the trace row is persisted AFTER dispatch, so on the FIRST call of a
    fresh conversation no chat session exists yet. The principal must fall back
    to the caller's own user (the owner the row will be created with) rather than
    returning an empty queue."""
    workspace_id = _workspace_id(page_service)
    token = f"conv-first-{uuid4()}"  # deliberately NOT seeded
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "fc-owner@example.com", workspace_id)
        review, _ = _seed_requested_change(
            session,
            workspace_id,
            "First Call Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        owner_id = owner.id
        review_id = review.id

    actor = mcp_default_actor(
        scopes=("review-read", "draft-write", "review-write"), user_id=owner_id
    )
    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id)},
        actor=actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,  # no chat row exists for it yet
    )

    assert response.envelope.result_status == "succeeded"
    assert review_id in {item.review_item_id for item in response.data.items}


def test_list_requested_changes_hides_unsupported_targets(page_service, editor_actor):
    """P2: an item the revision tool cannot act on (no context-page op, e.g. an
    agent-page review) must not be advertised in the AI queue even when routed to
    the principal — otherwise it looks fixable but every submit is rejected."""
    from backend.models.change import ChangeOperation

    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "us-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        supported, _ = _seed_requested_change(
            session,
            workspace_id,
            "Supported Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        unsupported, _ = _seed_requested_change(
            session,
            workspace_id,
            "Unsupported Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        # Make the second review's change set carry only a non context-page
        # target, the way an agent-page review would.
        for op in session.scalars(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == unsupported.change_set_id
            )
        ).all():
            op.target_type = "agent_page"
        supported_id = supported.id
        unsupported_id = unsupported.id

    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id)},
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    ids = {item.review_item_id for item in response.data.items}
    assert supported_id in ids
    assert unsupported_id not in ids


def test_list_requested_changes_advances_past_fully_filtered_page(
    page_service, editor_actor
):
    """P2 follow-up: a page that filters entirely down to unsupported targets
    must not return items=[] with a live next_cursor (a client reads that as
    end-of-list and strands the actionable items behind it). The server advances
    past the filtered page so the supported item is surfaced in the same call."""
    from backend.models.change import ChangeOperation

    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "rf-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        supported, _ = _seed_requested_change(
            session,
            workspace_id,
            "Refill Supported",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        supported_id = supported.id
        unsupported = []
        for idx in range(2):
            review, _ = _seed_requested_change(
                session,
                workspace_id,
                f"Refill Unsupported {idx}",
                reviewer_id=owner.id,
                audience_kind="requester",
            )
            for op in session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.change_set_id == review.change_set_id
                )
            ).all():
                op.target_type = "agent_page"
            unsupported.append(review)
        # Force the page split deterministically (list_reviews orders created_at
        # DESC): the two unsupported rows are NEWEST → fill page 1 at limit=2; the
        # supported row is OLDEST → sits alone on page 2.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        supported.created_at = base
        unsupported[0].created_at = base + timedelta(hours=1)
        unsupported[1].created_at = base + timedelta(hours=2)

    response = dispatch(
        "list_requested_changes",
        {"workspace_id": str(workspace_id), "limit": 2},
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    # The supported item is surfaced even though page 1 filtered to empty — a
    # verbatim empty-items+non-null-cursor first page would have stranded it.
    assert supported_id in {item.review_item_id for item in response.data.items}


def _page_base(session, page_id):
    from backend.models.context import ContextPage, ContextPageVersion

    page = session.get(ContextPage, page_id)
    version_id = page.latest_draft_version_id or page.current_version_id
    version = session.get(ContextPageVersion, version_id)
    return {
        "version_id": str(version.id),
        "content_hash": version.content_hash,
    }
