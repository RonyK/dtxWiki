from dataclasses import replace

from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.audit import AuditEvent
from backend.models.change import ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest
from backend.services.context_write_service import ContextWriteService


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _structure_map(workspace_id, page_id, actor, page_service):
    result = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page_id)},
        },
        actor=actor,
        page_service=page_service,
    )
    assert result.envelope.result_status == "succeeded"
    return result


def test_browser_alternative_cap_cannot_discard_reconciliation_branch(
    page_service,
    mcp_default_actor,
    monkeypatch,
):
    actor = mcp_default_actor(scopes=("read", "draft-write"))
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-branch-lifecycle",
        title="Reconciliation Branch Lifecycle",
        body_md="# Title\n\nbase paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    base_map = _structure_map(workspace_id, page.id, actor, page_service)
    paragraph = next(block for block in base_map.blocks if block.kind == "p")
    page_service.write_page(
        workspace_slug="default",
        slug="reconciliation-branch-lifecycle",
        title="Reconciliation Branch Lifecycle",
        body_md="# Title\n\nhead rewrite\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    patch_request = PatchDraftRequest(
        page_id=page.id,
        reason="create durable reconciliation branch",
        base=str(page.version_id),
        operations=[
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "branch rewrite",
            }
        ],
    )
    with page_service.session_factory.begin() as session:
        diverged = ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=patch_request,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            auto_approve_format_only=True,
        )
    assert diverged.push_outcome == "diverged"
    branch_id = diverged.alternative_draft_id
    assert branch_id is not None
    assert diverged.review_item_id is not None
    assert diverged.change_set_id is not None

    monkeypatch.setattr(
        "backend.services.page_service.MAX_ACTIVE_ALTERNATIVE_DRAFTS", 1
    )
    oldest_browser_branch = page_service.update_page(
        page_id=page.id,
        title="Oldest browser alternative",
        body_md="# Title\n\noldest browser alternative\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
        on_stale="preserve_as_alternative",
    )
    assert oldest_browser_branch.preserved_as_alternative is True
    newest_browser_branch = page_service.update_page(
        page_id=page.id,
        title="Newest browser alternative",
        body_md="# Title\n\nnewest browser alternative\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
        on_stale="preserve_as_alternative",
    )
    assert newest_browser_branch.preserved_as_alternative is True

    with page_service.session_factory() as session:
        assert session.get(ContextPageVersion, branch_id).graph_status == "branched"
        oldest_browser_version = session.get(
            ContextPageVersion, oldest_browser_branch.alternative_draft_id
        )
        newest_browser_version = session.get(
            ContextPageVersion, newest_browser_branch.alternative_draft_id
        )
        assert oldest_browser_version.graph_status == "discarded"
        assert newest_browser_version.graph_status == "branched"

    editor = replace(
        actor,
        actor_kind="human",
        workspace_role="editor",
        role="editor",
        token_kind="session",
        provenance="browser_session",
    )
    discarded = page_service.discard_alternative(
        page_id=page.id,
        version_id=branch_id,
        reason="branch is no longer needed",
        actor=editor,
    )
    assert discarded.is_discarded is True

    with page_service.session_factory() as session:
        review = session.get(ReviewItem, diverged.review_item_id)
        change_set = session.get(ChangeSet, diverged.change_set_id)
        assert review.review_type == "context_reconciliation"
        assert review.status == "superseded"
        assert review.claimed_by is None
        assert review.claimed_at is None
        assert change_set.status == "superseded"
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "context_page.draft.diverged_discarded",
                AuditEvent.target_id == branch_id,
            )
        )
        assert audit is not None
        assert audit.payload["reason"] == "branch is no longer needed"
        assert audit.payload["resolution"] == "explicit_discard"
        assert audit.payload["review_item_ids"] == [str(review.id)]
