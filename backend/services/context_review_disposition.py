from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace
from backend.services.context_version_refs import context_version_ref

ContextReviewDisposition = Literal["rejected", "pending", "approved", "none"]
OPEN_CONTEXT_REVIEW_STATUSES = frozenset({"pending", "changes_requested"})


def select_context_version_reviews(
    workspace_id: UUID,
    page_ids: Collection[UUID],
    *,
    review_type: str | None = "context_update",
    conditions=(),
):
    """Select reviews of Context page operations, with their change sets and
    operations, within one workspace. ``review_type=None`` keeps every type."""
    statement = select_in_workspace(
        ReviewItem,
        workspace_id=workspace_id,
        conditions=(
            *((ReviewItem.review_type == review_type,) if review_type else ()),
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id.in_(page_ids),
            *conditions,
        ),
    )
    statement = join_in_workspace(
        statement,
        ReviewItem,
        ChangeSet,
        workspace_id=workspace_id,
        on=ReviewItem.change_set_id == ChangeSet.id,
    )
    return join_in_workspace(
        statement,
        ReviewItem,
        ChangeOperation,
        workspace_id=workspace_id,
        on=ChangeSet.id == ChangeOperation.change_set_id,
    )


@dataclass(frozen=True)
class ContextVersionReviewMetadata:
    disposition: ContextReviewDisposition
    status: str | None
    review_item_id: UUID | None


def _disposition_from_statuses(statuses: set[str]) -> ContextReviewDisposition:
    # A rejection is the loudest outcome a version can carry, so it wins over a
    # later pending sibling; "none" means no review ever spoke for this version.
    if "rejected" in statuses:
        return "rejected"
    if statuses & OPEN_CONTEXT_REVIEW_STATUSES:
        return "pending"
    if "approved" in statuses:
        return "approved"
    return "none"


def context_version_review_dispositions(
    session: Session,
    versions: Iterable[ContextPageVersion],
    *,
    workspace_id: UUID | None = None,
) -> Mapping[UUID, ContextReviewDisposition]:
    """Derive review outcomes for a whole version list in one query.

    A thin projection of context_version_review_metadata rather than a second
    query of its own: the two ran the same joins over the same rows, and one
    rule living in two places is how they drift apart.
    """

    return {
        version_id: metadata.disposition
        for version_id, metadata in context_version_review_metadata(
            session, versions, workspace_id=workspace_id
        ).items()
    }


def context_version_review_metadata(
    session: Session,
    versions: Iterable[ContextPageVersion],
    *,
    workspace_id: UUID | None = None,
) -> Mapping[UUID, ContextVersionReviewMetadata]:
    """Resolve review status and identity in one scoped query per workspace.

    Explicit caller scope takes priority. Internal callers that omit it use
    the supplied versions' workspace identities; no query is ever unscoped.
    """

    versions = list(versions)
    if not versions:
        return {}
    diff_refs = {context_version_ref(version.id): version.id for version in versions}
    page_ids = {version.page_id for version in versions}
    scopes = (
        {workspace_id}
        if workspace_id is not None
        else {version.workspace_id for version in versions}
    )
    rows = []
    for scope in scopes:
        statement = select_context_version_reviews(
            scope,
            page_ids,
            conditions=(
                ReviewItem.status != "superseded",
                ChangeOperation.diff_ref.in_(diff_refs),
            ),
        )
        rows.extend(
            session.execute(
                statement.with_only_columns(
                    ChangeOperation.diff_ref,
                    ReviewItem.id,
                    ReviewItem.status,
                    ReviewItem.created_at,
                    ReviewItem.risk_signals,
                )
            ).all()
        )
    by_version: dict[UUID, list[tuple[UUID, str, object]]] = {
        version.id: [] for version in versions
    }
    seen: set[tuple[str, UUID]] = set()
    for diff_ref, review_item_id, status, created_at, risk_signals in rows:
        correction = (risk_signals or {}).get("migration_parent_repaired")
        if isinstance(correction, dict) and correction.get("inspection_only"):
            # An ancestry acknowledgement never changes the content's original
            # review outcome, including a rejection or withdrawal.
            continue
        identity = (diff_ref, review_item_id)
        if identity in seen:
            continue
        seen.add(identity)
        version_id = diff_refs.get(diff_ref)
        if version_id is not None:
            by_version[version_id].append((review_item_id, status, created_at))

    status_priority = {
        "rejected": 4,
        "changes_requested": 3,
        "pending": 2,
        "approved": 1,
    }
    result: dict[UUID, ContextVersionReviewMetadata] = {}
    for version in versions:
        candidates = by_version[version.id]
        if not candidates:
            result[version.id] = ContextVersionReviewMetadata(
                disposition="none", status=None, review_item_id=None
            )
            continue
        review_item_id, status, _created_at = max(
            candidates,
            key=lambda row: (status_priority.get(row[1], 0), row[2], str(row[0])),
        )
        result[version.id] = ContextVersionReviewMetadata(
            disposition=_disposition_from_statuses({row[1] for row in candidates}),
            status=status,
            review_item_id=review_item_id,
        )
    return result


def context_version_review_disposition(
    session: Session,
    version: ContextPageVersion,
    *,
    workspace_id: UUID | None = None,
) -> ContextReviewDisposition:
    """Derive a single Context version's review outcome from its review rows."""

    return context_version_review_dispositions(
        session, (version,), workspace_id=workspace_id
    )[version.id]
