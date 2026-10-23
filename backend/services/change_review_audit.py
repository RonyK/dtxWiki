import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.services.audit_service import AuditService
from backend.services.auto_approval import evaluate_context_write
from backend.services.lint import LintRegistry
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_risk_policy import (
    ReviewRiskResolver,
    review_risk_snapshot,
)
from backend.spi.types import AI_PROVENANCES
from backend.utils.uuid import parse_uuid_or_none


def _write_decision_summary(
    ai_authored: bool,
    review_required: bool,
) -> str:
    if review_required:
        return (
            "agent-authored page write requires human review"
            if ai_authored
            else "page write held for review"
        )
    return (
        "agent-authored page write approved by policy"
        if ai_authored
        else "human page write applied directly"
    )


@dataclass(frozen=True)
class ChangeReviewAuditResult:
    change_set_id: UUID
    change_operation_id: UUID
    review_item_id: UUID
    audit_event_id: UUID


def _bypass_decision(review: ReviewItem, change_set: ChangeSet) -> ReviewDecision:
    # review_required was False, so `review` above was constructed already
    # "approved" -- no human or AI ever decided anything. Every OTHER path
    # that resolves a ReviewItem (apply_decision, sweep_auto_approve_reviews)
    # writes a matching ReviewDecision row, so the Review screen's "Resolved"
    # count (ReviewItem.status) and its "Recent decisions" list (ReviewDecision
    # rows) always agree. This bypass path was the one exception: it left
    # "Resolved: N" standing next to "No decisions yet" for the same events
    # (H-6, 260908 UI audit). Recording the decision here closes that gap.
    return ReviewDecision(
        workspace_id=review.workspace_id,
        review_item_id=review.id,
        decision_type="approve",
        actor_type="system",
        actor_id=None,
        decision_reason="review not required by policy",
        resulting_review_status=review.status,
        resulting_change_set_status=change_set.status,
    )


