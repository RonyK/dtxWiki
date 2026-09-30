from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.services.change_review_audit import ChangeReviewAuditRecorder
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
)
from backend.spi.types import ActorContext

pytestmark = pytest.mark.integration
ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _targets(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    page = ContextPage(
        workspace_id=workspace.id,
        slug=f"provenance-{uuid4()}",
        title="Provenance Test",
        page_type="overview",
        lifecycle_status="draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace.id,
        page_id=page.id,
        version_no=1,
        body_md="# Test",
        content_hash=str(uuid4()),
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id=ACTOR_ID,
        version_status="draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.latest_draft_version_id = version.id
    return workspace, page, version


@pytest.mark.parametrize("kind", ["alternative", "comment", "source_notice"])
@pytest.mark.parametrize(
    ("provenance", "expected_status"),
    [("browser_session", "approved"), ("mcp_client", "pending")],
)
def test_audit_only_items_gate_ai_but_not_human(
    session_factory, kind, provenance, expected_status
):
    recorder = ChangeReviewAuditRecorder()
    with session_factory.begin() as session:
        workspace, page, version = _targets(session)
        common = dict(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance=provenance,
        )
        if kind == "alternative":
            result = recorder.record_context_page_alternative(
                **common, page_id=page.id, version_id=version.id
            )
        elif kind == "comment":
            result = recorder.record_context_page_inline_comment(
                **common,
                page_id=page.id,
                version_id=version.id,
                note="note",
                anchor={"anchor_text": "x"},
            )
        else:
            result = recorder.record_library_item_deleted(
                **common,
                page_id=page.id,
                library_item_id=uuid4(),
                item_title="deleted source",
                source_uri="https://example.com/deleted",
                note="source deleted notice",
            )
        review = session.get(ReviewItem, result.review_item_id)
        decisions = session.scalars(
            select(ReviewDecision).where(
                ReviewDecision.review_item_id == result.review_item_id
            )
        ).all()
    assert review.status == expected_status
    # H-6 (260908 UI audit): an "approved" ReviewItem with zero ReviewDecision
    # rows is exactly the bug -- the Review screen's "Resolved" count and its
    # "Recent decisions" list read these two sources and must never disagree
    # about the same event. A pending item has no decision yet either way.
    if expected_status == "approved":
        assert len(decisions) == 1
        assert decisions[0].decision_type == "approve"
        assert decisions[0].resulting_review_status == "approved"
    else:
        assert decisions == []


def test_malformed_direct_ai_write_gets_forced_high_review(session_factory):
    recorder = ChangeReviewAuditRecorder()
    with session_factory.begin() as session:
        workspace, page, version = _targets(session)
        result = recorder.record_context_page_write(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
            action="page.update",
            page_id=page.id,
            version_id=version.id,
            body="```python\nunclosed\n",
        )
        review = session.get(ReviewItem, result.review_item_id)
    assert review.status == "pending"
    assert review.risk_level == "high"
    assert review.risk_signals["lint_error"] is True


def test_direct_human_page_write_approval_has_matching_decision(session_factory):
    # H-6 (260908 UI audit), reproduced exactly: POST /api/v1/pages routes a
    # low-risk human browser write through record_context_page_write, which
    # created an "approved" ReviewItem with zero ReviewDecision rows -- the
    # Review screen showed "Resolved: 4" next to "No decisions yet" for the
    # same 4 pages.
    recorder = ChangeReviewAuditRecorder()
    with session_factory.begin() as session:
        workspace, page, version = _targets(session)
        result = recorder.record_context_page_write(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
            action="page.create",
            page_id=page.id,
            version_id=version.id,
            body="# Test",
        )
        review = session.get(ReviewItem, result.review_item_id)
        decisions = session.scalars(
            select(ReviewDecision).where(
                ReviewDecision.review_item_id == result.review_item_id
            )
        ).all()
    assert review.status == "approved"
    assert len(decisions) == 1
    assert decisions[0].decision_type == "approve"
    assert decisions[0].resulting_review_status == "approved"


def test_inert_context_write_flags_are_not_advertised_or_editable(session_factory):
    service = SettingsService()
    actor = ActorContext(
        actor_kind="human", actor_id=ACTOR_ID, provenance="browser_session"
    )
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        policies = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace.id,
        )
        assert policies.context_write.policy == {}
        with pytest.raises(
            SettingsPolicyUpdateRejected, match="unknown settings group"
        ):
            service.update_policy(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace.id,
                actor=actor,
                group="context_write",
                key="draft_only",
                value=True,
            )
