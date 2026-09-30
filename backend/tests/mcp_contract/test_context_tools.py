from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from backend.app.settings import Settings
from backend.mcp import context_tools
from backend.mcp.router import dispatch
from backend.models.audit import AuditEvent
from backend.models.change import ChangeSet
from backend.models.context import ContextCitation, ContextPage, ContextPageVersion
from backend.models.feedback import FeedbackItem
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_edit import PatchDraftRequest
from backend.schemas.context_write import (
    CitationCandidate,
    CiteResult,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.services.citation_service import CitationService
from backend.services.context_editing import section_content_hash
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.read_errors import (
    RejectedByPolicyError,
    RevisionSubmissionRequiredError,
    StaleBaseVersionError,
)

# P8.6: write_draft/update_draft/patch_draft are no longer callable MCP tools
# (deregistered -- see .claude/plan/260906-fix-epic-1-fixes-p9-1-decisions.md,
# search "P8.6"), but ContextWriteService's methods backing them remain fully
# live internal callers for PageService.reconcile_branch and
# ReviewService.submit_revision_for_review. Tests whose actual subject is this
# engine (body validation, CAS conflicts, patch operations, idempotency
# derivation) call it directly instead of going through MCP dispatch, the
# same pattern established in test_patch_draft_push_semantics.py and
# test_partial_edit_token_cost.py. `cite`/`submit_feedback`/`read_context_
# outline` stay on the live MCP client since those tools are unaffected.
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


def _patch_write_row_counts(page_service):
    with page_service.session_factory() as session:
        return (
            session.scalar(select(func.count()).select_from(ContextPageVersion)),
            session.scalar(select(func.count()).select_from(ChangeSet)),
            session.scalar(select(func.count()).select_from(ReviewItem)),
        )


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


def _patch_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("patch_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )


def _update_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("update_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().update_draft(
            session=session,
            workspace_id=workspace_id,
            request=UpdateDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )


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
    assert result.latest_draft_version_id == result.draft_version_id
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


def test_mcp_update_draft_returns_conflict_payload(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="MCP Stale Draft",
        page_type="overview",
        body_markdown="# MCP Stale Draft\n\nFirst.",
        reason="create draft",
    )
    base = _read_context_page(page_service, created.page_id)["version_info"]
    updated = _update_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base_version_id=base["version_id"],
        base_content_hash=base["content_hash"],
        update_mode="full_rewrite",
        body_markdown="# MCP Stale Draft\n\nSecond.",
        reason="advance draft",
    )

    with pytest.raises(StaleBaseVersionError) as exc_info:
        _update_draft(
            page_service,
            actor,
            workspace_id,
            page_id=created.page_id,
            base_version_id=base["version_id"],
            base_content_hash=base["content_hash"],
            update_mode="full_rewrite",
            body_markdown="# MCP Stale Draft\n\nStale.",
            reason="stale draft",
        )

    assert updated.outcome == "updated_draft"
    assert updated.body_changed is True
    assert updated.metadata_changed is False
    assert updated.persisted_content_hash == _draft_content_hash(
        page_service, updated.draft_version_id
    )
    assert updated.persisted_character_count == _draft_character_count(
        page_service, updated.draft_version_id
    )
    assert updated.version_status == "draft"
    assert updated.latest_draft_version_id == updated.draft_version_id
    assert updated.current_published_version_id is None
    assert updated.review_status == "pending"
    conflict = exc_info.value
    assert conflict.error_code == "stale_base_version"
    assert conflict.latest_version_id == updated.draft_version_id
    assert conflict.latest_content_hash is not None


def test_mcp_patch_draft_edits_a_known_section_without_a_pre_read(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Patch Without Read",
        page_type="overview",
        body_markdown="# Patch Without Read\n\n## Known section\n\nBefore.",
        reason="Create a known fixture",
    )

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
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
        reason="Edit the known section directly",
    )

    assert result.draft_version_id is not None
    receipt = result.model_dump(mode="json")
    operation = receipt["operation_receipts"][0]
    assert operation["op"] == "replace_text"
    assert operation["resolved_heading_path"] == [
        "Patch Without Read",
        "Known section",
    ]
    # "Before." itself is on line 5 -- the scope `at` resolved to (lines
    # 3-5, the whole "## Known section" subtree) is not the answer here;
    # "After." is one char shorter.
    assert operation["resolved_line_range"] == {"start_line": 5, "end_line": 5}
    assert operation["chars_delta"] == -1
    # A heading-path-addressed replace_text is not anchor- or section-hash-
    # gated -- these genuinely do not apply to this operation.
    assert operation["anchor_status"] is None
    assert operation["old_section_hash"] is None
    assert operation["new_section_hash"] is None
    assert receipt["risk"] in ("low", "medium", "high")
    assert receipt["outside_target_changed"] is False
    assert receipt["normalized"] is False
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, result.draft_version_id)
        assert version is not None
        assert "After." in version.body_md
        assert "Before." not in version.body_md


