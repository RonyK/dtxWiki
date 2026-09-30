"""Audit and close unreachable pending Context-page reviews (dry-run by default)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import get_settings
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace, default_actor_id
from backend.services.audit_service import AuditService

ALREADY_PUBLISHED = "target_already_published"
SUPERSEDED_OR_ORPHANED = "target_superseded_or_orphaned"
TARGET_VERSION_MISSING = "target_version_missing"


@dataclass(frozen=True)
class GhostReview:
    workspace_id: UUID
    workspace_slug: str
    page_id: UUID
    page_slug: str
    review_id: UUID
    change_set_id: UUID
    reason: str
    target_version_ids: tuple[UUID, ...]


def _target_version_ids(
    session: Session, change_set_id: UUID, page_id: UUID
) -> tuple[UUID, ...]:
    ids = set(
        session.scalars(
            select(ContextPageVersion.id).where(
                ContextPageVersion.page_id == page_id,
                ContextPageVersion.change_set_id == change_set_id,
            )
        )
    )
    refs = session.scalars(
        select(ChangeOperation.diff_ref).where(
            ChangeOperation.change_set_id == change_set_id,
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id == page_id,
        )
    )
    prefix = "context_page_version:"
    for ref in refs:
        if ref and ref.startswith(prefix):
            try:
                ids.add(UUID(ref.removeprefix(prefix)))
            except ValueError:
                pass
    return tuple(sorted(ids, key=str))


def _target_page_ids(session: Session, change_set_id: UUID) -> tuple[UUID, ...]:
    ids = set(
        session.scalars(
            select(ChangeOperation.target_id).where(
                ChangeOperation.change_set_id == change_set_id,
                ChangeOperation.target_type == "context_page",
            )
        )
    )
    return tuple(sorted(ids, key=str))


def _ghost_reason(
    session: Session,
    change_set_id: UUID,
    page: ContextPage,
    *,
    populate_existing: bool = False,
) -> tuple[str, tuple[UUID, ...]] | None:
    """Why this change set's review is unreachable, or None if it is still
    decidable. Read `page` fresh: whether a review is a ghost depends entirely
    on where the page's pointers are right now."""
    target_ids = _target_version_ids(session, change_set_id, page.id)
    versions = [
        v
        for version_id in target_ids
        if (
            v := session.get(
                ContextPageVersion,
                version_id,
                populate_existing=populate_existing,
            )
        )
        is not None
        and v.page_id == page.id
    ]
    if any(
        v.id == page.latest_draft_version_id and v.version_status == "draft"
        for v in versions
    ):
        return None
    if any(
        v.version_status == "published_snapshot"
        or v.id == page.current_published_version_id
        for v in versions
    ):
        reason = ALREADY_PUBLISHED
    elif versions:
        reason = SUPERSEDED_OR_ORPHANED
    else:
        reason = TARGET_VERSION_MISSING
    return reason, tuple(v.id for v in versions)


def find_ghost_reviews(
    session: Session, *, workspace_id: UUID | None = None
) -> list[GhostReview]:
    statement = (
        select(ReviewItem, ChangeSet, Workspace, ContextPage)
        .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
        .join(Workspace, Workspace.id == ReviewItem.workspace_id)
        .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
        .join(ContextPage, ContextPage.id == ChangeOperation.target_id)
        .where(
            ReviewItem.status == "pending",
            ReviewItem.claimed_by.is_(None),
            ReviewItem.review_type == "context_update",
            ChangeSet.workspace_id == ReviewItem.workspace_id,
            ChangeOperation.target_type == "context_page",
            ContextPage.workspace_id == ReviewItem.workspace_id,
        )
        .order_by(
            Workspace.slug, ContextPage.slug, ReviewItem.created_at, ReviewItem.id
        )
    )
    if workspace_id is not None:
        statement = statement.where(ReviewItem.workspace_id == workspace_id)
    ghosts = []
    for review, change_set, workspace, page in session.execute(statement).unique():
        classified = _ghost_reason(session, change_set.id, page)
        if classified is None:
            continue
        reason, version_ids = classified
        ghosts.append(
            GhostReview(
                workspace.id,
                workspace.slug,
                page.id,
                page.slug,
                review.id,
                change_set.id,
                reason,
                version_ids,
            )
        )
    return ghosts


