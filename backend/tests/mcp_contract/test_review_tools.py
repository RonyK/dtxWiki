"""MCP review-surface contract tests (item 11-c / T15b).

Dispatch-based, mirroring test_context_tools.py. Exercises the AI principal
mapping (P = conversation owner), the audience visibility, the soft-claim
conflict/expiry, idempotent retries, and the structural no-self-approval
guarantee (no MCP tool can move a review to approved).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp.protocol.tool_descriptors import TOOL_META, list_dtxwiki_tools
from backend.mcp.router import TOOL_HANDLERS, dispatch
from backend.mcp.scopes import TOOL_REQUIRED_ROLES, TOOL_REQUIRED_SCOPES
from backend.models.base import utc_now
from backend.models.mcp_chat import McpChatSession
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import ReviewDecisionRequest, WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.review_service import CLAIM_TTL_MINUTES, ReviewService

REVIEW_TOOL_NAMES = ("list_requested_changes", "submit_revision_for_review")


@pytest.fixture
def editor_actor(mcp_default_actor):
    # An MCP token carrying the review scopes; resolve_target_actor elevates the
    # mcp_client to owner inside its own (default) workspace, satisfying editor.
    # review-write gates submit_revision_for_review (review-mutating); the
    # mcp:write umbrella grants it in production.
    return mcp_default_actor(scopes=("review-read", "draft-write", "review-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id


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
# Registry coverage — both tools must appear in all five registries.
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
    """Risk 6 (structural): there is NO approve/reject/request_changes MCP tool.
    The only review-mutating tool is submit_revision_for_review, which returns
    pending. An AI can therefore never move a review to approved."""
    review_mutating = {"submit_revision_for_review"}
    forbidden = {"approve", "reject", "request_changes", "decide_review"}
    assert forbidden.isdisjoint(TOOL_HANDLERS)
    # The only review tool that writes is the revision submit (→ pending).
    assert "submit_revision_for_review" in TOOL_HANDLERS
    # list_requested_changes is read-only.
    assert TOOL_META["list_requested_changes"].read_only is True
    assert review_mutating.issubset(TOOL_HANDLERS)


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
    assert item.target.stable_url == (
        f"/p/{workspace_id}/{item.target.target_id}"
    )
    assert item.target.link_markdown == (
        f"[Self Routed Page]({item.target.stable_url})"
    )
    assert item.evidence is not None
    assert item.evidence.operations[0].document.reader_href == (
        f"/workspaces/{workspace_id}/context/pages/{item.target.target_id}"
    )


def test_list_requested_changes_owner_less_caller_is_empty(
    page_service, editor_actor
):
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


def test_submit_revision_returns_pending_never_approved(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "sr-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Revise Me Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "update": {
                "page_id": str(page_id),
                "base_version_id": base["version_id"],
                "base_content_hash": base["content_hash"],
                "update_mode": "full_rewrite",
                "body_markdown": "# Revise Me Page\n\nRevised body.",
                "reason": "AI revision",
            },
            "note": "fixed per request",
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.new_review_status == "pending"
    assert response.data.original_review_item_id == review_id
    # #7 one-pending-item invariant: the revision creates a NEW pending
    # ReviewItem on the revised draft, and the ORIGINAL is CLOSED (superseded)
    # so a human can't approve the stale original. Exactly one pending item
    # remains, pointing at the revised content.
    with page_service.session_factory() as session:
        original = session.get(ReviewItem, review_id)
        assert original.status == "superseded"
        assert original.claimed_by is None
        new_item = session.get(ReviewItem, response.data.new_review_item_id)
        assert new_item is not None
        assert new_item.status == "pending"
        assert new_item.change_set_id == response.data.new_change_set_id


def test_submit_revision_out_of_audience_is_not_found(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "oa-owner@example.com", workspace_id)
        other = _make_user(session, "oa-other@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        # Routed to `other` specifically (requester=other); owner is NOT in it.
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Not Yours Page",
            reviewer_id=other.id,
            audience_kind="requester",
        )
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "update": {
                "page_id": str(page_id),
                "base_version_id": base["version_id"],
                "base_content_hash": base["content_hash"],
                "update_mode": "full_rewrite",
                "body_markdown": "# Not Yours Page\n\nRevised.",
                "reason": "AI revision",
            },
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )
    assert response.envelope.result_status != "succeeded"
    assert response.envelope.error_code == "not_found"


def test_submit_revision_soft_claim_conflict(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "cc-owner@example.com", workspace_id)
        other = _make_user(session, "cc-other@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Contended Page",
            reviewer_id=owner.id,
            audience_kind="workspace_editors",
        )
        # Another principal already holds a live claim.
        review.claimed_by = other.id
        review.claimed_at = utc_now()
        session.flush()
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "update": {
                "page_id": str(page_id),
                "base_version_id": base["version_id"],
                "base_content_hash": base["content_hash"],
                "update_mode": "full_rewrite",
                "body_markdown": "# Contended Page\n\nMine.",
                "reason": "AI revision",
            },
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )
    assert response.envelope.result_status != "succeeded"
    assert response.envelope.error_code == "review_already_claimed"


def test_submit_revision_stale_claim_can_be_taken_over(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "st-owner@example.com", workspace_id)
        other = _make_user(session, "st-other@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Stale Claim Page",
            reviewer_id=owner.id,
            audience_kind="workspace_editors",
        )
        # An expired claim by another principal must not block (T9).
        review.claimed_by = other.id
        review.claimed_at = utc_now() - timedelta(minutes=CLAIM_TTL_MINUTES + 1)
        session.flush()
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "update": {
                "page_id": str(page_id),
                "base_version_id": base["version_id"],
                "base_content_hash": base["content_hash"],
                "update_mode": "full_rewrite",
                "body_markdown": "# Stale Claim Page\n\nMine now.",
                "reason": "AI revision",
            },
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )
    assert response.envelope.result_status == "succeeded"
    assert response.data.new_review_status == "pending"


def test_submit_revision_idempotent_retry(page_service, editor_actor):
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "id-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Idempotent Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    body = {
        "workspace_id": str(workspace_id),
        "review_item_id": str(review_id),
        "update": {
            "page_id": str(page_id),
            "base_version_id": base["version_id"],
            "base_content_hash": base["content_hash"],
            "update_mode": "full_rewrite",
            "body_markdown": "# Idempotent Page\n\nSame revision body.",
            "reason": "AI revision",
        },
    }
    first = dispatch(
        "submit_revision_for_review",
        body,
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )
    second = dispatch(
        "submit_revision_for_review",
        body,
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert first.envelope.result_status == "succeeded"
    assert second.envelope.result_status == "succeeded"
    # The retry returns the prior result, not a duplicate draft.
    assert second.data.deduplicated is True
    assert second.data.new_change_set_id == first.data.new_change_set_id
    assert second.data.new_review_item_id == first.data.new_review_item_id


def test_submit_revision_cross_principal_dedup_does_not_leak(
    page_service, editor_actor
):
    """HIGH #1: a dedup hit must be keyed on the per-conversation principal, NOT
    the shared MCP client actor_id. Owner A (in R's audience) submits a revision
    for R; owner B (DIFFERENT conversation, OUT of R's audience, SAME MCP client)
    submitting the same body+R must get not_found — never a dedup hit leaking
    A's new-draft identifiers."""
    workspace_id = _workspace_id(page_service)
    token_a = f"conv-A-{uuid4()}"
    token_b = f"conv-B-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner_a = _make_user(session, "xp-a@example.com", workspace_id)
        owner_b = _make_user(session, "xp-b@example.com", workspace_id, role="viewer")
        _seed_chat_session(session, workspace_id, token_a, owner_a.id)
        _seed_chat_session(session, workspace_id, token_b, owner_b.id)
        # Routed to A only (requester = the reviewer A); B is NOT in audience.
        review, draft = _seed_requested_change(
            session,
            workspace_id,
            "Cross Principal Page",
            reviewer_id=owner_a.id,
            audience_kind="requester",
        )
        review_id = review.id
        page_id = draft.page_id
        base = _page_base(session, page_id)

    body = {
        "workspace_id": str(workspace_id),
        "review_item_id": str(review_id),
        "update": {
            "page_id": str(page_id),
            "base_version_id": base["version_id"],
            "base_content_hash": base["content_hash"],
            "update_mode": "full_rewrite",
            "body_markdown": "# Cross Principal Page\n\nShared body.",
            "reason": "AI revision",
        },
    }
    # A (in audience) submits via conversation A.
    first = dispatch(
        "submit_revision_for_review",
        body,
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token_a,
    )
    assert first.envelope.result_status == "succeeded"

    # B (out of audience, SAME MCP client/actor) submits the SAME body via
    # conversation B → must be not_found, NOT a dedup hit of A's result.
    leaked = dispatch(
        "submit_revision_for_review",
        body,
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token_b,
    )
    assert leaked.envelope.result_status != "succeeded"
    assert leaked.envelope.error_code == "not_found"
    assert leaked.data is None


def test_submit_revision_rejects_target_mismatch(page_service, editor_actor):
    """P1: a revision routed for review R (page A) must not retarget the write to
    a DIFFERENT page B. The mismatched update.page_id is rejected BEFORE any
    claim/supersede, so R stays open and B is untouched."""
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "tm-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, _draft = _seed_requested_change(
            session,
            workspace_id,
            "Bound Page A",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        review_id = review.id
        # A SEPARATE page B the caller will try to retarget the revision onto.
        other = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Unrelated Page B",
                page_type="overview",
                body_markdown="# Unrelated Page B\n\nBody.",
                reason="separate page",
            ),
            actor_kind="human",
            actor_id=str(owner.id),
            actor_role="owner",
        )
        other_page_id = other.page_id
        base_b = _page_base(session, other_page_id)

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "update": {
                "page_id": str(other_page_id),  # MISMATCH: page B, not the bound A
                "base_version_id": base_b["version_id"],
                "base_content_hash": base_b["content_hash"],
                "update_mode": "full_rewrite",
                "body_markdown": "# Unrelated Page B\n\nHijacked.",
                "reason": "AI revision",
            },
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status != "succeeded"
    assert response.envelope.error_code == "validation_failed"
    assert response.data is None
    # The original request is NOT superseded and NOT claimed.
    with page_service.session_factory() as session:
        original = session.get(ReviewItem, review_id)
        assert original.status == "changes_requested"
        assert original.claimed_by is None


