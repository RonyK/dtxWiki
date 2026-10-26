"""Signed, actor-bound review selections and bounded approval execution.

The token contains the explicit visible IDs and HMACs of the approval state,
including the complete internal same-candidate impact set. Hidden sibling IDs
are never serialized. No process-local selection store or mutable cursor scan
is involved, so a different web worker can safely continue the same selection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.schemas.context_write import (
    ReviewBulkDecisionFilters,
    ReviewBulkDecisionPayload,
    ReviewBulkItemOutcome,
    ReviewBulkSkippedItem,
    ReviewDecisionRequest,
    ReviewSelectionItem,
    ReviewSelectionPayload,
    ReviewSelectionRequest,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.read_errors import ReadServiceError, ReadValidationError
from backend.services.review_approval import ReviewApprovalPreflight
from backend.services.review_service import (
    ReviewService,
    _expand_review_statuses,
    _parse_csv_filter,
    _targets_context_page,
)
from backend.utils.uuid import parse_uuid_or_none

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

SELECTION_LIMIT = 5000
SELECTION_TTL_SECONDS = 15 * 60
MAX_TOKEN_BYTES = 1_500_000
MAX_DECODED_BYTES = 2_000_000
TOKEN_KIND = "dtxwiki-review-selection-v1"
SELECTION_CHANGED_REASON = (
    "This candidate or its approval impact changed. Preview and confirm again."
)
SELECTION_EXPIRED_REASON = (
    "This review selection has expired. Preview again before approving."
)


def review_candidate_versions(
    session: Session, workspace_id: UUID, review_ids: list[UUID]
) -> dict[UUID, tuple[UUID, int]]:
    """Each Context review's reviewed (page, version_no), in three queries."""
    if not review_ids:
        return {}
    change_sets = dict(session.execute(
        select_in_workspace(
            ReviewItem, workspace_id=workspace_id,
            criteria=[ReviewItem.id.in_(review_ids)],
        ).with_only_columns(ReviewItem.id, ReviewItem.change_set_id)
    ).all())
    prefix = "context_page_version:"
    version_by_change_set = {
        change_set_id: parse_uuid_or_none(diff_ref[len(prefix) :])
        for change_set_id, diff_ref in session.execute(
            select_in_workspace(
                ChangeOperation, workspace_id=workspace_id,
                criteria=[
                    ChangeOperation.change_set_id.in_(set(change_sets.values())),
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.diff_ref.startswith(prefix),
                ],
            ).with_only_columns(
                ChangeOperation.change_set_id, ChangeOperation.diff_ref
            ).order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
        ).all()
    }
    versions = {
        version_id: (page_id, version_no)
        for version_id, page_id, version_no in session.execute(
            select_in_workspace_by_ids(
                ContextPageVersion, workspace_id=workspace_id,
                resource_ids={v for v in version_by_change_set.values() if v},
            ).with_only_columns(
                ContextPageVersion.id,
                ContextPageVersion.page_id,
                ContextPageVersion.version_no,
            )
        ).all()
    }
    return {
        review_id: versions[version_id]
        for review_id, change_set_id in change_sets.items()
        if (version_id := version_by_change_set.get(change_set_id)) in versions
    }


def order_newest_candidates_first(
    session: Session,
    workspace_id: UUID,
    review_ids: list[UUID],
    *,
    candidates: dict[UUID, tuple[UUID, int]] | None = None,
) -> list[UUID]:
    """Put each Context page's newest reviewed version first.

    Approving the newest selected draft of a page publishes the changes up
    to it and settles its earlier drafts, instead of publishing each draft
    in turn. Pages keep their first position and other reviews their order.
    """
    if len(review_ids) < 2:
        return list(review_ids)
    if candidates is None:
        candidates = review_candidate_versions(session, workspace_id, review_ids)
    page_rank: dict[UUID, int] = {}

    def key(item: tuple[int, UUID]) -> tuple[int, int, int]:
        position, review_id = item
        target = candidates.get(review_id)
        if target is None:
            return position, 0, position
        page_id, version_no = target
        return page_rank.setdefault(page_id, position), -version_no, position

    return [review_id for _, review_id in sorted(enumerate(review_ids), key=key)]


class ReviewSelectionError(ReadValidationError):
    status_code = 409


class ReviewSelectionLimitError(ReviewSelectionError):
    status_code = 422

    def __init__(self, total: int) -> None:
        super().__init__(
            "The review selection exceeds the execution limit. Narrow the filters.",
            error_code="review_selection_limit_exceeded",
        )
        self.total = total
        self.limit = SELECTION_LIMIT


