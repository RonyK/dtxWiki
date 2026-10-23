"""Golden persisted bytes at the body preparation writer boundaries.

Keep this one collected scenario: each writer has a different persistence path,
while Auto-fix on/off are two traversals of the same writer matrix. Expected
bytes are literals captured before BodyPreparer extraction, never calculated
by the normalizer or preparer under test.
"""

from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.ask import ContextIngressProposal
from backend.models.context import ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest, ReplaceTextOperation
from backend.schemas.context_ingress import ContextIngressAcceptRequest
from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.services.context_ingress_service import ContextIngressService
from backend.services.context_write_service import ContextWriteService
from backend.services.okf import import_okf_bundle
from backend.utils.content_hash import content_hash

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
RAW = "# Golden\n\n* item\n\n\n\nend   \n"
FIXED = "# Golden\n\n- item\n\nend\n"
BASE = "# Golden\n\nbase\n"


def _assert_stored(session_factory, version_id, expected, label):
    with session_factory() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None, label
        assert version.body_md.encode("utf-8") == expected.encode("utf-8"), label


def _published(page_service, slug, body=BASE):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title="Golden",
        body_md=body,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )


def _advance(page_service, created):
    return page_service.update_page(
        page_id=created.id,
        title="Golden",
        body_md="# Golden\n\nhead\n",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
    )


def _replace_base():
    return ReplaceTextOperation(
        op="replace_text", old_text="base", new_text="* item\n\n\n\nend   "
    )


