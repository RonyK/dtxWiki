"""SQLAlchemy persistence adapter for the Context attachment bounded context."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, case, delete, false, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
    AssetVersionUsage,
    AssetVersionUsagePage,
    AssociationState,
    ContentCapability,
    ContextMediaAssetRecord,
    ContextMediaAttachmentSnapshot,
    ContextMediaEditingNotOpenError,
    ContextMediaNotFoundError,
    ContextMediaOwnershipError,
    ContextMediaUnavailableError,
    StaleUsageTokenError,
    WorkspaceUsagePage,
)
from backend.context_media.ports import ContextLogicalQuotaExceededError
from backend.media.domain import BlobEpoch, BlobReference, BlobState
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaGcCandidate,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUploadRequest,
    ContextMediaUsage,
)
from backend.models.media import (
    BlobIncarnationRow,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.context_media_orphans import (
    context_media_orphan_clause,
)
from backend.repositories.context_visibility import (
    explicit_context_version_visible_expression,
    visible_context_page_expression,
)
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
)
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.services.actor_visible_read import context_media_version_is_readable
from backend.services.db_write_intent import acquire_write_intent

DEFAULT_CONTEXT_LOGICAL_QUOTA_BYTES = 10_000_000_000
_USAGE_LOCK_STATE_KEY = "_context_media_usage_lock_state"


class SqlAlchemyContextLogicalQuota:
    """Atomic Context live-asset logical quota ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_workspace(self, workspace_id: UUID) -> None:
        """Acquire the quota rank without changing quota state."""

        self._locked_logical_usage(workspace_id)

    def reserve(self, workspace_id: UUID, *, asset_id: UUID, byte_size: int) -> None:
        _validate_logical_charge(asset_id, byte_size)
        usage = self._locked_logical_usage(workspace_id)
        charge = self._session.scalar(
            select(ContextMediaLogicalCharge)
            .where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == asset_id,
            )
            .with_for_update()
        )
        if charge is not None and charge.state == "charged":
            if charge.byte_size != byte_size:
                raise ValueError("Context logical quota asset conflict")
            return
        if usage.charged_bytes + byte_size > usage.limit_bytes:
            raise ContextLogicalQuotaExceededError(limit_bytes=usage.limit_bytes)
        now = datetime.now(UTC)
        if charge is None:
            self._session.add(
                ContextMediaLogicalCharge(
                    workspace_id=workspace_id,
                    id=uuid5(
                        NAMESPACE_URL,
                        f"context-logical:{workspace_id}:{asset_id}",
                    ),
                    asset_id=asset_id,
                    byte_size=byte_size,
                    state="charged",
                    created_at=now,
                )
            )
        else:
            # asset_id is the client-supplied marker_uuid
            # (backend/services/context_image_ingest.py), and the
            # ContextMediaAsset row this charge originally belonged to may
            # since have been drained (drain_gc_candidate deletes the asset
            # row but deliberately never this charge -- see
            # backend/services/workspace_write.py's
            # count_media_dependencies, which counts a Context logical
            # charge "even after release" as a retained provenance root
            # blocking workspace deletion, the same family as
            # StoredBlobRow/LibraryLogicalCharge/MediaStagingLease). A
            # `released` charge for the same asset_id is therefore a
            # legitimate re-reservation under a marker the caller is
            # entitled to reuse, not a conflict -- re-charge the SAME row
            # (preserving its id/created_at as history) instead of raising.
            charge.byte_size = byte_size
            charge.state = "charged"
            charge.released_at = None
        usage.charged_bytes += byte_size
        usage.revision += 1
        usage.updated_at = now
        self._session.flush()

    def release(self, workspace_id: UUID, *, asset_id: UUID) -> None:
        usage = self._locked_logical_usage(workspace_id)
        charge = self._session.scalar(
            select(ContextMediaLogicalCharge)
            .where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == asset_id,
            )
            .with_for_update()
        )
        if charge is None or charge.state == "released":
            return
        if charge.byte_size > usage.charged_bytes:
            raise RuntimeError("Context logical quota counter drift")
        now = datetime.now(UTC)
        charge.state = "released"
        charge.released_at = now
        usage.charged_bytes -= charge.byte_size
        usage.revision += 1
        usage.updated_at = now
        self._session.flush()

    def _locked_logical_usage(
        self, workspace_id: UUID
    ) -> ContextMediaLogicalQuotaUsage:
        acquire_write_intent(self._session)
        _lock_open_workspace_media_intent(self._session, workspace_id)
        values = {
            "workspace_id": workspace_id,
            "limit_bytes": DEFAULT_CONTEXT_LOGICAL_QUOTA_BYTES,
            "charged_bytes": 0,
            "revision": 0,
            "updated_at": datetime.now(UTC),
        }
        dialect = self._session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(ContextMediaLogicalQuotaUsage).values(**values)
        elif dialect == "postgresql":
            statement = postgresql_insert(ContextMediaLogicalQuotaUsage).values(
                **values
            )
        else:
            raise RuntimeError("unsupported Context logical quota database")
        self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["workspace_id"])
        )
        usage = self._session.scalar(
            select(ContextMediaLogicalQuotaUsage)
            .where(ContextMediaLogicalQuotaUsage.workspace_id == workspace_id)
            .with_for_update()
        )
        if usage is None:
            raise RuntimeError("Context logical quota usage was not provisioned")
        return usage


class SqlAlchemyContextMediaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._logical_quota = SqlAlchemyContextLogicalQuota(session)
        self._blob_references = SqlAlchemyBlobReferenceLedger(session)

    def lock_workspace_for_usage(self, workspace_id: UUID) -> None:
        """Enter the shared media-intent lock before any Page row lock."""

        acquire_write_intent(self._session)
        _lock_required_open_workspace_media_intent(self._session, workspace_id)
        transaction = self._session.get_transaction()
        if transaction is None:
            raise RuntimeError(
                "Context media usage lock requires an active transaction"
            )
        nested_transaction = self._session.get_nested_transaction()
        lock_state = self._session.info.get(_USAGE_LOCK_STATE_KEY)
        if (
            not isinstance(lock_state, tuple)
            or len(lock_state) != 3
            or lock_state[0] is not transaction
            or lock_state[1] is not nested_transaction
            or not isinstance(lock_state[2], set)
        ):
            lock_state = (transaction, nested_transaction, set())
            self._session.info[_USAGE_LOCK_STATE_KEY] = lock_state
        lock_state[2].add(workspace_id)

    def add_asset(self, asset: ContextMediaAssetRecord) -> ContextMediaAssetRecord:
        _lock_context_workspace(self._session, asset.workspace_id)
        self._logical_quota.lock_workspace(asset.workspace_id)
        if asset.legacy_upload_asset_id is not None:
            anchored = self.get_asset_by_legacy_upload(
                asset.workspace_id,
                asset.legacy_upload_asset_id,
                for_update=True,
            )
            if anchored is not None:
                raise ValueError("legacy upload asset already has a Context anchor")
        if self.get_asset(asset.workspace_id, asset.asset_id) is not None:
            raise ValueError("Context media asset already exists")
        page = _lock_context_page(
            self._session,
            workspace_id=asset.workspace_id,
            page_id=asset.page_id,
        )
        if (
            page is None
            or page.workspace_id != asset.workspace_id
            or page.lifecycle_status == "archived"
        ):
            raise ContextMediaNotFoundError("Context Page was not found")
        # Uploads attach to a workspace-authorized mutable draft.
        # Lock the row so publication cannot race asset registration.
        editing_version = _lock_context_page_version(
            self._session,
            workspace_id=asset.workspace_id,
            page_id=asset.page_id,
            version_id=asset.created_for_version_id,
        )
        if editing_version is None:
            raise ContextMediaNotFoundError("editing version not found")
        if editing_version.version_status != "draft":
            raise ContextMediaEditingNotOpenError("draft is not open")
        self._logical_quota.reserve(
            asset.workspace_id,
            asset_id=asset.asset_id,
            byte_size=asset.logical_bytes,
        )
        incarnation = self._available_incarnation(asset.blob)
        inspection = self._session.get(
            StoredBlobInspectionRow,
            {"workspace_id": asset.workspace_id, "id": asset.inspection_id},
        )
        if (
            incarnation is None
            or inspection is None
            or inspection.blob_id != asset.blob.blob_id
            or inspection.blob_epoch != asset.blob.epoch.value
            or not inspection.ordinary_available
            or inspection.canonical_media_type != asset.canonical_media_type
        ):
            raise ContextMediaUnavailableError(
                "shared media incarnation is not available for Context"
            )
        self._blob_references.register(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=asset.asset_id,
            blob=asset.blob,
        )
        self._session.add(
            ContextMediaAsset(
                workspace_id=asset.workspace_id,
                id=asset.asset_id,
                page_id=asset.page_id,
                created_for_version_id=asset.created_for_version_id,
                blob_id=asset.blob.blob_id,
                blob_epoch=asset.blob.epoch.value,
                inspection_id=asset.inspection_id,
                legacy_upload_asset_id=asset.legacy_upload_asset_id,
                kind="image",
                original_filename=asset.original_filename,
                canonical_media_type=asset.canonical_media_type,
                logical_bytes=asset.logical_bytes,
                policy_revision=asset.policy_revision,
                association_state=asset.association_state.value,
                revision=asset.revision,
                created_by_actor_kind=asset.created_by.kind,
                created_by_actor_id=asset.created_by.id,
            )
        )
        self._session.flush()
        return asset

    def get_asset(
        self, workspace_id: UUID, asset_id: UUID
    ) -> ContextMediaAssetRecord | None:
        row = self._session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset_id},
        )
        return None if row is None else _asset_record(row)

    def get_asset_by_legacy_upload(
        self,
        workspace_id: UUID,
        legacy_upload_asset_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextMediaAssetRecord | None:
        statement = select(ContextMediaAsset).where(
            ContextMediaAsset.workspace_id == workspace_id,
            ContextMediaAsset.legacy_upload_asset_id == legacy_upload_asset_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return None if row is None else _asset_record(row)

    def replace_version_usage(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        reference_counts: Mapping[UUID, int],
    ) -> None:
        transaction = self._session.get_transaction()
        nested_transaction = self._session.get_nested_transaction()
        lock_state = self._session.info.get(_USAGE_LOCK_STATE_KEY)
        if (
            not isinstance(lock_state, tuple)
            or len(lock_state) != 3
            or lock_state[0] is not transaction
            or lock_state[1] is not nested_transaction
            or not isinstance(lock_state[2], set)
            or workspace_id not in lock_state[2]
        ):
            self.lock_workspace_for_usage(workspace_id)
        page = _lock_context_page(
            self._session,
            workspace_id=workspace_id,
            page_id=page_id,
        )
        version = self._session.get(ContextPageVersion, page_version_id)
        if (
            page is None
            or page.workspace_id != workspace_id
            or version is None
            or version.page_id != page_id
        ):
            raise ContextMediaNotFoundError("exact Context Page version was not found")
        for count in reference_counts.values():
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError("reference counts must be positive integers")
        asset_ids = set(reference_counts)
        if asset_ids:
            owned_assets = tuple(
                self._session.scalars(
                    select(ContextMediaAsset)
                    .where(
                        ContextMediaAsset.workspace_id == workspace_id,
                        ContextMediaAsset.page_id == page_id,
                        ContextMediaAsset.id.in_(asset_ids),
                        ContextMediaAsset.association_state
                        == AssociationState.ACTIVE.value,
                    )
                    .order_by(ContextMediaAsset.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
            )
            owned_ids = {asset.id for asset in owned_assets}
            if owned_ids != asset_ids:
                raise ContextMediaOwnershipError(
                    "logical reference is foreign, unclaimed, or tombstoned"
                )
            if any(self._content_capability(asset) is None for asset in owned_assets):
                raise ContextMediaUnavailableError(
                    "logical reference is pending, blocked, or quarantined"
                )
        self._session.execute(
            delete(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.page_id == page_id,
                ContextMediaUsage.page_version_id == page_version_id,
            )
        )
        self._session.add_all(
            ContextMediaUsage(
                workspace_id=workspace_id,
                page_id=page_id,
                page_version_id=page_version_id,
                asset_id=asset_id,
                reference_count=count,
            )
            for asset_id, count in reference_counts.items()
        )
        self._session.flush()

    def list_page_usage(
        self,
        workspace_id: UUID,
        page_id: UUID,
        *,
        used: bool | None = None,
    ) -> tuple[AssetUsageSnapshot, ...]:
        """Every live asset of one page, oldest first.

        ``used`` filters in SQL on whether any version references the asset.
        Snapshots carry no reader, so their ``has_unreadable_usage`` is not
        meaningful; this listing is editor-gated and does not expose it.
        """

        page = self._page(workspace_id, page_id)
        conditions = [
            ContextMediaAsset.page_id == page_id,
            ContextMediaAsset.association_state != AssociationState.TOMBSTONED.value,
        ]
        if used is not None:
            conditions.append(
                _asset_is_referenced() if used else ~_asset_is_referenced()
            )
        assets = tuple(
            self._session.scalars(
                select_in_workspace(
                    ContextMediaAsset,
                    workspace_id=workspace_id,
                    conditions=tuple(conditions),
                ).order_by(ContextMediaAsset.created_at, ContextMediaAsset.id)
            ).all()
        )
        totals = _usage_totals(
            self._session,
            workspace_id=workspace_id,
            asset_ids=(asset.id for asset in assets),
            reader=None,
        )
        return tuple(
            _usage_snapshot(
                asset,
                latest_draft_version_id=page.latest_draft_version_id,
                current_published_version_id=page.current_published_version_id,
                totals=totals.get(asset.id),
            )
            for asset in assets
        )

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
        """One page of a workspace's attachments, newest first.

        Page visibility and every filter apply in SQL before ``limit + 1``.
        Usage totals and preview candidates for the returned window are then
        read in one batch each, so the statement count does not grow with
        the window size.
        """

        actor = _reader(workspace_id, viewer, viewer_role)
        conditions = [
            _page_visible_to(actor),
            ContextMediaAsset.association_state != AssociationState.TOMBSTONED.value,
        ]
        if created_by is not None:
            conditions.append(
                ContextMediaAsset.created_by_actor_kind == created_by.kind
            )
            conditions.append(
                ContextMediaAsset.created_by_actor_id == str(created_by.id)
            )
        if page_id is not None:
            conditions.append(ContextMediaAsset.page_id == page_id)
        if orphan_only:
            conditions.append(context_media_orphan_clause())
        if used is not None:
            conditions.append(
                _asset_is_referenced() if used else ~_asset_is_referenced()
            )
        if filename_contains:
            # escape() so a user typing % or _ searches for those characters
            # rather than turning the box into a wildcard they cannot see.
            pattern = (
                filename_contains.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            conditions.append(
                ContextMediaAsset.original_filename.ilike(f"%{pattern}%", escape="\\")
            )
        if after is not None:
            cursor_created_at, cursor_asset_id = after
            # Written out rather than as a row-value comparison: tuple() IN/<
            # support differs between the SQLite and PostgreSQL profiles this
            # repo targets.
            conditions.append(
                or_(
                    ContextMediaAsset.created_at < cursor_created_at,
                    and_(
                        ContextMediaAsset.created_at == cursor_created_at,
                        ContextMediaAsset.id < cursor_asset_id,
                    ),
                )
            )

        fetched = tuple(
            self._session.execute(
                select_in_workspace(
                    ContextMediaAsset,
                    workspace_id=workspace_id,
                    conditions=tuple(conditions),
                )
                .join(ContextPage, _asset_page_join())
                .add_columns(
                    context_media_orphan_clause().label("is_orphan"),
                    ContextPage.title,
                    ContextPage.latest_draft_version_id,
                    ContextPage.current_published_version_id,
                )
                .order_by(
                    ContextMediaAsset.created_at.desc(),
                    ContextMediaAsset.id.desc(),
                )
                .limit(limit + 1)
            ).all()
        )
        has_more = len(fetched) > limit
        rows = fetched[:limit]
        # The last RETURNED row's key, not the discarded probe row's: cursoring
        # off the probe would skip it on the next page.
        next_after = (rows[-1][0].created_at, rows[-1][0].id) if has_more else None
        if not rows:
            return WorkspaceUsagePage(
                snapshots=(), has_more=has_more, next_after=next_after
            )

        assets = [row[0] for row in rows]
        totals = _usage_totals(
            self._session,
            workspace_id=workspace_id,
            asset_ids=(asset.id for asset in assets),
            reader=actor,
        )
        previews = _preview_version_ids(
            self._session,
            workspace_id=workspace_id,
            assets=assets,
            current_published_by_page={
                asset.page_id: published_id for asset, _, _, _, published_id in rows
            },
            reader=actor,
        )
        snapshots = tuple(
            ContextMediaAttachmentSnapshot(
                usage=_usage_snapshot(
                    asset,
                    latest_draft_version_id=latest_draft_id,
                    current_published_version_id=published_id,
                    totals=totals.get(asset.id),
                ),
                page_title=page_title,
                kind=asset.kind,
                created_at=asset.created_at,
                is_orphan=bool(is_orphan),
                preview_version_id=previews.get(asset.id),
            )
            for asset, is_orphan, page_title, latest_draft_id, published_id in rows
        )
        return WorkspaceUsagePage(
            snapshots=snapshots, has_more=has_more, next_after=next_after
        )

    def asset_usage_snapshots(
        self, workspace_id: UUID, asset_ids: Iterable[UUID]
    ) -> dict[UUID, AssetUsageSnapshot]:
        """Usage snapshots for a known batch of assets, keyed by asset id.

        For a caller holding no workspace read (the server-admin listing):
        every referencing version counts as unreadable.
        """

        ids = list(dict.fromkeys(asset_ids))
        if not ids:
            return {}
        rows = self._session.execute(
            select_in_workspace(
                ContextMediaAsset,
                workspace_id=workspace_id,
                conditions=(ContextMediaAsset.id.in_(ids),),
            )
            .join(ContextPage, _asset_page_join())
            .add_columns(
                ContextPage.latest_draft_version_id,
                ContextPage.current_published_version_id,
            )
        ).all()
        totals = _usage_totals(
            self._session,
            workspace_id=workspace_id,
            asset_ids=ids,
            reader=None,
        )
        return {
            asset.id: _usage_snapshot(
                asset,
                latest_draft_version_id=latest_draft_id,
                current_published_version_id=published_id,
                totals=totals.get(asset.id),
            )
            for asset, latest_draft_id, published_id in rows
        }

    def list_asset_version_usage(
        self,
        workspace_id: UUID,
        asset_id: UUID,
        *,
        viewer: ActorIdentity,
        viewer_role: str,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 25,
    ) -> AssetVersionUsagePage | None:
        """Readable versions that reference one asset, newest first.

        ``None`` when the asset is missing, tombstoned, or on a page the
        reader cannot see -- the same parent-page rule as the workspace
        listing. Unreadable versions are filtered in SQL before
        ``limit + 1`` and only ever surface as ``has_unreadable_usage``.
        """

        reader = _reader(workspace_id, viewer, viewer_role)
        target = self._session.execute(
            select_in_workspace(
                ContextMediaAsset,
                workspace_id=workspace_id,
                conditions=(
                    ContextMediaAsset.id == asset_id,
                    ContextMediaAsset.association_state
                    != AssociationState.TOMBSTONED.value,
                    _page_visible_to(reader),
                ),
            )
            .join(ContextPage, _asset_page_join())
            .with_only_columns(ContextPage.current_published_version_id)
        ).one_or_none()
        if target is None:
            return None
        version = aliased(ContextPageVersion)
        usage = ContextMediaUsage
        conditions = [
            usage.asset_id == asset_id,
            explicit_context_version_visible_expression(version, reader),
        ]
        if after is not None:
            cursor_created_at, cursor_version_id = after
            conditions.append(
                or_(
                    version.created_at < cursor_created_at,
                    and_(
                        version.created_at == cursor_created_at,
                        version.id < cursor_version_id,
                    ),
                )
            )
        fetched = self._session.execute(
            select_in_workspace(
                usage, workspace_id=workspace_id, conditions=tuple(conditions)
            )
            .join(version, _usage_version_join(version))
            .with_only_columns(
                version.id,
                version.version_no,
                version.version_status,
                usage.reference_count,
                version.created_at,
                version.last_saved_at,
            )
            .order_by(version.created_at.desc(), version.id.desc())
            .limit(limit + 1)
        ).all()
        has_more = len(fetched) > limit
        rows = fetched[:limit]
        totals = _usage_totals(
            self._session,
            workspace_id=workspace_id,
            asset_ids=(asset_id,),
            reader=reader,
        )
        return AssetVersionUsagePage(
            items=tuple(
                AssetVersionUsage(
                    version_id=row.id,
                    version_no=row.version_no,
                    version_status=row.version_status,
                    reference_count=row.reference_count,
                    created_at=row.created_at,
                    last_saved_at=row.last_saved_at,
                )
                for row in rows
            ),
            current_published_version_id=target.current_published_version_id,
            has_unreadable_usage=(
                asset_id in totals and totals[asset_id].has_unreadable
            ),
            has_more=has_more,
            next_after=(rows[-1].created_at, rows[-1].id) if has_more else None,
        )

    def get_page_asset_usage(
        self, workspace_id: UUID, page_id: UUID, asset_id: UUID
    ) -> AssetUsageSnapshot | None:
        page = self._page(workspace_id, page_id)
        asset = self._session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset_id},
        )
        if asset is None or asset.page_id != page_id:
            return None
        totals = _usage_totals(
            self._session,
            workspace_id=workspace_id,
            asset_ids=(asset_id,),
            reader=None,
        )
        return _usage_snapshot(
            asset,
            latest_draft_version_id=page.latest_draft_version_id,
            current_published_version_id=page.current_published_version_id,
            totals=totals.get(asset_id),
        )

    def tombstone_and_enqueue_gc(self, snapshot: AssetUsageSnapshot) -> None:
        workspace_id = snapshot.asset.workspace_id
        page_id = snapshot.asset.page_id
        _lock_context_workspace(self._session, workspace_id)
        self._logical_quota.lock_workspace(workspace_id)
        page = _lock_context_page(
            self._session,
            workspace_id=workspace_id,
            page_id=page_id,
        )
        if page is None:
            raise ContextMediaNotFoundError("Context media asset was not found")
        row = self._session.scalar(
            select(ContextMediaAsset)
            .where(
                ContextMediaAsset.workspace_id == workspace_id,
                ContextMediaAsset.page_id == page_id,
                ContextMediaAsset.id == snapshot.asset.asset_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.association_state == AssociationState.TOMBSTONED.value:
            raise ContextMediaNotFoundError("Context media asset was not found")
        # Lock the usage rows, then recount them under that lock with the
        # same aggregator every listing uses.
        self._session.execute(
            select_in_workspace(
                ContextMediaUsage,
                workspace_id=workspace_id,
                conditions=(
                    ContextMediaUsage.page_id == page_id,
                    ContextMediaUsage.asset_id == row.id,
                ),
            )
            .with_only_columns(ContextMediaUsage.page_version_id)
            .order_by(ContextMediaUsage.page_version_id)
            .with_for_update()
        ).all()
        locked_snapshot = _usage_snapshot(
            row,
            latest_draft_version_id=page.latest_draft_version_id,
            current_published_version_id=page.current_published_version_id,
            totals=_usage_totals(
                self._session,
                workspace_id=workspace_id,
                asset_ids=(row.id,),
                reader=None,
            ).get(row.id),
        )
        if locked_snapshot.usage_token != snapshot.usage_token:
            raise StaleUsageTokenError("usage token is stale")
        if (
            locked_snapshot.latest_draft_reference_count
            or locked_snapshot.current_published_reference_count
        ):
            raise ContextMediaUnavailableError("active media cannot be tombstoned")
        if locked_snapshot.historical_reference_count:
            raise ContextMediaUnavailableError("retained media cannot be tombstoned")
        now = datetime.now(UTC)
        next_revision = row.revision + 1
        result = self._session.execute(
            update(ContextMediaAsset)
            .where(
                ContextMediaAsset.workspace_id == workspace_id,
                ContextMediaAsset.page_id == page_id,
                ContextMediaAsset.id == row.id,
                ContextMediaAsset.association_state == AssociationState.ACTIVE.value,
                ContextMediaAsset.revision == row.revision,
            )
            .values(
                association_state=AssociationState.TOMBSTONED.value,
                revision=next_revision,
                tombstoned_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise StaleUsageTokenError("usage token is stale")
        self._logical_quota.release(workspace_id, asset_id=row.id)
        self._session.add(
            ContextMediaGcCandidate(
                workspace_id=workspace_id,
                id=uuid4(),
                asset_id=row.id,
                blob_id=row.blob_id,
                blob_epoch=row.blob_epoch,
                asset_revision=next_revision,
                state="queued",
                created_at=now,
            )
        )
        self._session.flush()
        self._session.expire(row)

    def drain_gc_candidate(self, *, workspace_id: UUID, asset_id: UUID) -> bool:
        """Row-level teardown for a queued GC candidate whose asset is
        tombstoned: deletes the upload-request marker (if any), the
        candidate row, and the asset row itself, in the only order the
        RESTRICT foreign keys allow (``ContextMediaUploadRequest`` and
        ``ContextMediaGcCandidate`` both RESTRICT to ``ContextMediaAsset``;
        the asset's own zero-usage precondition -- already enforced by the
        caller before tombstoning -- means ``ContextMediaUsage`` never
        blocks this).

        Physical bytes are deliberately NOT deleted here. ``blob_incarnation``
        is shared kernel storage, deduplicated per workspace+sha256 -- two
        ``ContextMediaAsset`` rows, or a Library/legacy-upload asset, can
        point at the same ``(blob_id, blob_epoch)``. This method therefore
        removes the Context owner edge from the durable cross-domain ledger;
        only the last owner queues ``MediaBlobGcCandidate``. The separately
        dispatched ``media_blob_gc`` application flow owns generation-fenced
        storage trash/purge and exact physical-quota release after this
        transaction commits.

        Returns False (no-op, not an error) if there is no queued candidate
        for this asset, or the asset is not tombstoned. Both should already
        hold by construction -- callers drain immediately after tombstoning
        -- but this stays defensive against a stale ``asset_id``.
        """
        _lock_context_workspace(self._session, workspace_id)
        candidate = self._session.scalar(
            select(ContextMediaGcCandidate)
            .where(
                ContextMediaGcCandidate.workspace_id == workspace_id,
                ContextMediaGcCandidate.asset_id == asset_id,
                ContextMediaGcCandidate.state == "queued",
            )
            .with_for_update()
        )
        if candidate is None:
            return False
        asset = self._session.scalar(
            select(ContextMediaAsset)
            .where(
                ContextMediaAsset.workspace_id == workspace_id,
                ContextMediaAsset.id == asset_id,
            )
            .with_for_update()
        )
        if (
            asset is None
            or asset.association_state != AssociationState.TOMBSTONED.value
        ):
            return False
        self._session.execute(
            delete(ContextMediaUploadRequest).where(
                ContextMediaUploadRequest.workspace_id == workspace_id,
                ContextMediaUploadRequest.asset_id == asset_id,
            )
        )
        self._session.delete(candidate)
        self._session.flush()
        self._blob_references.unregister(
            owner_kind=BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET,
            owner_id=asset.id,
            blob=BlobReference(
                workspace_id=asset.workspace_id,
                blob_id=asset.blob_id,
                epoch=BlobEpoch(asset.blob_epoch),
            ),
        )
        self._session.delete(asset)
        self._session.flush()
        return True

    def resolve_version_content(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        page_version_id: UUID,
        asset_id: UUID,
        actor: ActorIdentity,
    ) -> ContentCapability | None:
        asset = self._session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset_id},
        )
        if (
            asset is None
            or asset.page_id != page_id
            or asset.association_state != AssociationState.ACTIVE.value
        ):
            return None
        usage = self._session.get(
            ContextMediaUsage,
            {
                "workspace_id": workspace_id,
                "page_id": page_id,
                "asset_id": asset_id,
                "page_version_id": page_version_id,
            },
        )
        if usage is not None:
            # Real content: this exact version's Markdown references the
            # asset. The caller's outer version-visibility check
            # already decided whether this actor may see page_version_id at
            # all -- authorized uniformly like every other version's usage.
            #
            # NOT interchangeable with resolve_asset_content_capability below:
            # this method proves the asset is actually referenced by the ONE
            # version being served (via the usage row, or the narrower
            # uninserted-preview check further down) before ever calling
            # _content_capability. resolve_asset_content_capability skips
            # that proof entirely -- it would return content for ANY active
            # asset on the page, published or not. Do not "simplify" a public
            # or version-scoped read to call it instead.
            return self._content_capability(asset)
        # Uninserted uploads remain bound to the exact creating version.
        # The caller has already checked that version's read permission.
        if asset.created_for_version_id != page_version_id:
            return None
        return self._content_capability(asset)

    def resolve_asset_content_capability(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        asset_id: UUID,
    ) -> ContentCapability | None:
        # System-level blob-integrity check (e.g. legacy provenance
        # reconciliation confirming an already-anchored asset's stored
        # content still matches historical facts) -- not a visibility
        # decision, so unlike resolve_version_content this takes no actor
        # or page_version_id, and does NOT check that the asset is actually
        # referenced by any version's usage. NEVER call this from a REST/MCP
        # read path or anything else deciding what a caller may see: it
        # returns content for any ACTIVE asset on the page regardless of
        # whether it was ever published or inserted. Use
        # resolve_version_content (above) for
        # every visibility-scoped read.
        asset = self._session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset_id},
        )
        if (
            asset is None
            or asset.page_id != page_id
            or asset.association_state != AssociationState.ACTIVE.value
        ):
            return None
        return self._content_capability(asset)

    def _page(self, workspace_id: UUID, page_id: UUID) -> ContextPage:
        page = self._session.get(ContextPage, page_id)
        if page is None or page.workspace_id != workspace_id:
            raise ContextMediaNotFoundError("Context Page was not found")
        return page

    def _available_incarnation(
        self, reference: BlobReference
    ) -> BlobIncarnationRow | None:
        row = self._session.get(
            BlobIncarnationRow,
            {
                "workspace_id": reference.workspace_id,
                "blob_id": reference.blob_id,
                "blob_epoch": reference.epoch.value,
            },
        )
        if row is None or row.state != BlobState.AVAILABLE.value:
            return None
        if (
            self._session.get(
                SecurityBlockedBlobRow,
                {
                    "workspace_id": reference.workspace_id,
                    "blob_id": reference.blob_id,
                    "blob_epoch": reference.epoch.value,
                },
            )
            is not None
        ):
            return None
        return row

    def _content_capability(self, asset: ContextMediaAsset) -> ContentCapability | None:
        reference = BlobReference(
            workspace_id=asset.workspace_id,
            blob_id=asset.blob_id,
            epoch=BlobEpoch(asset.blob_epoch),
        )
        incarnation = self._available_incarnation(reference)
        inspection = self._session.get(
            StoredBlobInspectionRow,
            {"workspace_id": asset.workspace_id, "id": asset.inspection_id},
        )
        if (
            incarnation is None
            or inspection is None
            or inspection.blob_id != asset.blob_id
            or inspection.blob_epoch != asset.blob_epoch
            or not inspection.ordinary_available
            or inspection.canonical_media_type != asset.canonical_media_type
        ):
            return None
        return ContentCapability(
            asset_id=asset.id,
            blob=reference,
            storage_key=incarnation.storage_key,
            byte_size=incarnation.byte_size,
            canonical_media_type=asset.canonical_media_type,
            original_filename=asset.original_filename,
        )


class SqlAlchemyContextMediaUnitOfWork:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.repository: SqlAlchemyContextMediaRepository

    def __enter__(self) -> SqlAlchemyContextMediaUnitOfWork:
        self._session = self._session_factory()
        self.repository = SqlAlchemyContextMediaRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self._session is None:
            return False
        if exc_type is not None:
            self._session.rollback()
        self._session.close()
        self._session = None
        return False

    def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("Context media unit of work is not open")
        self._session.commit()

    def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("Context media unit of work is not open")
        self._session.rollback()


class SqlAlchemyContextMediaUnitOfWorkFactory:
    """Opens a UnitOfWork on whatever session_for(workspace_id) resolves to.

    D-7: Context media moved to the workspace plane, so there is no single
    session_factory to pin at construction -- every call names its own
    workspace and gets routed independently (a routing-capable
    DatabaseProvider may return a different physical database per call).
    """

    def __init__(self, session_for: Callable[[UUID], Callable[[], Session]]) -> None:
        self._session_for = session_for

    def __call__(self, workspace_id: UUID) -> SqlAlchemyContextMediaUnitOfWork:
        return SqlAlchemyContextMediaUnitOfWork(self._session_for(workspace_id))


def _asset_record(row: ContextMediaAsset) -> ContextMediaAssetRecord:
    return ContextMediaAssetRecord(
        workspace_id=row.workspace_id,
        asset_id=row.id,
        page_id=row.page_id,
        created_for_version_id=row.created_for_version_id,
        blob=BlobReference(
            workspace_id=row.workspace_id,
            blob_id=row.blob_id,
            epoch=BlobEpoch(row.blob_epoch),
        ),
        inspection_id=row.inspection_id,
        legacy_upload_asset_id=row.legacy_upload_asset_id,
        original_filename=row.original_filename,
        canonical_media_type=row.canonical_media_type,
        logical_bytes=row.logical_bytes,
        policy_revision=row.policy_revision,
        created_by=ActorIdentity(row.created_by_actor_kind, row.created_by_actor_id),
        association_state=AssociationState(row.association_state),
        revision=row.revision,
    )


# Bounded IN lists: SQLite builds without a raised variable limit cap a
# statement at 999 bound parameters.
_IN_BATCH = 400


@dataclass(frozen=True, slots=True)
class _UsageTotals:
    draft: int = 0
    published: int = 0
    retained: int = 0
    has_unreadable: bool = False


def _usage_version_join(version):
    return and_(
        version.workspace_id == ContextMediaUsage.workspace_id,
        version.page_id == ContextMediaUsage.page_id,
        version.id == ContextMediaUsage.page_version_id,
    )


def _asset_page_join():
    return and_(
        ContextPage.workspace_id == ContextMediaAsset.workspace_id,
        ContextPage.id == ContextMediaAsset.page_id,
    )


def _asset_is_referenced():
    """Correlated EXISTS: some version references the outer asset row."""

    return (
        select_in_workspace(
            ContextMediaUsage,
            workspace_id=ContextMediaAsset.workspace_id,
            conditions=(
                ContextMediaUsage.page_id == ContextMediaAsset.page_id,
                ContextMediaUsage.asset_id == ContextMediaAsset.id,
            ),
        )
        .with_only_columns(ContextMediaUsage.asset_id)
        .correlate_except(ContextMediaUsage)
        .exists()
    )


def _reader(
    workspace_id: UUID, viewer: ActorIdentity | None, viewer_role: str | None
) -> ActorContext | None:
    if viewer is None:
        return None
    return ActorContext(
        actor_kind=viewer.kind,
        actor_id=str(viewer.id),
        workspace_id=workspace_id,
        workspace_role=viewer_role or "viewer",
        role=viewer_role,
    )


def _page_visible_to(actor: ActorContext | None):
    """The attachment listings' parent-page rule for a joined ContextPage."""

    if actor_can_read_drafts(actor):
        return ContextPage.lifecycle_status != "archived"
    return visible_context_page_expression(actor)


def _usage_totals(
    session: Session,
    *,
    workspace_id: UUID,
    asset_ids: Iterable[UUID],
    reader: ActorContext | None,
) -> dict[UUID, _UsageTotals]:
    """Every referencing version's usage, summed per asset in one statement.

    The sums classify exactly as deletion does: versions whose status is
    ``draft``, the page's current published version, and every other
    retained version. ``reader`` decides only ``has_unreadable``, by the
    canonical version read rule; ``None`` is a caller holding no workspace
    read at all (the server-admin listing), for whom every referencing
    version is unreadable. It never changes a sum, so it never changes a
    ``usage_token``. Assets without usage are absent from the result.
    """

    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return {}
    version = aliased(ContextPageVersion)
    page = aliased(ContextPage)
    usage = ContextMediaUsage
    is_draft = version.version_status == "draft"
    is_published = and_(
        page.current_published_version_id.is_not(None),
        usage.page_version_id == page.current_published_version_id,
    )
    readable = (
        explicit_context_version_visible_expression(version, reader)
        if reader is not None
        else false()
    )
    totals: dict[UUID, _UsageTotals] = {}
    for start in range(0, len(ids), _IN_BATCH):
        rows = session.execute(
            select_in_workspace(
                usage,
                workspace_id=workspace_id,
                conditions=(usage.asset_id.in_(ids[start : start + _IN_BATCH]),),
            )
            .join(version, _usage_version_join(version))
            .join(
                page,
                and_(
                    page.workspace_id == usage.workspace_id,
                    page.id == usage.page_id,
                ),
            )
            .with_only_columns(
                usage.asset_id,
                func.sum(case((is_draft, usage.reference_count), else_=0)),
                func.sum(case((is_published, usage.reference_count), else_=0)),
                func.sum(
                    case(
                        (or_(is_draft, is_published), 0),
                        else_=usage.reference_count,
                    )
                ),
                func.max(case((readable, 0), else_=1)),
            )
            .group_by(usage.asset_id)
        ).all()
        for asset_id, draft, published, retained, unreadable in rows:
            totals[asset_id] = _UsageTotals(
                draft=int(draft),
                published=int(published),
                retained=int(retained),
                has_unreadable=bool(unreadable),
            )
    return totals


def _usage_snapshot(
    asset: ContextMediaAsset,
    *,
    latest_draft_version_id: UUID | None,
    current_published_version_id: UUID | None,
    totals: _UsageTotals | None,
) -> AssetUsageSnapshot:
    totals = totals or _UsageTotals()
    return AssetUsageSnapshot(
        asset=_asset_record(asset),
        latest_draft_version_id=latest_draft_version_id,
        current_published_version_id=current_published_version_id,
        latest_draft_reference_count=totals.draft,
        current_published_reference_count=totals.published,
        historical_reference_count=totals.retained,
        has_unreadable_usage=totals.has_unreadable,
    )


def _preview_version_ids(
    session: Session,
    *,
    workspace_id: UUID,
    assets: list[ContextMediaAsset],
    current_published_by_page: Mapping[UUID, UUID | None],
    reader: ActorContext | None,
) -> dict[UUID, UUID]:
    """The version each asset's preview is served through, when one exists.

    Candidates are exactly what the content route accepts: a version that
    references the asset, or the version it was uploaded for. Readability is
    the content route's own check (``context_media_version_is_readable``).
    Preference: current published, then the most recently saved referencing
    draft, then the upload's own version, then the newest other reference.
    A caller without a workspace read gets no preview.
    """

    if reader is None or not assets:
        return {}
    version = aliased(ContextPageVersion)
    asset_ids = [asset.id for asset in assets]
    referenced: dict[UUID, list] = defaultdict(list)
    for start in range(0, len(asset_ids), _IN_BATCH):
        for row in session.execute(
            select_in_workspace(
                ContextMediaUsage,
                workspace_id=workspace_id,
                conditions=(
                    ContextMediaUsage.asset_id.in_(
                        asset_ids[start : start + _IN_BATCH]
                    ),
                ),
            )
            .join(version, _usage_version_join(version))
            .with_only_columns(
                ContextMediaUsage.asset_id,
                version.id,
                version.version_status,
                version.created_at,
                version.last_saved_at,
            )
        ).all():
            referenced[row.asset_id].append(row)
    created_ids = list({asset.created_for_version_id for asset in assets})
    created = {}
    for start in range(0, len(created_ids), _IN_BATCH):
        for row in session.execute(
            select_in_workspace_by_ids(
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_ids=created_ids[start : start + _IN_BATCH],
            ).with_only_columns(
                ContextPageVersion.id,
                ContextPageVersion.version_status,
                ContextPageVersion.created_at,
                ContextPageVersion.last_saved_at,
            )
        ).all():
            created[row.id] = row

    previews: dict[UUID, UUID] = {}
    for asset in assets:
        published_id = current_published_by_page.get(asset.page_id)
        candidates = {row.id: row for row in referenced.get(asset.id, ())}
        referencing = set(candidates)
        own = created.get(asset.created_for_version_id)
        if own is not None:
            candidates.setdefault(own.id, own)

        def rank(
            row, *, asset=asset, published_id=published_id, referencing=referencing
        ):
            saved = row.last_saved_at or row.created_at
            if row.id == published_id:
                bucket = 0
            elif row.version_status == "draft" and row.id in referencing:
                bucket = 1
            elif row.id == asset.created_for_version_id:
                bucket = 2
            else:
                bucket = 3
            return (bucket, -saved.timestamp(), str(row.id))

        chosen = next(
            (
                row
                for row in sorted(candidates.values(), key=rank)
                if context_media_version_is_readable(
                    row,
                    current_published_version_id=published_id,
                    actor=reader,
                )
            ),
            None,
        )
        if chosen is not None:
            previews[asset.id] = chosen.id
    return previews


def _lock_context_workspace(
    session: Session, workspace_id: UUID
) -> WorkspaceMediaWriteIntent:
    """Enter the common Context-media write lock order.

    SQLite needs a write-intent fence before any mutable decision read.
    PostgreSQL keeps the workspace deletion gate shared and relies on the
    narrower session/page/asset ``FOR UPDATE`` locks below for concurrency.
    """

    acquire_write_intent(session)
    return _lock_open_workspace_media_intent(session, workspace_id)


def _lock_context_page_version(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    version_id: UUID,
) -> ContextPageVersion | None:
    return session.scalar(
        select(ContextPageVersion)
        .where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.page_id == page_id,
            ContextPageVersion.id == version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_context_page(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
) -> ContextPage | None:
    return session.scalar(
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id == page_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_open_workspace_media_intent(
    session: Session, workspace_id: UUID
) -> WorkspaceMediaWriteIntent:
    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        if session.get(Workspace, workspace_id) is None:
            raise ValueError("Workspace was not found")
        intent = WorkspaceMediaWriteIntent(
            workspace_id=workspace_id,
            state="open",
            delete_generation=0,
            updated_at=datetime.now(UTC),
        )
        session.add(intent)
        session.flush()
    if intent.state == "deleting":
        raise ValueError("workspace_media_deleting")
    if intent.state != "open":
        raise RuntimeError("invalid_workspace_media_intent_state")
    return intent


def _lock_required_open_workspace_media_intent(
    session: Session,
    workspace_id: UUID,
) -> WorkspaceMediaWriteIntent:
    """Lock the pre-provisioned global media barrier without adapter coupling."""

    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        raise ValueError("workspace_media_intent_not_configured")
    if intent.state == "deleting":
        raise ValueError("workspace_media_deleting")
    if intent.state != "open":
        raise RuntimeError("invalid_workspace_media_intent_state")
    return intent


def _validate_logical_charge(asset_id: UUID, byte_size: int) -> None:
    if not isinstance(asset_id, UUID):
        raise ValueError("Context asset id must be a UUID")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        raise ValueError("Context logical quota bytes must be non-negative")


__all__ = [
    "ContextLogicalQuotaExceededError",
    "SqlAlchemyContextLogicalQuota",
    "SqlAlchemyContextMediaRepository",
    "SqlAlchemyContextMediaUnitOfWork",
    "SqlAlchemyContextMediaUnitOfWorkFactory",
]
