import base64
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from backend.context_media.ports import ContextMediaRepository
from backend.domain.input_policy import validate_text
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatSession
from backend.models.review import ReviewAudienceKind, ReviewDecision, ReviewItem
from backend.models.user import User
from backend.models.workspace import Workspace, default_actor_id
from backend.policy.auth import ActorContext
from backend.policy.rbac import role_allows
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.review_receipts import decision_id_conflict
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.schemas.context_write import (
    PublishContextPageRequest,
    ReviewActionAvailability,
    ReviewActionCapabilities,
    ReviewCountsPayload,
    ReviewDecisionPayload,
    ReviewDecisionRecentItem,
    ReviewDecisionRequest,
    ReviewDecisionUndo,
    ReviewEvidenceOperation,
    ReviewEvidencePayload,
    ReviewItemDetail,
    ReviewItemListPayload,
    ReviewRejectEffect,
    ReviewSettlement,
    ReviewStatusByRiskBreakdown,
    ReviewStatusRiskBreakdown,
    ReviewVersionRef,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.mcp.review_tools import (
    ListRequestedChangesPayload,
    RequestedChangeClaim,
    RequestedChangeEvidence,
    RequestedChangeItem,
    RequestedChangeTarget,
    SubmitRevisionForReviewResult,
)
from backend.services.actor_visible_read import (
    context_page_visible,
    context_version_is_visible,
)
from backend.services.audit_service import AuditService
from backend.services.body_preparer import BodyPreparer
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.job_queue import (
    JobQueueService,
    context_page_undo_embed_idempotency_key,
)
from backend.services.read_errors import (
    ChatOwnerUnresolvableError,
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
    RejectedByPolicyError,
    ReviewAlreadyClaimedError,
    ReviewDecisionIdConflictError,
    ReviewDecisionImpactChangedError,
    ReviewDecisionUndoUnavailableError,
    ReviewRejectBlockedError,
    ScopeDeniedError,
)
from backend.services.review_approval import (
    APPROVAL_UNAVAILABLE_REASONS,
    ReviewApprovalPreflight,
)
from backend.services.review_decision_effect import (
    EFFECT_FORMAT,
    DecisionEffectRecorder,
    effect_still_current,
    record_decision_effect,
    restore_effect,
    review_status_before,
)
from backend.services.review_risk_policy import (
    RISK_ORDER,
    auto_approve_by_risk_entries,
    normalized_review_policy,
)
from backend.services.review_supersession import supersede_review_with_lineage
from backend.services.review_targets import operation_version_id, project_review_target
from backend.utils.actor_display import resolve_actor_display_name
from backend.utils.uuid import parse_uuid_or_none

if TYPE_CHECKING:
    from backend.services.context_lifecycle import ContextRejectionPlan

REVIEW_STATUS_BY_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
}
CHANGE_SET_STATUS_BY_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "needs_changes",
}
REVIEW_CURSOR_PREFIX = "review-offset:"
REVIEW_SCOPES = ("to_me", "from_me", "all")
BULK_REVIEW_LIMIT = 500
CHANGES_REQUESTED_STATUS = "changes_requested"
RESOLVED_REVIEW_STATUSES = ("approved", "rejected")
# Soft-claim TTL (D1): a claim older than this is stale and may be taken over
# by another actor. Mirrors mcp_chat_service.IDLE_CLOSE_MINUTES.
CLAIM_TTL_MINUTES = 30
_INDIVIDUAL_AUDIENCE_KINDS = (
    ReviewAudienceKind.REQUESTER.value,
    ReviewAudienceKind.CHAT_OWNER.value,
)


def _audience_falls_back_to_owners(decision):
    return and_(
        decision.audience_kind.in_(_INDIVIDUAL_AUDIENCE_KINDS),
        ~select(User.id)
        .where(User.id == decision.audience_user_id, User.status == "active")
        .exists(),
    )


def _decision_audience_predicate(
    decision,
    *,
    principal: UUID,
    principal_role: str | None,
    include_owner_fallback: bool = True,
):
    return or_(
        and_(
            decision.audience_kind.in_(_INDIVIDUAL_AUDIENCE_KINDS),
            decision.audience_user_id == principal,
        ),
        (
            and_(
                role_allows(principal_role or "viewer", "owner"),
                _audience_falls_back_to_owners(decision),
            )
            if include_owner_fallback
            else false()
        ),
        and_(
            role_allows(principal_role or "viewer", "editor"),
            or_(
                decision.audience_kind == ReviewAudienceKind.WORKSPACE_EDITORS.value,
                decision.audience_kind.is_(None),
            ),
        ),
    )


# Why a superseded review was closed, by the audit action that closed it.
_SETTLEMENT_REASONS = {
    "review.superseded_by_approved_publish": "superseded_by_approved_publish",
    "review.superseded_by_rejected_ancestor": "superseded_by_rejected_ancestor",
    "review.superseded_by_rejected_candidate": "superseded_by_rejected_candidate",
    "review.revision_submitted": "superseded_by_newer_draft",
    "review.superseded_by_human_page_write": "superseded_by_newer_draft",
    "review.superseded_by_content_replacement": "superseded_by_newer_draft",
    "review.superseded_by_decision_undo": "superseded_by_decision_undo",
}


@dataclass(frozen=True)
class _UndoCandidate:
    decision: ReviewDecision
    effect: dict
    # False: something changed after the decision (see effect_still_current).
    available: bool


@dataclass(frozen=True)
class _RejectionImpact:
    page: ContextPage
    version: ContextPageVersion
    # None: the version is off the live chain and the rejection moves nothing.
    plan: "ContextRejectionPlan | None"
    # Other open reviews of the rejected version.
    siblings: list[ReviewItem]
    # Open reviews of the later drafts retired with it.
    cascaded: list[ReviewItem]
    # The restored draft has no open review, so the rejection opens one.
    restores_review: bool
    fingerprint: str


def _context_version_id_from_diff_ref(diff_ref: str | None) -> UUID | None:
    prefix = "context_page_version:"
    if not isinstance(diff_ref, str) or not diff_ref.startswith(prefix):
        return None
    return parse_uuid_or_none(diff_ref[len(prefix) :])


def review_decision_audience_clause(
    actor_role: str | None,
    audience_for: UUID | None,
    *,
    include_owner_fallback: bool = True,
):
    """Shared decision disclosure for Review and Context activity readers."""
    if audience_for is None:
        return ~false() if role_allows(actor_role or "viewer", "editor") else false()
    return _decision_audience_predicate(
        ReviewDecision,
        principal=audience_for,
        principal_role=actor_role,
        include_owner_fallback=include_owner_fallback,
    )


