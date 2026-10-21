"""SQLAlchemy persistence adapter for the Context attachment bounded context."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
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
from backend.repositories.context_media_orphans import (
    context_media_orphan_clause,
)
from backend.repositories.context_visibility import (
    explicit_context_version_visible_expression,
    foreign_editing_version_exists,
    visible_context_page_expression,
)
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
)
from backend.services.actor_visible_read import context_version_is_visible
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
        # P3.2: every upload must name the uploader's OWN currently-open
        # "editing" row -- not merely an existing version of the right page.
        # Locked (not just read) because two concurrent uploads racing the
        # same version's status must not both observe "editing".
        #
        # Same opacity rule P2 established for editing rows generally: a
        # different author's version -- editing or not -- is indistinguishable from a
        # nonexistent one, because even a distinguishable "belongs to
        # another actor" response would itself confirm this exact version
        # id currently exists in someone's private editing session to a
        # caller who merely names it. The version's OWN author gets a clear,
        # non-opaque signal instead when it simply isn't "editing" any more
        # (already submitted/published/discarded) -- that's ordinary
        # same-author feedback, not a disclosure to someone else.
        editing_version = _lock_context_page_version(
            self._session,
            workspace_id=asset.workspace_id,
            page_id=asset.page_id,
            version_id=asset.created_for_version_id,
        )
        if editing_version is None or (
            editing_version.created_by_actor_kind != asset.created_by.kind
            or editing_version.created_by_actor_id != asset.created_by.id
        ):
            raise ContextMediaNotFoundError("Context Page version was not found")
        if editing_version.version_status != "editing":
            raise ContextMediaEditingNotOpenError("editing version is not open")
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
            # P8.5: reject a "late association" race, without breaking the
            # ordinary, expected reuse of an asset that was uploaded but left
            # unused during ITS OWN session and only gets a body reference
            # later, in a different (e.g. post-publish) revision -- that
            # reuse is exactly what
            # `test_context_media_in_place_autosave_replaces_exact_version_
            # usage` exercises, and publishing an editing row is a normal,
            # deliberate close, not a race. The one case this closes: the
            # creating editing row was DISCARDED (its content and any of its
            # never-used uploads were deliberately thrown away) and this is
            # that asset's first-ever reference, arriving in some other
            # version afterward -- a stale client racing (or simply
            # forgetting about) the discard, not a legitimate editorial
            # choice. An asset already used anywhere (even in a now-
            # discarded version) is ordinary reuse and stays allowed
            # regardless of status, so this checks first-use provenance of a
            # DISCARDED origin specifically, not `version_status` in general
            # -- it cannot be "fixed" by widening a draft/published gate.
            first_use_candidates = [
                asset
                for asset in owned_assets
                if asset.created_for_version_id != page_version_id
            ]
            if first_use_candidates:
                already_used_ids = set(
                    self._session.scalars(
                        select(ContextMediaUsage.asset_id)
                        .where(
                            ContextMediaUsage.workspace_id == workspace_id,
                            ContextMediaUsage.asset_id.in_(
                                asset.id for asset in first_use_candidates
                            ),
                        )
                        .distinct()
                    ).all()
                )
                never_used = [
                    asset
                    for asset in first_use_candidates
                    if asset.id not in already_used_ids
                ]
                if never_used:
                    discarded_origin_ids = set(
                        self._session.scalars(
                            select(ContextPageVersion.id).where(
                                ContextPageVersion.workspace_id == workspace_id,
                                ContextPageVersion.id.in_(
                                    asset.created_for_version_id for asset in never_used
                                ),
                                ContextPageVersion.version_status == "discarded",
                            )
                        ).all()
                    )
                    if any(
                        asset.created_for_version_id in discarded_origin_ids
                        for asset in never_used
                    ):
                        raise ContextMediaEditingNotOpenError(
                            "logical reference was never associated before "
                            "its creating editing session was discarded"
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
        viewer: ActorIdentity | None = None,
    ) -> tuple[AssetUsageSnapshot, ...]:
        page = self._page(workspace_id, page_id)
        assets = tuple(
            self._session.scalars(
                select(ContextMediaAsset)
                .where(
                    ContextMediaAsset.workspace_id == workspace_id,
                    ContextMediaAsset.page_id == page_id,
                    # P8.4: an "editing" row is private to its author, and so
                    # are the uploads staged against it -- their filenames and
                    # the `created_for_version_id` the content URL is built
                    # from would otherwise hand a non-author both the private
                    # file's name and the exact version id that naming
                    # directly is required to 404 on. Excluded in the query,
                    # not after the fetch. A `None` viewer hides every
                    # editing-staged asset, matching editing_row_hidden_from_
                    # actor's fail-closed answer for an unidentified actor.
                    ~foreign_editing_version_exists(
                        ContextMediaAsset.created_for_version_id,
                        None if viewer is None else (viewer.kind, viewer.id),
                    ),
                )
                .order_by(ContextMediaAsset.created_at, ContextMediaAsset.id)
            ).all()
        )
        usages = tuple(
            self._session.scalars(
                select(ContextMediaUsage).where(
                    ContextMediaUsage.workspace_id == workspace_id,
                    ContextMediaUsage.page_id == page_id,
                )
            ).all()
        )
        grouped: dict[UUID, list[ContextMediaUsage]] = defaultdict(list)
        for usage in usages:
            grouped[usage.asset_id].append(usage)
        return tuple(
            _usage_snapshot(
                self._session,
                asset,
                page,
                grouped.get(asset.id, ()),
                viewer=viewer,
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
        filename_contains: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> WorkspaceUsagePage:
        """One page of a workspace's attachments, newest first.

        The SQL asset window includes page visibility before limit + 1.
        Pages, usage rows and preview versions are then read in batches for
        the returned window. Preview authorization does not replace parent
        page visibility, and never changes pagination after the SQL fetch.
        """

        actor = (
            ActorContext(
                actor_kind=viewer.kind,
                actor_id=str(viewer.id),
                workspace_id=workspace_id,
                workspace_role=viewer_role or "viewer",
                role=viewer_role,
            )
            if viewer is not None
            else None
        )
        referenced_version = aliased(ContextPageVersion)
        readable_reference = (
            select(ContextMediaUsage.asset_id)
            .join(
                referenced_version,
                referenced_version.id == ContextMediaUsage.page_version_id,
            )
            .where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.page_id == ContextMediaAsset.page_id,
                ContextMediaUsage.asset_id == ContextMediaAsset.id,
                referenced_version.workspace_id == workspace_id,
                referenced_version.page_id == ContextMediaAsset.page_id,
                explicit_context_version_visible_expression(referenced_version, actor),
            )
            .exists()
        )
        conditions = [
            ContextPage.workspace_id == workspace_id,
            visible_context_page_expression(actor),
            ContextMediaAsset.workspace_id == workspace_id,
            ContextMediaAsset.association_state != AssociationState.TOMBSTONED.value,
            # P8.4, and the reason this method needs the most care in the file:
            # every listing before it was scoped to one page, so a private
            # editing upload could only ever leak to someone already on that
            # page. This one spans the workspace, which makes it the first
            # surface that could hand a NON-AUTHOR another author's in-progress
            # filename and the created_for_version_id its content URL is keyed
            # on. Masked in the query, for every caller including an owner --
            # the row is private by identity, not by role -- and a `None`
            # viewer hides every editing-staged asset rather than none.
            or_(
                ~foreign_editing_version_exists(
                    ContextMediaAsset.created_for_version_id,
                    None if viewer is None else (viewer.kind, viewer.id),
                ),
                readable_reference,
            ),
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

        # `limit + 1`: the probe row that used to be the caller's job (a
        # route passing `limit + 1` down and computing paging state on
        # whatever survived a Python filter applied afterward -- see
        # `WorkspaceUsagePage`'s docstring for why that silently truncated a
        # filtered listing). `has_more`/`next_after` below are fixed from
        # this SQL-ordered fetch before any caller-side filtering runs.
        fetched = tuple(
            self._session.execute(
                select(
                    ContextMediaAsset,
                    context_media_orphan_clause().label("is_orphan"),
                )
                .join(ContextPage, ContextPage.id == ContextMediaAsset.page_id)
                .where(*conditions)
                .order_by(
                    ContextMediaAsset.created_at.desc(),
                    ContextMediaAsset.id.desc(),
                )
                .limit(limit + 1)
            ).all()
        )
        has_more = len(fetched) > limit
        rows = fetched[:limit]
        # The last RETURNED row's key -- not the probe row's. The probe row
        # (index `limit`, present only when `has_more`) is discarded here and
        # never reaches a caller, so cursoring off its key would skip it on
        # the next page: `created_at < probe.created_at` excludes the probe
        # row itself along with everything after it. Cursoring off
        # `rows[-1]` instead resumes exactly where the returned page ended.
        # A filter applied below `rows` can shrink `snapshots`, but by
        # construction cannot reach back and change `next_after`, because it
        # is fixed here first. `None` whenever `has_more` is false: a
        # genuinely final page issues no cursor, and fixing that here means
        # every caller inherits it for free instead of each one having to
        # remember to gate on `has_more` itself.
        next_after = (rows[-1][0].created_at, rows[-1][0].id) if has_more else None
        if not rows:
            return WorkspaceUsagePage(
                snapshots=(), has_more=has_more, next_after=next_after
            )

        assets = [row[0] for row in rows]
        page_ids = {asset.page_id for asset in assets}
        pages = {
            page.id: page
            for page in self._session.scalars(
                select(ContextPage).where(
                    ContextPage.workspace_id == workspace_id,
                    ContextPage.id.in_(page_ids),
                )
            ).all()
        }
        version_ids = {asset.created_for_version_id for asset in assets}
        # Warms the identity map; the result is deliberately unused.
        self._session.scalars(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.id.in_(version_ids),
            )
        ).all()
        asset_ids = {asset.id for asset in assets}
        grouped: dict[UUID, list[ContextMediaUsage]] = defaultdict(list)
        for usage in self._session.scalars(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.asset_id.in_(asset_ids),
            )
        ).all():
            grouped[usage.asset_id].append(usage)

        preview_versions = {
            version.id: version
            for version in self._session.scalars(
                select(ContextPageVersion).where(
                    ContextPageVersion.workspace_id == workspace_id,
                    ContextPageVersion.id.in_(
                        version_ids
                        | {u.page_version_id for uses in grouped.values() for u in uses}
                    ),
                )
            ).all()
        }
        snapshots = []
        for asset, is_orphan in rows:
            page = pages.get(asset.page_id)
            if page is None:
                # The asset's page FK is NOT NULL and workspace-scoped, so a
                # STABLE snapshot can't reach this; it takes a concurrent
                # delete of the page between the asset query above and this
                # one -- a separate SELECT, not a transaction boundary this
                # session enforces (260912 review: this comment used to claim
                # "unreachable through the API," which overstated it). Skip
                # rather than raise, so one bad row can't take out the whole
                # listing -- and skipping here changes only `snapshots`, never
                # `has_more`/`next_after`, which were already fixed above from
                # the raw SQL fetch (`WorkspaceUsagePage`'s docstring).
                continue
            # A reference in a readable version permits preview even when
            # the upload originated in another author's private edit. An
            # uninserted upload permits preview only to its own uploader.
            candidates = [u.page_version_id for u in grouped.get(asset.id, ())]
            if (asset.created_by_actor_kind, asset.created_by_actor_id) == (
                None if viewer is None else viewer.kind,
                None if viewer is None else str(viewer.id),
            ):
                candidates.append(asset.created_for_version_id)
            preview_version_id = next(
                (
                    version_id
                    for version_id in candidates
                    if version_id in preview_versions
                    and context_version_is_visible(preview_versions[version_id], actor)
                ),
                None,
            )
            snapshots.append(
                ContextMediaAttachmentSnapshot(
                    usage=_usage_snapshot(
                        self._session,
                        asset,
                        page,
                        grouped.get(asset.id, ()),
                        viewer=viewer,
                    ),
                    page_title=page.title,
                    kind=asset.kind,
                    created_at=asset.created_at,
                    is_orphan=bool(is_orphan),
                    preview_version_id=preview_version_id,
                )
            )
        return WorkspaceUsagePage(
            snapshots=tuple(snapshots), has_more=has_more, next_after=next_after
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
        usages = tuple(
            self._session.scalars(
                select(ContextMediaUsage).where(
                    ContextMediaUsage.workspace_id == workspace_id,
                    ContextMediaUsage.page_id == page_id,
                    ContextMediaUsage.asset_id == asset_id,
                )
            ).all()
        )
        return _usage_snapshot(self._session, asset, page, usages)

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
        usages = tuple(
            self._session.scalars(
                select(ContextMediaUsage)
                .where(
                    ContextMediaUsage.workspace_id == workspace_id,
                    ContextMediaUsage.page_id == page_id,
                    ContextMediaUsage.asset_id == row.id,
                )
                .order_by(ContextMediaUsage.page_version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        locked_snapshot = _usage_snapshot(self._session, row, page, usages)
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
            # asset. The caller's own outer version-visibility check (role
            # for draft/published, author-only for a private editing head)
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
        # P3.2/P3.3: no usage row means this asset has never been inserted
        # into ANY version's body -- an uninserted upload preview, allowed
        # only for the asset's own uploader, and only against the exact
        # version it was created for. This is narrower than "may this actor
        # see page_version_id": an uninserted image was never actually
        # published as content, so it does not become role-visible just
        # because the surrounding version (e.g. a submitted draft) is.
        if (
            asset.created_for_version_id != page_version_id
            or asset.created_by_actor_kind != actor.kind
            or asset.created_by_actor_id != actor.id
        ):
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
        # whether it was ever published, inserted, or is still someone's
        # private uninserted upload. Use resolve_version_content (above) for
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


def _usage_snapshot(
    session: Session,
    asset: ContextMediaAsset,
    page: ContextPage,
    usages: tuple[ContextMediaUsage, ...] | list[ContextMediaUsage],
    *,
    viewer: ActorIdentity | None = None,
) -> AssetUsageSnapshot:
    latest_count = 0
    published_count = 0
    historical_count = 0
    active_ids = {
        version_id
        for version_id in (
            page.latest_draft_version_id,
            page.current_published_version_id,
        )
        if version_id is not None
    }
    for usage in usages:
        if usage.page_version_id == page.latest_draft_version_id:
            latest_count += usage.reference_count
        if usage.page_version_id == page.current_published_version_id:
            published_count += usage.reference_count
        if usage.page_version_id not in active_ids:
            historical_count += usage.reference_count
    # P8.4: the buckets above are the real, unmasked canonical facts --
    # `usage_token`'s CAS round-trip (`get_page_asset_usage`/
    # `tombstone_and_enqueue_gc`) depends on them staying exactly this way
    # regardless of viewer, or delete's optimistic-concurrency check would
    # break. A *viewer-facing* count is a separate, optional projection:
    # any `ContextMediaUsage` row still keyed to a version that is CURRENTLY
    # "editing" and NOT authored by `viewer` is that author's private,
    # unsubmitted content -- P2.2's "at most one open editing session per
    # page+author" means several authors can each hold their own concurrent
    # editing row on the same page, so this is not just `latest_draft_
    # version_id`'s row; any usage row can point at one. Exclude every such
    # row from every viewer-visible bucket rather than only the "latest" one.
    viewer_latest_count = latest_count
    viewer_published_count = published_count
    viewer_historical_count = historical_count
    if viewer is not None:
        distinct_version_ids = {usage.page_version_id for usage in usages}
        foreign_editing_ids = (
            {
                version_id
                for version_id, status, actor_kind, actor_id in session.execute(
                    select(
                        ContextPageVersion.id,
                        ContextPageVersion.version_status,
                        ContextPageVersion.created_by_actor_kind,
                        ContextPageVersion.created_by_actor_id,
                    ).where(ContextPageVersion.id.in_(distinct_version_ids))
                ).all()
                if status == "editing"
                and (actor_kind, actor_id) != (viewer.kind, viewer.id)
            }
            if distinct_version_ids
            else set()
        )
        if foreign_editing_ids:
            viewer_latest_count = 0
            viewer_published_count = 0
            viewer_historical_count = 0
            for usage in usages:
                if usage.page_version_id in foreign_editing_ids:
                    continue
                if usage.page_version_id == page.latest_draft_version_id:
                    viewer_latest_count += usage.reference_count
                if usage.page_version_id == page.current_published_version_id:
                    viewer_published_count += usage.reference_count
                if usage.page_version_id not in active_ids:
                    viewer_historical_count += usage.reference_count
    # P3.4: identity-map lookup -- cheap when the same version was already
    # loaded earlier in this transaction (e.g. via _lock_context_page_version
    # during the upload that created this asset), a single trivial PK read
    # otherwise. Not a hot path (page-level media listing/delete), so this is
    # not batched across the whole page the way list_page_usage's own asset
    # query is.
    created_for_version = session.get(ContextPageVersion, asset.created_for_version_id)
    return AssetUsageSnapshot(
        asset=_asset_record(asset),
        latest_draft_version_id=page.latest_draft_version_id,
        current_published_version_id=page.current_published_version_id,
        latest_draft_reference_count=latest_count,
        current_published_reference_count=published_count,
        historical_reference_count=historical_count,
        created_for_version_is_editing=(
            created_for_version is not None
            and created_for_version.version_status == "editing"
        ),
        viewer_visible_latest_draft_reference_count=viewer_latest_count,
        viewer_visible_current_published_reference_count=viewer_published_count,
        viewer_visible_historical_reference_count=viewer_historical_count,
    )


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
