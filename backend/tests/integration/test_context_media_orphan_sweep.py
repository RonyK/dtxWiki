"""Orphaned Context-media reclamation and retention boundaries.

Covers page-bound uploads after their own editing version closes, the retention
boundary (which gates tombstoning, not visibility), and the drain step that
removes a tombstoned asset's bookkeeping rows in the same tick -- both for
orphans this sweep itself reclaims and for a candidate queued by the
pre-existing manual delete-unused-media action.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from backend.context_media.adapters.sqlalchemy import SqlAlchemyContextMediaRepository
from backend.context_media.application import (
    DeleteUnusedMedia,
    RegisterMediaAsset,
    SyncVersionUsage,
)
from backend.context_media.domain import ActorIdentity, RegisterContextMediaAsset
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaGcCandidate,
    ContextMediaLogicalCharge,
    ContextMediaUploadRequest,
)
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.services.context_media_orphan_sweep import (
    ContextMediaOrphanSweepService,
    list_orphans,
)
from backend.spi.types import ActorContext

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _seed_page(
    session, workspace_id: UUID, actor: ActorIdentity, *, title: str = "Orphan sweep"
):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"orphan-sweep-{uuid4().hex[:8]}",
        title=title,
        current_version_id=None,
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="",
        content_hash="0" * 64,
        actor_type="human",
        actor_id=None,
        version_status="editing",
        created_by_actor_kind=actor.kind,
        created_by_actor_id=actor.id,
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    session.flush()
    return page, version


def _seed_shared_image(session, workspace_id: UUID):
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=NOW,
            )
        )
        session.flush()
    blob_id = uuid4()
    inspection_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id,
            id=blob_id,
            sha256="a" * 64,
            next_epoch=2,
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=68,
            storage_key=f"w/{workspace_id.hex}/a/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=NOW,
        )
    )
    session.flush()
    session.add(
        StoredBlobInspectionRow(
            workspace_id=workspace_id,
            id=inspection_id,
            blob_id=blob_id,
            blob_epoch=1,
            registry_version="registry-v1",
            detector_id="png-magic",
            detector_version="1",
            format_id="png",
            canonical_media_type="image/png",
            validation_schema_version="png-v1",
            metadata_json={},
            scanner_verdict="not_configured",
            scanner_provider=None,
            scanner_version=None,
            ordinary_available=True,
            created_at=NOW,
        )
    )
    session.flush()
    return BlobReference(workspace_id, blob_id, BlobEpoch(1)), inspection_id


def _candidate(
    workspace_id: UUID,
    page_id: UUID,
    created_for_version_id: UUID,
    blob: BlobReference,
    inspection_id: UUID,
    actor: ActorIdentity,
    *,
    asset_id: UUID | None = None,
) -> RegisterContextMediaAsset:
    return RegisterContextMediaAsset(
        workspace_id=workspace_id,
        asset_id=asset_id or uuid4(),
        page_id=page_id,
        created_for_version_id=created_for_version_id,
        blob=blob,
        inspection_id=inspection_id,
        original_filename="pixel.png",
        canonical_media_type="image/png",
        logical_bytes=68,
        policy_revision="context-mvp-v1",
        actor=actor,
    )


def _pin_created_at(
    session, workspace_id: UUID, asset_id: UUID, when: datetime
) -> None:
    """ContextMediaAsset.created_at defaults to the real wall clock at
    insert time (Base's utc_now default), not the fixed NOW used elsewhere
    in these tests -- pin it explicitly wherever a test's assertion depends
    on the asset's age relative to a retention cutoff, or the test becomes
    flaky depending on how close the real clock happens to be to NOW."""

    session.execute(
        update(ContextMediaAsset)
        .where(
            ContextMediaAsset.workspace_id == workspace_id,
            ContextMediaAsset.id == asset_id,
        )
        .values(created_at=when)
    )
    session.flush()


def _gc_candidate(session, workspace_id, asset_id):
    return session.scalar(
        select(ContextMediaGcCandidate).where(
            ContextMediaGcCandidate.workspace_id == workspace_id,
            ContextMediaGcCandidate.asset_id == asset_id,
        )
    )


def _asset_row(session, workspace_id, asset_id):
    return session.get(
        ContextMediaAsset, {"workspace_id": workspace_id, "id": asset_id}
    )


def _assert_fully_drained(session, workspace_id, asset_id) -> None:
    """Reclamation now means row-level teardown, not just tombstoning: the
    asset, its GC candidate, and any upload-request marker are all gone."""

    assert _asset_row(session, workspace_id, asset_id) is None
    assert _gc_candidate(session, workspace_id, asset_id) is None
    assert (
        session.scalar(
            select(ContextMediaUploadRequest).where(
                ContextMediaUploadRequest.workspace_id == workspace_id,
                ContextMediaUploadRequest.asset_id == asset_id,
            )
        )
        is None
    )


def test_page_bound_asset_with_no_surviving_usage_is_reclaimed(
    session_factory,
) -> None:
    """The dominant real-world page-bound orphan: uploaded onto a Page but
    never (or no longer) referenced by any version's Markdown.
    ``replace_version_usage`` deletes-then-reinserts usage rows for a given
    page_version_id (context_media/adapters/sqlalchemy.py:466-472), so
    syncing the same version a second time without the image is the
    production path by which a once-used asset's last usage row
    disappears -- the same effect a future history-pruning CASCADE
    (ContextMediaUsage FKs to context_page_version with
    ondelete="CASCADE") would have."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        page.latest_draft_version_id = version.id
        session.flush()
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            body_markdown=f"![pixel]({asset.logical_reference})",
        )
        assert (
            list_orphans(
                session,
                actor=ActorContext("system", "sweep-test"),
                workspace_id=workspace_id,
            )
            == ()
        )

        # The user edits the draft again and removes the image.
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            body_markdown="no image here",
        )
        asset_id = asset.asset_id
        _pin_created_at(session, workspace_id, asset_id, NOW)

    with session_factory.begin() as session:
        orphans = list_orphans(
            session,
            actor=ActorContext("system", "sweep-test"),
            workspace_id=workspace_id,
        )
        assert [item.asset_id for item in orphans] == [asset_id]
        assert orphans[0].page_id == page.id
        # The page title is resolved here (workspace-plane), not left as a
        # bare id for the read API to guess at.
        assert orphans[0].page_title == "Orphan sweep"

        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=1,
            now=NOW + timedelta(hours=2),
        )
        assert result.reclaimed_count == 1
        assert result.drained_count == 1

    with session_factory.begin() as session:
        _assert_fully_drained(session, workspace_id, asset_id)
        assert asset_id not in {
            item.asset_id
            for item in list_orphans(
                session,
                actor=ActorContext("system", "sweep-test"),
                workspace_id=workspace_id,
            )
        }


