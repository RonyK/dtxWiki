"""Protected access to immutable original Library source bytes.

The content-addressed blob key is deliberately not an authorization primitive.
Callers must identify the complete Workspace -> Library item -> version lineage;
this service validates that lineage and actor visibility before resolving the
opaque blob handle.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.orm import Session

from backend.library_media.domain import LibraryBlobRecord
from backend.library_media.ports import LibraryBlobRepositoryFactory
from backend.media.application import (
    DEFAULT_MAX_HTTP_RANGE_BYTES,
    ByteRangeNotSatisfiable,
    HttpByteRange,
    if_range_allows_range,
)
from backend.media.application import (
    parse_http_byte_range as _parse_http_byte_range,
)
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.media import SecurityBlockedBlobRow
from backend.models.workspace import WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import check_actor_can_access
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.actor_visible_read import actor_can_read_library_trust_level
from backend.services.library_blob_storage import (
    FilesystemLibraryBlobStore,
    LibraryBlobStorageError,
)
from backend.services.library_canonical import canonical_library_artifact_lookup
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadServiceError,
    RejectedByPolicyError,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9!#$&^_.+\-']+/[a-z0-9!#$&^_.+\-']+$",
    re.IGNORECASE,
)
_SAFE_FILENAME_BYTES = 180
_SAFE_ARTIFACT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "text/markdown",
        "text/plain",
    }
)
_ARTIFACT_SUFFIXES = {
    "application/json": ".json",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "text/markdown": ".md",
    "text/plain": ".txt",
}
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


class LibraryContentIntegrityError(ReadServiceError):
    """The persisted source lineage or immutable bytes failed validation."""

    error_code = "library_content_integrity"

    def __init__(self) -> None:
        super().__init__("Stored Library content failed an integrity check.")


class LibraryOriginalUnavailableError(ReadServiceError):
    """This version has no preserved original source to download.

    Distinct from a plain not-found (the item/version itself does not exist)
    so the frontend can tell a viewer "there is no original for this version"
    rather than a generic 404.
    """

    error_code = "library_original_unavailable"

    def __init__(self) -> None:
        super().__init__("Original Library source content is unavailable")


class LibraryByteRangeError(ReadServiceError):
    """A Range header cannot be served as one bounded byte range."""

    error_code = "library_range_not_satisfiable"

    def __init__(self, *, complete_length: int) -> None:
        super().__init__("The requested Library content byte range is invalid.")
        self.complete_length = complete_length


@dataclass(frozen=True, slots=True)
class VerifiedLibraryBlobHandle:
    """Path-free handle whose opener revalidates size and SHA-256 on each use."""

    byte_size: int
    sha256: str
    _opener: Callable[[], BinaryIO] = field(repr=False, compare=False)

    def open_binary(self) -> BinaryIO:
        return self._opener()


@dataclass(frozen=True, slots=True)
class LibraryOriginalContent:
    """Immutable, transport-neutral metadata for an authorized source read."""

    workspace_id: UUID
    library_item_id: UUID
    version_id: UUID
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    etag: str
    blob: VerifiedLibraryBlobHandle
    byte_range: HttpByteRange | None

    @property
    def content_length(self) -> int:
        return self.byte_range.length if self.byte_range is not None else self.byte_size

    @property
    def content_range(self) -> str | None:
        return self.byte_range.content_range if self.byte_range is not None else None


@dataclass(frozen=True, slots=True)
class LibraryArtifactContent:
    """Immutable, authorized content for one selected conversion artifact."""

    workspace_id: UUID
    library_item_id: UUID
    version_id: UUID
    artifact_id: UUID
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    etag: str
    blob: VerifiedLibraryBlobHandle
    byte_range: HttpByteRange | None

    @property
    def content_length(self) -> int:
        return self.byte_range.length if self.byte_range is not None else self.byte_size

    @property
    def content_range(self) -> str | None:
        return self.byte_range.content_range if self.byte_range is not None else None


class LibraryContentService:
    """Resolve an actor-authorized original source without exposing blob paths."""

    def __init__(
        self,
        *,
        store_for: Callable[[UUID], FilesystemLibraryBlobStore],
        blob_repository_factory: LibraryBlobRepositoryFactory,
        max_http_range_bytes: int = DEFAULT_MAX_HTTP_RANGE_BYTES,
    ) -> None:
        if max_http_range_bytes <= 0:
            raise ValueError("max_http_range_bytes must be positive")
        # D-5 (doc/design/workspace-database-separation-plan.md): every method
        # below already carries workspace_id (it authorizes against a
        # workspace-owned association before touching a blob), so the store
        # is resolved per call via BlobStoreProvider.store_for -- never
        # cached at construction -- the way session_for(workspace_id) already
        # is. This is what actually lets an overlay route a workspace's blobs
        # to a different disk/bucket; a store fixed here would ignore that.
        self._store_for = store_for
        self._blob_repository_factory = blob_repository_factory
        self._max_http_range_bytes = max_http_range_bytes

    def resolve_original(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        version_id: UUID,
        actor: ActorContext,
        range_header: str | None = None,
        if_range_header: str | None = None,
    ) -> LibraryOriginalContent:
        """Authorize and resolve one original source by its full association."""

        check_actor_can_access(actor, workspace_id, "read", session=session)

        item = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=library_item_id,
        )
        if item is None:
            raise ReadNotFoundError("Library source version not found")
        if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            raise ReadNotFoundError("Library source version not found")
        if not actor_can_read_library_trust_level(item.trust_level, actor):
            raise RejectedByPolicyError(
                "restricted source requires elevated access",
                error_code="restricted_library_denied",
            )

        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version is None or version.library_item_id != item.id:
            raise ReadNotFoundError("Library source version not found")
        blob = self._require_source_blob(
            session,
            workspace_id=workspace_id,
            version=version,
        )

        try:
            handle = self._store_for(workspace_id).open_handle(
                blob.storage_key,
                byte_size=blob.byte_size,
            )
            # Validate before returning metadata. The path-free wrapper invokes
            # the same checking opener again at stream time, closing the gap
            # between authorization and response iteration.
            with handle.open_binary():
                pass
        except LibraryBlobStorageError as exc:
            raise LibraryContentIntegrityError() from exc

        sha256 = blob.sha256.value
        etag = _opaque_library_etag(
            content_kind="original",
            actor=actor,
            membership_role=_membership_role(session, actor, workspace_id),
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            artifact_id=None,
            lifecycle_status=item.lifecycle_status,
            trust_level=item.trust_level,
            blob=blob.reference,
        )
        effective_range = (
            range_header if if_range_allows_range(if_range_header, etag) else None
        )
        normalized_range = parse_http_byte_range(
            effective_range,
            complete_length=blob.byte_size,
            max_range_bytes=self._max_http_range_bytes,
        )
        return LibraryOriginalContent(
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            filename=safe_download_filename(version.original_filename, sha256=sha256),
            media_type=safe_media_type(version.detected_media_type or blob.media_type),
            byte_size=blob.byte_size,
            sha256=sha256,
            etag=etag,
            blob=VerifiedLibraryBlobHandle(
                byte_size=blob.byte_size,
                sha256=sha256,
                _opener=handle.open_binary,
            ),
            byte_range=normalized_range,
        )

    def resolve_artifact(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        artifact_id: UUID,
        actor: ActorContext,
        range_header: str | None = None,
        if_range_header: str | None = None,
    ) -> LibraryArtifactContent:
        """Authorize and resolve one artifact -- conversion or the item's
        currently selected preview-generation artifact alike (P9.2 unifies
        both onto this one flat transport; see
        ``library_canonical.canonical_library_artifact_lookup``).
        """

        check_actor_can_access(actor, workspace_id, "read", session=session)
        lookup = canonical_library_artifact_lookup(
            session,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
        )
        if lookup is None:
            raise ReadNotFoundError("Library artifact content not found")
        item, version, artifact = lookup
        if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            raise ReadNotFoundError("Library artifact content not found")
        if not actor_can_read_library_trust_level(item.trust_level, actor):
            raise RejectedByPolicyError(
                "restricted source requires elevated access",
                error_code="restricted_library_denied",
            )

        blob = self._require_artifact_blob(
            session,
            workspace_id=workspace_id,
            artifact=artifact,
        )
        try:
            handle = self._store_for(workspace_id).open_handle(
                blob.storage_key,
                byte_size=blob.byte_size,
            )
            with handle.open_binary():
                pass
        except LibraryBlobStorageError as exc:
            raise LibraryContentIntegrityError() from exc

        sha256 = blob.sha256.value
        etag = _opaque_library_etag(
            content_kind="artifact",
            actor=actor,
            membership_role=_membership_role(session, actor, workspace_id),
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            artifact_id=artifact.id,
            lifecycle_status=item.lifecycle_status,
            trust_level=item.trust_level,
            blob=blob.reference,
        )
        effective_range = (
            range_header if if_range_allows_range(if_range_header, etag) else None
        )
        normalized_range = parse_http_byte_range(
            effective_range,
            complete_length=blob.byte_size,
            max_range_bytes=self._max_http_range_bytes,
        )
        media_type = safe_media_type(artifact.media_type or blob.media_type)
        if media_type not in _SAFE_ARTIFACT_MEDIA_TYPES:
            media_type = "application/octet-stream"
        return LibraryArtifactContent(
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            artifact_id=artifact.id,
            filename=_artifact_filename(artifact, media_type=media_type, sha256=sha256),
            media_type=media_type,
            byte_size=blob.byte_size,
            sha256=sha256,
            etag=etag,
            blob=VerifiedLibraryBlobHandle(
                byte_size=blob.byte_size,
                sha256=sha256,
                _opener=handle.open_binary,
            ),
            byte_range=normalized_range,
        )

    def _require_source_blob(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        version: LibraryItemVersion,
    ) -> LibraryBlobRecord:
        if (
            version.source_blob_workspace_id is None
            or version.source_blob_id is None
            or version.source_blob_epoch is None
        ):
            raise LibraryOriginalUnavailableError()
        if version.source_blob_workspace_id != workspace_id:
            raise LibraryContentIntegrityError()
        reference = BlobReference(
            workspace_id=version.source_blob_workspace_id,
            blob_id=version.source_blob_id,
            epoch=BlobEpoch(version.source_blob_epoch),
        )
        if _security_blocked(session, reference):
            raise ReadNotFoundError("Original Library source content is unavailable")
        blob = self._blob_repository_factory(session).get(reference)
        if blob is None:
            raise LibraryContentIntegrityError()

        sha256 = blob.sha256.value
        version_hash = str(version.content_hash).lower()
        if (
            not _SHA256_RE.fullmatch(sha256)
            or not _SHA256_RE.fullmatch(version_hash)
            or sha256 != version_hash
            or version.byte_size is None
            or version.byte_size < 0
            or version.byte_size != blob.byte_size
        ):
            raise LibraryContentIntegrityError()
        try:
            store = self._store_for(workspace_id)
            expected_keys = {
                store.storage_key_for_hash(
                    version.source_blob_workspace_id,
                    sha256,
                ),
                store.blob_key(
                    blob.reference,
                    blob.sha256,
                    publish_generation=1,
                ).value,
            }
        except LibraryBlobStorageError as exc:
            raise LibraryContentIntegrityError() from exc
        if blob.storage_key not in expected_keys:
            raise LibraryContentIntegrityError()
        return blob

    def _require_artifact_blob(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        artifact: LibraryArtifact,
    ) -> LibraryBlobRecord:
        if (
            artifact.blob_workspace_id is None
            or artifact.blob_id is None
            or artifact.blob_epoch is None
            or artifact.content_sha256 is None
            or artifact.byte_size is None
        ):
            raise ReadNotFoundError("Library artifact content is unavailable")
        if artifact.blob_workspace_id != workspace_id:
            raise LibraryContentIntegrityError()
        reference = BlobReference(
            workspace_id=artifact.blob_workspace_id,
            blob_id=artifact.blob_id,
            epoch=BlobEpoch(artifact.blob_epoch),
        )
        if _security_blocked(session, reference):
            raise ReadNotFoundError("Library artifact content is unavailable")
        blob = self._blob_repository_factory(session).get(reference)
        if blob is None:
            raise LibraryContentIntegrityError()
        sha256 = blob.sha256.value
        artifact_hash = artifact.content_sha256.lower()
        if (
            not _SHA256_RE.fullmatch(sha256)
            or not _SHA256_RE.fullmatch(artifact_hash)
            or sha256 != artifact_hash
            or artifact.byte_size < 0
            or artifact.byte_size != blob.byte_size
            or (
                artifact.media_type is not None
                and safe_media_type(artifact.media_type)
                != safe_media_type(blob.media_type)
            )
        ):
            raise LibraryContentIntegrityError()
        try:
            expected_key = (
                self._store_for(workspace_id)
                .blob_key(
                    blob.reference,
                    blob.sha256,
                    publish_generation=1,
                )
                .value
            )
        except LibraryBlobStorageError as exc:
            raise LibraryContentIntegrityError() from exc
        if blob.storage_key != expected_key:
            raise LibraryContentIntegrityError()
        return blob


def _membership_role(
    session: Session,
    actor: ActorContext,
    workspace_id: UUID,
) -> str | None:
    if actor.user_id is None:
        return None
    membership = session.get(WorkspaceMembership, (actor.user_id, workspace_id))
    return membership.role if membership is not None else None


def _opaque_library_etag(
    *,
    content_kind: str,
    actor: ActorContext,
    membership_role: str | None,
    workspace_id: UUID,
    library_item_id: UUID,
    version_id: UUID,
    artifact_id: UUID | None,
    lifecycle_status: str,
    trust_level: str,
    blob: BlobReference,
) -> str:
    """Bind a non-content-derived validator to authorization and association facts."""

    facts = {
        "schema": "library-content-etag-v1",
        "content_kind": content_kind,
        "authorization": {
            "actor_kind": actor.actor_kind,
            "actor_id": actor.actor_id,
            "user_id": str(actor.user_id) if actor.user_id is not None else None,
            "workspace_id": (
                str(actor.workspace_id) if actor.workspace_id is not None else None
            ),
            "workspace_role": actor.workspace_role,
            "effective_role": actor.role,
            "membership_role": membership_role,
            "scopes": sorted(actor.scopes),
            "token_kind": actor.token_kind,
            "token_id": str(actor.token_id) if actor.token_id is not None else None,
            "client_id": actor.client_id,
            "token_workspace_ids": sorted(
                str(candidate) for candidate in actor.token_workspace_ids
            ),
            "provenance": actor.provenance,
            "resource_lifecycle": lifecycle_status,
            "resource_trust": trust_level,
        },
        "association": {
            "workspace_id": str(workspace_id),
            "library_item_id": str(library_item_id),
            "version_id": str(version_id),
            "artifact_id": str(artifact_id) if artifact_id is not None else None,
            "blob_id": str(blob.blob_id),
            "blob_epoch": blob.epoch.value,
        },
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f'"lib-v1-{hashlib.sha256(encoded).hexdigest()[:32]}"'


def _security_blocked(session: Session, reference: BlobReference) -> bool:
    return (
        session.get(
            SecurityBlockedBlobRow,
            {
                "workspace_id": reference.workspace_id,
                "blob_id": reference.blob_id,
                "blob_epoch": reference.epoch.value,
            },
        )
        is not None
    )


def parse_http_byte_range(
    header_value: str | None,
    *,
    complete_length: int,
    max_range_bytes: int = DEFAULT_MAX_HTTP_RANGE_BYTES,
) -> HttpByteRange | None:
    """Compatibility wrapper around the Shared Media range primitive."""

    try:
        return _parse_http_byte_range(
            header_value,
            complete_length=complete_length,
            max_range_bytes=max_range_bytes,
        )
    except ByteRangeNotSatisfiable as exc:
        raise LibraryByteRangeError(complete_length=exc.complete_length) from exc


def safe_download_filename(value: str | None, *, sha256: str) -> str:
    """Return a bounded header-safe leaf name, falling back on source identity."""

    normalized = unicodedata.normalize("NFC", value or "")
    leaf = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        character
        if character.isalnum() or character in {" ", ".", "-", "_", "(", ")"}
        else "_"
        for character in leaf
    ).strip(" .")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if (
        not cleaned
        or cleaned in {".", ".."}
        or cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS
    ):
        cleaned = f"library-{sha256[:12]}.bin"
    while len(cleaned.encode("utf-8")) > _SAFE_FILENAME_BYTES:
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip(" .")
    return cleaned or f"library-{sha256[:12]}.bin"


def safe_media_type(value: str | None) -> str:
    """Normalize a server-detected media type without reflecting parameters."""

    normalized = (value or "").split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE_RE.fullmatch(normalized):
        return "application/octet-stream"
    return normalized


def _artifact_filename(
    artifact: LibraryArtifact,
    *,
    media_type: str,
    sha256: str,
) -> str:
    # P9.2: this now also names preview-generation artifacts, which carry
    # their filename directly on ``preview_detail`` instead of nested in a
    # conversion ``location_model``.
    candidate: str | None = None
    if artifact.stage == "preview_generation" and artifact.preview_detail is not None:
        candidate = artifact.preview_detail.filename
    elif artifact.conversion_detail is not None:
        location_model = artifact.conversion_detail.location_model
        candidate = (
            location_model.get("filename")
            if isinstance(location_model, dict)
            else None
        )
    if not isinstance(candidate, str) or not candidate.strip():
        role = artifact.role or artifact.kind or "artifact"
        suffix = _ARTIFACT_SUFFIXES.get(media_type, ".bin")
        candidate = f"{role}-{str(artifact.id)[:12]}{suffix}"
    return safe_download_filename(candidate, sha256=sha256)


__all__ = [
    "DEFAULT_MAX_HTTP_RANGE_BYTES",
    "HttpByteRange",
    "LibraryArtifactContent",
    "LibraryByteRangeError",
    "LibraryContentIntegrityError",
    "LibraryContentService",
    "LibraryOriginalContent",
    "VerifiedLibraryBlobHandle",
    "parse_http_byte_range",
    "safe_download_filename",
    "safe_media_type",
]
