"""Shared projections of saved Context drafts; never changes editing state."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, cast, exists, func, literal, or_, select
from sqlalchemy.orm import load_only

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    join_in_workspace,
    select_in_workspace,
    select_projection_in_workspace,
)
from backend.schemas.context_drafts import (
    ContextDraftAuthor,
    ContextDraftInfo,
    ContextDraftReview,
    ContextDraftSummary,
    OpenDrafts,
)
from backend.services.read_errors import ReadNotFoundError, ReadValidationError

_OPEN_REVIEW_STATUSES = frozenset({"pending", "changes_requested"})


def _review_conditions(page):
    return (
        ChangeOperation.workspace_id == page.workspace_id,
        ChangeOperation.target_type == "context_page",
        ChangeOperation.target_id == page.id,
        ChangeSet.workspace_id == page.workspace_id,
        ReviewItem.workspace_id == page.workspace_id,
        ReviewItem.review_type == "context_update",
    )


def _review_statement(page, *, conditions=()):
    query = select_in_workspace(
        ChangeOperation,
        workspace_id=page.workspace_id,
        conditions=(*_review_conditions(page), *conditions),
    )
    query = join_in_workspace(
        query,
        ChangeOperation,
        ChangeSet,
        workspace_id=page.workspace_id,
        on=ChangeSet.id == ChangeOperation.change_set_id,
    )
    return join_in_workspace(
        query,
        ChangeOperation,
        ReviewItem,
        workspace_id=page.workspace_id,
        on=ReviewItem.change_set_id == ChangeSet.id,
    )


def _review_exists(page, *, only_open=False):
    # UUID storage differs by dialect (SQLite hex, PostgreSQL UUID). Normalize
    # both sides without changing the persisted diff_ref format.
    version_ref = literal("context_page_version:") + func.replace(
        cast(ContextPageVersion.id, String), "-", ""
    )
    conditions = (func.replace(ChangeOperation.diff_ref, "-", "") == version_ref,)
    if only_open:
        conditions += (ReviewItem.status.in_(tuple(_OPEN_REVIEW_STATUSES)),)
    return exists(
        _review_statement(page, conditions=conditions)
        .with_only_columns(literal(1))
        .correlate(ContextPageVersion)
    )


def _reviews_for_versions(session, page, versions):
    by_ref = {f"context_page_version:{version.id}": version.id for version in versions}
    if not by_ref:
        return {}
    rows = session.execute(
        _review_statement(
            page, conditions=(ChangeOperation.diff_ref.in_(by_ref),)
        ).with_only_columns(ChangeOperation.diff_ref, ReviewItem)
    ).all()
    by_version = {}
    for diff_ref, review in rows:
        by_version.setdefault(by_ref[diff_ref], {})[review.id] = review
    return {
        version_id: list(reviews.values()) for version_id, reviews in by_version.items()
    }


def _review_info(reviews):
    if not reviews:
        return None
    review = max(
        reviews,
        key=lambda row: (
            row.status in _OPEN_REVIEW_STATUSES,
            row.created_at,
            str(row.id),
        ),
    )
    return ContextDraftReview(id=review.id, status=review.status)


def _behind_for_versions(session, page, versions):
    if page.current_published_version_id is None:
        return {version.id: False for version in versions}
    parents = {version.id: version.parent_version_id for version in versions}
    frontier = {
        parent for parent in parents.values() if parent is not None
    } - set(parents) - {page.current_published_version_id}
    if frontier:
        ancestry = select_projection_in_workspace(
            ContextPageVersion,
            workspace_id=page.workspace_id,
            columns=(ContextPageVersion.id, ContextPageVersion.parent_version_id),
            predicates=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id.in_(frontier),
            ),
        ).cte("draft_ancestry", recursive=True)
        parent_rows = select_projection_in_workspace(
            ContextPageVersion,
            workspace_id=page.workspace_id,
            columns=(ContextPageVersion.id, ContextPageVersion.parent_version_id),
            predicates=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == ancestry.c.parent_version_id,
                ContextPageVersion.id != page.current_published_version_id,
            ),
        )
        # UNION deduplicates parent edges, so a legacy cycle terminates.
        # Reaching the published head requires no older-history lookup.
        ancestry = ancestry.union(parent_rows)
        rows = session.execute(
            select(ancestry.c.id, ancestry.c.parent_version_id)
        ).all()
        parents.update(rows)
    result = {}
    for version in versions:
        seen = set()
        cursor = version.id
        while cursor is not None:
            if cursor == page.current_published_version_id:
                result[version.id] = False
                break
            if cursor in seen or cursor not in parents:
                result[version.id] = None
                break
            seen.add(cursor)
            cursor = parents[cursor]
        else:
            result[version.id] = True
    return result


def project_context_draft(session, page, version, actor):
    """Facts about the selected draft, including a closed review when present."""
    if (
        version is None
        or version.version_status not in {"editing", "draft"}
        or not actor_can_read_drafts(actor)
    ):
        return None
    reviews = _reviews_for_versions(session, page, [version]).get(version.id, [])
    return _draft_info(
        version, reviews, _behind_for_versions(session, page, [version])[version.id]
    )


def _draft_info(version, reviews, is_behind, *, display_name=None):
    return ContextDraftInfo(
        author=ContextDraftAuthor(
            actor_kind=version.created_by_actor_kind,
            actor_id=version.created_by_actor_id,
            display_name=version.actor_display_name or display_name,
        ),
        last_saved_at=version.last_saved_at,
        is_editing=version.version_status == "editing",
        review=_review_info(reviews),
        is_behind=is_behind,
    )


def _decode_cursor(cursor, workspace_id, page_id):
    if cursor is None:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        if (
            payload["v"] != 1
            or payload["workspace_id"] != str(workspace_id)
            or payload["page_id"] != str(page_id)
        ):
            raise ValueError
        saved_at = datetime.fromisoformat(payload["last_saved_at"])
        if saved_at.tzinfo is None or not isinstance(payload["version_id"], str):
            raise ValueError
        return saved_at, UUID(payload["version_id"])
    except (
        ValueError,
        KeyError,
        TypeError,
        binascii.Error,
        UnicodeDecodeError,
        RecursionError,
    ) as exc:
        raise ReadValidationError(
            "cursor must be an opaque draft pagination token"
        ) from exc


def _encode_cursor(page, version):
    payload = {
        "v": 1,
        "workspace_id": str(page.workspace_id),
        "page_id": str(page.id),
        "last_saved_at": version.last_saved_at.isoformat(),
        "version_id": str(version.id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def list_context_drafts(session, *, workspace_id, page_id, actor, limit=5, cursor=None):
    """Count and page open drafts without loading their Markdown bodies."""
    from backend.services.actor_visible_read import context_page_visible

    if not 1 <= limit <= 100:
        raise ReadValidationError("limit must be between 1 and 100")
    boundary = _decode_cursor(cursor, workspace_id, page_id)
    page = get_in_workspace(
        session, ContextPage, workspace_id=workspace_id, resource_id=page_id
    )
    if not context_page_visible(page, actor, workspace_id=workspace_id):
        raise ReadNotFoundError("context page not found")
    return _project_open_drafts(session, page, actor, limit=limit, boundary=boundary)


def project_open_drafts(session, page, actor):
    """Embed discovery metadata without denying an independently allowed read.

    Archived review evidence and explicit version reads keep their existing
    body access. A page outside ordinary discovery simply has no open list.
    """
    from backend.services.actor_visible_read import context_page_visible

    if page is None or not context_page_visible(
        page, actor, workspace_id=page.workspace_id
    ):
        return OpenDrafts()
    return _project_open_drafts(session, page, actor)


def _project_open_drafts(session, page, actor, *, limit=5, boundary=None):
    if not actor_can_read_drafts(actor):
        return OpenDrafts()
    workspace_id, page_id = page.workspace_id, page.id
    conditions = (
        ContextPageVersion.workspace_id == workspace_id,
        ContextPageVersion.page_id == page_id,
        or_(
            ContextPageVersion.version_status == "editing",
            (ContextPageVersion.version_status == "draft")
            & (~_review_exists(page) | _review_exists(page, only_open=True)),
        ),
    )
    scoped_query = select_in_workspace(
        ContextPageVersion, workspace_id=workspace_id, conditions=conditions
    )
    total = session.scalar(
        scoped_query.with_only_columns(func.count(), maintain_column_froms=True)
    )
    query = scoped_query.options(
        load_only(
            ContextPageVersion.id,
            ContextPageVersion.page_id,
            ContextPageVersion.workspace_id,
            ContextPageVersion.parent_version_id,
            ContextPageVersion.version_status,
            ContextPageVersion.created_by_actor_kind,
            ContextPageVersion.created_by_actor_id,
            ContextPageVersion.actor_type,
            ContextPageVersion.actor_id,
            ContextPageVersion.actor_display_name,
            ContextPageVersion.last_saved_at,
        )
    )
    if boundary is not None:
        saved_at, version_id = boundary
        pagination_conditions = (
            or_(
                ContextPageVersion.last_saved_at < saved_at,
                (ContextPageVersion.last_saved_at == saved_at)
                & (ContextPageVersion.id > version_id),
            ),
        )
        query = query.where(*pagination_conditions)
    rows = list(
        session.scalars(
            query.order_by(
                ContextPageVersion.last_saved_at.desc(), ContextPageVersion.id.asc()
            ).limit(limit + 1)
        )
    )
    selected = rows[:limit]
    reviews = _reviews_for_versions(session, page, selected)
    behind = _behind_for_versions(session, page, selected)
    items = []
    for row in selected:
        row_reviews = reviews.get(row.id, [])
        info = _draft_info(row, row_reviews, behind[row.id])
        items.append(
            ContextDraftSummary(
                **info.model_dump(),
                version_id=row.id,
                version_status=row.version_status,
                is_mine=(
                    row.created_by_actor_kind == actor.actor_kind
                    and row.created_by_actor_id == actor.actor_id
                ),
                state="reviewing"
                if row.version_status != "editing"
                and any(
                    review.status in _OPEN_REVIEW_STATUSES for review in row_reviews
                )
                else "writing",
            )
        )
    return OpenDrafts(
        total=total or 0,
        items=items,
        next_cursor=_encode_cursor(page, selected[-1]) if len(rows) > limit else None,
    )
