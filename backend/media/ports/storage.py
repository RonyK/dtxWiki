"""Narrow byte-storage capabilities; these ports do not authorize content."""

from collections.abc import Iterable
from typing import Protocol

from backend.media.domain import BlobReference, Sha256Digest
from backend.media.ports.contracts import (
    GenerationFence,
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    PublishReceipt,
    StagedBlob,
    StorageKey,
)


class BlobStorageLayout(Protocol):
    """Create opaque keys without exposing filesystem paths to use cases."""

    def staging_key(self, target: PhysicalChargeTarget) -> StorageKey: ...

    def blob_key(
        self,
        blob: BlobReference,
        sha256: Sha256Digest,
        *,
        publish_generation: int,
    ) -> StorageKey: ...


class ReadHandle(Protocol):
    @property
    def byte_size(self) -> int: ...

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class RangeReadHandle(ReadHandle, Protocol):
    def read_range(self, start: int, end_exclusive: int) -> bytes: ...


class BlobStager(Protocol):
    def stage(
        self,
        chunks: Iterable[bytes],
        *,
        target: PhysicalChargeTarget,
        max_bytes: int,
    ) -> StagedBlob: ...


class BlobPublisher(Protocol):
    def publish_no_overwrite(
        self, staged: StagedBlob, *, target: StorageKey, fence: GenerationFence
    ) -> PublishReceipt: ...


class BlobReader(Protocol):
    def open(self, key: StorageKey) -> ReadHandle: ...


class RangeReadableBlob(Protocol):
    def open_range(self, key: StorageKey) -> RangeReadHandle: ...


class PhysicalObjectDeleter(Protocol):
    """Delete a non-incarnation object without owning quota evidence.

    Blob incarnations deliberately use :class:`GcTrashStore` so callers cannot
    bypass retention and the generation-fenced trash lifecycle.  The store
    reports only whether the key was deleted or already absent; the application
    reconciles that observation with DB-authoritative charge facts.
    """

    def delete(
        self,
        key: StorageKey,
        *,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult: ...


class SecurityQuarantineStore(Protocol):
    def quarantine(self, key: StorageKey, *, fence: GenerationFence) -> None: ...

    def restore(self, key: StorageKey, *, fence: GenerationFence) -> None: ...


class GcTrashStore(Protocol):
    def trash(self, key: StorageKey, *, fence: GenerationFence) -> None: ...

    def restore(self, key: StorageKey, *, fence: GenerationFence) -> None: ...

    def purge(
        self,
        key: StorageKey,
        *,
        fence: GenerationFence,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult: ...
