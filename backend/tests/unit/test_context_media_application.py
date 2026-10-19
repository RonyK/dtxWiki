from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from backend.context_media.application import (
    DeleteUnusedMedia,
    ListWorkspaceMedia,
    RegisterMediaAsset,
    SyncVersionUsage,
)
from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
    AssociationState,
    ContextMediaAssetRecord,
    ContextMediaAttachmentSnapshot,
    ContextMediaOwnershipError,
    MediaInUseError,
    RegisterContextMediaAsset,
    StaleUsageTokenError,
    UsageState,
    WorkspaceUsagePage,
)
from backend.media.domain import BlobEpoch, BlobReference

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)


class FakeRepository:
    """P3.1: no more session methods -- add_asset is a direct page-owned
    insert. The editing-version ownership/status check RegisterMediaAsset
    used to run itself now lives in SqlAlchemyContextMediaRepository.add_asset
    (it locks and validates the foreign ContextPageVersion row, which this
    fake has no notion of), so it is exercised by the integration-level
    repository tests, not here -- this fake just records what it is given.
    """

    def __init__(self) -> None:
        self.assets = {}
        self.replacements: list[tuple[UUID, UUID, UUID, dict[UUID, int]]] = []
        self.snapshot: AssetUsageSnapshot | None = None
        self.tombstoned: list[UUID] = []
        self.workspace_rows: list[ContextMediaAttachmentSnapshot] = []
        self.list_workspace_usage_calls = 0

    def list_workspace_usage(
        self,
        workspace_id: UUID,
        *,
        viewer=None,
        viewer_role=None,
        created_by=None,
        page_id=None,
        orphan_only: bool = False,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> WorkspaceUsagePage:
        """Mirrors the real adapter's keyset-plus-probe-row contract.

        Not a stub returning canned pages -- it re-derives `has_more` and
        `next_after` from the same DESC-ordered, `limit + 1` fetch the real
        SQL does, over `self.workspace_rows`, so a test can seed the rows and
        then drive `ListWorkspaceMedia` exactly the way a real repository
        would: `has_more`/`next_after` fixed before ListWorkspaceMedia's
        `usage` filter ever sees the rows.
        """

        del viewer, created_by, page_id, orphan_only, filename_contains
        self.list_workspace_usage_calls += 1
        ordered = sorted(
            self.workspace_rows,
            key=lambda row: (row.created_at, row.usage.asset.asset_id),
            reverse=True,
        )
        if after is not None:
            ordered = [
                row
                for row in ordered
                if (row.created_at, row.usage.asset.asset_id) < after
            ]
        fetched = ordered[: limit + 1]
        has_more = len(fetched) > limit
        page = fetched[:limit]
        next_after = (
            (page[-1].created_at, page[-1].usage.asset.asset_id)
            if has_more
            else None
        )
        return WorkspaceUsagePage(
            snapshots=tuple(page), has_more=has_more, next_after=next_after
        )

    def add_asset(self, asset):
        self.assets[(asset.workspace_id, asset.asset_id)] = asset
        return asset

    def get_asset(self, workspace_id, asset_id):
        return self.assets.get((workspace_id, asset_id))

    def replace_version_usage(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        reference_counts: Mapping[UUID, int],
    ) -> None:
        self.replacements.append(
            (
                workspace_id,
                page_id,
                page_version_id,
                dict(reference_counts),
            )
        )

    def get_page_asset_usage(self, workspace_id, page_id, asset_id):
        del workspace_id, page_id, asset_id
        return self.snapshot

    def tombstone_and_enqueue_gc(self, snapshot):
        self.tombstoned.append(snapshot.asset.asset_id)


def _candidate(
    workspace_id: UUID,
    actor: ActorIdentity,
    *,
    page_id: UUID,
    created_for_version_id: UUID,
    legacy_upload_asset_id: UUID | None = None,
) -> RegisterContextMediaAsset:
    return RegisterContextMediaAsset(
        workspace_id=workspace_id,
        asset_id=uuid4(),
        page_id=page_id,
        created_for_version_id=created_for_version_id,
        blob=BlobReference(workspace_id, uuid4(), BlobEpoch(1)),
        inspection_id=uuid4(),
        original_filename="image.jpg",
        canonical_media_type="image/jpeg",
        logical_bytes=12,
        policy_revision="context-mvp-v1",
        actor=actor,
        legacy_upload_asset_id=legacy_upload_asset_id,
    )


def _asset(
    *,
    workspace_id: UUID,
    page_id: UUID | None = None,
    created_by: ActorIdentity | None = None,
) -> ContextMediaAssetRecord:
    return ContextMediaAssetRecord(
        workspace_id=workspace_id,
        asset_id=uuid4(),
        page_id=page_id or uuid4(),
        created_for_version_id=uuid4(),
        blob=BlobReference(workspace_id, uuid4(), BlobEpoch(1)),
        inspection_id=uuid4(),
        original_filename="image.png",
        canonical_media_type="image/png",
        logical_bytes=12,
        policy_revision="context-mvp-v1",
        created_by=created_by or ActorIdentity("human", str(uuid4())),
        association_state=AssociationState.ACTIVE,
        revision=1,
    )


def _attachment(
    *,
    workspace_id: UUID,
    created_at: datetime,
    used: bool,
    asset_id: UUID | None = None,
) -> ContextMediaAttachmentSnapshot:
    record = _asset(workspace_id=workspace_id)
    if asset_id is not None:
        record = replace(record, asset_id=asset_id)
    usage = AssetUsageSnapshot(
        asset=record,
        latest_draft_version_id=None,
        current_published_version_id=None,
        latest_draft_reference_count=0,
        current_published_reference_count=1 if used else 0,
        historical_reference_count=0,
        created_for_version_is_editing=False,
    )
    return ContextMediaAttachmentSnapshot(
        usage=usage,
        page_title="Page",
        kind="image",
        created_at=created_at,
        is_orphan=False,
    )


def test_usage_filter_does_not_truncate_an_interleaved_listing() -> None:
    """260912 review, T1: a `usage=unused` walk must recover every unused
    row even when used rows are interleaved among them and repeatedly land
    in the same fetched window as unused ones. Before the fix, `has_more`
    was computed from the FILTERED count, so a window that happened to keep
    few (or zero) unused rows could under-report `has_more` and end the
    walk with unseen unused rows still in the workspace."""

    repository = FakeRepository()
    workspace_id = uuid4()
    unused_ids: set[UUID] = set()
    for index in range(10):
        asset_id = uuid4()
        used = index % 2 == 1
        if not used:
            unused_ids.add(asset_id)
        repository.workspace_rows.append(
            _attachment(
                workspace_id=workspace_id,
                created_at=NOW - timedelta(seconds=index),
                used=used,
                asset_id=asset_id,
            )
        )

    seen: set[UUID] = set()
    cursor = None
    for _ in range(20):  # generous bound; a real walk terminates in <= 5 pages
        projections, has_more, next_after = ListWorkspaceMedia().execute(
            repository,
            workspace_id=workspace_id,
            after=cursor,
            limit=3,
            usage=UsageState.UNUSED,
        )
        for item in projections:
            assert item.asset.asset.asset_id not in seen, "a row was served twice"
            seen.add(item.asset.asset.asset_id)
        if not has_more:
            break
        cursor = next_after
    else:
        pytest.fail("walk did not terminate -- has_more never went false")

    assert seen == unused_ids


def test_a_genuinely_final_page_carries_no_cursor() -> None:
    """260912 review, T2: when SQL's `limit + 1` probe does not return an
    extra row, `next_after` must be None -- a real final page must not hand
    the caller a cursor that resumes into nothing."""

    repository = FakeRepository()
    workspace_id = uuid4()
    for index in range(5):
        repository.workspace_rows.append(
            _attachment(
                workspace_id=workspace_id,
                created_at=NOW - timedelta(seconds=index),
                used=False,
            )
        )

    projections, has_more, next_after = ListWorkspaceMedia().execute(
        repository,
        workspace_id=workspace_id,
        limit=5,
        usage=UsageState.UNUSED,
    )

    assert len(projections) == 5
    assert has_more is False
    assert next_after is None


def test_a_used_leading_window_returns_empty_items_with_a_live_cursor() -> None:
    """260912 review, T3: a fetched window that is ALL "used" must still
    report `has_more`/`next_after` from what SQL saw, not from the (empty)
    filtered result -- an empty `items` page with a live cursor, not a page
    that looks like the end of the listing. The next page, using that
    cursor, must then recover the unused rows sitting behind the window."""

    repository = FakeRepository()
    workspace_id = uuid4()
    unused_ids: set[UUID] = set()
    # Three newest rows (the limit=2 window plus its probe row) are used;
    # two older rows behind them are unused.
    for index in range(3):
        repository.workspace_rows.append(
            _attachment(
                workspace_id=workspace_id,
                created_at=NOW - timedelta(seconds=index),
                used=True,
            )
        )
    for index in range(3, 5):
        asset_id = uuid4()
        unused_ids.add(asset_id)
        repository.workspace_rows.append(
            _attachment(
                workspace_id=workspace_id,
                created_at=NOW - timedelta(seconds=index),
                used=False,
                asset_id=asset_id,
            )
        )

    first_projections, first_has_more, first_next_after = ListWorkspaceMedia().execute(
        repository,
        workspace_id=workspace_id,
        limit=2,
        usage=UsageState.UNUSED,
    )

    assert first_projections == ()
    assert first_has_more is True
    assert first_next_after is not None

    # Recovery may take more than one further page (the used rows are
    # spread across the probe window of more than one fetch) -- what
    # matters is that the walk terminates with every unused row seen,
    # exactly like T1.
    recovered: set[UUID] = set()
    cursor = first_next_after
    has_more = True
    for _ in range(10):
        projections, has_more, cursor = ListWorkspaceMedia().execute(
            repository,
            workspace_id=workspace_id,
            after=cursor,
            limit=2,
            usage=UsageState.UNUSED,
        )
        recovered.update(item.asset.asset.asset_id for item in projections)
        if not has_more:
            break
    else:
        pytest.fail("recovery walk did not terminate")

    assert recovered == unused_ids
    assert has_more is False


def test_registration_and_usage_sync() -> None:
    repository = FakeRepository()
    workspace_id = uuid4()
    page_id = uuid4()
    editing_version_id = uuid4()
    version_id = uuid4()
    actor = ActorIdentity("human", str(uuid4()))
    legacy_upload_asset_id = uuid4()
    candidate = _candidate(
        workspace_id,
        actor,
        page_id=page_id,
        created_for_version_id=editing_version_id,
        legacy_upload_asset_id=legacy_upload_asset_id,
    )
    asset = RegisterMediaAsset().execute(repository, candidate, now=NOW)

    assert asset.created_by == actor
    assert asset.page_id == page_id
    assert asset.created_for_version_id == editing_version_id
    assert asset.legacy_upload_asset_id == legacy_upload_asset_id

    reference = asset.logical_reference
    counts = SyncVersionUsage().execute(
        repository,
        workspace_id=workspace_id,
        page_id=page_id,
        page_version_id=version_id,
        body_markdown=f"![a]({reference}) and ![b]({reference})",
    )
    assert counts == {asset.asset_id: 2}
    assert repository.replacements[-1] == (
        workspace_id,
        page_id,
        version_id,
        {asset.asset_id: 2},
    )


def test_delete_requires_current_unused_projection_token() -> None:
    repository = FakeRepository()
    workspace_id = uuid4()
    actor = ActorIdentity("human", str(uuid4()))
    asset = _asset(workspace_id=workspace_id, created_by=actor)
    repository.snapshot = AssetUsageSnapshot(asset, uuid4(), uuid4(), 0, 0, 0, False)

    with pytest.raises(StaleUsageTokenError):
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=asset.page_id,
            asset_id=asset.asset_id,
            usage_token="stale",
            actor=actor,
        )

    active = AssetUsageSnapshot(asset, uuid4(), uuid4(), 1, 0, 0, False)
    repository.snapshot = active
    with pytest.raises(MediaInUseError):
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=asset.page_id,
            asset_id=asset.asset_id,
            usage_token=active.usage_token,
            actor=actor,
        )

    repository.snapshot = AssetUsageSnapshot(asset, uuid4(), uuid4(), 0, 0, 0, False)
    DeleteUnusedMedia().execute(
        repository,
        workspace_id=workspace_id,
        page_id=asset.page_id,
        asset_id=asset.asset_id,
        usage_token=repository.snapshot.usage_token,
        actor=actor,
    )
    assert repository.tombstoned == [asset.asset_id]


