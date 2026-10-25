import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.container import ToolCallContext, assemble
from backend.app.settings import Settings
from backend.mcp import maintenance_tools
from backend.mcp.router import dispatch
from backend.mcp.scopes import visible_tool_names
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding, LintRun
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.services.body_preparer import BodyPreparer
from backend.services.client_round_state import (
    MAINTENANCE_STATE_KIND,
    ClientRoundStateCodec,
)
from backend.services.delegated_tasks import create_task
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


class _ProtocolOnlyProvider:
    """Structural DatabaseProvider with no concrete-provider reach-through."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def control_session_factory(self):
        return self._session_factory

    def session_for(self, _workspace_id):
        return self._session_factory


def _seed_folder_task(page_service, workspace_id, suffix):
    with page_service.session_factory.begin() as session:
        folder = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            parent_id=None,
            sort_key=f"maintenance-{suffix}",
            title=f"Maintenance {suffix}",
            slug=f"maintenance-{suffix}",
            target_kind=None,
            target_id=None,
            visibility_status="visible",
        )
        session.add(folder)
        session.flush()
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
        return task.id


def _round_trip(page_service, actor, workspace_id, wire, answer=None):
    first = dispatch(
        "wiki_maintenance",
        {"workspace_id": str(workspace_id), "wire": wire, "budget": 1},
        actor=actor,
        page_service=page_service,
    )
    if wire == "stable":
        task_id = first.maintenance_tasks[0]["task_id"]
        token = first.resume_token
    else:
        request = first.input_required["inputRequests"][0]
        task_id = request["id"]
        token = first.input_required["requestState"]
    second = dispatch(
        "wiki_maintenance",
        {
            "workspace_id": str(workspace_id),
            "wire": wire,
            "resume_token": token,
            "input_responses": {
                task_id: answer
                or {
                    "nav_summary": "Implementation belongs here; policy does not.",
                    "negative_cues": ["Policy belongs elsewhere."],
                }
            },
        },
        actor=actor,
        page_service=page_service,
    )
    return first, second, UUID(task_id)


def _published_page(session, workspace_id, slug):
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
        body_md=f"# {page.title}\n\nPublished maintenance content.",
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="maintenance-wire-test",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    return page, version


def _seed_m3_task(
    page_service, workspace_id, task_type, suffix, media_installer_factory=None
):
    with page_service.session_factory.begin() as session:
        if task_type == "page_meta_refresh":
            page, version = _published_page(
                session, workspace_id, f"page-meta-{suffix}"
            )
            task = create_task(
                session,
                workspace_id=workspace_id,
                task_type=task_type,
                target_kind="context_page",
                target_id=page.id,
                source="publish",
                fingerprint=str(version.id),
                payload={"published_version_id": str(version.id)},
                priority=90,
            )
        elif task_type == "folder_overview_draft":
            folder = WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                parent_id=None,
                sort_key=f"overview-{suffix}",
                title=f"Overview {suffix}",
                slug=f"overview-{suffix}",
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
                    slug=f"overview-{suffix}-child-{index}",
                    target_kind=None,
                    target_id=None,
                    visibility_status="visible",
                )
                for index in range(5)
            )
            session.flush()
            task = create_task(
                session,
                workspace_id=workspace_id,
                task_type=task_type,
                target_kind="workspace_tree_node",
                target_id=folder.id,
                source="lint",
                fingerprint="missing_overview",
                payload={"child_count": 5, "title": folder.title},
                priority=80,
            )
        elif task_type == "near_duplicate_verdict":
            page, _ = _published_page(
                session, workspace_id, f"duplicate-source-{suffix}"
            )
            candidate, _ = _published_page(
                session, workspace_id, f"duplicate-candidate-{suffix}"
            )
            now = datetime.now(UTC)
            run = LintRun(
                workspace_id=workspace_id,
                lint_scope={"kind": "librarian"},
                trigger_type="worker",
                status="completed",
                finding_count_by_severity={"info": 1},
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            source_finding = LintFinding(
                lint_run_id=run.id,
                workspace_id=workspace_id,
                target_type="context_page",
                target_id=page.id,
                rule_id="kb.semantic_near_duplicate",
                severity="info",
                status="open",
                suggested_action=json.dumps(
                    {
                        "candidate_page_id": str(candidate.id),
                        "similarity": 0.97,
                    }
                ),
                fingerprint=f"near-duplicate:{page.id}:{candidate.id}",
            )
            session.add(source_finding)
            session.flush()
            task = create_task(
                session,
                workspace_id=workspace_id,
                task_type=task_type,
                target_kind="context_page",
                target_id=page.id,
                source="lint",
                fingerprint=f"near-duplicate:{page.id}:{candidate.id}",
                payload={
                    "candidate_page_id": str(candidate.id),
                    "finding_id": str(source_finding.id),
                    "similarity": 0.97,
                },
                priority=70,
            )
        elif task_type == "import_batch_classify":
            registration = LibraryService().register_library_item(
                session=session,
                workspace_id=workspace_id,
                canonical_uri=f"https://example.com/import-{suffix}.pdf",
                title="Imported Manual",
                raw_text="Imported maintenance source.",
                format_hint="pdf",
            )
            ExtractionService(
                media_installer_factory=media_installer_factory
            ).extract_library_item_version(
                session=session,
                library_item_version_id=registration.version.id,
            )
            task = session.scalar(
                select(ClientDelegatedTask).where(
                    ClientDelegatedTask.workspace_id == workspace_id,
                    ClientDelegatedTask.task_type == task_type,
                    ClientDelegatedTask.target_id == registration.library_item.id,
                )
            )
            assert task is not None
        else:  # pragma: no cover - the parametrized catalog is closed below.
            raise AssertionError(task_type)
        return task.id


def _m3_answer(task_type):
    return {
        "page_meta_refresh": {
            "aliases": ["Maintenance Source"],
            "nav_description": "Published maintenance source.",
            "placement_hint": "keep",
        },
        "folder_overview_draft": {
            "title": "Maintenance Overview",
            "outline": ["Purpose", "Sources", "Navigation"],
            "body_md": "# Maintenance Overview\n\nA review-only overview draft.",
        },
        "near_duplicate_verdict": {
            "verdict": "distinct",
            "rationale": "The pages serve different workflows.",
            "negative_cues": ["Candidate content belongs elsewhere."],
        },
        "import_batch_classify": {
            "title": "Imported Manual",
            "page_type": "guide",
            "placement_hint": "/context/imports/",
        },
    }[task_type]


def _m3_domain_effect(page_service, task_id, task_type):
    with page_service.session_factory() as session:
        task = session.get(ClientDelegatedTask, task_id)
        assert task is not None
        common = {"status": task.status, "result_kind": task.result_ref.split(":")[0]}
        if task_type in {"page_meta_refresh", "import_batch_classify"}:
            finding = session.scalar(
                select(LintFinding).where(LintFinding.fingerprint == f"cdt:{task.id}")
            )
            assert finding is not None
            action = json.loads(finding.suggested_action)
            action.pop("task_id")
            return {**common, "rule_id": finding.rule_id, "action": action}
        if task_type == "near_duplicate_verdict":
            finding = session.get(LintFinding, UUID(task.payload["finding_id"]))
            assert finding is not None
            action = json.loads(finding.suggested_action)
            verdict = dict(action["client_llm_verdict"])
            verdict.pop("task_id")
            return {
                **common,
                "source_status": finding.status,
                "verdict": verdict,
                "negative_cues": action["negative_cues"],
            }
        parts = task.result_ref.split(":")
        page = session.get(ContextPage, UUID(parts[1]))
        version = session.get(ContextPageVersion, UUID(parts[3]))
        review = session.get(ReviewItem, UUID(parts[5]))
        assert page is not None and version is not None and review is not None
        return {
            **common,
            "page": (page.title, page.page_type, page.lifecycle_status),
            "body_md": version.body_md,
            "review_status": review.status,
        }


def test_preview_and_stable_yield_identical_domain_effects(
    page_service,
    mcp_default_actor,
    monkeypatch,
):
    settings = Settings(
        session_secret="maintenance-secret",
        mcp_preview_2026_07_28=True,
    )
    monkeypatch.setattr(maintenance_tools, "get_settings", lambda: settings)
    actor = mcp_default_actor(scopes=("mcp:write",))
    workspace_id = _workspace_id(page_service)
    _seed_folder_task(page_service, workspace_id, "stable")
    stable_first, stable_second, stable_task_id = _round_trip(
        page_service, actor, workspace_id, "stable"
    )
    _seed_folder_task(page_service, workspace_id, "preview")
    preview_first, preview_second, preview_task_id = _round_trip(
        page_service, actor, workspace_id, "mrtr_preview"
    )

    assert stable_first.wire_contract == "stable_2025_11_25"
    assert preview_first.wire_contract == "mrtr_preview_2026_07_28"
    assert stable_second.staged == [stable_task_id]
    assert preview_second.staged == [preview_task_id]
    with page_service.session_factory() as session:
        tasks = session.scalars(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.id.in_([stable_task_id, preview_task_id])
            )
        ).all()
        findings = session.scalars(
            select(LintFinding).where(
                LintFinding.fingerprint.in_(
                    [f"cdt:{stable_task_id}", f"cdt:{preview_task_id}"]
                )
            )
        ).all()
    assert {task.status for task in tasks} == {"staged"}
    assert {finding.rule_id for finding in findings} == {
        "maintenance.nav_summary_suggestion"
    }
    normalized = []
    for finding in findings:
        action = json.loads(finding.suggested_action)
        action.pop("task_id")
        normalized.append(action)
    assert normalized[0] == normalized[1]


def test_assembled_settings_sign_preview_round_trip_with_protocol_provider(
    page_service,
    mcp_default_actor,
):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="assembled-maintenance-secret",
        mcp_preview_2026_07_28=True,
    )
    provider = _ProtocolOnlyProvider(page_service.session_factory)
    container = assemble(
        settings,
        db=provider,
        page_service=page_service,
    )
    actor = mcp_default_actor(scopes=("mcp:write",))
    workspace_id = _workspace_id(page_service)
    task_id = _seed_folder_task(page_service, workspace_id, "assembled-settings")
    tool_context = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=page_service,
        services=container.get_services(),
    )

    first = dispatch(
        "wiki_maintenance",
        {
            "workspace_id": str(workspace_id),
            "wire": "mrtr_preview",
            "budget": 1,
        },
        tool_context=tool_context,
    )
    request = first.input_required["inputRequests"][0]
    assert request["id"] == str(task_id)
    claims = ClientRoundStateCodec(
        secret="assembled-maintenance-secret",
        kind=MAINTENANCE_STATE_KIND,
    ).verify(
        first.input_required["requestState"],
        workspace_id=workspace_id,
    )
    assert claims.task_ids == (task_id,)

    second = dispatch(
        "wiki_maintenance",
        {
            "workspace_id": str(workspace_id),
            "wire": "mrtr_preview",
            "resume_token": first.input_required["requestState"],
            "input_responses": {
                str(task_id): {
                    "nav_summary": "Implementation belongs here; policy does not.",
                    "negative_cues": ["Policy belongs elsewhere."],
                }
            },
        },
        tool_context=tool_context,
    )

    assert second.staged == [task_id]


@pytest.mark.parametrize(
    "task_type",
    (
        "page_meta_refresh",
        "folder_overview_draft",
        "near_duplicate_verdict",
        "import_batch_classify",
    ),
)
def test_m3_task_preview_and_stable_yield_identical_domain_effects(
    page_service,
    mcp_default_actor,
    monkeypatch,
    task_type,
    media_installer_factory,
):
    prepared_provenances = []
    prepare = BodyPreparer.prepare

    def record_prepare(self, input, ctx):
        prepared_provenances.append(ctx.actor_provenance)
        return prepare(self, input, ctx)

    monkeypatch.setattr(BodyPreparer, "prepare", record_prepare)
    monkeypatch.setattr(
        maintenance_tools,
        "get_settings",
        lambda: Settings(
            session_secret="maintenance-secret",
            mcp_preview_2026_07_28=True,
        ),
    )
    actor = mcp_default_actor(scopes=("mcp:write",))
    workspace_id = _workspace_id(page_service)
    stable_task_id = _seed_m3_task(
        page_service, workspace_id, task_type, "stable", media_installer_factory
    )
    _, stable_result, returned_stable_id = _round_trip(
        page_service,
        actor,
        workspace_id,
        "stable",
        _m3_answer(task_type),
    )
    assert returned_stable_id == stable_task_id
    preview_task_id = _seed_m3_task(
        page_service, workspace_id, task_type, "preview", media_installer_factory
    )
    _, preview_result, returned_preview_id = _round_trip(
        page_service,
        actor,
        workspace_id,
        "mrtr_preview",
        _m3_answer(task_type),
    )
    assert returned_preview_id == preview_task_id
    assert stable_result.staged == [stable_task_id]
    assert preview_result.staged == [preview_task_id]
    if task_type == "folder_overview_draft":
        assert set(prepared_provenances) == {actor.provenance}
    assert _m3_domain_effect(
        page_service, stable_task_id, task_type
    ) == _m3_domain_effect(page_service, preview_task_id, task_type)


def test_preview_flag_off_returns_stable_shape_and_warning(
    page_service,
    mcp_default_actor,
    monkeypatch,
):
    monkeypatch.setattr(
        maintenance_tools,
        "get_settings",
        lambda: Settings(
            session_secret="maintenance-secret",
            mcp_preview_2026_07_28=False,
        ),
    )
    actor = mcp_default_actor(scopes=("mcp:write",))
    workspace_id = _workspace_id(page_service)
    _seed_folder_task(page_service, workspace_id, "fallback")

    result = dispatch(
        "wiki_maintenance",
        {"workspace_id": str(workspace_id), "wire": "mrtr_preview"},
        actor=actor,
        page_service=page_service,
    )

    assert result.wire_contract == "stable_2025_11_25"
    assert result.input_required is None
    assert result.maintenance_tasks
    assert [warning.code for warning in result.envelope.warnings] == [
        "mrtr_preview_unavailable"
    ]


def test_wiki_maintenance_requires_write_scope(page_service, mcp_default_actor):
    workspace_id = _workspace_id(page_service)
    read_actor = mcp_default_actor(scopes=("mcp:read",))

    assert "wiki_maintenance" not in visible_tool_names(read_actor)
    result = dispatch(
        "wiki_maintenance",
        {"workspace_id": str(workspace_id)},
        actor=read_actor,
        page_service=page_service,
    )
    assert result.envelope.error_code == "scope_denied"


def test_wiki_maintenance_rejects_tampered_state_without_mutation(
    page_service,
    mcp_default_actor,
    monkeypatch,
):
    monkeypatch.setattr(
        maintenance_tools,
        "get_settings",
        lambda: Settings(session_secret="maintenance-secret"),
    )
    actor = mcp_default_actor(scopes=("mcp:write",))
    workspace_id = _workspace_id(page_service)
    task_id = _seed_folder_task(page_service, workspace_id, "tampered")
    first = dispatch(
        "wiki_maintenance",
        {"workspace_id": str(workspace_id)},
        actor=actor,
        page_service=page_service,
    )
    token = first.resume_token
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    rejected = dispatch(
        "wiki_maintenance",
        {
            "workspace_id": str(workspace_id),
            "resume_token": tampered,
            "input_responses": {str(task_id): {"nav_summary": "Valid summary"}},
        },
        actor=actor,
        page_service=page_service,
    )

    assert rejected.envelope.error_code == "client_state_invalid"
    with page_service.session_factory() as session:
        task = session.get(ClientDelegatedTask, task_id)
        assert task.status == "leased"
        assert session.scalar(select(LintFinding)) is None