def test_mcp_patch_draft_resent_insert_section_does_not_duplicate(page_service, actor):
    """A-4 regression, engine level: patch_draft derives an idempotency key
    from the conversation, the request's own byte-identical content, and the
    effective base (ContextWriteService.patch_draft itself, not the deleted
    MCP wrapper -- see request_fingerprint()/effective_idempotency_key in
    context_write_service.py). A naive resend (no explicit idempotency_key)
    must leave exactly one copy of the inserted heading, replayed from the
    idempotency ledger before it ever reaches the edit engine again.
    """
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Resend Target",
        page_type="overview",
        body_markdown="# Resend Target\n\nintro text",
        reason="Create a fixture with nothing after its only heading",
    )

    payload = dict(
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
            {
                "op": "insert_section",
                "at": {"by": "heading_path", "heading_path": ["Resend Target"]},
                "position": "append_child",
                "title": "Appended",
                "content": "appended body",
            }
        ],
        reason="append a child section",
    )

    first = _patch_draft(page_service, actor, workspace_id, **payload)
    assert first.operation_receipts[0].outcome == "applied"

    # A naive resend: the byte-identical request, no idempotency_key. The
    # derived key is the same, so the ledger replays the first receipt rather
    # than re-deriving an edit-engine outcome.
    second = _patch_draft(page_service, actor, workspace_id, **payload)
    assert second.push_outcome == "replayed"
    assert second.model_copy(update={"push_outcome": first.push_outcome}) == first

    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, first.draft_version_id)
        assert version is not None
        assert version.body_md.count("## Appended") == 1


def test_mcp_patch_draft_accepts_the_section_hash_read_context_outline_returned(
    page_service, mcp_default_actor
):
    """A-1 regression: read_context_outline's section_hash must be directly
    usable as patch_draft's expected_section_hash. The two tools must hash in
    the same (normalized) space -- computing the expected value locally, as
    the unit tests for section_content_hash do, cannot catch a drift back to
    raw hashing on one side, so this test takes the hash straight out of a
    real read_context_outline response instead of recomputing it.
    """
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Outline Hash Round Trip",
        page_type="overview",
        body_markdown="# Outline Hash Round Trip\n\nbody\n",
        reason="create fixture for read-then-edit hash round trip",
    )

    outline = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(created.page_id)},
        },
        actor=actor,
        page_service=page_service,
    )
    assert outline.envelope.result_status == "succeeded"
    (section,) = outline.outline

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
            {
                "op": "replace_section",
                "at": {"by": "section_id", "section_id": section.section_id},
                "content": "# Outline Hash Round Trip\n\nreplaced\n",
                "expected_section_hash": section.section_hash,
            }
        ],
        reason="replace using the hash read_context_outline returned",
    )

    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, result.draft_version_id)
        assert version is not None
        assert "replaced" in version.body_md


def test_mcp_patch_draft_replace_section_receipt_carries_both_section_hashes(
    page_service, mcp_default_actor
):
    """Operation-receipt regression: old_section_hash/new_section_hash used
    to always be null (getattr(data, "operation_receipts", None) always
    missed -- the field did not exist on UpdateDraftResult). replace_section
    is the operation these two are meant for: old is the precondition the
    caller already proved, new is the hash of what actually landed.
    """
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace(page_service)
    body = "# Section Hash Receipt\n\n## Known section\n\nBefore.\n"
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Section Hash Receipt",
        page_type="overview",
        body_markdown=body,
        reason="create fixture for section-hash receipt",
    )

    outline = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(created.page_id)},
        },
        actor=actor,
        page_service=page_service,
    )
    known_section = next(
        entry for entry in outline.outline if entry.title == "Known section"
    )
    new_content = "## Known section\n\nAfter.\n"

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
            {
                "op": "replace_section",
                "at": {"by": "section_id", "section_id": known_section.section_id},
                "content": new_content,
                "expected_section_hash": known_section.section_hash,
            }
        ],
        reason="replace the known section",
    )

    receipt = result.model_dump(mode="json")
    operation = receipt["operation_receipts"][0]
    assert operation["op"] == "replace_section"
    assert operation["old_section_hash"] == known_section.section_hash
    assert operation["new_section_hash"] == section_content_hash(new_content)
    assert operation["old_section_hash"] != operation["new_section_hash"]
    assert operation["resolved_heading_path"] == [
        "Section Hash Receipt",
        "Known section",
    ]


