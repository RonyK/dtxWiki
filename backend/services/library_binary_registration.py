"""Database registration for an already admitted immutable Library upload."""

from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.library_media.ports import (
    LibraryBlobRepositoryFactory,
    LibraryLogicalQuotaFactory,
)
from backend.media.domain import BlobReference
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
)
from backend.services.audit_service import AuditService
from backend.services.conversion_orchestrator import (
    LibraryConversionOrchestrator,
    ScheduledConversion,
    get_or_create_library_blob,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.library_ingest import AcceptedLibraryUpload
from backend.services.library_service import library_item_for_version_insert_statement
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.preview_orchestrator import (
    LibraryPreviewOrchestrator,
    ScheduledPreview,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.settings_service import (
    normalized_library_policy,
    settings_policy_revision,
)
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.conversion import ConversionLimits
from backend.spi.preview import PreviewLimits


@dataclass(frozen=True, slots=True)
class BinaryLibraryRegistration:
    library_item: LibraryItem
    version: LibraryItemVersion
    conversion: ScheduledConversion | None
    preview: ScheduledPreview | None
    created_library_item: bool
    created_version: bool


class LibraryUploadPolicyChangedError(RuntimeError):
    """The policy snapshot used to stream an upload is no longer current."""

    error_code = "library_upload_policy_changed"

    def __init__(self, *, current_revision: str) -> None:
        super().__init__(
            "Library upload policy changed while the file was being validated; "
            "retry the upload."
        )
        self.current_revision = current_revision


_INERT_SOURCE_FORMATS = frozenset({"html", "javascript"})


def deterministic_new_item_id(workspace_id: UUID, upload_operation_id: UUID) -> UUID:
    """The id a `POST library/items` call with this operation id creates.

    Deterministic (not random) so that replaying the same operation id
    resolves to the same item on both this quota pre-check and the actual
    registration in ``register()`` below, instead of a fresh random id each
    attempt that would defeat replay detection entirely.
    """

    return uuid5(NAMESPACE_URL, f"library-item:{workspace_id}:{upload_operation_id}")


class LibraryBinaryRegistrationService:
    """Persist source identity, latest pointer, immutable recipe, and queue job."""

    def __init__(
        self,
        *,
        conversion_orchestrator: LibraryConversionOrchestrator,
        preview_orchestrator: LibraryPreviewOrchestrator | None = None,
        blob_repository_factory: LibraryBlobRepositoryFactory,
        logical_quota_factory: LibraryLogicalQuotaFactory,
        audit_service: AuditService | None = None,
    ) -> None:
        self.conversion_orchestrator = conversion_orchestrator
        self.preview_orchestrator = preview_orchestrator
        self.blob_repository_factory = blob_repository_factory
        self.logical_quota_factory = logical_quota_factory
        self.audit_service = audit_service or AuditService()

    def ensure_source_quota_capacity(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        existing_item_id: UUID | None,
        upload_operation_id: UUID,
        sha256: str,
        byte_size: int,
        expected_policy_revision: str,
    ) -> dict:
        """Lock quota and policy decision state before immutable installation.

        Taking the quota lock before deciding whether this is a replay makes a
        same-digest race observe the winner's committed version. The refreshed
        Workspace row remains locked in the caller-owned transaction through
        Shared Media installation and Library registration.

        ``existing_item_id`` is the caller's explicit target (P4.2: no
        filename-derived lookup) -- ``None`` means "a new item", scoping the
        content-hash reuse check to ``deterministic_new_item_id`` instead (so
        an idempotent replay of the same operation id is still free; a
        genuinely new operation is not). A given item id names the exact item
        this upload adds a version to; content-hash reuse is scoped to that
        one item only.
        """

        lookup_item_id = existing_item_id or deterministic_new_item_id(
            workspace_id, upload_operation_id
        )
        quota = self.logical_quota_factory(session)
        # Provision and lock the source ledger first.  A later item/version read
        # must observe any competing upload that held this lock before us.
        quota.ensure_capacity(
            workspace_id,
            quota_kind="source",
            additional_bytes=0,
        )
        workspace = session.scalar(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if workspace is None:
            raise ValueError("Workspace was not found")
        current_policy = normalized_library_policy(workspace.library_policy)
        current_revision = settings_policy_revision("library", current_policy)
        if not compare_digest(expected_policy_revision, current_revision):
            raise LibraryUploadPolicyChangedError(
                current_revision=current_revision,
            )

        existing_version_id = session.scalar(
            select(LibraryItemVersion.id).where(
                LibraryItemVersion.library_item_id == lookup_item_id,
                LibraryItemVersion.workspace_id == workspace_id,
                LibraryItemVersion.content_hash == sha256,
            )
        )
        quota.ensure_capacity(
            workspace_id,
            quota_kind="source",
            additional_bytes=0 if existing_version_id is not None else byte_size,
        )
        return current_policy

    def register(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        accepted: AcceptedLibraryUpload,
        workspace_policy: dict | None,
        limits: ConversionLimits,
        actor_kind: str,
        actor_id: str,
        upload_operation_id: UUID,
        existing_item_id: UUID | None = None,
        parent_node_id: UUID | None = None,
        schedule_optional_processing: bool = True,
    ) -> BinaryLibraryRegistration:
        """Register an admitted upload's bytes as a Library item version.

        P4.2: the caller states its intent explicitly instead of a filename
        deriving a synthetic URI that silently upserted same-named uploads
        into one item. ``existing_item_id`` is None for `POST library/items`
        (always a new item, so two uploads sharing a filename never merge)
        and set to the path's item id for
        `POST library/items/{item_id}/versions` (always that exact item,
        never a filename lookup).

        ``upload_operation_id`` is the client's idempotency handle. For a new
        item it also doubles as that item's identity seed (deterministic,
        not random) so that replaying the same operation id lands on the
        exact same item instead of creating a duplicate every retry -- the
        media-install layer already rejects a replay carrying different
        bytes before this method is ever called, so by the time this runs
        the only two cases are "genuinely new" (insert succeeds) and "exact
        replay" (insert collides on this deterministic id, and the existing
        row is fetched instead), mirroring how the old filename-URI upsert
        used to catch a collision -- just keyed by operation id, not by a
        human-chosen filename that unrelated uploads could coincidentally
        share.
        """

        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("Workspace was not found")
        # Begin SQLite's outer write transaction before the item-creation
        # savepoint so a later quota rejection rolls back the whole registration.
        acquire_write_intent(session)
        detection = accepted.detection
        source_blob = get_or_create_library_blob(
            session,
            repository_factory=self.blob_repository_factory,
            workspace_id=workspace_id,
            stored=accepted.blob,
            media_type=detection.detected_media_type,
            format_id=detection.format_id,
            detector_id=detection.detector,
            metadata={
                "original_filename": detection.original_filename,
                "extension": detection.extension,
                "confidence_milli": int(detection.confidence * 1000),
            },
        )
        created_item = False
        if existing_item_id is None:
            # Deterministic, not random: a replay of the same operation id
            # must resolve to the same item id so the insert below collides
            # (caught as a replay) instead of minting a duplicate item. Two
            # unrelated "new item" uploads get unrelated operation ids, so
            # they never collide with each other regardless of filename.
            new_item_id = deterministic_new_item_id(workspace_id, upload_operation_id)
            candidate = LibraryItem(
                id=new_item_id,
                workspace_id=workspace_id,
                connector_type="manual_upload",
                format_hint=detection.format_id,
                uri=f"upload://{workspace_id}/{new_item_id}",
                title=detection.original_filename,
                metadata_json={},
                # Passive rendering does not make an active source trustworthy
                # when it is downloaded and opened by another application.
                trust_level=(
                    "unknown"
                    if detection.format_id in _INERT_SOURCE_FORMATS
                    else "trusted"
                ),
                lifecycle_status="ready",
                created_by_actor_type=actor_kind,
                created_by_actor_id=_uuid_or_none(actor_id),
            )
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                item = candidate
                created_item = True
            except IntegrityError:
                item = session.scalar(
                    select(LibraryItem).where(
                        LibraryItem.workspace_id == workspace_id,
                        LibraryItem.id == new_item_id,
                    )
                )
                if item is None:
                    raise
        else:
            item = session.scalar(
                select(LibraryItem).where(
                    LibraryItem.workspace_id == workspace_id,
                    LibraryItem.id == existing_item_id,
                )
            )
            if (
                item is None
                or item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
            ):
                raise ReadNotFoundError("library item not found")
        if created_item:
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="library_item",
                target_id=item.id,
                title=item.title or item.uri,
                slug=None,
                section="library",
                parent_id=parent_node_id,
                default_parent_slug="library",
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
            )

        item = session.scalar(library_item_for_version_insert_statement(item.id))
        if item is None:
            raise ValueError("Library item disappeared during upload registration")
        item.format_hint = detection.format_id
        if (
            detection.format_id in _INERT_SOURCE_FORMATS
            and item.trust_level != "restricted"
        ):
            # Reused items and alias changes must not preserve a stale trusted
            # label for a newly active original. Explicit restriction wins.
            item.trust_level = "unknown"
        if not item.title:
            item.title = detection.original_filename

        version = session.scalar(
            select(LibraryItemVersion).where(
                LibraryItemVersion.library_item_id == item.id,
                LibraryItemVersion.content_hash == accepted.blob.sha256,
            )
        )
        created_version = version is None
        if version is None:
            latest_version_no = session.scalar(
                select(LibraryItemVersion.version_no)
                .where(LibraryItemVersion.library_item_id == item.id)
                .order_by(LibraryItemVersion.version_no.desc())
                .limit(1)
            )
            version = LibraryItemVersion(
                id=uuid4(),
                workspace_id=item.workspace_id,
                library_item_id=item.id,
                version_no=(latest_version_no or 0) + 1,
                content_hash=accepted.blob.sha256,
                raw_storage_ref=None,
                source_blob_workspace_id=source_blob.reference.workspace_id,
                source_blob_id=source_blob.reference.blob_id,
                source_blob_epoch=source_blob.reference.epoch.value,
                original_filename=detection.original_filename,
                declared_media_type=detection.declared_media_type,
                detected_media_type=detection.detected_media_type,
                format_id=detection.format_id,
                byte_size=accepted.blob.byte_size,
                detection_json=detection.as_json(),
                processing_status=(
                    accepted.processing_disposition
                    if schedule_optional_processing
                    else "stored_unconverted"
                ),
                previous_version_id=item.current_version_id,
            )
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION,
                owner_id=version.id,
                blob=source_blob.reference,
            )
            session.add(version)
            session.flush()
        else:
            self._verify_or_upgrade_existing_version(
                version=version,
                accepted=accepted,
                source_blob=source_blob.reference,
            )
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION,
                owner_id=version.id,
                blob=source_blob.reference,
            )

        # This registration operation resulted in `version` either way (fresh
        # insert or content-hash dedup reuse) -- it becomes current regardless
        # of which branch produced it, otherwise re-uploading bytes matching an
        # older version leaves current_version_id pointing at a version that
        # is no longer the one this operation just registered.
        item.current_version_id = version.id

        self.logical_quota_factory(session).reserve(
            workspace_id,
            quota_kind="source",
            associations={version.id: accepted.blob.byte_size},
        )
        session.add_all([item, version])
        session.flush()

        scheduled = None
        preview = None
        if schedule_optional_processing:
            scheduled = self.schedule_conversion(
                session=session,
                workspace_id=workspace_id,
                version_id=version.id,
                workspace_policy=workspace_policy,
                limits=limits,
                actor_kind=actor_kind,
                actor_id=actor_id,
                allow_conversion=accepted.processing_disposition == "queued",
            )
            preview = self.schedule_preview(
                session=session,
                workspace_id=workspace_id,
                version_id=version.id,
                limits=limits,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="library.upload.accepted",
            target_kind="library_item",
            target_id=item.id,
            payload={
                "library_item_version_id": str(version.id),
                "source_sha256": accepted.blob.sha256,
                "byte_size": accepted.blob.byte_size,
                "format_id": detection.format_id,
                "processing_status": version.processing_status,
                "created_library_item": created_item,
                "created_version": created_version,
            },
        )
        return BinaryLibraryRegistration(
            library_item=item,
            version=version,
            conversion=scheduled,
            preview=preview,
            created_library_item=created_item,
            created_version=created_version,
        )

    def schedule_conversion(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        workspace_policy: dict | None,
        limits: ConversionLimits,
        actor_kind: str,
        actor_id: str,
        allow_conversion: bool,
    ) -> ScheduledConversion:
        """Persist optional semantic work after the source transaction commits."""

        version = session.get(LibraryItemVersion, version_id)
        if version is None:
            raise ValueError("Library source version was not found for conversion")
        return self.conversion_orchestrator.schedule_initial(
            session=session,
            workspace_id=workspace_id,
            version=version,
            workspace_policy=workspace_policy,
            limits=limits,
            actor_kind=actor_kind,
            actor_id=actor_id,
            allow_conversion=allow_conversion,
        )

    def schedule_preview(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        limits: ConversionLimits,
        actor_kind: str,
        actor_id: str,
    ) -> ScheduledPreview | None:
        """Persist optional Preview work independently from source acceptance."""

        if self.preview_orchestrator is None:
            return None
        return self.preview_orchestrator.schedule(
            session=session,
            workspace_id=workspace_id,
            version_id=version_id,
            limits=PreviewLimits(
                max_input_bytes=limits.max_input_bytes,
                max_variant_bytes=min(
                    limits.max_asset_bytes,
                    limits.max_output_bytes,
                ),
                max_total_output_bytes=limits.max_output_bytes,
                timeout_seconds=limits.timeout_seconds,
            ),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason="initial",
        )

    @staticmethod
    def _verify_or_upgrade_existing_version(
        *,
        version: LibraryItemVersion,
        accepted: AcceptedLibraryUpload,
        source_blob: BlobReference,
    ) -> None:
        detection = accepted.detection
        if version.content_hash != accepted.blob.sha256:
            raise ValueError("Library version source hash conflict")
        if version.source_blob_id is not None and (
            version.source_blob_workspace_id != source_blob.workspace_id
            or version.source_blob_id != source_blob.blob_id
            or version.source_blob_epoch != source_blob.epoch.value
        ):
            raise ValueError("Library version source identity conflict")
        if (
            version.byte_size is not None
            and version.byte_size != accepted.blob.byte_size
        ):
            raise ValueError("Library version source size conflict")
        for attribute, expected in (
            ("original_filename", detection.original_filename),
            ("detected_media_type", detection.detected_media_type),
            ("format_id", detection.format_id),
        ):
            current = getattr(version, attribute)
            if current is not None and current != expected:
                raise ValueError(f"Library version {attribute} conflict")

        # Existing Library versions are immutable provenance records.  This
        # branch also upgrades rows created before binary-source metadata was
        # introduced, so fill only absent legacy fields and never rewrite a
        # first-seen client Content-Type or detection evidence on a replay.
        if version.source_blob_id is None:
            version.source_blob_workspace_id = source_blob.workspace_id
            version.source_blob_id = source_blob.blob_id
            version.source_blob_epoch = source_blob.epoch.value
        if version.original_filename is None:
            version.original_filename = detection.original_filename
        if version.declared_media_type is None:
            version.declared_media_type = detection.declared_media_type
        if version.detected_media_type is None:
            version.detected_media_type = detection.detected_media_type
        if version.format_id is None:
            version.format_id = detection.format_id
        if version.byte_size is None:
            version.byte_size = accepted.blob.byte_size
        if version.detection_json is None:
            version.detection_json = detection.as_json()


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "BinaryLibraryRegistration",
    "LibraryBinaryRegistrationService",
    "LibraryUploadPolicyChangedError",
]
