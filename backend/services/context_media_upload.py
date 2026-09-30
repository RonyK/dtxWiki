"""Upload bridge into page-owned Context assets and Shared Media."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.context_media.application import RegisterMediaAsset
from backend.context_media.domain import (
    CONTEXT_MEDIA_POLICY_REVISION,
    ActorIdentity,
    ContextMediaAssetRecord,
    RegisterContextMediaAsset,
    context_image_format_for_media_type,
    context_image_inspection_schema_revision,
)
from backend.context_media.ports import (
    ContextLogicalQuotaFactory,
    ContextMediaRepository,
)
from backend.media.application import InstallValidatedBlob
from backend.media.ports.contracts import IdempotencyConflictError
from backend.media.ports.install import (
    BlobAssociationHold,
    BlobAssociationHoldProducerKind,
)
from backend.models.context_media import ContextMediaUploadRequest
from backend.models.media import (
    MediaInstallRequest,
    MediaStagingLease,
    StoredBlobInspectionRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.services.context_image_validation import (
    ValidatedContextImage,
)
from backend.services.db_write_intent import acquire_write_intent


@dataclass(frozen=True, slots=True)
class ContextMediaUploadResult:
    asset: ContextMediaAssetRecord
    reused: bool

    @property
    def logical_ref(self) -> str:
        return self.asset.logical_reference

    @property
    def content_url(self) -> str:
        # P3.3: every private content URL is exact-version now -- the
        # version this asset was created for is always a valid one to name,
        # whether or not it has since become the current draft/published
        # head (resolve_version_content's usage-row lookup and its
        # uninserted-preview fallback both key on this same id).
        return (
            f"/api/v1/workspaces/{self.asset.workspace_id}/context/pages/"
            f"{self.asset.page_id}/versions/{self.asset.created_for_version_id}"
            f"/media/{self.asset.asset_id}/content"
        )


class ContextMediaUploadService:
    """Register a validated Context image while preserving the legacy API."""

    def __init__(
        self,
        *,
        media_installer_factory: Callable[
            [sessionmaker[Session], UUID], InstallValidatedBlob
        ],
        logical_quota_factory: ContextLogicalQuotaFactory,
        context_media_repository_factory: Callable[[object], ContextMediaRepository],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._media_installer_factory = media_installer_factory
        self._logical_quota_factory = logical_quota_factory
        self._context_media_repository_factory = context_media_repository_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        asset_id: UUID,
        original_filename: str,
        canonical_media_type: str,
        content: bytes,
        sha256: str,
        page_id: UUID,
        editing_version_id: UUID,
        actor_kind: str,
        actor_id: str,
        max_bytes: int,
        validated_image: ValidatedContextImage,
        policy_revision: str = CONTEXT_MEDIA_POLICY_REVISION,
        legacy_upload_asset_id: UUID | None = None,
    ) -> ContextMediaUploadResult:
        image_format = context_image_format_for_media_type(canonical_media_type)
        if image_format is None:
            raise ValueError("Context media supports PNG, JPEG, GIF, and WebP only")
        normalized_filename = _normalized_filename(original_filename)
        if not policy_revision or len(policy_revision) > 160:
            raise ValueError("Context media policy revision is invalid")
        if legacy_upload_asset_id is not None and not isinstance(
            legacy_upload_asset_id, UUID
        ):
            raise ValueError("legacy_upload_asset_id must be a UUID")
        staging_actor_id = _stable_actor_uuid(actor_kind, actor_id)
        actor = ActorIdentity(actor_kind, actor_id)
        now = self._clock()
        # Preflight replay/legacy-anchor check in a short domain transaction,
        # then close it before Shared Media performs its durable DB/filesystem
        # install.  Publishing through a journal bound to this domain UoW would
        # let a later association rollback erase every charge/request row while
        # leaving the already-published file behind. The actual editing-version
        # ownership/status check (P3.2) happens inside RegisterMediaAsset ->
        # repository.add_asset, in the SECOND transaction below, where the
        # asset row is actually created -- a preflight check here would just
        # be a second, driftable copy of that same authorization decision.
        with session_factory.begin() as transaction:
            _lock_workspace_media_uploads(transaction, workspace_id)
            context_repository = self._context_media_repository_factory(transaction)
            if legacy_upload_asset_id is not None:
                self._logical_quota_factory(transaction).lock_workspace(workspace_id)
                _assert_legacy_anchor(
                    context_repository,
                    workspace_id=workspace_id,
                    asset_id=asset_id,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                )
            request = transaction.get(
                ContextMediaUploadRequest,
                {"workspace_id": workspace_id, "marker_uuid": asset_id},
            )
            existing = context_repository.get_asset(workspace_id, asset_id)
            if request is not None:
                _assert_upload_replay(
                    request=request,
                    existing=existing,
                    page_id=page_id,
                    editing_version_id=editing_version_id,
                    sha256=sha256,
                    byte_size=len(content),
                    canonical_media_type=canonical_media_type,
                    normalized_filename=normalized_filename,
                    policy_revision=policy_revision,
                    actor=actor,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                )
                assert existing is not None
                return ContextMediaUploadResult(asset=existing, reused=True)
            if existing is not None:
                expected_owner = (page_id, editing_version_id)
                actual_owner = (existing.page_id, existing.created_for_version_id)
                if (
                    actual_owner != expected_owner
                    or existing.created_by != actor
                    or existing.original_filename != normalized_filename
                    or existing.canonical_media_type != canonical_media_type
                    or existing.logical_bytes != len(content)
                    or existing.policy_revision != policy_revision
                    or existing.legacy_upload_asset_id != legacy_upload_asset_id
                ):
                    raise ValueError(
                        "upload marker already identifies a different Context asset"
                    )

        if validated_image.canonical_media_type != canonical_media_type:
            raise ValueError("validated Context image media type mismatch")

        def assert_not_historical_policy_replay() -> None:
            with session_factory.begin() as transaction:
                _assert_current_policy_does_not_replay_historical_install(
                    transaction,
                    workspace_id=workspace_id,
                    asset_id=asset_id,
                    staging_actor_id=staging_actor_id,
                    page_id=page_id,
                    editing_version_id=editing_version_id,
                    actor=actor,
                    normalized_filename=normalized_filename,
                    canonical_media_type=canonical_media_type,
                    content=content,
                    sha256=sha256,
                    policy_revision=policy_revision,
                    validated_image=validated_image,
                    image_format_id=image_format.format_id,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                )

        assert_not_historical_policy_replay()
        inspection_schema_revision = context_image_inspection_schema_revision(
            policy_revision
        )
        try:
            available = self._media_installer_factory(
                session_factory, workspace_id
            ).execute(
                workspace_id=workspace_id,
                actor_id=staging_actor_id,
                session_key=f"context:{editing_version_id}",
                idempotency_key=f"context:{asset_id}",
                content=content,
                max_bytes=max_bytes,
                canonical_media_type=canonical_media_type,
                format_id=image_format.format_id,
                detector_id="context-image-signature",
                detector_version="1",
                registry_version=inspection_schema_revision,
                validation_schema_version=inspection_schema_revision,
                metadata=_context_inspection_metadata(
                    asset_id=asset_id,
                    actor=actor,
                    page_id=page_id,
                    editing_version_id=editing_version_id,
                    normalized_filename=normalized_filename,
                    validated_image=validated_image,
                    policy_revision=policy_revision,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                ),
                admit_new_operation=(policy_revision == CONTEXT_MEDIA_POLICY_REVISION),
                association_hold=BlobAssociationHold(
                    producer_kind=(
                        BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD
                    ),
                    producer_id=asset_id,
                    producer_generation=1,
                    # This is the durable synchronous association lease, not
                    # GC's liveness guess. A replay extends it before reusing
                    # the blob; once it expires, registration loses safely to
                    # a generation-fenced collector instead of attaching to
                    # missing bytes.
                    expires_at=now + timedelta(hours=1),
                ),
            )
        except IdempotencyConflictError:
            # Close the race where a historical install becomes durable after
            # the preflight read but before the current installer binds.
            assert_not_historical_policy_replay()
            raise
        if (
            available.reference.workspace_id != workspace_id
            or available.sha256.value != sha256
            or available.byte_size != len(content)
            or available.canonical_media_type != canonical_media_type
        ):
            raise RuntimeError("Shared Media installer changed immutable upload facts")

        # Association activation and logical quota remain one idempotent domain
        # decision. If this transaction rejects or rolls back, the immutable
        # blob is still durably tracked and physically charged for reconciliation.
        with session_factory.begin() as transaction:
            _lock_workspace_media_uploads(transaction, workspace_id)
            logical_quota = self._logical_quota_factory(transaction)
            # This row is the operation-binding decision lock. It must precede
            # request/asset reads so identical contenders observe the committed
            # winner instead of both deciding that the marker is unbound.
            logical_quota.lock_workspace(workspace_id)
            context_repository = self._context_media_repository_factory(transaction)
            if legacy_upload_asset_id is not None:
                _assert_legacy_anchor(
                    context_repository,
                    workspace_id=workspace_id,
                    asset_id=asset_id,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                )
            request = transaction.get(
                ContextMediaUploadRequest,
                {"workspace_id": workspace_id, "marker_uuid": asset_id},
            )
            existing = context_repository.get_asset(workspace_id, asset_id)
            if request is not None:
                _assert_upload_replay(
                    request=request,
                    existing=existing,
                    page_id=page_id,
                    editing_version_id=editing_version_id,
                    sha256=sha256,
                    byte_size=len(content),
                    canonical_media_type=canonical_media_type,
                    normalized_filename=normalized_filename,
                    policy_revision=policy_revision,
                    actor=actor,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                )
                assert existing is not None
                return ContextMediaUploadResult(asset=existing, reused=True)
            if existing is not None:
                if (
                    (existing.page_id, existing.created_for_version_id)
                    != (page_id, editing_version_id)
                    or existing.created_by != actor
                    or existing.blob != available.reference
                    or existing.inspection_id != available.inspection_id
                    or existing.canonical_media_type != canonical_media_type
                    or existing.logical_bytes != len(content)
                    or existing.original_filename != normalized_filename
                    or existing.policy_revision != policy_revision
                    or existing.legacy_upload_asset_id != legacy_upload_asset_id
                ):
                    raise ValueError(
                        "upload marker already identifies a different Context asset"
                    )
                asset = existing
            else:
                logical_quota.reserve(
                    workspace_id,
                    asset_id=asset_id,
                    byte_size=len(content),
                )
                # P3.2: repository.add_asset (adapter) is where the
                # editing-version ownership/status check actually happens --
                # it locks and validates ContextPageVersion directly, a
                # foreign table this bounded context's own Protocol has no
                # notion of, the same way it already locks/validates
                # ContextPage. See SqlAlchemyContextMediaRepository.add_asset.
                asset = RegisterMediaAsset().execute(
                    context_repository,
                    RegisterContextMediaAsset(
                        workspace_id=workspace_id,
                        asset_id=asset_id,
                        page_id=page_id,
                        created_for_version_id=editing_version_id,
                        blob=available.reference,
                        inspection_id=available.inspection_id,
                        original_filename=normalized_filename,
                        canonical_media_type=canonical_media_type,
                        logical_bytes=len(content),
                        policy_revision=policy_revision,
                        actor=actor,
                        legacy_upload_asset_id=legacy_upload_asset_id,
                    ),
                    now=now,
                )
            transaction.add(
                ContextMediaUploadRequest(
                    workspace_id=workspace_id,
                    marker_uuid=asset_id,
                    page_id=asset.page_id,
                    editing_version_id=asset.created_for_version_id,
                    asset_id=asset.asset_id,
                    sha256=sha256,
                    actor_kind=actor.kind,
                    actor_id=actor.id,
                )
            )
            transaction.flush()
        return ContextMediaUploadResult(asset=asset, reused=False)


def _stable_actor_uuid(actor_kind: str, actor_id: str) -> UUID:
    try:
        return UUID(actor_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"media-actor:{actor_kind}:{actor_id}")


def _lock_workspace_media_uploads(session: Session, workspace_id: UUID) -> None:
    """Serialize one workspace's operation binding before any upload side effect."""

    acquire_write_intent(session)
    # The media intent is the universal first workspace-scoped media lock.  In
    # particular, do not lock Workspace first: hard delete takes the intent and
    # later deletes Workspace, so the reverse order would permit a two-session
    # Workspace <-> WorkspaceMediaWriteIntent deadlock on PostgreSQL.
    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        raise ValueError("workspace_media_intent_not_configured")
    if intent.state != "open":
        raise ValueError("workspace_media_deleting")
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("Workspace was not found")
    assert intent.state == "open"