def test_mcp_patch_draft_unscoped_replace_text_receipt_reports_the_changed_line(
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
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Unscoped Replace Text Line Range",
        page_type="overview",
        body_markdown=body,
        reason="create fixture for line-range receipt",
    )

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
            {
                "op": "replace_text",
                "old_text": "original text",
                "new_text": "revised",
            }
        ],
        reason="unscoped replace on the last line",
    )

    operation = result.model_dump(mode="json")["operation_receipts"][0]
    # "original text" is on line 5 of the 5-line body -- not "the whole
    # document", which is what char_start:char_end (0..len(body), since `at`
    # was omitted) would report.
    assert operation["resolved_line_range"] == {"start_line": 5, "end_line": 5}
    assert operation["chars_delta"] == -6


def test_mcp_patch_draft_anchor_address_usage_is_audited_without_authored_text(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Anchor deprecation audit",
        page_type="overview",
        body_markdown="# Audit\n\nprivate-anchor-text\n",
        reason="create anchor audit fixture",
    )

    _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
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
        reason="exercise deprecated anchor addressing",
    )

    with page_service.session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "mcp.patch_draft.anchor_address_used",
                    AuditEvent.target_id == created.page_id,
                )
            )
        )
    assert len(events) == 1
    assert events[0].payload["operation_count"] == 1
    assert "private-anchor-text" not in str(events[0].payload)


def test_mcp_patch_draft_dry_run_creates_no_version_change_set_or_review(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Patch Dry Run",
        page_type="overview",
        body_markdown="# Patch Dry Run\n\nBefore.",
        reason="Create a dry-run fixture",
    )
    before = _patch_write_row_counts(page_service)

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base=str(created.draft_version_id),
        operations=[
            {
                "op": "replace_text",
                "old_text": "Before.",
                "new_text": "After.",
            }
        ],
        reason="Simulate the edit only",
        dry_run=True,
    )

    assert result.draft_version_id is None
    assert result.change_set_id is None
    assert result.review_item_id is None
    assert _patch_write_row_counts(page_service) == before


def test_mcp_submit_feedback_creates_feedback(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="MCP Feedback Target",
        page_type="guide",
        body_markdown="# MCP Feedback Target\n\nBody.",
        reason="create feedback target",
    )

    response = dispatch(
        "submit_feedback",
        {
            "workspace_id": str(workspace_id),
            "target_type": "context_page",
            "target_id": str(created.page_id),
            "body": "Needs better citation.",
            "severity": "major",
        },
        actor=actor,
        page_service=page_service,
    )

    with page_service.session_factory() as session:
        feedback = session.get(FeedbackItem, response.data.feedback_id)

    assert response.envelope.result_status == "succeeded"
    assert response.data.next_action == "agent_resolution_queued"
    assert feedback.target_id == created.page_id


def _read_context_page(page_service, page_id):
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        version = session.get(ContextPageVersion, page.current_version_id)
        return {
            "version_info": {
                "version_id": version.id,
                "content_hash": version.content_hash,
            }
        }


def test_update_draft_says_why_the_edit_needs_review(page_service, mcp_default_actor):
    """`write_draft` reports its lint findings; `update_draft` computed the same
    ones and dropped them, so an agent learned why its first write needed review
    and never again for any edit after it."""

    actor = mcp_default_actor(scopes=("draft-write", "read"))
    workspace_id = _workspace(page_service)
    unclosed_fence = "# Title\n\n```python\nprint('never closed')\n"

    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Update Review Reasons",
        page_type="concept",
        body_markdown="# Title\n\nClean body.\n",
        reason="seed a draft to update",
    )

    updated = _update_draft(
        page_service,
        actor,
        workspace_id,
        page_id=created.page_id,
        base_version_id=created.draft_version_id,
        base_content_hash=_draft_content_hash(page_service, created.draft_version_id),
        update_mode="full_rewrite",
        body_markdown=unclosed_fence,
        reason="introduce an unclosed fence",
    )

    codes = {reason.code for reason in updated.review_reasons}
    assert "md.code_fence_balance" in codes, updated.review_reasons


