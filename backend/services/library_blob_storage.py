"""Filesystem adapter for immutable, content-addressed Library blobs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ntpath
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

from backend.app.data_dir import data_dir
from backend.media.domain import BlobEpoch, BlobReference, Sha256Digest
from backend.media.ports.contracts import (
    GenerationFence,
    GenerationFenceError,
    IdempotencyConflictError,
    NoOverwriteError,
    PhysicalChargeTarget,
    PhysicalDeleteDisposition,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    PublishDisposition,
    PublishReceipt,
    StagedBlob,
    StorageKey,
)

LIBRARY_BLOB_ROOT_ENV = "DTXWIKI_LIBRARY_BLOB_ROOT"
_CHUNK_SIZE = 1024 * 1024


class LibraryBlobStorageError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class LibraryBlobTooLargeError(LibraryBlobStorageError):
    def __init__(self, *, limit_bytes: int) -> None:
        super().__init__(
            "library_upload_too_large",
            f"Library upload exceeds the {limit_bytes}-byte limit.",
        )
        self.limit_bytes = limit_bytes


@dataclass(frozen=True, slots=True)
class StoredLibraryBlob:
    workspace_id: UUID
    sha256: str
    byte_size: int
    storage_key: str
    created: bool


class StagedLibraryBlob:
    """One bounded staging file that is deleted unless explicitly committed."""

    def __init__(
        self,
        *,
        store: FilesystemLibraryBlobStore,
        path: Path,
        workspace_id: UUID,
        sha256: str,
        byte_size: int,
    ) -> None:
        self.store = store
        self.path = path
        self.workspace_id = workspace_id
        self.sha256 = sha256
        self.byte_size = byte_size
        self._committed = False

    def commit(self) -> StoredLibraryBlob:
        if self._committed:
            storage_key = self.store.storage_key_for_hash(
                self.workspace_id,
                self.sha256,
            )
            return StoredLibraryBlob(
                workspace_id=self.workspace_id,
                sha256=self.sha256,
                byte_size=self.byte_size,
                storage_key=storage_key,
                created=False,
            )
        _verify_file(
            self.path,
            expected_sha256=self.sha256,
            expected_size=self.byte_size,
            error_code="library_blob_staging_integrity",
        )
        storage_key = self.store.storage_key_for_hash(
            self.workspace_id,
            self.sha256,
        )
        destination = self.store.path_for_key(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating the fan-out directories. This rejects a
        # symlink/junction inserted between the first check and mkdir. Portable
        # Python cannot completely close the remaining directory-swap race.
        destination = self.store.path_for_key(storage_key)
        created = False
        try:
            # A hard-link is an atomic create-if-absent publication on local
            # filesystems. Unlike os.replace(), it cannot silently overwrite a
            # same-key winner from another process.
            os.link(self.path, destination)
            created = True
        except FileExistsError:
            _verify_file(
                destination,
                expected_sha256=self.sha256,
                expected_size=self.byte_size,
                error_code="library_blob_collision",
            )
        except OSError as exc:
            raise LibraryBlobStorageError(
                "library_blob_publish_unavailable",
                "Stored Library content could not be published safely.",
            ) from exc
        else:
            try:
                _verify_file(
                    destination,
                    expected_sha256=self.sha256,
                    expected_size=self.byte_size,
                    error_code="library_blob_integrity",
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise

        self.path.unlink(missing_ok=True)
        self._committed = True
        return StoredLibraryBlob(
            workspace_id=self.workspace_id,
            sha256=self.sha256,
            byte_size=self.byte_size,
            storage_key=storage_key,
            created=created,
        )

    def cleanup(self) -> None:
        if not self._committed:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> StagedLibraryBlob:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()


@dataclass(frozen=True, slots=True)
class FilesystemBlobHandle:
    path: Path
    byte_size: int
    expected_sha256: str

    def open_binary(self) -> BinaryIO:
        try:
            stream = _open_regular_file(self.path)
            actual_size, actual_sha256 = _hash_stream(stream)
        except (OSError, ValueError) as exc:
            raise LibraryBlobStorageError(
                "library_blob_integrity", "Stored Library content failed validation."
            ) from exc
        if actual_size != self.byte_size or actual_sha256 != self.expected_sha256:
            stream.close()
            raise LibraryBlobStorageError(
                "library_blob_integrity", "Stored Library content failed validation."
            )
        stream.seek(0)
        return stream

    def copy_to(self, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".copy", dir=destination.parent
        )
        temporary_path = Path(raw_path)
        copied_size = 0
        copied_digest = hashlib.sha256()
        try:
            with self.open_binary() as source, os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(_CHUNK_SIZE):
                    written = target.write(chunk)
                    if written != len(chunk):
                        raise OSError("short write while copying Library content")
                    copied_size += written
                    copied_digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if (
                copied_size != self.byte_size
                or copied_digest.hexdigest() != self.expected_sha256
            ):
                raise LibraryBlobStorageError(
                    "library_blob_integrity",
                    "Stored Library content changed while it was copied.",
                )
            _verify_file(
                temporary_path,
                expected_sha256=self.expected_sha256,
                expected_size=self.byte_size,
                error_code="library_blob_copy_integrity",
            )
            os.replace(temporary_path, destination)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise
        return copied_size


class FilesystemMediaReadHandle:
    """ReadHandle implementation returned through the Shared Media port."""

    def __init__(self, stream: BinaryIO, *, byte_size: int) -> None:
        self._stream = stream
        self._byte_size = byte_size

    @property
    def byte_size(self) -> int:
        return self._byte_size

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


class FilesystemLibraryBlobStore:
    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get(LIBRARY_BLOB_ROOT_ENV)
        resolved = root or (Path(configured) if configured else data_dir() / "library")
        self.root = resolved.expanduser().resolve()
        self.blob_root = self.root / "blobs"
        self.staging_root = self.root / "staging"
        self.trash_root = self.root / "trash"
        _assert_no_link_or_reparse(self.root, self.blob_root)
        _assert_no_link_or_reparse(self.root, self.staging_root)
        _assert_no_link_or_reparse(self.root, self.trash_root)

    async def stage_upload(
        self,
        upload: Any,
        *,
        workspace_id: UUID,
        max_bytes: int,
    ) -> StagedLibraryBlob:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        # UploadFile.read() is asynchronous, while local filesystem creation,
        # writes, flush and fsync are blocking operations. Keep those operations
        # on worker threads instead of stalling the application event loop.
        path, stream = await asyncio.to_thread(self._new_staging_file, workspace_id)
        digest = hashlib.sha256()
        count = 0
        try:
            while True:
                chunk = await upload.read(_CHUNK_SIZE)
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise LibraryBlobTooLargeError(limit_bytes=max_bytes)
                await asyncio.to_thread(_write_staging_chunk, stream, digest, chunk)
            await asyncio.to_thread(_flush_fsync_close, stream)
        except BaseException:
            await asyncio.to_thread(_close_and_unlink, stream, path)
            raise
        return StagedLibraryBlob(
            store=self,
            path=path,
            workspace_id=workspace_id,
            sha256=digest.hexdigest(),
            byte_size=count,
        )

    def stage_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        workspace_id: UUID,
        max_bytes: int,
    ) -> StagedLibraryBlob:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        path, stream = self._new_staging_file(workspace_id)
        digest = hashlib.sha256()
        count = 0
        try:
            with stream:
                for chunk in chunks:
                    count += len(chunk)
                    if count > max_bytes:
                        raise LibraryBlobTooLargeError(limit_bytes=max_bytes)
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return StagedLibraryBlob(
            store=self,
            path=path,
            workspace_id=workspace_id,
            sha256=digest.hexdigest(),
            byte_size=count,
        )

    def put_bytes(
        self,
        content: bytes,
        *,
        workspace_id: UUID,
        max_bytes: int,
    ) -> StoredLibraryBlob:
        with self.stage_chunks(
            (content,), workspace_id=workspace_id, max_bytes=max_bytes
        ) as staged:
            return staged.commit()

    def staging_key(self, target: PhysicalChargeTarget) -> StorageKey:
        if target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("staging key requires a staging lease target")
        return StorageKey(
            f"stage/{target.workspace_id.hex}/{target.object_id.hex}/"
            f"{target.generation}"
        )

    def blob_key(
        self,
        blob: BlobReference,
        sha256: Sha256Digest,
        *,
        publish_generation: int,
    ) -> StorageKey:
        if (
            isinstance(publish_generation, bool)
            or not isinstance(publish_generation, int)
            or publish_generation <= 0
        ):
            raise ValueError("publish_generation must be positive")
        workspace_token = _workspace_token(blob.workspace_id)
        return StorageKey(
            f"b/{workspace_token}/{blob.blob_id.hex}/{blob.epoch.value}/"
            f"{publish_generation}/"
            f"{sha256.value[:2]}/{sha256.value}"
        )

    def stage(
        self,
        chunks: Iterable[bytes],
        *,
        target: PhysicalChargeTarget,
        max_bytes: int,
    ) -> StagedBlob:
        key = self.staging_key(target)
        staged = self.stage_chunks(
            chunks,
            workspace_id=target.workspace_id,
            max_bytes=max_bytes,
        )
        destination = self._path_for_staging_key(key)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            try:
                os.link(staged.path, destination)
            except FileExistsError:
                try:
                    _verify_file(
                        destination,
                        expected_sha256=staged.sha256,
                        expected_size=staged.byte_size,
                        error_code="media_staging_idempotency_conflict",
                    )
                except LibraryBlobStorageError as exc:
                    raise IdempotencyConflictError("media_stage") from exc
            _verify_file(
                destination,
                expected_sha256=staged.sha256,
                expected_size=staged.byte_size,
                error_code="media_staging_integrity",
            )
        finally:
            staged.cleanup()
        return StagedBlob(
            target=target,
            storage_key=key,
            byte_size=staged.byte_size,
            sha256=Sha256Digest(staged.sha256),
        )

    def publish_no_overwrite(
        self,
        staged: StagedBlob,
        *,
        target: StorageKey,
        fence: GenerationFence,
    ) -> PublishReceipt:
        if staged.target.workspace_id != fence.blob.workspace_id:
            raise ValueError("publish workspace mismatch")
        source = self._path_for_staging_key(staged.storage_key)
        destination = self.path_for_key(target.value)
        expected_target = self.blob_key(
            fence.blob,
            staged.sha256,
            publish_generation=fence.generation,
        )
        if target != expected_target:
            if target.value.rsplit("/", 1)[-1] != staged.sha256.value:
                raise NoOverwriteError(target.value)
            raise IdempotencyConflictError("media_publish_fence")
        destination.parent.mkdir(parents=True, exist_ok=True)
        disposition = PublishDisposition.PUBLISHED
        if source.exists():
            _verify_file(
                source,
                expected_sha256=staged.sha256.value,
                expected_size=staged.byte_size,
                error_code="media_staging_integrity",
            )
            try:
                os.link(source, destination)
            except FileExistsError:
                disposition = PublishDisposition.IDEMPOTENT_REPLAY
            except OSError as exc:
                raise LibraryBlobStorageError(
                    "media_publish_unavailable",
                    "Stored media content could not be published.",
                ) from exc
        elif destination.exists():
            disposition = PublishDisposition.IDEMPOTENT_REPLAY
        else:
            raise LibraryBlobStorageError(
                "media_staging_missing",
                "Staged media content is unavailable.",
            )
        try:
            _verify_file(
                destination,
                expected_sha256=staged.sha256.value,
                expected_size=staged.byte_size,
                error_code="media_publish_collision",
            )
        except LibraryBlobStorageError as exc:
            if disposition is PublishDisposition.IDEMPOTENT_REPLAY:
                raise NoOverwriteError(target.value) from exc
            destination.unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)
        return PublishReceipt(
            disposition=disposition,
            fence=fence,
            storage_key=target,
            byte_size=staged.byte_size,
            sha256=staged.sha256,
        )

    def delete(
        self,
        key: StorageKey,
        *,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult:
        if target.kind is PhysicalObjectKind.BLOB_INCARNATION:
            raise ValueError("blob incarnation deletion requires the GC trash store")
        if target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("physical object kind is not supported")
        if key != self.staging_key(target):
            raise ValueError("physical object key mismatch")
        path = self._path_for_staging_key(key)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return PhysicalDeleteResult(
            disposition=(
                PhysicalDeleteDisposition.DELETED
                if existed
                else PhysicalDeleteDisposition.ALREADY_MISSING
            ),
            target=target,
            storage_key=key,
        )

    def trash(self, key: StorageKey, *, fence: GenerationFence) -> None:
        source, trashed = self._gc_paths(key, fence=fence)
        trashed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _assert_no_link_or_reparse(self.trash_root, trashed)
        try:
            if source.exists():
                source_stat = source.lstat()
                if _is_link_or_reparse(source_stat) or not stat.S_ISREG(
                    source_stat.st_mode
                ):
                    raise ValueError("blob path is not a regular file")
                try:
                    os.link(source, trashed)
                except FileExistsError:
                    pass
                _verify_same_gc_object(source, trashed, key=key)
                source.unlink()
            elif trashed.exists():
                _verify_gc_object(trashed, key=key)
            # If neither exists, a prior purge may have completed before its DB
            # transaction committed.  The application authority will replay
            # purge and persist an already-missing deletion receipt.
        except (OSError, ValueError, LibraryBlobStorageError) as exc:
            raise LibraryBlobStorageError(
                "media_gc_trash_failed",
                "Stored media content could not be moved to GC trash.",
            ) from exc

    def restore(self, key: StorageKey, *, fence: GenerationFence) -> None:
        destination, trashed = self._gc_paths(key, fence=fence)
        try:
            if destination.exists():
                _verify_gc_object(destination, key=key)
                if trashed.exists():
                    _verify_same_gc_object(destination, trashed, key=key)
                    trashed.unlink()
                return
            if not trashed.exists():
                raise FileNotFoundError(trashed)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(trashed, destination)
            _verify_same_gc_object(destination, trashed, key=key)
            trashed.unlink()
        except (OSError, ValueError, LibraryBlobStorageError) as exc:
            raise LibraryBlobStorageError(
                "media_gc_restore_failed",
                "Stored media content could not be restored from GC trash.",
            ) from exc

    def purge(
        self,
        key: StorageKey,
        *,
        fence: GenerationFence,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult:
        _live, trashed = self._gc_paths(key, fence=fence, target=target)
        try:
            existed = trashed.exists()
            if existed:
                _verify_gc_object(trashed, key=key)
                trashed.unlink()
        except (OSError, ValueError, LibraryBlobStorageError) as exc:
            raise LibraryBlobStorageError(
                "media_gc_purge_failed",
                "Stored media content could not be purged.",
            ) from exc
        return PhysicalDeleteResult(
            disposition=(
                PhysicalDeleteDisposition.DELETED
                if existed
                else PhysicalDeleteDisposition.ALREADY_MISSING
            ),
            target=target,
            storage_key=key,
        )

    def purge_workspace(self, workspace_id: UUID) -> list[str]:
        """Delete every stored byte belonging to one workspace.

        Called after a workspace purge commits, never inside its transaction:
        the database is the record of what exists, so files must not disappear
        while a rollback could still put the rows back. The four roots are all
        partitioned by workspace -- blobs under both the legacy ``w/<token>``
        and media ``b/<token>`` prefixes, staging and GC trash by raw hex -- so
        a purge is four directory removals rather than a walk over every key.

        Returns the roots that existed and were removed. A failure here leaves
        orphaned bytes with no rows pointing at them, which is wasted disk and
        not a correctness problem, so the caller logs rather than fails: the
        alternative is refusing a deletion the user already confirmed.
        """

        token = _workspace_token(workspace_id)
        removed: list[str] = []
        candidates = (
            self.blob_root / "w" / token,
            self.blob_root / "b" / token,
            self.staging_root / "media" / workspace_id.hex,
            self.trash_root / workspace_id.hex,
        )
        for candidate in candidates:
            root = (
                self.blob_root
                if candidate.is_relative_to(self.blob_root)
                else self.staging_root
                if candidate.is_relative_to(self.staging_root)
                else self.trash_root
            )
            if not candidate.exists():
                continue
            # Same containment guard the read paths use: never follow a link
            # out of the store, and never delete outside the root we resolved.
            _assert_no_link_or_reparse(root, candidate)
            resolved = candidate.resolve()
            if not _path_is_within(root, resolved):
                raise LibraryBlobStorageError(
                    "media_workspace_purge_path_invalid",
                    "Stored media content could not be purged.",
                )
            shutil.rmtree(resolved)
            removed.append(str(resolved))
        return removed

    def open(self, key: StorageKey) -> FilesystemMediaReadHandle:
        path = self.path_for_key(key.value)
        try:
            byte_size = path.stat().st_size
            expected_sha256 = key.value.rsplit("/", 1)[-1]
            _verify_file(
                path,
                expected_sha256=expected_sha256,
                expected_size=byte_size,
                error_code="media_blob_integrity",
            )
            stream = _open_regular_file(path)
        except (OSError, ValueError) as exc:
            raise LibraryBlobStorageError(
                "media_blob_unavailable",
                "Stored media content is unavailable.",
            ) from exc
        return FilesystemMediaReadHandle(stream, byte_size=byte_size)

    def open_handle(self, storage_key: str, *, byte_size: int) -> FilesystemBlobHandle:
        path = self.path_for_key(storage_key)
        try:
            path_stat = path.lstat()
            if _is_link_or_reparse(path_stat) or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("blob path is not a regular file")
            actual_size = path_stat.st_size
        except FileNotFoundError as exc:
            raise LibraryBlobStorageError(
                "library_blob_missing", "Stored Library content is unavailable."
            ) from exc
        except (OSError, ValueError) as exc:
            raise LibraryBlobStorageError(
                "library_blob_integrity", "Stored Library content failed validation."
            ) from exc
        if actual_size != byte_size:
            raise LibraryBlobStorageError(
                "library_blob_integrity", "Stored Library content failed validation."
            )
        return FilesystemBlobHandle(
            path=path,
            byte_size=byte_size,
            expected_sha256=storage_key.rsplit("/", 1)[-1],
        )

    def storage_key_for_hash(self, workspace_id: UUID, sha256: str) -> str:
        if not isinstance(workspace_id, UUID):
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        normalized = sha256.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        workspace_token = _workspace_token(workspace_id)
        return f"w/{workspace_token}/{normalized[:2]}/{normalized}"

    def path_for_key(self, storage_key: str) -> Path:
        normalized_key = storage_key.replace("\\", "/")
        pieces = normalized_key.split("/")
        if pieces[0] == "w" and len(pieces) == 4:
            expected = self._validate_legacy_storage_key(pieces)
        elif pieces[0] == "b" and len(pieces) == 7:
            expected = self._validate_media_storage_key(pieces)
        else:
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        if normalized_key != expected:
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        candidate = self.blob_root / Path(expected)
        _assert_no_link_or_reparse(self.blob_root, candidate)
        path = candidate.resolve()
        if not _path_is_within(self.blob_root, path):
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        return path

    def _validate_legacy_storage_key(self, pieces: list[str]) -> str:
        try:
            workspace_bytes = base64.b64decode(
                pieces[1] + "==",
                altchars=b"-_",
                validate=True,
            )
            workspace_id = UUID(bytes=workspace_bytes)
        except (ValueError, TypeError) as exc:
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            ) from exc
        return self.storage_key_for_hash(workspace_id, pieces[-1])

    def _validate_media_storage_key(self, pieces: list[str]) -> str:
        try:
            workspace_bytes = base64.b64decode(
                pieces[1] + "==",
                altchars=b"-_",
                validate=True,
            )
            workspace_id = UUID(bytes=workspace_bytes)
            blob_id = UUID(hex=pieces[2])
            epoch = int(pieces[3])
            publish_generation = int(pieces[4])
            digest = Sha256Digest(pieces[6])
            blob = BlobReference(workspace_id, blob_id, BlobEpoch(epoch))
        except (ValueError, TypeError) as exc:
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            ) from exc
        expected = self.blob_key(
            blob,
            digest,
            publish_generation=publish_generation,
        ).value
        if pieces[5] != digest.value[:2]:
            raise LibraryBlobStorageError(
                "library_blob_key_invalid", "Invalid Library content key."
            )
        return expected

    def _path_for_staging_key(self, key: StorageKey) -> Path:
        pieces = key.value.split("/")
        try:
            if len(pieces) != 4 or pieces[0] != "stage":
                raise ValueError
            workspace_id = UUID(hex=pieces[1])
            staging_id = UUID(hex=pieces[2])
            generation = int(pieces[3])
            target = PhysicalChargeTarget(
                workspace_id=workspace_id,
                kind=PhysicalObjectKind.STAGING_LEASE,
                object_id=staging_id,
                generation=generation,
            )
            if key != self.staging_key(target):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise LibraryBlobStorageError(
                "media_staging_key_invalid", "Invalid media staging key."
            ) from exc
        candidate = (
            self.staging_root
            / "media"
            / workspace_id.hex
            / f"{staging_id.hex}.{generation}.stage"
        )
        _assert_no_link_or_reparse(self.staging_root, candidate)
        return candidate.resolve()

    def _gc_paths(
        self,
        key: StorageKey,
        *,
        fence: GenerationFence,
        target: PhysicalChargeTarget | None = None,
    ) -> tuple[Path, Path]:
        if not isinstance(key, StorageKey) or not isinstance(fence, GenerationFence):
            raise ValueError("GC storage identity is invalid")
        pieces = key.value.split("/")
        if len(pieces) != 7 or pieces[0] != "b":
            raise ValueError("GC requires a blob-incarnation key")
        # path_for_key performs the complete opaque-key and containment check.
        live = self.path_for_key(key.value)
        try:
            workspace_bytes = base64.b64decode(
                pieces[1] + "==", altchars=b"-_", validate=True
            )
            key_workspace_id = UUID(bytes=workspace_bytes)
            key_blob_id = UUID(hex=pieces[2])
            key_epoch = int(pieces[3])
            publish_generation = int(pieces[4])
        except (ValueError, TypeError) as exc:
            raise ValueError("GC blob key is invalid") from exc
        if (
            key_workspace_id != fence.blob.workspace_id
            or key_blob_id != fence.blob.blob_id
            or key_epoch != fence.blob.epoch.value
        ):
            raise GenerationFenceError("GC fence does not match the blob key")
        if target is not None and (
            target.kind is not PhysicalObjectKind.BLOB_INCARNATION
            or target.workspace_id != fence.blob.workspace_id
            or target.object_id != fence.blob.blob_id
            or target.blob != fence.blob
            or target.generation != publish_generation
        ):
            raise ValueError("GC charge target does not match the blob key")
        candidate = (
            self.trash_root
            / fence.blob.workspace_id.hex
            / fence.blob.blob_id.hex
            / str(fence.blob.epoch.value)
            / str(fence.generation)
            / f"{publish_generation}.{pieces[-1]}.trash"
        )
        _assert_no_link_or_reparse(self.trash_root, candidate)
        trashed = candidate.resolve()
        if not _path_is_within(self.trash_root, trashed):
            raise ValueError("GC trash path escaped its root")
        return live, trashed

    def _new_staging_file(self, workspace_id: UUID) -> tuple[Path, BinaryIO]:
        candidate = self.staging_root / str(workspace_id)
        _assert_no_link_or_reparse(self.staging_root, candidate)
        directory = candidate.resolve()
        if not _path_is_within(self.staging_root, directory):
            raise LibraryBlobStorageError(
                "library_staging_invalid", "Invalid Library staging scope."
            )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keep the lexical path rooted in ``staging_root`` for the second
        # reparse-point check. On Windows, ``resolve()`` may return the same
        # path in the extended ``\\?\`` namespace while ``staging_root`` uses
        # the drive-letter namespace.
        _assert_no_link_or_reparse(self.staging_root, candidate)
        descriptor, raw_path = tempfile.mkstemp(
            prefix="upload-", suffix=".stage", dir=directory
        )
        return Path(raw_path), os.fdopen(descriptor, "wb")


def _open_regular_file(path: Path) -> BinaryIO:
    before = path.lstat()
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Library blob is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(before, opened):
            raise ValueError("Library blob changed while it was opened")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _same_file_identity(before: os.stat_result, opened: os.stat_result) -> bool:
    if before.st_dev != opened.st_dev:
        return False
    # Some Windows filesystems report inode 0. In that case the content hash and
    # byte-count checks remain the authoritative identity check.
    return not before.st_ino or not opened.st_ino or before.st_ino == opened.st_ino


def _workspace_token(workspace_id: UUID) -> str:
    return base64.urlsafe_b64encode(workspace_id.bytes).decode("ascii").rstrip("=")


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _path_is_within(root: Path, candidate: Path) -> bool:
    r"""Return whether *candidate* is contained by *root*.

    ``Path.resolve()`` can expose an existing Windows path through the
    extended-length namespace (``\\?\C:\\...`` or ``\\?\UNC\\...``) even when
    the configured root uses the equivalent ordinary namespace. Comparing the
    two ``Path`` objects directly then produces a false negative. Normalize
    only those two documented namespace aliases before a component-aware
    ``commonpath`` comparison; device namespaces such as ``\\?\GLOBALROOT``
    remain distinct and fail closed.
    """

    if os.name == "nt":
        return _windows_path_is_within(root, candidate)
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _windows_path_is_within(root: str | Path, candidate: str | Path) -> bool:
    normalized_root = _normalize_windows_namespace(root)
    normalized_candidate = _normalize_windows_namespace(candidate)
    try:
        return (
            ntpath.commonpath((normalized_root, normalized_candidate))
            == normalized_root
        )
    except ValueError:
        # Different drives, or an absolute/relative mismatch, cannot establish
        # containment.
        return False


def _normalize_windows_namespace(path: str | Path) -> str:
    normalized = ntpath.normpath(os.fspath(path))
    folded = normalized.casefold()
    extended_unc_prefix = "\\\\?\\unc\\"
    extended_prefix = "\\\\?\\"
    if folded.startswith(extended_unc_prefix):
        normalized = "\\\\" + normalized[len(extended_unc_prefix) :]
    elif (
        normalized.startswith(extended_prefix)
        and len(normalized) >= len(extended_prefix) + 3
        and normalized[len(extended_prefix)].isalpha()
        and normalized[len(extended_prefix) + 1] == ":"
        and normalized[len(extended_prefix) + 2] in "\\/"
    ):
        normalized = normalized[len(extended_prefix) :]
    return ntpath.normcase(ntpath.normpath(normalized))


def _assert_no_link_or_reparse(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LibraryBlobStorageError(
            "library_blob_path_unsafe", "Unsafe Library storage path."
        ) from exc

    descendants = (
        root / Path(*relative.parts[:index])
        for index in range(1, len(relative.parts) + 1)
    )
    paths = (root, *descendants)
    for current in paths:
        try:
            path_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LibraryBlobStorageError(
                "library_blob_path_unsafe", "Unsafe Library storage path."
            ) from exc
        if _is_link_or_reparse(path_stat):
            raise LibraryBlobStorageError(
                "library_blob_path_unsafe", "Unsafe Library storage path."
            )


def _hash_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    while chunk := stream.read(_CHUNK_SIZE):
        byte_size += len(chunk)
        digest.update(chunk)
    return byte_size, digest.hexdigest()


def _verify_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    error_code: str,
) -> None:
    try:
        with _open_regular_file(path) as stream:
            actual_size, actual_sha256 = _hash_stream(stream)
    except (OSError, ValueError) as exc:
        raise LibraryBlobStorageError(
            error_code, "Stored Library content failed an integrity check."
        ) from exc
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise LibraryBlobStorageError(
            error_code, "Stored Library content failed an integrity check."
        )


def _verify_gc_object(path: Path, *, key: StorageKey) -> None:
    try:
        path_stat = path.lstat()
        if _is_link_or_reparse(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise ValueError("GC object is not a regular file")
    except (OSError, ValueError) as exc:
        raise LibraryBlobStorageError(
            "media_gc_integrity", "Stored media content failed an integrity check."
        ) from exc
    _verify_file(
        path,
        expected_sha256=key.value.rsplit("/", 1)[-1],
        expected_size=path_stat.st_size,
        error_code="media_gc_integrity",
    )


def _verify_same_gc_object(source: Path, target: Path, *, key: StorageKey) -> None:
    _verify_gc_object(source, key=key)
    _verify_gc_object(target, key=key)
    if source.stat().st_size != target.stat().st_size:
        raise LibraryBlobStorageError(
            "media_gc_integrity", "Stored media content failed an integrity check."
        )


def _write_staging_chunk(stream: BinaryIO, digest: Any, chunk: bytes) -> None:
    written = stream.write(chunk)
    if written != len(chunk):
        raise OSError("short write while staging Library content")
    digest.update(chunk)


def _flush_fsync_close(stream: BinaryIO) -> None:
    try:
        stream.flush()
        os.fsync(stream.fileno())
    finally:
        stream.close()


def _close_and_unlink(stream: BinaryIO, path: Path) -> None:
    try:
        stream.close()
    finally:
        path.unlink(missing_ok=True)


__all__ = [
    "LIBRARY_BLOB_ROOT_ENV",
    "FilesystemBlobHandle",
    "FilesystemMediaReadHandle",
    "FilesystemLibraryBlobStore",
    "LibraryBlobStorageError",
    "LibraryBlobTooLargeError",
    "StagedLibraryBlob",
    "StoredLibraryBlob",
]