def test_all_body_writers_preserve_golden_persisted_bytes(
    page_service, session_factory, mcp_default_actor
):
    """R4 W1-W12, including intentional browser/patch/ingress skip paths."""
    service = ContextWriteService()
    reader = mcp_default_actor(scopes=("read", "draft-write"))
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

    for enabled in (False, True):
        suffix = "on" if enabled else "off"
        expected = FIXED if enabled else RAW
        with session_factory.begin() as session:
            session.get(Workspace, workspace_id).auto_fix_enabled = enabled

        # W1: first editing-version creation.
        first = page_service.create_page_first(
            workspace_slug="default",
            title="Golden",
            body_md=RAW,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        _assert_stored(session_factory, first.version_id, expected, f"W1 {suffix}")

        # W2: legacy slug writer; W3: explicit page update.
        written = _published(page_service, f"w2-{suffix}", RAW)
        _assert_stored(session_factory, written.version_id, expected, f"W2 {suffix}")
        base = _published(page_service, f"w3-{suffix}")
        updated = page_service.update_page(
            page_id=base.id,
            title="Golden",
            body_md=RAW,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
            base_version_id=base.version_id,
            base_content_hash=base.content_hash,
        )
        _assert_stored(session_factory, updated.version_id, expected, f"W3 {suffix}")

        # W4: a normal browser autosave preserves authored bytes even with
        # Auto-fix enabled; the AI provenance of the same command opts in.
        for provenance in ("browser_session", "mcp_client"):
            editing = page_service.create_page_first(
                workspace_slug="default",
                title="Golden",
                body_md=BASE,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )
            saved = page_service.autosave_page(
                page_id=editing.id,
                workspace_id=workspace_id,
                title="Golden",
                body_md=RAW,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance=provenance,
                base_version_id=editing.version_id,
                base_content_hash=editing.content_hash,
                idempotency_key=f"w4-{suffix}-{provenance}",
            )
            _assert_stored(
                session_factory,
                saved.version_id,
                RAW if provenance == "browser_session" else expected,
                f"W4 {suffix} {provenance}",
            )

        # W5: both stale-base alternative paths normalize, including browser
        # autosave, whose ordinary successful path deliberately skips it.
        for writer in ("update", "autosave"):
            base = _published(page_service, f"w5-{suffix}-{writer}")
            _advance(page_service, base)
            stale_args = dict(
                page_id=base.id,
                title="Golden",
                body_md=RAW,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
                base_version_id=base.version_id,
                base_content_hash=base.content_hash,
                on_stale="preserve_as_alternative",
            )
            if writer == "autosave":
                alternative = page_service.autosave_page(
                    **stale_args,
                    workspace_id=workspace_id,
                    idempotency_key=f"w5-{suffix}",
                )
            else:
                alternative = page_service.update_page(**stale_args)
            assert alternative.preserved_as_alternative is True
            _assert_stored(
                session_factory,
                alternative.alternative_draft_id,
                expected,
                f"W5 {suffix} {writer}",
            )

        # W6: editing-session typed patches follow workspace Auto-fix.
        editing = page_service.create_page_first(
            workspace_slug="default",
            title="Golden",
            body_md=BASE,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        )
        patched = page_service.patch_context_edit(
            page_id=editing.id,
            version_id=editing.version_id,
            workspace_id=workspace_id,
            operations=[_replace_base()],
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
            expected_editing_revision=1,
            idempotency_key=f"w6-{suffix}",
        )
        _assert_stored(session_factory, patched.version_id, expected, f"W6 {suffix}")

        # W7: Context draft creation.
        with session_factory.begin() as session:
            draft = service.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=WriteDraftRequest(
                    title=f"W7 {suffix}",
                    page_type="overview",
                    body_markdown=RAW,
                    reason="golden body writer matrix",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
        _assert_stored(
            session_factory, draft.draft_version_id, expected, f"W7 {suffix}"
        )

        # W8: patch_draft's current-base path, then W9: the stale overlapping
        # block path must land a branch after preparing the caller's body.
        base = _published(page_service, f"w8-{suffix}")
        with session_factory.begin() as session:
            patched = service.patch_draft(
                session=session,
                workspace_id=workspace_id,
                request=PatchDraftRequest(
                    page_id=base.id,
                    base=str(base.version_id),
                    operations=[_replace_base()],
                    reason="golden current-base patch",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        _assert_stored(
            session_factory, patched.draft_version_id, expected, f"W8 {suffix}"
        )
        base = _published(page_service, f"w9-{suffix}")
        structure = dispatch(
            "read_structure_map",
            {"workspace_id": str(workspace_id), "target": {"page_id": str(base.id)}},
            actor=reader,
            page_service=page_service,
        )
        assert structure.envelope.result_status == "succeeded"
        paragraph = next(block for block in structure.blocks if block.kind == "p")
        _advance(page_service, base)
        with session_factory.begin() as session:
            diverged = service.patch_draft(
                session=session,
                workspace_id=workspace_id,
                request=PatchDraftRequest(
                    page_id=base.id,
                    base=str(base.version_id),
                    operations=[
                        {
                            "op": "replace_block",
                            "block": paragraph.id,
                            "expected": paragraph.hash,
                            "content": "* item   ",
                        }
                    ],
                    reason="golden diverged patch",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        assert diverged.push_outcome == "diverged"
        _assert_stored(
            session_factory,
            diverged.alternative_draft_id,
            "# Golden\n\n- item\n" if enabled else "# Golden\n\n* item   \n",
            f"W9 {suffix}",
        )

        # W10: full-body Context update through _persist_context_update.
        base = _published(page_service, f"w10-{suffix}")
        with session_factory.begin() as session:
            updated = service.update_draft(
                session=session,
                workspace_id=workspace_id,
                request=UpdateDraftRequest(
                    page_id=base.id,
                    base_version_id=base.version_id,
                    base_content_hash=base.content_hash,
                    update_mode="full_rewrite",
                    body_markdown=RAW,
                    reason="golden whole-body update",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        _assert_stored(
            session_factory, updated.draft_version_id, expected, f"W10 {suffix}"
        )

        # A valid rewrite can repair historical unsafe bytes. Preparing the
        # old base for an auto-approval comparison must not reject that write.
        legacy = _published(page_service, f"w10-legacy-{suffix}")
        legacy_body = "![inline](data:image/png;base64,aGVsbG8=)"
        legacy_hash = content_hash(legacy_body)
        with session_factory.begin() as session:
            stored = session.get(ContextPageVersion, legacy.version_id)
            stored.body_md = legacy_body
            stored.content_hash = legacy_hash
        with session_factory.begin() as session:
            repaired = service.update_draft(
                session=session,
                workspace_id=workspace_id,
                request=UpdateDraftRequest(
                    page_id=legacy.id,
                    base_version_id=legacy.version_id,
                    base_content_hash=legacy_hash,
                    update_mode="full_rewrite",
                    body_markdown=RAW,
                    reason="repair historical unsafe content",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        _assert_stored(
            session_factory,
            repaired.draft_version_id,
            expected,
            f"W10 legacy repair {suffix}",
        )

        # W11: ingress acceptance follows workspace Auto-fix.
        with session_factory.begin() as session:
            parent = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.slug == "context",
                )
            )
            proposal = ContextIngressProposal(
                workspace_id=workspace_id,
                source_kind="library_item",
                proposed_context_page_type="overview",
                proposed_parent_tree_node_id=parent.id,
                proposed_title=f"W11 {suffix}",
                proposed_save_mode="create_page",
                placement_confidence="high",
            )
            session.add(proposal)
            session.flush()
            accepted = ContextIngressService().accept_proposal(
                session=session,
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                request=ContextIngressAcceptRequest(
                    final_save_mode="create_page",
                    final_parent_node_id=parent.id,
                    final_title=f"W11 {suffix}",
                    body_markdown=RAW,
                    review_required=True,
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        _assert_stored(
            session_factory,
            accepted.context_page_version_id,
            expected,
            f"W11 {suffix}",
        )

        # W12: OKF frontmatter consumption reaches write_page's body policy.
        slug = f"w12-{suffix}"
        assert import_okf_bundle(
            page_service,
            workspace_slug="default",
            bundle={
                f"context/{slug}.md": "---\ntitle: Golden\ntype: concept\n---\n" + RAW
            },
            actor_id=ACTOR_ID,
            actor_provenance="browser_session",
        ) == [slug]
        imported = page_service.read_page(workspace_slug="default", slug=slug)
        _assert_stored(
            session_factory,
            imported.version_id,
            f'---\ntitle: "Golden"\nslug: {slug}\ntype: concept\n'
            "tags: []\naliases: []\n---\n" + expected,
            f"W12 {suffix}",
        )

        # Copy-only lifecycle transitions must retain historical authored
        # bytes even after the workspace enables Auto-fix.
        with session_factory.begin() as session:
            session.get(Workspace, workspace_id).auto_fix_enabled = False
        to_edit = _published(page_service, f"copy-edit-{suffix}", RAW)
        to_restore = _published(page_service, f"copy-restore-{suffix}", RAW)
        with session_factory.begin() as session:
            session.get(Workspace, workspace_id).auto_fix_enabled = enabled
        copied = page_service.begin_context_edit(
            page_id=to_edit.id,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
            idempotency_key=f"copy-edit-{suffix}",
        )
        _assert_stored(session_factory, copied.version_id, RAW, f"begin copy {suffix}")
        _advance(page_service, to_restore)
        restored = page_service.restore_version(
            page_id=to_restore.id,
            version_id=to_restore.version_id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_provenance="mcp_client",
        )
        _assert_stored(
            session_factory, restored.version_id, RAW, f"restore copy {suffix}"
        )