def _normalized_filename(value: str) -> str:
    candidate = unicodedata.normalize("NFKC", value or "image")
    candidate = re.split(r"[\\/]", candidate)[-1]
    candidate = "".join(
        character for character in candidate if character >= " " and character != "\x7f"
    ).strip()
    return (candidate or "image")[:255]


def _assert_upload_replay(
    *,
    request: ContextMediaUploadRequest,
    existing: ContextMediaAssetRecord | None,
    page_id: UUID,
    editing_version_id: UUID,
    sha256: str,
    byte_size: int,
    canonical_media_type: str,
    normalized_filename: str,
    policy_revision: str,
    actor: ActorIdentity,
    legacy_upload_asset_id: UUID | None,
) -> None:
    owner_matches = (page_id, editing_version_id) == (
        request.page_id,
        request.editing_version_id,
    )
    non_policy_facts_changed = (
        existing is None
        or request.asset_id != existing.asset_id
        or not owner_matches
        or request.sha256 != sha256
        or request.actor_kind != actor.kind
        or request.actor_id != actor.id
        or existing.created_by != actor
        or existing.logical_bytes != byte_size
        or existing.canonical_media_type != canonical_media_type
        or existing.original_filename != normalized_filename
        or existing.legacy_upload_asset_id != legacy_upload_asset_id
    )
    if non_policy_facts_changed:
        raise ValueError("upload marker already identifies a different Context asset")
    assert existing is not None
    if existing.policy_revision != policy_revision:
        if policy_revision == CONTEXT_MEDIA_POLICY_REVISION:
            raise ValueError("media_policy_changed")
        raise ValueError("upload marker already identifies a different Context asset")