def test_page_bound_orphan_with_a_blank_page_title_resolves_page_title_to_none(
    session_factory,
) -> None:
    """`_page_titles` drops a falsy title rather than surfacing an empty
    string that would render as a blank name. A missing title must not
    blank the whole row, though -- every other field on the candidate must
    still come through."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor, title="")
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        asset_id = asset.asset_id
        page_id = page.id
        _pin_created_at(session, workspace_id, asset_id, NOW)

    with session_factory.begin() as session:
        orphans = list_orphans(
            session,
            actor=ActorContext("system", "sweep-test"),
            workspace_id=workspace_id,
        )
        assert [item.asset_id for item in orphans] == [asset_id]
        assert orphans[0].page_id == page.id
        assert orphans[0].page_id == page_id
        assert orphans[0].page_title is None
        assert orphans[0].original_filename == "pixel.png"


def test_page_bound_asset_referenced_by_a_superseded_version_is_not_reclaimed(
    session_factory,
) -> None:
    """Edge case the user specifically asked about: an asset still used
    anywhere must not be reclaimed. A single ContextMediaAsset can only
    ever belong to one Page (page_id is fixed on the asset row and
    ContextMediaUsage FKs to that same page_id) -- images are not shared
    cross-Page in this schema -- so the reachable version of "used
    elsewhere" is a *different version of the same Page* (a superseded
    draft), which the historical_reference_count guard exists for."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            body_markdown=f"![pixel]({asset.logical_reference})",
        )
        # A newer draft version supersedes version and drops the image --
        # version's usage row is untouched, so it becomes purely
        # historical rather than disappearing.
        version_2 = ContextPageVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="",
            content_hash="1" * 64,
            actor_type="human",
            actor_id=None,
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=str(uuid4()),
        )
        session.add(version_2)
        session.flush()
        page.latest_draft_version_id = version_2.id
        session.flush()
        asset_id = asset.asset_id
        _pin_created_at(session, workspace_id, asset_id, NOW)

        usage = repository.get_page_asset_usage(workspace_id, page.id, asset_id)
        assert usage is not None
        assert usage.latest_draft_reference_count == 0
        assert usage.historical_reference_count == 1

        assert (
            list_orphans(
                session,
                actor=ActorContext("system", "sweep-test"),
                workspace_id=workspace_id,
            )
            == ()
        )

        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=1,
            now=NOW + timedelta(hours=2),
        )
        assert result.reclaimed_count == 0
        assert result.drained_count == 0

    with session_factory.begin() as session:
        row = _asset_row(session, workspace_id, asset_id)
        assert row is not None and row.association_state == "active"


