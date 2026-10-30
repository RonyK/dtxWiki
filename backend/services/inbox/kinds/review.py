"""Review kinds: reviews to act on now, and decisions on your changes.

``review_attention`` is current state, derived on read: a review the viewer
can decide now (``ReviewService.actionable_reviews``). It leaves the inbox
when it is decided, superseded, claimed by someone else or no longer routed
to the viewer. A withdrawn request for changes brings back the pending item
with its earlier key, revision and read mark, and nothing new is announced.

``review_result`` is a message: a person approved or rejected your change.
``ReviewResultNotifier`` writes it when the decision is made. Automatic
decisions, your own decisions and undo write nothing; an undone or
superseded decision hides its message.
"""

from collections.abc import Mapping, Sequence
from functools import cache
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.change import ChangeSet
from backend.models.review import ReviewDecision, ReviewItem
from backend.policy.rbac import role_allows
from backend.repositories.workspace_scoped import select_in_workspace_by_ids
from backend.services.inbox.base import (
    ComputedScan,
    InboxDisplay,
    InboxKind,
    InboxSnapshot,
    InboxViewer,
)
from backend.services.inbox.links import review_path
from backend.services.inbox.scopes import WorkspaceInbox
from backend.services.inbox.sources import STORED, ComputedSource
from backend.services.review_service import ReviewService
from backend.utils.uuid import parse_uuid_or_none

REVIEW_ATTENTION = "review_attention"
REVIEW_RESULT = "review_result"

PENDING = "pending"
CHANGES_REQUESTED = "changes_requested"
RESULT_DECISIONS = frozenset({"approve", "reject"})


@cache
def _reviews() -> ReviewService:
    # Read-only queries need none of the service's collaborators.
    return ReviewService()


def _workspace_id(viewer: InboxViewer) -> UUID:
    return viewer.scope.owner(viewer.user_id)["workspace_id"]


def _review_id(item_key: str) -> UUID | None:
    return parse_uuid_or_none(item_key.partition(":")[0])


def _load(
    session: Session, workspace_id: UUID, review_ids: set[UUID]
) -> dict[UUID, ReviewItem]:
    if not review_ids:
        return {}
    return {
        review.id: review
        for review in session.scalars(
            select_in_workspace_by_ids(
                ReviewItem, workspace_id=workspace_id, resource_ids=review_ids
            )
        )
    }


# -- review_attention --------------------------------------------------------


def _attention_snapshot(
    review: ReviewItem, request: ReviewDecision | None
) -> InboxSnapshot:
    if request is None:
        return InboxSnapshot(
            f"{review.id}:{PENDING}", str(review.id), review.created_at
        )
    return InboxSnapshot(
        f"{review.id}:{CHANGES_REQUESTED}", str(request.id), request.created_at
    )


def _attention_scan(session: Session, viewer: InboxViewer, limit: int) -> ComputedScan:
    rows, total = _reviews().actionable_reviews(
        session,
        _workspace_id(viewer),
        principal=viewer.user_id,
        principal_role=viewer.role,
        limit=limit,
    )
    return ComputedScan(
        [_attention_snapshot(review, request) for review, request in rows], total
    )


def _attention_get(
    session: Session, viewer: InboxViewer, item_key: str
) -> InboxSnapshot | None:
    review_id = _review_id(item_key)
    if review_id is None:
        return None
    found = _reviews().actionable_review(
        session,
        _workspace_id(viewer),
        review_id,
        principal=viewer.user_id,
        principal_role=viewer.role,
    )
    if found is None:
        return None
    snapshot = _attention_snapshot(*found)
    return snapshot if snapshot.item_key == item_key else None


def _attention_resolve(
    session: Session, viewer: InboxViewer, snapshots: Sequence[InboxSnapshot]
) -> Mapping[str, InboxDisplay]:
    workspace_id = _workspace_id(viewer)
    reviews = _load(
        session,
        workspace_id,
        {review_id for s in snapshots if (review_id := _review_id(s.item_key))},
    )
    titles = _reviews().review_target_titles(
        session, workspace_id, list(reviews.values()), viewer.actor
    )
    displays: dict[str, InboxDisplay] = {}
    for snapshot in snapshots:
        review = reviews.get(_review_id(snapshot.item_key))
        if review is None:
            continue
        displays[snapshot.item_key] = InboxDisplay(
            title=titles.get(review.id),
            href=review_path(workspace_id, review.id),
            params={"state": snapshot.item_key.partition(":")[2]},
        )
    return displays


def review_attention_kind() -> InboxKind:
    return InboxKind(
        REVIEW_ATTENTION,
        WorkspaceInbox.name,
        ComputedSource(scan=_attention_scan, get=_attention_get),
        _attention_resolve,
        reopen_on_new_revision=True,
    )


# -- review_result -----------------------------------------------------------


def _result_resolve(
    session: Session, viewer: InboxViewer, snapshots: Sequence[InboxSnapshot]
) -> Mapping[str, InboxDisplay]:
    # Decisions are visible to editors; a recipient who lost the role or left
    # the workspace no longer sees them.
    if not role_allows(viewer.role or "none", "editor"):
        return {}
    workspace_id = _workspace_id(viewer)
    reviews = _load(
        session,
        workspace_id,
        {review_id for s in snapshots if (review_id := _review_id(s.item_key))},
    )
    latest = _reviews().latest_decisions(session, workspace_id, reviews)
    current: list[tuple[InboxSnapshot, ReviewItem, ReviewDecision]] = []
    for snapshot in snapshots:
        review = reviews.get(_review_id(snapshot.item_key))
        decision = latest.get(review.id) if review is not None else None
        # An undo or a supersession moved the review on from this decision.
        if (
            review is None
            or decision is None
            or str(decision.id) != snapshot.revision
            or review.status != decision.resulting_review_status
        ):
            continue
        current.append((snapshot, review, decision))
    titles = _reviews().review_target_titles(
        session, workspace_id, [review for _s, review, _d in current], viewer.actor
    )
    return {
        snapshot.item_key: InboxDisplay(
            title=titles.get(review.id),
            href=review_path(workspace_id, review.id),
            params={"decision": decision.decision_type},
        )
        for snapshot, review, decision in current
    }


def review_result_kind() -> InboxKind:
    return InboxKind(
        REVIEW_RESULT,
        WorkspaceInbox.name,
        STORED,
        _result_resolve,
        reopen_on_new_revision=True,
    )


class ReviewResultNotifier:
    """Writes ``review_result`` for the person whose change was decided.

    The recipient is the human who submitted the change, or for a change
    made in an MCP chat, that chat's owner. Nobody is told about their own
    decision or about an automatic one.
    """

    def __init__(self, inbox) -> None:
        # A callable returning the InboxService, so the review and inbox
        # services can be built in either order.
        self._inbox = inbox

    def on_decision(
        self,
        session: Session,
        *,
        review: ReviewItem,
        change_set: ChangeSet,
        decision: ReviewDecision,
        principal: UUID | None,
    ) -> None:
        if decision.decision_type not in RESULT_DECISIONS:
            return
        if decision.actor_type == "system":
            return
        recipient = (
            change_set.actor_id
            if change_set.actor_type == "human"
            else ReviewService.chat_owner(session, change_set)
        )
        if recipient is None or recipient == principal:
            return
        self._inbox().put(
            session,
            scope=WorkspaceInbox(review.workspace_id),
            recipient_ids=[recipient],
            kind=REVIEW_RESULT,
            item_key=str(review.id),
            revision=str(decision.id),
            event_at=decision.created_at,
            ref={"decision": decision.decision_type},
        )
