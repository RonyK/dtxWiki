import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from backend.models.coverage import KnowledgeCoverageGap
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding, LintRun
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.lint import LintRunCreateRequest
from backend.services.client_round_state import (
    MAINTENANCE_STATE_KIND,
    ClientRoundStateCodec,
)
from backend.services.coverage_service import record_coverage_gap
from backend.services.delegated_tasks import create_task
from backend.services.lint_service import LintService
from backend.services.maintenance_rounds import (
    MAX_BATCH,
    TASK_BUILDERS,
    TASK_STAGERS,
    TASK_TOKEN_BUDGET,
    TASK_VALIDATORS,
    DelegatedTaskCard,
    lease_round,
    submit_answers,
)

NOW = datetime(2026, 7, 12, 1, 0, tzinfo=UTC)


def _codec() -> ClientRoundStateCodec:
    return ClientRoundStateCodec(
        secret="maintenance-test-secret",
        kind=MAINTENANCE_STATE_KIND,
        now=lambda: NOW,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _seed_gap(session, workspace_id, index: int) -> ClientDelegatedTask:
    gap = record_coverage_gap(
        session,
        workspace_id=workspace_id,
        query=f"missing concept {index}",
        surface="search_rest",
        tried_lanes=["entity", "keyword"],
        nearest_candidates=[],
    )
    assert gap is not None
    task = session.scalar(
        select(ClientDelegatedTask).where(ClientDelegatedTask.target_id == gap.id)
    )
    assert task is not None
    return task


def _seed_folder_task(session, workspace_id, *, long_children=False):
    folder = WorkspaceTreeNode(
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        parent_id=None,
        sort_key="maintenance-folder",
        title="Maintenance Folder",
        slug="maintenance-folder",
        target_kind=None,
        target_id=None,
        visibility_status="visible",
    )
    session.add(folder)
    session.flush()
    if long_children:
        for index in range(20):
            session.add(
                WorkspaceTreeNode(
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=folder.id,
                    sort_key=f"{index:02d}",
                    title=f"Child {index} " + ("가나다라" * 300),
                    slug=f"child-{index}",
                    target_kind=None,
                    target_id=None,
                    visibility_status="visible",
                )
            )
    task = create_task(
        session,
        workspace_id=workspace_id,
        task_type="folder_nav_summary",
        target_kind="workspace_tree_node",
        target_id=folder.id,
        source="lint",
        fingerprint="missing_nav_summary",
        priority=50,
    )
    assert task is not None
    return task


def test_lease_round_skips_restricted_import_task_for_unprivileged_actor(
    session_factory,
):
    # H-04: an import_batch_classify card embeds the target Library item's
    # extracted body. An MCP actor that cannot read a restricted item must not
    # lease that task — it stays PENDING (not expired) for an owner.
    from backend.models.library import LibraryItem
    from backend.policy.auth import ActorContext
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-import.pdf",
            title="Restricted Import",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=item.id,
            source="import",
            fingerprint="h04:restricted-import",
            priority=10,
        )
        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="maintenance-editor",
            scopes=("mcp:write",),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )
        owner = ActorContext(
            actor_kind="system",
            actor_id="maintenance-owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
            token_kind="system",
        )

        assert _task_target_visible(session, task, editor) is False
        assert _task_target_visible(session, task, owner) is True

        round_ = lease_round(
            session, workspace_id=wsid, codec=_codec(), actor=editor, now=NOW
        )
        session.refresh(task)
        assert task.id not in {card.task_id for card in round_.tasks}
        # Skipped, NOT expired — still available for a higher-privileged actor.
        assert task.status == "pending"


