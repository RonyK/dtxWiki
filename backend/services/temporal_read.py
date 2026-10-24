"""Bitemporal read primitives (design §17.2-B, N2.5).

The publish timeline reconstructs "what was published at time T" from
ChangeOperation rows: every path that makes a version live leaves an
operation referencing ``context_page_version:<id>`` — REST direct writes
(``create``/``update``, published at creation), MCP publishes (``publish``),
and B-8 archives (``archive``). An undone approval leaves
``publish_revert`` naming the version live again (no version: nothing is).
The operation's ChangeSet.created_at is the publish instant — NOT
``version.created_at``, which is draft-creation time and mis-resolves drafts
published late.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.utils.uuid import parse_uuid_or_none

_PUBLISH_OPERATIONS = ("create", "update", "publish")
_TIMELINE_OPERATIONS = (*_PUBLISH_OPERATIONS, "archive", "publish_revert")
_VERSION_REF_PREFIX = "context_page_version:"

_PUBLISHED_STATUSES = {"published_snapshot", "superseded_snapshot"}


def _naive_utc(value: datetime) -> datetime:
    """DB timestamps are naive UTC; convert aware inputs instead of
    stripping their offset (a +09:00 caller must not skew by 9 hours)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class TimelineEvent:
    page_id: UUID
    version_id: UUID | None
    operation: str  # create | update | publish | archive | publish_revert
    occurred_at: datetime
    change_set_id: UUID
    reason: str | None


def _version_id_from_ref(diff_ref: str | None) -> UUID | None:
    if not diff_ref or not diff_ref.startswith(_VERSION_REF_PREFIX):
        return None
    return parse_uuid_or_none(diff_ref[len(_VERSION_REF_PREFIX) :])


def _draft_write_version_ids(
    session: Session,
    rows: list,
) -> set[UUID]:
    """Version ids whose create/update operations are draft writes, not
    publish instants.

    Two write shapes share the create/update operation kind: REST direct
    writes (the version is live at the operation instant) and draft-creating
    writes (restore_version, autosave — the version goes live later, via a
    separate ``publish`` operation, or never). A create/update event is a
    draft write iff its version has its own ``publish`` operation (which
    carries the true instant) or never reached a published status.
    """
    referenced: set[UUID] = set()
    published_by_publish_op: set[UUID] = set()
    for operation, *_rest in rows:
        version_id = _version_id_from_ref(operation.diff_ref)
        if version_id is None:
            continue
        if operation.operation == "publish":
            published_by_publish_op.add(version_id)
        elif operation.operation in _PUBLISH_OPERATIONS:
            referenced.add(version_id)
    if not referenced:
        return set()
    published_status_ids = set(
        session.scalars(
            select(ContextPageVersion.id)
            .where(ContextPageVersion.id.in_(referenced))
            .where(ContextPageVersion.version_status.in_(_PUBLISHED_STATUSES))
        ).all()
    )
    return {
        version_id
        for version_id in referenced
        if version_id in published_by_publish_op
        or version_id not in published_status_ids
    }


def _publish_operation_version_ids(
    session: Session, page_ids: set[UUID]
) -> set[UUID]:
    if not page_ids:
        return set()
    result: set[UUID] = set()
    for diff_ref in session.scalars(
        select(ChangeOperation.diff_ref)
        .where(ChangeOperation.target_type == "context_page")
        .where(ChangeOperation.target_id.in_(page_ids))
        .where(ChangeOperation.operation == "publish")
    ).all():
        version_id = _version_id_from_ref(diff_ref)
        if version_id is not None:
            result.add(version_id)
    return result


def _is_publish_instant(
    operation, version_id: UUID | None, draft_writes: set[UUID]
) -> bool:
    """Whether a publish-family op is a genuine publish instant.

    The single owner of the dominance rule (previously copied at three sites):
    a create/update/publish op counts iff it names a version AND is not a
    draft write (restore/autosave, whose real instant arrives as a separate
    ``publish`` op or never).
    """
    if operation.operation not in _PUBLISH_OPERATIONS:
        return False
    if version_id is None:
        return False
    if operation.operation in ("create", "update") and version_id in draft_writes:
        return False
    return True


def _pages_published_before(
    session: Session,
    page_ids: set[UUID],
    *,
    before: datetime,
    publish_refs: set[UUID],
) -> set[UUID]:
    """Bulk first-publish history check for one candidate-page batch."""
    if not page_ids:
        return set()
    rows = session.execute(
        select(ChangeOperation, ChangeSet)
        .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
        .where(ChangeOperation.target_type == "context_page")
        .where(ChangeOperation.target_id.in_(page_ids))
        .where(ChangeOperation.operation.in_(_PUBLISH_OPERATIONS))
        .where(ChangeSet.created_at <= before)
    ).all()
    draft_writes = _draft_write_version_ids(session, rows) | publish_refs
    published: set[UUID] = set()
    for operation, _change_set in rows:
        version_id = _version_id_from_ref(operation.diff_ref)
        if _is_publish_instant(operation, version_id, draft_writes):
            published.add(operation.target_id)
    return published