class ReviewService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository]
        | None = None,
        body_preparer: BodyPreparer | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.body_preparer = (
            body_preparer if body_preparer is not None else BodyPreparer()
        )
        self.context_media_repository_factory = context_media_repository_factory

    def list_reviews(
        self,
        session: Session,
        workspace_id: UUID,
        status: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        audience_for: UUID | None = None,
        scope: str = "all",
        actor_role: str | None = None,
        actor: ActorContext | None = None,
        target_page_id: UUID | None = None,
        include_deferred_reconciliations: bool = False,
    ) -> ReviewItemListPayload:
        # scope (B4): "all" = workspace-wide (current behavior); "to_me" =
        # requests routed to the authenticated principal P; "from_me" = open
        # requests P authored. P (audience_for) is server-derived from the
        # authenticated actor, never client-supplied.
        offset = _parse_cursor(cursor)
        statement = select(ReviewItem).where(ReviewItem.workspace_id == workspace_id)
        statement, resolved_scope = self.apply_list_visibility(
            session=session,
            statement=statement,
            workspace_id=workspace_id,
            audience_for=audience_for,
            scope=scope,
            actor_role=actor_role,
        )
        statuses = _expand_review_statuses(_parse_csv_filter(status))
        if statuses:
            statement = statement.where(ReviewItem.status.in_(statuses))
        risk_levels = _parse_csv_filter(risk_level)
        if risk_levels:
            statement = statement.where(ReviewItem.risk_level.in_(risk_levels))
        if target_page_id is not None:
            statement = statement.where(_targets_context_page(target_page_id))
        review_policy = session.scalar(
            select(Workspace.review_policy).where(Workspace.id == workspace_id)
        )
        attention_days = normalized_review_policy(review_policy)["reconciliation"][
            "attention_after_days"
        ]
        attention_cutoff = utc_now() - timedelta(days=attention_days)
        deferred_reconciliation = and_(
            ReviewItem.review_type == "context_reconciliation",
            ReviewItem.status.in_(("pending", "changes_requested")),
            ReviewItem.created_at <= attention_cutoff,
        )
        deferred_reconciliation_count = (
            session.scalar(
                select(func.count()).select_from(
                    statement.where(deferred_reconciliation).order_by(None).subquery()
                )
            )
            or 0
        )
        if not include_deferred_reconciliations:
            statement = statement.where(~deferred_reconciliation)
        order_by = (
            (ReviewItem.updated_at.desc(), ReviewItem.id.desc())
            if statuses and set(statuses).issubset(set(RESOLVED_REVIEW_STATUSES))
            else (ReviewItem.created_at.desc(), ReviewItem.id.desc())
        )
        rows = session.scalars(
            statement.order_by(*order_by).offset(offset).limit(limit + 1)
        ).all()
        total = None
        if target_page_id is not None:
            total = session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
        return ReviewItemListPayload(
            items=[
                self._detail(
                    session,
                    review,
                    actor_user_id=audience_for,
                    actor_role=actor_role,
                    actor=actor,
                )
                for review in rows[:limit]
            ],
            next_cursor=_encode_cursor(offset + limit) if len(rows) > limit else None,
            total=total,
            deferred_reconciliation_count=deferred_reconciliation_count,
        )

    def discard_reconciliations_bulk(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        review_item_ids: list[UUID],
        reason: str,
        actor_kind: str,
        actor_id: str,
    ):
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)
        from backend.models.context import ContextPage, ContextPageVersion
        from backend.schemas.context_reconciliation import (
            ContextReconciliationBulkDiscardResult,
            ContextReconciliationBulkDiscardSkipped,
        )
        from backend.services.context_reconciliation import (
            discard_reconciliation_branch,
        )

        discarded: list[UUID] = []
        skipped: list[ContextReconciliationBulkDiscardSkipped] = []
        for review_item_id in review_item_ids:
            review = session.scalar(
                select(ReviewItem).where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.id == review_item_id,
                )
            )
            if review is None or review.review_type != "context_reconciliation":
                skipped.append(
                    ContextReconciliationBulkDiscardSkipped(
                        review_item_id=review_item_id,
                        error_code="not_found",
                    )
                )
                continue
            operation = session.scalar(
                select(ChangeOperation).where(
                    ChangeOperation.workspace_id == workspace_id,
                    ChangeOperation.change_set_id == review.change_set_id,
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.operation == "patch_draft_diverged_manifest",
                )
            )
            version_id = _context_version_id_from_diff_ref(
                operation.diff_ref if operation is not None else None
            )
            page = (
                session.scalar(
                    select(ContextPage).where(
                        ContextPage.workspace_id == workspace_id,
                        ContextPage.id == operation.target_id,
                    )
                )
                if operation is not None
                else None
            )
            branch = (
                session.scalar(
                    select(ContextPageVersion).where(
                        ContextPageVersion.workspace_id == workspace_id,
                        ContextPageVersion.page_id == operation.target_id,
                        ContextPageVersion.id == version_id,
                    )
                )
                if operation is not None and version_id is not None
                else None
            )
            if page is None or branch is None or branch.page_id != page.id:
                skipped.append(
                    ContextReconciliationBulkDiscardSkipped(
                        review_item_id=review_item_id,
                        error_code="reconcile_target_invalid",
                    )
                )
                continue
            try:
                discard_reconciliation_branch(
                    session,
                    page=page,
                    branch=branch,
                    reason=reason,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    audit_service=self.audit_service,
                )
            except ReadValidationError as exc:
                skipped.append(
                    ContextReconciliationBulkDiscardSkipped(
                        review_item_id=review_item_id,
                        error_code=exc.error_code,
                    )
                )
                continue
            discarded.append(review_item_id)
        return ContextReconciliationBulkDiscardResult(
            discarded_review_item_ids=discarded,
            skipped=skipped,
        )

    def apply_list_visibility(
        self,
        *,
        session: Session,
        statement,
        workspace_id: UUID,
        audience_for: UUID | None,
        scope: str,
        actor_role: str | None,
    ):
        """Apply the review queue's fail-closed audience policy to a query."""
        resolved_scope = scope
        if actor_role is None:
            # This seam is consumed outside REST by activity/MCP/overlays. A
            # missing resolved role must never mean workspace-wide access.
            return statement.where(false()), resolved_scope
        if scope == "all" and not role_allows(actor_role, "editor"):
            resolved_scope = "to_me"
        if resolved_scope not in ("to_me", "from_me"):
            return statement, resolved_scope
        if audience_for is None:
            # A non-user principal has no personal queue. Never fall through to
            # the workspace-wide statement when an audience cannot be resolved.
            return statement.where(false()), resolved_scope
        return (
            self._apply_scope_predicate(
                session=session,
                statement=statement,
                workspace_id=workspace_id,
                principal=audience_for,
                scope=resolved_scope,
                principal_role=actor_role,
            ),
            resolved_scope,
        )

    def count_reviews(
        self,
        session: Session,
        workspace_id: UUID,
        risk_level: str | None = None,
        audience_for: UUID | None = None,
        actor_role: str | None = None,
    ) -> ReviewCountsPayload:
        risk_levels = _parse_csv_filter(risk_level)
        status_statement = (
            select(ReviewItem.status, func.count(ReviewItem.id))
            .where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.status.in_(
                    [
                        "pending",
                        CHANGES_REQUESTED_STATUS,
                        *RESOLVED_REVIEW_STATUSES,
                    ]
                ),
            )
            .group_by(ReviewItem.status)
        )
        if risk_levels:
            status_statement = status_statement.where(
                ReviewItem.risk_level.in_(risk_levels)
            )
        status_counts = dict(session.execute(status_statement).all())
        risk_statement = (
            select(ReviewItem.risk_level, func.count(ReviewItem.id))
            .where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.status.in_(
                    [
                        "pending",
                        CHANGES_REQUESTED_STATUS,
                        *RESOLVED_REVIEW_STATUSES,
                    ]
                ),
            )
            .group_by(ReviewItem.risk_level)
        )
        if risk_levels:
            risk_statement = risk_statement.where(
                ReviewItem.risk_level.in_(risk_levels)
            )
        risk_counts = dict(session.execute(risk_statement).all())
        # RO-1: status x risk matrix so each lifecycle card can show per-risk
        # chips. Same status set / risk filter as the totals above.
        status_risk_statement = (
            select(
                ReviewItem.status,
                ReviewItem.risk_level,
                func.count(ReviewItem.id),
            )
            .where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.status.in_(
                    [
                        "pending",
                        CHANGES_REQUESTED_STATUS,
                        *RESOLVED_REVIEW_STATUSES,
                    ]
                ),
            )
            .group_by(ReviewItem.status, ReviewItem.risk_level)
        )
        if risk_levels:
            status_risk_statement = status_risk_statement.where(
                ReviewItem.risk_level.in_(risk_levels)
            )
        status_by_risk: dict[str, dict[str, int]] = {
            "pending": {"high": 0, "medium": 0, "low": 0},
            "changes_requested": {"high": 0, "medium": 0, "low": 0},
            "resolved": {"high": 0, "medium": 0, "low": 0},
        }
        for status_val, risk_val, count in session.execute(status_risk_statement):
            if status_val in RESOLVED_REVIEW_STATUSES:
                bucket = "resolved"
            elif status_val == CHANGES_REQUESTED_STATUS:
                bucket = "changes_requested"
            elif status_val == "pending":
                bucket = "pending"
            else:
                continue
            if risk_val in ("high", "medium", "low"):
                status_by_risk[bucket][risk_val] += int(count)
        auto_approve_candidate_counts_by_risk = (
            self.count_auto_approve_candidates_by_risk(
                session=session,
                workspace_id=workspace_id,
            )
        )
        # Codex #6: workspace-wide review state (per-status / per-risk / the
        # status x risk matrix / auto-approve governance counts) is visible only
        # to editor-or-higher. Sub-editor (viewer) principals keep ONLY their
        # personal queue (to_me/from_me), mirroring the list_reviews scope clamp
        # — they must never receive workspace-wide review aggregates.
        if not role_allows(actor_role or "viewer", "editor"):
            status_counts = {}
            risk_counts = {}
            status_by_risk = {
                "pending": {"high": 0, "medium": 0, "low": 0},
                "changes_requested": {"high": 0, "medium": 0, "low": 0},
                "resolved": {"high": 0, "medium": 0, "low": 0},
            }
            auto_approve_candidate_counts_by_risk = {"low": 0, "medium": 0, "high": 0}
        return ReviewCountsPayload(
            pending=int(status_counts.get("pending", 0)),
            changes_requested=int(status_counts.get(CHANGES_REQUESTED_STATUS, 0)),
            resolved=sum(
                int(status_counts.get(status, 0)) for status in RESOLVED_REVIEW_STATUSES
            ),
            risk_counts=ReviewStatusRiskBreakdown(
                high=int(risk_counts.get("high", 0)),
                medium=int(risk_counts.get("medium", 0)),
                low=int(risk_counts.get("low", 0)),
            ),
            status_by_risk=ReviewStatusByRiskBreakdown(
                pending=ReviewStatusRiskBreakdown(**status_by_risk["pending"]),
                changes_requested=ReviewStatusRiskBreakdown(
                    **status_by_risk["changes_requested"]
                ),
                resolved=ReviewStatusRiskBreakdown(**status_by_risk["resolved"]),
            ),
            to_me=self._count_scope_reviews(
                session=session,
                workspace_id=workspace_id,
                risk_levels=risk_levels,
                audience_for=audience_for,
                scope="to_me",
                principal_role=actor_role,
            ),
            from_me=self._count_scope_reviews(
                session=session,
                workspace_id=workspace_id,
                risk_levels=risk_levels,
                audience_for=audience_for,
                scope="from_me",
                principal_role=actor_role,
            ),
            auto_approve_candidate_count=sum(
                auto_approve_candidate_counts_by_risk.values()
            ),
            auto_approve_candidate_counts_by_risk=ReviewStatusRiskBreakdown(
                **auto_approve_candidate_counts_by_risk
            ),
        )

    def sweep_auto_approve_reviews(
        self,
        session: Session,
        workspace_id: UUID,
        now: datetime | None = None,
    ) -> list[UUID]:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        policy = normalized_review_policy(workspace.review_policy)
        entries = auto_approve_by_risk_entries(policy)
        if not entries:
            return []
        current_time = now or datetime.now(UTC)
        delay_by_risk = {
            str(entry["risk_level"]): int(entry["delay_days"]) for entry in entries
        }
        candidate_predicates = [
            and_(
                ReviewItem.risk_level == risk_level,
                ReviewItem.created_at <= current_time - timedelta(days=delay_days),
            )
            for risk_level, delay_days in delay_by_risk.items()
        ]
        review_rows = list(
            session.execute(
                select(ReviewItem.id, ReviewItem.risk_level, ReviewItem.risk_signals)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.status == "pending",
                    ReviewItem.review_type != "context_reconciliation",
                    or_(*candidate_predicates),
                )
                .order_by(ReviewItem.created_at.asc(), ReviewItem.id.asc())
            )
        )
        approved: list[UUID] = []
        for review_id, risk_level, risk_signals in review_rows:
            review = get_in_workspace(
                session,
                ReviewItem,
                workspace_id=workspace_id,
                resource_id=review_id,
            )
            if review is None or review.review_type in {
                "agent_guide",
                "memory",
                "context_reconciliation",
            }:
                continue
            if (
                _requires_human_review(risk_signals)
                or _caller_requested_review(risk_signals)
                or _has_lint_error(risk_signals)
            ):
                continue
            delay_days = delay_by_risk[risk_level]
            self.apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(
                    decision_type="approve",
                    decision_reason=(
                        "auto_approved_by_policy: aged out "
                        f"({risk_level}, {delay_days}d)"
                    ),
                ),
                actor_kind="system",
                actor_id=str(default_actor_id()),
                actor_user_id=None,
                actor_role="owner",
                actor_provenance="internal",
            )
            approved.append(review_id)
        return approved

    def count_auto_approve_candidates(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> int:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        return sum(
            self.count_auto_approve_candidates_by_risk(
                session=session,
                workspace_id=workspace_id,
            ).values()
        )

    def count_auto_approve_candidates_by_risk(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> dict[str, int]:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        entries = auto_approve_by_risk_entries(
            normalized_review_policy(workspace.review_policy)
        )
        counts = {"low": 0, "medium": 0, "high": 0}
        if not entries:
            return counts
        now = datetime.now(UTC)
        candidate_predicates = [
            and_(
                ReviewItem.risk_level == str(entry["risk_level"]),
                ReviewItem.created_at <= now - timedelta(days=int(entry["delay_days"])),
            )
            for entry in entries
        ]
        rows = session.execute(
            select(ReviewItem.risk_level, ReviewItem.risk_signals).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.status == "pending",
                ReviewItem.review_type != "context_reconciliation",
                or_(*candidate_predicates),
            )
        ).all()
        for risk_level, risk_signals in rows:
            if (
                risk_level in counts
                and not _requires_human_review(risk_signals)
                and not _caller_requested_review(risk_signals)
                and not _has_lint_error(risk_signals)
            ):
                counts[risk_level] += 1
        return counts

    def bulk_review_ids(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        status: str | None = None,
        risk_level: str | None = None,
        audience_for: UUID | None = None,
        scope: str = "all",
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        limit: int | None = None,
    ) -> list[UUID]:
        statement = select(ReviewItem.id).where(ReviewItem.workspace_id == workspace_id)
        statuses = _expand_review_statuses(_parse_csv_filter(status))
        if statuses:
            statement = statement.where(ReviewItem.status.in_(statuses))
        risk_levels = _parse_csv_filter(risk_level)
        if risk_levels:
            statement = statement.where(ReviewItem.risk_level.in_(risk_levels))
        if scope in ("to_me", "from_me"):
            if audience_for is None:
                statement = statement.where(false())
            else:
                statement = self._apply_scope_predicate(
                    session=session,
                    statement=statement,
                    workspace_id=workspace_id,
                    principal=audience_for,
                    scope=scope,
                    principal_role=actor_role,
                )
        if not role_allows(actor_role or "none", "editor"):
            statement = statement.where(false())
        elif actor_user_id is not None:
            statement = statement.where(
                or_(
                    ReviewItem.status == "pending",
                    and_(
                        ReviewItem.status == CHANGES_REQUESTED_STATUS,
                        self._current_audience_predicate(
                            principal=actor_user_id, principal_role=actor_role
                        ),
                    ),
                )
            )
        else:
            statement = statement.where(ReviewItem.status == "pending")
        statement = statement.order_by(
            ReviewItem.created_at.desc(),
            ReviewItem.id.desc(),
        )
        if limit is not None:
            statement = statement.limit(max(1, min(limit, BULK_REVIEW_LIMIT)))
        return list(session.scalars(statement).all())

    def _current_audience_predicate(
        self, *, principal: UUID, principal_role: str | None
    ):
        latest_rc_id = self._latest_request_changes_subquery()
        latest_rc = aliased(ReviewDecision)
        return (
            select(latest_rc.id)
            .where(
                latest_rc.id == latest_rc_id,
                _decision_audience_predicate(
                    latest_rc, principal=principal, principal_role=principal_role
                ),
            )
            .exists()
        )

    def _count_scope_reviews(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        risk_levels: list[str],
        audience_for: UUID | None,
        scope: str,
        principal_role: str | None,
    ) -> int:
        if audience_for is None:
            return 0
        statement = select(func.count(ReviewItem.id.distinct())).where(
            ReviewItem.workspace_id == workspace_id,
            ReviewItem.status == CHANGES_REQUESTED_STATUS,
        )
        if risk_levels:
            statement = statement.where(ReviewItem.risk_level.in_(risk_levels))
        statement = self._apply_scope_predicate(
            session=session,
            statement=statement,
            workspace_id=workspace_id,
            principal=audience_for,
            scope=scope,
            principal_role=principal_role,
        )
        return int(session.scalar(statement) or 0)

    @staticmethod
    def _latest_request_changes_subquery():
        # Correlated scalar subquery: the id of the latest request_changes
        # decision for the outer ReviewItem, ordered created_at DESC, id DESC
        # (matches _detail / _current_audience_kind). Used to evaluate the
        # "current audience" predicate without materializing a list.
        inner = aliased(ReviewDecision)
        return (
            select(inner.id)
            .where(
                inner.review_item_id == ReviewItem.id,
                inner.decision_type == "request_changes",
            )
            .order_by(inner.created_at.desc(), inner.id.desc())
            .limit(1)
            .correlate(ReviewItem)
            .scalar_subquery()
        )

    def _apply_scope_predicate(
        self,
        *,
        session: Session,
        statement,
        workspace_id: UUID,
        principal: UUID,
        scope: str,
        principal_role: str | None,
    ):
        latest_rc_id = self._latest_request_changes_subquery()
        latest_rc = aliased(ReviewDecision)
        statement = statement.join(latest_rc, latest_rc.id == latest_rc_id).where(
            ReviewItem.status == CHANGES_REQUESTED_STATUS
        )
        if scope == "from_me":
            # Requests I authored (symmetric, trivial on the ledger).
            return statement.where(latest_rc.actor_id == principal)
        return statement.where(
            _decision_audience_predicate(
                latest_rc, principal=principal, principal_role=principal_role
            )
        )

    def get_review(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> ReviewItemDetail:
        review = self._require_review(session, workspace_id, review_item_id)
        # Codex #6: detail must not be a by-id bypass of the list_reviews scope
        # clamp. A sub-editor (viewer) may only retrieve a review routed to or
        # authored by them; otherwise its existence is not revealed. And even on
        # a visible item, workspace governance internals (risk_signals /
        # risk_policy_snapshot) are withheld from non-editors.
        is_editor = role_allows(actor_role or "viewer", "editor")
        if not is_editor and not self._review_visible_to_principal(
            session, review, actor_user_id, actor_role
        ):
            raise ReadNotFoundError("review not found")
        detail = self._detail(
            session,
            review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor=actor,
            include_decision_preflight=True,
            include_undo=True,
        )
        if not is_editor:
            detail = detail.model_copy(
                update={"risk_signals": None, "risk_policy_snapshot": None}
            )
        return detail

    def _review_visible_to_principal(
        self,
        session: Session,
        review: ReviewItem,
        principal: UUID | None,
        principal_role: str | None,
    ) -> bool:
        """True if ``review`` is in the principal's personal queue (to_me or
        from_me) — the same audience predicate list_reviews clamps viewers to."""
        if principal is None:
            return False
        for scope in ("to_me", "from_me"):
            statement = self._apply_scope_predicate(
                session=session,
                statement=select(ReviewItem.id).where(ReviewItem.id == review.id),
                workspace_id=review.workspace_id,
                principal=principal,
                scope=scope,
                principal_role=principal_role,
            )
            if session.scalar(statement) is not None:
                return True
        return False

    def can_decide_review(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
    ) -> bool:
        # L7: lightweight decidability check for the bulk dry-run preview. It
        # resolves the item and reuses _can_decide_review (role + status +
        # audience), avoiding the full ReviewItemDetail build (decisions,
        # revision payloads, evidence, change-set, chat-owner) that get_review
        # would do just to read .can_decide.
        review = self._require_review(session, workspace_id, review_item_id)
        return self._can_decide_review(
            session=session,
            review=review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        )

    def list_recent_decisions(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        limit: int = 10,
        audience_for: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> list[ReviewDecisionRecentItem]:
        # RD-1: most recent ReviewDecision rows (newest first). Decisions routed
        # privately to another user (requester / chat_owner) are hidden; broadly
        # visible ones (approve/reject = NULL audience, workspace_editors) and
        # those routed to the viewer are included.
        statement = (
            select(ReviewDecision, ReviewItem)
            .join(ReviewItem, ReviewDecision.review_item_id == ReviewItem.id)
            .where(ReviewItem.workspace_id == workspace_id)
            .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
            .limit(limit)
        )
        statement = statement.where(
            review_decision_audience_clause(actor_role, audience_for)
        )
        decision_rows = list(session.execute(statement))
        actor_ids = {
            decision.actor_id
            for decision, _review in decision_rows
            if decision.actor_type == "human" and decision.actor_id is not None
        }
        actor_names = (
            # .all() matters: a Result has .keys(), so dict() would treat it as
            # a mapping and subscript it instead of iterating the rows.
            dict(
                session.execute(
                    select(User.id, User.display_name).where(User.id.in_(actor_ids))
                ).all()
            )
            if actor_ids
            else {}
        )
        target_metadata = _recent_context_target_metadata(
            session,
            reviews=[review for _decision, review in decision_rows],
            workspace_id=workspace_id,
            actor=actor,
        )

        items: list[ReviewDecisionRecentItem] = []
        for decision, review in decision_rows:
            target = target_metadata.get(review.change_set_id, _EMPTY_TARGET_METADATA)
            target_title = target["target_title"]
            items.append(
                ReviewDecisionRecentItem(
                    decision_id=decision.id,
                    review_item_id=review.id,
                    review_target_title=target_title,
                    target_title=target_title,
                    target_slug=target["target_slug"],
                    target_version_no=target["target_version_no"],
                    decision_summary=review.decision_summary,
                    decision_summary_code=review.decision_summary_code,
                    decision_type=decision.decision_type,
                    actor_display_name=_hydrated_actor_display_name(
                        actor_kind=decision.actor_type,
                        display_name=actor_names.get(decision.actor_id),
                    ),
                    decision_reason=decision.decision_reason,
                    resulting_review_status=decision.resulting_review_status,
                    risk_level=review.risk_level,
                    created_at=decision.created_at,
                )
            )
        return items

    @staticmethod
    def _approval_supersession_context(
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet,
    ) -> tuple[ContextPage, ContextPageVersion, list[ReviewItem]] | None:
        """Resolve the version an approval publishes and the reviews it settles.

        Approval publishes the review's exact version: the latest draft or an
        earlier linear draft of the same chain (the changes up to it). Open
        reviews of that version and of its own draft ancestors are settled
        with it. Newer drafts and versions off this lineage (branched
        alternatives, other forks) keep their reviews, as does a review that
        also covers one of them.
        """
        from backend.services.context_lifecycle import (
            is_publishable_draft,
            live_draft_chain,
        )

        if (
            review.status not in {"pending", CHANGES_REQUESTED_STATUS}
            or review.review_type != "context_update"
        ):
            return None
        target = session.execute(
            select(ChangeOperation, ContextPage)
            .join(ContextPage, ContextPage.id == ChangeOperation.target_id)
            .where(
                ChangeOperation.change_set_id == change_set.id,
                ChangeOperation.target_type == "context_page",
            )
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
        ).first()
        if target is None:
            return None
        operation, page = target
        prefix = "context_page_version:"
        version_id = (
            parse_uuid_or_none(operation.diff_ref[len(prefix) :])
            if operation.diff_ref and operation.diff_ref.startswith(prefix)
            else None
        )
        version = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=change_set.workspace_id,
                resource_id=version_id,
            )
            if version_id
            else None
        )
        if version is None or not is_publishable_draft(session, page, version):
            return None
        chain_ids = [draft.id for draft in live_draft_chain(session, page)]
        settled_refs = [
            f"{prefix}{settled_id}"
            for settled_id in (
                chain_ids[chain_ids.index(version.id) :]
                if version.id in chain_ids
                else [version.id]
            )
        ]
        change_set_ids = set(
            session.scalars(
                select_in_workspace(
                    ChangeOperation,
                    workspace_id=page.workspace_id,
                    criteria=[
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id == page.id,
                        ChangeOperation.diff_ref.in_(settled_refs),
                    ],
                ).with_only_columns(ChangeOperation.change_set_id)
            ).all()
        )
        outside = (
            session.execute(
                select_in_workspace(
                    ChangeOperation,
                    workspace_id=page.workspace_id,
                    criteria=[
                        ChangeOperation.change_set_id.in_(change_set_ids),
                        ChangeOperation.diff_ref.startswith(prefix),
                        ChangeOperation.diff_ref.not_in(settled_refs),
                    ],
                ).with_only_columns(
                    ChangeOperation.change_set_id, ChangeOperation.diff_ref
                )
            ).all()
            if change_set_ids
            else []
        )
        if outside:
            # A review that also covers a version outside this version's own
            # history (a newer draft, a fork) is not settled by approving it.
            parents = dict(
                session.execute(
                    select_in_workspace(
                        ContextPageVersion,
                        workspace_id=page.workspace_id,
                        criteria=[ContextPageVersion.page_id == page.id],
                    ).with_only_columns(
                        ContextPageVersion.id, ContextPageVersion.parent_version_id
                    )
                ).all()
            )
            history: set[UUID] = set()
            cursor = page.current_published_version_id
            while cursor is not None and cursor not in history:
                history.add(cursor)
                cursor = parents.get(cursor)
            change_set_ids -= {
                change_set_id
                for change_set_id, diff_ref in outside
                if parse_uuid_or_none(diff_ref[len(prefix) :]) not in history
            }
        pending_reviews = (
            list(
                session.scalars(
                    select_in_workspace(
                        ReviewItem,
                        workspace_id=page.workspace_id,
                        criteria=[
                            ReviewItem.change_set_id.in_(change_set_ids),
                            ReviewItem.status.in_(
                                ("pending", CHANGES_REQUESTED_STATUS)
                            ),
                            ReviewItem.review_type == "context_update",
                            ReviewItem.id != review.id,
                        ],
                    )
                ).all()
            )
            if change_set_ids
            else []
        )
        return page, version, pending_reviews

    def _rejection_impact(
        self,
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet,
        *,
        lock: bool = True,
    ) -> "_RejectionImpact | None":
        """What rejecting a Context review does, for its preview and decision.

        The decision recomputes this under the page lock, so the fingerprint
        the preview showed proves the actor confirmed exactly this impact.
        """

        if review.review_type != "context_update":
            return None
        prefix = "context_page_version:"
        target = session.scalar(
            select_in_workspace(
                ChangeOperation,
                workspace_id=change_set.workspace_id,
                criteria=[
                    ChangeOperation.change_set_id == change_set.id,
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.diff_ref.startswith(prefix),
                ],
            )
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
            .limit(1)
        )
        version_id = (
            parse_uuid_or_none(target.diff_ref[len(prefix) :])
            if target is not None
            else None
        )
        if version_id is None:
            return None

        from backend.repositories.context_page_repo import get_page_by_id_for_update
        from backend.services.context_lifecycle import (
            live_draft_chain,
            plan_context_rejection,
        )

        page = (
            get_page_by_id_for_update(session, target.target_id)
            if lock
            else get_in_workspace(
                session,
                ContextPage,
                workspace_id=review.workspace_id,
                resource_id=target.target_id,
            )
        )
        if page is not None and lock:
            # ``with_for_update`` can return an already-loaded identity.  The
            # review evidence path commonly loaded this page earlier in the
            # transaction, so refresh after the lock rather than deciding from
            # a stale lifecycle/head snapshot.
            session.refresh(page)
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=change_set.workspace_id,
            resource_id=version_id,
        )
        if (
            page is None
            or page.workspace_id != change_set.workspace_id
            or version is None
            or version.page_id != page.id
        ):
            return None
        plan = plan_context_rejection(session, page, version)
        if plan is not None:
            # A review covering several drafts of the chain rejects from the
            # earliest of them, as approving it publishes the newest: its
            # later drafts were written on that one.
            refs = set(
                session.scalars(
                    select_in_workspace(
                        ChangeOperation,
                        workspace_id=change_set.workspace_id,
                        criteria=[
                            ChangeOperation.change_set_id == change_set.id,
                            ChangeOperation.target_type == "context_page",
                            ChangeOperation.target_id == page.id,
                            ChangeOperation.diff_ref.startswith(prefix),
                        ],
                    ).with_only_columns(ChangeOperation.diff_ref)
                ).all()
            )
            earliest = next(
                (
                    member
                    for member in reversed(live_draft_chain(session, page))
                    if f"{prefix}{member.id}" in refs
                ),
                version,
            )
            if earliest.id != version.id:
                version = earliest
                plan = plan_context_rejection(session, page, version)
        open_statuses = ("pending", CHANGES_REQUESTED_STATUS)
        if plan is None:
            # The version is off the live chain, so the rejection retires
            # nothing: it closes only the redundant reviews of that exact
            # version and leaves a claimed one to the reviewer already working
            # it. (Content replacement in update_draft supersedes page-wide.)
            siblings = self._open_reviews_within(
                session,
                page,
                {f"{prefix}{version.id}"},
                statuses=("pending",),
                exclude_id=review.id,
                unclaimed_only=True,
            )
            cascaded: list[ReviewItem] = []
        else:
            # The rejected draft and the later drafts written on it retire, so
            # every open review within them closes, claimed or not; one that
            # reviews only the rejected draft is its sibling. A review that
            # also covers a surviving version stays open.
            siblings = self._open_reviews_within(
                session,
                page,
                {f"{prefix}{version.id}"},
                statuses=open_statuses,
                exclude_id=review.id,
            )
            sibling_ids = {item.id for item in siblings}
            cascaded = [
                item
                for item in self._open_reviews_within(
                    session,
                    page,
                    {f"{prefix}{member.id}" for member in plan.retired},
                    statuses=open_statuses,
                    exclude_id=review.id,
                )
                if item.id not in sibling_ids
            ]
        destination = plan.destination if plan is not None else None
        restores_review = destination is not None and not session.scalar(
            select_in_workspace(
                ReviewItem,
                workspace_id=page.workspace_id,
                criteria=[
                    ReviewItem.change_set_id.in_(
                        select_in_workspace(
                            ChangeOperation,
                            workspace_id=page.workspace_id,
                            criteria=[
                                ChangeOperation.target_type == "context_page",
                                ChangeOperation.target_id == page.id,
                                ChangeOperation.diff_ref == f"{prefix}{destination.id}",
                            ],
                        ).with_only_columns(ChangeOperation.change_set_id)
                    ),
                    ReviewItem.review_type == "context_update",
                    ReviewItem.status.in_(open_statuses),
                    # The review being decided closes; it cannot keep it open.
                    ReviewItem.id != review.id,
                ],
            )
            .with_only_columns(ReviewItem.id)
            .limit(1)
        )
        fingerprint = sha256(
            json.dumps(
                {
                    "version": str(version.id),
                    "effect": plan.effect if plan is not None else "no_head_change",
                    "retired": [
                        str(member.id) for member in (plan.retired if plan else ())
                    ],
                    "destination": str(destination.id) if destination else None,
                    "siblings": sorted(str(item.id) for item in siblings),
                    "cascaded": sorted(str(item.id) for item in cascaded),
                    "restores_review": restores_review,
                    "metadata_kept": bool(plan and plan.metadata_kept),
                    "blocked": bool(plan and plan.blocked),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:32]
        return _RejectionImpact(
            page=page,
            version=version,
            plan=plan,
            siblings=siblings,
            cascaded=cascaded,
            restores_review=restores_review,
            fingerprint=fingerprint,
        )

    def _open_restored_draft_review(
        self,
        session: Session,
        *,
        page: ContextPage,
        destination: ContextPageVersion,
        rejected: ContextPageVersion,
        actor_kind: str,
        actor_id: str,
    ) -> ReviewItem:
        """Open a review for a draft that became the edit head again.

        Its own review closed when a later draft replaced it. With that later
        draft rejected, it is the head again and needs a live review before it
        can publish.
        """

        reason = (
            f"v{destination.version_no} became the edit head again after "
            f"v{rejected.version_no} was rejected"
        )
        risk = self._restored_draft_risk(session, page, destination)
        restore_set = ChangeSet(
            workspace_id=page.workspace_id,
            reason=reason,
            trigger_type="context_draft_restore",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="draft_created",
            risk_level=risk["risk_level"],
        )
        session.add(restore_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=page.workspace_id,
                change_set_id=restore_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="restore_after_rejection",
                diff_ref=f"context_page_version:{destination.id}",
            )
        )
        restored = ReviewItem(
            workspace_id=page.workspace_id,
            change_set_id=restore_set.id,
            review_type="context_update",
            status="pending",
            **risk,
            decision_summary="Draft restored after a later draft was rejected",
            decision_summary_code="context_draft_restored_after_rejection",
        )
        session.add(restored)
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=page.workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="review.opened_for_restored_draft",
            target_kind="review_item",
            target_id=restored.id,
            payload={
                "version_id": str(destination.id),
                "rejected_version_id": str(rejected.id),
                "change_set_id": str(restore_set.id),
            },
        )
        return restored

    @staticmethod
    def _restored_draft_risk(
        session: Session, page: ContextPage, destination: ContextPageVersion
    ) -> dict[str, object]:
        """Carry the restored version's own review evidence, never its child's.

        Several reviews may speak for the same version. Keep the greatest
        recorded risk and every mandatory human-review veto. Missing evidence
        cannot establish that automation is safe.
        """

        source_sets = select_in_workspace(
            ChangeOperation,
            workspace_id=page.workspace_id,
            criteria=[
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.diff_ref == f"context_page_version:{destination.id}",
            ],
        ).with_only_columns(ChangeOperation.change_set_id)
        source_match = ReviewItem.change_set_id.in_(source_sets)
        if destination.change_set_id is not None:
            source_match = or_(
                source_match, ReviewItem.change_set_id == destination.change_set_id
            )
        sources = list(
            session.scalars(
                select_in_workspace(
                    ReviewItem,
                    workspace_id=page.workspace_id,
                    criteria=[
                        source_match,
                        ReviewItem.review_type == "context_update",
                    ],
                ).order_by(ReviewItem.created_at, ReviewItem.id)
            ).all()
        )

        def complete(source: ReviewItem) -> bool:
            return (
                source.risk_level in RISK_ORDER
                and bool(source.risk_policy_version)
                and bool(source.risk_resolution_summary)
                and isinstance(source.risk_signals, dict)
                and isinstance(source.risk_signals.get("human_review_required"), bool)
                and isinstance(source.risk_policy_snapshot, dict)
                and {
                    "preset",
                    "policy_version",
                    "default",
                    "review_required_from_risk",
                }
                <= source.risk_policy_snapshot.keys()
            )

        source = max(
            sources,
            key=lambda item: (RISK_ORDER.get(item.risk_level, 2), complete(item)),
            default=None,
        )
        signals = (
            deepcopy(source.risk_signals)
            if source is not None and isinstance(source.risk_signals, dict)
            else {}
        )
        for flag in ("human_review_required", "caller_needs_review", "lint_error"):
            signals[flag] = any(
                bool((item.risk_signals or {}).get(flag))
                for item in sources
                if isinstance(item.risk_signals, dict)
            )
        if not sources or any(not complete(item) for item in sources):
            signals["human_review_required"] = True
        return {
            "risk_level": source.risk_level
            if source is not None and source.risk_level in RISK_ORDER
            else "high",
            "risk_policy_version": source.risk_policy_version if source else None,
            "risk_resolution_summary": (
                source.risk_resolution_summary if source else None
            ),
            "risk_signals": signals,
            "risk_policy_snapshot": (
                deepcopy(source.risk_policy_snapshot)
                if source is not None and isinstance(source.risk_policy_snapshot, dict)
                else None
            ),
        }

    @staticmethod
    def _open_reviews_within(
        session: Session,
        page: ContextPage,
        refs: set[str],
        *,
        statuses: tuple[str, ...],
        exclude_id: UUID,
        unclaimed_only: bool = False,
    ) -> list[ReviewItem]:
        """Open Context reviews whose reviewed versions all lie within ``refs``."""

        if not refs:
            return []
        change_set_ids = set(
            session.scalars(
                select_in_workspace(
                    ChangeOperation,
                    workspace_id=page.workspace_id,
                    criteria=[
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id == page.id,
                        ChangeOperation.diff_ref.in_(refs),
                    ],
                ).with_only_columns(ChangeOperation.change_set_id)
            ).all()
        )
        if change_set_ids:
            change_set_ids -= set(
                session.scalars(
                    select_in_workspace(
                        ChangeOperation,
                        workspace_id=page.workspace_id,
                        criteria=[
                            ChangeOperation.change_set_id.in_(change_set_ids),
                            ChangeOperation.diff_ref.startswith(
                                "context_page_version:"
                            ),
                            ChangeOperation.diff_ref.not_in(refs),
                        ],
                    ).with_only_columns(ChangeOperation.change_set_id)
                ).all()
            )
        if not change_set_ids:
            return []
        criteria = [
            ReviewItem.change_set_id.in_(change_set_ids),
            ReviewItem.review_type == "context_update",
            ReviewItem.status.in_(statuses),
            ReviewItem.id != exclude_id,
        ]
        if unclaimed_only:
            criteria += [
                ReviewItem.claimed_by.is_(None),
                ReviewItem.claimed_at.is_(None),
            ]
        return list(
            session.scalars(
                select_in_workspace(
                    ReviewItem, workspace_id=page.workspace_id, criteria=criteria
                ).order_by(ReviewItem.id)
            ).all()
        )

    def _action_unavailable_reason(
        self,
        session: Session,
        review: ReviewItem,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
        claimant_id: UUID | None,
    ) -> str | None:
        """Common gates without changing the historical ``can_decide`` contract."""
        if review.review_type == "context_reconciliation":
            return "reconciliation_required"
        if review.status not in {"pending", CHANGES_REQUESTED_STATUS}:
            return "review_resolved"
        if not role_allows(actor_role or "none", "editor"):
            return "insufficient_role"
        if not self._can_decide_review(
            session=session,
            review=review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        ):
            return "not_in_audience"
        if self._claim_is_live(review) and review.claimed_by != claimant_id:
            return "claim_conflict"
        return None

    def _approval_unavailable_reason(
        self,
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet | None,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
        actor_id: str | None,
        actor_kind: str,
        actor_provenance: str,
        publication_checks: bool = True,
    ) -> str | None:
        """Read-only approval gates used by both previews and actual decisions.

        The publication service still owns its transaction-time media, topology
        and objective-floor checks. This early projection cannot waive them.
        ``publication_checks=False`` skips the body scans a decision repeats in
        the publication writer, which reports each refusal's own error code.
        """
        common = self._action_unavailable_reason(
            session,
            review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            claimant_id=parse_uuid_or_none(actor_id) if actor_id else actor_user_id,
        )
        if common is not None:
            return common
        if actor_provenance not in {"browser_session", "internal"}:
            return "human_approval_required"
        if change_set is None or change_set.workspace_id != review.workspace_id:
            return "change_set_unavailable"
        if review.review_type == "context_update":
            context = self._approval_supersession_context(session, review, change_set)
            if context is None:
                return "stale_candidate"
            page, version, siblings = context
            from backend.context_media.domain import (
                ActorIdentity,
                ContextMediaError,
                validate_markdown_media_policy,
            )
            from backend.services.context_lifecycle import (
                assert_context_publish_candidate,
            )
            from backend.services.context_media_legacy_cutover import (
                require_no_visible_legacy_media,
            )
            from backend.services.lint import publish_floor_findings

            try:
                assert_context_publish_candidate(
                    session,
                    page=page,
                    version=version,
                    require_latest_draft=True,
                    allow_chain_ancestor=True,
                )
            except ReadServiceError:
                return "stale_candidate"
            if not publication_checks:
                return None
            if publish_floor_findings(
                version.body_md, registry=self.body_preparer.registry
            ):
                return "publication_blocked"
            try:
                require_no_visible_legacy_media(
                    workspace_id=review.workspace_id,
                    body_markdown=version.body_md,
                )
                references = validate_markdown_media_policy(version.body_md)
                if references:
                    if self.context_media_repository_factory is None:
                        return "publication_blocked"
                    repository = self.context_media_repository_factory(session)
                    for asset_id in set(references):
                        asset = repository.get_asset(review.workspace_id, asset_id)
                        if (
                            asset is None
                            or asset.page_id != page.id
                            or str(asset.association_state) != "active"
                            or repository.resolve_version_content(
                                workspace_id=review.workspace_id,
                                page_id=page.id,
                                page_version_id=version.id,
                                asset_id=asset_id,
                                actor=ActorIdentity(
                                    kind=actor_kind,
                                    id=actor_id or str(actor_user_id or "internal"),
                                ),
                            )
                            is None
                        ):
                            return "publication_blocked"
            except (ReadServiceError, ContextMediaError):
                return "publication_blocked"
            # All pending exact-version siblings will be superseded by approval.
            # A rejected review of these bytes is not part of that effect and
            # continues to block publication, as ContextWriteService requires.
            from backend.services.context_write_service import ContextWriteService

            settled_ids = {review.id, *(sibling.id for sibling in siblings)}
            if any(
                candidate.id not in settled_ids
                and candidate.status not in {"approved", "superseded"}
                for candidate in ContextWriteService._reviews_for_version(
                    session, version
                )
            ):
                return "publication_blocked"
        elif review.review_type == "agent_page_activation":
            operation = session.scalar(
                select_in_workspace(
                    ChangeOperation,
                    workspace_id=review.workspace_id,
                    criteria=[
                        ChangeOperation.change_set_id == change_set.id,
                        ChangeOperation.target_type == "agent_page",
                    ],
                ).order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
            )
            version_id = (
                operation_version_id(operation, "agent_page") if operation else None
            )
            page = (
                get_in_workspace(
                    session,
                    AgentPage,
                    workspace_id=review.workspace_id,
                    resource_id=operation.target_id,
                )
                if operation
                else None
            )
            version = (
                get_in_workspace(
                    session,
                    AgentPageVersion,
                    workspace_id=review.workspace_id,
                    resource_id=version_id,
                )
                if version_id
                else None
            )
            if page is None or version is None or version.agent_page_id != page.id:
                return "candidate_unavailable"
            if version.version_status != "draft":
                return "stale_candidate"
            from backend.services.agent_page_service import AgentPageService

            proposed = AgentPageService._proposed_page_meta(version)
            try:
                AgentPageService._ensure_activation_allowed(
                    page=page,
                    proposed_category=proposed.get("category", page.category),
                    actor_kind=actor_kind,
                    actor_role=actor_role or "none",
                )
            except ReadServiceError:
                return "activation_not_allowed"
        return None

    def approval_preflight(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        *,
        actor: ActorContext,
        actor_role: str,
        lock: bool = False,
        publication_checks: bool = True,
    ) -> ReviewApprovalPreflight:
        """Snapshot an approval and its full internal same-candidate effect.

        ``lock=True`` takes the same page -> review locks as apply_decision;
        callers compare their signed impact fingerprint before any mutation.
        Executions pass ``publication_checks=False``: the publication writer
        re-runs the body scans in the same transaction.
        """
        actor = replace(
            actor,
            workspace_id=workspace_id,
            workspace_role=actor_role,
            role=actor_role,
        )
        if lock:
            self._lock_context_page_for_review_update(
                session,
                workspace_id=workspace_id,
                review_item_id=review_item_id,
            )
            review = self._require_review_for_update(
                session, workspace_id, review_item_id
            )
        else:
            review = self._require_review(session, workspace_id, review_item_id)
        if not role_allows(
            actor_role, "editor"
        ) and not self._review_visible_to_principal(
            session,
            review,
            actor.user_id,
            actor_role,
        ):
            raise ReadNotFoundError("review not found")
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=workspace_id,
            resource_id=review.change_set_id,
        )
        reason = self._approval_unavailable_reason(
            session,
            review,
            change_set,
            actor_user_id=actor.user_id,
            actor_role=actor_role,
            actor_id=actor.actor_id,
            actor_kind=actor.actor_kind,
            actor_provenance=actor.provenance,
            publication_checks=publication_checks,
        )
        context = (
            self._approval_supersession_context(
                session,
                review,
                change_set,
            )
            if change_set is not None and reason is None
            else None
        )
        page, version, siblings = context if context is not None else (None, None, [])
        if lock and siblings:
            # Claim/routing writes lock only their own review row. Hold every
            # affected row as well as the page while the caller compares the
            # signed impact and applies the decision in this transaction.
            siblings = list(
                session.scalars(
                    select_in_workspace_by_ids(
                        ReviewItem,
                        workspace_id=workspace_id,
                        resource_ids=[sibling.id for sibling in siblings],
                    )
                    .order_by(ReviewItem.id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                ).all()
            )
        operations = session.scalars(
            select_in_workspace(
                ChangeOperation,
                workspace_id=workspace_id,
                criteria=[ChangeOperation.change_set_id == review.change_set_id],
            ).order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
        ).all()
        primary = next(
            (
                operation
                for operation in operations
                if (
                    review.review_type == "context_update"
                    and operation.target_type
                    in {"context_page", "context_page_version"}
                )
                or (
                    review.review_type == "agent_page_activation"
                    and operation.target_type in {"agent_page", "agent_page_version"}
                )
            ),
            None,
        )
        if context is not None:
            primary = next(
                (
                    operation
                    for operation in operations
                    if operation.target_type == "context_page"
                    and operation.target_id == page.id
                    and operation_version_id(operation, "context_page") == version.id
                ),
                primary,
            )
        metadata = dict(_EMPTY_TARGET_METADATA)
        if primary is not None:
            document, content = project_review_target(
                session,
                primary,
                workspace_id,
                actor,
                allow_archived_context=self._can_decide_review(
                    session=session,
                    review=review,
                    actor_user_id=actor.user_id,
                    actor_role=actor_role,
                ),
            )
            if document is not None:
                metadata = {
                    "target_title": document.title,
                    "target_slug": document.slug,
                    "target_version_no": content.candidate_version_no,
                }
            if context is None and primary.target_type in {
                "context_page",
                "context_page_version",
            }:
                candidate_id = operation_version_id(primary, "context_page")
                version = (
                    get_in_workspace(
                        session,
                        ContextPageVersion,
                        workspace_id=workspace_id,
                        resource_id=candidate_id,
                    )
                    if candidate_id
                    else None
                )
                raw_page_id = (
                    primary.target_id
                    if primary.target_type == "context_page"
                    else version.page_id
                    if version
                    else None
                )
                page = (
                    get_in_workspace(
                        session,
                        ContextPage,
                        workspace_id=workspace_id,
                        resource_id=raw_page_id,
                    )
                    if raw_page_id
                    else None
                )
                if version is not None and (page is None or version.page_id != page.id):
                    version = None
            elif primary.target_type in {"agent_page", "agent_page_version"}:
                candidate_id = operation_version_id(primary, "agent_page")
                version = (
                    get_in_workspace(
                        session,
                        AgentPageVersion,
                        workspace_id=workspace_id,
                        resource_id=candidate_id,
                    )
                    if candidate_id
                    else None
                )
                raw_page_id = (
                    primary.target_id
                    if primary.target_type == "agent_page"
                    else version.agent_page_id
                    if version
                    else None
                )
                page = (
                    get_in_workspace(
                        session,
                        AgentPage,
                        workspace_id=workspace_id,
                        resource_id=raw_page_id,
                    )
                    if raw_page_id
                    else None
                )
                if version is not None and (
                    page is None or version.agent_page_id != page.id
                ):
                    version = None
        elif operations:
            targets = [
                project_review_target(session, op, workspace_id, actor)
                for op in operations
            ]
            documents = {
                (document.document_kind, document.document_id)
                for document, _ in targets
                if document is not None and document.document_id
            }
            if len(documents) == 1 and all(
                doc is not None and doc.document_id for doc, _ in targets
            ):
                document, content = targets[0]
                metadata = {
                    "target_title": document.title,
                    "target_slug": document.slug,
                    "target_version_no": content.candidate_version_no,
                }
        visible_ids = tuple(
            sorted(
                (sibling.id for sibling in siblings)
                if metadata["target_title"] is not None
                else (),
                key=str,
            )
        )
        if isinstance(page, ContextPage):
            page_state = [
                str(page.id),
                str(page.current_version_id),
                str(page.current_published_version_id),
                str(page.latest_draft_version_id),
                page.lifecycle_status,
            ]
            version_state = (
                [
                    str(version.id),
                    version.version_status,
                    version.graph_status,
                    str(version.parent_version_id),
                    version.content_hash,
                    version.editing_revision,
                    str(version.last_saved_at),
                ]
                if isinstance(version, ContextPageVersion)
                else None
            )
        elif isinstance(page, AgentPage):
            page_state = [
                str(page.id),
                str(page.current_active_version_id),
                str(page.latest_draft_version_id),
                page.lifecycle_status,
                page.category,
                page.sensitivity,
                str(page.updated_at),
            ]
            version_state = (
                [
                    str(version.id),
                    version.version_status,
                    sha256(version.body_markdown.encode()).hexdigest(),
                    version.frontmatter,
                ]
                if isinstance(version, AgentPageVersion)
                else None
            )
        else:
            page_state = version_state = None
        context_metadata = None
        if isinstance(page, ContextPage):
            context_metadata = self._context_approval_metadata_fingerprint(
                session,
                page,
                version if isinstance(version, ContextPageVersion) else None,
            )
        fingerprint = {
            "workspace_id": str(workspace_id),
            "review_id": str(review.id),
            "role": actor_role,
            "principal": str(actor.user_id),
            "actor_id": actor.actor_id,
            "provenance": actor.provenance,
            "review_state": [
                review.status,
                str(review.updated_at),
                str(review.claimed_by),
                str(review.claimed_at),
            ],
            "change_state": [change_set.status, str(change_set.updated_at)]
            if change_set
            else None,
            "operations": [
                [
                    str(op.id),
                    op.target_type,
                    str(op.target_id),
                    op.operation,
                    op.diff_ref,
                    op.diff_summary,
                ]
                for op in operations
            ],
            "page_state": page_state,
            "version_state": version_state,
            "context_metadata": context_metadata,
            "sibling_state": sorted(
                [
                    [
                        str(sibling.id),
                        sibling.status,
                        str(sibling.updated_at),
                        str(sibling.claimed_by),
                        str(sibling.claimed_at),
                    ]
                    for sibling in siblings
                ]
            ),
            "eligibility": reason,
        }
        return ReviewApprovalPreflight(
            eligible=reason is None,
            reason_code=reason,
            reason=APPROVAL_UNAVAILABLE_REASONS.get(reason) if reason else None,
            page_id=page.id if page else None,
            version_id=version.id if version else None,
            superseded_review_ids=tuple(
                sorted((sibling.id for sibling in siblings), key=str)
            ),
            visible_superseded_review_ids=visible_ids,
            target_title=metadata["target_title"],
            target_version_no=metadata["target_version_no"],
            fingerprint_parts=fingerprint,
        )

    @staticmethod
    def _context_approval_metadata_fingerprint(
        session: Session,
        page: ContextPage,
        version: ContextPageVersion | None,
    ) -> dict[str, object]:
        """Bind the metadata that the exact candidate will actually publish.

        A body hash does not cover canonical title/tags/placement or metadata
        patches staged in a different change set. Publication merges every
        patch bound to this version, so a preview must cover that same ledger,
        not just the selected review's own operations.
        """
        metadata: dict[str, object] = {
            "canonical": {
                "title": page.title,
                "slug": page.slug,
                "page_type": page.page_type,
                "tags": list(page.tags or []),
                "aliases": list(page.aliases or []),
                "placement_parent_node_id": (
                    str(page.placement_parent_node_id)
                    if page.placement_parent_node_id is not None
                    else None
                ),
                "placement_reason": page.placement_reason,
                "placement_confidence": page.placement_confidence,
                "updated_at": str(page.updated_at),
            },
        }
        if version is None:
            return metadata
        from backend.services.page_service import (
            effective_metadata_for_payload,
            pending_page_metadata_patch,
        )

        # Use the publication writer's canonical merge rather than inventing
        # another JSON-patch ordering or ignoring patches from sibling changes.
        metadata["pending_patch"] = pending_page_metadata_patch(
            session,
            page=page,
            version_id=version.id,
        )
        title, page_type, tags, aliases = effective_metadata_for_payload(
            session,
            page,
            version,
        )
        metadata["effective_candidate"] = {
            "title": title,
            "page_type": page_type,
            "tags": tags,
            "aliases": aliases,
        }
        patches = session.scalars(
            select_in_workspace(
                ChangeOperation,
                workspace_id=page.workspace_id,
                criteria=[
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.operation == "metadata_patch",
                    ChangeOperation.diff_ref == f"context_page_version:{version.id}",
                ],
            )
            .order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
            .execution_options(populate_existing=True)
        ).all()
        metadata["staged_operations"] = [
            [
                str(patch.id),
                str(patch.change_set_id),
                str(patch.created_at),
                patch.diff_ref,
                patch.diff_summary,
            ]
            for patch in patches
        ]
        return metadata

    def apply_decision(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        request: ReviewDecisionRequest,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> ReviewItemDetail:
        with record_decision_effect(session) as recorder:
            return self._decide(
                session,
                workspace_id,
                review_item_id,
                request,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                actor=actor,
                recorder=recorder,
            )

    def _decide(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        request: ReviewDecisionRequest,
        *,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        actor_user_id: UUID | None,
        actor_role: str | None,
        actor: ActorContext | None,
        recorder: DecisionEffectRecorder,
    ) -> ReviewItemDetail:
        validate_request_inputs(request)
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        # Context writers lock page -> review when replacing reviewed bytes.
        # Take the same order here before the review row lock so approval or a
        # revision racing browser autosave cannot deadlock on PostgreSQL.
        locked_page = self._lock_context_page_for_review_update(
            session,
            workspace_id=workspace_id,
            review_item_id=review_item_id,
        )
        if isinstance(locked_page, ContextPage):
            recorder.page_id = locked_page.id
        review = self._require_review_for_update(session, workspace_id, review_item_id)
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=workspace_id,
            resource_id=review.change_set_id,
        )
        if request.decision_type == "approve" and actor_provenance not in {
            "browser_session",
            "internal",
        }:
            raise ReadValidationError("AI-authenticated actors cannot approve reviews")
        if change_set is None or change_set.workspace_id != workspace_id:
            raise ReadNotFoundError("change set not found")

        principal = actor_user_id or parse_uuid_or_none(actor_id)
        if request.decision_id is not None:
            # A receipt is a read of this actor's earlier decision, even if a
            # later request_changes round has a different audience or claim.
            # Keep the current editor/provenance gates, but do not re-run the
            # mutation's pending-state gate or move any current state on replay.
            if not role_allows(actor_role or "none", "editor"):
                raise ReadValidationError("this actor cannot decide this review")
            actor_uuid = parse_uuid_or_none(actor_id)
            if actor_uuid is None:
                raise ReadValidationError(
                    "decision_id requires an authenticated UUID actor"
                )
            existing = get_in_workspace(
                session,
                ReviewDecision,
                workspace_id=workspace_id,
                resource_id=request.decision_id,
            )
            if existing is not None:
                if not self._decision_matches_request(
                    existing,
                    workspace_id=workspace_id,
                    review_item_id=review.id,
                    actor_kind=actor_kind,
                    actor_id=actor_uuid,
                    request=request,
                ):
                    raise ReviewDecisionIdConflictError()
                receipt = self._detail(
                    session,
                    review,
                    actor_user_id=principal,
                    actor_role=actor_role,
                    actor=actor,
                    include_undo=actor_kind == "human",
                )
                if existing.decision_type == "approve":
                    receipt.published_version_id = next((
                        item.content.candidate_version_id
                        for item in receipt.evidence.operations
                        if item.document is not None
                        and item.document.document_kind == "context"
                        and item.content is not None
                        and item.content.candidate_version_id is not None
                    ), None)
                return receipt

        # A retry of the exact terminal rejection is a read of the original
        # result, not a second state transition. This guard runs under the same
        # review-row lock and deliberately precedes the normal pending-status
        # decision gate below.
        if (
            request.decision_id is None
            and request.decision_type == "reject"
            and review.status == "rejected"
            and role_allows(actor_role or "none", "editor")
            and session.scalar(
                select(ReviewDecision.id)
                .where(
                    ReviewDecision.review_item_id == review.id,
                    ReviewDecision.decision_type == "reject",
                    ReviewDecision.resulting_review_status == "rejected",
                )
                .limit(1)
            )
            is not None
        ):
            return self._detail(
                session,
                review,
                actor_user_id=actor_user_id or parse_uuid_or_none(actor_id),
                actor_role=actor_role,
                actor=actor,
            )

        if review.status not in {"pending", CHANGES_REQUESTED_STATUS}:
            raise ReadValidationError("cannot decide a review that is already resolved")

        # A live soft-claim (D1) held by ANOTHER actor blocks this decision (P1):
        # the holder is actively working the item, so a non-holder cannot
        # approve/reject/reroute it out from under them. The holder's own
        # decision proceeds (and clears the claim below); an expired claim does
        # not block.
        if self._claim_is_live(review) and review.claimed_by != parse_uuid_or_none(
            actor_id
        ):
            raise ReviewAlreadyClaimedError(
                "this review is being worked by another collaborator; it must "
                "be released before you can decide it"
            )
        if not self._can_decide_review(
            session=session,
            review=review,
            actor_user_id=principal,
            actor_role=actor_role,
        ):
            raise ReadValidationError("this actor cannot decide this review")

        if request.decision_type == "approve":
            # Body scans stay with the publication writer below, which refuses
            # with each defect's own code (lint warnings, legacy media 409, ...).
            unavailable = self._approval_unavailable_reason(
                session,
                review,
                change_set,
                actor_user_id=principal,
                actor_role=actor_role,
                actor_id=actor_id,
                actor_kind=actor_kind,
                actor_provenance=actor_provenance,
                publication_checks=False,
            )
            if (
                unavailable == "stale_candidate"
                and review.review_type == "context_update"
            ):
                raise ReadValidationError(
                    "cannot approve a stale or superseded Context review"
                )
            if unavailable == "activation_not_allowed":
                raise ScopeDeniedError(APPROVAL_UNAVAILABLE_REASONS[unavailable])
            if unavailable is not None:
                raise RejectedByPolicyError(
                    APPROVAL_UNAVAILABLE_REASONS[unavailable],
                    error_code=unavailable,
                )

        approval_context = None
        if (
            request.decision_type == "approve"
            and review.review_type == "context_update"
        ):
            approval_context = self._approval_supersession_context(
                session,
                review,
                change_set,
            )
            if approval_context is None:
                raise ReadValidationError(
                    "cannot approve a stale or superseded Context review"
                )
        rejection_impact = (
            self._rejection_impact(session, review, change_set)
            if request.decision_type == "reject"
            else None
        )
        if (
            rejection_impact is not None
            and rejection_impact.plan is not None
            and rejection_impact.plan.blocked
        ):
            raise ReviewRejectBlockedError()
        if request.expected_impact_fingerprint is not None and (
            rejection_impact is None
            or rejection_impact.fingerprint != request.expected_impact_fingerprint
        ):
            raise ReviewDecisionImpactChangedError()
        audience_kind, audience_user_id = self._resolve_audience(
            session=session,
            request=request,
            change_set=change_set,
            actor_id=actor_id,
        )
        decision = ReviewDecision(
            workspace_id=review.workspace_id,
            review_item_id=review.id,
            decision_type=request.decision_type,
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            decision_reason=request.decision_reason,
            audience_kind=audience_kind,
            audience_user_id=audience_user_id,
            resulting_review_status=REVIEW_STATUS_BY_DECISION[request.decision_type],
            resulting_change_set_status=CHANGE_SET_STATUS_BY_DECISION[
                request.decision_type
            ],
        )
        if request.decision_id is not None:
            decision.id = request.decision_id
            try:
                # The page/review locks serialize retries of this review. The
                # ledger's primary key also arbitrates a concurrent reuse on a
                # DIFFERENT review before either request changes review state.
                with session.begin_nested():
                    session.add(decision)
                    session.flush()
            except IntegrityError as exc:
                if not decision_id_conflict(exc):
                    raise
                raise ReviewDecisionIdConflictError() from exc
        else:
            session.add(decision)
        review.status = REVIEW_STATUS_BY_DECISION[request.decision_type]
        change_set.status = CHANGE_SET_STATUS_BY_DECISION[request.decision_type]
        # Any new decision ends the current work cycle (resolution or a fresh
        # request_changes round), so the soft-claim (D1) is released.
        review.claimed_by = None
        review.claimed_at = None
        session.flush()

        reject_effect = "no_head_change"
        rejection_record: dict[str, object] = {}
        if request.decision_type == "reject" and rejection_impact is not None:
            page = rejection_impact.page
            version = rejection_impact.version
            plan = rejection_impact.plan
            if plan is not None:
                from backend.services.context_lifecycle import (
                    apply_context_rejection,
                )
                from backend.services.context_write_service import ContextWriteService
                from backend.services.page_service import sync_page_projections
                from backend.services.tree_service import (
                    ensure_primary_tree_placement,
                )

                resulting_version, restored_fields = apply_context_rejection(
                    session, page, plan
                )
                reject_effect = plan.effect
                if restored_fields & {"title", "placement_parent_node_id"}:
                    ensure_primary_tree_placement(
                        session=session,
                        workspace_id=workspace_id,
                        target_kind="context_page",
                        target_id=page.id,
                        title=page.title,
                        slug=page.slug,
                        section="context",
                        parent_id=page.placement_parent_node_id,
                        default_parent_slug="inbox",
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        can_have_children=True,
                    )
                # Pointer state is the source of truth for both projections and
                # indexing. Refresh them immediately after the move, before any
                # other rejection bookkeeping in this transaction.
                sync_page_projections(session, page=page, version=resulting_version)
                ContextWriteService(
                    audit_service=self.audit_service,
                    body_preparer=self.body_preparer,
                    context_media_repository_factory=(
                        self.context_media_repository_factory
                    ),
                ).enqueue_head_reindex(
                    session,
                    page=page,
                    transition_version=version,
                    decision_id=decision.id,
                )
                rejection_record["retired_version_ids"] = [
                    str(member.id) for member in plan.retired
                ]
            for sibling in rejection_impact.siblings:
                supersede_review_with_lineage(
                    session=session,
                    audit_service=self.audit_service,
                    workspace_id=workspace_id,
                    review=sibling,
                    new_change_set_id=change_set.id,
                    new_review_item_id=review.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    audit_action="review.superseded_by_rejected_candidate",
                    payload={
                        "rejected_version_id": str(version.id),
                        "rejected_version_no": version.version_no,
                    },
                )
            for later in rejection_impact.cascaded:
                supersede_review_with_lineage(
                    session=session,
                    audit_service=self.audit_service,
                    workspace_id=workspace_id,
                    review=later,
                    new_change_set_id=change_set.id,
                    new_review_item_id=review.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    audit_action="review.superseded_by_rejected_ancestor",
                    payload={
                        "rejected_version_id": str(version.id),
                        "rejected_version_no": version.version_no,
                    },
                )
            if rejection_impact.cascaded:
                rejection_record["cascaded_review_ids"] = [
                    str(later.id) for later in rejection_impact.cascaded
                ]
            if (
                rejection_impact.restores_review
                and plan is not None
                and plan.destination is not None
            ):
                restored = self._open_restored_draft_review(
                    session,
                    page=page,
                    destination=plan.destination,
                    rejected=version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                rejection_record["restored_review_item_id"] = str(restored.id)
        published_version_id = None
        restore_version_id = (
            rejection_impact.version.id if rejection_impact is not None else None
        )
        superseded_review_count = 0
        if approval_context is not None and actor_kind == "human":
            page, version, pending_reviews = approval_context
            for pending_review in pending_reviews:
                supersede_review_with_lineage(
                    session=session,
                    audit_service=self.audit_service,
                    workspace_id=workspace_id,
                    review=pending_review,
                    new_change_set_id=change_set.id,
                    new_review_item_id=review.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    audit_action="review.superseded_by_approved_publish",
                    payload={"published_version_no": version.version_no},
                )
            superseded_review_count = len(pending_reviews)
            # Deferred import keeps review_service <-> page write imports
            # acyclic while reusing the sole published-pointer writer.
            from backend.services.context_write_service import ContextWriteService

            published = ContextWriteService(
                audit_service=self.audit_service,
                body_preparer=self.body_preparer,
                context_media_repository_factory=self.context_media_repository_factory,
            ).publish_draft(
                session=session,
                workspace_id=workspace_id,
                page_id=page.id,
                request=PublishContextPageRequest(
                    version_id=version.id,
                    reason="approved through Context review",
                ),
                actor_kind=actor_kind,
                actor_id=actor_id,
                publish_chain_ancestor=True,
                review_decision_id=decision.id,
            )
            published_version_id = published.published_version_id
            restore_version_id = published.previous_published_version_id
        if (
            request.decision_type == "approve"
            and review.review_type == "agent_page_activation"
        ):
            operation = session.scalar(
                select(ChangeOperation)
                .where(
                    ChangeOperation.change_set_id == change_set.id,
                    ChangeOperation.target_type == "agent_page",
                )
                .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
            )
            prefix = "agent_page_version:"
            version_id = (
                parse_uuid_or_none(operation.diff_ref[len(prefix) :])
                if operation is not None
                and operation.diff_ref
                and operation.diff_ref.startswith(prefix)
                else None
            )
            if operation is None or version_id is None:
                raise ReadValidationError("AgentPage activation review has no draft")
            from backend.services.agent_page_service import AgentPageService

            AgentPageService(audit_service=self.audit_service).activate(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=operation.target_id,
                version_id=version_id,
                reason="approved through AgentPage review",
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_role=actor_role or "none",
            )
        # Emitted last so the effect record holds everything the decision did,
        # publication included. Only Context reviews can be undone.
        session.flush()
        effect = (
            recorder.effect(restore_version_id=restore_version_id)
            if review.review_type == "context_update"
            and isinstance(locked_page, ContextPage)
            else None
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="review.decision",
            target_kind="review_item",
            target_id=review.id,
            payload={
                "decision_id": str(decision.id),
                "decision_type": request.decision_type,
                "change_set_id": str(change_set.id),
                **(
                    {"reject_effect": reject_effect, **rejection_record}
                    if request.decision_type == "reject"
                    else {}
                ),
                **(
                    {"effect": effect}
                    if effect is not None
                    # Decided while undo existed, but with an effect undo does
                    # not reverse (e.g. reviving an archived page).
                    else {"effect_unsupported": True}
                    if review.review_type == "context_update"
                    and isinstance(locked_page, ContextPage)
                    else {}
                ),
            },
        )
        detail = self._detail(
            session,
            review,
            actor_user_id=principal,
            actor_role=actor_role,
            actor=actor,
            include_undo=actor_kind == "human",
        )
        detail.published_version_id = published_version_id
        detail.superseded_review_count = superseded_review_count
        return detail

    def undo_decision(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        decision_id: UUID,
        *,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> ReviewItemDetail:
        """Undo a review's latest decision while its result is still current.

        The decision's recorded effect is written back: statuses, the page's
        heads and metadata, and the reviews it settled or opened. The decision
        stays in the history, followed by an ``undo`` record.
        """

        if actor_provenance not in {"browser_session", "internal"}:
            raise ReadValidationError(
                "AI-authenticated actors cannot undo review decisions"
            )
        page = self._lock_context_page_for_review_update(
            session,
            workspace_id=workspace_id,
            review_item_id=review_item_id,
        )
        review = self._require_review_for_update(session, workspace_id, review_item_id)
        principal = actor_user_id or parse_uuid_or_none(actor_id)
        latest = self._latest_decisions(session, review, limit=2)
        if (
            len(latest) == 2
            and latest[0].decision_type == "undo"
            and latest[0].actor_id == principal
            and latest[1].id == decision_id
        ):
            # A retry of this actor's undo reads its result.
            return self._detail(
                session,
                review,
                actor_user_id=principal,
                actor_role=actor_role,
                actor=actor,
                include_undo=True,
            )
        candidate = self._undo_candidate(
            session, review, latest[0] if latest else None,
            actor_user_id=principal, actor_role=actor_role,
        )
        if candidate is None or candidate.decision.id != decision_id:
            raise ReviewDecisionUndoUnavailableError("not_undoable")
        if not candidate.available:
            raise ReviewDecisionUndoUnavailableError("later_changes")
        decision, effect = candidate.decision, candidate.effect
        restored = restore_effect(session, workspace_id, effect)
        for created_id in effect["created"].get("review_item", []):
            created = get_in_workspace(
                session,
                ReviewItem,
                workspace_id=workspace_id,
                resource_id=UUID(created_id),
            )
            if created is not None:
                supersede_review_with_lineage(
                    session=session,
                    audit_service=self.audit_service,
                    workspace_id=workspace_id,
                    review=created,
                    new_change_set_id=review.change_set_id,
                    new_review_item_id=review.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    audit_action="review.superseded_by_decision_undo",
                    payload={"undone_decision_id": str(decision.id)},
                )
        if decision.actor_type == "system":
            # Without a human veto the next sweep would approve it again.
            review.risk_signals = {
                **(review.risk_signals or {}),
                "human_review_required": True,
            }
        if review.status != CHANGES_REQUESTED_STATUS:
            # Withdrawing a request releases any claim taken after it. When
            # undo restores an earlier request, keep its recorded claim.
            review.claimed_by = None
            review.claimed_at = None
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=workspace_id,
            resource_id=review.change_set_id,
        )
        undo = ReviewDecision(
            workspace_id=workspace_id,
            review_item_id=review.id,
            decision_type="undo",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            resulting_review_status=review.status,
            resulting_change_set_status=(
                change_set.status if change_set is not None else review.status
            ),
        )
        session.add(undo)
        session.flush()
        if isinstance(page, ContextPage) and "context_page" in restored:
            self._resync_page_after_undo(
                session,
                page=page,
                columns=restored["context_page"],
                undo=undo,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="review.decision.undo",
            target_kind="review_item",
            target_id=review.id,
            payload={
                "decision_id": str(undo.id),
                "undone_decision_id": str(decision.id),
                "undone_decision_type": decision.decision_type,
                "change_set_id": str(review.change_set_id),
            },
        )
        return self._detail(
            session,
            review,
            actor_user_id=principal,
            actor_role=actor_role,
            actor=actor,
            include_undo=True,
        )

    def _resync_page_after_undo(
        self,
        session: Session,
        *,
        page: ContextPage,
        columns: set[str],
        undo: ReviewDecision,
        actor_kind: str,
        actor_id: str,
    ) -> None:
        """Re-derive what follows the page's restored heads and metadata."""

        from backend.services.change_propagation import (
            process_page_publish_maintenance,
        )
        from backend.services.page_service import sync_page_projections
        from backend.services.tree_service import ensure_primary_tree_placement

        if columns & {"title", "placement_parent_node_id"}:
            ensure_primary_tree_placement(
                session=session,
                workspace_id=page.workspace_id,
                target_kind="context_page",
                target_id=page.id,
                title=page.title,
                slug=page.slug,
                section="context",
                parent_id=page.placement_parent_node_id,
                default_parent_slug="inbox",
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
            )
        live_ids = {
            page.current_published_version_id,
            page.latest_draft_version_id,
            page.current_version_id,
        } - {None}
        live = [
            version
            for version_id in live_ids
            if (
                version := get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=page.workspace_id,
                    resource_id=version_id,
                )
            )
            is not None
        ]
        for version in live or [None]:
            sync_page_projections(session, page=page, version=version)
        JobQueueService().enqueue(
            session=session,
            workspace_id=page.workspace_id,
            job_type="embed",
            trigger_type="context_published",
            idempotency_key=context_page_undo_embed_idempotency_key(page.id, undo.id),
            target_ref={"kind": "context_page", "id": str(page.id)},
        )
        if "current_published_version_id" not in columns:
            return
        # The timeline names what is published from now on, so reads "as of"
        # a later instant resolve to the restored version (or to nothing).
        published_id = page.current_published_version_id
        revert = ChangeSet(
            workspace_id=page.workspace_id,
            reason="review decision undone",
            trigger_type="context_page_publish_revert",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="applied",
        )
        session.add(revert)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=page.workspace_id,
                change_set_id=revert.id,
                target_type="context_page",
                target_id=page.id,
                operation="publish_revert",
                diff_ref=(
                    f"context_page_version:{published_id}"
                    if published_id is not None
                    else None
                ),
            )
        )
        if published_id is not None:
            # Dependents learn the published content changed again. The page's
            # own stale warning was restored with the effect; keep it.
            process_page_publish_maintenance(
                session,
                page=page,
                published_version_id=published_id,
                reason=revert.reason,
                resolve_republished=False,
            )

    @staticmethod
    def _latest_decisions(
        session: Session, review: ReviewItem, *, limit: int
    ) -> list[ReviewDecision]:
        return list(
            session.scalars(
                select_in_workspace(
                    ReviewDecision,
                    workspace_id=review.workspace_id,
                    criteria=[ReviewDecision.review_item_id == review.id],
                )
                .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
                .limit(limit)
            )
        )

    @staticmethod
    def _decision_effect(
        session: Session, review: ReviewItem, decision: ReviewDecision
    ) -> dict | None:
        """The effect recorded with ``decision`` (None if none is usable)."""

        payload = ReviewService._decision_audit_payload(session, review, decision)
        effect = (payload or {}).get("effect")
        if isinstance(effect, dict) and effect.get("format") == EFFECT_FORMAT:
            return effect
        return None

    @staticmethod
    def _decision_audit_payload(
        session: Session, review: ReviewItem, decision: ReviewDecision
    ) -> dict | None:
        """The review.decision audit payload written with ``decision``.

        The decision row and its audit share one transaction, so a window
        around the decision finds the audit through the (workspace_id,
        created_at) index.
        """

        window = timedelta(minutes=10)
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == review.workspace_id,
                AuditEvent.created_at >= decision.created_at - window,
                AuditEvent.created_at <= decision.created_at + window,
                AuditEvent.target_kind == "review_item",
                AuditEvent.target_id == review.id,
                AuditEvent.action == "review.decision",
            )
        )
        for event in events:
            payload = event.payload or {}
            if payload.get("decision_id") == str(decision.id):
                return payload
        return None

    @staticmethod
    def _may_undo_decision(
        review: ReviewItem,
        decision: ReviewDecision | None,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
    ) -> bool:
        """Whether the decision belongs to this actor's undo permissions.

        The decider undoes their own decision (an owner anyone's); any editor
        may undo an automatic approval.
        """

        if review.review_type != "context_update" or not role_allows(
            actor_role or "none", "editor"
        ):
            return False
        if (
            decision is None
            or decision.decision_type not in REVIEW_STATUS_BY_DECISION
            or review.status != decision.resulting_review_status
        ):
            return False
        if (
            decision.actor_type != "system"
            and decision.actor_id != actor_user_id
            and not role_allows(actor_role or "none", "owner")
        ):
            return False
        return True

    def _undo_candidate(
        self,
        session: Session,
        review: ReviewItem,
        decision: ReviewDecision | None,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
    ) -> _UndoCandidate | None:
        if not self._may_undo_decision(
            review, decision, actor_user_id=actor_user_id, actor_role=actor_role
        ):
            return None
        assert decision is not None
        effect = self._decision_effect(session, review, decision)
        if effect is None:
            return None
        if (
            decision.decision_type == "request_changes"
            and review_status_before(effect, review.id) != "pending"
        ):
            # A re-routed request: undoing it would have to restore the
            # earlier round's audience.
            return None
        return _UndoCandidate(
            decision=decision,
            effect=effect,
            available=effect_still_current(
                session,
                review.workspace_id,
                effect,
                decided_at=decision.created_at,
            ),
        )

    def _undo_payload(
        self,
        session: Session,
        review: ReviewItem,
        decision: ReviewDecision | None,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
        evidence: ReviewEvidencePayload | None,
    ) -> ReviewDecisionUndo | None:
        candidate = self._undo_candidate(
            session, review, decision,
            actor_user_id=actor_user_id, actor_role=actor_role,
        )
        if candidate is None:
            if not self._may_undo_decision(
                review, decision, actor_user_id=actor_user_id, actor_role=actor_role
            ):
                return None
            assert decision is not None
            payload = self._decision_audit_payload(session, review, decision) or {}
            # Decided before undo existed: nothing was recorded. Such decisions
            # are not undoable and get no substitute recovery path either.
            unsupported = "effect" in payload or bool(payload.get("effect_unsupported"))
            # Recovery uses only the actor-visible, exact versions already
            # projected in this review's evidence; a parent draft is not an old
            # publication.
            operations = [
                operation
                for operation in (evidence.operations if evidence else ())
                if operation.document is not None
                and operation.document.document_kind == "context"
                and operation.document.document_id is not None
            ]
            page_ids = {operation.document.document_id for operation in operations}
            page_id = next(iter(page_ids)) if len(page_ids) == 1 else None
            versions = {
                operation.content.candidate_version_id: operation.content
                for operation in operations
                if page_id is not None
                and operation.content is not None
                and operation.content.can_read
                and operation.content.candidate_version_id is not None
            }
            restore = (
                next(iter(versions.values()))
                if unsupported
                and decision.decision_type == "reject"
                and len(versions) == 1
                else None
            )
            return ReviewDecisionUndo(
                decision_id=decision.id,
                decision_type=decision.decision_type,
                available=False,
                reason_code="unsupported_effect" if unsupported else "not_recorded",
                page_id=page_id,
                restore_version_id=restore.candidate_version_id if restore else None,
                restore_version_no=restore.candidate_version_no if restore else None,
            )
        effect = candidate.effect
        restore = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=review.workspace_id,
                resource_id=UUID(effect["restore_version_id"]),
            )
            if not candidate.available and effect.get("restore_version_id")
            else None
        )
        return ReviewDecisionUndo(
            decision_id=candidate.decision.id,
            decision_type=candidate.decision.decision_type,
            available=candidate.available,
            reason_code=None if candidate.available else "later_changes",
            restore_version_id=restore.id if restore is not None else None,
            restore_version_no=restore.version_no if restore is not None else None,
            page_id=parse_uuid_or_none(effect.get("page_id")),
        )

    def claim_review(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> ReviewItemDetail:
        # Soft-claim (D1/B6): the first actor to work a broadcast/open
        # changes_requested item takes a claim, blocking competing fixes. A
        # live claim held by ANOTHER principal raises review_already_claimed
        # (409); re-claiming one's own (or a stale, TTL-expired) claim refreshes
        # it. "Edit directly" just claims — resolution still reuses approve.
        review = self._require_review_for_update(session, workspace_id, review_item_id)
        # Only an active changes_requested item can be claimed; pending/
        # approved/rejected items are out of state.
        if review.status != CHANGES_REQUESTED_STATUS:
            raise ReadValidationError("only a changes_requested review can be claimed")
        claimant = parse_uuid_or_none(actor_id)
        # Audience gate (P2): only an actor in the review's current routed
        # audience may claim it — the named recipient for requester/chat_owner
        # routes, any editor+ for workspace_editors/NULL broadcast routes (the
        # same predicate as the to_me queue). This stops a non-target editor from
        # grabbing a routed item off the workspace-wide list and blocking the
        # intended recipient.
        if claimant is None or not self._principal_in_current_audience(
            session, review, claimant, actor_role
        ):
            raise ReadValidationError(
                "only the routed recipient (or an editor on a broadcast route) "
                "can claim this review"
            )
        if self._claim_is_live(review) and review.claimed_by != claimant:
            raise ReviewAlreadyClaimedError(
                "this review is already being worked by another collaborator"
            )
        review.claimed_by = claimant
        review.claimed_at = utc_now()
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="review.claim",
            target_kind="review_item",
            target_id=review.id,
            payload={"claimed_by": str(claimant) if claimant else None},
        )
        return self._detail(
            session,
            review,
            actor_user_id=claimant,
            actor_role=actor_role,
            actor=actor,
        )

    def release_review(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
        actor_role: str | None = None,
    ) -> ReviewItemDetail:
        # Release a claim. Only the holder may release a live claim; a stale or
        # absent claim is a no-op (idempotent). Releasing someone else's live
        # claim is rejected with review_already_claimed (409).
        review = self._require_review_for_update(session, workspace_id, review_item_id)
        actor_uuid = parse_uuid_or_none(actor_id)
        if self._claim_is_live(review) and review.claimed_by != actor_uuid:
            raise ReviewAlreadyClaimedError(
                "this review is claimed by another collaborator"
            )
        had_claim = review.claimed_by is not None
        review.claimed_by = None
        review.claimed_at = None
        session.flush()
        if had_claim:
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="review.release",
                target_kind="review_item",
                target_id=review.id,
                payload={},
            )
        return self._detail(
            session,
            review,
            actor_user_id=actor_uuid,
            actor_role=actor_role or "editor",
            actor=actor,
        )

    @staticmethod
    def _claim_is_live(review: ReviewItem) -> bool:
        if review.claimed_by is None or review.claimed_at is None:
            return False
        claimed_at = review.claimed_at
        # SQLite round-trips DateTime(timezone=True) as offset-naive; treat a
        # naive value as UTC so the comparison with the aware utc_now() works
        # on both SQLite and Postgres.
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=UTC)
        return claimed_at > utc_now() - timedelta(minutes=CLAIM_TTL_MINUTES)

    # ------------------------------------------------------------------
    # MCP review surface (T15b / item 11-c)
    # ------------------------------------------------------------------

    def list_requested_changes_for_principal(
        self,
        session: Session,
        workspace_id: UUID,
        principal: UUID | None,
        principal_role: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        actor: ActorContext | None = None,
    ) -> ListRequestedChangesPayload:
        """The AI-facing projection of the change-requests routed to P (§3.1).

        Reuses the T15-B4 visibility query (list_reviews with audience_for=P,
        scope=to_me) restricted to changes_requested, then projects each item to
        the MCP shape. An owner-less caller (principal None) gets an empty list.
        """
        if principal is None:
            return ListRequestedChangesPayload(items=[], next_cursor=None)
        # Only advertise items the revision tool can actually act on (context
        # drafts). The support filter runs AFTER list_reviews has already capped
        # and cursored the page, so a page can filter entirely down to
        # unsupported targets (e.g. agent-page reviews). Returning that empty
        # page verbatim WITH a non-null next_cursor would read to a client as
        # "end of list" — it stops paging and silently strands the actionable
        # items just past the filtered page. So advance through underlying pages
        # until we have at least one revisable item or the feed is exhausted; the
        # cursor we return then honors the usual "non-null = more pages" contract.
        next_cursor = cursor
        items: list[RequestedChangeItem] = []
        while True:
            payload = self.list_reviews(
                session=session,
                workspace_id=workspace_id,
                status=CHANGES_REQUESTED_STATUS,
                limit=limit,
                cursor=next_cursor,
                audience_for=principal,
                scope="to_me",
                actor_role=principal_role,
                actor=actor,
            )
            items.extend(
                self._project_requested_change(detail, principal)
                for detail in payload.items
                if self._detail_supports_revision(detail)
            )
            next_cursor = payload.next_cursor
            if items or next_cursor is None:
                break
        return ListRequestedChangesPayload(items=items, next_cursor=next_cursor)

    @staticmethod
    def _project_requested_change(
        detail: ReviewItemDetail,
        principal: UUID,
    ) -> RequestedChangeItem:
        latest_rc = next(
            (
                decision
                for decision in detail.decisions
                if decision.decision_type == "request_changes"
            ),
            None,
        )
        target = None
        operations: list[ReviewEvidenceOperation] = []
        if detail.evidence is not None:
            operations = list(detail.evidence.operations)
            # Pick the primary page op deterministically (LOW #6): a multi-op
            # change set may carry auxiliary ops (e.g. a workspace_tree_node
            # placement) at operations[0]. Prefer a context page op; fall back
            # to the first op only if none is a page op.
            primary = next(
                (
                    op
                    for op in operations
                    if op.target_type in ("context_page", "context_page_version")
                ),
                operations[0] if operations else None,
            )
            if primary is not None:
                target = RequestedChangeTarget(
                    target_type=primary.target_type,
                    target_id=primary.target_id,
                    slug=primary.target_slug,
                    title=primary.target_title,
                )
        return RequestedChangeItem(
            review_item_id=detail.id,
            change_set_id=detail.change_set_id,
            audience_kind=detail.current_audience_kind,
            reason=latest_rc.decision_reason if latest_rc is not None else None,
            target=target,
            evidence=RequestedChangeEvidence(operations=operations),
            claim=RequestedChangeClaim(
                claimed=detail.claimed_by is not None,
                claimed_by_me=detail.claimed_by == principal,
                claimed_at=detail.claimed_at,
            ),
            latest_decision_at=(
                latest_rc.created_at if latest_rc is not None else None
            ),
        )

    def submit_revision_for_review(
        self,
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
        principal: UUID | None,
        actor_kind: str,
        actor_id: str,
        principal_role: str | None = None,
        *,
        context_write_service,
        actor_provenance: str = "internal",
        update: UpdateDraftRequest | None = None,
        create: WriteDraftRequest | None = None,
        note: str | None = None,
        origin_chat_session_id: UUID | None = None,
    ) -> SubmitRevisionForReviewResult:
        """Submit a revised draft for an open request (state machine T6).

        Acquires the soft-claim, creates a new draft via the existing write
        path, and returns the ORIGINAL review item to ``pending`` for a human to
        approve (the AI never approves — Risk 6). Idempotent on
        (review_item_id, principal, revision-hash): a duplicate submit by the
        SAME principal returns the prior result instead of creating a second
        draft. The idempotency key is the per-conversation principal P, never
        the shared MCP client actor_id — two principals sharing one client must
        not collide, and a dedup hit is never returned to a principal who did
        not author it.
        """
        if note is not None:
            validate_text(note, field="note", max_length=2000)
        self._lock_context_page_for_review_update(
            session,
            workspace_id=workspace_id,
            review_item_id=review_item_id,
        )
        review = self._require_review_for_update(session, workspace_id, review_item_id)
        if principal is None:
            # No effective principal (owner-less caller) → no audience.
            raise ReadNotFoundError("review item not found")
        # The principal-keyed prior result (proof THIS principal already
        # submitted this exact revision for this item). Computed up front but
        # applied AFTER the audience gate so it can never leak existence to an
        # out-of-audience caller (HIGH #1).
        content_hash = _revision_content_hash(update, create)
        prior = self._prior_revision_result(
            session, review_item_id, principal, content_hash
        )
        # Audience gate FIRST (no existence leak outside the caller's audience).
        # An active changes_requested item must pass the current-audience
        # predicate. Once the item has been resolved back to pending, the only
        # legitimate caller is a retry by the principal who authored the prior
        # revision — its own prior result is the proof of past membership.
        if review.status == CHANGES_REQUESTED_STATUS:
            if not self._principal_in_current_audience(
                session, review, principal, principal_role
            ):
                raise ReadNotFoundError("review item not found")
            if prior is not None:
                return prior
        else:
            # Not changes_requested: the only valid caller is a same-principal
            # idempotent retry (its prior result proves past audience
            # membership). Anyone else — including an out-of-audience caller on
            # the same MCP client — gets not_found, never a status leak that the
            # item exists (HIGH #1: no existence leak outside audience).
            if prior is not None:
                return prior
            if self._withdrawn_request_reached(
                session, review, principal, principal_role
            ):
                raise ReadNotFoundError(
                    "the change request on this review was withdrawn; it no "
                    "longer accepts revisions"
                )
            raise ReadNotFoundError("review item not found")

        # Bind the revision to the original review's target (P1). The revised
        # draft MUST update the SAME context page this change-request is about.
        # A mismatched update.page_id — or a `create`, which always writes a
        # different/new page — would otherwise persist an unrelated draft AND
        # supersede this review below, silently dropping the requested change and
        # smuggling an unrelated draft into pending. Checked AFTER the audience
        # gate so an out-of-audience caller still gets a uniform not_found (no
        # target/existence leak), and before any claim/write/supersede.
        bound_page_id = self._revision_target_page_id(session, review)
        if bound_page_id is None:
            # No supported context-page target (e.g. an agent-page review): not
            # revisable here, and must not be superseded by a context write.
            raise ReadValidationError("this review has no revisable context page")
        if update is None or update.page_id != bound_page_id:
            raise ReadValidationError(
                "a revision must update the page this review is about"
            )

        # Soft-claim (T4): block a competing live claim by another principal.
        if self._claim_is_live(review) and review.claimed_by != principal:
            raise ReviewAlreadyClaimedError(
                "this review is already being worked by another collaborator"
            )
        review.claimed_by = principal
        review.claimed_at = utc_now()
        session.flush()

        # Create the new draft via the EXISTING update path (its own ChangeSet +
        # ReviewItem at pending, with origin_chat_session_id for this AI's chat).
        # Only `update` reaches here — the binding gate above rejects `create`
        # (it would target a different page than the review is bound to).
        draft = context_write_service.update_draft(
            session=session,
            workspace_id=workspace_id,
            request=update,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance=actor_provenance,
            origin_chat_session_id=origin_chat_session_id,
            # A revision answering a change request must never auto-approve — it
            # goes back to a human ("the AI never approves" — Risk 6). (re-review
            # r2 #1/#2: an Auto-fix format-only resubmit was being force-approved.)
            auto_approve_format_only=False,
            allow_changes_requested_revision=True,
        )
        if draft.change_set_id is None or draft.review_item_id is None:
            # A no-op revision (the resubmitted body is identical to the current
            # draft — the Auto-fix no-op short-circuit produced no change_set).
            # Do NOT supersede the original request; the author must actually
            # address the requested changes. (re-review r2: no fail-open / crash.)
            # Release the soft-claim we took above so the item is not left
            # claimed after a rejected no-op (re-review r3 :1104).
            review.claimed_by = None
            review.claimed_at = None
            session.flush()
            raise ReadValidationError(
                "the submitted revision is identical to the current draft; "
                "please address the requested changes"
            )

        # Resolve T6 (#7 one-pending-item invariant): the AI's revision is a NEW
        # draft created above with its OWN ChangeSet + pending ReviewItem (status
        # 'draft_created'). Reusing the original and keeping its stale
        # change_set_id would leave TWO competing pending items for one page — a
        # human could approve the STALE original and silently drop the AI's fix.
        # So CLOSE the original instead: mark it and its now-stale ChangeSet
        # 'superseded'. The new draft's ReviewItem is then the single live
        # pending item, pointing at the revised content. The original→new lineage
        # is recorded as the audit event below (O3, not a ledger decision); the
        # idempotency key is still the original review_item_id + principal.
        supersede_review_with_lineage(
            session=session,
            audit_service=self.audit_service,
            workspace_id=workspace_id,
            review=review,
            new_change_set_id=draft.change_set_id,
            new_review_item_id=draft.review_item_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            payload={
                # principal (per-conversation owner P) is the idempotency key —
                # NOT actor_id (the shared MCP client). actor_id is still
                # recorded by AuditService for the actor trail.
                "principal": str(principal),
                "content_hash": content_hash,
                "note": note,
            },
        )
        return SubmitRevisionForReviewResult(
            original_review_item_id=review.id,
            new_change_set_id=draft.change_set_id,
            new_review_item_id=draft.review_item_id,
            new_review_status="pending",
            deduplicated=False,
        )

    def _withdrawn_request_reached(
        self,
        session: Session,
        review: ReviewItem,
        principal: UUID,
        principal_role: str | None,
    ) -> bool:
        """Whether the latest decision undid a request this principal received.

        Only its audience learns that the request was withdrawn; anyone else
        still gets a plain not_found.
        """

        latest = self._latest_decisions(session, review, limit=2)
        if (
            len(latest) < 2
            or latest[0].decision_type != "undo"
            or latest[1].decision_type != "request_changes"
            or not role_allows(principal_role or "viewer", "editor")
        ):
            return False
        request = latest[1]
        if request.audience_kind in (
            ReviewAudienceKind.REQUESTER.value,
            ReviewAudienceKind.CHAT_OWNER.value,
        ):
            return request.audience_user_id == principal
        return True

    def _principal_in_current_audience(
        self,
        session: Session,
        review: ReviewItem,
        principal: UUID,
        principal_role: str | None,
    ) -> bool:
        if review.status != CHANGES_REQUESTED_STATUS:
            return False
        if not role_allows(principal_role or "viewer", "editor"):
            return False
        in_audience = session.scalar(
            select(
                func.coalesce(
                    _decision_audience_predicate(
                        ReviewDecision,
                        principal=principal,
                        principal_role=principal_role,
                    ),
                    false(),
                )
            )
            .where(
                ReviewDecision.review_item_id == review.id,
                ReviewDecision.decision_type == "request_changes",
            )
            .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
            .limit(1)
        )
        # No decision yet is also a legacy editor broadcast (D2).
        return in_audience is None or bool(in_audience)

    def _prior_revision_result(
        self,
        session: Session,
        review_item_id: UUID,
        principal: UUID,
        content_hash: str | None,
    ) -> SubmitRevisionForReviewResult | None:
        # Idempotent retry (Risk 5 / D1): if THIS PRINCIPAL already submitted a
        # revision with the same content for this item, return the prior result
        # instead of creating a duplicate draft. Keyed on the per-conversation
        # principal P (audit payload 'principal'), NOT the shared MCP client
        # actor_id — so two principals on one client never collide and a dedup
        # hit never leaks to a different principal (HIGH #1). content_hash None
        # (e.g. a metadata/citation-only update with no distinguishing fields)
        # cannot be deduped here; the soft-claim still guards the double-fix.
        if content_hash is None:
            return None
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.action == "review.revision_submitted",
                AuditEvent.target_id == review_item_id,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        ).all()
        for event in events:
            payload = event.payload or {}
            if payload.get("content_hash") != content_hash:
                continue
            # Same principal (compare resolved UUIDs to tolerate str/uuid).
            if parse_uuid_or_none(payload.get("principal")) != principal:
                continue
            new_change_set_id = parse_uuid_or_none(payload.get("new_change_set_id"))
            new_review_item_id = parse_uuid_or_none(payload.get("new_review_item_id"))
            if new_change_set_id is None or new_review_item_id is None:
                continue
            return SubmitRevisionForReviewResult(
                original_review_item_id=review_item_id,
                new_change_set_id=new_change_set_id,
                new_review_item_id=new_review_item_id,
                new_review_status="pending",
                deduplicated=True,
            )
        return None

    @staticmethod
    def _decision_matches_request(
        decision: ReviewDecision,
        *,
        workspace_id: UUID,
        review_item_id: UUID,
        actor_kind: str,
        actor_id: UUID,
        request: ReviewDecisionRequest,
    ) -> bool:
        audience_matches = decision.audience_kind == request.audience_kind
        if request.audience_kind == ReviewAudienceKind.REQUESTER.value:
            audience_matches = (
                audience_matches and decision.audience_user_id == actor_id
            )
        elif request.audience_kind == ReviewAudienceKind.CHAT_OWNER.value:
            # Chat ownership may have changed since this decision. The stored
            # recipient is the original snapshot, not an instruction to reroute.
            audience_matches = (
                audience_matches and decision.audience_user_id is not None
            )
        else:
            audience_matches = audience_matches and decision.audience_user_id is None
        return (
            decision.workspace_id == workspace_id
            and decision.review_item_id == review_item_id
            and decision.actor_type == actor_kind
            and decision.actor_id == actor_id
            and decision.decision_type == request.decision_type
            and decision.decision_reason == request.decision_reason
            and audience_matches
        )

    def _resolve_audience(
        self,
        *,
        session: Session,
        request: ReviewDecisionRequest,
        change_set: ChangeSet,
        actor_id: str,
    ) -> tuple[str | None, UUID | None]:
        # Audience is meaningful only for request_changes (ⓐ/B2). Kinds 1 & 2
        # snapshot a concrete principal; kind 3 leaves audience_user_id NULL and
        # is evaluated dynamically against workspace membership at query time.
        if request.decision_type != "request_changes":
            return None, None
        kind = request.audience_kind
        if kind == ReviewAudienceKind.REQUESTER.value:
            return kind, parse_uuid_or_none(actor_id)
        if kind == ReviewAudienceKind.CHAT_OWNER.value:
            owner_id = self._resolve_chat_owner(session, change_set)
            if owner_id is None:
                raise ChatOwnerUnresolvableError(
                    "this change has no identified chat owner to route to"
                )
            return kind, owner_id
        # workspace_editors, or an omitted audience_kind (None): editor
        # broadcast, no user snapshot. NULL audience is the default and is
        # treated as kind-3 by the visibility filter (D2).
        return kind, None

    @staticmethod
    def _resolve_chat_owner(
        session: Session,
        change_set: ChangeSet,
    ) -> UUID | None:
        # chat_owner resolves via the F2 link: ChangeSet.origin_chat_session_id
        # -> McpChatSession.owner_user_id. Unresolvable if the change has no
        # origin chat session or that session has no owner_user_id.
        if change_set.origin_chat_session_id is None:
            return None
        chat = get_in_workspace(
            session,
            McpChatSession,
            workspace_id=change_set.workspace_id,
            resource_id=change_set.origin_chat_session_id,
        )
        if chat is None:
            return None
        return chat.owner_user_id

    @staticmethod
    def _require_review(
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
    ) -> ReviewItem:
        review = get_in_workspace(
            session,
            ReviewItem,
            workspace_id=workspace_id,
            resource_id=review_item_id,
        )
        if review is None:
            raise ReadNotFoundError("review item not found")
        return review

    @staticmethod
    def _require_review_for_update(
        session: Session,
        workspace_id: UUID,
        review_item_id: UUID,
    ) -> ReviewItem:
        # SQLite's SELECT ... FOR UPDATE is a no-op, so acquire its database
        # writer lock before the fresh decision-state read. PostgreSQL keeps
        # using the narrower row lock below.
        acquire_workspace_write_intent(session)

        review = session.scalar(
            select(ReviewItem)
            .where(ReviewItem.id == review_item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if review is None or review.workspace_id != workspace_id:
            raise ReadNotFoundError("review item not found")
        return review

    @staticmethod
    def _lock_context_page_for_review_update(
        session: Session,
        *,
        workspace_id: UUID,
        review_item_id: UUID,
    ) -> ContextPage | AgentPage | None:
        # SQLite ignores FOR UPDATE. Take its writer lock before loading any
        # decision state; the populated SELECT below then refreshes the page
        # under that lock for undo, approval and rejection alike.
        acquire_workspace_write_intent(session)
        review = get_in_workspace(
            session,
            ReviewItem,
            workspace_id=workspace_id,
            resource_id=review_item_id,
        )
        if review is None:
            return None
        target_type = {
            "context_update": "context_page",
            "agent_page_activation": "agent_page",
        }.get(review.review_type)
        if target_type is None:
            return None
        target = session.scalar(
            select_in_workspace(
                ChangeOperation,
                workspace_id=workspace_id,
                criteria=[
                    ChangeOperation.change_set_id == review.change_set_id,
                    ChangeOperation.target_type == target_type,
                ],
            )
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
            .limit(1)
        )
        if target is None:
            return None
        model = ContextPage if target_type == "context_page" else AgentPage
        return session.scalar(
            select_in_workspace(
                model,
                workspace_id=workspace_id,
                criteria=[model.id == target.target_id],
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    @staticmethod
    def _revision_target_page_id(
        session: Session,
        review: ReviewItem,
    ) -> UUID | None:
        """The context page a revision of ``review`` must target (P1 binding).

        Derived from the review's OWN change-set operations: the primary
        context-page op (a ``context_page`` op → its id; a
        ``context_page_version`` op → its page_id). Returns None when the review
        has no supported context-page target (e.g. an agent-page review) — the
        MCP surface then treats it as not-revisable rather than letting a
        revision retarget the write and supersede an unrelated request.
        """
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=review.workspace_id,
            resource_id=review.change_set_id,
        )
        if change_set is None:
            return None
        operations = session.scalars(
            select(ChangeOperation)
            .where(ChangeOperation.change_set_id == change_set.id)
            .order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
        ).all()
        primary = next(
            (
                op
                for op in operations
                if op.target_type in ("context_page", "context_page_version")
            ),
            None,
        )
        if primary is None:
            return None
        if primary.target_type == "context_page":
            return primary.target_id
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=review.workspace_id,
            resource_id=primary.target_id,
        )
        return version.page_id if version is not None else None

    @staticmethod
    def _detail_supports_revision(detail: ReviewItemDetail) -> bool:
        """Whether submit_revision_for_review can act on this item.

        The MCP revision path only writes context-page drafts, so a request
        whose change set carries no context-page operation (e.g. an agent-page
        review) is not actionable and must not be advertised in the AI queue —
        otherwise it looks fixable but every submit is rejected.
        """
        if detail.evidence is None:
            return False
        return any(
            op.target_type in ("context_page", "context_page_version")
            for op in detail.evidence.operations
        )

    def _can_decide_review(
        self,
        *,
        session: Session,
        review: ReviewItem,
        actor_user_id: UUID | None,
        actor_role: str | None,
    ) -> bool:
        # A reconciliation review is an unresolved branch notification, not a
        # generic approve/reject workflow. Until a dedicated workbench exists,
        # only patch_draft(reconciles=...) or explicit branch discard may settle
        # it; a generic decision would hide the review while leaving the branch
        # live and therefore lies about state.
        if review.review_type == "context_reconciliation":
            return False
        if not role_allows(actor_role or "none", "editor"):
            return False
        if review.status == "pending":
            return True
        if review.status == CHANGES_REQUESTED_STATUS:
            return actor_user_id is not None and self._principal_in_current_audience(
                session, review, actor_user_id, actor_role
            )
        return False

    def _can_claim_review(
        self,
        *,
        session: Session,
        review: ReviewItem,
        actor_user_id: UUID | None,
        actor_role: str | None,
    ) -> bool:
        if (
            actor_user_id is None
            or review.status != CHANGES_REQUESTED_STATUS
            or not role_allows(actor_role or "none", "editor")
        ):
            return False
        if not self._principal_in_current_audience(
            session, review, actor_user_id, actor_role
        ):
            return False
        return not self._claim_is_live(review) or review.claimed_by == actor_user_id

    def _action_capabilities(
        self,
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet | None,
        *,
        actor_user_id: UUID | None,
        actor_role: str | None,
        actor: ActorContext | None,
        evidence: ReviewEvidencePayload | None,
    ) -> ReviewActionCapabilities:
        common = self._action_unavailable_reason(
            session,
            review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            claimant_id=parse_uuid_or_none(actor.actor_id) if actor else actor_user_id,
        )
        if common is None and change_set is None:
            common = "change_set_unavailable"
        approve = self._approval_unavailable_reason(
            session,
            review,
            change_set,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_id=actor.actor_id if actor else None,
            actor_kind=actor.actor_kind if actor else "human",
            actor_provenance=actor.provenance if actor else "internal",
        )
        edit = common
        if edit is None and review.status != CHANGES_REQUESTED_STATUS:
            edit = "not_requested_changes"
        if edit is None and not self._can_claim_review(
            session=session,
            review=review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        ):
            edit = "not_in_audience"
        if edit is None and not (
            evidence is not None
            and any(
                op.document is not None
                and op.document.document_kind == "context"
                and op.document.can_open
                and op.document.slug
                for op in evidence.operations
            )
        ):
            edit = "unsupported_revision"
        return ReviewActionCapabilities(
            approve=ReviewActionAvailability(
                allowed=approve is None, reason_code=approve
            ),
            reject=ReviewActionAvailability(allowed=common is None, reason_code=common),
            request_changes=ReviewActionAvailability(
                allowed=common is None, reason_code=common
            ),
            edit_directly=ReviewActionAvailability(
                allowed=edit is None, reason_code=edit
            ),
        )

    def _reject_effect(
        self,
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet | None,
        actor: ActorContext | None,
        *,
        allowed: bool,
    ) -> ReviewRejectEffect | None:
        if not allowed or change_set is None:
            return None
        impact = self._rejection_impact(session, review, change_set, lock=False)
        if impact is None:
            return ReviewRejectEffect(effect="no_head_change")
        page, version, plan = impact.page, impact.version, impact.plan
        metadata = _review_context_target_metadata(
            page=page,
            version=version,
            actor=actor,
            workspace_id=review.workspace_id,
            allow_archived=True,
        )
        if metadata["target_title"] is None or not context_version_is_visible(
            version, actor
        ):
            return None
        published = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=review.workspace_id,
                resource_id=page.current_published_version_id,
            )
            if page.current_published_version_id
            else None
        )
        if published is not None and (
            published.page_id != page.id
            or not context_version_is_visible(published, actor)
        ):
            published = None
        later = sorted(
            (
                member
                for member in (plan.retired if plan is not None else ())
                if member.version_no > version.version_no
                and context_version_is_visible(member, actor)
            ),
            key=lambda member: member.version_no,
        )
        destination = plan.destination if plan is not None else None
        visible_ids = sorted((sibling.id for sibling in impact.siblings), key=str)
        cascaded_ids = sorted((item.id for item in impact.cascaded), key=str)
        return ReviewRejectEffect(
            effect=plan.effect if plan is not None else "no_head_change",
            document_id=page.id,
            candidate_version_id=version.id,
            candidate_version_no=version.version_no,
            published_version_id=published.id if published else None,
            published_version_no=published.version_no if published else None,
            visible_sibling_review_ids=visible_ids,
            visible_sibling_count=len(visible_ids),
            cascaded_versions=[
                ReviewVersionRef(version_id=member.id, version_no=member.version_no)
                for member in later
            ],
            visible_cascaded_review_ids=cascaded_ids,
            cascaded_review_count=len(cascaded_ids),
            resulting_draft_version_id=destination.id if destination else None,
            resulting_draft_version_no=(
                destination.version_no if destination else None
            ),
            restores_review=impact.restores_review,
            metadata_kept=bool(plan and plan.metadata_kept),
            blocked_reason=(
                "private_edit_in_progress"
                if plan is not None and plan.blocked
                else None
            ),
            impact_fingerprint=impact.fingerprint,
        )

    def _detail(
        self,
        session: Session,
        review: ReviewItem,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
        *,
        include_decision_preflight: bool = False,
        include_undo: bool = False,
    ) -> ReviewItemDetail:
        """Project one review; ``actions``/``reject_effect`` only on request.

        Those run the approval preflight (lint and media scans of the whole
        candidate body), so only the single-review read computes them. Lists
        and write responses, some inside locked transactions, return None.
        """
        if actor is not None and actor_role is not None:
            # REST list callers historically passed their default-workspace
            # actor alongside a separately authorized target role. Bind both
            # at this service seam before evaluating document visibility.
            actor = replace(
                actor,
                workspace_id=review.workspace_id,
                workspace_role=actor_role,
                role=actor_role,
            )
        decisions = session.scalars(
            select(ReviewDecision)
            .where(ReviewDecision.review_item_id == review.id)
            .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
        ).all()
        decision_payloads = [
            self._decision_payload(session, decision) for decision in decisions
        ]
        decision_payloads.extend(self._revision_submitted_payloads(session, review))
        decision_payloads.sort(
            key=_review_decision_sort_key,
            reverse=True,
        )
        can_decide = self._can_decide_review(
            session=session,
            review=review,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
        )
        evidence = self._evidence(
            session,
            review,
            actor,
            allow_archived_context=can_decide,
        )
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=review.workspace_id,
            resource_id=review.change_set_id,
        )
        actions = reject_effect = None
        if include_decision_preflight:
            actions = self._action_capabilities(
                session,
                review,
                change_set,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                actor=actor,
                evidence=evidence,
            )
            reject_effect = self._reject_effect(
                session,
                review,
                change_set,
                actor,
                allowed=actions.reject.allowed,
            )
        chat_owner_available = (
            change_set is not None
            and self._resolve_chat_owner(session, change_set) is not None
        )
        approval_context = (
            self._approval_supersession_context(session, review, change_set)
            if change_set is not None
            else None
        )
        preapproval_superseded_review_count = (
            len(approval_context[2])
            if approval_context is not None
            and evidence is not None
            and any(
                operation.document is not None
                and operation.document.document_id == approval_context[0].id
                and operation.content is not None
                and operation.content.candidate_version_id == approval_context[1].id
                for operation in evidence.operations
            )
            else 0
        )
        settlement = (
            self._settlement(session, review) if review.status == "superseded" else None
        )
        reconciliation_page_id: UUID | None = None
        reconciliation_version_id: UUID | None = None
        reconciliation_base_version_no: int | None = None
        reconciliation_head_advance_count: int | None = None
        reconciliation_conflict_count: int | None = None
        reconciliation_branch_actor_kind: str | None = None
        reconciliation_branch_actor_display_name: str | None = None
        reconciliation_chat_session_id: UUID | None = None
        reconciliation_available = False
        if review.review_type == "context_reconciliation" and actor_can_read_drafts(
            actor
        ):
            reconciliation_operation = session.scalar(
                select(ChangeOperation).where(
                    ChangeOperation.workspace_id == review.workspace_id,
                    ChangeOperation.change_set_id == review.change_set_id,
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.operation == "patch_draft_diverged_manifest",
                )
            )
            candidate_version_id = _context_version_id_from_diff_ref(
                reconciliation_operation.diff_ref
                if reconciliation_operation is not None
                else None
            )
            candidate_page = (
                session.scalar(
                    select(ContextPage).where(
                        ContextPage.workspace_id == review.workspace_id,
                        ContextPage.id == reconciliation_operation.target_id,
                    )
                )
                if reconciliation_operation is not None
                else None
            )
            candidate_version = (
                session.scalar(
                    select(ContextPageVersion).where(
                        ContextPageVersion.workspace_id == review.workspace_id,
                        ContextPageVersion.page_id
                        == reconciliation_operation.target_id,
                        ContextPageVersion.id == candidate_version_id,
                    )
                )
                if reconciliation_operation is not None
                and candidate_version_id is not None
                else None
            )
            if (
                candidate_page is not None
                and candidate_version is not None
                and candidate_page.workspace_id == review.workspace_id
                and candidate_version.workspace_id == review.workspace_id
                and candidate_version.page_id == candidate_page.id
            ):
                from backend.services.context_reconciliation import (
                    load_reconciliation_snapshot,
                    reconciliation_branch_is_available,
                )

                if reconciliation_branch_is_available(candidate_version):
                    try:
                        snapshot = load_reconciliation_snapshot(
                            session,
                            page=candidate_page,
                            branch_id=candidate_version.id,
                            actor_kind=actor.actor_kind if actor else None,
                            actor_id=actor.actor_id if actor else None,
                        )
                    except ReadServiceError:
                        pass
                    else:
                        reconciliation_available = True
                        reconciliation_page_id = candidate_page.id
                        reconciliation_version_id = candidate_version.id
                        reconciliation_base_version_no = snapshot.base.version_no
                        reconciliation_head_advance_count = snapshot.head_advance_count
                        reconciliation_conflict_count = len(snapshot.conflict_ids)
                        reconciliation_branch_actor_kind = snapshot.branch.actor_type
                        reconciliation_branch_actor_display_name = (
                            snapshot.branch.actor_display_name
                        )
                        if (
                            change_set is not None
                            and change_set.origin_chat_session_id is not None
                        ):
                            chat = session.scalar(
                                select(McpChatSession).where(
                                    McpChatSession.workspace_id == review.workspace_id,
                                    McpChatSession.id
                                    == change_set.origin_chat_session_id,
                                )
                            )
                            if chat is not None:
                                reconciliation_chat_session_id = chat.id
        return ReviewItemDetail(
            id=review.id,
            workspace_id=review.workspace_id,
            change_set_id=review.change_set_id,
            review_type=review.review_type,
            status=review.status,
            risk_level=review.risk_level,
            risk_policy_version=review.risk_policy_version,
            risk_resolution_summary=review.risk_resolution_summary,
            risk_signals=review.risk_signals,
            risk_policy_snapshot=review.risk_policy_snapshot,
            decision_summary=review.decision_summary,
            decision_summary_code=review.decision_summary_code,
            current_audience_kind=_current_audience_kind(session, review, decisions),
            chat_owner_available=chat_owner_available,
            # Expose the claim only while it is LIVE (P2): an expired/TTL'd claim
            # is available for takeover, so it must read as unclaimed rather than
            # render a stale claimed_by/claimed_at as if still active.
            claimed_by=review.claimed_by if self._claim_is_live(review) else None,
            claimed_at=review.claimed_at if self._claim_is_live(review) else None,
            can_decide=can_decide,
            can_claim=self._can_claim_review(
                session=session,
                review=review,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
            ),
            actions=actions,
            reject_effect=reject_effect,
            created_at=review.created_at,
            updated_at=review.updated_at,
            evidence=evidence,
            decisions=decision_payloads,
            superseded_review_count=preapproval_superseded_review_count,
            settlement=settlement,
            undo=(
                self._undo_payload(
                    session,
                    review,
                    decisions[0] if decisions else None,
                    actor_user_id=actor_user_id,
                    actor_role=actor_role,
                    evidence=evidence,
                )
                if include_undo
                else None
            ),
            reconciliation_available=reconciliation_available,
            reconciliation_page_id=reconciliation_page_id,
            reconciliation_version_id=reconciliation_version_id,
            reconciliation_base_version_no=reconciliation_base_version_no,
            reconciliation_head_advance_count=reconciliation_head_advance_count,
            reconciliation_conflict_count=reconciliation_conflict_count,
            reconciliation_branch_actor_kind=reconciliation_branch_actor_kind,
            reconciliation_branch_actor_display_name=(
                reconciliation_branch_actor_display_name
            ),
            reconciliation_chat_session_id=reconciliation_chat_session_id,
            can_discard_reconciliation=(
                reconciliation_available
                and role_allows(actor_role or "viewer", "editor")
            ),
        )

    @staticmethod
    def _settlement(session: Session, review: ReviewItem) -> ReviewSettlement | None:
        """Why this superseded review closed, from the audit that closed it.

        Supersession writes the review and its audit together, so a window
        around the review's last update finds that audit through the
        (workspace_id, created_at) index instead of scanning the workspace.
        """

        window = timedelta(minutes=10)
        event = session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == review.workspace_id,
                AuditEvent.created_at >= review.updated_at - window,
                AuditEvent.created_at <= review.updated_at + window,
                AuditEvent.target_kind == "review_item",
                AuditEvent.target_id == review.id,
                AuditEvent.action.in_(_SETTLEMENT_REASONS),
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        if event is None:
            return None
        payload = event.payload or {}
        version_no = payload.get(
            "rejected_version_no", payload.get("published_version_no")
        )
        return ReviewSettlement(
            reason_code=_SETTLEMENT_REASONS[event.action],
            by_review_item_id=parse_uuid_or_none(payload.get("new_review_item_id")),
            by_version_no=version_no if isinstance(version_no, int) else None,
        )

    def _decision_payload(
        self,
        session: Session,
        decision: ReviewDecision,
    ) -> ReviewDecisionPayload:
        actor_id = str(decision.actor_id) if decision.actor_id is not None else None
        return ReviewDecisionPayload(
            decision_id=decision.id,
            decision_type=decision.decision_type,
            actor_type=decision.actor_type,
            actor_id=decision.actor_id,
            actor_display_name=resolve_actor_display_name(
                session,
                actor_kind=decision.actor_type,
                actor_id=actor_id,
            ),
            decision_reason=decision.decision_reason,
            audience_kind=decision.audience_kind,
            audience_user_id=decision.audience_user_id,
            resulting_review_status=decision.resulting_review_status,
            resulting_change_set_status=decision.resulting_change_set_status,
            created_at=decision.created_at,
        )

    def _revision_submitted_payloads(
        self,
        session: Session,
        review: ReviewItem,
    ) -> list[ReviewDecisionPayload]:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.action == "review.revision_submitted",
                AuditEvent.target_id == review.id,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        ).all()
        payloads: list[ReviewDecisionPayload] = []
        for event in events:
            event_payload = event.payload or {}
            principal = parse_uuid_or_none(event_payload.get("principal"))
            actor_id = str(principal) if principal is not None else None
            payloads.append(
                ReviewDecisionPayload(
                    decision_id=event.id,
                    decision_type="revision_submitted",
                    actor_type="human" if principal is not None else event.actor_kind,
                    actor_id=principal,
                    actor_display_name=resolve_actor_display_name(
                        session,
                        actor_kind=(
                            "human" if principal is not None else event.actor_kind
                        ),
                        actor_id=actor_id or event.actor_id,
                    ),
                    decision_reason=None,
                    audience_kind=None,
                    audience_user_id=None,
                    resulting_review_status="superseded",
                    resulting_change_set_status="revision_pending",
                    created_at=event.created_at,
                )
            )
        return payloads

    @staticmethod
    def _evidence(
        session: Session,
        review: ReviewItem,
        actor: ActorContext | None = None,
        *,
        allow_archived_context: bool = False,
    ) -> ReviewEvidencePayload | None:
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=review.workspace_id,
            resource_id=review.change_set_id,
        )
        if change_set is None:
            return None
        operations = session.scalars(
            select_in_workspace(
                ChangeOperation,
                workspace_id=change_set.workspace_id,
                criteria=[
                    ChangeOperation.change_set_id == change_set.id,
                    # Recorded draft metadata is bookkeeping, not a change.
                    ChangeOperation.operation != "metadata_snapshot",
                ],
            ).order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
        ).all()
        projected = []
        all_visible = bool(operations)
        for operation in operations:
            document, content = project_review_target(
                session,
                operation,
                change_set.workspace_id,
                actor,
                allow_archived_context=allow_archived_context,
            )
            visible = (
                document is not None
                and document.document_id is not None
                and content.mode != "unavailable"
                and (
                    content.mode == "metadata_only"
                    or not operation.diff_ref
                    or content.candidate_version_id is not None
                )
            ) or (
                # Document-less metadata (a feedback-only capture) is returned
                # only after its own target authorized this actor.
                document is None and content.mode == "metadata_only"
            )
            all_visible = all_visible and visible
            projected.append(
                ReviewEvidenceOperation(
                    operation_id=operation.id,
                    target_type=operation.target_type,
                    target_id=operation.target_id,
                    target_title=document.title if document else None,
                    target_slug=document.slug if document else None,
                    target_version_no=content.candidate_version_no,
                    operation=operation.operation,
                    diff_summary=operation.diff_summary if visible else None,
                    diff_ref=operation.diff_ref
                    if visible and content.candidate_version_id is not None
                    else None,
                    document=document,
                    content=content,
                )
            )
        return ReviewEvidencePayload(
            change_set_status=change_set.status,
            trigger_type=change_set.trigger_type,
            reason=change_set.reason if all_visible else None,
            cite_call_skipped=change_set.cite_call_skipped,
            operations=projected,
        )