def test_task_target_visible_gates_folder_task_with_restricted_child(session_factory):
    # H-04 (Codex round-2): a folder_nav_summary card lists the folder's children
    # titles. A restricted-Library child is hidden from an editor+mcp:write
    # maintenance actor (which cannot read restricted Library), so the folder
    # task must be skipped for that actor and visible to an owner.
    from backend.models.library import LibraryItem
    from backend.policy.auth import ActorContext
    from backend.services.delegated_tasks import create_task
    from backend.services.maintenance_rounds import _task_target_visible
    from backend.services.tree_service import (
        create_tree_node,
        ensure_primary_tree_placement,
    )

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        context_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == wsid,
                WorkspaceTreeNode.section == "knowledge",
            )
        ) or session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == wsid,
                WorkspaceTreeNode.parent_id.is_(None),
                WorkspaceTreeNode.node_kind == "folder",
            )
        )
        folder = create_tree_node(
            session,
            workspace_id=wsid,
            section=context_root.section,
            node_kind="folder",
            parent_id=context_root.id,
            title="Summary Folder",
            slug="summary-folder",
            actor_kind="human",
            actor_id="owner",
        )
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-child.pdf",
            title="Restricted Child",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        placed = ensure_primary_tree_placement(
            session=session,
            workspace_id=wsid,
            target_kind="library_item",
            target_id=item.id,
            title=item.title,
            section=context_root.section,
            parent_id=folder.id,
            default_parent_slug=folder.slug,
            actor_kind="human",
            actor_id="owner",
        )
        placed.parent_id = folder.id
        session.flush()
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="folder_nav_summary",
            target_kind="workspace_tree_node",
            target_id=folder.id,
            source="lint",
            fingerprint="h04:folder-restricted-child",
            priority=10,
        )
        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="maintenance-editor",
            scopes=("mcp:write",),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )
        owner = ActorContext(
            actor_kind="system",
            actor_id="maintenance-owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
            token_kind="system",
        )

        assert _task_target_visible(session, task, editor) is False
        assert _task_target_visible(session, task, owner) is True


def test_task_target_visible_gates_coverage_gap_with_restricted_candidate(
    session_factory,
):
    # H-04/coverage (Codex round-3): a coverage_gap_review card copies the gap's
    # stored nearest_candidates. A restricted-Library candidate must make the
    # task invisible to an editor+mcp:write actor that cannot read it.
    from backend.models.coverage import KnowledgeCoverageGap
    from backend.models.library import LibraryItem
    from backend.policy.auth import ActorContext
    from backend.services.delegated_tasks import create_task
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-candidate.pdf",
            title="Restricted Candidate",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        gap = KnowledgeCoverageGap(
            workspace_id=wsid,
            query_norm="deployment secrets",
            surface="ask",
            tried_lanes=["keyword"],
            nearest_candidates=[
                {
                    "id": str(item.id),
                    "kind": "library_item",
                    "title": "Restricted Candidate",
                    "concept_path": "",
                    "match_reason": "trigram",
                }
            ],
            status="open",
        )
        session.add(gap)
        session.flush()
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="coverage_gap_review",
            target_kind="coverage_gap",
            target_id=gap.id,
            source="coverage_gap",
            fingerprint="r3:coverage-restricted",
            priority=10,
        )
        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="maintenance-editor",
            scopes=("mcp:write",),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )
        owner = ActorContext(
            actor_kind="system",
            actor_id="maintenance-owner",
            scopes=("*",),
            workspace_id=wsid,
            workspace_role="owner",
            role="owner",
            token_kind="system",
        )

        assert _task_target_visible(session, task, editor) is False
        assert _task_target_visible(session, task, owner) is True


def test_coverage_candidate_dangling_agent_fails_closed(session_factory):
    # Codex round-4: a dangling agent_page/context_page coverage candidate must
    # fail CLOSED (consistent with library candidates) — the stored candidate
    # still carries the deleted target's snapshot title.
    from uuid import uuid4

    from backend.models.coverage import KnowledgeCoverageGap
    from backend.policy.auth import ActorContext
    from backend.services.delegated_tasks import create_task
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        gap = KnowledgeCoverageGap(
            workspace_id=wsid,
            query_norm="deleted secret persona",
            surface="ask",
            tried_lanes=["keyword"],
            nearest_candidates=[
                {
                    "id": str(uuid4()),  # no such agent page (deleted)
                    "kind": "agent_page",
                    "title": "Deleted Secret Persona",
                    "concept_path": "",
                    "match_reason": "trigram",
                }
            ],
            status="open",
        )
        session.add(gap)
        session.flush()
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="coverage_gap_review",
            target_kind="coverage_gap",
            target_id=gap.id,
            source="coverage_gap",
            fingerprint="r4:coverage-dangling",
            priority=10,
        )
        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="maintenance-editor",
            scopes=("mcp:write",),
            workspace_id=wsid,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )
        # A deleted candidate cannot be re-authorized: fail closed for everyone.
        assert _task_target_visible(session, task, editor) is False


def _maintenance_editor(wsid):
    from backend.policy.auth import ActorContext

    return ActorContext(
        actor_kind="mcp_client",
        actor_id="maintenance-editor",
        scopes=("mcp:write",),
        workspace_id=wsid,
        workspace_role="editor",
        role="editor",
        token_kind="mcp_client",
    )