class ChangeReviewAuditRecorder:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        *,
        lint_registry: LintRegistry | None = None,
    ) -> None:
        self.lint_registry = (
            lint_registry if lint_registry is not None else LintRegistry.builtin()
        )
        self.audit_service = audit_service or AuditService()
        self.risk_resolver = ReviewRiskResolver()

    def record_context_page_write(
        self,
        session: Session,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        action: str,
        page_id: UUID,
        version_id: UUID,
        actor_provenance: str,
        payload: dict[str, Any] | None = None,
        body: str | None = None,
        needs_review: bool = False,
    ) -> ChangeReviewAuditResult:
        ai_authored = actor_provenance in AI_PROVENANCES
        operation_kind = "create" if action == "page.create" else "update"
        gate = evaluate_context_write(
            session,
            workspace_id=workspace_id,
            needs_review=needs_review,
            body=body,
            lint_registry=self.lint_registry,
        )
        # Report the fact -- who wrote it -- and let the resolver decide whether
        # that needs a person. Forcing `review_required` here was the same
        # decision made twice, and it put it in the one place a workspace could
        # not reach.
        risk_resolution = self._resolve_risk(
            session=session,
            workspace_id=workspace_id,
            event="direct_page_write_audit",
            signals={
                "ai_authored": ai_authored,
                **gate.force_signals(),
            },
            actor_provenance=actor_provenance,
        )
        review_required = risk_resolution.review_required
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason=action,
            trigger_type=(
                "agent_authenticated_write" if review_required else "manual_edit"
            ),
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="pending_review" if review_required else "applied",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation=operation_kind,
            diff_summary=action,
            diff_ref=f"context_page_version:{version_id}",
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="context_update",
            status="pending" if review_required else "approved",
            risk_level=risk_resolution.risk_level,
            **review_risk_snapshot(risk_resolution),
            decision_summary=_write_decision_summary(ai_authored, review_required),
        )
        session.add_all([operation, review])
        session.flush()
        if not review_required:
            session.add(_bypass_decision(review, change_set))
            session.flush()

        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=action,
            target_kind="context_page",
            target_id=page_id,
            payload=payload,
            event_summary=action,
            retention_class="standard",
        )
        return ChangeReviewAuditResult(
            change_set_id=change_set.id,
            change_operation_id=operation.id,
            review_item_id=review.id,
            audit_event_id=audit_event.id,
        )

    def record_context_page_alternative(
        self,
        session: Session,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        page_id: UUID,
        version_id: UUID,
        actor_provenance: str,
        payload: dict[str, Any] | None = None,
    ) -> ChangeReviewAuditResult:
        risk_resolution = self._resolve_risk(
            session=session,
            workspace_id=workspace_id,
            event="stale_alternative",
            signals={
                "ai_authored": actor_provenance in AI_PROVENANCES,
                "review_required": True,
            },
            actor_provenance=actor_provenance,
        )
        review_required = risk_resolution.review_required
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="page.preserve_alternative",
            trigger_type="manual_stale_recovery",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="pending_review" if review_required else "applied",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation="preserve_alternative",
            diff_summary="stale edit preserved as alternative draft",
            diff_ref=f"context_page_version:{version_id}",
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="context_update",
            status="pending" if review_required else "approved",
            risk_level=risk_resolution.risk_level,
            **review_risk_snapshot(risk_resolution),
            decision_summary="stale edit preserved as alternative draft",
        )
        session.add_all([operation, review])
        session.flush()
        if not review_required:
            session.add(_bypass_decision(review, change_set))
            session.flush()

        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="page.preserve_alternative",
            target_kind="context_page",
            target_id=page_id,
            payload=payload,
            event_summary="page.preserve_alternative",
            retention_class="standard",
        )
        return ChangeReviewAuditResult(
            change_set_id=change_set.id,
            change_operation_id=operation.id,
            review_item_id=review.id,
            audit_event_id=audit_event.id,
        )

    def record_context_page_inline_comment(
        self,
        session: Session,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        page_id: UUID,
        version_id: UUID,
        note: str,
        anchor: dict[str, Any],
        actor_provenance: str,
    ) -> ChangeReviewAuditResult:
        risk_resolution = self._resolve_risk(
            session=session,
            workspace_id=workspace_id,
            event="inline_comment",
            signals={
                "ai_authored": actor_provenance in AI_PROVENANCES,
                "review_required": True,
            },
            actor_provenance=actor_provenance,
        )
        review_required = risk_resolution.review_required
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="page.inline_comment",
            trigger_type="inline_comment",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="pending_review" if review_required else "applied",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation="inline_comment",
            diff_summary=note[:512],
            diff_ref=json.dumps(
                {
                    "kind": "page_inline_comment_anchor",
                    "version_id": str(version_id),
                    "anchor": anchor,
                },
                separators=(",", ":"),
            ),
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="page_inline_comment",
            status="pending" if review_required else "approved",
            risk_level=risk_resolution.risk_level,
            **review_risk_snapshot(risk_resolution),
            decision_summary=note,
        )
        session.add_all([operation, review])
        session.flush()
        if not review_required:
            session.add(_bypass_decision(review, change_set))
            session.flush()

        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="page.inline_comment",
            target_kind="context_page",
            target_id=page_id,
            payload={
                "review_item_id": str(review.id),
                "change_set_id": str(change_set.id),
                "change_operation_id": str(operation.id),
                "anchor": anchor,
            },
            event_summary="page.inline_comment",
            retention_class="standard",
        )
        return ChangeReviewAuditResult(
            change_set_id=change_set.id,
            change_operation_id=operation.id,
            review_item_id=review.id,
            audit_event_id=audit_event.id,
        )

    def record_library_item_deleted(
        self,
        session: Session,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        page_id: UUID,
        library_item_id: UUID,
        item_title: str,
        source_uri: str,
        note: str,
        actor_provenance: str,
    ) -> ChangeReviewAuditResult:
        """Notify reviewers that a Library source cited by ``page_id`` was
        hard-deleted. Mirrors :meth:`record_context_page_inline_comment`: one
        pending ReviewItem per citing page surfaces the deletion in the review
        queue. The page body keeps a render-time tombstone for the dead
        ``@cite{...}`` token, so this notice is informational (low risk)."""
        risk_resolution = self._resolve_risk(
            session=session,
            workspace_id=workspace_id,
            event="source_deleted_notice",
            signals={
                "ai_authored": actor_provenance in AI_PROVENANCES,
                "review_required": True,
            },
            actor_provenance=actor_provenance,
        )
        review_required = risk_resolution.review_required
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="library.source.deleted",
            trigger_type="source_deleted",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="pending_review" if review_required else "applied",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page_id,
            operation="source_deleted_notice",
            diff_summary=note[:512],
            diff_ref=json.dumps(
                {
                    "kind": "library_source_deleted",
                    "library_item_id": str(library_item_id),
                    "item_title": item_title,
                    "source_uri": source_uri,
                },
                separators=(",", ":"),
            ),
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="source_deleted_notice",
            status="pending" if review_required else "approved",
            risk_level=risk_resolution.risk_level,
            **review_risk_snapshot(risk_resolution),
            decision_summary=note,
        )
        session.add_all([operation, review])
        session.flush()
        if not review_required:
            session.add(_bypass_decision(review, change_set))
            session.flush()

        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="library.source.deleted_notice",
            target_kind="context_page",
            target_id=page_id,
            payload={
                "review_item_id": str(review.id),
                "change_set_id": str(change_set.id),
                "change_operation_id": str(operation.id),
                "library_item_id": str(library_item_id),
                "source_uri": source_uri,
            },
            event_summary="library.source.deleted_notice",
            retention_class="standard",
        )
        return ChangeReviewAuditResult(
            change_set_id=change_set.id,
            change_operation_id=operation.id,
            review_item_id=review.id,
            audit_event_id=audit_event.id,
        )

    def _resolve_risk(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        event: str,
        signals: dict[str, object] | None = None,
        actor_provenance: str = "internal",
    ):
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        return self.risk_resolver.resolve(
            workspace=workspace,
            event=event,
            signals=signals,
            actor_provenance=actor_provenance,
        )
