from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.mcp.router import dispatch
from backend.models.audit import AuditEvent
from backend.models.context import ContextPageVersion
from backend.models.feedback import FeedbackItem
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import (
    RejectedByPolicyError,
)

# write_draft is no longer a callable MCP tool, but ContextWriteService.
# write_draft still backs internal page creation, so its tests call it
# directly. Edits of an existing draft go through the live
# patch_context_edit/update_context_edit tools.
#
# Three tests were deleted outright rather than retargeted, each because its
# exact subject no longer exists and an equivalent already exists elsewhere:
# - the read-only-actor-denied-write_draft scope test -> superseded by
#   test_read_only_actor_cannot_create_context_page in
#   test_context_edit_tools.py (identical scope-denial contract, new tool).
# - the "patch_draft is a thin receipt adapter" test -> it monkeypatched
#   ContextWriteService.patch_draft and dispatched "patch_draft" specifically
#   to test the now-deleted MCP wrapper's own receipt-mapping code; patch_
#   context_edit has separate, already-covered receipt-mapping logic.
# - the two write_draft idempotency-replay/conflict tests -> write_draft's
#   idempotent-create behavior lived entirely in the deleted MCP wrapper
#   (PageService.create_page_first, the replacement, takes no idempotency_key
#   at all -- a known, already-logged gap, not something to fake coverage for
#   here).


@pytest.fixture
def actor(mcp_default_actor):
    # Fix-6 Task 1.2: mcp_client must operate inside own workspace.
    return mcp_default_actor(scopes=("cite", "draft-write", "feedback-write"))


@pytest.fixture
def read_only_actor(mcp_default_actor):
    return mcp_default_actor(
        actor_id="read-only-mcp", scopes=("guide", "search", "read")
    )


def _draft_content_hash(page_service, version_id):
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None
        return version.content_hash


def _draft_character_count(page_service, version_id):
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None
        return len(version.body_md)


def _workspace(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _write_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("write_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_role=actor.workspace_role,
        )


def _create(page_service, actor, workspace_id, title, body_md):
    created = dispatch(
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": title,
            "body_md": body_md,
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert created.envelope.result_status == "succeeded", created.envelope
    return created.data


def _patch(page_service, actor, workspace_id, draft, operations):
    patched = dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(draft.id),
            "version_id": str(draft.version_id),
            "expected_editing_revision": draft.editing_revision,
            "operations": operations,
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert patched.envelope.result_status == "succeeded", patched.envelope
    return patched.data


def test_mcp_write_draft_creates_reviewable_context_draft(page_service, actor):
    workspace_id = _workspace(page_service)

    result = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="MCP Context Draft",
        page_type="overview",
        body_markdown="# MCP Context Draft\n\nNeeds citation.",
        reason="create MCP context draft",
    )

    assert result.next_action == "more_citation_needed"
    assert result.citation_warnings == ["citation_missing"]
    assert result.outcome == "created_draft"
    assert result.body_changed is True
    assert result.metadata_changed is True
    assert result.persisted_content_hash == _draft_content_hash(
        page_service, result.draft_version_id
    )
    assert result.persisted_character_count == _draft_character_count(
        page_service, result.draft_version_id
    )
    assert result.version_status == "draft"
    assert result.current_published_version_id is None
    assert result.review_status == "pending"


@pytest.mark.parametrize(
    "body_markdown",
    (
        "![inline](data:image/png;base64,aGVsbG8=)",
        '<img alt="inline" src="data:image/png;base64,aGVsbG8=">',
        # MCP-03: reference-form images and srcset destinations were missed by
        # the prior pair of form-specific regexes.
        "![diagram][asset]\n\n[asset]: data:image/png;base64,aGVsbG8=\n",
        '<img alt="inline" srcset="data:image/png;base64,aGVsbG8= 1x">',
    ),
)
def test_mcp_write_draft_refuses_embedded_data_images(
    page_service, actor, body_markdown
):
    with pytest.raises(RejectedByPolicyError) as exc_info:
        _write_draft(
            page_service,
            actor,
            _workspace(page_service),
            title="No Embedded Images",
            page_type="overview",
            body_markdown=body_markdown,
            reason="exercise image upload boundary",
        )

    assert exc_info.value.error_code == "media_markdown_policy_invalid"
    assert "upload_image" in str(exc_info.value)
    assert "data:" in str(exc_info.value)


def test_mcp_patch_edits_a_known_section_without_a_pre_read(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "Patch Without Read",
        "# Patch Without Read\n\n## Known section\n\nBefore.",
    )

    result = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_text",
                "old_text": "Before.",
                "new_text": "After.",
                "at": {
                    "by": "heading_path",
                    "heading_path": ["Patch Without Read", "Known section"],
                },
            }
        ],
    )

    operation = result.edit_receipt.operations[0]
    assert operation.op == "replace_text"
    assert operation.heading_path == ["Patch Without Read", "Known section"]
    # "Before." itself is on line 5 -- the scope `at` resolved to (lines
    # 3-5, the whole "## Known section" subtree) is not the answer here;
    # "After." is one char shorter.
    assert operation.line_range == (5, 5)
    assert operation.chars_removed - operation.chars_added == 1
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, created.version_id)
        assert version is not None
        assert "After." in version.body_md
        assert "Before." not in version.body_md