_EMPTY_TARGET_METADATA: dict[str, str | int | None] = {
    "target_title": None,
    "target_slug": None,
    "target_version_no": None,
}


def _recent_context_target_metadata(
    session: Session,
    *,
    reviews: list[ReviewItem],
    workspace_id: UUID,
    actor: ActorContext | None,
) -> dict[UUID, dict[str, str | int | None]]:
    """Hydrate one recent-decision page with a fixed set of batch queries.

    The caller has already loaded ReviewDecision + ReviewItem. This projection
    adds exactly three target queries for a Context decision page: all
    operations, all referenced versions, then all parent pages. No lookup is
    performed inside the per-row projection below.
    """

    change_set_ids = {review.change_set_id for review in reviews}
    if not change_set_ids:
        return {}
    operations = session.scalars(
        select(ChangeOperation)
        .where(ChangeOperation.change_set_id.in_(change_set_ids))
        .order_by(
            ChangeOperation.change_set_id.asc(),
            ChangeOperation.created_at.asc(),
            ChangeOperation.id.asc(),
        )
    ).all()
    primary_by_change_set: dict[UUID, ChangeOperation] = {}
    for operation in operations:
        if operation.target_type not in ("context_page", "context_page_version"):
            continue
        primary_by_change_set.setdefault(operation.change_set_id, operation)

    version_ids = {
        version_id
        for operation in primary_by_change_set.values()
        if (version_id := _context_version_id_for_operation(operation)) is not None
    }
    versions = (
        session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.id.in_(version_ids))
        ).all()
        if version_ids
        else []
    )
    versions_by_id = {version.id: version for version in versions}
    page_ids: set[UUID] = set()
    for operation in primary_by_change_set.values():
        if operation.target_type == "context_page":
            page_ids.add(operation.target_id)
            continue
        version = versions_by_id.get(operation.target_id)
        if version is not None:
            page_ids.add(version.page_id)
    page_ids.update(version.page_id for version in versions)
    pages = (
        session.scalars(select(ContextPage).where(ContextPage.id.in_(page_ids))).all()
        if page_ids
        else []
    )
    pages_by_id = {page.id: page for page in pages}

    hydrated: dict[UUID, dict[str, str | int | None]] = {}
    for change_set_id, operation in primary_by_change_set.items():
        version_id = _context_version_id_for_operation(operation)
        version = versions_by_id.get(version_id) if version_id is not None else None
        page_id = (
            operation.target_id
            if operation.target_type == "context_page"
            else version.page_id
            if version is not None
            else None
        )
        page = pages_by_id.get(page_id) if page_id is not None else None
        hydrated[change_set_id] = _review_context_target_metadata(
            page=page,
            version=version,
            actor=actor,
            workspace_id=workspace_id,
        )
    return hydrated


