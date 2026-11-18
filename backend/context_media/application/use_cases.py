"""Framework-free Context attachment use cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from backend.context_media.domain import (
    ActorIdentity,
    AssetVersionUsagePage,
    AssociationState,
    ContentCapability,
    ContextMediaAssetProjection,
    ContextMediaAssetRecord,
    ContextMediaAttachmentProjection,
    ContextMediaNotFoundError,
    ContextMediaUnavailableError,
    MediaInUseError,
    MediaUsedInHistoryError,
    RegisterContextMediaAsset,
    StaleUsageTokenError,
    UsageState,
    validate_markdown_media_policy,
)
from backend.context_media.ports import (
    ContextMediaRepository,
    ContextMediaUnitOfWorkFactory,
)


class RegisterMediaAsset:
    def execute(
        self,
        repository: ContextMediaRepository,
        candidate: RegisterContextMediaAsset,
        *,
        now: datetime,
    ) -> ContextMediaAssetRecord:
        # The open draft the asset is uploaded into is its
        # owning context now, not a ContextMediaSession -- validated by
        # `repository.add_asset` (it already locks and checks `page_id`
        # against `ContextPage` the same way; `created_for_version_id`
        # against `ContextPageVersion` follows the identical pattern,
        # since both are foreign tables this bounded context's own
        # Protocol has no notion of).
        record = ContextMediaAssetRecord(
            workspace_id=candidate.workspace_id,
            asset_id=candidate.asset_id,
            page_id=candidate.page_id,
            created_for_version_id=candidate.created_for_version_id,
            blob=candidate.blob,
            inspection_id=candidate.inspection_id,
            legacy_upload_asset_id=candidate.legacy_upload_asset_id,
            original_filename=candidate.original_filename,
            canonical_media_type=candidate.canonical_media_type,
            logical_bytes=candidate.logical_bytes,
            policy_revision=candidate.policy_revision,
            created_by=candidate.actor,
            association_state=AssociationState.ACTIVE,
            revision=1,
        )
        return repository.add_asset(record)


class SyncVersionUsage:
    """Replace the exact PageVersion authorization index in its caller transaction."""

    def execute(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        body_markdown: str,
    ) -> dict[UUID, int]:
        counts = Counter(validate_markdown_media_policy(body_markdown))
        repository.replace_version_usage(
            workspace_id=workspace_id,
            page_id=page_id,
            page_version_id=page_version_id,
            reference_counts=counts,
        )
        return dict(counts)


class ListPageMedia:
    def execute(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        page_id: UUID,
        used: bool | None = None,
    ) -> tuple[ContextMediaAssetProjection, ...]:
        return tuple(
            ContextMediaAssetProjection.from_snapshot(snapshot)
            for snapshot in repository.list_page_usage(
                workspace_id, page_id, used=used
            )
        )


class ListWorkspaceMedia:
    """One page of a workspace's attachments.

    ``used`` filters on usage in any version, including versions the viewer
    cannot read. It runs in SQL before the page window is cut, so the
    repository's ``has_more``/``next_after`` pass through unchanged.
    """

    def execute(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        viewer: ActorIdentity | None = None,
        viewer_role: str | None = None,
        created_by: ActorIdentity | None = None,
        page_id: UUID | None = None,
        orphan_only: bool = False,
        used: bool | None = None,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[ContextMediaAttachmentProjection, ...],
        bool,
        tuple[datetime, UUID] | None,
    ]:
        page = repository.list_workspace_usage(
            workspace_id,
            viewer=viewer,
            viewer_role=viewer_role,
            created_by=created_by,
            page_id=page_id,
            orphan_only=orphan_only,
            used=used,
            filename_contains=filename_contains,
            after=after,
            limit=limit,
        )
        projections = tuple(
            ContextMediaAttachmentProjection.from_snapshot(snapshot)
            for snapshot in page.snapshots
        )
        return projections, page.has_more, page.next_after


class DeleteUnusedMedia:
    def execute(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        page_id: UUID,
        asset_id: UUID,
        usage_token: str,
        actor: ActorIdentity,
    ) -> None:
        snapshot = repository.get_page_asset_usage(workspace_id, page_id, asset_id)
        if (
            snapshot is None
            or snapshot.asset.association_state is AssociationState.TOMBSTONED
        ):
            raise ContextMediaNotFoundError("Context media asset was not found")
        if usage_token != snapshot.usage_token:
            raise StaleUsageTokenError("usage token is stale")
        if snapshot.state is UsageState.ACTIVE:
            raise MediaInUseError("media is referenced by an active Page version")
        if snapshot.state is UsageState.HISTORY_ONLY:
            raise MediaUsedInHistoryError("media is referenced by retained history")
        repository.tombstone_and_enqueue_gc(snapshot)


class ResolveContent:
    def version(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        asset_id: UUID,
        actor: ActorIdentity,
    ) -> ContentCapability:
        # The caller checks version visibility. Content must be referenced
        # by that version or be an upload created for that exact version.
        capability = repository.resolve_version_content(
            workspace_id=workspace_id,
            page_id=page_id,
            page_version_id=page_version_id,
            asset_id=asset_id,
            actor=actor,
        )
        if capability is None:
            raise ContextMediaUnavailableError("Context media content is unavailable")
        return capability


class ContextMediaService:
    """Standalone transaction owner; Page writes use the narrow use cases directly."""

    def __init__(
        self,
        uow_factory: ContextMediaUnitOfWorkFactory,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_asset(
        self, candidate: RegisterContextMediaAsset
    ) -> ContextMediaAssetRecord:
        with self._uow_factory(candidate.workspace_id) as uow:
            result = RegisterMediaAsset().execute(
                uow.repository, candidate, now=self._clock()
            )
            uow.commit()
            return result

    def list_page_assets(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        used: bool | None = None,
    ) -> tuple[ContextMediaAssetProjection, ...]:
        with self._uow_factory(workspace_id) as uow:
            return ListPageMedia().execute(
                uow.repository,
                workspace_id=workspace_id,
                page_id=page_id,
                used=used,
            )

    def list_workspace_assets(
        self,
        *,
        workspace_id: UUID,
        viewer: ActorIdentity | None = None,
        viewer_role: str | None = None,
        created_by: ActorIdentity | None = None,
        page_id: UUID | None = None,
        orphan_only: bool = False,
        used: bool | None = None,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> tuple[
        tuple[ContextMediaAttachmentProjection, ...],
        bool,
        tuple[datetime, UUID] | None,
    ]:
        with self._uow_factory(workspace_id) as uow:
            return ListWorkspaceMedia().execute(
                uow.repository,
                workspace_id=workspace_id,
                viewer=viewer,
                viewer_role=viewer_role,
                created_by=created_by,
                page_id=page_id,
                orphan_only=orphan_only,
                used=used,
                filename_contains=filename_contains,
                after=after,
                limit=limit,
            )

    def list_asset_version_usage(
        self,
        *,
        workspace_id: UUID,
        asset_id: UUID,
        viewer: ActorIdentity,
        viewer_role: str,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 25,
    ) -> AssetVersionUsagePage | None:
        with self._uow_factory(workspace_id) as uow:
            return uow.repository.list_asset_version_usage(
                workspace_id,
                asset_id,
                viewer=viewer,
                viewer_role=viewer_role,
                after=after,
                limit=limit,
            )

    def delete_unused(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        asset_id: UUID,
        usage_token: str,
        actor: ActorIdentity,
    ) -> None:
        with self._uow_factory(workspace_id) as uow:
            DeleteUnusedMedia().execute(
                uow.repository,
                workspace_id=workspace_id,
                page_id=page_id,
                asset_id=asset_id,
                usage_token=usage_token,
                actor=actor,
            )
            uow.commit()

    def resolve_version_content(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        asset_id: UUID,
        actor: ActorIdentity,
    ) -> ContentCapability:
        with self._uow_factory(workspace_id) as uow:
            return ResolveContent().version(
                uow.repository,
                workspace_id=workspace_id,
                page_id=page_id,
                page_version_id=page_version_id,
                asset_id=asset_id,
                actor=actor,
            )