def test_mcp_patch_accepts_the_section_hash_read_context_outline_returned(
    page_service, mcp_default_actor
):
    """A-1 regression: read_context_outline's section_hash must be directly
    usable as patch_context_edit's expected_section_hash. The two tools must hash in
    the same (normalized) space -- computing the expected value locally, as
    the unit tests for section_content_hash do, cannot catch a drift back to
    raw hashing on one side, so this test takes the hash straight out of a
    real read_context_outline response instead of recomputing it.
    """
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "Outline Hash Round Trip",
        "# Outline Hash Round Trip\n\nbody\n",
    )

    outline = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
        },
        actor=actor,
        page_service=page_service,
    )
    assert outline.envelope.result_status == "succeeded", outline.envelope
    (section,) = outline.outline

    _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_section",
                "at": {"by": "section_id", "section_id": section.section_id},
                "content": "# Outline Hash Round Trip\n\nreplaced\n",
                "expected_section_hash": section.section_hash,
            }
        ],
    )

    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, created.version_id)
        assert version is not None
        assert "replaced" in version.body_md


def test_mcp_patch_unscoped_replace_text_receipt_reports_the_changed_line(
    page_service, actor
):
    """resolved_line_range regression, one level over the outside_target_
    changed fix: it was still derived from char_start:char_end, replace_
    text's SEARCH SCOPE (the whole document when `at` is omitted), not from
    changed_ranges (the real edited span) -- so a one-line change on a
    5-line document reported "lines 1-5" instead of the line it landed on.
    """
    workspace_id = _workspace(page_service)
    body = "# Doc\n\n## A\n\noriginal text\n"
    created = _create(
        page_service, actor, workspace_id, "Unscoped Replace Text Line Range", body
    )

    result = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [{"op": "replace_text", "old_text": "original text", "new_text": "revised"}],
    )

    operation = result.edit_receipt.operations[0]
    # "original text" is on line 5 of the 5-line body -- not "the whole
    # document", which is what char_start:char_end (0..len(body), since `at`
    # was omitted) would report.
    assert operation.line_range == (5, 5)
    assert operation.chars_removed - operation.chars_added == 6


