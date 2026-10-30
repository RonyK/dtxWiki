"""One real transactional lifecycle for the lint system writer."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.lint import LintFinding
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.services.lint import LintRegistry
from backend.services.lint_fix_writer import LintFixWriter
from backend.services.lint_service import LintService


def test_lint_fix_writer_keeps_edits_cas_retries_and_rollback_atomic(page_service):
    # Exception to the ordinary unit-first budget: existing writer matrices do
    # not execute queued lint, hold real page locks, replay durable receipts,
    # or compose begin/save/submit/publish inside a caller-owned transaction.
    # One page lifecycle covers distinct failures: pipeline+reference copying,
    # duplicate retries, a live edit, a changed head, outer rollback, and an
    # explicit auto-approval veto. These cannot be proved by mocked sessions.
    registry = LintRegistry.builtin()
    writer = LintFixWriter(lint_registry=registry)
    target = page_service.write_page(
        workspace_slug="default",
        slug="lint-target",
        title="Lint Target",
        body_md="# Lint Target",
        actor_kind="human",
        actor_id="author",
        actor_provenance="browser_session",
    )
    workspace_id = target.workspace_id
    missing_id = uuid4()

    def legacy_source(base=None, *, suffix=""):
        with page_service.session_factory.begin() as session:
            workspace = session.get(Workspace, workspace_id)
            workspace.lint_policy = {"link_fix_mode": "suggest"}
            workspace.auto_fix_enabled = False
        page = page_service.write_page(
            workspace_slug="default",
            slug="lint-source",
            title="Lint Source",
            body_md=(
                "# Lint Source\n\n[Target](/context/lint-target.md)\n \n"
                f"[Missing](/p/{workspace_id}/{missing_id})\n" + suffix
            ),
            actor_kind="human",
            actor_id="author",
            actor_provenance="browser_session",
            base_version_id=base.version_id if base else None,
            base_content_hash=base.content_hash if base else None,
        )
        with page_service.session_factory.begin() as session:
            workspace = session.get(Workspace, workspace_id)
            workspace.lint_policy = {"link_fix_mode": "auto"}
            workspace.auto_fix_enabled = True
        return page

    source = legacy_source()
    with page_service.session_factory.begin() as session:
        claim = ContextClaim(
            workspace_id=workspace_id,
            page_version_id=source.version_id,
            claim_text="Lint Source",
            location_in_page={"kind": "whole_page"},
            created_by_actor_type="human",
        )
        session.add(claim)
        session.flush()
        source_claim_id = claim.id
        citation = ContextCitation(
            workspace_id=workspace_id,
            target_kind="context_claim",
            target_id=claim.id,
            citation_type="context_page",
            cited_context_page_version_id=target.version_id,
            verification_status="verified",
            created_via="cite_tool",
            location={"kind": "whole_page"},
            snippet="Original cited evidence",
        )
        session.add(citation)
    with page_service.session_factory.begin() as session:
        run = LintService(lint_registry=registry).execute_queued_run(
            session, workspace_id=workspace_id, run_id=None, trigger_type="manual"
        )
        run_id = run.id
    with page_service.session_factory() as session:
        page = session.get(ContextPage, source.id)
        fixed_id = page.current_published_version_id
        assert fixed_id != source.version_id
        fixed = session.get(ContextPageVersion, fixed_id)
        assert f"](/p/{workspace_id}/{target.id})" in fixed.body_md
        assert page.latest_draft_version_id is None
        copied_claim = session.scalar(
            select(ContextClaim).where(ContextClaim.page_version_id == fixed_id)
        )
        assert copied_claim.id != source_claim_id
        copied_citation = session.scalar(
            select(ContextCitation).where(
                ContextCitation.target_kind == "context_claim",
                ContextCitation.target_id == copied_claim.id,
            )
        )
        assert copied_citation.cited_context_page_version_id == target.version_id
        assert copied_citation.verification_status == "verified"
        assert copied_citation.snippet == "Original cited evidence"
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "page.lint_autofix",
                AuditEvent.target_id == source.id,
            )
        )
        assert audit.payload["source"] == "lint.autofix"
        assert audit.payload["module_fix_counts"]["links"] >= 1
        assert audit.payload["module_fix_counts"]["format"] >= 1
        # Reconciliation uses actual current findings: a repaired locator
        # resolves immediately, while another unavailable destination remains.
        noncanonical = list(
            session.scalars(
                select(LintFinding).where(
                    LintFinding.target_id == source.id,
                    LintFinding.rule_id == "link.internal-noncanonical",
                )
            )
        )
        assert noncanonical and all(row.status == "fixed" for row in noncanonical)
        assert (
            session.scalar(
                select(LintFinding.id).where(
                    LintFinding.target_id == source.id,
                    LintFinding.rule_id == "link.unresolved",
                    LintFinding.status == "open",
                )
            )
            is not None
        )
        fixed_count = session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == source.id)
        )
    with page_service.session_factory.begin() as session:
        replay = writer.fix_page(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            page_id=source.id,
            base_version_id=source.version_id,
            base_content_hash=source.content_hash,
        )
        assert replay.state == "fixed" and replay.version_id == fixed_id
        LintService(lint_registry=registry).execute_queued_run(
            session, workspace_id=workspace_id, run_id=run_id, trigger_type="manual"
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == source.id)
            )
            == fixed_count
        )

    base = page_service.read_page(page_id=source.id)
    busy_base = legacy_source(base)
    editing = page_service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="author",
        actor_provenance="browser_session",
        idempotency_key="lint-test-open",
        base_version_id=busy_base.version_id,
        base_content_hash=busy_base.content_hash,
    )
    with page_service.session_factory.begin() as session:
        before = session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == source.id)
        )
        skipped = writer.fix_page(
            session,
            workspace_id=workspace_id,
            run_id=uuid4(),
            page_id=source.id,
            base_version_id=busy_base.version_id,
            base_content_hash=busy_base.content_hash,
        )
        assert skipped.state == "skipped"
        assert (
            session.get(ContextPageVersion, editing.version_id).editing_revision
            == editing.editing_revision
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == source.id)
            )
            == before
        )
    page_service.discard_context_edit(
        page_id=source.id,
        version_id=editing.version_id,
        workspace_id=workspace_id,
        actor_kind="human",
        actor_id="author",
        idempotency_key="lint-test-discard",
    )
    changed = legacy_source(busy_base, suffix="\nA later human change.\n")

    def transaction_counts(session):
        # All effects of this one page lifecycle belong to the same workspace.
        # A caught error or outer rollback must not orphan any of these rows.
        return {
            model.__tablename__: session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == workspace_id)
            )
            for model in (
                ContextPageVersion, ContextClaim, ContextCitation, ChangeSet,
                ChangeOperation, ReviewItem, AuditEvent, MCPWriteIdempotency,
            )
        }

    with page_service.session_factory.begin() as session:
        stale = writer.fix_page(
            session,
            workspace_id=workspace_id,
            run_id=uuid4(),
            page_id=source.id,
            base_version_id=busy_base.version_id,
            base_content_hash=busy_base.content_hash,
        )
        assert stale.state == "skipped"
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == changed.version_id
        )
        version_count = session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == source.id)
        )
        before_replay = transaction_counts(session)
        # The completed run cannot become a new sweep after a later human edit
        # reintroduces the same fixable locator. A fresh run is required.
        completed = LintService(lint_registry=registry).execute_queued_run(
            session, workspace_id=workspace_id, run_id=run_id, trigger_type="manual"
        )
        assert completed.id == run_id
        assert transaction_counts(session) == before_replay
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == changed.version_id
        )
    rollback_run_id = uuid4()
    with pytest.raises(RuntimeError, match="rollback caller"):
        with page_service.session_factory.begin() as session:
            result = writer.fix_page(
                session,
                workspace_id=workspace_id,
                run_id=rollback_run_id,
                page_id=source.id,
                base_version_id=changed.version_id,
                base_content_hash=changed.content_hash,
            )
            assert result.state == "fixed"
            raise RuntimeError("rollback caller")
    with page_service.session_factory.begin() as session:
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == changed.version_id
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == source.id)
            )
            == version_count
        )
        assert transaction_counts(session) == before_replay
        assert (
            session.scalar(
                select(MCPWriteIdempotency.id).where(
                    MCPWriteIdempotency.idempotency_key
                    == f"lint-fix:{rollback_run_id}:{source.id}:{changed.version_id}"
                )
            )
            is None
        )
        workspace = session.get(Workspace, workspace_id)
        workspace.lint_policy = {
            "link_fix_mode": "auto",
            "auto_approve_link_fixes": False,
        }
        session.get(ContextPage, source.id).page_type = "decision"
    with page_service.session_factory.begin() as session:
        reviewed = writer.fix_page(
            session,
            workspace_id=workspace_id,
            run_id=uuid4(),
            page_id=source.id,
            base_version_id=changed.version_id,
            base_content_hash=changed.content_hash,
        )
        assert reviewed.state == "review"
        page = session.get(ContextPage, source.id)
        assert page.current_published_version_id == changed.version_id
        assert page.latest_draft_version_id == reviewed.version_id
        review = session.scalar(
            select(ReviewItem)
            .join(
                ChangeOperation,
                ChangeOperation.change_set_id == ReviewItem.change_set_id,
            )
            .where(
                ReviewItem.review_type == "context_update",
                ChangeOperation.target_id == source.id,
                ChangeOperation.diff_ref
                == f"context_page_version:{reviewed.version_id}",
            )
        )
        assert review.status == "pending"
        assert review.risk_signals["decision_page_type"] is True
        assert review.risk_signals["human_review_required"] is True

    # A pending new draft must not hide a defect that remains in the published
    # version. The same R3 multi-version reconciliation handles this re-run.
    with page_service.session_factory.begin() as session:
        LintService(lint_registry=registry).execute_queued_run(
            session, workspace_id=workspace_id, run_id=None, trigger_type="manual"
        )
        assert (
            session.scalar(
                select(LintFinding.id).where(
                    LintFinding.target_id == source.id,
                    LintFinding.rule_id == "link.internal-noncanonical",
                    LintFinding.status == "open",
                )
            )
            is not None
        )