def test_open_editing_upload_is_protected_without_age_limit(session_factory) -> None:
    """Replaces expired-session retention: editing has no TTL or age limit."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        _pin_created_at(session, workspace_id, asset.asset_id, NOW)
        # A separate visible head does not remove protection of the upload's
        # own private row (the page-first pointer shape).
        visible = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="visible",
            content_hash="1" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="other",
            version_status="draft",
        )
        session.add(visible)
        session.flush()
        page.latest_draft_version_id = visible.id
        session.flush()
        future = NOW + timedelta(days=3650)
        assert (
            list_orphans(
                session,
                actor=ActorContext("system", "sweep-test"),
                workspace_id=workspace_id,
                now=future,
            )
            == ()
        )
        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session, workspace_id=workspace_id, retention_hours=1, now=future
        )
        assert (
            result.inspected_count
            == result.reclaimed_count
            == result.drained_count
            == 0
        )
        assert (
            _asset_row(session, workspace_id, asset.asset_id).association_state
            == "active"
        )
        # Once submitted, ordinary asset-age retention applies immediately.
        version.version_status = "draft"
        session.flush()
        orphans = list_orphans(
            session,
            actor=ActorContext("system", "sweep-test"),
            workspace_id=workspace_id,
            now=future,
        )
        assert [item.asset_id for item in orphans] == [asset.asset_id]
        assert orphans[0].orphaned_since == NOW
        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session, workspace_id=workspace_id, retention_hours=1, now=future
        )
        assert result.reclaimed_count == result.drained_count == 1
        _assert_fully_drained(session, workspace_id, asset.asset_id)


def test_autosave_via_service_keeps_editing_status_and_protects_unattached_upload(
    session_factory, page_service
) -> None:
    """P8.1 regression: the P6-era `_set_current_version` eager promotion
    ("editing" -> "draft" on the very first autosave) silently reopened this
    exact protection window (`_page_orphan_statement`'s
    `created_for_version_is_editing` gate only matches `version_status ==
    "editing"`) on top of breaking the P2.2 author-only read privacy this
    session's own P8.1 fix restores. Prove the fix through the real
    `autosave_page` code path (not a hand-built row), across more than one
    autosave, not just the first."""

    actor_kind, actor_id = "human", str(uuid4())
    created = page_service.create_page_first(
        workspace_slug="default",
        title="Media protection target",
        body_md="",
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_provenance="browser_human",
    )

    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        actor = ActorIdentity(actor_kind, actor_id)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                created.id,
                created.version_id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        asset_id = asset.asset_id
        _pin_created_at(session, workspace_id, asset_id, NOW)

    first = page_service.autosave_page(
        page_id=created.id,
        workspace_id=workspace_id,
        title=created.title,
        body_md="# First autosave, no image reference\n",
        actor_kind=actor_kind,
        actor_id=actor_id,
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_human",
        idempotency_key="orphan-sweep-autosave-1",
        expected_editing_revision=created.editing_revision,
    )
    assert first.version_status == "editing"
    assert first.version_id == created.version_id

    second = page_service.autosave_page(
        page_id=created.id,
        workspace_id=workspace_id,
        title=created.title,
        body_md="# Second autosave, still no image reference\n",
        actor_kind=actor_kind,
        actor_id=actor_id,
        base_version_id=first.version_id,
        base_content_hash=first.content_hash,
        actor_provenance="browser_human",
        idempotency_key="orphan-sweep-autosave-2",
        expected_editing_revision=first.editing_revision,
    )
    assert second.version_status == "editing"
    assert second.version_id == created.version_id

    with session_factory.begin() as session:
        future = NOW + timedelta(days=3650)
        assert (
            list_orphans(
                session,
                actor=ActorContext("system", "sweep-test"),
                workspace_id=workspace_id,
                now=future,
            )
            == ()
        )
        row = _asset_row(session, workspace_id, asset_id)
        assert row is not None and row.association_state == "active"


def test_submitted_uninserted_upload_is_reclaimed_exactly_once(session_factory) -> None:
    """Replaces claim reclassification: closing editing produces one candidate."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        _pin_created_at(session, workspace_id, asset.asset_id, NOW)
        version.version_status = "draft"
        session.flush()
        orphans = list_orphans(
            session,
            actor=ActorContext("system", "sweep-test"),
            workspace_id=workspace_id,
        )
        assert [(item.asset_id, item.page_id) for item in orphans] == [
            (asset.asset_id, page.id)
        ]
        service = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository)
        first = service.sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=24,
            now=NOW + timedelta(days=365),
        )
        assert first.reclaimed_count == first.drained_count == 1
        second = service.sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=24,
            now=NOW + timedelta(days=365),
        )
        assert second.reclaimed_count == second.drained_count == 0
        _assert_fully_drained(session, workspace_id, asset.asset_id)