def _assert_current_policy_does_not_replay_historical_install(
    session: Session,
    *,
    workspace_id: UUID,
    asset_id: UUID,
    staging_actor_id: UUID,
    page_id: UUID,
    editing_version_id: UUID,
    actor: ActorIdentity,
    normalized_filename: str,
    canonical_media_type: str,
    content: bytes,
    sha256: str,
    policy_revision: str,
    validated_image: ValidatedContextImage,
    image_format_id: str,
    legacy_upload_asset_id: UUID | None,
) -> None:
    """Reject an exact current-style replay of a historical partial install.

    Shared Media binds the policy revision inside immutable inspection metadata.
    Context association can still fail after that install commits, leaving no
    Context request row for the ordinary replay check. Read and compare the
    durable Shared Media fingerprint before invoking the installer so the
    policy error has no database, quota, staging, blob, or filesystem effect.
    """

    if policy_revision != CONTEXT_MEDIA_POLICY_REVISION:
        return
    idempotency_key = f"context:{asset_id}"
    lease = session.scalar(
        select(MediaStagingLease).where(
            MediaStagingLease.workspace_id == workspace_id,
            MediaStagingLease.idempotency_key == idempotency_key,
        )
    )
    if lease is None:
        return
    request = session.scalar(
        select(MediaInstallRequest).where(
            MediaInstallRequest.workspace_id == workspace_id,
            MediaInstallRequest.idempotency_key == idempotency_key,
        )
    )
    if request is None:
        return
    inspection = session.get(
        StoredBlobInspectionRow,
        {"workspace_id": workspace_id, "id": request.inspection_id},
    )
    if inspection is None:
        return
    metadata = dict(inspection.metadata_json)
    stored_policy_revision = metadata.pop("context_policy_revision", None)
    if not isinstance(stored_policy_revision, str) or (
        stored_policy_revision == policy_revision
    ):
        return

    expected_metadata = _context_inspection_metadata(
        asset_id=asset_id,
        actor=actor,
        page_id=page_id,
        editing_version_id=editing_version_id,
        normalized_filename=normalized_filename,
        validated_image=validated_image,
        policy_revision=stored_policy_revision,
        legacy_upload_asset_id=legacy_upload_asset_id,
    )
    expected_metadata.pop("context_policy_revision")
    exact_non_policy_fingerprint = (
        lease.actor_id == staging_actor_id
        and lease.session_key == f"context:{editing_version_id}"
        and lease.byte_size == len(content)
        and lease.sha256 == sha256
        and request.staging_id == lease.id
        and request.staging_generation == lease.generation
        and inspection.detector_id == "context-image-signature"
        and inspection.detector_version == "1"
        and inspection.format_id == image_format_id
        and inspection.canonical_media_type == canonical_media_type
        and metadata == expected_metadata
    )
    if exact_non_policy_fingerprint:
        raise ValueError("media_policy_changed")