def _add_published_page(session, wsid, *, slug, title, body, lifecycle="published"):
    from backend.models.context import ContextPage, ContextPageVersion

    page = ContextPage(
        workspace_id=wsid,
        slug=slug,
        title=title,
        page_type="concept",
        lifecycle_status=lifecycle,
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=wsid,
        page_id=page.id,
        body_md=body,
        content_hash=f"hash-{slug}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    session.flush()
    return page, version


def test_task_target_visible_gates_archived_import_item(session_factory):
    # Codex round-7: import_batch_classify checked only restricted trust. An
    # archived/hidden but non-restricted Library item still leaks its extracted
    # body, so the leasing actor must also be blocked on lifecycle.
    from backend.models.library import LibraryItem
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        archived = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/archived-import.pdf",
            title="Archived Import",
            trust_level="trusted",
            lifecycle_status="archived",
        )
        live = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/live-import.pdf",
            title="Live Import",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add_all([archived, live])
        session.flush()
        editor = _maintenance_editor(wsid)

        archived_task = create_task(
            session,
            workspace_id=wsid,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=archived.id,
            source="import",
            fingerprint="r7:archived-import",
            priority=10,
        )
        live_task = create_task(
            session,
            workspace_id=wsid,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=live.id,
            source="import",
            fingerprint="r7:live-import",
            priority=10,
        )

        # Archived body must not surface even to a trust-cleared actor.
        assert _task_target_visible(session, archived_task, editor) is False
        assert _task_target_visible(session, live_task, editor) is True


def test_task_target_visible_gates_archived_context_page_task(session_factory):
    # Codex round-7: page_meta_refresh embeds the target page's title + body
    # excerpt. An archived page's content must not surface into the card.
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        archived, archived_v = _add_published_page(
            session,
            wsid,
            slug="archived-meta",
            title="Archived Secret Page",
            body="secret archived body",
            lifecycle="archived",
        )
        live, live_v = _add_published_page(
            session,
            wsid,
            slug="live-meta",
            title="Live Page",
            body="live body",
        )
        editor = _maintenance_editor(wsid)

        archived_task = create_task(
            session,
            workspace_id=wsid,
            task_type="page_meta_refresh",
            target_kind="context_page",
            target_id=archived.id,
            source="lint",
            fingerprint="r7:archived-meta",
            payload={"published_version_id": str(archived_v.id)},
            priority=10,
        )
        live_task = create_task(
            session,
            workspace_id=wsid,
            task_type="page_meta_refresh",
            target_kind="context_page",
            target_id=live.id,
            source="lint",
            fingerprint="r7:live-meta",
            payload={"published_version_id": str(live_v.id)},
            priority=10,
        )

        assert _task_target_visible(session, archived_task, editor) is False
        assert _task_target_visible(session, live_task, editor) is True


def test_task_target_visible_gates_near_duplicate_archived_candidate(session_factory):
    # Codex round-7: near_duplicate_verdict embeds BOTH the target page and the
    # payload candidate page. An archived candidate must block the task even when
    # the target page is visible.
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        page, _ = _add_published_page(
            session, wsid, slug="dup-primary", title="Primary", body="primary body"
        )
        archived_candidate, _ = _add_published_page(
            session,
            wsid,
            slug="dup-archived",
            title="Archived Candidate",
            body="archived candidate body",
            lifecycle="archived",
        )
        editor = _maintenance_editor(wsid)

        task = create_task(
            session,
            workspace_id=wsid,
            task_type="near_duplicate_verdict",
            target_kind="context_page",
            target_id=page.id,
            source="lint",
            fingerprint="r7:near-dup-archived",
            payload={"candidate_page_id": str(archived_candidate.id)},
            priority=10,
        )

        assert _task_target_visible(session, task, editor) is False


def test_task_target_visible_coverage_scalar_fails_closed(session_factory):
    # Codex round-7: a corrupted non-list nearest_candidates scalar must fail
    # closed (skip the task), not pass the empty-iterable all() and then crash
    # the card builder slicing an int.
    from backend.services.maintenance_rounds import (
        TASK_BUILDERS,
        _task_target_visible,
    )

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        gap = KnowledgeCoverageGap(
            workspace_id=wsid,
            query_norm="corrupt scalar gap",
            surface="ask",
            tried_lanes=["keyword"],
            nearest_candidates=42,
            status="open",
        )
        session.add(gap)
        session.flush()
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="coverage_gap_review",
            target_kind="coverage_gap",
            target_id=gap.id,
            source="coverage_gap",
            fingerprint="r7:coverage-scalar",
            priority=10,
        )
        editor = _maintenance_editor(wsid)

        assert _task_target_visible(session, task, editor) is False
        # The builder must also tolerate the scalar without crashing.
        card = TASK_BUILDERS["coverage_gap_review"](session, task)
        assert card is not None


