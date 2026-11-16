from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace

ContextReviewDisposition = Literal["rejected", "pending", "approved", "none"]

_VERSION_DIFF_PREFIX = "context_page_version:"


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
    if statuses & {"pending", "changes_requested"}:
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
    diff_refs = {
        f"{_VERSION_DIFF_PREFIX}{version.id}": version.id for version in versions
    }
    page_ids = {version.page_id for version in versions}
    conditions = [
        ReviewItem.review_type == "context_update",
        ReviewItem.status != "superseded",
        ChangeOperation.target_type == "context_page",
        ChangeOperation.target_id.in_(page_ids),
        ChangeOperation.diff_ref.in_(diff_refs.keys()),
    ]
    scopes = (
        {workspace_id}
        if workspace_id is not None
        else {version.workspace_id for version in versions}
    )
    rows = []
    for scope in scopes:
        statement = select_in_workspace(
            ReviewItem,
            workspace_id=scope,
            conditions=conditions,
        )
        statement = join_in_workspace(
            statement,
            ReviewItem,
            ChangeSet,
            workspace_id=scope,
            on=ReviewItem.change_set_id == ChangeSet.id,
        )
        statement = join_in_workspace(
            statement,
            ReviewItem,
            ChangeOperation,
            workspace_id=scope,
            on=ChangeSet.id == ChangeOperation.change_set_id,
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
