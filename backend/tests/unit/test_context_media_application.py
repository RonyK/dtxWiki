from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.context_media.application import (
    DeleteUnusedMedia,
    RegisterMediaAsset,
    SyncVersionUsage,
)
from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
    AssociationState,
    ContextMediaAssetRecord,
    MediaInUseError,
    RegisterContextMediaAsset,
    StaleUsageTokenError,
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
    repository.snapshot = AssetUsageSnapshot(asset, uuid4(), uuid4(), 0, 0, 0)

    with pytest.raises(StaleUsageTokenError):
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=asset.page_id,
            asset_id=asset.asset_id,
            usage_token="stale",
            actor=actor,
        )

    active = AssetUsageSnapshot(asset, uuid4(), uuid4(), 1, 0, 0)
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

    repository.snapshot = AssetUsageSnapshot(asset, uuid4(), uuid4(), 0, 0, 0)
    DeleteUnusedMedia().execute(
        repository,
        workspace_id=workspace_id,
        page_id=asset.page_id,
        asset_id=asset.asset_id,
        usage_token=repository.snapshot.usage_token,
        actor=actor,
    )
    assert repository.tombstoned == [asset.asset_id]


def test_unused_upload_can_be_deleted_by_another_editor() -> None:
    repository = FakeRepository()
    workspace_id = uuid4()
    owner = ActorIdentity("human", str(uuid4()))
    stranger = ActorIdentity("human", str(uuid4()))
    asset = _asset(workspace_id=workspace_id, created_by=owner)
    repository.snapshot = AssetUsageSnapshot(asset, None, None, 0, 0, 0)

    DeleteUnusedMedia().execute(
        repository,
        workspace_id=workspace_id,
        page_id=asset.page_id,
        asset_id=asset.asset_id,
        usage_token=repository.snapshot.usage_token,
        actor=stranger,
    )
    assert repository.tombstoned == [asset.asset_id]
