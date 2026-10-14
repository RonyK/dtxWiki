from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, EdgeProjection, PageLink
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding, LintRun
from backend.services.delegated_tasks import (
    create_task,
    produce_page_meta_refresh_task,
)
from backend.services.entity_ref import context_concept_path
from backend.services.page_link_ordering import page_link_snippet_order

_LIVE_FINDING_STATUSES = ("open", "acknowledged")
_OPEN_TASK_STATUSES = ("pending", "leased", "answered", "dismissed")


def process_page_publish_maintenance(
    session: Session,
    *,
    page: ContextPage,
    published_version_id: UUID,
    reason: str | None,
) -> int:
    """Run every synchronous maintenance projection for one publish instant."""

    created = mark_dependent_pages_stale(
        session,
        workspace_id=page.workspace_id,
        published_page_id=page.id,
        published_version_id=published_version_id,
        reason=reason,
    )
    produce_page_meta_refresh_task(
        session,
        workspace_id=page.workspace_id,
        page_id=page.id,
        published_version_id=published_version_id,
        title=page.title,
        concept_path=context_concept_path(page.slug),
        reason=reason,
    )
    return created


def mark_dependent_pages_stale(
    session: Session,
    *,
    workspace_id: UUID,
    published_page_id: UUID,
    published_version_id: UUID,
    reason: str | None,
) -> int:
    _resolve_stale_findings_for_republished_page(
        session,
        workspace_id=workspace_id,
        page_id=published_page_id,
    )
    dependent_ids = list(
        session.scalars(
            select(EdgeProjection.source_id)
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == "context_page",
                EdgeProjection.target_kind == "context_page",
                EdgeProjection.target_id == published_page_id,
                EdgeProjection.edge_kind.in_(("wikilink", "citation_page")),
                EdgeProjection.source_id != published_page_id,
            )
            .distinct()
        ).all()
    )
    if not dependent_ids:
        return 0
    dependents = session.scalars(
        select(ContextPage).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id.in_(dependent_ids),
            ContextPage.lifecycle_status != "archived",
        )
    ).all()
    if not dependents:
        return 0

    now = datetime.now(UTC)
    lint_run: LintRun | None = None
    created = 0
    for dependent in dependents:
        dependent.stale_candidate = True
        anchor = session.scalar(
            select(PageLink.first_snippet)
            .where(
                PageLink.workspace_id == workspace_id,
                PageLink.source_page_id == dependent.id,
                PageLink.source_version_id
                == dependent.current_published_version_id,
                PageLink.target_page_id == published_page_id,
            )
            .order_by(*page_link_snippet_order())
            .limit(1)
        )
        fingerprint = _stale_fingerprint(dependent.id, published_page_id, anchor)
        candidates = session.scalars(
            select(LintFinding)
            .where(
                LintFinding.workspace_id == workspace_id,
                LintFinding.rule_id == "kb.dependent_maybe_stale",
                LintFinding.target_id == dependent.id,
                LintFinding.status.in_(_LIVE_FINDING_STATUSES),
            )
            .order_by(LintFinding.created_at.desc(), LintFinding.id.desc())
        ).all()
        matching = [
            finding
            for finding in candidates
            if _suggested_source_page_id(finding) == published_page_id
        ]
        action = {
            "published_page_id": str(published_page_id),
            "published_version_id": str(published_version_id),
            "anchor": anchor,
            "reason": reason,
            "confidence": "unreviewed",
        }
        if matching:
            finding = matching[0]
            for duplicate in matching[1:]:
                duplicate.status = "fixed"
                duplicate.resolved_at = now
                expire_stale_contradiction_tasks(
                    session,
                    workspace_id=workspace_id,
                    finding_ids={duplicate.id},
                    target_ids={dependent.id},
                )
            finding.fingerprint = fingerprint
            finding.severity = "info"
            finding.suggested_action = json.dumps(
                action, ensure_ascii=False, sort_keys=True
            )
            expire_stale_contradiction_tasks(
                session,
                workspace_id=workspace_id,
                finding_ids={finding.id},
                target_ids={dependent.id},
            )
        else:
            if lint_run is None:
                lint_run = LintRun(
                    workspace_id=workspace_id,
                    lint_scope={"kind": "maintenance"},
                    trigger_type="publish_propagation",
                    status="completed",
                    finding_count_by_severity={},
                    started_at=now,
                    completed_at=now,
                )
                session.add(lint_run)
                session.flush()
            finding = LintFinding(
                lint_run_id=lint_run.id,
                workspace_id=workspace_id,
                target_type="context_page",
                target_id=dependent.id,
                rule_id="kb.dependent_maybe_stale",
                severity="info",
                status="open",
                suggested_action=json.dumps(
                    action, ensure_ascii=False, sort_keys=True
                ),
                fingerprint=fingerprint,
            )
            session.add(finding)
            session.flush()
            created += 1
            _expire_suppressed_stale_tasks(
                session,
                workspace_id=workspace_id,
                target_id=dependent.id,
                fingerprint=fingerprint,
            )
        create_task(
            session,
            workspace_id=workspace_id,
            task_type="stale_contradiction_check",
            target_kind="context_page",
            target_id=dependent.id,
            source="publish",
            fingerprint=fingerprint,
            payload={
                **action,
                "finding_id": str(finding.id),
                "dependent_page_id": str(dependent.id),
            },
            priority=55,
        )
    if lint_run is not None:
        lint_run.finding_count_by_severity = {"info": created}
    session.flush()
    return created