def close_ghost_reviews(
    session: Session, *, workspace_id: UUID | None = None, apply: bool = False
) -> list[GhostReview]:
    ghosts = find_ghost_reviews(session, workspace_id=workspace_id)
    if not apply:
        return ghosts
    audit = AuditService()
    closed = []
    # Context reviews are single-page gates. Refuse ambiguous legacy rows
    # instead of closing a review based on only one of several page targets.
    candidates = [
        ghost
        for ghost in ghosts
        if _target_page_ids(session, ghost.change_set_id) == (ghost.page_id,)
    ]
    # Lock the complete page batch before taking any ReviewItem lock. This is
    # stronger than merely sorting the loop: a later page acquisition while a
    # prior review lock is held can still invert a concurrent page -> review
    # decision or replacement transaction.
    locked_pages = {
        page_id: session.get(
            ContextPage,
            page_id,
            with_for_update=True,
            populate_existing=True,
        )
        for page_id in sorted({ghost.page_id for ghost in candidates}, key=str)
    }
    for ghost in candidates:
        # Discovery read all of these rows a moment ago, so every part of the
        # ghost classification must be revalidated while both locks are held.
        page = locked_pages[ghost.page_id]
        if page is None or page.workspace_id != ghost.workspace_id:
            continue
        review = session.get(
            ReviewItem,
            ghost.review_id,
            with_for_update=True,
            populate_existing=True,
        )
        # ChangeSet is deliberately refreshed without a row lock. The global
        # mutation order is Page -> ReviewItem; introducing a third lock here
        # would require a separately proven order across every review path.
        change_set = session.get(
            ChangeSet,
            ghost.change_set_id,
            populate_existing=True,
        )
        if (
            review is None
            or review.workspace_id != ghost.workspace_id
            or review.status != "pending"
            or review.claimed_by is not None
            or review.review_type != "context_update"
            or review.change_set_id != ghost.change_set_id
            or change_set is None
            or change_set.id != ghost.change_set_id
            or change_set.workspace_id != ghost.workspace_id
            # Only unresolved Context-update workflows may be terminalized by
            # this repair. Terminal, foreign-workflow, and unknown states are
            # evidence that the stale discovery no longer authorizes a close.
            or change_set.status not in {"draft_created", "pending_review"}
        ):
            continue
        if _target_page_ids(session, change_set.id) != (page.id,):
            continue
        classified = _ghost_reason(
            session,
            review.change_set_id,
            page,
            populate_existing=True,
        )
        if classified is None:
            continue
        reason, version_ids = classified
        review.status = "superseded"
        review.claimed_at = None
        change_set.status = "superseded"
        session.flush()
        audit.emit(
            session=session,
            workspace_id=ghost.workspace_id,
            actor_kind="system",
            actor_id=str(default_actor_id()),
            action="review.ghost_closed",
            target_kind="review_item",
            target_id=review.id,
            payload={
                "reason": reason,
                "change_set_id": str(ghost.change_set_id),
                "page_id": str(ghost.page_id),
                "target_version_ids": [str(v) for v in version_ids],
            },
        )
        closed.append(
            GhostReview(
                ghost.workspace_id,
                ghost.workspace_slug,
                ghost.page_id,
                ghost.page_slug,
                ghost.review_id,
                ghost.change_set_id,
                reason,
                version_ids,
            )
        )
    return closed


def _resolve_workspace(session: Session, value: str | None) -> UUID | None:
    if value is None:
        return None
    workspace = session.scalar(select(Workspace).where(Workspace.slug == value))
    if workspace is None:
        try:
            workspace = session.get(Workspace, UUID(value))
        except ValueError:
            workspace = None
    if workspace is None:
        raise ValueError(f"workspace not found: {value}")
    return workspace.id


def _print_report(ghosts: list[GhostReview], *, apply: bool) -> None:
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: {len(ghosts)} ghost "
        "pending context review(s)"
    )
    current_workspace = current_page = None
    for ghost in ghosts:
        if ghost.workspace_id != current_workspace:
            print(f"workspace {ghost.workspace_slug} ({ghost.workspace_id})")
            current_workspace, current_page = ghost.workspace_id, None
        if ghost.page_id != current_page:
            print(f"  page {ghost.page_slug} ({ghost.page_id})")
            current_page = ghost.page_id
        versions = ", ".join(map(str, ghost.target_version_ids)) or "<unresolved>"
        print(
            f"    review {ghost.review_id} reason={ghost.reason} "
            f"change_set={ghost.change_set_id} target_versions={versions}"
        )
    if not apply:
        print("No changes made. Re-run with --apply to close the listed reviews.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close unreachable pending Context reviews."
    )
    parser.add_argument("--workspace", help="workspace slug or UUID (default: all)")
    parser.add_argument(
        "--apply", action="store_true", help="close and audit reported reviews"
    )
    args = parser.parse_args(argv)
    factory = make_session_factory_from_settings(get_settings())
    with factory() as session:
        try:
            workspace_id = _resolve_workspace(session, args.workspace)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        ghosts = close_ghost_reviews(
            session, workspace_id=workspace_id, apply=args.apply
        )
        _print_report(ghosts, apply=args.apply)
        session.commit() if args.apply else session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
