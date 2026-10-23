import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select

from backend.mcp.maintenance_wire import MrtrPreviewWireAdapter, StableWireAdapter
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding, LintRun
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.services.client_round_state import (
    MAINTENANCE_STATE_KIND,
    ClientRoundStateCodec,
)
from backend.services.delegated_tasks import create_task
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.maintenance_rounds import (
    TASK_TOKEN_BUDGET,
    UNTRUSTED_CONTEXT_NOTICE,
    lease_round,
    submit_answers,
)
from backend.services.trail_analysis import run_trail_tick

NOW = datetime(2026, 7, 12, 5, 0, tzinfo=UTC)


def _codec() -> ClientRoundStateCodec:
    return ClientRoundStateCodec(
        secret="sprint6-c3-secret",
        kind=MAINTENANCE_STATE_KIND,
        now=lambda: NOW,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _page(session, workspace_id, slug):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {page.title}\n\nPublished body for delegated maintenance.",
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="sprint6-c3",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    return page, version


def _overview_folder(session, workspace_id, slug):
    folder = WorkspaceTreeNode(
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        parent_id=None,
        sort_key=slug,
        title=slug.replace("-", " ").title(),
        slug=slug,
        target_kind=None,
        target_id=None,
        visibility_status="visible",
    )
    session.add(folder)
    session.flush()
    session.add_all(
        WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            parent_id=folder.id,
            sort_key=f"child-{index}",
            title=f"Child {index}",
            slug=f"{slug}-child-{index}",
            target_kind=None,
            target_id=None,
            visibility_status="visible",
        )
        for index in range(5)
    )
    session.flush()
    return folder


def _near_duplicate_finding(session, workspace_id, page, candidate):
    run = LintRun(
        workspace_id=workspace_id,
        lint_scope={"kind": "librarian"},
        trigger_type="worker",
        status="completed",
        finding_count_by_severity={"info": 1},
        started_at=NOW,
        completed_at=NOW,
    )
    session.add(run)
    session.flush()
    finding = LintFinding(
        lint_run_id=run.id,
        workspace_id=workspace_id,
        target_type="context_page",
        target_id=page.id,
        rule_id="kb.semantic_near_duplicate",
        severity="info",
        status="open",
        suggested_action=json.dumps(
            {"candidate_page_id": str(candidate.id), "similarity": 0.97}
        ),
        fingerprint=f"near-duplicate:{page.id}:{candidate.id}",
    )
    session.add(finding)
    session.flush()
    return finding