def test_delete_of_a_still_editing_upload_is_restricted_to_its_own_author() -> None:
    """P3.4: while created_for_version_id names a still-open editing row,
    DeleteUnusedMedia -- unlike the reference-count-only state check above --
    also enforces that only the asset's own uploader may remove it. The
    orphan sweep never reaches this branch for a real orphan (its own query
    excludes protected assets before ever calling DeleteUnusedMedia); this
    is specifically about the explicit per-asset delete command."""

    repository = FakeRepository()
    workspace_id = uuid4()
    owner = ActorIdentity("human", str(uuid4()))
    stranger = ActorIdentity("human", str(uuid4()))
    asset = _asset(workspace_id=workspace_id, created_by=owner)
    repository.snapshot = AssetUsageSnapshot(asset, None, None, 0, 0, 0, True)

    with pytest.raises(ContextMediaOwnershipError):
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=asset.page_id,
            asset_id=asset.asset_id,
            usage_token=repository.snapshot.usage_token,
            actor=stranger,
        )
    assert repository.tombstoned == []

    DeleteUnusedMedia().execute(
        repository,
        workspace_id=workspace_id,
        page_id=asset.page_id,
        asset_id=asset.asset_id,
        usage_token=repository.snapshot.usage_token,
        actor=owner,
    )
    assert repository.tombstoned == [asset.asset_id]
