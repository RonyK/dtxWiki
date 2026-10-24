import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import and_, exists, false, func, or_, select
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
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.context_write import (
    PublishContextPageRequest,
    ReviewCountsPayload,
    ReviewDecisionPayload,
    ReviewDecisionRecentItem,
    ReviewDecisionRequest,
    ReviewEvidenceOperation,
    ReviewEvidencePayload,
    ReviewItemDetail,
    ReviewItemListPayload,
    ReviewStatusByRiskBreakdown,
    ReviewStatusRiskBreakdown,
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
    actor_can_read_agent_page,
    context_page_visible,
)
from backend.services.audit_service import AuditService
from backend.services.body_preparer import BodyPreparer
from backend.services.db_write_intent import acquire_write_intent
from backend.services.read_errors import (
    ChatOwnerUnresolvableError,
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
    ReviewAlreadyClaimedError,
)
from backend.services.review_risk_policy import (
    auto_approve_by_risk_entries,
    normalized_review_policy,
)
from backend.services.review_supersession import supersede_review_with_lineage
from backend.utils.actor_display import resolve_actor_display_name
from backend.utils.uuid import parse_uuid_or_none

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


def _context_version_id_from_diff_ref(diff_ref: str | None) -> UUID | None:
    prefix = "context_page_version:"
    if not isinstance(diff_ref, str) or not diff_ref.startswith(prefix):
        return None
    return parse_uuid_or_none(diff_ref[len(prefix) :])


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
                        self._current_audience_predicate(principal=actor_user_id),
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

    def _current_audience_predicate(self, *, principal: UUID):
        latest_rc_id = self._latest_request_changes_subquery()
        latest_rc = aliased(ReviewDecision)
        return (
            select(latest_rc.id)
            .where(
                latest_rc.id == latest_rc_id,
                or_(
                    and_(
                        latest_rc.audience_kind.in_(
                            [
                                ReviewAudienceKind.REQUESTER.value,
                                ReviewAudienceKind.CHAT_OWNER.value,
                            ]
                        ),
                        latest_rc.audience_user_id == principal,
                    ),
                    latest_rc.audience_kind
                    == ReviewAudienceKind.WORKSPACE_EDITORS.value,
                    latest_rc.audience_kind.is_(None),
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
        # scope == "to_me": the ⓐ visibility predicate for principal P. The two
        # named-individual kinds (requester, chat_owner) both snapshot
        # audience_user_id, so they collapse to a single membership test.
        is_editor = role_allows(principal_role or "viewer", "editor")
        targeted = and_(
            latest_rc.audience_kind.in_(
                [
                    ReviewAudienceKind.REQUESTER.value,
                    ReviewAudienceKind.CHAT_OWNER.value,
                ]
            ),
            latest_rc.audience_user_id == principal,
        )
        if is_editor:
            # kind 3 (workspace_editors) is dynamic membership, and a NULL
            # audience on a legacy request_changes decision is treated as a
            # kind-3 broadcast (D2) so historical items never vanish.
            targeted = or_(
                targeted,
                latest_rc.audience_kind == ReviewAudienceKind.WORKSPACE_EDITORS.value,
                latest_rc.audience_kind.is_(None),
            )
        return statement.where(targeted)

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
        if not role_allows(actor_role or "viewer", "editor"):
            # Codex #6: a sub-editor (viewer) sees ONLY decisions routed to them
            # personally — never the workspace-wide approve/reject broadcast feed.
            if audience_for is None:
                return []
            statement = statement.where(ReviewDecision.audience_user_id == audience_for)
        elif audience_for is not None:
            statement = statement.where(
                or_(
                    ReviewDecision.audience_kind.is_(None),
                    ReviewDecision.audience_kind
                    == ReviewAudienceKind.WORKSPACE_EDITORS.value,
                    ReviewDecision.audience_user_id == audience_for,
                )
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
        """Resolve the exact sibling set a human approval would supersede."""
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
            .order_by(ChangeOperation.created_at.desc())
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
        if (
            version is None
            or version.page_id != page.id
            or page.latest_draft_version_id != version.id
        ):
            return None
        pending_reviews = (
            session.scalars(
                select(ReviewItem)
                .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
                .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
                .where(
                    ReviewItem.workspace_id == page.workspace_id,
                    ChangeSet.workspace_id == page.workspace_id,
                    ReviewItem.status.in_(("pending", CHANGES_REQUESTED_STATUS)),
                    ReviewItem.review_type == "context_update",
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.diff_ref == f"context_page_version:{version.id}",
                    ReviewItem.id != review.id,
                )
            )
            .unique()
            .all()
        )
        return page, version, pending_reviews

    @staticmethod
    def _rejection_supersession_context(
        session: Session,
        review: ReviewItem,
        change_set: ChangeSet,
    ) -> tuple[ContextPage, ContextPageVersion, list[ReviewItem]] | None:
        """Resolve a locked context-update review's exact version lineage."""

        if review.review_type != "context_update":
            return None
        operations = session.scalars(
            select(ChangeOperation)
            .where(
                ChangeOperation.change_set_id == change_set.id,
                ChangeOperation.target_type == "context_page",
            )
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
        ).all()
        prefix = "context_page_version:"
        target = next(
            (
                operation
                for operation in operations
                if operation.diff_ref and operation.diff_ref.startswith(prefix)
            ),
            None,
        )
        if target is None:
            return None
        version_id = parse_uuid_or_none(target.diff_ref[len(prefix) :])
        if version_id is None:
            return None

        from backend.repositories.context_page_repo import get_page_by_id_for_update

        page = get_page_by_id_for_update(session, target.target_id)
        if page is not None:
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
        version_ref = f"context_page_version:{version.id}"
        # Narrower than the supersession in ContextWriteService.update_draft, on
        # purpose. That one fires because the CONTENT was replaced, so every
        # older pending review of the page is reviewing text that no longer
        # exists -- it supersedes them all, claimed included, and clears the
        # claim. This one fires because a reviewer refused ONE candidate, which
        # says nothing about a different candidate, so it closes only the
        # redundant reviews of that exact version and leaves a claimed one to
        # the reviewer already working it. Do not unify them.
        pending_reviews = session.scalars(
            select(ReviewItem)
            .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ReviewItem.workspace_id == page.workspace_id,
                ChangeSet.workspace_id == page.workspace_id,
                ReviewItem.status == "pending",
                ReviewItem.review_type == "context_update",
                ReviewItem.claimed_by.is_(None),
                ReviewItem.claimed_at.is_(None),
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.diff_ref == version_ref,
                ReviewItem.id != review.id,
            )
            .distinct()
        ).all()
        return page, version, pending_reviews

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
        validate_request_inputs(request)
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        # Context writers lock page -> review when replacing reviewed bytes.
        # Take the same order here before the review row lock so approval or a
        # revision racing browser autosave cannot deadlock on PostgreSQL.
        self._lock_context_page_for_review_update(
            session,
            workspace_id=workspace_id,
            review_item_id=review_item_id,
        )
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

        # A retry of the exact terminal rejection is a read of the original
        # result, not a second state transition. This guard runs under the same
        # review-row lock and deliberately precedes the normal pending-status
        # decision gate below.
        if (
            request.decision_type == "reject"
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
        principal = actor_user_id or parse_uuid_or_none(actor_id)
        if not self._can_decide_review(
            session=session,
            review=review,
            actor_user_id=principal,
            actor_role=actor_role,
        ):
            raise ReadValidationError("this actor cannot decide this review")

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
        rejection_context = (
            self._rejection_supersession_context(session, review, change_set)
            if request.decision_type == "reject"
            else None
        )
        audience_kind, audience_user_id = self._resolve_audience(
            session=session,
            request=request,
            change_set=change_set,
            actor_id=actor_id,
        )
        review.status = REVIEW_STATUS_BY_DECISION[request.decision_type]
        change_set.status = CHANGE_SET_STATUS_BY_DECISION[request.decision_type]
        # Any decision ends the current work cycle (resolution or a fresh
        # request_changes round), so the soft-claim (D1) is released.
        review.claimed_by = None
        review.claimed_at = None
        decision = ReviewDecision(
            workspace_id=review.workspace_id,
            review_item_id=review.id,
            decision_type=request.decision_type,
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            decision_reason=request.decision_reason,
            audience_kind=audience_kind,
            audience_user_id=audience_user_id,
            resulting_review_status=review.status,
            resulting_change_set_status=change_set.status,
        )
        session.add(decision)
        session.flush()

        reject_effect = "no_head_change"
        if request.decision_type == "reject" and rejection_context is not None:
            page, version, pending_reviews = rejection_context
            if (
                version.version_status == "draft"
                and page.latest_draft_version_id == version.id
            ):
                page.latest_draft_version_id = None
                was_archived = page.lifecycle_status == "archived"
                if page.current_published_version_id is not None:
                    page.current_version_id = page.current_published_version_id
                    if not was_archived:
                        page.lifecycle_status = "published"
                    reject_effect = "restore_published"
                    resulting_version = get_in_workspace(
                        session,
                        ContextPageVersion,
                        workspace_id=workspace_id,
                        resource_id=page.current_published_version_id,
                    )
                else:
                    page.current_version_id = None
                    if not was_archived:
                        page.lifecycle_status = "draft"
                    reject_effect = "hide_unpublished"
                    resulting_version = None
                session.flush()

                # Pointer state is the source of truth for both projections and
                # indexing. Refresh them immediately after the move, before any
                # other rejection bookkeeping in this transaction.
                from backend.services.context_write_service import ContextWriteService
                from backend.services.page_service import sync_page_projections

                sync_page_projections(
                    session,
                    page=page,
                    version=resulting_version,
                )
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
                )

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
                    audit_action="review.superseded_by_rejected_candidate",
                    payload={"rejected_version_id": str(version.id)},
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
                    {"reject_effect": reject_effect}
                    if request.decision_type == "reject"
                    else {}
                ),
            },
        )
        published_version_id = None
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
            )
            published_version_id = published.published_version_id
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
                .order_by(ChangeOperation.created_at.desc())
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
        detail = self._detail(
            session,
            review,
            actor_user_id=principal,
            actor_role=actor_role,
            actor=actor,
        )
        detail.published_version_id = published_version_id
        detail.superseded_review_count = superseded_review_count
        return detail

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
            actor_role="editor",
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
        latest_rc = session.scalar(
            select(ReviewDecision)
            .where(
                ReviewDecision.review_item_id == review.id,
                ReviewDecision.decision_type == "request_changes",
            )
            .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
        )
        if latest_rc is None:
            # NULL/legacy audience = kind-3 broadcast (D2): editor+ only.
            return True
        kind = latest_rc.audience_kind
        if kind in (
            ReviewAudienceKind.REQUESTER.value,
            ReviewAudienceKind.CHAT_OWNER.value,
        ):
            return latest_rc.audience_user_id == principal
        # workspace_editors or NULL audience -> editor broadcast (D2).
        return True

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
        acquire_write_intent(session)

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
    ) -> None:
        page_id = session.scalar(
            select(ChangeOperation.target_id)
            .join(ChangeSet, ChangeSet.id == ChangeOperation.change_set_id)
            .join(ReviewItem, ReviewItem.change_set_id == ChangeSet.id)
            .where(
                ReviewItem.id == review_item_id,
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.review_type == "context_update",
                ChangeOperation.target_type == "context_page",
            )
            .order_by(ChangeOperation.created_at.desc())
            .limit(1)
        )
        if page_id is None:
            return
        session.scalar(
            select(ContextPage)
            .where(
                ContextPage.id == page_id,
                ContextPage.workspace_id == workspace_id,
            )
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

    def _detail(
        self,
        session: Session,
        review: ReviewItem,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
    ) -> ReviewItemDetail:
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
            len(approval_context[2]) if approval_context is not None else 0
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
            current_audience_kind=_current_audience_kind(review, decisions),
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
            created_at=review.created_at,
            updated_at=review.updated_at,
            evidence=evidence,
            decisions=decision_payloads,
            superseded_review_count=preapproval_superseded_review_count,
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
            select(ChangeOperation)
            .where(ChangeOperation.change_set_id == change_set.id)
            .order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
        ).all()
        return ReviewEvidencePayload(
            change_set_status=change_set.status,
            trigger_type=change_set.trigger_type,
            reason=change_set.reason,
            cite_call_skipped=change_set.cite_call_skipped,
            operations=[
                ReviewEvidenceOperation(
                    operation_id=operation.id,
                    target_type=operation.target_type,
                    target_id=operation.target_id,
                    **_operation_target_metadata(
                        session,
                        operation,
                        change_set.workspace_id,
                        actor,
                        allow_archived_context=allow_archived_context,
                    ),
                    operation=operation.operation,
                    diff_summary=operation.diff_summary,
                    diff_ref=operation.diff_ref,
                )
                for operation in operations
            ],
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
        and version is not None
        and version.page_id == page.id
        and page.current_version_id is None
        and page.latest_draft_version_id is None
        and page.current_published_version_id is None
    )
    if not ordinarily_visible and not scoped_headless_preview:
        return {
            "target_title": None,
            "target_slug": None,
            "target_version_no": (version.version_no if version is not None else None),
        }
    return {
        "target_title": page.title,
        "target_slug": page.slug,
        "target_version_no": version.version_no if version is not None else None,
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


def _operation_target_metadata(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None = None,
    *,
    allow_archived_context: bool = False,
) -> dict[str, str | int | None]:
    target_title: str | None = None
    target_slug: str | None = None
    target_version_no: int | None = None

    def _agent_meta(page) -> tuple[str | None, str | None]:
        # A sensitive (memory/agent_guide) or archived Agent page's title/slug is
        # owner-only; redact it for a reviewer who cannot read the page. LEAK-03:
        # a caller with no actor at all gets the redacted form too (fail closed).
        if page is None or page.workspace_id != workspace_id:
            return None, None
        if (
            actor is None
            or page.lifecycle_status == "archived"
            or not actor_can_read_agent_page(page, actor)
        ):
            return None, None
        return page.title, page.slug

    if operation.target_type in ("context_page", "context_page_version"):
        version_id = _context_version_id_for_operation(operation)
        version = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=version_id,
            )
            if version_id is not None
            else None
        )
        if (
            version is not None
            and operation.target_type == "context_page"
            and version.page_id != operation.target_id
        ):
            version = None
        page_id = (
            operation.target_id
            if operation.target_type == "context_page"
            else version.page_id
            if version is not None
            else None
        )
        metadata = _review_context_target_metadata(
            page=(
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=page_id,
                )
                if page_id is not None
                else None
            ),
            version=version,
            actor=actor,
            workspace_id=workspace_id,
            allow_archived=allow_archived_context,
        )
        target_title = metadata["target_title"]
        target_slug = metadata["target_slug"]
        target_version_no = metadata["target_version_no"]
    elif operation.target_type == "agent_page":
        target_title, target_slug = _agent_meta(
            get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=operation.target_id,
            )
        )
    elif operation.target_type == "agent_page_version":
        version = get_in_workspace(
            session,
            AgentPageVersion,
            workspace_id=workspace_id,
            resource_id=operation.target_id,
        )
        if version is not None:
            target_title, target_slug = _agent_meta(
                get_in_workspace(
                    session,
                    AgentPage,
                    workspace_id=workspace_id,
                    resource_id=version.agent_page_id,
                )
            )
            target_version_no = version.version_no

    if target_version_no is None and operation.diff_ref:
        target_version_no = _version_no_for_diff_ref(
            session,
            operation.diff_ref,
            workspace_id=workspace_id,
        )
    return {
        "target_title": target_title,
        "target_slug": target_slug,
        "target_version_no": target_version_no,
    }


def _version_no_for_diff_ref(
    session: Session,
    diff_ref: str,
    *,
    workspace_id: UUID,
) -> int | None:
    kind, _, raw_id = diff_ref.partition(":")
    if not raw_id:
        return None
    target_id = parse_uuid_or_none(raw_id)
    if target_id is None:
        return None
    if kind == "context_page_version":
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
        return version.version_no if version is not None else None
    if kind == "agent_page_version":
        version = get_in_workspace(
            session,
            AgentPageVersion,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
        return version.version_no if version is not None else None
    return None


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