def test_m3_catalog_stages_findings_and_reviewed_overview_draft(
    session_factory, media_installer_factory
):
    from backend.services.context_write_service import ContextWriteService

    written_bodies = []

    class RecordingWriter(ContextWriteService):
        def write_draft(self, **kwargs):
            written_bodies.append(kwargs["request"].body_markdown)
            return super().write_draft(**kwargs)

    writer = RecordingWriter()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, version = _page(session, workspace_id, "maintenance-source")
        candidate, _ = _page(session, workspace_id, "maintenance-candidate")
        folder = _overview_folder(session, workspace_id, "maintenance-overview")
        conflicting_draft = ContextPage(
            workspace_id=workspace_id,
            slug="maintenance-overview",
            title="Maintenance Overview",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(conflicting_draft)
        session.flush()
        duplicate_finding = _near_duplicate_finding(
            session, workspace_id, page, candidate
        )
        page_meta = create_task(
            session,
            workspace_id=workspace_id,
            task_type="page_meta_refresh",
            target_kind="context_page",
            target_id=page.id,
            source="publish",
            fingerprint=str(version.id),
            payload={"published_version_id": str(version.id)},
            priority=90,
        )
        overview = create_task(
            session,
            workspace_id=workspace_id,
            task_type="folder_overview_draft",
            target_kind="workspace_tree_node",
            target_id=folder.id,
            source="lint",
            fingerprint="missing_overview",
            payload={"child_count": 5, "title": folder.title},
            priority=80,
        )
        duplicate = create_task(
            session,
            workspace_id=workspace_id,
            task_type="near_duplicate_verdict",
            target_kind="context_page",
            target_id=page.id,
            source="lint",
            fingerprint=f"near-duplicate:{page.id}:{candidate.id}",
            payload={
                "candidate_page_id": str(candidate.id),
                "finding_id": str(duplicate_finding.id),
                "similarity": 0.97,
            },
            priority=70,
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/imported-manual",
            title="Imported Manual",
            raw_text="Imported source text for classification.",
            format_hint="pdf",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        imported = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "import_batch_classify"
            )
        )
        assert all(
            task is not None for task in (page_meta, overview, duplicate, imported)
        )

        round_ = lease_round(
            session,
            workspace_id=workspace_id,
            codec=_codec(),
            budget=8,
            now=NOW,
        )
        cards = {card.task_type: card for card in round_.tasks}
        assert set(cards) == {
            "page_meta_refresh",
            "folder_overview_draft",
            "near_duplicate_verdict",
            "import_batch_classify",
        }
        assert all(
            card.untrusted_context.startswith(UNTRUSTED_CONTEXT_NOTICE)
            and card.estimated_tokens <= TASK_TOKEN_BUDGET
            for card in cards.values()
        )
        stable = StableWireAdapter().render_round(round_)
        preview = MrtrPreviewWireAdapter().render_round(round_)
        stable_cards = {
            item["task_type"]: (item["task_id"], item["response_schema"])
            for item in stable["maintenance_tasks"]
        }
        preview_cards = {
            item["taskType"]: (item["id"], item["responseSchema"])
            for item in preview["input_required"]["inputRequests"]
        }
        assert stable_cards == preview_cards
        assert set(stable_cards) == {
            "page_meta_refresh",
            "folder_overview_draft",
            "near_duplicate_verdict",
            "import_batch_classify",
        }
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(page_meta.id): {
                    "aliases": ["Maintenance Source"],
                    "nav_description": "Published maintenance source.",
                    "placement_hint": "keep",
                },
                str(overview.id): {
                    "title": "Maintenance Overview",
                    "outline": ["Purpose", "Sources", "Navigation"],
                    "body_md": (
                        "# Maintenance Overview\n\nA review-only overview draft."
                    ),
                },
                str(duplicate.id): {
                    "verdict": "distinct",
                    "rationale": "The pages serve different workflows.",
                    "negative_cues": ["Candidate content belongs to another workflow."],
                },
                str(imported.id): {
                    "title": "Imported Manual",
                    "page_type": "guide",
                    "placement_hint": "/context/imports/",
                },
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="maintenance-client",
            now=NOW,
            context_write_service=writer,
        )
        assert written_bodies == [
            "# Maintenance Overview\n\nA review-only overview draft."
        ]
        assert set(outcome.staged) == {
            page_meta.id,
            overview.id,
            duplicate.id,
            imported.id,
        }
        findings = session.scalars(
            select(LintFinding).where(
                LintFinding.rule_id.in_(
                    (
                        "maintenance.page_meta_suggestion",
                        "maintenance.import_batch_classification",
                    )
                )
            )
        ).all()
        assert len(findings) == 2
        assert all(
            json.loads(finding.suggested_action)["source"] == "client_llm"
            for finding in findings
        )
        duplicate_action = json.loads(duplicate_finding.suggested_action)
        assert duplicate_action["client_llm_verdict"]["verdict"] == "distinct"
        assert duplicate_action["negative_cues"] == [
            "Candidate content belongs to another workflow."
        ]

        result_parts = overview.result_ref.split(":")
        draft_page = session.get(ContextPage, UUID(result_parts[1]))
        review = session.get(ReviewItem, UUID(result_parts[-1]))
        assert draft_page.id != conflicting_draft.id
        assert conflicting_draft.page_type == "concept"
        assert conflicting_draft.current_published_version_id is None
        assert draft_page.lifecycle_status == "draft"
        assert draft_page.current_published_version_id is None
        assert review.status == "pending"


