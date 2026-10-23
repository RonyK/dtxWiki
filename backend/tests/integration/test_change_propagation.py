import json
from uuid import uuid4

from sqlalchemy import select

from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding
from backend.models.workspace import Workspace
from backend.repositories import context_page_repo
from backend.schemas.context_write import PublishContextPageRequest
from backend.schemas.lint import LintFindingDecisionRequest
from backend.services.change_propagation import (
    expire_stale_contradiction_tasks,
    mark_dependent_pages_stale,
)
from backend.services.context_write_service import ContextWriteService
from backend.services.lint_service import LintService
from backend.tests.utils.prepared_body import prepare_context_body


def _page(session, workspace_id, slug):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=slug.title(),
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {slug}",
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="test",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    return page, version


def _edge(session, workspace_id, source, target):
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=source.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=1.0,
            source_ref={"table": "page_links"},
        )
    )


def test_publish_propagation_marks_only_direct_dependents_and_stages_review(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        source, source_version = _page(session, workspace_id, "source")
        dependent, dependent_version = _page(session, workspace_id, "dependent")
        second_hop, _ = _page(session, workspace_id, "second-hop")
        _edge(session, workspace_id, dependent, source)
        _edge(session, workspace_id, second_hop, dependent)
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=dependent.id,
                source_version_id=dependent_version.id,
                target_slug=source.slug,
                target_page_id=source.id,
                occurrence_count=1,
                first_snippet="See source policy",
            )
        )

        created = mark_dependent_pages_stale(
            session,
            workspace_id=workspace_id,
            published_page_id=source.id,
            published_version_id=source_version.id,
            reason="policy changed",
        )
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.dependent_maybe_stale"
            )
        )
        task = session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "stale_contradiction_check"
            )
        )

        assert created == 1
        assert dependent.stale_candidate is True
        assert second_hop.stale_candidate is False
        assert "See source policy" in finding.suggested_action
        assert task.target_id == dependent.id
        assert task.payload["finding_id"] == str(finding.id)
        assert finding.severity == "info"

        task.status = "leased"
        task.lease_expires_at = source_version.created_at
        created_again = mark_dependent_pages_stale(
            session,
            workspace_id=workspace_id,
            published_page_id=source.id,
            published_version_id=uuid4(),
            reason="newer policy text",
        )
        session.refresh(finding)
        action = json.loads(finding.suggested_action)
        live_tasks = session.scalars(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "stale_contradiction_check",
                ClientDelegatedTask.status.in_(("pending", "leased")),
            )
        ).all()
        assert created_again == 0
        assert len(
            session.scalars(
                select(LintFinding).where(
                    LintFinding.rule_id == "kb.dependent_maybe_stale"
                )
            ).all()
        ) == 1
        assert action["reason"] == "newer policy text"
        assert "client_llm_verdict" not in action
        assert len(live_tasks) == 1

        LintService().decide_finding(
            session,
            workspace_id,
            finding.id,
            LintFindingDecisionRequest(decision="dismiss", reason="still valid"),
            actor_kind="human",
            actor_id="test",
            actor=None,
        )
        assert dependent.stale_candidate is False
        assert live_tasks[0].status == "expired"


def test_direct_publish_paths_share_propagation_and_dependent_publish_resolves(
    page_service, session_factory
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        source, source_version = _page(session, workspace_id, "rest-source")
        dependent, dependent_version = _page(
            session, workspace_id, "rest-dependent"
        )
        _edge(session, workspace_id, dependent, source)
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=dependent.id,
                source_version_id=dependent_version.id,
                target_slug=source.slug,
                target_page_id=source.id,
                occurrence_count=1,
                first_snippet="REST dependency",
            )
        )
        source_id = source.id
        dependent_id = dependent.id
        source_hash = source_version.content_hash
        source_version_id = source_version.id
        dependent_hash = dependent_version.content_hash
        dependent_version_id = dependent_version.id

    first = page_service.write_page(
        workspace_slug="default",
        slug="rest-source",
        title="Rest-Source",
        body_md="# Rest source\n\nChanged through write_page.",
        actor_kind="human",
        actor_id="test",
        base_version_id=source_version_id,
        base_content_hash=source_hash,
        actor_provenance="browser_session",
    )
    second = page_service.update_page(
        page_id=source_id,
        title="Rest-Source",
        body_md="# Rest source\n\nChanged through update_page.",
        actor_kind="human",
        actor_id="test",
        base_version_id=first.version_id,
        base_content_hash=first.content_hash,
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        findings = session.scalars(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.dependent_maybe_stale",
                LintFinding.target_id == dependent_id,
            )
        ).all()
        assert len(findings) == 1
        assert json.loads(findings[0].suggested_action)["published_version_id"] == str(
            second.version_id
        )
        dependent = session.get(ContextPage, dependent_id)
        assert dependent.stale_candidate is True

    page_service.update_page(
        page_id=dependent_id,
        title="Rest-Dependent",
        body_md="# Rest dependent\n\nReviewed after the source change.",
        actor_kind="human",
        actor_id="test",
        base_version_id=dependent_version_id,
        base_content_hash=dependent_hash,
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.dependent_maybe_stale",
                LintFinding.target_id == dependent_id,
            )
        )
        dependent = session.get(ContextPage, dependent_id)
        assert finding.status == "fixed"
        assert finding.resolved_at is not None
        assert dependent.stale_candidate is False


def test_mcp_publish_service_path_triggers_stale_propagation(session_factory):
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        source, source_version = _page(session, workspace_id, "mcp-source")
        dependent, dependent_version = _page(
            session,
            workspace_id,
            "mcp-dependent",
        )
        _edge(session, workspace_id, dependent, source)
        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=dependent.id,
                source_version_id=dependent_version.id,
                target_slug=source.slug,
                target_page_id=source.id,
                occurrence_count=1,
                first_snippet="MCP dependency",
            )
        )
        draft = context_page_repo.insert_version(
            session,
            page_id=source.id,
            parent_version_id=source_version.id,
            body=prepare_context_body(
                session,
                page_id=source.id,
                body_md="# MCP source\n\nChanged through publish_draft.",
                actor_kind="human",
                actor_id="test",
            ),
            actor_kind="human",
            actor_id="test",
            version_status="draft",
        )
        source.current_version_id = draft.id
        source.latest_draft_version_id = draft.id

        service.publish_draft(
            session,
            workspace_id,
            source.id,
            PublishContextPageRequest(
                version_id=draft.id,
                reason="publish through MCP service path",
            ),
            actor_kind="human",
            actor_id="test",
        )

        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.dependent_maybe_stale",
                LintFinding.target_id == dependent.id,
            )
        )

        assert finding is not None
        assert json.loads(finding.suggested_action)["published_version_id"] == str(
            draft.id
        )
        assert dependent.stale_candidate is True


def test_stale_task_expiry_filters_by_dependent_target_in_sql(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        target_id = uuid4()
        captured = []
        original_scalars = session.scalars

        def capture(statement, *args, **kwargs):
            captured.append(statement)
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", capture)
        expired = expire_stale_contradiction_tasks(
            session,
            workspace_id=workspace_id,
            finding_ids={uuid4()},
            target_ids={target_id},
        )

    assert expired == 0
    assert captured
    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "client_delegated_task.target_id IN" in sql
    assert target_id.hex in sql
