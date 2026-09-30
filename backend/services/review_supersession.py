from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.change import ChangeSet
from backend.models.review import ReviewItem
from backend.services.audit_service import AuditService


def supersede_review_with_lineage(
    *,
    session: Session,
    audit_service: AuditService,
    workspace_id: UUID,
    review: ReviewItem,
    new_change_set_id: UUID,
    new_review_item_id: UUID,
    actor_kind: str,
    actor_id: str,
    audit_action: str = "review.revision_submitted",
    payload: dict[str, Any] | None = None,
) -> None:
    review.status = "superseded"
    review.claimed_by = None
    review.claimed_at = None
    stale_change_set = session.get(ChangeSet, review.change_set_id)
    if stale_change_set is not None:
        stale_change_set.status = "superseded"
    session.flush()
    audit_service.emit(
        session=session,
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=audit_action,
        target_kind="review_item",
        target_id=review.id,
        payload={
            "original_review_item_id": str(review.id),
            "new_change_set_id": str(new_change_set_id),
            "new_review_item_id": str(new_review_item_id),
            **(payload or {}),
        },
    )