def test_build_page_meta_refresh_expires_superseded_version(session_factory):
    # Codex round-8: page_meta fingerprint = published version id, so a republish
    # leaves the old version's task pending. The builder must not emit the
    # superseded version's body — return None so lease_round expires it.
    from backend.models.context import ContextPageVersion
    from backend.services.maintenance_rounds import TASK_BUILDERS

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        page, v1 = _add_published_page(
            session, wsid, slug="meta-superseded", title="Meta", body="v1 secret"
        )
        stale_task = create_task(
            session,
            workspace_id=wsid,
            task_type="page_meta_refresh",
            target_kind="context_page",
            target_id=page.id,
            source="lint",
            fingerprint=str(v1.id),
            payload={"published_version_id": str(v1.id)},
            priority=10,
        )
        # Republish: V2 becomes the head; the V1 task stays pending.
        v1.version_status = "superseded_snapshot"
        session.flush()
        v2 = ContextPageVersion(
            workspace_id=wsid,
            page_id=page.id,
            version_no=2,
            body_md="v2 sanitized",
            content_hash="hash-meta-v2",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(v2)
        session.flush()
        page.current_published_version_id = v2.id
        session.flush()

        # Stale-version task must not build a card.
        assert TASK_BUILDERS["page_meta_refresh"](session, stale_task) is None
        # A task for the current head builds normally.
        head_task = create_task(
            session,
            workspace_id=wsid,
            task_type="page_meta_refresh",
            target_kind="context_page",
            target_id=page.id,
            source="lint",
            fingerprint=str(v2.id),
            payload={"published_version_id": str(v2.id)},
            priority=10,
        )
        assert TASK_BUILDERS["page_meta_refresh"](session, head_task) is not None

        # lease_round expires the stale task (builder None) and leases the head.
        round_ = lease_round(
            session,
            workspace_id=wsid,
            codec=_codec(),
            actor=_maintenance_editor(wsid),
            now=NOW,
        )
        session.refresh(stale_task)
        assert stale_task.status == "expired"
        assert head_task.id in {card.task_id for card in round_.tasks}


def test_build_stale_contradiction_expires_archived_source(session_factory):
    # Codex round-8: the card forwards the whole payload incl. the source page's
    # identifiers/reason. If the source was archived after staging, the check is
    # obsolete and its references must not be re-emitted.
    from backend.services.maintenance_rounds import TASK_BUILDERS

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        dependent, _ = _add_published_page(
            session, wsid, slug="stale-dep", title="Dependent", body="dep body"
        )
        archived_source, _ = _add_published_page(
            session,
            wsid,
            slug="stale-src-archived",
            title="Archived Source",
            body="source body",
            lifecycle="archived",
        )
        live_source, _ = _add_published_page(
            session, wsid, slug="stale-src-live", title="Live Source", body="src body"
        )

        archived_task = create_task(
            session,
            workspace_id=wsid,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=dependent.id,
            source="publish",
            fingerprint="r8:stale-archived",
            payload={
                "published_page_id": str(archived_source.id),
                "dependent_page_id": str(dependent.id),
            },
            priority=10,
        )
        live_task = create_task(
            session,
            workspace_id=wsid,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=dependent.id,
            source="publish",
            fingerprint="r8:stale-live",
            payload={
                "published_page_id": str(live_source.id),
                "dependent_page_id": str(dependent.id),
            },
            priority=10,
        )

        build = TASK_BUILDERS["stale_contradiction_check"]
        assert build(session, archived_task) is None
        assert build(session, live_task) is not None


def test_lease_round_scans_past_hidden_high_priority_tasks(session_factory):
    # Codex round-8/9: hidden high-priority tasks must not starve visible tasks
    # behind them. lease_round walks the pending queue past skipped tasks (up to
    # MAX_LEASE_SCAN) rather than capping the SQL window at the batch budget.
    from backend.models.library import LibraryItem

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        editor = _maintenance_editor(wsid)
        # MAX_BATCH restricted imports the editor cannot see, all higher priority
        # (lower number) than the one visible task.
        for index in range(MAX_BATCH):
            item = LibraryItem(
                workspace_id=wsid,
                connector_type="upload",
                format_hint="pdf",
                uri=f"https://example.com/hidden-{index}.pdf",
                title=f"Hidden {index}",
                trust_level="restricted",
                lifecycle_status="registered",
            )
            session.add(item)
            session.flush()
            create_task(
                session,
                workspace_id=wsid,
                task_type="import_batch_classify",
                target_kind="library_item",
                target_id=item.id,
                source="import",
                fingerprint=f"r8:hidden-{index}",
                priority=1,
            )
        # One visible task, lower priority, queued behind all the hidden ones.
        visible_task = create_task(
            session,
            workspace_id=wsid,
            task_type="golden_query_candidates",
            target_kind="workspace",
            target_id=wsid,
            source="lint",
            fingerprint="r8:visible-golden",
            payload={"existing_golden": [], "page_samples": [], "k": 5},
            priority=50,
        )

        round_ = lease_round(
            session, workspace_id=wsid, codec=_codec(), actor=editor, now=NOW
        )

        # The visible task is leased despite MAX_BATCH hidden tasks ahead of it.
        assert visible_task.id in {card.task_id for card in round_.tasks}


def test_build_import_classify_expires_superseded_version(session_factory):
    # Codex round-9: import fingerprint = version id, so a refresh leaves the old
    # version's task pending. The builder must not emit the superseded version's
    # extracted body — return None so lease_round expires it (Library analog of
    # the page_meta superseded-head leak).
    from backend.models.library import (
        LibraryArtifact,
        LibraryArtifactConversionDetail,
        LibraryAttempt,
        LibraryAttemptConversionDetail,
        LibraryItem,
        LibraryItemVersion,
        LibraryRun,
        LibraryRunConversionDetail,
    )
    from backend.services.maintenance_rounds import TASK_BUILDERS

    def _add_completed_version(session, item, version_no, index_text):
        version = LibraryItemVersion(
            workspace_id=item.workspace_id,
            library_item_id=item.id,
            version_no=version_no,
            content_hash=f"h{version_no}",
            raw_storage_ref=f"inline://raw{version_no}",
            processing_status="completed",
        )
        session.add(version)
        session.flush()
        # session_factory (backend/tests/conftest.py) uses a bare
        # create_engine() with no PRAGMA foreign_keys=ON, so run_id/
        # attempt_id/blob_id need not reference other real rows -- only the
        # NOT NULL/CHECK-satisfying columns and the
        # selected_conversion_attempt_id link (required by
        # get_canonical_markdown_artifact now that A-1 retired the
        # nullable-attempt "belongs by default" shape) matter.
        run = LibraryRun(
            workspace_id=item.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test",
            output_schema_version="1",
            idempotency_key=f"test:{version.id}",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=item.workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id="test.provider",
            provider_version="1",
            provider_spi_version="1",
            config_hash="test",
            input_sha256="0" * 64,
            output_schema_version="1",
            status="completed",
            chain_index=0,
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()
        artifact = LibraryArtifact(
            workspace_id=item.workspace_id,
            library_item_version_id=version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=item.workspace_id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="markdown",
            kind="text",
            media_type="text/plain",
            content_sha256=hashlib.sha256(index_text.encode("utf-8")).hexdigest(),
            byte_size=len(index_text.encode("utf-8")),
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            index_text=index_text,
        )
        session.add(artifact)
        session.flush()
        version.selected_conversion_attempt_id = attempt.id
        session.add(version)
        session.flush()
        return version

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        item = LibraryItem(
            workspace_id=wsid,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/import-superseded.pdf",
            title="Import",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        v1 = _add_completed_version(session, item, 1, "v1 secret body")
        item.current_version_id = v1.id
        session.flush()

        stale_task = create_task(
            session,
            workspace_id=wsid,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=item.id,
            source="import",
            fingerprint=str(v1.id),
            payload={
                "library_item_version_id": str(v1.id),
                "title": "Import",
                "uri": "https://example.com/import-superseded.pdf",
                "format_hint": "pdf",
            },
            priority=10,
        )
        build = TASK_BUILDERS["import_batch_classify"]
        # While V1 is the current version, the task builds normally.
        assert build(session, stale_task) is not None

        # Refresh: V2 becomes the current version; the V1 task stays pending.
        v2 = _add_completed_version(session, item, 2, "v2 sanitized body")
        item.current_version_id = v2.id
        session.flush()

        # The superseded-version task must not emit V1's body.
        assert build(session, stale_task) is None


def test_import_task_builder_treats_foreign_item_like_missing(session_factory):
    from backend.models.library import LibraryItem, LibraryItemVersion

    with session_factory.begin() as session:
        requested_workspace_id = _workspace_id(session)
        foreign_workspace = Workspace(
            slug="maintenance-foreign-import",
            title="Maintenance Foreign Import",
            name="Maintenance Foreign Import",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_item = LibraryItem(
            workspace_id=foreign_workspace.id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/maintenance-foreign-import.pdf",
            title="Foreign import body must stay opaque",
            trust_level="trusted",
            lifecycle_status="registered",
        )
        session.add(foreign_item)
        session.flush()
        foreign_version = LibraryItemVersion(
            workspace_id=foreign_workspace.id,
            library_item_id=foreign_item.id,
            version_no=1,
            content_hash="foreign-maintenance-hash",
            raw_storage_ref="inline://foreign-maintenance",
            processing_status="completed",
        )
        session.add(foreign_version)
        session.flush()

        foreign_task = create_task(
            session,
            workspace_id=requested_workspace_id,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=foreign_item.id,
            source="import",
            fingerprint="foreign-maintenance-item",
            payload={"library_item_version_id": str(foreign_version.id)},
            priority=10,
        )
        missing_task = create_task(
            session,
            workspace_id=requested_workspace_id,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=uuid4(),
            source="import",
            fingerprint="missing-maintenance-item",
            payload={"library_item_version_id": str(uuid4())},
            priority=10,
        )

        build = TASK_BUILDERS["import_batch_classify"]
        assert build(session, foreign_task) is None
        assert build(session, missing_task) is None


def test_build_golden_query_drops_archived_and_stale_samples(session_factory):
    # Codex round-9: golden page_samples carry a title/path with no re-check. The
    # builder must re-resolve each by page_id, drop archived/missing/legacy
    # samples, and refresh the title/path from the current row.
    from backend.services.maintenance_rounds import TASK_BUILDERS

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        live, _ = _add_published_page(
            session, wsid, slug="golden-live", title="Live Title", body="b"
        )
        archived, _ = _add_published_page(
            session,
            wsid,
            slug="golden-archived",
            title="Archived Title",
            body="b",
            lifecycle="archived",
        )
        renamed, _ = _add_published_page(
            session, wsid, slug="golden-renamed", title="Current Title", body="b"
        )

        task = create_task(
            session,
            workspace_id=wsid,
            task_type="golden_query_candidates",
            target_kind="workspace",
            target_id=wsid,
            source="lint",
            fingerprint="r9:golden-samples",
            payload={
                "existing_golden": [],
                "k": 5,
                "page_samples": [
                    {"page_id": str(live.id), "title": "Live Title"},
                    {"page_id": str(archived.id), "title": "Archived Title"},
                    {"page_id": str(renamed.id), "title": "Stale Old Title"},
                    {"title": "Legacy No Id"},
                ],
            },
            priority=10,
        )

        card = TASK_BUILDERS["golden_query_candidates"](session, task)
        assert card is not None
        body = card.untrusted_context
        # Live sample kept; archived/legacy dropped; renamed refreshed to current.
        assert "Live Title" in body
        assert "Archived Title" not in body
        assert "Legacy No Id" not in body
        assert "Stale Old Title" not in body
        assert "Current Title" in body


def test_task_target_visible_gates_stale_contradiction_hidden_source(session_factory):
    # Codex round-10: the stale-contradiction card emits the source page's
    # ids/reason. Gate the source page (payload published_page_id) per-actor, not
    # just the dependent — an archived/hidden source must skip the task even when
    # the dependent is visible.
    from backend.services.maintenance_rounds import _task_target_visible

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        dependent, _ = _add_published_page(
            session, wsid, slug="sc-dep", title="Dependent", body="b"
        )
        archived_source, _ = _add_published_page(
            session,
            wsid,
            slug="sc-src-archived",
            title="Archived Source",
            body="b",
            lifecycle="archived",
        )
        live_source, _ = _add_published_page(
            session, wsid, slug="sc-src-live", title="Live Source", body="b"
        )
        editor = _maintenance_editor(wsid)

        hidden_task = create_task(
            session,
            workspace_id=wsid,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=dependent.id,
            source="publish",
            fingerprint="r10:hidden-src",
            payload={"published_page_id": str(archived_source.id)},
            priority=10,
        )
        live_task = create_task(
            session,
            workspace_id=wsid,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=dependent.id,
            source="publish",
            fingerprint="r10:live-src",
            payload={"published_page_id": str(live_source.id)},
            priority=10,
        )

        assert _task_target_visible(session, hidden_task, editor) is False
        assert _task_target_visible(session, live_task, editor) is True


def test_build_golden_query_drops_existing_golden(session_factory):
    # Codex round-10/11: existing_golden's expected paths AND free-text queries
    # can name pages/entities the leasing client cannot see; neither can be
    # re-authorized after the fact, so the card must drop existing_golden wholly.
    from backend.services.maintenance_rounds import TASK_BUILDERS

    with session_factory.begin() as session:
        wsid = _workspace_id(session)
        task = create_task(
            session,
            workspace_id=wsid,
            task_type="golden_query_candidates",
            target_kind="workspace",
            target_id=wsid,
            source="lint",
            fingerprint="r10:golden-existing",
            payload={
                "existing_golden": [
                    {
                        "query": "refund policy",
                        "expected": ["/context/secret-archived.md"],
                        "lane": "entity",
                    }
                ],
                "page_samples": [],
                "k": 5,
            },
            priority=10,
        )

        card = TASK_BUILDERS["golden_query_candidates"](session, task)
        assert card is not None
        body = card.untrusted_context
        # existing_golden dropped entirely: neither query text nor path leaks.
        assert "refund policy" not in body
        assert "/context/secret-archived.md" not in body


def test_lease_round_orders_tasks_and_caps_at_eight(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        tasks = [_seed_gap(session, workspace_id, index) for index in range(10)]
        for index, task in enumerate(tasks):
            task.priority = 100 - index
            task.created_at = NOW + timedelta(seconds=index)

        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            budget=20,
            now=NOW,
        )

        assert len(round_.tasks) == MAX_BATCH
        assert [card.task_id for card in round_.tasks] == [
            task.id for task in reversed(tasks[2:])
        ]
        assert round_.remaining_pending == 2


def test_lease_round_reclaims_expired_and_ignores_unknown_pending_count(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        registered = _seed_gap(session, workspace_id, 1)
        registered.status = "leased"
        registered.lease_expires_at = NOW - timedelta(seconds=1)
        unregistered = create_task(
            session,
            workspace_id=workspace_id,
            task_type="future_unregistered_task",
            target_kind="context_page",
            target_id=uuid4(),
            source="publish",
            fingerprint="v1",
        )
        assert unregistered is not None

        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            now=NOW,
        )

        assert [card.task_id for card in round_.tasks] == [registered.id]
        assert unregistered.status == "pending"
        assert round_.remaining_pending == 0


def test_lease_round_expires_task_whose_target_is_already_resolved(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_gap(session, workspace_id, 2)
        gap = session.get(KnowledgeCoverageGap, task.target_id)
        gap.status = "resolved"

        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            now=NOW,
        )

        assert round_.tasks == ()
        assert task.status == "expired"
        assert round_.remaining_pending == 0


def test_lease_round_excludes_guarded_update_contention_loser(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="race_test",
            target_kind="context_page",
            target_id=uuid4(),
            source="lint",
            fingerprint="race",
        )
        assert task is not None

        def racing_builder(session, task):
            task.status = "leased"
            session.flush()
            return DelegatedTaskCard(
                task_id=task.id,
                task_type=task.task_type,
                target_kind=task.target_kind,
                target_id=task.target_id,
                instruction="race",
                untrusted_context="race",
                response_schema={},
                estimated_tokens=1,
                truncated=False,
            )

        monkeypatch.setitem(TASK_BUILDERS, "race_test", racing_builder)
        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            now=NOW,
        )

        assert round_.tasks == ()


def test_invalid_answer_returns_task_to_pending_without_finding(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): {"nav_summary": "x" * 161}},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )

        assert outcome.staged == ()
        assert outcome.rejected == ((task.id, "invalid_answer"),)
        assert task.status == "pending"
        assert session.scalar(select(func.count(LintFinding.id))) == 0


def test_generated_answer_string_limits_match_wire_schema_contract():
    page_meta = TASK_VALIDATORS["page_meta_refresh"]
    overview = TASK_VALIDATORS["folder_overview_draft"]
    golden = TASK_VALIDATORS["golden_query_candidates"]

    assert (
        page_meta(
            {
                "aliases": ["a" * 80],
                "nav_description": "Valid description.",
            }
        )
        is not None
    )
    assert (
        page_meta(
            {
                "aliases": ["a" * 81],
                "nav_description": "Valid description.",
            }
        )
        is None
    )
    assert (
        overview(
            {
                "title": "Overview",
                "outline": ["a" * 201, "Second", "Third"],
                "body_md": "# Overview",
            }
        )
        is None
    )
    assert (
        golden(
            {
                "queries": [
                    {
                        "query": "q" * 2049,
                        "expected_concept_path": "/context/page.md",
                    }
                ]
            }
        )
        is None
    )
    assert (
        golden(
            {
                "queries": [
                    {
                        "query": "valid query",
                        "expected_concept_path": "/" + "p" * 512,
                    }
                ]
            }
        )
        is None
    )


def test_valid_answer_stages_finding_and_replay_is_rejected(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        answer = {
            "nav_summary": "Deployment implementation; policy belongs elsewhere.",
            "negative_cues": ["Billing policy belongs in /policy."],
        }
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): answer},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )
        replay = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): answer},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )
        finding = session.scalar(select(LintFinding))

        assert outcome.staged == (task.id,)
        assert task.status == "staged"
        assert task.result_ref == f"lint_finding:{finding.id}"
        assert replay.rejected == ((task.id, "task_not_leased"),)
        action = json.loads(finding.suggested_action)
        assert action["source"] == "client_llm"
        assert action["task_id"] == str(task.id)
        assert finding.fingerprint == f"cdt:{task.id}"


