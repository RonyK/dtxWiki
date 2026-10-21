"""Internal read-only approval snapshot shared by preview and execution."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

APPROVAL_UNAVAILABLE_REASONS = {
    "review_resolved": "This review has already been resolved.",
    "reconciliation_required": (
        "This branch requires reconciliation or explicit discard."
    ),
    "insufficient_role": "An editor role is required for this action.",
    "not_in_audience": "This action is unavailable for the current review audience.",
    "claim_conflict": "Another collaborator is currently working on this review.",
    "human_approval_required": (
        "Approval requires a browser session or an internal actor."
    ),
    "change_set_unavailable": "The reviewed change is no longer available.",
    "candidate_unavailable": "The reviewed version is no longer available.",
    "stale_candidate": "This version is not the current publishable candidate.",
    "publication_blocked": "The reviewed content cannot currently be published.",
    "activation_not_allowed": "This actor cannot activate the reviewed Agent page.",
}


@dataclass(frozen=True)
class ReviewApprovalPreflight:
    eligible: bool
    reason_code: str | None
    reason: str | None
    page_id: UUID | None = None
    version_id: UUID | None = None
    # Internal impact IDs are used only to bind a signed preview. Public result
    # rows/counts must use the actor-visible projection instead.
    superseded_review_ids: tuple[UUID, ...] = ()
    visible_superseded_review_ids: tuple[UUID, ...] = ()
    target_title: str | None = None
    target_version_no: int | None = None
    fingerprint_parts: dict[str, Any] = field(default_factory=dict)
