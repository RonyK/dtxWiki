"""T15 review collaboration: audience routing, visibility/scope, soft-claim."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import (
    ChatOwnerUnresolvableError,
    ReadNotFoundError,
    ReadValidationError,
    ReviewAlreadyClaimedError,
    ReviewDecisionIdConflictError,
)
from backend.services.review_service import CLAIM_TTL_MINUTES, ReviewService


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


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


def _seed_review(session, workspace_id, title: str, actor_id: str) -> ReviewItem:
    draft = ContextWriteService().write_draft(
        session=session,
        workspace_id=workspace_id,
        request=WriteDraftRequest(
            title=title,
            page_type="overview",
            body_markdown=f"# {title}\n\nBody.",
            reason="seed review",
        ),
        actor_kind="human",
        actor_id=actor_id,
        actor_role="owner",
    )
    return session.get(ReviewItem, draft.review_item_id)


def _actor(user: User, workspace_id, role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(user.id),
        user_id=user.id,
        workspace_id=workspace_id,
        workspace_role=role,
        role=role,
        token_kind="session",
        provenance="browser_session",
    )


def _request_changes(service, session, workspace_id, review, *, audience_kind, actor):
    return service.apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review.id,
        request=ReviewDecisionRequest(
            decision_type="request_changes",
            decision_reason="Please revise.",
            audience_kind=audience_kind,
        ),
        actor_kind="human",
        actor_id=str(actor),
        actor_role="owner",
        actor_provenance="browser_session",
    )


def test_get_review_detail_not_visible_to_uninvolved_viewer(session_factory):
    # Codex #6: detail must not be a by-id bypass of the list scope clamp — an
    # uninvolved viewer must not retrieve a review (no governance-internal leak).
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "author6b@example.com", ws.id)  # editor
        viewer = _make_user(session, "viewer6b@example.com", ws.id, role="viewer")
        review = _seed_review(session, ws.id, "Hidden Review", str(author.id))

        with pytest.raises(ReadNotFoundError):
            service.get_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                actor_user_id=viewer.id,
                actor_role="viewer",
            )

        # An editor still gets the full detail.
        detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_user_id=author.id,
            actor_role="editor",
        )
        assert detail.id == review.id


def test_cross_workspace_review_id_is_opaque_without_decision_or_audit_mutation(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        local_owner = _make_user(
            session,
            "cross-workspace-local-owner@example.com",
            workspace.id,
            role="owner",
        )
        foreign_workspace = Workspace(
            slug="cross-workspace-review-foreign",
            title="Foreign review workspace",
            name="Foreign review workspace",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_change_set = ChangeSet(
            workspace_id=foreign_workspace.id,
            reason="foreign review",
            trigger_type="mcp_write_draft",
            actor_type="human",
            actor_id=uuid4(),
        )
        session.add(foreign_change_set)
        session.flush()
        foreign_review = ReviewItem(
            workspace_id=foreign_workspace.id,
            change_set_id=foreign_change_set.id,
            review_type="context_update",
            status="pending",
            risk_level="low",
        )
        session.add(foreign_review)
        session.flush()

        audit_count_before = session.scalar(select(func.count(AuditEvent.id)))
        decision_count_before = session.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.review_item_id == foreign_review.id
            )
        )
        decision_request = ReviewDecisionRequest(decision_type="approve")

        with pytest.raises(ReadNotFoundError) as missing_error:
            service.get_review(
                session=session,
                workspace_id=workspace.id,
                review_item_id=uuid4(),
                actor_user_id=local_owner.id,
                actor_role="owner",
            )
        with pytest.raises(ReadNotFoundError) as foreign_error:
            service.get_review(
                session=session,
                workspace_id=workspace.id,
                review_item_id=foreign_review.id,
                actor_user_id=local_owner.id,
                actor_role="owner",
            )
        with pytest.raises(ReadNotFoundError) as foreign_decision_error:
            service.apply_decision(
                session=session,
                workspace_id=workspace.id,
                review_item_id=foreign_review.id,
                request=decision_request,
                actor_kind="human",
                actor_id=str(local_owner.id),
                actor_user_id=local_owner.id,
                actor_role="owner",
                actor_provenance="browser_session",
            )

        session.refresh(foreign_review)
        session.refresh(foreign_change_set)
        assert foreign_error.value.error_code == "not_found"
        assert str(foreign_error.value) == str(missing_error.value)
        assert str(foreign_decision_error.value) == str(missing_error.value)
        assert foreign_review.status == "pending"
        assert foreign_change_set.status == "draft_created"
        assert session.scalar(select(func.count(AuditEvent.id))) == audit_count_before
        assert (
            session.scalar(
                select(func.count(ReviewDecision.id)).where(
                    ReviewDecision.review_item_id == foreign_review.id
                )
            )
            == decision_count_before
        )


def test_review_evidence_does_not_hydrate_foreign_version_metadata(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        local_author = _make_user(
            session,
            "foreign-evidence-local-author@example.com",
            workspace.id,
            role="owner",
        )
        local_review = _seed_review(
            session,
            workspace.id,
            "Local evidence target",
            str(local_author.id),
        )

        foreign_workspace = Workspace(
            slug="foreign-evidence-version-workspace",
            title="Foreign evidence workspace",
            name="Foreign evidence workspace",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-evidence-target",
            title="Foreign evidence target",
            page_type="overview",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=47,
            body_md="# Foreign evidence target",
            content_hash="foreign-evidence-version",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=str(uuid4()),
        )
        session.add(foreign_version)
        local_operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == local_review.change_set_id
            )
        )
        assert local_operation is not None
        local_operation.diff_ref = f"context_page_version:{foreign_version.id}"
        session.flush()

        detail = service.get_review(
            session=session,
            workspace_id=workspace.id,
            review_item_id=local_review.id,
            actor_user_id=local_author.id,
            actor_role="owner",
            actor=_actor(local_author, workspace.id, "owner"),
        )

        assert detail.evidence is not None
        operation = detail.evidence.operations[0]
        assert operation.target_title == "Local evidence target"
        assert operation.target_slug == "local-evidence-target"
        assert operation.target_version_no is None
        assert operation.document.document_id == local_operation.target_id
        assert operation.document.can_open is True
        assert operation.content.can_read is False
        assert operation.content.candidate_version_id is None
        assert operation.content.unavailable_reason == "candidate_unavailable"
        assert detail.actions.approve.allowed is False
        assert detail.actions.approve.reason_code == "stale_candidate"


def test_recent_decisions_hidden_from_uninvolved_viewers(session_factory):
    # Codex #6: a viewer sees only decisions routed to them, never the
    # workspace-wide approve/reject broadcast feed.
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "author6c@example.com", ws.id)  # editor
        owner = _make_user(session, "owner6c@example.com", ws.id, role="owner")
        viewer = _make_user(session, "viewer6c@example.com", ws.id, role="viewer")
        review = _seed_review(session, ws.id, "Approved Page", str(author.id))
        service.apply_decision(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=str(owner.id),
            actor_user_id=owner.id,
            actor_role="owner",
            actor_provenance="browser_session",
        )

        owner_recent = service.list_recent_decisions(
            session=session,
            workspace_id=ws.id,
            audience_for=owner.id,
            actor_role="owner",
        )
        viewer_recent = service.list_recent_decisions(
            session=session,
            workspace_id=ws.id,
            audience_for=viewer.id,
            actor_role="viewer",
        )

    assert any(d.review_item_id == review.id for d in owner_recent)
    assert viewer_recent == []


def test_recent_decision_targets_are_batched_and_keep_summary_separate(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "recent-author@example.com", ws.id)
        owner = _make_user(session, "recent-owner@example.com", ws.id, role="owner")
        reviews = [
            _seed_review(session, ws.id, f"Recent target {index}", str(author.id))
            for index in range(6)
        ]
        review_ids = [review.id for review in reviews]
        for review in reviews:
            service.apply_decision(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=str(owner.id),
                actor_user_id=owner.id,
                actor_role="owner",
                actor_provenance="browser_session",
            )
        session.flush()

        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_query)
        try:
            recent = service.list_recent_decisions(
                session=session,
                workspace_id=ws.id,
                limit=len(reviews),
                audience_for=owner.id,
                actor_role="owner",
                actor=_actor(owner, ws.id, "owner"),
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_query)

    # One query each for decision/review rows, human names, operations,
    # Context versions, and Context pages. The count is independent of N.
    assert query_count == 5
    by_review_id = {item.review_item_id: item for item in recent}
    assert set(by_review_id) == set(review_ids)
    for index, review_id in enumerate(review_ids):
        item = by_review_id[review_id]
        assert item.target_title == f"Recent target {index}"
        assert item.review_target_title == item.target_title
        assert item.target_slug == f"recent-target-{index}"
        assert item.target_version_no == 1
        assert item.decision_summary == "context draft requires review"


def test_recent_headless_rejected_target_preview_follows_detail_visibility(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "headless-author@example.com", ws.id)
        owner = _make_user(session, "headless-owner@example.com", ws.id, role="owner")
        review = _seed_review(session, ws.id, "Headless rejected", str(author.id))
        # Reject the pending review directly. Routing a changes-request to the
        # requester first would put the review in a state only that requester
        # may decide, which is a different rule than the one under test here.
        service.apply_decision(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=str(owner.id),
            actor_user_id=owner.id,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.change_set_id == review.change_set_id
            )
        )
        assert version is not None
        page = session.get(ContextPage, version.page_id)
        assert page is not None
        assert page.current_version_id is None
        assert page.latest_draft_version_id is None
        assert page.current_published_version_id is None

        editor_recent = service.list_recent_decisions(
            session=session,
            workspace_id=ws.id,
            audience_for=author.id,
            actor_role="editor",
            actor=_actor(author, ws.id, "editor"),
        )
        viewer_recent = service.list_recent_decisions(
            session=session,
            workspace_id=ws.id,
            audience_for=author.id,
            actor_role="viewer",
            actor=_actor(author, ws.id, "viewer"),
        )

    editor_reject = next(
        item for item in editor_recent if item.decision_type == "reject"
    )
    assert editor_reject.target_title == "Headless rejected"
    assert editor_reject.target_slug == "headless-rejected"
    assert editor_reject.target_version_no == 1

    # The rejection left no visible head, so the title exists only for actors
    # who may read drafts. A viewer either does not see the row at all (a
    # broadcast decision is not in their personal feed) or sees it redacted --
    # what must never happen is the title reaching them.
    assert all(item.target_title is None for item in viewer_recent)
    assert all(item.target_slug is None for item in viewer_recent)


def test_count_reviews_hides_workspace_aggregate_from_viewers(session_factory):
    # Codex #6: GET /reviews/counts must not leak workspace-wide review state to
    # viewer principals — only editor-or-higher sees the aggregate; viewers keep
    # only their personal queue (to_me/from_me).
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "author6@example.com", ws.id)
        _seed_review(session, ws.id, "Counted Page", str(author.id))

        editor_counts = service.count_reviews(
            session=session, workspace_id=ws.id, actor_role="editor"
        )
        viewer_counts = service.count_reviews(
            session=session, workspace_id=ws.id, actor_role="viewer"
        )

    # Editor sees the real workspace aggregate (the seeded pending review).
    assert editor_counts.pending >= 1
    assert sum(editor_counts.risk_counts.model_dump().values()) >= 1

    # Viewer gets the workspace aggregate zeroed.
    assert viewer_counts.pending == 0
    assert viewer_counts.changes_requested == 0
    assert viewer_counts.resolved == 0
    assert viewer_counts.risk_counts.model_dump() == {"high": 0, "medium": 0, "low": 0}
    assert viewer_counts.auto_approve_candidate_count == 0
    for bucket in ("pending", "changes_requested", "resolved"):
        breakdown = getattr(viewer_counts.status_by_risk, bucket)
        assert breakdown.high == breakdown.medium == breakdown.low == 0


def test_requester_audience_snapshots_actor_and_routes_to_me(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        requester = _make_user(session, "requester@example.com", ws.id)
        other = _make_user(session, "other@example.com", ws.id)
        review = _seed_review(session, ws.id, "Requester Page", str(requester.id))

        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="requester",
            actor=requester.id,
        )

        to_requester = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=requester.id,
            scope="to_me",
            actor_role="editor",
        )
        to_other = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=other.id,
            scope="to_me",
            actor_role="editor",
        )

    # requester sees their own self-routed request; another editor does NOT
    # (requester kind is a named individual, not a broadcast).
    assert [item.id for item in to_requester.items] == [review.id]
    assert to_requester.items[0].current_audience_kind == "requester"
    assert to_other.items == []


def test_review_detail_capabilities_follow_status_role_and_audience(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        requester = _make_user(session, "cap-req@example.com", ws.id)
        outsider = _make_user(session, "cap-out@example.com", ws.id)
        review = _seed_review(session, ws.id, "Capability Page", str(requester.id))

        pending = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=requester.id,
            actor_role="editor",
            actor=_actor(requester, ws.id, "editor"),
        )
        pending_detail = service.get_review(
            session, ws.id, review.id, actor_user_id=requester.id,
            actor_role="editor", actor=_actor(requester, ws.id, "editor"),
        )
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="requester",
            actor=requester.id,
        )
        routed = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=requester.id,
            scope="to_me",
            actor_role="editor",
            actor=_actor(requester, ws.id, "editor"),
        )
        outsider_view = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=outsider.id,
            actor_role="editor",
            actor=_actor(outsider, ws.id, "editor"),
        )
        routed_detail = service.get_review(
            session, ws.id, review.id, actor_user_id=requester.id,
            actor_role="editor", actor=_actor(requester, ws.id, "editor"),
        )
        outsider_detail = service.get_review(
            session, ws.id, review.id, actor_user_id=outsider.id,
            actor_role="editor", actor=_actor(outsider, ws.id, "editor"),
        )

    assert pending.items[0].can_decide is True
    assert pending.items[0].can_claim is False
    assert routed.items[0].can_decide is True
    assert routed.items[0].can_claim is True
    assert outsider_view.items[0].can_decide is False
    assert outsider_view.items[0].can_claim is False
    # Rows stay cheap; the single-review read carries the decision preflight.
    assert pending.items[0].actions is pending.items[0].reject_effect is None
    assert pending_detail.actions.approve.allowed is True
    assert routed_detail.actions.edit_directly.allowed is True
    assert outsider_detail.actions.approve.allowed is False
    assert outsider_detail.actions.approve.reason_code == "not_in_audience"


def test_decision_receipt_replay_preserves_later_round_and_rejects_reuse(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        owner = _make_user(session, "receipt-owner@example.com", workspace.id, "owner")
        other = _make_user(session, "receipt-other@example.com", workspace.id)
        review = _seed_review(session, workspace.id, "Receipt target", str(owner.id))
        unrelated = _seed_review(session, workspace.id, "Receipt other", str(owner.id))
        request = ReviewDecisionRequest(
            decision_id=uuid4(),
            decision_type="request_changes",
            decision_reason="  Original request.  ",
            audience_kind="requester",
        )

        def decide(request_body, *, target=review, acting=owner):
            return service.apply_decision(
                session=session,
                workspace_id=workspace.id,
                review_item_id=target.id,
                request=request_body,
                actor_kind="human",
                actor_id=str(acting.id),
                actor_user_id=acting.id,
                actor_role="owner" if acting is owner else "editor",
                actor_provenance="browser_session",
                actor=_actor(
                    acting, workspace.id, "owner" if acting is owner else "editor"
                ),
            )

        first = decide(request)
        assert first.decisions[0].decision_id == request.decision_id
        later = decide(ReviewDecisionRequest(
            decision_id=uuid4(),
            decision_type="request_changes",
            decision_reason="A later routing round.",
            audience_kind="workspace_editors",
        ))
        service.claim_review(
            session, workspace.id, review.id,
            actor_kind="human", actor_id=str(other.id), actor_role="editor",
        )
        replay = decide(request)
        assert replay.current_audience_kind == later.current_audience_kind
        assert replay.claimed_by == other.id
        assert len(replay.decisions) == 2
        assert session.scalar(select(func.count(ReviewDecision.id))) == 2
        assert session.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "review.decision"
        )) == 2

        for changed, target, acting in [
            (
                request.model_copy(update={"decision_reason": "Different payload"}),
                review, owner,
            ),
            (request, unrelated, owner),
            (request, review, other),
        ]:
            with pytest.raises(ReviewDecisionIdConflictError):
                decide(changed, target=target, acting=acting)
        assert unrelated.status == "pending"
        assert review.claimed_by == other.id
        assert session.scalar(select(func.count(ReviewDecision.id))) == 2


def test_review_decision_payload_includes_actor_display_name(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "display@example.com", ws.id)
        review = _seed_review(session, ws.id, "Display Page", str(reviewer.id))

        result = _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )

    assert result.decisions[0].actor_display_name == "display"


def test_review_counts_reuse_audience_predicates(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "count-reviewer@example.com", ws.id)
        other = _make_user(session, "count-other@example.com", ws.id)
        review_to_reviewer = _seed_review(
            session, ws.id, "Count Direct", str(reviewer.id)
        )
        review_to_editors = _seed_review(
            session, ws.id, "Count Broadcast", str(other.id)
        )
        _request_changes(
            service,
            session,
            ws.id,
            review_to_reviewer,
            audience_kind="requester",
            actor=reviewer.id,
        )
        _request_changes(
            service,
            session,
            ws.id,
            review_to_editors,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )

        to_me = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=reviewer.id,
            scope="to_me",
            actor_role="editor",
        )
        from_me = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=reviewer.id,
            scope="from_me",
            actor_role="editor",
        )
        counts = service.count_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=reviewer.id,
            actor_role="editor",
        )

    assert counts.to_me == len(to_me.items) == 2
    assert counts.from_me == len(from_me.items) == 2


def test_revision_submitted_audit_is_projected_into_decision_ledger(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "revision@example.com", ws.id)
        review = _seed_review(session, ws.id, "Revision Ledger", str(reviewer.id))
        session.add(
            AuditEvent(
                workspace_id=ws.id,
                actor_kind="mcp_client",
                actor_id="dev-mcp-client",
                actor_type="mcp_client",
                action="review.revision_submitted",
                target_kind="review_item",
                target_type="review_item",
                target_id=review.id,
                payload={"principal": str(reviewer.id)},
            )
        )
        session.flush()

        detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_role="owner",
        )

    assert detail.decisions[0].decision_type == "revision_submitted"
    assert detail.decisions[0].actor_display_name == "revision"
    assert detail.decisions[0].resulting_change_set_status == "revision_pending"


def test_chat_owner_audience_resolves_via_origin_chat_session(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        owner = _make_user(session, "chatowner@example.com", ws.id)
        reviewer = _make_user(session, "reviewer@example.com", ws.id)
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == ws.id)
        )
        chat = McpChatSession(
            workspace_id=ws.id,
            client_id=client.id,
            mcp_session_id="conv-chatowner",
            owner_user_id=owner.id,
            started_at=utc_now(),
            last_activity_at=utc_now(),
            tool_call_count=1,
            write_count=1,
            error_count=0,
            title_user_edited=False,
        )
        session.add(chat)
        session.flush()
        review = _seed_review(session, ws.id, "Chat Owner Page", str(reviewer.id))
        # Link the change to the originating chat session (B3 write-path result).
        change_set = session.get(ChangeSet, review.change_set_id)
        change_set.origin_chat_session_id = chat.id
        session.flush()

        result = _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="chat_owner",
            actor=reviewer.id,
        )

        to_owner = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=owner.id,
            scope="to_me",
            actor_role="editor",
        )

    # audience_user_id snapshotted the resolved chat owner; the owner sees it.
    assert result.decisions[0].audience_kind == "chat_owner"
    assert result.decisions[0].audience_user_id == owner.id
    assert [item.id for item in to_owner.items] == [review.id]


def test_chat_owner_unresolvable_raises(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "reviewer2@example.com", ws.id)
        # No origin_chat_session_id on the change -> chat owner unresolvable.
        review = _seed_review(session, ws.id, "No Owner Page", str(reviewer.id))
        with pytest.raises(ChatOwnerUnresolvableError):
            _request_changes(
                service,
                session,
                ws.id,
                review,
                audience_kind="chat_owner",
                actor=reviewer.id,
            )


def test_detail_chat_owner_available_flag(session_factory):
    """T15-F support: ReviewItemDetail.chat_owner_available is True iff the
    change has a resolvable chat owner (origin chat session with an owner),
    so the routing dialog can enable/disable the 'chat owner' option upfront."""
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        owner = _make_user(session, "flag-owner@example.com", ws.id)
        reviewer = _make_user(session, "flag-reviewer@example.com", ws.id)
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == ws.id)
        )
        chat = McpChatSession(
            workspace_id=ws.id,
            client_id=client.id,
            mcp_session_id="conv-flag",
            owner_user_id=owner.id,
            started_at=utc_now(),
            last_activity_at=utc_now(),
            tool_call_count=1,
            write_count=1,
            error_count=0,
            title_user_edited=False,
        )
        session.add(chat)
        session.flush()

        # Chat-originated change with a known owner -> available.
        chat_review = _seed_review(session, ws.id, "Chat Origin Page", str(reviewer.id))
        session.get(
            ChangeSet, chat_review.change_set_id
        ).origin_chat_session_id = chat.id
        # Non-chat change (no origin) -> not available.
        plain_review = _seed_review(session, ws.id, "Plain Page", str(reviewer.id))
        # Owner-less chat origin -> not available.
        ownerless_chat = McpChatSession(
            workspace_id=ws.id,
            client_id=client.id,
            mcp_session_id="conv-ownerless",
            owner_user_id=None,
            started_at=utc_now(),
            last_activity_at=utc_now(),
            tool_call_count=1,
            write_count=1,
            error_count=0,
            title_user_edited=False,
        )
        session.add(ownerless_chat)
        session.flush()
        ownerless_review = _seed_review(
            session, ws.id, "Ownerless Origin Page", str(reviewer.id)
        )
        session.get(
            ChangeSet, ownerless_review.change_set_id
        ).origin_chat_session_id = ownerless_chat.id
        session.flush()

        chat_detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=chat_review.id,
            actor_role="owner",
        )
        plain_detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=plain_review.id,
            actor_role="owner",
        )
        ownerless_detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=ownerless_review.id,
            actor_role="owner",
        )

    assert chat_detail.chat_owner_available is True
    assert plain_detail.chat_owner_available is False
    assert ownerless_detail.chat_owner_available is False


def _author_and_changes_requested(session, service, writer, *, body, suffix):
    ws = _workspace(session)
    author = _make_user(session, f"rev-author-{suffix}@example.com", ws.id)
    reviewer = _make_user(
        session, f"rev-reviewer-{suffix}@example.com", ws.id, role="owner"
    )
    created = writer.write_draft(  # Auto-fix OFF -> body stored as authored
        session=session,
        workspace_id=ws.id,
        request=WriteDraftRequest(
            title=f"Rev {suffix}",
            page_type="overview",
            body_markdown=body,
            reason="v1",
        ),
        actor_kind="human",
        actor_id=str(author.id),
        actor_role="owner",
    )
    review = session.get(ReviewItem, created.review_item_id)
    _request_changes(
        service,
        session,
        ws.id,
        review,
        audience_kind="workspace_editors",
        actor=reviewer.id,
    )
    ws.auto_fix_enabled = True
    base_hash = session.get(ContextPageVersion, created.draft_version_id).content_hash
    return ws, author, created, review, base_hash


def test_revision_format_only_is_not_auto_approved(session_factory):
    # re-review r2 #1/#2: under Auto-fix, a format-only revision of a
    # changes_requested review must go to a human (pending), never be
    # system-auto-approved — "the AI never approves" (Risk 6).
    service = ReviewService()
    writer = ContextWriteService()
    with session_factory.begin() as session:
        ws, author, created, review, base_hash = _author_and_changes_requested(
            session,
            service,
            writer,
            body="# Rev\n\n* item\n\nBody.\n",
            suffix="fo",
        )
        result = service.submit_revision_for_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            principal=author.id,
            actor_kind="human",
            actor_id=str(author.id),
            principal_role="editor",
            context_write_service=writer,
            update=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=created.draft_version_id,
                base_content_hash=base_hash,
                update_mode="full_rewrite",
                body_markdown="# Rev\n\n* item\n\nBody.\n",  # normalizes "*"->"-"
                citations=[],
                reason="reformat",
            ),
        )
        # the NEW review is a genuine reformat (bytes changed) but must be pending
        assert session.get(ReviewItem, result.new_review_item_id).status == "pending"
        content = service.get_review(
            session, ws.id, result.new_review_item_id, actor_user_id=author.id,
            actor_role="editor", actor=_actor(author, ws.id, "editor"),
        ).evidence.operations[0].content
        # Approval publishes the whole revision; the page was never published.
        assert (content.baseline_kind, content.baseline_version_id) == ("empty", None)


def test_revision_noop_is_rejected_not_superseded(session_factory):
    # re-review r2: a true no-op revision (resubmitted body identical to the
    # canonical draft) is rejected, NOT silently superseded/auto-approved.
    service = ReviewService()
    writer = ContextWriteService()
    with session_factory.begin() as session:
        ws, author, created, review, base_hash = _author_and_changes_requested(
            session,
            service,
            writer,
            body="# NoopRev\n\n- item\n\nBody.\n",
            suffix="noop",  # already canonical
        )
        with pytest.raises(ReadValidationError):
            service.submit_revision_for_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                principal=author.id,
                actor_kind="human",
                actor_id=str(author.id),
                principal_role="editor",
                context_write_service=writer,
                update=UpdateDraftRequest(
                    page_id=created.page_id,
                    base_version_id=created.draft_version_id,
                    base_content_hash=base_hash,
                    update_mode="full_rewrite",
                    body_markdown="# NoopRev\n\n- item\n\nBody.\n",
                    citations=[],
                    reason="noop",
                ),
            )
        session.refresh(review)
        assert review.status == "changes_requested"  # not superseded


def test_workspace_editors_broadcast_visible_to_any_editor_not_viewer(
    session_factory,
):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "rv@example.com", ws.id)
        editor = _make_user(session, "ed@example.com", ws.id, role="editor")
        viewer = _make_user(session, "vw@example.com", ws.id, role="viewer")
        review = _seed_review(session, ws.id, "Broadcast Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )

        to_editor = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=editor.id,
            scope="to_me",
            actor_role="editor",
        )
        to_viewer = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=viewer.id,
            scope="to_me",
            actor_role="viewer",
        )

    assert [item.id for item in to_editor.items] == [review.id]
    assert to_viewer.items == []


def test_request_changes_without_audience_defaults_to_editor_broadcast(
    session_factory,
):
    """G1#1: request_changes with no audience_kind persists a NULL audience and
    behaves as the editor broadcast (D2) — the single-button default path."""
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "defrv@example.com", ws.id)
        editor = _make_user(session, "defed@example.com", ws.id, role="editor")
        viewer = _make_user(session, "defvw@example.com", ws.id, role="viewer")
        review = _seed_review(session, ws.id, "Default Page", str(reviewer.id))
        result = service.apply_decision(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Please revise.",
            ),
            actor_kind="human",
            actor_id=str(reviewer.id),
            actor_role="owner",
            actor_provenance="browser_session",
        )
        to_editor = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=editor.id,
            scope="to_me",
            actor_role="editor",
        )
        to_viewer = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=viewer.id,
            scope="to_me",
            actor_role="viewer",
        )

    # NULL audience persisted; surfaces as broadcast to editors, not viewers.
    assert result.decisions[0].audience_kind is None
    assert result.decisions[0].audience_user_id is None
    assert result.current_audience_kind is None
    assert [item.id for item in to_editor.items] == [review.id]
    assert to_viewer.items == []


def test_null_audience_legacy_is_broadcast_to_editors(session_factory):
    """D2: a changes_requested item whose latest request_changes decision has a
    NULL audience_kind (legacy) is treated as a kind-3 broadcast."""
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "lr@example.com", ws.id)
        editor = _make_user(session, "le@example.com", ws.id, role="editor")
        review = _seed_review(session, ws.id, "Legacy Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )
        # Simulate a legacy decision: clear the audience_kind on the latest
        # request_changes decision.
        review.decisions[0].audience_kind = None
        session.flush()

        to_editor = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=editor.id,
            scope="to_me",
            actor_role="editor",
        )

    assert [item.id for item in to_editor.items] == [review.id]


def test_from_me_scope_lists_requests_i_authored(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "author@example.com", ws.id)
        review = _seed_review(session, ws.id, "From Me Page", str(author.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=author.id,
        )

        from_me = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=author.id,
            scope="from_me",
            actor_role="editor",
        )
        # An item I did NOT author is not in my from_me.
        other = _make_user(session, "author2@example.com", ws.id)
        from_other = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=other.id,
            scope="from_me",
            actor_role="editor",
        )

    assert [item.id for item in from_me.items] == [review.id]
    assert from_other.items == []


def test_all_scope_is_workspace_wide(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "ar@example.com", ws.id)
        review = _seed_review(session, ws.id, "All Scope Page", str(reviewer.id))
        listed = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            scope="all",
            actor_role="editor",
        )
    assert review.id in [item.id for item in listed.items]


def test_list_reviews_without_resolved_role_is_empty(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        author = _make_user(session, "missing-role@example.com", ws.id)
        _seed_review(session, ws.id, "Missing Role Page", str(author.id))

        listed = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            scope="all",
        )

    assert listed.items == []


def test_viewer_all_scope_is_clamped_to_personal_queue(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        requester = _make_user(session, "viewer-requester@example.com", ws.id)
        outsider = _make_user(session, "viewer-outsider@example.com", ws.id)
        routed = _seed_review(session, ws.id, "Routed Review", str(requester.id))
        hidden = _seed_review(session, ws.id, "Hidden Review", str(outsider.id))
        _request_changes(
            service,
            session,
            ws.id,
            routed,
            audience_kind="requester",
            actor=requester.id,
        )

        listed = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=requester.id,
            scope="all",
            actor_role="viewer",
        )

    ids = {item.id for item in listed.items}
    assert routed.id in ids
    assert hidden.id not in ids


def test_non_user_principal_to_me_from_me_is_empty(session_factory):
    """architect LOW: a non-user principal (audience_for None, e.g. an
    mcp_client/system actor) has no personal queue, so scope=to_me/from_me must
    be EMPTY — not fall through to the workspace-wide list."""
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        reviewer = _make_user(session, "nonuser-rv@example.com", ws.id)
        review = _seed_review(session, ws.id, "Non User Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )

        to_me = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=None,
            scope="to_me",
            actor_role="editor",
        )
        from_me = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=None,
            scope="from_me",
            actor_role="editor",
        )
        # scope=all still returns the item (sanity: it does exist).
        all_scope = service.list_reviews(
            session=session,
            workspace_id=ws.id,
            audience_for=None,
            scope="all",
            actor_role="editor",
        )

    assert to_me.items == []
    assert from_me.items == []
    assert review.id in [item.id for item in all_scope.items]


def test_claim_blocks_second_claimer_and_release_frees_it(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        first = _make_user(session, "claim1@example.com", ws.id)
        second = _make_user(session, "claim2@example.com", ws.id)
        reviewer = _make_user(session, "claimrv@example.com", ws.id)
        review = _seed_review(session, ws.id, "Claim Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )

        claimed = service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(first.id),
            actor_role="editor",
        )
        assert claimed.claimed_by == first.id
        assert claimed.claimed_at is not None

        # A live claim by another principal is rejected.
        with pytest.raises(ReviewAlreadyClaimedError):
            service.claim_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                actor_kind="human",
                actor_id=str(second.id),
                actor_role="editor",
            )

        # The holder re-claiming is idempotent (refreshes).
        again = service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(first.id),
            actor_role="editor",
        )
        assert again.claimed_by == first.id

        # Release by the holder frees it; the second actor can now claim.
        released = service.release_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(first.id),
        )
        assert released.claimed_by is None
        retaken = service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(second.id),
            actor_role="editor",
        )
        assert retaken.claimed_by == second.id


def test_stale_claim_can_be_taken_over(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        first = _make_user(session, "stale1@example.com", ws.id)
        second = _make_user(session, "stale2@example.com", ws.id)
        reviewer = _make_user(session, "stalerv@example.com", ws.id)
        review = _seed_review(session, ws.id, "Stale Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )
        review.claimed_by = first.id
        review.claimed_at = utc_now() - timedelta(minutes=CLAIM_TTL_MINUTES + 1)
        session.flush()

        # The claim is stale (> TTL) so a different actor may take it over.
        retaken = service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(second.id),
            actor_role="editor",
        )
    assert retaken.claimed_by == second.id


def test_claim_rejects_non_changes_requested_review(session_factory):
    """G2#4: claiming a review that is not changes_requested (here: pending) is
    out-of-state and rejected with a validation error."""
    from backend.services.read_errors import ReadValidationError

    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        editor = _make_user(session, "stateguard@example.com", ws.id)
        reviewer = _make_user(session, "stateguardrv@example.com", ws.id)
        review = _seed_review(session, ws.id, "Pending Page", str(reviewer.id))
        # No request_changes -> status is still "pending".
        with pytest.raises(ReadValidationError):
            service.claim_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                actor_kind="human",
                actor_id=str(editor.id),
            )


def test_resolution_clears_claim(session_factory):
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        holder = _make_user(session, "res1@example.com", ws.id)
        reviewer = _make_user(session, "resrv@example.com", ws.id)
        review = _seed_review(session, ws.id, "Resolve Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )
        service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(holder.id),
            actor_role="editor",
        )
        detail = service.get_review(
            session, ws.id, review.id,
            actor_user_id=reviewer.id, actor_role="editor",
            actor=_actor(reviewer, ws.id, "editor"),
        )
        assert detail.actions.approve.allowed is False
        assert detail.actions.reject.allowed is False
        assert detail.actions.request_changes.allowed is False
        assert detail.actions.edit_directly.allowed is False
        assert detail.actions.approve.reason_code == "claim_conflict"
        assert detail.reject_effect is None
        # The claim HOLDER applying a decision clears their own claim. (A
        # non-holder is blocked while the claim is live — see
        # test_decision_blocked_by_another_live_claim.)
        resolved = service.apply_decision(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=str(holder.id),
            actor_role="owner",
            actor_provenance="browser_session",
        )
    assert resolved.claimed_by is None
    assert resolved.claimed_at is None


def test_decision_blocked_by_another_live_claim(session_factory):
    # P1: a non-holder cannot decide a review while another actor holds a live
    # soft-claim (would bypass the conflict protection the claim provides).
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        holder = _make_user(session, "p1holder@example.com", ws.id)
        reviewer = _make_user(session, "p1rev@example.com", ws.id)
        review = _seed_review(session, ws.id, "P1 Page", str(reviewer.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=reviewer.id,
        )
        service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(holder.id),
            actor_role="editor",
        )
        with pytest.raises(ReviewAlreadyClaimedError):
            service.apply_decision(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=str(reviewer.id),
                actor_provenance="browser_session",
            )


def test_claim_requires_audience_membership(session_factory):
    # P2: only an actor in the routed audience may claim. A non-recipient editor
    # cannot grab a requester-routed review off the workspace-wide list.
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        requester = _make_user(session, "p2req@example.com", ws.id)
        outsider = _make_user(session, "p2out@example.com", ws.id)
        review = _seed_review(session, ws.id, "P2 Page", str(requester.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="requester",
            actor=requester.id,
        )
        # A non-recipient editor is rejected.
        with pytest.raises(ReadValidationError):
            service.claim_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                actor_kind="human",
                actor_id=str(outsider.id),
            )
        # A matching UUID is insufficient without a resolved editor role.
        with pytest.raises(ReadValidationError):
            service.claim_review(
                session=session,
                workspace_id=ws.id,
                review_item_id=review.id,
                actor_kind="human",
                actor_id=str(requester.id),
            )
        # The routed recipient can claim.
        claimed = service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(requester.id),
            actor_role="editor",
        )
        assert claimed.claimed_by == requester.id


def test_apply_decision_loads_review_with_row_lock(session_factory, monkeypatch):
    # apply_decision performs the same check-then-act sequence as claim/release/
    # submit_revision_for_review, so it must load the ReviewItem through the
    # shared SELECT ... FOR UPDATE helper — two concurrent decisions cannot
    # both pass the guards on PostgreSQL (no-op on SQLite).
    service = ReviewService()
    locked_ids = []
    original = ReviewService._require_review_for_update

    def spy(session, workspace_id, review_item_id):
        locked_ids.append(review_item_id)
        return original(session, workspace_id, review_item_id)

    monkeypatch.setattr(ReviewService, "_require_review_for_update", staticmethod(spy))
    with session_factory.begin() as session:
        ws = _workspace(session)
        editor = _make_user(session, "lockdecide@example.com", ws.id)
        review = _seed_review(session, ws.id, "Locked Decision Page", str(editor.id))
        decided = service.apply_decision(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=str(editor.id),
            actor_user_id=editor.id,
            actor_role="editor",
            actor_provenance="browser_session",
        )
    assert review.id in locked_ids
    assert decided.status == "approved"


def test_detail_hides_expired_claim(session_factory):
    # P2: an expired (TTL'd) claim is available for takeover, so it must read as
    # unclaimed in the detail payload rather than a stale active claim.
    service = ReviewService()
    with session_factory.begin() as session:
        ws = _workspace(session)
        editor = _make_user(session, "p2exp@example.com", ws.id)
        review = _seed_review(session, ws.id, "P2 Expiry Page", str(editor.id))
        _request_changes(
            service,
            session,
            ws.id,
            review,
            audience_kind="workspace_editors",
            actor=editor.id,
        )
        service.claim_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_kind="human",
            actor_id=str(editor.id),
            actor_role="editor",
        )
        # Age the claim past the TTL.
        claimed_review = session.get(ReviewItem, review.id)
        claimed_review.claimed_at = utc_now() - timedelta(minutes=CLAIM_TTL_MINUTES + 1)
        session.flush()
        detail = service.get_review(
            session=session,
            workspace_id=ws.id,
            review_item_id=review.id,
            actor_role="editor",
        )
    assert detail.claimed_by is None
    assert detail.claimed_at is None