def test_update_draft_requires_revision_submission_for_active_change_request(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Revision required",
        page_type="overview",
        body_markdown="# Draft\n\nOriginal.",
        reason="create draft",
    )
    with page_service.session_factory.begin() as session:
        review = session.get(ReviewItem, created.review_item_id)
        review.status = "changes_requested"
    base = _read_context_page(page_service, created.page_id)["version_info"]

    with pytest.raises(RevisionSubmissionRequiredError) as exc_info:
        _update_draft(
            page_service,
            actor,
            workspace_id,
            page_id=created.page_id,
            base_version_id=base["version_id"],
            base_content_hash=base["content_hash"],
            update_mode="full_rewrite",
            body_markdown="# Draft\n\nParallel edit.",
            reason="generic update",
        )
    assert exc_info.value.review_item_id == created.review_item_id


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
    created = _write_draft(
        page_service,
        actor,
        workspace_id,
        title="Metadata Validation Target",
        page_type="overview",
        body_markdown="# Metadata Validation Target",
        reason="seed target",
    )
    base = _read_context_page(page_service, created.page_id)["version_info"]
    with page_service.session_factory() as session:
        before_versions = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )
        before_reviews = session.scalar(select(func.count()).select_from(ReviewItem))

    # Invalid metadata_patch values fail Pydantic construction of
    # UpdateDraftRequest itself, before ContextWriteService.update_draft is
    # ever called -- the same "validation_failed" boundary the deleted MCP
    # wrapper reported, just one layer lower now that dispatch's own
    # generic validation-error envelope wrapping is out of the picture.
    with pytest.raises(ValidationError):
        _update_draft(
            page_service,
            actor,
            workspace_id,
            page_id=created.page_id,
            base_version_id=base["version_id"],
            base_content_hash=base["content_hash"],
            update_mode="metadata_update",
            metadata_patch=metadata_patch,
            reason="invalid metadata",
        )

    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ContextPageVersion))
            == before_versions
        )
        assert (
            session.scalar(select(func.count()).select_from(ReviewItem))
            == before_reviews
        )