def _assert_legacy_anchor(
    repository: ContextMediaRepository,
    *,
    workspace_id: UUID,
    asset_id: UUID,
    legacy_upload_asset_id: UUID,
) -> None:
    anchored = repository.get_asset_by_legacy_upload(
        workspace_id,
        legacy_upload_asset_id,
        for_update=True,
    )
    if anchored is not None and anchored.asset_id != asset_id:
        raise ValueError("legacy upload asset already has a different Context anchor")


def _context_inspection_metadata(
    *,
    asset_id: UUID,
    actor: ActorIdentity,
    page_id: UUID,
    editing_version_id: UUID,
    normalized_filename: str,
    validated_image: ValidatedContextImage,
    policy_revision: str,
    legacy_upload_asset_id: UUID | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "context_actor_id": actor.id,
        "context_actor_kind": actor.kind,
        "context_asset_id": str(asset_id),
        "context_original_filename": normalized_filename,
        "context_image_height": validated_image.height,
        "context_image_width": validated_image.width,
        "context_page_id": str(page_id),
        "context_editing_version_id": str(editing_version_id),
    }
    # ``context-mvp-v1`` was committed before animated-image resource facts
    # became part of the immutable Shared Media inspection fingerprint.  Exact
    # historical retries must reproduce that older shape; adding the v2 fields
    # turns a recoverable partial install into an idempotency conflict.
    if policy_revision != "context-mvp-v1":
        metadata["context_image_frame_count"] = validated_image.frame_count
        metadata["context_image_total_pixels"] = validated_image.total_pixels
    if legacy_upload_asset_id is not None:
        metadata["context_legacy_upload_asset_id"] = str(legacy_upload_asset_id)
    metadata["context_policy_revision"] = policy_revision
    return metadata


__all__ = [
    "CONTEXT_MEDIA_POLICY_REVISION",
    "ContextMediaUploadResult",
    "ContextMediaUploadService",
]