def test_m3_invalid_answers_return_every_task_to_pending(
    session_factory, media_installer_factory
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, version = _page(session, workspace_id, "invalid-source")
        candidate, _ = _page(session, workspace_id, "invalid-candidate")
        folder = _overview_folder(session, workspace_id, "invalid-overview")
        duplicate_finding = _near_duplicate_finding(
            session, workspace_id, page, candidate
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/invalid-import",
            title="Invalid Imported Manual",
            raw_text="Imported source text for invalid answer validation.",
            format_hint="pdf",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        imported = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "import_batch_classify"
            )
        )
        tasks = [
            create_task(
                session,
                workspace_id=workspace_id,
                task_type="page_meta_refresh",
                target_kind="context_page",
                target_id=page.id,
                source="publish",
                fingerprint=str(version.id),
                payload={"published_version_id": str(version.id)},
            ),
            create_task(
                session,
                workspace_id=workspace_id,
                task_type="folder_overview_draft",
                target_kind="workspace_tree_node",
                target_id=folder.id,
                source="lint",
                fingerprint="missing_overview",
                payload={"child_count": 5, "title": folder.title},
            ),
            create_task(
                session,
                workspace_id=workspace_id,
                task_type="near_duplicate_verdict",
                target_kind="context_page",
                target_id=page.id,
                source="lint",
                fingerprint=f"near-duplicate:{page.id}:{candidate.id}",
                payload={
                    "candidate_page_id": str(candidate.id),
                    "finding_id": str(duplicate_finding.id),
                },
            ),
            imported,
        ]
        assert all(task is not None for task in tasks)
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        answers = {
            "page_meta_refresh": {
                "aliases": ["1", "2", "3", "4", "5", "6"],
                "nav_description": "Too many aliases.",
                "placement_hint": "keep",
            },
            "folder_overview_draft": {
                "title": "Too Short",
                "outline": ["Only", "Two"],
                "body_md": "# Invalid",
            },
            "near_duplicate_verdict": {
                "verdict": "distinct",
                "rationale": "A distinction needs a negative cue.",
                "negative_cues": [],
            },
            "import_batch_classify": {
                "title": "x" * 121,
                "page_type": "guide",
                "placement_hint": "/context/imports/",
            },
        }
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(card.task_id): answers[card.task_type] for card in round_.tasks
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="maintenance-client",
            now=NOW,
        )
        assert outcome.staged == ()
        assert {reason for _, reason in outcome.rejected} == {"invalid_answer"}
        assert all(task.status == "pending" for task in tasks)
        assert session.scalar(select(func.count(LintFinding.id))) == 1
        assert session.scalar(select(func.count(ReviewItem.id))) == 0


def test_m4_answers_escalate_advice_without_human_resolution(session_factory, tmp_path):
    golden = tmp_path / "golden.json"
    golden.write_text('[{"query":"kept"}]', encoding="utf-8")
    before = golden.read_bytes()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, _ = _page(session, workspace_id, "stale-dependent")
        from backend.models.lint import LintRun

        source_run = LintRun(
            workspace_id=workspace_id,
            lint_scope={"kind": "maintenance"},
            trigger_type="publish",
            status="completed",
            finding_count_by_severity={"info": 1},
        )
        session.add(source_run)
        session.flush()
        source_finding = LintFinding(
            lint_run_id=source_run.id,
            workspace_id=workspace_id,
            target_type="context_page",
            target_id=page.id,
            rule_id="kb.dependent_maybe_stale",
            severity="info",
            status="acknowledged",
            suggested_action=json.dumps({"confidence": "unreviewed"}),
            fingerprint="stale-source",
        )
        session.add(source_finding)
        session.flush()
        stale_task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=page.id,
            source="publish",
            fingerprint="stale-source",
            payload={
                "finding_id": str(source_finding.id),
                "published_page_id": str(page.id),
                "diff": "policy changed",
            },
            priority=60,
        )
        golden_task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="golden_query_candidates",
            target_kind="workspace",
            target_id=workspace_id,
            source="lint",
            fingerprint="golden-sample",
            payload={"page_samples": [{"title": page.title}]},
            priority=50,
        )
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(stale_task.id): {
                    "contradicts": "yes",
                    "rationale": "The dependent text states the old policy.",
                },
                str(golden_task.id): {
                    "queries": [
                        {
                            "query": "What changed in the stale policy?",
                            "expected_concept_path": "/context/stale-dependent.md",
                        }
                    ]
                },
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="maintenance-client",
            now=NOW,
        )
        assert set(outcome.staged) == {stale_task.id, golden_task.id}
        assert source_finding.status == "acknowledged"
        assert source_finding.severity == "warning"
        action = json.loads(source_finding.suggested_action)
        assert action["confidence"] == "high"
        assert action["client_llm_verdict"]["source"] == "client_llm"
        golden_finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "maintenance.golden_query_candidates"
            )
        )
        assert (
            json.loads(golden_finding.suggested_action)["queries"][0][
                "expected_concept_path"
            ]
            == "/context/stale-dependent.md"
        )
    assert golden.read_bytes() == before