def test_answer_for_task_outside_signed_batch_is_rejected(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        signed_task = _seed_gap(session, workspace_id, 21)
        outside_task = _seed_gap(session, workspace_id, 22)
        signed_task.priority = 1
        outside_task.priority = 2
        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            budget=1,
            now=NOW,
        )
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(signed_task.id): {
                    "verdict": "dismiss",
                    "rationale": "Out of scope.",
                    "proposal": None,
                },
                str(outside_task.id): {
                    "verdict": "dismiss",
                    "rationale": "Forged batch member.",
                    "proposal": None,
                },
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )

        assert outcome.staged == (signed_task.id,)
        assert (outside_task.id, "not_in_batch") in outcome.rejected
        assert outside_task.status == "pending"


def test_stager_exception_never_leaves_task_staged(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        sibling = _seed_gap(session, workspace_id, 77)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        token = round_.resume_token
        task_id = task.id

    def fail_stager(*_args, **_kwargs):
        raise RuntimeError("staging failed")

    monkeypatch.setitem(TASK_STAGERS, "folder_nav_summary", fail_stager)
    with session_factory.begin() as session:
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=token,
            answers={
                str(task_id): {"nav_summary": "Valid summary"},
                str(sibling.id): {
                    "verdict": "dismiss",
                    "rationale": "Out of scope.",
                    "proposal": None,
                },
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )
        assert outcome.staged == (sibling.id,)
        assert outcome.rejected == ((task_id, "staging_failed"),)
    with session_factory() as session:
        task = session.get(ClientDelegatedTask, task_id)
        sibling = session.get(ClientDelegatedTask, sibling.id)
        assert task.status == "pending"
        assert sibling.status == "staged"
        assert session.scalar(select(func.count(LintFinding.id))) == 1


def test_stager_failure_without_sibling_does_not_leave_empty_lint_run(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )

    def fail_stager(*_args, **_kwargs):
        raise RuntimeError("staging failed")

    monkeypatch.setitem(TASK_STAGERS, "folder_nav_summary", fail_stager)
    with session_factory.begin() as session:
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): {"nav_summary": "Valid summary"}},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )

        assert outcome.rejected == ((task.id, "staging_failed"),)
        assert session.scalar(select(func.count(LintRun.id))) == 0