def test_mcp_cite_returns_typed_candidates(
    page_service, actor, media_installer_factory
):
    workspace_id = _workspace(page_service)
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/mcp-cite",
            title="MCP Cite Source",
            raw_text="MCP cite tool returns typed candidates.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

    response = dispatch(
        "cite",
        {
            "workspace_id": str(workspace_id),
            "claim_text": "cite tool returns typed candidates",
            "desired_citation_type": "raw_source",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.citation_candidates[0].library_artifact_id == artifact.id
    assert response.data.citation_candidates[0].write_citation.model_dump(
        exclude_none=True
    ) == {
        "target_kind": "context_claim",
        "citation_type": "derived_artifact",
        "library_artifact_id": artifact.id,
        "location": artifact.conversion_detail.location_model,
        "snippet": response.data.citation_candidates[0].snippet,
        "created_via": "cite_tool",
    }
    assert response.input_required is None


def test_mcp_cite_hides_restricted_library_candidates(
    page_service, media_installer_factory
):
    workspace_id = _workspace(page_service)
    with page_service.session_factory.begin() as session:
        editor = User(
            email="cite-editor@example.com",
            password_hash="unused",
            display_name="Cite Editor",
        )
        session.add(editor)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=editor.id,
            workspace_id=workspace_id,
            role="editor",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/mcp-cite-restricted",
            title="Restricted MCP cite source",
            trust_level="restricted",
            raw_text="restricted-cite-marker must remain private",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        editor_id = editor.id

    editor_actor = ActorContext(
        actor_kind="human",
        actor_id=str(editor_id),
        scopes=("mcp:write", "cite"),
        user_id=editor_id,
        token_kind="mcp_client",
    )

    response = dispatch(
        "cite",
        {
            "workspace_id": str(workspace_id),
            "claim_text": "restricted-cite-marker",
            "desired_citation_type": "raw_source",
        },
        actor=editor_actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert all(
        candidate.library_artifact_id != artifact.id
        for candidate in response.data.citation_candidates
    )


def test_mcp_cite_preview_stages_verification_without_mutating_citations(
    page_service, actor, monkeypatch, media_installer_factory
):
    workspace_id = _workspace(page_service)
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/mcp-cite-preview",
            title="MCP Cite Preview Source",
            raw_text="Preview citation verification supports this exact claim.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        citation = ContextCitation(
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=uuid4(),
            target_location={},
            library_artifact_id=artifact.id,
            citation_type="derived_artifact",
            location={},
            verification_status="weak",
        )
        session.add(citation)
        session.flush()
        citation_id = citation.id
        citation_count = session.scalar(select(func.count(ContextCitation.id)))
    monkeypatch.setattr(
        context_tools,
        "get_settings",
        lambda: Settings(
            session_secret="cite-preview-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    request = {
        "workspace_id": str(workspace_id),
        "claim_text": "Preview citation verification supports this exact claim",
        "desired_citation_type": "raw_source",
        "wire": "mrtr_preview",
    }

    first = dispatch("cite", request, actor=actor, page_service=page_service)
    assert first.input_required["inputRequests"][0]["id"] == "cite_verify"
    second = dispatch(
        "cite",
        {
            **request,
            "resume_token": first.input_required["requestState"],
            "input_responses": {
                "cite_verify": {
                    "candidate_id": str(artifact.id),
                    "supported": "yes",
                    "confidence": 0.95,
                    "rationale": "The extracted sentence directly supports the claim.",
                }
            },
        },
        actor=actor,
        page_service=page_service,
    )
    assert second.envelope.result_status == "succeeded"
    assert second.data.suggested_verification.candidate_id == artifact.id
    assert second.data.suggested_verification.supported == "yes"
    with page_service.session_factory() as session:
        assert session.scalar(select(func.count(ContextCitation.id))) == citation_count
        assert session.get(ContextCitation, citation_id).verification_status == "weak"

    tampered = first.input_required["requestState"][:-1] + "x"
    rejected = dispatch(
        "cite",
        {
            **request,
            "resume_token": tampered,
            "input_responses": {
                "cite_verify": {
                    "candidate_id": str(artifact.id),
                    "supported": "yes",
                    "confidence": 1,
                    "rationale": "forged",
                }
            },
        },
        actor=actor,
        page_service=page_service,
    )
    assert rejected.envelope.error_code == "client_state_invalid"


def test_mcp_cite_preview_signs_only_the_eight_exposed_candidates(
    page_service, actor, monkeypatch
):
    workspace_id = _workspace(page_service)
    candidate_ids = [uuid4() for _ in range(10)]

    def many_candidates(*_args, **_kwargs):
        return CiteResult(
            citation_candidates=[
                CitationCandidate(
                    citation_type="derived_artifact",
                    library_artifact_id=candidate_id,
                    snippet=f"Candidate {index}",
                    support_level="partial",
                    freshness="current",
                    recommendation="needs_review",
                )
                for index, candidate_id in enumerate(candidate_ids)
            ]
        )

    monkeypatch.setattr(CitationService, "cite", many_candidates)
    monkeypatch.setattr(
        context_tools,
        "get_settings",
        lambda: Settings(
            session_secret="cite-top-eight-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    request = {
        "workspace_id": str(workspace_id),
        "claim_text": "Candidate cap claim",
        "desired_citation_type": "raw_source",
        "wire": "mrtr_preview",
    }

    first = dispatch("cite", request, actor=actor, page_service=page_service)
    exposed = first.input_required["inputRequests"][0]["candidates"]
    assert [item["id"] for item in exposed] == [
        str(candidate_id) for candidate_id in candidate_ids[:8]
    ]

    rejected_hidden = dispatch(
        "cite",
        {
            **request,
            "resume_token": first.input_required["requestState"],
            "input_responses": {
                "cite_verify": {
                    "candidate_id": str(candidate_ids[8]),
                    "supported": "yes",
                    "confidence": 1,
                    "rationale": "This candidate was not exposed.",
                }
            },
        },
        actor=actor,
        page_service=page_service,
    )
    assert rejected_hidden.envelope.result_status == "failed"
    assert rejected_hidden.envelope.error_code == "validation_failed"


def test_cite_enforces_workspace_accepted_citation_types(
    page_service, actor, media_installer_factory
):
    workspace_id = _workspace(page_service)
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.citation_policy = {
            "strictness": "standard",
            "accepted_citation_types": ["context_page"],
        }
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/disallowed-citation",
            title="Disallowed source type",
            raw_text="policy scoped citation candidate",
        )
        ExtractionService(media_installer_factory=media_installer_factory).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

    response = dispatch(
        "cite",
        {
            "workspace_id": str(workspace_id),
            "claim_text": "policy scoped citation candidate",
            "desired_citation_type": "raw_source",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    assert response.data.citation_candidates == []