class ReviewSelectionService:
    def __init__(self, review: ReviewService, secret: str) -> None:
        if not secret:
            raise ValueError("review selection signing requires a server secret")
        self.review = review
        self._key = hmac.digest(
            secret.encode("utf-8"), TOKEN_KIND.encode("ascii"), "sha256"
        )

    @staticmethod
    def _actor_binding(actor: ActorContext, role: str) -> dict[str, str | None]:
        return {
            "kind": actor.actor_kind,
            "id": str(actor.actor_id),
            "user": str(actor.user_id) if actor.user_id else None,
            "provenance": actor.provenance,
            "role": role,
        }

    def _digest(self, value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._key, encoded, hashlib.sha256).hexdigest()

    def _fingerprint(self, preflight: ReviewApprovalPreflight) -> str:
        return self._digest(
            {
                "state": preflight.fingerprint_parts,
                "eligible": preflight.eligible,
                "code": preflight.reason_code,
                "page": str(preflight.page_id) if preflight.page_id else None,
                "version": str(preflight.version_id) if preflight.version_id else None,
                # All internal siblings are committed, while only the actor-visible
                # projection is exposed in the response below.
                "impact": sorted(
                    str(value) for value in preflight.superseded_review_ids
                ),
            }
        )

    def _encode(self, claims: dict[str, Any]) -> str:
        raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_DECODED_BYTES:
            raise ReviewSelectionLimitError(int(claims["total"]))
        encoded = base64.urlsafe_b64encode(zlib.compress(raw)).rstrip(b"=")
        signature = base64.urlsafe_b64encode(
            hmac.digest(self._key, encoded, "sha256")
        ).rstrip(b"=")
        token = f"{encoded.decode()}.{signature.decode()}"
        if len(token) > MAX_TOKEN_BYTES:
            raise ReviewSelectionLimitError(int(claims["total"]))
        return token

    def _decode(
        self, token: str, *, workspace_id: UUID, actor: ActorContext, role: str
    ) -> dict[str, Any]:
        try:
            if len(token) > MAX_TOKEN_BYTES:
                raise ValueError("oversized token")
            payload, signature = token.split(".")
            expected = (
                base64.urlsafe_b64encode(
                    hmac.digest(self._key, payload.encode("ascii"), "sha256")
                )
                .rstrip(b"=")
                .decode("ascii")
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            compressed = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_DECODED_BYTES + 1)
            if len(raw) > MAX_DECODED_BYTES or not decoder.eof or decoder.unused_data:
                raise ValueError("invalid token body")
            claims = json.loads(raw)
            if (
                not isinstance(claims, dict)
                or claims.get("kind") != TOKEN_KIND
                or claims.get("workspace") != str(workspace_id)
                or claims.get("actor") != self._actor_binding(actor, role)
                or not isinstance(claims.get("expires"), int)
                or not isinstance(claims.get("issued"), int)
                or not isinstance(claims.get("total"), int)
                or not isinstance(claims.get("entries"), list)
                or not 0 <= claims["total"] <= SELECTION_LIMIT
                or len(claims["entries"]) > claims["total"]
                or claims["expires"] - claims["issued"] != SELECTION_TTL_SECONDS
            ):
                raise ValueError("invalid claims")
            for entry in claims["entries"]:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 4
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], str)
                    or len(entry[1]) != 64
                    or not isinstance(entry[2], bool)
                    or (entry[3] is not None and not isinstance(entry[3], str))
                ):
                    raise ValueError("invalid selection entry")
                UUID(entry[0])
        except (ValueError, TypeError, KeyError, UnicodeError, zlib.error) as exc:
            raise ReviewSelectionError(
                "This review selection is invalid or belongs to another reviewer. "
                "Preview again.",
                error_code="review_selection_invalid",
            ) from exc
        if claims["expires"] <= int(time.time()):
            raise ReviewSelectionError(
                SELECTION_EXPIRED_REASON,
                error_code="review_selection_expired",
            )
        return claims

    def target_ids(
        self,
        session: Session,
        workspace_id: UUID,
        request: ReviewSelectionRequest,
        actor: ActorContext,
        role: str,
    ) -> list[UUID]:
        if request.review_item_ids is not None:
            ids = list(dict.fromkeys(request.review_item_ids))
            if len(ids) > SELECTION_LIMIT:
                raise ReviewSelectionLimitError(len(ids))
            # An explicit ID must obey the same personal-queue visibility as a
            # by-ID read. Preflight enforces that again without trusting IDs.
            return ids
        filters = request.filters or ReviewBulkDecisionFilters()
        statuses = _expand_review_statuses(_parse_csv_filter(filters.status))
        if not statuses:
            statuses = (
                ["pending"]
                if filters.target_page_id is not None
                else ["pending", "changes_requested"]
            )
        criteria = []
        if statuses:
            criteria.append(ReviewItem.status.in_(statuses))
        risks = _parse_csv_filter(filters.risk_level)
        if risks:
            criteria.append(ReviewItem.risk_level.in_(risks))
        if filters.target_page_id is not None:
            criteria.extend([
                _targets_context_page(filters.target_page_id),
                ReviewItem.review_type == "context_update",
            ])
        statement = select_in_workspace(
            ReviewItem, workspace_id=workspace_id, criteria=criteria,
        ).with_only_columns(ReviewItem.id, func.count().over())
        statement, _scope = self.review.apply_list_visibility(
            session=session, statement=statement, workspace_id=workspace_id,
            audience_for=actor.user_id, scope=filters.scope, actor_role=role,
        )
        rows = session.execute(
            statement.order_by(
                ReviewItem.created_at.desc(), ReviewItem.id.desc()
            ).limit(SELECTION_LIMIT + 1)
        ).all()
        total = int(rows[0][1]) if rows else 0
        if total > SELECTION_LIMIT:
            raise ReviewSelectionLimitError(total)
        return [row[0] for row in rows]

    def create(
        self,
        session: Session,
        workspace_id: UUID,
        request: ReviewSelectionRequest,
        *,
        actor: ActorContext,
        actor_role: str,
    ) -> ReviewSelectionPayload:
        selected = self.target_ids(session, workspace_id, request, actor, actor_role)
        candidates = review_candidate_versions(session, workspace_id, selected)
        target_ids = order_newest_candidates_first(
            session, workspace_id, selected, candidates=candidates
        )
        entries: list[list[Any]] = []
        items: list[ReviewSelectionItem] = []
        visible_impact: set[UUID] = set()
        representative_ids: set[UUID] = set()
        covered_by: dict[UUID, tuple[UUID, ReviewApprovalPreflight, str]] = {}
        eligible_count = 0
        for review_id in target_ids:
            if review_id in covered_by:
                # One representative's domain snapshot already commits every
                # sibling's identity/status/claim. Repeating its full sibling
                # query per selected duplicate would make large groups O(N²).
                # Only actor-visible siblings may reuse representative metadata.
                representative_id, representative, signature = covered_by[review_id]
                entries.append(
                    [
                        str(review_id),
                        self._digest([str(representative_id), signature]),
                        False,
                        "representative_not_approved",
                    ]
                )
                items.append(
                    ReviewSelectionItem(
                        id=review_id,
                        eligible=False,
                        will_auto_settle=True,
                        reason_code="superseded_by_approved_publish",
                        reason="Settled with the selected approved version.",
                        target_title=representative.target_title,
                        # Its own version: an earlier draft settled with a
                        # later one is not that later version.
                        target_version_no=(
                            candidates[review_id][1]
                            if review_id in candidates
                            else representative.target_version_no
                        ),
                    )
                )
                continue
            preflight = self.review.approval_preflight(
                session,
                workspace_id,
                review_id,
                actor=actor,
                actor_role=actor_role,
            )
            signature = self._fingerprint(preflight)
            if preflight.eligible:
                eligible_count += 1
                representative_ids.add(review_id)
                visible_impact.update(preflight.visible_superseded_review_ids)
                for sibling_id in preflight.visible_superseded_review_ids:
                    covered_by[sibling_id] = (review_id, preflight, signature)
            entries.append(
                [
                    str(review_id),
                    signature,
                    preflight.eligible,
                    preflight.reason_code,
                ]
            )
            items.append(
                ReviewSelectionItem(
                    id=review_id,
                    eligible=preflight.eligible,
                    auto_settled_review_item_ids=(
                        sorted(preflight.visible_superseded_review_ids, key=str)
                        if preflight.eligible
                        else []
                    ),
                    reason_code=preflight.reason_code,
                    reason=preflight.reason,
                    target_title=preflight.target_title,
                    target_version_no=preflight.target_version_no,
                )
            )
        for item in items:
            if item.id in visible_impact and item.id not in representative_ids:
                item.will_auto_settle = True
                item.reason_code = "superseded_by_approved_publish"
                item.reason = (
                    "This review will be settled with the selected approved version."
                )
        now = int(time.time())
        claims = {
            "kind": TOKEN_KIND,
            "workspace": str(workspace_id),
            "actor": self._actor_binding(actor, actor_role),
            "filters": request.filters.model_dump(mode="json")
            if request.filters
            else None,
            "issued": now,
            "expires": now + SELECTION_TTL_SECONDS,
            "total": len(entries),
            "entries": entries,
        }
        return ReviewSelectionPayload(
            selection_token=self._encode(claims),
            expires_at=datetime.fromtimestamp(claims["expires"], UTC),
            total=len(entries),
            review_item_ids=target_ids,
            eligible_count=eligible_count,
            items=items,
            auto_settled_review_item_ids=sorted(visible_impact, key=str),
        )

    def execute(
        self,
        session_factory: sessionmaker,
        workspace_id: UUID,
        token: str,
        *,
        actor: ActorContext,
        actor_role: str,
        batch_limit: int = 500,
        dry_run: bool = False,
    ) -> ReviewBulkDecisionPayload:
        if not 1 <= batch_limit <= 500:
            raise ReadValidationError("batch_limit must be between 1 and 500")
        claims = self._decode(
            token, workspace_id=workspace_id, actor=actor, role=actor_role
        )
        approved: list[UUID] = []
        skipped: list[ReviewBulkSkippedItem] = []
        auto_settled: set[UUID] = set()
        internal_settled: set[UUID] = set()
        outcomes: list[ReviewBulkItemOutcome] = []
        handled: set[UUID] = set()
        reconfirm = False
        entries = claims["entries"]
        execution_limit = len(entries) if dry_run else batch_limit
        for raw_id, expected_signature, expected_eligible, expected_reason in entries:
            review_id = UUID(raw_id)
            if review_id in internal_settled:
                handled.add(review_id)
                continue
            if len(handled) >= execution_limit:
                break
            if claims["expires"] <= int(time.time()):
                if not approved:
                    raise ReviewSelectionError(
                        SELECTION_EXPIRED_REASON,
                        error_code="review_selection_expired",
                    )
                # Preserve committed results if a long batch crosses expiry.
                # The unsubmitted items are explicit failures to reselect; no
                # expired continuation is offered as an executable action.
                reconfirm = True
                for entry in entries:
                    expired_id = UUID(entry[0])
                    if expired_id in handled or expired_id in internal_settled:
                        continue
                    reason = (
                        "The selection expired before this review was submitted. "
                        "Preview again."
                    )
                    skipped.append(
                        ReviewBulkSkippedItem(
                            id=expired_id,
                            reason=reason,
                            reason_code="review_selection_expired",
                        )
                    )
                    outcomes.append(
                        ReviewBulkItemOutcome(
                            id=expired_id,
                            outcome="failed",
                            reason=reason,
                            reason_code="review_selection_expired",
                        )
                    )
                    handled.add(expired_id)
                break
            try:
                with session_factory.begin() as session:
                    if not dry_run:
                        acquire_workspace_write_intent(session)
                    preflight = self.review.approval_preflight(
                        session,
                        workspace_id,
                        review_id,
                        actor=actor,
                        actor_role=actor_role,
                        lock=not dry_run,
                        publication_checks=dry_run,
                    )
                    metadata = {
                        "target_title": preflight.target_title,
                        "target_version_no": preflight.target_version_no,
                    }
                    if not expected_eligible:
                        reason_code = expected_reason or "not_eligible_for_approval"
                        representative_missing = (
                            reason_code == "representative_not_approved"
                        )
                        if representative_missing:
                            reconfirm = True
                        reason = (
                            (
                                "The selected representative was not approved. "
                                "Preview the remaining reviews again."
                            )
                            if representative_missing
                            else preflight.reason
                            or "This review was excluded from the confirmed selection."
                        )
                        outcomes.append(
                            ReviewBulkItemOutcome(
                                id=review_id,
                                outcome="failed"
                                if representative_missing
                                else "excluded",
                                reason_code=reason_code,
                                reason=reason,
                                **metadata,
                            )
                        )
                        skipped.append(
                            ReviewBulkSkippedItem(
                                id=review_id, reason=reason, reason_code=reason_code
                            )
                        )
                    elif not hmac.compare_digest(
                        expected_signature, self._fingerprint(preflight)
                    ):
                        if not approved:
                            raise ReviewSelectionError(
                                SELECTION_CHANGED_REASON,
                                error_code="review_selection_changed",
                            )
                        reconfirm = True
                        reason = SELECTION_CHANGED_REASON
                        outcomes.append(
                            ReviewBulkItemOutcome(
                                id=review_id,
                                outcome="failed",
                                reason_code="review_selection_changed",
                                reason=reason,
                                **metadata,
                            )
                        )
                        skipped.append(
                            ReviewBulkSkippedItem(
                                id=review_id,
                                reason=reason,
                                reason_code="review_selection_changed",
                            )
                        )
                    elif not preflight.eligible:
                        # Normally covered by the fingerprint mismatch. Kept as
                        # a fail-closed guard if preflight evolves independently.
                        reason = (
                            preflight.reason or "This review can no longer be approved."
                        )
                        skipped.append(
                            ReviewBulkSkippedItem(
                                id=review_id,
                                reason=reason,
                                reason_code=preflight.reason_code,
                            )
                        )
                        outcomes.append(
                            ReviewBulkItemOutcome(
                                id=review_id,
                                outcome="failed",
                                reason=reason,
                                reason_code=preflight.reason_code,
                                **metadata,
                            )
                        )
                    else:
                        decided = (
                            self.review.apply_decision(
                                session=session,
                                workspace_id=workspace_id,
                                review_item_id=review_id,
                                request=ReviewDecisionRequest(decision_type="approve"),
                                actor_kind=actor.actor_kind,
                                actor_id=actor.actor_id,
                                actor_user_id=actor.user_id,
                                actor_role=actor_role,
                                actor_provenance=actor.provenance,
                                actor=actor,
                            )
                            if not dry_run
                            else None
                        )
                        approved.append(review_id)
                        outcomes.append(
                            ReviewBulkItemOutcome(
                                id=review_id,
                                outcome="approved",
                                decision_id=(
                                    decided.undo.decision_id
                                    if decided is not None and decided.undo is not None
                                    else None
                                ),
                                **metadata,
                            )
                        )
                        internal_settled.update(preflight.superseded_review_ids)
                        sibling_versions = review_candidate_versions(
                            session, workspace_id,
                            list(preflight.visible_superseded_review_ids),
                        )
                        for sibling_id in preflight.visible_superseded_review_ids:
                            if sibling_id not in auto_settled:
                                auto_settled.add(sibling_id)
                                sibling = sibling_versions.get(sibling_id)
                                outcomes.append(
                                    ReviewBulkItemOutcome(
                                        id=sibling_id,
                                        outcome="auto_settled",
                                        reason_code="superseded_by_approved_publish",
                                        reason="Settled with the approved version.",
                                        target_title=preflight.target_title,
                                        target_version_no=(
                                            sibling[1]
                                            if sibling is not None
                                            and sibling[0] == preflight.page_id
                                            else None
                                        ),
                                    )
                                )
            except ReviewSelectionError:
                raise
            except ReadServiceError as exc:
                reason = (
                    "This review could not be approved. "
                    "Refresh its status before trying again."
                )
                reason_code = exc.error_code
                if expected_eligible and exc.error_code == "not_found":
                    if not approved:
                        raise ReviewSelectionError(
                            SELECTION_CHANGED_REASON,
                            error_code="review_selection_changed",
                        ) from exc
                    reconfirm = True
                    reason = SELECTION_CHANGED_REASON
                    reason_code = "review_selection_changed"
                skipped.append(
                    ReviewBulkSkippedItem(
                        id=review_id, reason=reason, reason_code=reason_code
                    )
                )
                outcomes.append(
                    ReviewBulkItemOutcome(
                        id=review_id,
                        outcome="failed",
                        reason=reason,
                        reason_code=reason_code,
                    )
                )
            handled.add(review_id)
        # Supersession may settle selected siblings beyond this execution batch.
        # Remove those known outcomes as well, without resubmitting decisions.
        handled.update(
            UUID(entry[0]) for entry in entries if UUID(entry[0]) in internal_settled
        )
        remaining = [entry for entry in entries if UUID(entry[0]) not in handled]
        # A later representative can settle a selected sibling previously
        # excluded in this batch. Its final outcome is settlement, not a failure.
        skipped = [item for item in skipped if item.id not in auto_settled]
        outcomes_by_id = {item.id: item for item in outcomes}
        next_token = None
        if remaining and not dry_run:
            next_token = self._encode({**claims, "entries": remaining})
        return ReviewBulkDecisionPayload(
            approved=approved,
            skipped=skipped,
            auto_settled_review_item_ids=sorted(auto_settled, key=str),
            outcomes=list(outcomes_by_id.values()),
            next_selection_token=next_token,
            selection_complete=not remaining,
            processed_count=len(handled),
            remaining_count=len(remaining),
            total=claims["total"],
            requires_reconfirmation=reconfirm,
        )
