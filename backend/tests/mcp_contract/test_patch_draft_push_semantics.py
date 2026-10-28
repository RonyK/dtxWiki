import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import ReadValidationError, StaleBaseVersionError

# P8.6: patch_draft is no longer a callable MCP tool (write_draft/update_draft/
# patch_draft were deregistered), but ContextWriteService.patch_draft -- the
# push/rebase/diverge/reconcile engine these tests exercise -- remains fully
# intact as a load-bearing internal caller for PageService.reconcile_branch and
# ReviewService.submit_revision_for_review. These tests call the engine
# directly instead of going through MCP dispatch. Auth/scope/idempotency-key
# envelope concerns that dispatch() used to also implicitly exercise are
# deliberately not re-covered here -- see test_context_reconciliation_api.py
# and the other MCP contract tests for those.


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("read", "draft-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _structure_map(workspace_id, page_id, actor, page_service):
    return dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page_id)}},
        actor=actor,
        page_service=page_service,
    )


def _replace_block_op(block, content):
    return {
        "op": "replace_block",
        "block": block.id,
        "expected": block.hash,
        "content": content,
    }


def _patch_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("patch_draft", {...})``.

    Calls ContextWriteService.patch_draft directly, the same way
    test_context_write_service.py does -- patch_draft is no longer a
    reachable MCP tool, but the engine underneath it is still fully live.
    """
    with page_service.session_factory.begin() as session:
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )


def test_base_equal_to_head_is_committed(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-committed",
        title="Push Committed",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="committed push",
        base=str(page.version_id),
        operations=[_replace_block_op(paragraph, "edited")],
    )

    assert result.push_outcome == "committed"
    assert result.changed_since_base_ids == []


def test_stale_base_with_untouched_target_is_rebased_onto_head(page_service, actor):
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).auto_fix_enabled = True
    for suffix, before, replacement in (
        ("text", "first", "first, edited"),
        ("hard-break", "first\ncontinued", "first  \ncontinued"),
    ):
        slug = f"push-rebased-{suffix}"
        page = page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title="Push Rebased",
            body_md=f"# Title\n\n{before}\n\nsecond\n",
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
        )
        base_map = _structure_map(workspace_id, page.id, actor, page_service)
        first_para = next(b for b in base_map.blocks if b.kind == "p")
        second_para = next(b for b in base_map.blocks if b.preview == "second")

        # Advancing an unrelated block preserves its ID. A hard-break-only
        # candidate still has a real delta despite its unchanged canonical hash.
        page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title="Push Rebased",
            body_md=f"# Title\n\n{before}\n\nsecond!\n",
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
            base_version_id=page.version_id,
            base_content_hash=page.content_hash,
        )
        result = _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="rebase onto head",
            base=str(page.version_id),
            operations=[_replace_block_op(first_para, replacement)],
        )

        assert result.push_outcome == "rebased"
        assert result.changed_since_base_ids == [second_para.id]
        with page_service.session_factory() as session:
            stored = session.get(ContextPageVersion, result.draft_version_id)
            assert stored.body_md == f"# Title\n\n{replacement}\n\nsecond!\n"
        after_map = _structure_map(workspace_id, page.id, actor, page_service)
        assert any(b.preview.startswith("first") for b in after_map.blocks)
        assert any(b.preview == "second!" for b in after_map.blocks)


def test_stale_raw_no_effect_keeps_head_without_creating_a_branch(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-stale-no-effect",
        title="Push Stale No Effect",
        body_md="# Title\n\noriginal stable paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in base_map.blocks if block.kind == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-stale-no-effect",
        title="Push Stale No Effect",
        body_md="# Title\n\nnew head paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="authored delta is empty",
        base=str(page.version_id),
        operations=[_replace_block_op(paragraph, paragraph.preview)],
    )
    assert result.push_outcome == "rebased"
    assert result.alternative_draft_id is None
    assert result.draft_version_id == head.version_id
    assert result.operation_receipts[0].outcome == "no_effect"

    reviewed = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="empty delta still explicitly needs review",
        base=str(page.version_id),
        needs_review=True,
        operations=[_replace_block_op(paragraph, paragraph.preview)],
    )
    assert reviewed.push_outcome == "rebased"
    assert reviewed.change_set_id is not None
    assert reviewed.review_item_id is not None
    assert reviewed.review_status == "pending"
    assert reviewed.operation_receipts[0].outcome == "no_effect"


def test_partial_stale_no_effect_receipts_stay_aligned_with_caller_operations(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    base_body = "first stable paragraph\n\nsecond stable paragraph\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-partial-no-effect",
        title="Push Partial No Effect",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    first = next(
        block for block in base_map.blocks if block.preview.startswith("first")
    )
    second = next(
        block for block in base_map.blocks if block.preview.startswith("second")
    )
    page_service.write_page(
        workspace_slug="default",
        slug="push-partial-no-effect",
        title="Push Partial No Effect",
        body_md="first changed at head\n\nsecond stable paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="one empty and one real delta",
        base=str(page.version_id),
        operations=[
            _replace_block_op(first, first.preview),
            _replace_block_op(second, "second applied edit"),
        ],
    )
    assert [receipt.op for receipt in result.operation_receipts] == [
        "replace_block",
        "replace_block",
    ]
    assert [receipt.outcome for receipt in result.operation_receipts] == [
        "no_effect",
        "applied",
    ]
    assert result.operation_receipts[0].chars_delta == 0
    assert result.operation_receipts[1].chars_delta != 0
    after = _structure_map(workspace_id, page.id, actor, page_service)
    assert any(block.preview == "first changed at head" for block in after.blocks)
    assert any(block.preview == "second applied edit" for block in after.blocks)


def test_stale_normalization_no_effect_preview_and_commit_keep_head(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-normalized-no-effect",
        title="Push Normalized No Effect",
        body_md="# Title\n\noriginal stable paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in base_map.blocks if block.kind == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-normalized-no-effect",
        title="Push Normalized No Effect",
        body_md="# Title\n\nnew head paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.auto_fix_enabled = True
    payload = {
        "page_id": page.id,
        "reason": "normalization removes the authored delta",
        "base": str(page.version_id),
        "operations": [_replace_block_op(paragraph, "original stable paragraph   ")],
    }
    preview = _patch_draft(
        page_service, actor, workspace_id, **{**payload, "dry_run": True}
    )
    assert preview.push_outcome == "rebased"
    assert preview.predicted_block_map is not None
    assert preview.risk is not None
    assert preview.normalized is True
    result = _patch_draft(page_service, actor, workspace_id, **payload)
    assert result.push_outcome == "rebased"
    assert result.draft_version_id == head.version_id
    assert result.operation_receipts[0].outcome == "no_effect"
    assert result.risk == preview.risk


def test_content_identical_dry_run_and_commit_report_the_same_risk(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-noop-risk-parity",
        title="Push Noop Risk Parity",
        body_md="same paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in structure_map.blocks if block.kind == "p")
    payload = {
        "page_id": page.id,
        "reason": "content-identical risk parity",
        "base": str(page.version_id),
        "idempotency_key": "push-noop-risk-parity",
        "operations": [_replace_block_op(paragraph, "same paragraph")],
    }

    preview = _patch_draft(
        page_service, actor, workspace_id, **{**payload, "dry_run": True}
    )
    committed = _patch_draft(page_service, actor, workspace_id, **payload)

    assert preview.risk is not None
    assert committed.risk == preview.risk


@pytest.mark.parametrize(
    ("content", "expected_normalized"),
    [
        ("canonical paragraph", False),
        ("canonical paragraph   ", True),
    ],
    ids=["engine-noop", "normalization-noop"],
)
def test_normalized_identical_dry_run_predicts_the_required_successor_workflow(
    page_service, actor, content, expected_normalized
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-dry-successor-parity",
        title="Push Dry Successor Parity",
        body_md="# Title\n\ncanonical paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in base_map.blocks if block.kind == "p")
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.auto_fix_enabled = True
        base_version = session.get(ContextPageVersion, page.version_id)
        # A retired snapshot cannot be reused as the actionable draft even if
        # normalization makes the submitted body byte-identical to it.
        base_version.version_status = "superseded_snapshot"

    payload = {
        "page_id": page.id,
        "base": str(page.version_id),
        "reason": "normalized identical body still needs a successor",
        "operations": [_replace_block_op(paragraph, content)],
    }
    preview = _patch_draft(
        page_service, actor, workspace_id, **{**payload, "dry_run": True}
    )
    assert preview.draft_version_id is None
    assert preview.review_status == "pending"
    assert preview.normalized is expected_normalized
    assert preview.predicted_block_map is not None
    assert preview.risk is not None

    committed = _patch_draft(page_service, actor, workspace_id, **payload)
    assert committed.review_status == preview.review_status
    assert committed.draft_version_id != page.version_id
    assert committed.persisted_content_hash == preview.persisted_content_hash
    assert committed.persisted_character_count == preview.persisted_character_count
    assert committed.normalized == preview.normalized
    with page_service.session_factory() as session:
        successor = session.get(ContextPageVersion, committed.draft_version_id)
        assert successor.parent_version_id == page.version_id
        assert successor.body_md == "# Title\n\ncanonical paragraph\n"
        assert successor.block_map == preview.predicted_block_map


def test_stale_base_with_touched_target_diverges_into_a_branch(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-diverged",
        title="Push Diverged",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in base_map.blocks if b.kind == "p")
    base_version_id = str(page.version_id)

    # Someone else changes the SAME block this request will target.
    page_service.write_page(
        workspace_slug="default",
        slug="push-diverged",
        title="Push Diverged",
        body_md="# Title\n\nfirst, changed by someone else\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="conflicting push",
        base=base_version_id,
        operations=[_replace_block_op(paragraph, "first, my edit")],
    )

    assert result.push_outcome == "diverged"
    assert result.changed_since_base_ids == [paragraph.id]

    # The target paragraph's content rewrite at head passed block_identity's
    # similarity threshold, so head now holds a NEW id for that slot (base's
    # old id is what `conflicting_ids` names) -- that new id legitimately
    # shows up as its own "clean change" (it is not itself in conflict; it
    # just occupies the same conceptual position the conflicting edit does).
    assert len(result.clean_changes) == 1
    assert result.clean_changes[0]["summary"] == "first, changed by someone else"
    assert result.resolution_instruction
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["kind"] == "p"
    # This rewrite retires the old paragraph identity entirely; remappable
    # table-row conflicts exercise the non-null form below.
    assert conflict["resolution_target_id"] is None
    assert conflict["base"]["text"] == "first"
    assert conflict.get("yours") is not None
    assert conflict["yours"]["text"] == "first, my edit"
    assert conflict["head"]["deleted"] is None
    assert conflict["head"]["identity_retired"] is True
    assert conflict["head"]["text"] is None
    assert conflict.get("markers") is None
    assert result.alternative_draft_id is not None

    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, result.alternative_draft_id)
        assert branch.graph_status == "branched"
        assert branch.version_status == "draft"
        assert branch.parent_version_id.hex == base_version_id.replace("-", "")
        assert "my edit" in branch.body_md
        assert branch.reconcile_manifest["base"]["version_id"] == base_version_id
        assert branch.reconcile_manifest["head"]["version_id"] != base_version_id
        assert branch.reconcile_manifest["conflicting_ids"] == [paragraph.id]
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == branch.change_set_id,
                ChangeOperation.operation == "patch_draft_diverged_manifest",
            )
        )
        assert operation is not None
        assert "my edit" not in operation.diff_summary
        assert operation.diff_ref == f"context_page_version:{branch.id}"
        assert session.scalar(
            select(ReviewItem).where(ReviewItem.change_set_id == branch.change_set_id)
        )
        assert session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.diverged",
                AuditEvent.target_id == page.id,
            )
        )

    # Head itself was never touched by the diverged push.
    after_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_paragraph = next(b for b in after_map.blocks if b.kind == "p")
    assert "changed by someone else" in head_paragraph.preview
    assert "my edit" not in head_paragraph.preview

    # A hard-break-only HEAD change must also conflict: its canonical block
    # hash equals base, but rebasing would overwrite the author's line break.
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).auto_fix_enabled = True
    hard_break_base = page_service.write_page(
        workspace_slug="default",
        slug="push-diverged-hard-break",
        title="Push Diverged",
        body_md="# Title\n\nfirst\ncontinued\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    hard_break_map = _structure_map(
        workspace_id, hard_break_base.id, actor, page_service
    )
    hard_break_target = next(b for b in hard_break_map.blocks if b.kind == "p")
    hard_break_head = page_service.write_page(
        workspace_slug="default",
        slug="push-diverged-hard-break",
        title="Push Diverged",
        body_md="# Title\n\nfirst  \ncontinued\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=hard_break_base.version_id,
        base_content_hash=hard_break_base.content_hash,
    )
    assert hard_break_head.content_hash == hard_break_base.content_hash
    hard_break_result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=hard_break_base.id,
        reason="conflict with a hard break added at head",
        base=str(hard_break_base.version_id),
        operations=[_replace_block_op(hard_break_target, "first, my edit\ncontinued")],
    )
    assert hard_break_result.push_outcome == "diverged"
    assert hard_break_result.changed_since_base_ids == [hard_break_target.id]
    assert hard_break_result.conflicts[0]["head"]["text"] == "first  \ncontinued"
    with page_service.session_factory() as session:
        assert (
            session.get(ContextPageVersion, hard_break_head.version_id).body_md
            == "# Title\n\nfirst  \ncontinued\n"
        )
        assert (
            session.get(
                ContextPageVersion, hard_break_result.alternative_draft_id
            ).body_md
            == "# Title\n\nfirst, my edit\ncontinued\n"
        )


def test_unknown_base_is_rejected_with_a_recoverable_error(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-unknown-base",
        title="Push Unknown Base",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="bad base",
            base="00000000-0000-0000-0000-0000000000ff",
            operations=[_replace_block_op(paragraph, "edited")],
        )

    assert exc_info.value.error_code == "base_not_found"


def test_omitted_base_without_a_read_ledger_fails_closed(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-omitted-base",
        title="Push Omitted Base",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="no base supplied",
            operations=[_replace_block_op(paragraph, "edited")],
        )

    assert exc_info.value.error_code == "base_required"


def test_stale_base_for_legacy_text_operation_fails_closed(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-stale-legacy",
        title="Push Stale Legacy",
        body_md="# Title\n\nalpha\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    page_service.write_page(
        workspace_slug="default",
        slug="push-stale-legacy",
        title="Push Stale Legacy",
        body_md="# Title\n\nalpha?\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )

    with pytest.raises(StaleBaseVersionError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="stale legacy patch",
            base=str(page.version_id),
            operations=[
                {"op": "replace_text", "old_text": "alpha", "new_text": "mine"}
            ],
        )

    assert exc_info.value.error_code == "stale_base_version"
    after = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph_after = next(block for block in after.blocks if block.kind == "p")
    assert paragraph_after.preview == "alpha?"


def test_reverted_identical_body_requires_unambiguous_version_id(page_service, actor):
    workspace_id = _workspace_id(page_service)
    first = page_service.write_page(
        workspace_slug="default",
        slug="push-ambiguous-hash",
        title="Push Ambiguous Hash",
        body_md="# Title\n\nalpha\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    second = page_service.write_page(
        workspace_slug="default",
        slug="push-ambiguous-hash",
        title="Push Ambiguous Hash",
        body_md="# Title\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=first.version_id,
        base_content_hash=first.content_hash,
    )
    reverted = page_service.write_page(
        workspace_slug="default",
        slug="push-ambiguous-hash",
        title="Push Ambiguous Hash",
        body_md="# Title\n\nalpha\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=second.version_id,
        base_content_hash=second.content_hash,
    )
    structure_map = _structure_map(workspace_id, first.id, actor, page_service)
    paragraph = next(block for block in structure_map.blocks if block.kind == "p")

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=first.id,
            reason="ambiguous hash",
            base=first.content_hash,
            operations=[_replace_block_op(paragraph, "edited")],
        )
    assert exc_info.value.error_code == "base_ambiguous"

    exact = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=first.id,
        reason="exact reverted version",
        base=str(reverted.version_id),
        operations=[_replace_block_op(paragraph, "edited")],
    )
    assert exact.push_outcome == "committed"


def test_dry_run_diverged_push_previews_the_conflict_payload_without_branching(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-diverged-dry-run",
        title="Push Diverged Dry Run",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in base_map.blocks if b.kind == "p")
    base_version_id = str(page.version_id)

    page_service.write_page(
        workspace_slug="default",
        slug="push-diverged-dry-run",
        title="Push Diverged Dry Run",
        body_md="# Title\n\nfirst, changed by someone else\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )

    result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="preview a conflicting push",
        base=base_version_id,
        dry_run=True,
        operations=[_replace_block_op(paragraph, "first, my edit")],
    )

    assert result.push_outcome == "diverged"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].get("yours") is not None
    assert result.conflicts[0]["yours"]["text"] == "first, my edit"
    assert result.alternative_draft_id is None

    # Nothing was actually branched or persisted.
    after_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_paragraph = next(b for b in after_map.blocks if b.kind == "p")
    assert head_paragraph.preview == "first, changed by someone else"

    commit_result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="preview a conflicting push",
        base=base_version_id,
        operations=[_replace_block_op(paragraph, "first, my edit")],
    )
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, commit_result.alternative_draft_id)
        assert branch.block_map == result.predicted_block_map


def test_reconcile_carries_the_branchs_clean_change_and_discards_the_branch(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile",
        title="Push Reconcile",
        body_md="# Title\n\nfirst\n\nsecond\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    first_para = next(b for b in base_map.blocks if b.preview == "first")
    second_para = next(b for b in base_map.blocks if b.preview == "second")
    base_version_id = str(page.version_id)

    # Someone else makes a SMALL edit to "first" -- small enough that
    # block_identity's fuzzy tier keeps its id stable (see this file's
    # other tests for why: a rewrite past the similarity threshold would
    # instead churn the id, which is a separate, documented limitation of
    # synthesize_clean_rebase_ops, not what this test is covering).
    page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile",
        title="Push Reconcile",
        body_md="# Title\n\nfirst?\n\nsecond\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )

    # A batched diverging push small-edits BOTH paragraphs -- "first!"
    # conflicts with head's "first?", "second!" is the branch's own clean
    # contribution.
    diverge_result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="batched diverging push",
        base=base_version_id,
        operations=[
            _replace_block_op(first_para, "first!"),
            _replace_block_op(second_para, "second!"),
        ],
    )
    assert diverge_result.push_outcome == "diverged"
    branch_id = diverge_result.alternative_draft_id
    conflict = diverge_result.conflicts[0]
    assert conflict["block_id"] == first_para.id
    assert conflict["resolution_target_id"] is not None

    head_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_first = next(b for b in head_map.blocks if b.id == first_para.id)
    assert head_first.preview == "first?"

    reconcile_result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="reconcile diverged branch",
        base=str(head_map.document.version_id),
        reconciles=[branch_id],
        operations=[
            {
                "op": "replace_block",
                "block": head_first.id,
                "expected": head_first.hash,
                "content": "first, reconciled",
            }
        ],
    )

    assert reconcile_result.push_outcome in ("committed", "rebased")

    final_map = _structure_map(workspace_id, page.id, actor, page_service)
    previews = {b.preview for b in final_map.blocks}
    assert "first, reconciled" in previews
    assert "second!" in previews  # the branch's clean carry landed

    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, branch_id)
        assert branch.graph_status == "superseded"


def test_reconcile_rejects_when_any_original_conflict_is_unresolved(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-incomplete",
        title="Push Reconcile Incomplete",
        body_md="# Title\n\nfirst\n\nsecond\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    first = next(block for block in base_map.blocks if block.preview == "first")
    second = next(block for block in base_map.blocks if block.preview == "second")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-incomplete",
        title="Push Reconcile Incomplete",
        body_md="# Title\n\nfirst?\n\nsecond?\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="two conflicts",
        base=str(page.version_id),
        operations=[
            _replace_block_op(first, "first!"),
            _replace_block_op(second, "second!"),
        ],
    )
    assert {item["block_id"] for item in diverged.conflicts} == {
        first.id,
        second.id,
    }
    head_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_first = next(block for block in head_map.blocks if block.id == first.id)

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="resolve only one conflict",
            base=str(head.version_id),
            reconciles=[diverged.alternative_draft_id],
            operations=[_replace_block_op(head_first, "first resolved")],
        )
    assert exc_info.value.error_code == "reconcile_target_invalid"
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert branch.graph_status == "branched"


def test_noop_reconcile_closes_branch_review_and_audits_lifecycle(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-noop",
        title="Push Reconcile Noop",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in base_map.blocks if block.kind == "p")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-noop",
        title="Push Reconcile Noop",
        body_md="# Title\n\nfirst?\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="branch to resolve as head",
        base=str(page.version_id),
        operations=[_replace_block_op(paragraph, "first!")],
    )
    head_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_paragraph = next(
        block for block in head_map.blocks if block.id == paragraph.id
    )

    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="keep head",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[_replace_block_op(head_paragraph, "first?")],
    )
    assert reconciled.draft_version_id != head.version_id
    assert reconciled.push_outcome == "committed"
    assert reconciled.changed_since_base_ids == []
    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, reconciled.draft_version_id)
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert merged.parent_version_id == head.version_id
        assert merged.version_status == "draft"
        assert merged.body_md == "# Title\n\nfirst?\n"
        assert branch.graph_status == "superseded"
        review = session.scalar(
            select(ReviewItem).where(ReviewItem.change_set_id == branch.change_set_id)
        )
        assert review.status == "superseded"
        assert session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.reconciled",
                AuditEvent.target_id == page.id,
            )
        )


def test_retired_conflict_keep_head_is_a_no_effect_decision_that_closes_branch(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-retired-keep-head",
        title="Push Retired Keep Head",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    alpha = next(block for block in base_map.blocks if block.preview == "alpha")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-retired-keep-head",
        title="Push Retired Keep Head",
        body_md="beta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(page.version_id),
        reason="retired conflict",
        operations=[_replace_block_op(alpha, "alpha authored")],
    )
    assert diverged.conflicts[0]["resolution_target_id"] is None
    with page_service.session_factory.begin() as session:
        branch_review = session.get(ReviewItem, diverged.review_item_id)
        assert branch_review.review_type == "context_reconciliation"
        branch_review.status = "changes_requested"

    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        reason="keep retired head decision",
        operations=[
            {
                "op": "resolve_conflict",
                "block": alpha.id,
                "resolution": "keep_head",
            }
        ],
    )
    assert reconciled.draft_version_id != head.version_id
    assert reconciled.push_outcome == "committed"
    assert len(reconciled.operation_receipts) == 1
    assert reconciled.operation_receipts[0].op == "resolve_conflict"
    assert reconciled.operation_receipts[0].outcome == "no_effect"
    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, reconciled.draft_version_id)
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        review = session.get(ReviewItem, diverged.review_item_id)
        assert merged.parent_version_id == head.version_id
        assert merged.version_status == "draft"
        assert merged.body_md == head.body_md
        assert branch.graph_status == "superseded"
        assert review.status == "superseded"


def test_reconcile_redivergence_carries_lineage_and_closes_only_the_source_branch(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    base_body = (
        "first paragraph with stable identity\n\n"
        "second paragraph with clean contribution\n"
    )
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-rediverged-lineage",
        title="Push Reconcile Rediverged Lineage",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    first = next(
        block for block in base_map.blocks if block.preview.startswith("first")
    )
    second = next(
        block for block in base_map.blocks if block.preview.startswith("second")
    )
    head_one = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-rediverged-lineage",
        title="Push Reconcile Rediverged Lineage",
        body_md=base_body.replace("stable identity", "stable identity head one"),
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    d1 = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(page.version_id),
        reason="D1 conflict plus clean contribution",
        operations=[
            _replace_block_op(first, "first paragraph authored in D1"),
            _replace_block_op(second, "second paragraph carried from D1"),
        ],
    )
    assert d1.push_outcome == "diverged"
    head_one_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_one_first = next(
        block for block in head_one_map.blocks if block.id == first.id
    )

    # A normal page update must not page-wide supersede the independent D1
    # reconciliation workflow, even though it advances the same linear head.
    head_two = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        base=str(head_one.version_id),
        reason="advance head before reconciling D1",
        operations=[
            _replace_block_op(
                head_one_first,
                "first paragraph with stable identity head two",
            )
        ],
    )
    assert head_two.push_outcome == "committed"
    with page_service.session_factory() as session:
        assert session.get(ReviewItem, d1.review_item_id).status == "pending"
        assert (
            session.get(ContextPageVersion, d1.alternative_draft_id).graph_status
            == "branched"
        )

    d2 = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        # Reconcile exactly the original D1 head. The now-newer linear
        # head changed the same target, so this must become D2.
        base=str(head_one.version_id),
        reconciles=[d1.alternative_draft_id],
        reason="D1 reconciliation rediverges into D2",
        operations=[
            _replace_block_op(head_one_first, "first paragraph resolved from D1")
        ],
    )
    assert d2.push_outcome == "diverged"
    assert d2.alternative_draft_id != d1.alternative_draft_id
    with page_service.session_factory() as session:
        d1_branch = session.get(ContextPageVersion, d1.alternative_draft_id)
        d2_branch = session.get(ContextPageVersion, d2.alternative_draft_id)
        d1_review = session.get(ReviewItem, d1.review_item_id)
        d1_change_set = session.get(ChangeSet, d1.change_set_id)
        d2_review = session.get(ReviewItem, d2.review_item_id)
        assert d1_branch.graph_status == "superseded"
        assert d1_review.status == "superseded"
        assert d1_change_set.status == "superseded"
        assert d2_branch.graph_status == "branched"
        assert d2_review.status == "pending"
        assert d2_review.review_type == "context_reconciliation"
        assert d2_branch.reconcile_manifest["supersedes_branch_version_id"] == str(
            d1_branch.id
        )
        assert "second paragraph carried from D1" in d2_branch.body_md
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.reconcile_rediverged",
                AuditEvent.target_id == page.id,
            )
        )
        assert audit.payload["branch_version_id"] == str(d1_branch.id)
        assert audit.payload["successor_branch_version_id"] == str(d2_branch.id)


@pytest.mark.parametrize(
    ("clean_kind", "body", "expected_text", "must_exist"),
    [
        (
            "replace_block",
            "# Title\n\nconflict\n\nclean target\n",
            "clean replaced",
            True,
        ),
        (
            "insert_after",
            "# Title\n\nconflict\n\ninsert anchor\n",
            "inserted clean",
            True,
        ),
        (
            "delete_block",
            "# Title\n\nconflict\n\ndelete target\n",
            "delete target",
            False,
        ),
        (
            "insert_row",
            ("# Title\n\nconflict\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"),
            "| 3 | 4 |",
            True,
        ),
    ],
)
def test_reconcile_preserves_each_nonconflicting_operation_kind(
    page_service,
    actor,
    clean_kind,
    body,
    expected_text,
    must_exist,
):
    workspace_id = _workspace_id(page_service)
    slug = f"push-reconcile-clean-{clean_kind.replace('_', '-')}"
    page = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=f"Push Reconcile Clean {clean_kind}",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    conflict = next(block for block in base_map.blocks if block.preview == "conflict")
    if clean_kind == "insert_row":
        table = next(block for block in base_map.blocks if block.kind == "table")
        row = table.rows[0]
        clean_operation = {
            "op": "insert_row",
            "row": row.id,
            "expected": row.hash,
            "content": "| 3 | 4 |",
        }
    else:
        target = next(
            block
            for block in base_map.blocks
            if block.kind == "p" and block.id != conflict.id
        )
        clean_operation = {
            "op": clean_kind,
            "block": target.id,
            "expected": target.hash,
        }
        if clean_kind == "replace_block":
            clean_operation["content"] = "clean replaced"
        elif clean_kind == "insert_after":
            clean_operation["content"] = "inserted clean"

    head = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=f"Push Reconcile Clean {clean_kind}",
        body_md=body.replace("\nconflict\n", "\nconflict?\n", 1),
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason=f"diverge with clean {clean_kind}",
        base=str(page.version_id),
        operations=[
            _replace_block_op(conflict, "conflict!"),
            clean_operation,
        ],
    )
    assert diverged.push_outcome == "diverged"
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert [
            operation["op"]
            for operation in branch.reconcile_manifest["clean_operations"]
        ] == [clean_kind]

    head_map = _structure_map(workspace_id, page.id, actor, page_service)
    head_conflict = next(block for block in head_map.blocks if block.id == conflict.id)
    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason=f"reconcile clean {clean_kind}",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[_replace_block_op(head_conflict, "conflict resolved")],
    )
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, reconciled.draft_version_id)
        assert (expected_text in version.body_md) is must_exist


def test_changed_table_row_conflict_reports_and_reconciles_current_row_id(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    base_body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-row",
        title="Push Reconcile Row",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    old_row = next(block for block in base_map.blocks if block.kind == "table").rows[0]
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-row",
        title="Push Reconcile Row",
        body_md="| a | b |\n|---|---|\n| 9 | 2 |\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="insert after changed row",
        base=str(page.version_id),
        operations=[
            {
                "op": "insert_row",
                "row": old_row.id,
                "expected": old_row.hash,
                "content": "| 3 | 4 |",
            }
        ],
    )
    assert diverged.push_outcome == "diverged"
    assert len(diverged.conflicts) == 1
    conflict = diverged.conflicts[0]
    assert conflict["block_id"] == old_row.id
    assert conflict["kind"] == "table_row"
    assert conflict["head"]["text"] == "| 9 | 2 |"
    assert conflict["resolution_target_id"] is not None

    head_map = _structure_map(workspace_id, page.id, actor, page_service)
    current_row = next(
        block for block in head_map.blocks if block.kind == "table"
    ).rows[0]
    assert conflict["resolution_target_id"] == current_row.id
    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="reconcile changed row",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[
            {
                "op": "insert_row",
                "row": current_row.id,
                "expected": current_row.hash,
                "content": "| 3 | 4 |",
            }
        ],
    )
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, reconciled.draft_version_id)
        assert "| 9 | 2 |" in version.body_md
        assert "| 3 | 4 |" in version.body_md


@pytest.mark.parametrize(
    ("target_text", "head_body", "expected_body"),
    [
        ("alpha", "beta\n\ngamma\n", "alpha restored\n\nbeta\n\ngamma\n"),
        ("beta", "alpha\n\ngamma\n", "alpha\n\nbeta restored\n\ngamma\n"),
        ("gamma", "alpha\n\nbeta\n", "alpha\n\nbeta\n\ngamma restored\n"),
    ],
)
def test_retired_conflict_restore_yours_uses_durable_neighbor_proof(
    page_service, actor, target_text, head_body, expected_body
):
    workspace_id = _workspace_id(page_service)
    base_body = "alpha\n\nbeta\n\ngamma\n"
    slug = f"push-restore-{target_text}"
    page = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=f"Restore {target_text}",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    target = next(block for block in base_map.blocks if block.preview == target_text)
    head = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=f"Restore {target_text}",
        body_md=head_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="restore retired contribution",
        base=str(page.version_id),
        operations=[_replace_block_op(target, f"{target_text} restored")],
    )
    conflict = diverged.conflicts[0]
    assert conflict["resolution_target_id"] is None
    assert conflict["head"]["deleted"] is True
    assert conflict["head"]["identity_retired"] is True
    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="choose authored retired contribution",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[
            {
                "op": "resolve_conflict",
                "block": target.id,
                "resolution": "restore_yours",
            }
        ],
    )
    assert reconciled.operation_receipts[0].op == "resolve_conflict"
    assert reconciled.operation_receipts[0].outcome == "applied"
    assert reconciled.operation_receipts[0].chars_delta > 0
    assert reconciled.operation_receipts[0].resolved_line_range is not None
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, reconciled.draft_version_id)
        assert version.body_md == expected_body
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert branch.graph_status == "superseded"


def test_adjacent_retired_conflicts_restore_as_one_ordered_gap_transition(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-adjacent-retired",
        title="Restore Adjacent Retired",
        body_md="alpha\n\nbeta\n\ngamma\n\ndelta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    beta = next(block for block in base_map.blocks if block.preview == "beta")
    gamma = next(block for block in base_map.blocks if block.preview == "gamma")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-adjacent-retired",
        title="Restore Adjacent Retired",
        body_md="alpha\n\ndelta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="restore two adjacent retired contributions",
        base=str(page.version_id),
        operations=[
            _replace_block_op(beta, "beta restored"),
            _replace_block_op(gamma, "gamma restored"),
        ],
    )
    assert diverged.push_outcome == "diverged"
    assert {entry["block_id"] for entry in diverged.conflicts} == {
        beta.id,
        gamma.id,
    }

    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="restore the complete adjacent retired set",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[
            {
                "op": "resolve_conflict",
                "block": beta.id,
                "resolution": "restore_yours",
            },
            {
                "op": "resolve_conflict",
                "block": gamma.id,
                "resolution": "restore_yours",
            },
        ],
    )

    with page_service.session_factory() as session:
        merged = session.get(ContextPageVersion, reconciled.draft_version_id)
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert merged.body_md == ("alpha\n\nbeta restored\n\ngamma restored\n\ndelta\n")
        assert branch.graph_status == "superseded"


def test_retired_rewrite_restore_yours_fails_closed_without_durable_target_provenance(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    base_body = "alpha\n\nbeta\n\ngamma\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-rewrite",
        title="Restore Rewrite",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    target = next(block for block in base_map.blocks if block.preview == "beta")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-rewrite",
        title="Restore Rewrite",
        body_md="alpha\n\ncompletely unrelated head rewrite\n\ngamma\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="branch over retired rewrite",
        base=str(page.version_id),
        operations=[_replace_block_op(target, "beta restored")],
    )
    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="replace bounded rewritten region",
            base=str(head.version_id),
            reconciles=[diverged.alternative_draft_id],
            operations=[
                {
                    "op": "resolve_conflict",
                    "block": target.id,
                    "resolution": "restore_yours",
                }
            ],
        )
    assert exc_info.value.error_code == "reconcile_target_invalid"
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, head.version_id)
        assert version.body_md == (
            "alpha\n\ncompletely unrelated head rewrite\n\ngamma\n"
        )
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert branch.graph_status == "branched"


def test_retired_restore_yours_does_not_overwrite_a_separately_inserted_head_block(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-independent-head-insert",
        title="Restore Independent Head Insert",
        body_md="alpha\n\nbeta\n\ngamma\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    alpha = next(block for block in base_map.blocks if block.preview == "alpha")
    beta = next(block for block in base_map.blocks if block.preview == "beta")
    deleted = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="delete beta at head",
        base=str(page.version_id),
        operations=[
            {
                "op": "delete_block",
                "block": beta.id,
                "expected": beta.hash,
            }
        ],
    )
    after_delete_map = _structure_map(workspace_id, page.id, actor, page_service)
    current_alpha = next(
        block for block in after_delete_map.blocks if block.id == alpha.id
    )
    inserted = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="insert an independent head contribution",
        base=str(deleted.draft_version_id),
        operations=[
            {
                "op": "insert_after",
                "block": current_alpha.id,
                "expected": current_alpha.hash,
                "content": "unrelated X",
            }
        ],
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="author beta from the stale base",
        base=str(page.version_id),
        operations=[_replace_block_op(beta, "beta yours")],
    )
    assert diverged.conflicts[0]["head"]["deleted"] is None

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="do not overwrite the independent head block",
            base=str(inserted.draft_version_id),
            reconciles=[diverged.alternative_draft_id],
            operations=[
                {
                    "op": "resolve_conflict",
                    "block": beta.id,
                    "resolution": "restore_yours",
                }
            ],
        )

    assert exc_info.value.error_code == "reconcile_target_invalid"
    with page_service.session_factory() as session:
        head = session.get(ContextPageVersion, inserted.draft_version_id)
        assert "unrelated X" in head.body_md
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert branch.graph_status == "branched"


def test_retired_multiblock_insert_restore_yours_fails_closed(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-multiblock-insert",
        title="Restore Multiblock Insert",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    alpha = next(block for block in base_map.blocks if block.preview == "alpha")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-multiblock-insert",
        title="Restore Multiblock Insert",
        body_md="beta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="author two inserted blocks",
        base=str(page.version_id),
        operations=[
            {
                "op": "insert_after",
                "block": alpha.id,
                "expected": alpha.hash,
                "content": "mine one\n\nmine two",
            }
        ],
    )

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="multi-block restore must not truncate",
            base=str(head.version_id),
            reconciles=[diverged.alternative_draft_id],
            operations=[
                {
                    "op": "resolve_conflict",
                    "block": alpha.id,
                    "resolution": "restore_yours",
                }
            ],
        )

    assert exc_info.value.error_code == "reconcile_target_invalid"
    with page_service.session_factory() as session:
        branch = session.get(ContextPageVersion, diverged.alternative_draft_id)
        assert "mine one" in branch.body_md
        assert "mine two" in branch.body_md
        assert branch.graph_status == "branched"


def test_retired_single_block_insert_restore_yours_preserves_the_complete_delta(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-single-block-insert",
        title="Restore Single Block Insert",
        body_md="alpha\n\nbeta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    alpha = next(block for block in base_map.blocks if block.preview == "alpha")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-single-block-insert",
        title="Restore Single Block Insert",
        body_md="beta\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="author one inserted block",
        base=str(page.version_id),
        operations=[
            {
                "op": "insert_after",
                "block": alpha.id,
                "expected": alpha.hash,
                "content": "mine one",
            }
        ],
    )

    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="restore one complete inserted block",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[
            {
                "op": "resolve_conflict",
                "block": alpha.id,
                "resolution": "restore_yours",
            }
        ],
    )

    assert reconciled.operation_receipts[0].chars_delta > 0
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, reconciled.draft_version_id)
        assert version.body_md == "mine one\n\nbeta\n"


def test_restore_yours_receipt_skips_internal_clean_carry_receipts(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-receipt-projection",
        title="Restore Receipt Projection",
        body_md="alpha\n\nbeta\n\ngamma\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    alpha = next(block for block in base_map.blocks if block.preview == "alpha")
    gamma = next(block for block in base_map.blocks if block.preview == "gamma")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-receipt-projection",
        title="Restore Receipt Projection",
        body_md="beta\n\ngamma\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="one retired conflict plus one clean contribution",
        base=str(page.version_id),
        operations=[
            _replace_block_op(alpha, "alpha authored"),
            _replace_block_op(gamma, "gamma clean"),
        ],
    )
    reconciled = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="project only the caller restoration receipt",
        base=str(head.version_id),
        reconciles=[diverged.alternative_draft_id],
        operations=[
            {
                "op": "resolve_conflict",
                "block": alpha.id,
                "resolution": "restore_yours",
            }
        ],
    )

    assert len(reconciled.operation_receipts) == 1
    receipt = reconciled.operation_receipts[0]
    assert receipt.op == "resolve_conflict"
    assert receipt.chars_delta > 0
    assert receipt.resolved_line_range is not None
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, reconciled.draft_version_id)
        assert version.body_md == "alpha authored\n\nbeta\n\ngamma clean\n"


def test_retired_restore_yours_fails_closed_without_a_unique_neighbor_region(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    base_body = "alpha\n\nbeta\n\ngamma\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-ambiguous",
        title="Restore Ambiguous",
        body_md=base_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    target = next(block for block in base_map.blocks if block.preview == "beta")
    head = page_service.write_page(
        workspace_slug="default",
        slug="push-restore-ambiguous",
        title="Restore Ambiguous",
        body_md="unrelated one\n\nunrelated two\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverged = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="ambiguous retired branch",
        base=str(page.version_id),
        operations=[_replace_block_op(target, "beta restored")],
    )
    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="ambiguous restore must fail",
            base=str(head.version_id),
            reconciles=[diverged.alternative_draft_id],
            operations=[
                {
                    "op": "resolve_conflict",
                    "block": target.id,
                    "resolution": "restore_yours",
                }
            ],
        )
    assert exc_info.value.error_code == "reconcile_target_invalid"


def test_reconcile_rejects_a_version_that_is_not_an_active_diverged_branch(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-invalid",
        title="Push Reconcile Invalid",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="reconcile the head itself",
            base=str(page.version_id),
            reconciles=[page.version_id],  # head, not a diverged branch
            operations=[_replace_block_op(paragraph, "edited")],
        )

    assert exc_info.value.error_code == "reconcile_target_invalid"


def test_reconcile_rejects_non_block_addressed_operations(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-legacy-ops",
        title="Push Reconcile Legacy Ops",
        body_md="# Title\n\nfirst\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(b for b in structure_map.blocks if b.kind == "p")

    head = page_service.write_page(
        workspace_slug="default",
        slug="push-reconcile-legacy-ops",
        title="Push Reconcile Legacy Ops",
        body_md="# Title\n\nfirst, changed elsewhere\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    diverge_result = _patch_draft(
        page_service,
        actor,
        workspace_id,
        page_id=page.id,
        reason="diverge",
        base=str(page.version_id),
        operations=[_replace_block_op(paragraph, "my edit")],
    )
    branch_id = diverge_result.alternative_draft_id

    with pytest.raises(ReadValidationError) as exc_info:
        _patch_draft(
            page_service,
            actor,
            workspace_id,
            page_id=page.id,
            reason="reconcile with a legacy op",
            base=str(head.version_id),
            reconciles=[branch_id],
            operations=[
                {"op": "replace_text", "old_text": "first", "new_text": "second"}
            ],
        )

    assert exc_info.value.error_code == "reconcile_target_invalid"