def expire_stale_contradiction_tasks(
    session: Session,
    *,
    workspace_id: UUID,
    finding_ids: set[UUID],
    target_ids: set[UUID] | None = None,
) -> int:
    if not finding_ids:
        return 0
    statement = select(ClientDelegatedTask).where(
        ClientDelegatedTask.workspace_id == workspace_id,
        ClientDelegatedTask.task_type == "stale_contradiction_check",
        ClientDelegatedTask.status.in_(_OPEN_TASK_STATUSES),
    )
    if target_ids:
        statement = statement.where(ClientDelegatedTask.target_id.in_(target_ids))
    tasks = session.scalars(statement).all()
    expired = 0
    for task in tasks:
        raw_id = task.payload.get("finding_id")
        try:
            finding_id = UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        if finding_id not in finding_ids:
            continue
        task.status = "expired"
        task.lease_expires_at = None
        expired += 1
    session.flush()
    return expired


def _resolve_stale_findings_for_republished_page(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
) -> None:
    findings = session.scalars(
        select(LintFinding).where(
            LintFinding.workspace_id == workspace_id,
            LintFinding.rule_id == "kb.dependent_maybe_stale",
            LintFinding.target_id == page_id,
            LintFinding.status.in_(_LIVE_FINDING_STATUSES),
        )
    ).all()
    now = datetime.now(UTC)
    for finding in findings:
        finding.status = "fixed"
        finding.resolved_at = now
    expire_stale_contradiction_tasks(
        session,
        workspace_id=workspace_id,
        finding_ids={finding.id for finding in findings},
        target_ids={page_id},
    )
    page = session.get(ContextPage, page_id)
    if page is not None and page.workspace_id == workspace_id:
        page.stale_candidate = False


def _stale_fingerprint(
    dependent_page_id: UUID,
    published_page_id: UUID,
    anchor: str | None,
) -> str:
    base = f"kb.dependent_maybe_stale|{dependent_page_id}|{published_page_id}"
    normalized = " ".join((anchor or "").split()).casefold()
    if not normalized:
        return base
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{base}|anchor:{digest}"


def _suggested_source_page_id(finding: LintFinding) -> UUID | None:
    try:
        action = json.loads(finding.suggested_action or "{}")
        return UUID(str(action.get("published_page_id")))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _expire_suppressed_stale_tasks(
    session: Session,
    *,
    workspace_id: UUID,
    target_id: UUID,
    fingerprint: str,
) -> None:
    tasks = session.scalars(
        select(ClientDelegatedTask).where(
            ClientDelegatedTask.workspace_id == workspace_id,
            ClientDelegatedTask.task_type == "stale_contradiction_check",
            ClientDelegatedTask.target_id == target_id,
            ClientDelegatedTask.fingerprint == fingerprint,
            ClientDelegatedTask.status == "dismissed",
        )
    ).all()
    for task in tasks:
        task.status = "expired"
        task.lease_expires_at = None


def clear_stale_candidate_if_resolved(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    excluding_finding_id: UUID,
) -> None:
    remaining = session.scalar(
        select(func.count(LintFinding.id)).where(
            LintFinding.workspace_id == workspace_id,
            LintFinding.rule_id == "kb.dependent_maybe_stale",
            LintFinding.target_id == page_id,
            LintFinding.id != excluding_finding_id,
            LintFinding.status.in_(("open", "acknowledged")),
        )
    )
    if remaining:
        return
    page = session.get(ContextPage, page_id)
    if page is not None and page.workspace_id == workspace_id:
        page.stale_candidate = False
