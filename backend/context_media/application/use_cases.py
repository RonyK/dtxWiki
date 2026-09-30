"""Framework-free Context attachment use cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from backend.context_media.domain import (
    ActorIdentity,
    AssociationState,
    ContentCapability,
    ContextMediaAssetProjection,
    ContextMediaAssetRecord,
    ContextMediaAttachmentProjection,
    ContextMediaNotFoundError,
    ContextMediaOwnershipError,
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
        # P3.1/P3.2: the caller's own open "editing" row is the asset's
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
        usage: UsageState | None = None,
        viewer: ActorIdentity | None = None,
    ) -> tuple[ContextMediaAssetProjection, ...]:
        projections = tuple(
            ContextMediaAssetProjection.from_snapshot(snapshot)
            for snapshot in repository.list_page_usage(
                workspace_id, page_id, viewer=viewer
            )
            if snapshot.asset.association_state is not AssociationState.TOMBSTONED
        )
        if usage is None:
            return projections
        if usage is UsageState.UNUSED:
            return tuple(item for item in projections if item.usage_state is usage)
        return tuple(
            item
            for item in projections
            if item.usage_state in {UsageState.ACTIVE, UsageState.HISTORY_ONLY}
        )


class ListWorkspaceMedia:
    """One page of a workspace's attachments.

    The ``usage`` filter runs AFTER the repository's viewer masking, on the
    projection's recomputed ``usage_state``. That ordering is the point: a
    ``used`` request must never surface an asset whose only reference lives in
    somebody else's private editing draft, and ``ContextMediaAssetProjection.
    from_snapshot`` is what turns the masked counts into the state we filter on.

    260912 review: that same ordering used to break pagination. ``has_more``
    and ``next_cursor`` were computed by the ROUTE from ``len(projections)``
    -- AFTER this filter ran -- so a request like ``usage=unused&limit=50``
    that fetched 51 rows and kept only 8 unused ones reported
    ``has_more = 8 > 50 = False`` and stopped the listing there, even with
    hundreds of unused assets still unseen. No error, no truncation marker.
    ``repository.list_workspace_usage`` now fixes ``has_more``/``next_after``
    from what SQL actually returned, before this method's filter ever touches
    the rows (``WorkspaceUsagePage``'s docstring has the full argument) --
    this method's only job is to pass that pair through unfiltered.
    """

    def execute(
        self,
        repository: ContextMediaRepository,
        *,
        workspace_id: UUID,
        viewer: ActorIdentity | None = None,
        created_by: ActorIdentity | None = None,
        page_id: UUID | None = None,
        orphan_only: bool = False,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
        usage: UsageState | None = None,
    ) -> tuple[
        tuple[ContextMediaAttachmentProjection, ...],
        bool,
        tuple[datetime, UUID] | None,
    ]:
        page = repository.list_workspace_usage(
            workspace_id,
            viewer=viewer,
            created_by=created_by,
            page_id=page_id,
            orphan_only=orphan_only,
            filename_contains=filename_contains,
            after=after,
            limit=limit,
        )
        # Not filtered here: the adapter's query already excludes
        # TOMBSTONED in SQL (`association_state != TOMBSTONED.value`), so a
        # tombstoned row never reaches `page.snapshots`. This used to be a
        # second, redundant filter on the same condition; kept out entirely
        # rather than kept as a defensive no-op, because a defensive filter
        # applied AFTER `has_more`/`next_after` are fixed is exactly the shape
        # of bug this method exists to not repeat -- see the class docstring.
        projections = tuple(
            ContextMediaAttachmentProjection.from_snapshot(snapshot)
            for snapshot in page.snapshots
        )
        if usage is not None:
            if usage is UsageState.UNUSED:
                projections = tuple(
                    item for item in projections if item.asset.usage_state is usage
                )
            else:
                projections = tuple(
                    item
                    for item in projections
                    if item.asset.usage_state
                    in {UsageState.ACTIVE, UsageState.HISTORY_ONLY}
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
        # P3.4: while the asset's creating editing session is still open, it
        # is that author's unshared upload -- only they may explicitly
        # delete it (the sweep never reaches here for one: the orphan query
        # excludes it first). Once no longer "editing", ordinary editor-wide
        # deletion applies, same as any other zero-usage page asset.
        if (
            snapshot.created_for_version_is_editing
            and snapshot.asset.created_by != actor
        ):
            raise ContextMediaOwnershipError(
                "media belongs to another actor's open editing session"
            )
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
        # P3.3: the version-scoped route is now the only private content
        # read -- it covers both an exact-version usage read (draft/
        # published/history, unconditional once the caller's outer
        # version-visibility check already passed) and, when no usage row
        # exists yet, an uninserted upload's preview via
        # `created_for_version_id`. The fallback is narrower than "may this
        # actor see this version" -- an uninserted image was never actually
        # published as content, so it stays private to its own uploader
        # specifically even if the surrounding version is itself a
        # role-visible submitted draft; `actor` is what lets
        # resolve_version_content enforce that. The versionless page
        # preview this used to sit beside is gone; there is no
        # owner-independent asset read left.
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
        usage: UsageState | None = None,
        viewer: ActorIdentity | None = None,
    ) -> tuple[ContextMediaAssetProjection, ...]:
        with self._uow_factory(workspace_id) as uow:
            return ListPageMedia().execute(
                uow.repository,
                workspace_id=workspace_id,
                page_id=page_id,
                usage=usage,
                viewer=viewer,
            )

    def list_workspace_assets(
        self,
        *,
        workspace_id: UUID,
        viewer: ActorIdentity | None = None,
        created_by: ActorIdentity | None = None,
        page_id: UUID | None = None,
        orphan_only: bool = False,
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
        usage: UsageState | None = None,
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
                created_by=created_by,
                page_id=page_id,
                orphan_only=orphan_only,
                filename_contains=filename_contains,
                after=after,
                limit=limit,
                usage=usage,
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