def _versions_published_before(
    session: Session,
    version_ids: set[UUID],
    *,
    before: datetime,
) -> set[UUID]:
    """Which of ``version_ids`` had a genuine publish instant at/before
    ``before`` — used to suppress a later metadata-only re-touch of an
    already-published version from surfacing as a fresh publish event."""
    if not version_ids:
        return set()
    refs = [f"{_VERSION_REF_PREFIX}{version_id}" for version_id in version_ids]
    rows = session.execute(
        select(ChangeOperation, ChangeSet)
        .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
        .where(ChangeOperation.target_type == "context_page")
        .where(ChangeOperation.operation.in_(_PUBLISH_OPERATIONS))
        .where(ChangeOperation.diff_ref.in_(refs))
        .where(ChangeSet.created_at <= before)
    ).all()
    draft_writes = _draft_write_version_ids(session, rows)
    published: set[UUID] = set()
    for operation, _change_set in rows:
        version_id = _version_id_from_ref(operation.diff_ref)
        if version_id in version_ids and _is_publish_instant(
            operation, version_id, draft_writes
        ):
            published.add(version_id)
    return published


def publish_timeline(
    session: Session,
    page_id: UUID,
) -> list[TimelineEvent]:
    """Ascending publish/archive events for one page.

    A version's publish instant is its FIRST genuine publish-family op; a
    later create/update naming the same (already-published) version is a
    metadata re-touch, not a second publish, and is dropped. A ``publish`` op
    is always an instant: the same version publishes again only after an
    undone approval reverted it.
    """
    rows = session.execute(
        select(ChangeOperation, ChangeSet)
        .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
        .where(ChangeOperation.target_type == "context_page")
        .where(ChangeOperation.target_id == page_id)
        .where(ChangeOperation.operation.in_(_TIMELINE_OPERATIONS))
        .order_by(
            ChangeSet.created_at.asc(),
            ChangeSet.id.asc(),
            ChangeOperation.id.asc(),
        )
    ).all()
    draft_writes = _draft_write_version_ids(session, rows)
    events: list[TimelineEvent] = []
    published_versions: set[UUID] = set()
    for operation, change_set in rows:
        version_id = _version_id_from_ref(operation.diff_ref)
        if operation.operation in ("archive", "publish_revert"):
            pass
        elif not _is_publish_instant(operation, version_id, draft_writes):
            continue
        elif version_id in published_versions and operation.operation != "publish":
            continue
        else:
            published_versions.add(version_id)
        events.append(
            TimelineEvent(
                page_id=page_id,
                version_id=version_id,
                operation=operation.operation,
                occurred_at=change_set.created_at,
                change_set_id=change_set.id,
                reason=change_set.reason,
            )
        )
    return events


def resolve_version_as_of(
    session: Session,
    page: ContextPage,
    as_of: datetime,
) -> ContextPageVersion | None:
    """The version published at instant ``as_of`` (None = nothing was).

    Only versions that were live at ``as_of`` resolve (viewer-safe by
    construction): published/superseded snapshots, or a version a ``publish``
    op made live before an approval undo returned it to draft. An ``archive``
    event before ``as_of`` with no later publish yields None. Falls back to the
    published head when a page has a head but no timeline rows (legacy/seeded data).
    """
    naive_as_of = _naive_utc(as_of)
    events = publish_timeline(session, page.id)
    chosen: TimelineEvent | None = None
    for event in events:
        if _naive_utc(event.occurred_at) > naive_as_of:
            break
        chosen = event
    if chosen is None:
        # Legacy fallback: no timeline rows — use the published head when it
        # already existed at as_of.
        if events:
            return None
        head_id = page.current_published_version_id
        if head_id is None:
            return None
        head = session.get(ContextPageVersion, head_id)
        if head is None:
            return None
        return head if _naive_utc(head.created_at) <= naive_as_of else None
    if chosen.operation == "archive" or chosen.version_id is None:
        return None
    version = session.get(ContextPageVersion, chosen.version_id)
    if version is None:
        return None
    # A ``publish`` op proves the version was live at that instant even after
    # an undone approval returned it to draft; other ops need its status.
    if (
        version.version_status not in _PUBLISHED_STATUSES
        and chosen.operation != "publish"
    ):
        return None
    return version


