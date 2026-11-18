"""Inward-owned persistence and transaction seams for Context media."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
    AssetVersionUsagePage,
    ContentCapability,
    ContextMediaAssetRecord,
    WorkspaceUsagePage,
)


class ContextMediaRepository(Protocol):
    def lock_workspace_for_usage(self, workspace_id: UUID) -> None: ...

    def add_asset(self, asset: ContextMediaAssetRecord) -> ContextMediaAssetRecord: ...

    def get_asset(
        self, workspace_id: UUID, asset_id: UUID
    ) -> ContextMediaAssetRecord | None: ...

    def get_asset_by_legacy_upload(
        self,
        workspace_id: UUID,
        legacy_upload_asset_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextMediaAssetRecord | None: ...

    def replace_version_usage(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        reference_counts: Mapping[UUID, int],
    ) -> None: ...

    def list_page_usage(
        self,
        workspace_id: UUID,
        page_id: UUID,
        *,
        used: bool | None = None,
    ) -> tuple[AssetUsageSnapshot, ...]:
        # Live assets of one page; ``used`` filters on any version's usage.
        ...

    def list_workspace_usage(
        self,
        workspace_id: UUID,
        *,
        viewer: ActorIdentity | None = None,
        viewer_role: str | None = None,
        created_by: ActorIdentity | None = None,
        page_id: UUID | None = None,
        orphan_only: bool = False,
        used: bool | None = None,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> WorkspaceUsagePage:
        # `created_by` is the "mine" filter. `after` is the keyset cursor
        # (created_at, asset_id), matching the DESC ordering the adapter
        # applies. `limit` is the PAGE size; every filter runs before the
        # adapter's internal `limit + 1` probe, so `has_more`/`next_after`
        # describe exactly the returned window.
        ...

    def asset_usage_snapshots(
        self, workspace_id: UUID, asset_ids: Iterable[UUID]
    ) -> dict[UUID, AssetUsageSnapshot]:
        # Batch snapshots for a caller holding no workspace read.
        ...

    def list_asset_version_usage(
        self,
        workspace_id: UUID,
        asset_id: UUID,
        *,
        viewer: ActorIdentity,
        viewer_role: str,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 25,
    ) -> AssetVersionUsagePage | None: ...

    def get_page_asset_usage(
        self, workspace_id: UUID, page_id: UUID, asset_id: UUID
    ) -> AssetUsageSnapshot | None: ...

    def tombstone_and_enqueue_gc(self, snapshot: AssetUsageSnapshot) -> None: ...

    def drain_gc_candidate(self, *, workspace_id: UUID, asset_id: UUID) -> bool: ...

    def resolve_version_content(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        asset_id: UUID,
        actor: ActorIdentity,
    ) -> ContentCapability | None: ...

    def resolve_asset_content_capability(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        asset_id: UUID,
    ) -> ContentCapability | None:
        # System-level blob-integrity check ONLY (e.g. legacy provenance
        # reconciliation). Unlike resolve_version_content, this does not
        # check that the asset is referenced by any version's usage and
        # takes no actor -- it is not a visibility decision. Never call this
        # from a REST/MCP read path or anything else deciding what a caller
        # may see.
        ...


class ContextMediaUnitOfWork(Protocol):
    repository: ContextMediaRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class ContextMediaUnitOfWorkFactory(Protocol):
    # Context media is workspace-owned data (workspace_id is part of every primary
    # key), so the factory routes per call instead of pinning one session
    # source at construction -- there is no single "the" plane to bind ahead
    # of time.
    def __call__(self, workspace_id: UUID) -> ContextMediaUnitOfWork: ...