def test_mcp_patch_anchor_address_usage_is_audited_without_authored_text(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "Anchor deprecation audit",
        "# Audit\n\nprivate-anchor-text\n",
    )
    _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "insert_text",
                "at": {
                    "by": "anchor",
                    "anchor_text": "private-anchor-text",
                    "char_offset_hint": 9,
                },
                "new_text": " inserted",
                "side": "after",
            }
        ],
    )

    with page_service.session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "mcp.patch_draft.anchor_address_used",
                    AuditEvent.target_id == created.id,
                )
            )
        )
    assert len(events) == 1
    assert events[0].payload["operation_count"] == 1
    assert "private-anchor-text" not in str(events[0].payload)


def test_mcp_feedback_is_linked_by_the_review_request_that_addresses_it(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "MCP Feedback Target",
        "# MCP Feedback Target\n\nBody.",
    )

    response = dispatch(
        "submit_feedback",
        {
            "workspace_id": str(workspace_id),
            "target_type": "context_page",
            "target_id": f"/p/{workspace_id}/{created.id}",
            "body": "Needs better citation.",
            "severity": "major",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    assert response.data.next_action == "agent_resolution_queued"

    submitted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "feedback_id": str(response.data.feedback_id),
            "idempotency_key": str(uuid4()),
        },
        actor=actor,
        page_service=page_service,
    )
    assert submitted.envelope.result_status == "succeeded", submitted.envelope

    with page_service.session_factory() as session:
        feedback = session.get(FeedbackItem, response.data.feedback_id)
        review = session.get(ReviewItem, submitted.data.review_item_id)
    assert feedback.target_id == created.id
    assert feedback.status == "draft_created"
    assert feedback.resolved_by_change_set_id == review.change_set_id


def _update(page_service, actor, workspace_id, draft, **fields):
    return dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(draft.id),
            "version_id": str(draft.version_id),
            "expected_editing_revision": draft.editing_revision,
            "idempotency_key": str(uuid4()),
            **fields,
        },
        actor=actor,
        page_service=page_service,
    )


def test_update_says_why_the_edit_needs_review(page_service, mcp_default_actor):
    """Creation reports its lint findings; a later whole-body update must
    report the same ones, or an agent learns why its first write needed review
    and never again for any edit after it."""

    actor = mcp_default_actor(scopes=("draft-write", "read"))
    workspace_id = _workspace(page_service)
    unclosed_fence = "# Title\n\n```python\nprint('never closed')\n"
    created = _create(
        page_service,
        actor,
        workspace_id,
        "Update Review Reasons",
        "# Title\n\nClean body.\n",
    )

    updated = _update(
        page_service, actor, workspace_id, created, body_md=unclosed_fence
    )

    assert updated.envelope.result_status == "succeeded", updated.envelope
    codes = {reason.code for reason in updated.data.publish_blocked_reasons}
    assert "md.code_fence_balance" in codes, updated.data.publish_blocked_reasons


@pytest.mark.parametrize(
    "metadata_patch",
    [
        {"page_type": "outside-taxonomy"},
        {"tags": "not-a-list"},
        {"placement_parent_node_id": "not-a-uuid"},
    ],
)
def test_mcp_metadata_patch_rejects_invalid_values_without_writing(
    page_service, actor, metadata_patch
):
    workspace_id = _workspace(page_service)
    body = "# Metadata Validation Target"
    created = _create(
        page_service, actor, workspace_id, "Metadata Validation Target", body
    )
    with page_service.session_factory() as session:
        before_revision = session.get(
            ContextPageVersion, created.version_id
        ).editing_revision
        before_versions = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )
        before_reviews = session.scalar(select(func.count()).select_from(ReviewItem))

    refused = _update(
        page_service, actor, workspace_id, created, body_md=body, **metadata_patch
    )

    assert refused.envelope.error_code == "validation_failed", refused.envelope
    with page_service.session_factory() as session:
        assert (
            session.get(ContextPageVersion, created.version_id).editing_revision
            == before_revision
        )
        assert (
            session.scalar(select(func.count()).select_from(ContextPageVersion))
            == before_versions
        )
        assert (
            session.scalar(select(func.count()).select_from(ReviewItem))
            == before_reviews
        )