def test_submit_revision_rejects_create_source(page_service, editor_actor):
    """P1: `create` always writes a NEW page, so it can never match the review's
    bound page — it is rejected before any supersede."""
    workspace_id = _workspace_id(page_service)
    token = f"conv-{uuid4()}"
    with page_service.session_factory.begin() as session:
        owner = _make_user(session, "cr-owner@example.com", workspace_id)
        _seed_chat_session(session, workspace_id, token, owner.id)
        review, _ = _seed_requested_change(
            session,
            workspace_id,
            "Create Reject Page",
            reviewer_id=owner.id,
            audience_kind="requester",
        )
        review_id = review.id

    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(review_id),
            "create": {
                "title": "Brand New Page",
                "page_type": "overview",
                "body_markdown": "# Brand New Page\n\nBody.",
                "reason": "AI revision",
            },
        },
        actor=editor_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
        conversation_token=token,
    )

    assert response.envelope.result_status != "succeeded"
    assert response.envelope.error_code == "validation_failed"
    with page_service.session_factory() as session:
        assert session.get(ReviewItem, review_id).status == "changes_requested"


def test_submit_revision_requires_mcp_write_scope(page_service, mcp_default_actor):
    """The public read-only PAT scope cannot mutate review state."""
    workspace_id = _workspace_id(page_service)
    read_only_actor = mcp_default_actor(scopes=("mcp:read",))
    response = dispatch(
        "submit_revision_for_review",
        {
            "workspace_id": str(workspace_id),
            "review_item_id": str(uuid4()),
            "update": {
                "page_id": str(uuid4()),
                "update_mode": "full_rewrite",
                "body_markdown": "# x\n\ny",
                "reason": "AI revision",
            },
        },
        actor=read_only_actor,
        page_service=page_service,
        session_factory=page_service.session_factory,
    )
    assert response.envelope.result_status == "rejected_by_policy"
    assert response.envelope.error_code == "scope_denied"


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