def _context_version_id_for_operation(operation: ChangeOperation) -> UUID | None:
    if operation.target_type == "context_page_version":
        return operation.target_id
    if not operation.diff_ref:
        return None
    kind, _, raw_id = operation.diff_ref.partition(":")
    if kind != "context_page_version":
        return None
    return parse_uuid_or_none(raw_id)


def _review_context_target_metadata(
    *,
    page: ContextPage | None,
    version: ContextPageVersion | None,
    actor: ActorContext | None,
    workspace_id: UUID,
    allow_archived: bool = False,
) -> dict[str, str | int | None]:
    if page is None or page.workspace_id != workspace_id:
        return dict(_EMPTY_TARGET_METADATA)
    # LEAK-03: this projector is a visibility boundary, not a formatter. A
    # caller that cannot name its actor gets the redacted form — fail CLOSED,
    # not open. (Every REST read supplies a scoped actor and takes the normal
    # visibility branch below.)
    ordinarily_visible = actor is not None and context_page_visible(
        page,
        actor,
        workspace_id=workspace_id,
        allow_archived=allow_archived,
    )
    # A rejected first draft deliberately leaves no readable head. Review
    # detail is nevertheless a scoped evidence read: an editor/owner may still
    # identify the exact version bound to that review, while a viewer keeps the
    # ordinary redacted form. This is the same projection used by recent
    # decisions, so the two surfaces cannot drift into separate visibility
    # rules.
    scoped_headless_preview = (
        not ordinarily_visible
        and actor_can_read_drafts(actor)
        and page.lifecycle_status != "archived"
        and version is not None
        and version.page_id == page.id
        and context_version_is_visible(version, actor)
        and page.current_version_id is None
        and page.latest_draft_version_id is None
        and page.current_published_version_id is None
    )
    if not ordinarily_visible and not scoped_headless_preview:
        return dict(_EMPTY_TARGET_METADATA)
    return {
        "target_title": page.title,
        "target_slug": page.slug,
        "target_version_no": (
            version.version_no
            if version is not None and context_version_is_visible(version, actor)
            else None
        ),
    }