def test_zero_retention_disables_reclamation_but_not_visibility(
    session_factory,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        asset_id = asset.asset_id

        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=0,
            now=NOW + timedelta(days=3650),
        )
        assert result.inspected_count == 0
        assert result.reclaimed_count == 0
        assert result.drained_count == 0

        orphans = list_orphans(
            session,
            actor=ActorContext("system", "sweep-test"),
            workspace_id=workspace_id,
        )
        assert [item.asset_id for item in orphans] == [asset_id]
        row = _asset_row(session, workspace_id, asset_id)
        assert row.association_state == "active"


def test_retention_boundary_is_inclusive(session_factory) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        asset_id = asset.asset_id
        _pin_created_at(session, workspace_id, asset_id, NOW)
        service = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository)

        just_under = service.sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=24,
            now=NOW + timedelta(hours=24) - timedelta(seconds=1),
        )
        assert just_under.reclaimed_count == 0
        row = _asset_row(session, workspace_id, asset_id)
        assert row.association_state == "active"

        at_boundary = service.sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=24,
            now=NOW + timedelta(hours=24),
        )
        assert at_boundary.reclaimed_count == 1
        assert at_boundary.drained_count == 1

    with session_factory.begin() as session:
        _assert_fully_drained(session, workspace_id, asset_id)


def test_drain_removes_an_upload_request_marker_too(session_factory) -> None:
    """The drain step's RESTRICT-FK ordering is the fragile part: a
    ContextMediaUploadRequest row (the idempotency marker written by the
    real upload flow -- not something RegisterMediaAsset's use case creates
    on its own, so untested by the other cases here) must be deleted before
    the asset row, or the FK blocks it."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        asset_id = asset.asset_id
        _pin_created_at(session, workspace_id, asset_id, NOW)
        session.add(
            ContextMediaUploadRequest(
                workspace_id=workspace_id,
                marker_uuid=uuid4(),
                page_id=page.id,
                editing_version_id=version.id,
                asset_id=asset_id,
                sha256="a" * 64,
                actor_kind=actor.kind,
                actor_id=actor.id,
                created_at=NOW,
            )
        )
        session.flush()

    with session_factory.begin() as session:
        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=1,
            now=NOW + timedelta(hours=2),
        )
        assert result.reclaimed_count == 1
        assert result.drained_count == 1

    with session_factory.begin() as session:
        _assert_fully_drained(session, workspace_id, asset_id)


def test_drain_processes_a_candidate_queued_by_the_manual_delete_action(
    session_factory,
) -> None:
    """The sweep's drain phase is a general drainer, not just a cleanup of
    what it tombstones itself: the pre-existing manual delete-unused-media
    action (DeleteUnusedMedia, used by the workspace asset DELETE endpoint)
    enqueues a GC candidate via the same tombstone_and_enqueue_gc producer
    but has no drainer of its own. A sweep tick with retention disabled --
    so it tombstones nothing new -- must still drain that candidate."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, actor),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        asset_id = asset.asset_id
        usage = repository.get_page_asset_usage(workspace_id, page.id, asset_id)
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            asset_id=asset_id,
            usage_token=usage.usage_token,
            actor=actor,
        )
        assert _asset_row(session, workspace_id, asset_id).association_state == (
            "tombstoned"
        )
        assert _gc_candidate(session, workspace_id, asset_id).state == "queued"

    with session_factory.begin() as session:
        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=0,
            now=NOW + timedelta(days=3650),
        )
        assert result.reclaimed_count == 0
        assert result.drained_count == 1

    with session_factory.begin() as session:
        _assert_fully_drained(session, workspace_id, asset_id)


