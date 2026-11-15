"""Golden persisted bytes at the body preparation writer boundaries.

Keep this one collected scenario: each writer has a different persistence path,
while Auto-fix on/off are two traversals of the same writer matrix. Expected
bytes are literals captured before BodyPreparer extraction, never calculated
by the normalizer or preparer under test.
"""

from unicodedata import normalize
from uuid import uuid4

from sqlalchemy import func, select

from backend.models.ask import ContextIngressProposal
from backend.models.context import ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.context_edit import ReplaceTextOperation
from backend.schemas.context_ingress import ContextIngressAcceptRequest
from backend.schemas.context_write import WriteDraftRequest
from backend.services.body_preparer import BodyPreparer
from backend.services.context_ingress_service import ContextIngressService
from backend.services.context_write_service import ContextWriteService
from backend.services.okf import import_okf_bundle

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
    """R4 W1-W12 golden, with the intentional R5/R6 policy changes applied.

    W8-W10 (patch_draft and update_draft) left with those writers."""
    prepared_provenances = []

    class RecordingPreparer(BodyPreparer):
        def prepare(self, input, ctx):
            prepared_provenances.append(ctx.actor_provenance)
            return super().prepare(input, ctx)

    service = ContextWriteService(body_preparer=RecordingPreparer())
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
        expected_fixes = [{"module_id": "format", "count": 1}] if enabled else []
        assert first.lint_report["fixes"] == expected_fixes

        # W2: legacy slug writer; W3: explicit page update.
        written = _published(page_service, f"w2-{suffix}", RAW)
        _assert_stored(session_factory, written.version_id, expected, f"W2 {suffix}")
        assert written.lint_report["fixes"] == expected_fixes
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
        assert updated.lint_report["fixes"] == expected_fixes

        # Whitespace-insensitive content hashes intentionally collide for these
        # bodies, but a Markdown hard break is a real authored byte change.
        for writer in ("write", "update"):
            slug = f"hard-break-{suffix}-{writer}"
            authored = "café\nb\n"
            initial = _published(page_service, slug, authored)
            save = (
                page_service.write_page
                if writer == "write"
                else page_service.update_page
            )
            save_args = dict(
                title="Golden",
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )
            if writer == "write":
                save_args.update(workspace_slug="default", slug=slug)
            else:
                save_args.update(page_id=initial.id)
            # One input exposes failures to ignore NFD, CRLF or the final
            # newline. Auto-fix handles CRLF/EOF first; NFD still reaches the
            # authored-body comparison with either policy setting.
            equivalent = normalize("NFD", authored).rstrip("\n").replace("\n", "\r\n")
            unchanged = save(
                **save_args,
                body_md=equivalent,
                base_version_id=initial.version_id,
                base_content_hash=initial.content_hash,
            )
            assert unchanged.version_id == initial.version_id
            with session_factory() as session:
                version_count = session.scalar(
                    select(func.count(ContextPageVersion.id)).where(
                        ContextPageVersion.page_id == initial.id
                    )
                )
                assert version_count == 1
            save_args["body_md"] = "café  \nb\n"
            changed = save(
                **save_args,
                base_version_id=initial.version_id,
                base_content_hash=initial.content_hash,
            )
            repeated = save(
                **save_args,
                base_version_id=changed.version_id,
                base_content_hash=changed.content_hash,
            )
            assert changed.version_id != initial.version_id
            assert changed.content_hash == initial.content_hash
            assert repeated.version_id == changed.version_id
            _assert_stored(
                session_factory,
                changed.version_id,
                "café  \nb\n",
                f"hard break {suffix} {writer}",
            )

        # W4: browser and AI autosaves use the same Workspace Auto-fix policy.
        for provenance in ("browser_session", "mcp_client"):
            editing = page_service.create_page_first(
                workspace_slug="default",
                title="Golden",
                body_md=BASE,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance="browser_session",
            )
            autosave_args = dict(
                page_id=editing.id,
                workspace_id=workspace_id,
                title="Golden",
                body_md=RAW,
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_provenance=provenance,
                base_version_id=editing.version_id,
                expected_editing_revision=editing.editing_revision,
                base_content_hash=editing.content_hash,
                idempotency_key=f"w4-{suffix}-{provenance}",
                with_edit_receipt=True,
            )
            saved = page_service.autosave_page(**autosave_args)
            assert saved.lint_report["fixes"] == expected_fixes
            _assert_stored(
                session_factory,
                saved.version_id,
                expected,
                f"W4 {suffix} {provenance}",
            )
            if provenance == "mcp_client":
                replay = page_service.autosave_page(**autosave_args)
                assert saved._edit_receipt is not None
                assert replay._edit_receipt.model_dump() == (
                    saved._edit_receipt.model_dump()
                )
                assert replay.version_id == saved.version_id
                assert replay.body_md == saved.body_md
                assert replay.editing_revision == saved.editing_revision
                assert replay.lint_report == saved.lint_report

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
        assert patched.lint_report["fixes"] == expected_fixes

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
        assert prepared_provenances[-1] == "internal"

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
                f"/p/{workspace_id}/{uuid4()}": (
                    f"---\ntitle: Golden\nslug: {slug}\ntype: concept\n---\n" + RAW
                )
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
        assert copied.lint_report["fixes"] == []
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
        assert restored.lint_report["fixes"] == []
