"""Shared projections of saved Context drafts; never changes editing state."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import load_only

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, ContextVersionParent
from backend.models.review import ReviewItem
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    join_in_workspace,
    select_in_workspace,
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


def _reviews_for_versions(session, workspace_id, versions):
    by_ref = {f"context_page_version:{version.id}": version.id for version in versions}
    if not by_ref:
        return {}
    query = select_in_workspace(
        ChangeOperation,
        workspace_id=workspace_id,
        conditions=(
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id.in_({version.page_id for version in versions}),
            ChangeOperation.diff_ref.in_(by_ref),
            ReviewItem.review_type == "context_update",
        ),
    )
    query = join_in_workspace(
        query,
        ChangeOperation,
        ChangeSet,
        workspace_id=workspace_id,
        on=ChangeSet.id == ChangeOperation.change_set_id,
    )
    query = join_in_workspace(
        query,
        ChangeOperation,
        ReviewItem,
        workspace_id=workspace_id,
        on=ReviewItem.change_set_id == ChangeSet.id,
    )
    by_version = {}
    for diff_ref, review in session.execute(
        query.with_only_columns(ChangeOperation.diff_ref, ReviewItem)
    ):
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


def _topology(session, workspace_id, page_ids):
    """Read parent IDs for selected pages without hydrating history bodies."""
    parents = {
        version_id: (page_id, [parent_id] if parent_id else [])
        for version_id, page_id, parent_id in session.execute(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.page_id.in_(page_ids),
                    ContextPageVersion.version_status != "discarded",
                ),
            ).with_only_columns(
                ContextPageVersion.id,
                ContextPageVersion.page_id,
                ContextPageVersion.parent_version_id,
            )
        )
    }
    for version_id, parent_id in session.execute(
        select_in_workspace(
            ContextVersionParent,
            workspace_id=workspace_id,
            conditions=(ContextVersionParent.page_id.in_(page_ids),),
        )
        .with_only_columns(
            ContextVersionParent.version_id, ContextVersionParent.parent_version_id
        )
        .order_by(ContextVersionParent.version_id, ContextVersionParent.ordinal)
    ):
        if version_id in parents:
            parents[version_id][1].append(parent_id)
    return parents


def _behind(page, version_id, parents):
    if page.current_published_version_id is None:
        return False
    pending, seen, active = [(version_id, False)], set(), set()
    while pending:
        current, leaving = pending.pop()
        if leaving:
            active.remove(current)
            seen.add(current)
            continue
        if current == page.current_published_version_id:
            return False
        if current in active:
            return None
        if current in seen:
            continue
        row = parents.get(current)
        if row is None or row[0] != page.id:
            return None
        active.add(current)
        pending.append((current, True))
        pending.extend((parent, False) for parent in row[1])

    return True


def _last_savers(session, workspace_id, versions):
    """Resolve opaque system/MCP principal IDs from existing save audit rows."""
    wanted = [version for version in versions if version.actor_id is None]
    if not wanted:
        return {}
    version_id = AuditEvent.payload["version_id"].as_string()
    events = (
        select(
            version_id.label("version_id"),
            AuditEvent.actor_kind,
            AuditEvent.actor_id,
            func.row_number()
            .over(
                partition_by=version_id,
                order_by=(
                    AuditEvent.payload["editing_revision"]
                    .as_integer()
                    .desc()
                    .nulls_last(),
                    AuditEvent.created_at.desc(),
                    AuditEvent.id.desc(),
                ),
            )
            .label("ordinal"),
        )
        .where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.target_kind == "context_page",
            AuditEvent.target_id.in_({version.page_id for version in wanted}),
            AuditEvent.action == "context_page.draft.save",
            version_id.in_([str(version.id) for version in wanted]),
        )
        .subquery()
    )
    return {
        UUID(version_id): (kind, actor_id)
        for version_id, kind, actor_id in session.execute(
            select(events.c.version_id, events.c.actor_kind, events.c.actor_id).where(
                events.c.ordinal == 1
            )
        )
    }


def project_context_drafts(session, pages, versions, actor):
    """Batch selected draft facts, parents and last-saver identities."""
    if not actor_can_read_drafts(actor):
        return {}
    by_page = {page.id: page for page in pages}
    drafts = [
        version
        for version in versions
        if version.version_status == "draft" and version.page_id in by_page
    ]
    result = {}
    for start in range(0, len(drafts), 350):
        selected = drafts[start : start + 350]
        workspace_id = selected[0].workspace_id
        reviews = _reviews_for_versions(session, workspace_id, selected)
        parents = _topology(
            session, workspace_id, {version.page_id for version in selected}
        )
        savers = _last_savers(session, workspace_id, selected)
        for version in selected:
            page = by_page[version.page_id]
            kind, actor_id = (
                (version.actor_type, str(version.actor_id))
                if version.actor_id
                else savers.get(
                    version.id,
                    (version.created_by_actor_kind, version.created_by_actor_id),
                )
            )
            result[version.id] = ContextDraftInfo(
                author=ContextDraftAuthor(
                    actor_kind=kind,
                    actor_id=actor_id,
                    display_name=version.actor_display_name,
                ),
                parent_version_ids=list(parents.get(version.id, (None, []))[1]),
                last_saved_at=version.last_saved_at,
                is_editing=not any(
                    review.status in _OPEN_REVIEW_STATUSES
                    for review in reviews.get(version.id, [])
                ),
                review=_review_info(reviews.get(version.id, [])),
                is_behind=_behind(page, version.id, parents),
            )
    return result


def project_context_draft(session, page, version, actor):
    """Facts about one explicitly selected draft."""
    if version is None:
        return None
    return project_context_drafts(session, [page], [version], actor).get(version.id)


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
        ContextPageVersion.version_status == "draft",
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
    projections = project_context_drafts(session, [page], selected, actor)
    items = []
    for row in selected:
        info = projections[row.id]
        items.append(
            ContextDraftSummary(
                **info.model_dump(),
                version_id=row.id,
                version_status=row.version_status,
                is_mine=(
                    row.created_by_actor_kind == getattr(actor, "actor_kind", None)
                    and row.created_by_actor_id == getattr(actor, "actor_id", None)
                ),
                state="reviewing"
                if info.review is not None
                and info.review.status in _OPEN_REVIEW_STATUSES
                else "writing",
            )
        )
    return OpenDrafts(
        total=total or 0,
        items=items,
        next_cursor=_encode_cursor(page, selected[-1]) if len(rows) > limit else None,
    )
