"""Derived orphan discovery and reclamation for Context-owned media.

P3.1/P3.4 (doc/design/api-surface-redesign.md §5.5): ContextMediaSession and
its claim step are gone -- every asset is page-owned from the moment it is
registered, via the caller's own open "editing" row
(``created_for_version_id``). There is exactly one orphan shape now: a
page-owned asset with zero surviving ``ContextMediaUsage`` rows, i.e. an
upload never (or no longer) written into any version's Markdown.

That shape alone is not enough to call it abandoned, though. An asset just
uploaded into a still-open "editing" session also has zero usage rows --
that is what "not yet inserted into the body" looks like for content that
is actively being worked on, not orphaned. ``_page_orphan_statement``
therefore excludes any asset whose ``created_for_version_id`` is currently
status ``"editing"`` outright, not merely with a longer grace window:
disconnects, MCP session teardown, and elapsed wall-clock time are not
disposal signals for an open editing session (see R-09,
review/2026-09-04-minor-fixes-vs-feature-epic1-code-review.md). Once that
version is submitted (draft), published, or discarded, the protection lifts
and the asset becomes an ordinary orphan candidate, subject to the same
retention-hours grace period as before.

``CONTEXT_PAGE_LIFECYCLE_STATUSES`` has no hard-delete state (backend/
models/context.py) and nothing in ``backend/`` deletes a ``ContextPageVersion``
row, so "the Page was deleted" cannot happen yet. The same predicate would
also cover that case if a future history-pruning feature ever does
CASCADE-delete a ``ContextPageVersion`` (``ContextMediaUsage`` FKs to
``context_page_version`` with ``ondelete="CASCADE"``) -- but read the
retention-hours setting's comment and ``OrphanCandidate.orphaned_since``'s
docstring before tuning this shape's window, because right now it is
entirely an editing grace period, not a cleanup delay.

Orphan state is not stored as a column (plan D-7 explicitly rejects a new
"orphan" status: a derived query is the single source of truth). This
module is that derived query, plus the sweep that reclaims orphans past a
configurable retention threshold by tombstoning them through the existing
``context_media`` write path -- never a hand-written ``DELETE`` -- so the
existing ``ContextMediaGcCandidate`` producer, quota release, and RESTRICT
foreign keys stay intact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.context_media.application import DeleteUnusedMedia
from backend.context_media.domain import (
    ActorIdentity,
    AssociationState,
    ContextMediaNotFoundError,
    MediaInUseError,
    MediaUsedInHistoryError,
    StaleUsageTokenError,
)
from backend.context_media.ports import ContextMediaRepository
from backend.models.context import ContextPage
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaGcCandidate,
)
from backend.repositories.context_media_orphans import (
    context_media_orphan_clause,
)

_SWEEP_ACTOR = ActorIdentity("system", "context_media_orphan_sweep")
"""The sweep is a system process, not a specific user's action. It only
ever reaches DeleteUnusedMedia's author-restriction check
(context_media/application/use_cases.py) for an asset _page_orphan_statement
has already proven is NOT protected by an open editing session, so that
check can never actually fire against this identity -- it exists to satisfy
the required `actor` parameter, not to claim the sweep IS the uploader."""


@dataclass(frozen=True, slots=True)
class OrphanCandidate:
    """One derived orphan, projected for the read-side listing endpoints."""

    asset_id: UUID
    workspace_id: UUID
    page_id: UUID
    page_title: str | None
    """Title of the owning Page, resolved here rather than left to the
    caller: a bare page_id is not something an operator can act on, and
    ContextPage is workspace-plane data this query is already holding the
    right session for.

    The uploader's name is deliberately NOT resolved here. It lives on
    ``users``, an identity-plane table (doc/design/
    workspace-database-separation-plan.md §2), so reading it on this
    workspace-plane session would break the moment a routing overlay puts
    the two in different databases. The endpoints resolve it on the control
    session and pass it into ``orphan_read_projection`` instead.
    """
    original_filename: str
    canonical_media_type: str
    logical_bytes: int
    association_state: str
    created_by_actor_kind: str
    created_by_actor_id: str
    created_at: datetime
    orphaned_since: datetime
    """When this asset first became structurally unreachable. Currently
    always the asset's own ``created_at`` -- a page-bound asset has no other
    "became orphaned" event, since it can gain a usage edge at any time
    until it is tombstoned, and an asset still protected by an open editing
    session never appears here at all (see module docstring).

    Caveat for a future history-pruning feature (none exists today): if a
    ``ContextPageVersion`` is ever CASCADE-deleted, an asset created a year
    ago whose only usage was in a version pruned yesterday would still
    report ``orphaned_since`` from a year ago and be reclaimed on the very
    next tick, not after a fresh retention window starting at the prune.
    There is no recorded orphaning event to anchor on instead, and D-7
    forbids a new state column to hold one -- this is a known, accepted
    gap, not an oversight, but real if that feature lands."""


@dataclass(frozen=True, slots=True)
class OrphanSweepResult:
    inspected_count: int
    reclaimed_count: int
    drained_count: int
    run_at: datetime


def _page_orphan_statement(workspace_id: UUID):
    """Page-bound orphans: owned by a Page, never referenced by any of its
    versions' Markdown (draft, published, or historical), AND not currently
    protected by an open "editing" session (see module docstring).

    The predicate itself lives in ``backend/repositories/context_media_orphans``
    so the workspace attachment listing can filter on exactly the definition
    this sweep reclaims by, instead of a second copy that would drift."""

    return select(ContextMediaAsset).where(
        ContextMediaAsset.workspace_id == workspace_id,
        ContextMediaAsset.association_state != AssociationState.TOMBSTONED.value,
        context_media_orphan_clause(),
    )


def list_orphans(
    session: Session, *, workspace_id: UUID, now: datetime | None = None
) -> tuple[OrphanCandidate, ...]:
    """All current orphans, regardless of age.

    Visibility is deliberately not retention-gated: a user should be able to
    see (and, via the existing delete-unused-media action, remove) an orphan
    immediately, even if it is not yet old enough for the automatic sweep to
    reclaim it. Only removal is age-gated -- see
    ``ContextMediaOrphanSweepService``. ``now`` is accepted for parity with
    the sweep's own explicit ``run_at`` (kept so a listing taken mid-sweep
    and the sweep's own decisions read from the same clock), though nothing
    in this query currently depends on it.
    """

    del now
    page_assets = tuple(session.scalars(_page_orphan_statement(workspace_id)))
    page_titles = _page_titles(
        session,
        workspace_id=workspace_id,
        page_ids={asset.page_id for asset in page_assets},
    )
    candidates = [
        OrphanCandidate(
            asset_id=asset.id,
            workspace_id=asset.workspace_id,
            page_id=asset.page_id,
            page_title=page_titles.get(asset.page_id),
            original_filename=asset.original_filename,
            canonical_media_type=asset.canonical_media_type,
            logical_bytes=asset.logical_bytes,
            association_state=asset.association_state,
            created_by_actor_kind=asset.created_by_actor_kind,
            created_by_actor_id=asset.created_by_actor_id,
            created_at=asset.created_at,
            orphaned_since=asset.created_at,
        )
        for asset in page_assets
    ]
    candidates.sort(key=lambda item: (item.orphaned_since, item.asset_id))
    return tuple(candidates)


def _page_titles(
    session: Session, *, workspace_id: UUID, page_ids: set[UUID]
) -> dict[UUID, str]:
    """One batched title lookup for every page-bound orphan in the listing.

    Per-row `session.get` would be N queries for a list whose whole point is
    to be scanned, so this mirrors ``resolve_actor_display_names``' one-query
    shape.
    """

    if not page_ids:
        return {}
    rows = session.execute(
        select(ContextPage.id, ContextPage.title).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id.in_(page_ids),
        )
    )
    return {page_id: title for page_id, title in rows if title}


def orphan_read_projection(
    candidate: OrphanCandidate, *, uploaded_by_display_name: str
) -> dict[str, Any]:
    """Wire-shape a candidate for ``ContextMediaOrphanPayload`` (api/ imports
    services, never models -- mirrors job_read_service.job_read_projection).

    ``uploaded_by_display_name`` is a required argument rather than something
    resolved here, because resolving it means reading ``users`` -- an
    identity-plane table this workspace-plane service must not touch (see
    ``OrphanCandidate.page_title``). Making it required means a caller that
    forgets it fails loudly instead of silently rendering a raw actor id,
    which is what this field exists to stop.
    """

    return {
        "asset_id": candidate.asset_id,
        "workspace_id": candidate.workspace_id,
        "page_id": candidate.page_id,
        "page_title": candidate.page_title,
        "uploaded_by_display_name": uploaded_by_display_name,
        "original_filename": candidate.original_filename,
        "media_type": candidate.canonical_media_type,
        "bytes": candidate.logical_bytes,
        "association_state": candidate.association_state,
        "created_by_actor_kind": candidate.created_by_actor_kind,
        "created_by_actor_id": candidate.created_by_actor_id,
        "created_at": candidate.created_at,
        "orphaned_since": candidate.orphaned_since,
    }


class ContextMediaOrphanSweepService:
    """Retention-gated reclamation, run by the ``context_media_orphan_sweep``
    job (one per workspace, seeded like ``media_install_reconcile``).

    The retention grace period runs BEFORE tombstoning, not after: an
    orphan stays ``active`` -- and therefore rescuable, simply by being
    used, which drops it out of the derived orphan query with no state to
    unwind -- for the full ``retention_hours`` window. Only once that
    threshold passes does phase 1 tombstone it, and phase 2 drains its GC
    candidate immediately in the same tick. There is deliberately no
    window after tombstoning during which "reuse" needs to be detected and
    cancelled, because a tombstoned asset can never be reused (see
    ``ContextMediaGcCandidate``'s docstring for why ``cancelled`` has no
    writer).

    1. Tombstone every orphan at least ``retention_hours`` old, through the
       existing write path -- ``DeleteUnusedMedia``/``tombstone_and_enqueue_gc``
       -- so the existing ``ContextMediaGcCandidate`` producer fires and the
       logical quota charge releases. Never a hand-written ``DELETE``.
    2. Drain: for every ``queued`` GC candidate in the workspace -- this
       tick's new ones and any left over from the pre-existing manual
       delete-unused-media action, which enqueues via the same
       ``tombstone_and_enqueue_gc`` producer but has no drainer of its own
       -- delete the candidate, its upload-request marker, and the asset
       row (``SqlAlchemyContextMediaRepository.drain_gc_candidate``).
       This transaction also unregisters the asset's durable Shared Media
       owner edge. If it was the final owner, it queues a shared blob-GC
       candidate. The separately dispatched ``media_blob_gc`` job consumes
       only committed candidates and owns physical trash/purge transactions;
       no filesystem mutation occurs inside this domain sweep.

    ``retention_hours <= 0`` disables phase 1 (0 disables reclamation);
    phase 2 always runs so a pre-existing queued candidate is not stranded
    forever just because retention is currently disabled.

    Takes a ``context_media_repository_factory`` rather than importing
    ``SqlAlchemyContextMediaRepository`` directly: G1a (tests/static/
    test_architecture_boundaries.py) confines concrete
    ``context_media.adapters`` construction to ``backend/app/container.py``,
    the media composition root. The container already exposes exactly this
    factory (``ServiceRegistry.context_media_repository_factory``) for the
    same reason ``ContextMediaUploadService`` takes one.
    """

    def __init__(
        self,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository],
    ) -> None:
        self._context_media_repository_factory = context_media_repository_factory

    def sweep(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        retention_hours: int,
        now: datetime | None = None,
    ) -> OrphanSweepResult:
        run_at = now or datetime.now(UTC)
        repository = self._context_media_repository_factory(session)
        inspected_count = 0
        reclaimed_count = 0

        if retention_hours > 0:
            cutoff = run_at - timedelta(hours=retention_hours)

            for asset in session.scalars(_page_orphan_statement(workspace_id)):
                inspected_count += 1
                if asset.created_at > cutoff:
                    continue
                projection = repository.get_page_asset_usage(
                    workspace_id, asset.page_id, asset.id
                )
                if projection is None:
                    continue
                try:
                    DeleteUnusedMedia().execute(
                        repository,
                        workspace_id=workspace_id,
                        page_id=asset.page_id,
                        asset_id=asset.id,
                        usage_token=projection.usage_token,
                        actor=_SWEEP_ACTOR,
                    )
                    reclaimed_count += 1
                except (
                    ContextMediaNotFoundError,
                    StaleUsageTokenError,
                    MediaInUseError,
                    MediaUsedInHistoryError,
                ):
                    continue

        drained_count = 0
        queued_asset_ids = session.scalars(
            select(ContextMediaGcCandidate.asset_id).where(
                ContextMediaGcCandidate.workspace_id == workspace_id,
                ContextMediaGcCandidate.state == "queued",
            )
        ).all()
        for asset_id in queued_asset_ids:
            if repository.drain_gc_candidate(
                workspace_id=workspace_id, asset_id=asset_id
            ):
                drained_count += 1

        return OrphanSweepResult(
            inspected_count=inspected_count,
            reclaimed_count=reclaimed_count,
            drained_count=drained_count,
            run_at=run_at,
        )