def test_reupload_with_the_same_marker_is_not_bricked_by_a_drained_asset(
    session_factory,
) -> None:
    """asset_id IS the client-supplied marker_uuid
    (backend/services/context_image_ingest.py:250), so after a drain the
    user is entitled to re-upload under the same marker -- the asset it
    named is gone. But ContextMediaLogicalCharge outlives the drain
    deliberately (workspace_write.py's count_media_dependencies counts it
    "even after release" as a workspace-deletion provenance blocker, the
    same family as StoredBlobRow/LibraryLogicalCharge/MediaStagingLease --
    it is not accumulation without purpose, unlike the GC-candidate queue
    was). SqlAlchemyContextLogicalQuota.reserve must treat a `released`
    charge for the same asset_id as a legitimate re-reservation (re-charge
    the same row) rather than the permanent conflict it used to raise."""
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        marker_uuid = uuid4()
        RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
                asset_id=marker_uuid,
            ),
            now=NOW,
        )
        version.version_status = "draft"
        session.flush()
        _pin_created_at(session, workspace_id, marker_uuid, NOW)

    with session_factory.begin() as session:
        result = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository).sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=1,
            now=NOW + timedelta(hours=2),
        )
        assert result.reclaimed_count == 1
        assert result.drained_count == 1

    with session_factory.begin() as session:
        _assert_fully_drained(session, workspace_id, marker_uuid)
        # The charge is a deliberately-retained provenance root, unlike the
        # rows drain_gc_candidate removes -- it survives, released.
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == marker_uuid,
            )
        )
        assert charge is not None and charge.state == "released"

    with session_factory.begin() as session:
        repository = SqlAlchemyContextMediaRepository(session)
        # A new upload must target a new, currently open version.
        editing = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="",
            content_hash="2" * 64,
            actor_type="human",
            created_by_actor_kind=actor.kind,
            created_by_actor_id=actor.id,
            version_status="editing",
        )
        session.add(editing)
        session.flush()
        reregistered = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                editing.id,
                blob,
                inspection_id,
                actor,
                asset_id=marker_uuid,
            ),
            now=NOW,
        )
        assert reregistered.asset_id == marker_uuid

    with session_factory.begin() as session:
        row = _asset_row(session, workspace_id, marker_uuid)
        assert row is not None and row.association_state == "active"
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == marker_uuid,
            )
        )
        assert charge is not None and charge.state == "charged"
        assert charge.released_at is None


def test_reserve_still_rejects_a_charged_asset_with_mismatched_bytes(
    session_factory,
) -> None:
    """The fix for the released-charge case must not loosen the still-active
    case: reusing a marker_uuid for a DIFFERENT byte size while its asset is
    still charged (not yet drained) is a genuine conflict, not a
    re-reservation."""
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        repository = SqlAlchemyContextMediaRepository(session)
        quota = repository._logical_quota  # - unit-level probe
        quota.lock_workspace(workspace_id)
        marker_uuid = uuid4()
        quota.reserve(workspace_id, asset_id=marker_uuid, byte_size=68)

        with pytest.raises(ValueError, match="Context logical quota asset conflict"):
            quota.reserve(workspace_id, asset_id=marker_uuid, byte_size=69)