def _hydrated_actor_display_name(
    *,
    actor_kind: str,
    display_name: str | None,
) -> str:
    if actor_kind == "human":
        return (
            display_name.strip()
            if display_name and display_name.strip()
            else "Human editor"
        )
    if actor_kind == "agent":
        return "Agent worker"
    if actor_kind == "mcp_client":
        return "MCP client"
    if actor_kind == "system":
        return "System"
    return actor_kind.replace("_", " ").title()


def _revision_content_hash(
    update: UpdateDraftRequest | None,
    create: WriteDraftRequest | None,
) -> str | None:
    """The idempotency hash for a submit_revision_for_review call.

    Canonical over EVERY request field that changes the resulting draft, so two
    genuinely different revisions never collide and get one silently dropped
    (MED #5): for update — update_mode, body_markdown, base_version_id,
    patch_description, citations, metadata_patch (the full UpdateDraftRequest,
    which no longer carries target_location -- D-2, it was never read on the
    update path); for create — the full WriteDraftRequest. This is NOT
    the markdown content_hash the draft path computes (that would alias
    different update_modes with the same fragment); it is a sha256 over the
    canonical JSON of the request. Returns None only when neither source is
    present (the validator forbids that), so retries always dedup correctly.
    """
    request = update if update is not None else create
    if request is None:
        return None
    payload = {
        "kind": "update" if update is not None else "create",
        "request": request.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        pass
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return 0
    if not decoded.startswith(REVIEW_CURSOR_PREFIX):
        return 0
    try:
        return max(0, int(decoded.removeprefix(REVIEW_CURSOR_PREFIX)))
    except ValueError:
        return 0


def _encode_cursor(offset: int) -> str:
    raw = f"{REVIEW_CURSOR_PREFIX}{offset}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _current_audience_kind(
    session: Session,
    review: ReviewItem,
    decisions: list[ReviewDecision],
) -> str | None:
    # Current audience = audience_kind of the latest request_changes decision,
    # but only while the item is actively changes_requested (ⓐ). `decisions` is
    # ordered created_at DESC, id DESC, so the first request_changes match is the
    # latest round.
    if review.status != "changes_requested":
        return None
    for decision in decisions:
        if decision.decision_type == "request_changes":
            # Same fallback as _audience_falls_back_to_owners, evaluated on the
            # already-loaded decision.
            if decision.audience_kind in _INDIVIDUAL_AUDIENCE_KINDS:
                recipient = (
                    session.get(User, decision.audience_user_id)
                    if decision.audience_user_id is not None
                    else None
                )
                if recipient is None or recipient.status != "active":
                    return "workspace_owners"
            return decision.audience_kind
    return None