@dataclass(frozen=True)
class WhatChangedEntry:
    concept_path: str
    title: str
    change_kind: str  # created | updated | archived | unpublished
    page_id: UUID
    version_id: UUID | None
    previous_version_id: UUID | None
    changed_at: datetime
    reason: str | None
    change_set_id: UUID


def what_changed(
    session: Session,
    workspace_id: UUID,
    *,
    since: datetime,
    actor,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WhatChangedEntry], bool]:
    """Published-change delta since ``since`` (ascending), actor-visible.

    created = the page's first publish event; archived rows surface the B-8
    cascade (the client's cache-invalidation signal — currently-archived
    pages appear ONLY as archived entries). An undone first publication emits
    unpublished for actors who can still see the draft. Returns (entries, has_more).
    """
    from backend.services.context_visibility import visible_context_version_id
    from backend.services.entity_ref import context_concept_path

    naive_since = _naive_utc(since)
    # One ordered snapshot query: the OFFSET-batched form re-sorted skipped
    # rows per batch and was not snapshot-stable (a concurrent publish shifted
    # the window). what_changed is a delta feed bounded by ``since``, so
    # materializing the whole since-window once, classifying in a single
    # ascending pass, then windowing by offset/limit is both correct and
    # cheaper.
    rows = session.execute(
        select(ChangeOperation, ChangeSet, ContextPage)
        .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
        .join(ContextPage, ChangeOperation.target_id == ContextPage.id)
        .where(ContextPage.workspace_id == workspace_id)
        .where(ChangeOperation.target_type == "context_page")
        .where(ChangeOperation.operation.in_(_TIMELINE_OPERATIONS))
        .where(ChangeSet.created_at > naive_since)
        .order_by(
            ChangeSet.created_at.asc(),
            ChangeSet.id.asc(),
            ChangeOperation.id.asc(),
        )
    ).all()

    page_ids = {page.id for _operation, _change_set, page in rows}
    op_rows = [(operation, change_set) for operation, change_set, _page in rows]
    publish_refs = _publish_operation_version_ids(session, page_ids)
    draft_writes = _draft_write_version_ids(session, op_rows) | publish_refs
    published_pages = _pages_published_before(
        session, page_ids, before=naive_since, publish_refs=publish_refs
    )
    version_ids = {
        version_id
        for operation, _change_set, _page in rows
        if (version_id := _version_id_from_ref(operation.diff_ref)) is not None
    }
    parent_by_version = (
        dict(
            session.execute(
                select(
                    ContextPageVersion.id,
                    ContextPageVersion.parent_version_id,
                ).where(ContextPageVersion.id.in_(version_ids))
            ).all()
        )
        if version_ids
        else {}
    )
    # Versions already published at/before ``since`` — a create/update naming
    # one is a metadata-only re-touch, not a fresh publish, so it must not
    # surface as an 'updated' delta with a misleading previous_version_id.
    seen_versions = _versions_published_before(
        session, version_ids, before=naive_since
    )

    entries: list[WhatChangedEntry] = []
    for operation, change_set, page in rows:
        # Visibility gates every entry: an archived draft-only page must
        # not surface its slug/title/reason to an actor who never saw it.
        if visible_context_version_id(page, actor)[0] is None:
            continue
        if page.lifecycle_status == "archived" and operation.operation != "archive":
            continue
        version_id = _version_id_from_ref(operation.diff_ref)
        if operation.operation == "archive":
            change_kind = "archived"
        elif operation.operation == "publish_revert":
            # An undone approval makes the version it replaced live again. An
            # undone first publication leaves no published version to name.
            if version_id is None:
                change_kind = "unpublished"
            else:
                seen_versions.add(version_id)
                change_kind = "updated"
        else:
            if not _is_publish_instant(operation, version_id, draft_writes):
                continue
            if version_id in seen_versions and operation.operation != "publish":
                continue
            seen_versions.add(version_id)
            change_kind = "updated" if page.id in published_pages else "created"
            published_pages.add(page.id)
        entries.append(
            WhatChangedEntry(
                concept_path=context_concept_path(page.slug),
                title=page.title,
                change_kind=change_kind,
                page_id=page.id,
                version_id=version_id,
                previous_version_id=(
                    parent_by_version.get(version_id)
                    if change_kind == "updated"
                    and version_id is not None
                    and operation.operation != "publish_revert"
                    else None
                ),
                changed_at=change_set.created_at,
                reason=change_set.reason,
                change_set_id=change_set.id,
            )
        )
    window = entries[offset : offset + limit]
    has_more = len(entries) > offset + limit
    return window, has_more
