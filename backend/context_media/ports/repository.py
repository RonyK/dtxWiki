"""Inward-owned persistence and transaction seams for Context media."""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
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
        viewer: ActorIdentity | None = None,
    ) -> tuple[AssetUsageSnapshot, ...]:
        # `viewer` scopes out other authors' private "editing" uploads (P8.4).
        # `None` is fail-closed: it hides every editing-staged asset.
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
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> WorkspaceUsagePage:
        # Same `viewer` contract as list_page_usage, and it matters more here:
        # this is the first listing that spans a whole workspace, so it is the
        # first place another author's private "editing" upload could leak.
        # `None` is fail-closed.
        #
        # `created_by` is the "mine" filter. `after` is the keyset cursor
        # (created_at, asset_id), matching the DESC ordering the adapter
        # applies. `limit` is the PAGE size the caller wants -- the
        # implementation is responsible for fetching one extra row internally
        # to populate `WorkspaceUsagePage.has_more`/`next_after` (260912
        # review: this used to be the caller's job, passing `limit + 1` down
        # and computing paging state on whatever survived a Python filter
        # applied afterward -- which silently truncated a filtered listing
        # once the filter removed enough of that fetched page. See
        # `WorkspaceUsagePage`'s docstring for why that state has to be fixed
        # here, before any caller-side filtering can touch it.
        ...

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
    # D-7 (doc/design/workspace-database-separation-plan.md): Context media is
    # workspace-owned data (workspace_id is part of every table's primary
    # key), so the factory routes per call instead of pinning one session
    # source at construction -- there is no single "the" plane to bind ahead
    # of time.
    def __call__(self, workspace_id: UUID) -> ContextMediaUnitOfWork: ...