def _review_decision_sort_key(decision: ReviewDecisionPayload) -> tuple[float, str]:
    created_at = decision.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (created_at.timestamp(), str(decision.decision_id))


def _parse_csv_filter(value: str | None) -> list[str]:
    if value is None:
        return []
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _expand_review_statuses(statuses: list[str]) -> list[str]:
    expanded: list[str] = []
    for status in statuses:
        if status == "resolved":
            expanded.extend(RESOLVED_REVIEW_STATUSES)
        else:
            expanded.append(status)
    return expanded


def _targets_context_page(page_id: UUID):
    """Reviews whose change set touches one Context page.

    A review has no target column of its own -- the target lives on its change
    operations, and a Context write is recorded against either the page or the
    version it produced. Matching only the first would drop most of a page's
    queue, which is why this covers both.
    """
    version_ids = select(ContextPageVersion.id).where(
        ContextPageVersion.page_id == page_id
    )
    return exists(
        select(ChangeOperation.id).where(
            ChangeOperation.change_set_id == ReviewItem.change_set_id,
            or_(
                and_(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                ),
                and_(
                    ChangeOperation.target_type == "context_page_version",
                    ChangeOperation.target_id.in_(version_ids),
                ),
            ),
        )
    )


def _caller_requested_review(risk_signals: object) -> bool:
    return isinstance(risk_signals, dict) and bool(
        risk_signals.get("caller_needs_review")
    )


def _requires_human_review(risk_signals: object) -> bool:
    # `ai_authored` records who wrote the change; it is not on its own a reason
    # a person must decide. Vetoing on it left an AI draft unable to age out
    # under any policy, so a workspace had no way to say its agents are trusted.
    # `human_review_required` is the resolver's verdict, and that is the veto.
    return isinstance(risk_signals, dict) and bool(
        risk_signals.get("human_review_required")
    )


def _has_lint_error(risk_signals: object) -> bool:
    return isinstance(risk_signals, dict) and bool(risk_signals.get("lint_error"))