def test_expired_submission_returns_task_to_pending_immediately(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): {"nav_summary": "Valid summary"}},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW + timedelta(seconds=901),
        )

        assert outcome.staged == ()
        assert outcome.rejected == ((task.id, "lease_expired"),)
        assert task.status == "pending"
        assert task.lease_expires_at is None


def test_dismiss_coverage_gap_stages_verdict_and_resolves_gap(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_gap(session, workspace_id, 99)
        gap_id = task.target_id
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(task.id): {
                    "verdict": "dismiss",
                    "rationale": "Out of scope.",
                    "proposal": None,
                }
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )
        gap = session.get(KnowledgeCoverageGap, gap_id)
        finding = session.scalar(select(LintFinding))

        assert outcome.staged == (task.id,)
        assert gap.status == "dismissed"
        assert gap.resolved_at == NOW
        assert finding.rule_id == "maintenance.coverage_gap_verdict"


def test_maintenance_finding_survives_regular_lint_reconciliation(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        task = _seed_folder_task(session, workspace_id)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={str(task.id): {"nav_summary": "A valid navigation summary."}},
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="client-1",
            now=NOW,
        )
        finding = session.scalar(select(LintFinding))
        LintService().run_lint(
            session=session,
            workspace_id=workspace_id,
            request=LintRunCreateRequest(),
            actor_kind="system",
            actor_id="maintenance-test",
            rules=[],
        )

        assert finding.status == "open"


def test_untrusted_context_is_truncated_to_token_budget(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_folder_task(session, workspace_id, long_children=True)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )

        assert round_.tasks[0].truncated is True
        assert round_.tasks[0].estimated_tokens <= TASK_TOKEN_BUDGET
        assert round_.tasks[0].untrusted_context.endswith(
            "...[truncated to 2k-token task budget]"
        )