def test_closed_source_rejects_late_stale_and_duplicate_answers(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page, _ = _page(session, workspace_id, "closed-source")
        candidate, _ = _page(session, workspace_id, "closed-candidate")
        stale_finding = _near_duplicate_finding(session, workspace_id, page, candidate)
        stale_finding.rule_id = "kb.dependent_maybe_stale"
        stale_finding.fingerprint = "closed-stale"
        stale_finding.suggested_action = json.dumps({"confidence": "unreviewed"})
        duplicate_finding = _near_duplicate_finding(
            session, workspace_id, page, candidate
        )
        stale_task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=page.id,
            source="publish",
            fingerprint="closed-stale",
            payload={
                "finding_id": str(stale_finding.id),
                "published_page_id": str(page.id),
                "diff": "changed",
            },
        )
        duplicate_task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="near_duplicate_verdict",
            target_kind="context_page",
            target_id=page.id,
            source="lint",
            fingerprint=duplicate_finding.fingerprint,
            payload={
                "finding_id": str(duplicate_finding.id),
                "candidate_page_id": str(candidate.id),
            },
        )
        round_ = lease_round(
            session, workspace_id=workspace_id, codec=_codec(), now=NOW
        )
        stale_finding.status = "dismissed"
        duplicate_finding.status = "fixed"
        session.flush()
        before_count = session.scalar(select(func.count(LintFinding.id)))

        outcome = submit_answers(
            session,
            workspace_id=workspace_id,
            resume_token=round_.resume_token,
            answers={
                str(stale_task.id): {
                    "contradicts": "yes",
                    "rationale": "Late answer must not win.",
                },
                str(duplicate_task.id): {
                    "verdict": "distinct",
                    "rationale": "Late duplicate answer.",
                    "negative_cues": ["Keep separate."],
                },
            },
            codec=_codec(),
            actor_kind="mcp_client",
            actor_id="maintenance-client",
            now=NOW,
        )

        assert outcome.staged == ()
        assert {reason for _, reason in outcome.rejected} == {"source_inactive"}
        assert stale_task.status == "expired"
        assert duplicate_task.status == "expired"
        assert session.scalar(select(func.count(LintFinding.id))) == before_count
        assert "client_llm_verdict" not in stale_finding.suggested_action
        assert "client_llm_verdict" not in duplicate_finding.suggested_action


def test_import_classification_is_format_scoped_and_staged_deduplicated(
    session_factory,
    media_installer_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        markdown = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/no-q8-markdown",
            title="No Q8 Markdown",
            raw_text="Markdown source.",
            format_hint="markdown",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=markdown.version.id,
        )
        assert (
            session.scalar(
                select(ClientDelegatedTask).where(
                    ClientDelegatedTask.target_id == markdown.library_item.id,
                    ClientDelegatedTask.task_type == "import_batch_classify",
                )
            )
            is None
        )

        pdf = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/one-q8-pdf",
            title="One Q8 PDF",
            raw_text="PDF source.",
            format_hint="pdf",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=pdf.version.id,
        )
        task = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.target_id == pdf.library_item.id,
                ClientDelegatedTask.task_type == "import_batch_classify",
            )
        )
        task.status = "staged"
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=pdf.version.id,
        )
        count = session.scalar(
            select(func.count(ClientDelegatedTask.id)).where(
                ClientDelegatedTask.target_id == pdf.library_item.id,
                ClientDelegatedTask.task_type == "import_batch_classify",
            )
        )

    assert count == 1


def test_folder_overview_task_reconciles_when_overview_appears(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        folder = _overview_folder(session, workspace_id, "reconciled-overview")
        first = run_trail_tick(session, workspace_id, now=NOW)
        task = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "folder_overview_draft",
                ClientDelegatedTask.target_id == folder.id,
            )
        )
        assert first["folder_overview_tasks"] == 1
        assert task.fingerprint == "missing_overview"
        assert task.payload["child_count"] == 5

        overview = ContextPage(
            workspace_id=workspace_id,
            slug="reconciled-overview-page",
            title="Reconciled Overview",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(overview)
        session.flush()
        session.add(
            WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind="page_ref",
                parent_id=folder.id,
                sort_key="overview",
                title=overview.title,
                slug=overview.slug,
                target_kind="context_page",
                target_id=overview.id,
                visibility_status="visible",
            )
        )
        session.flush()
        second = run_trail_tick(session, workspace_id, now=NOW)

    assert second["folder_overview_tasks"] == 0
    assert task.status == "expired"
