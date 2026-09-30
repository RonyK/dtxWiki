"""Transaction and persistence seams owned by the media application layer."""

from collections.abc import Callable
from typing import Protocol, Self
from uuid import UUID

from backend.media.domain import StoredBlob, WorkspaceBlobIdentity
from backend.media.ports.coordination import Outbox
from backend.media.ports.quota import PhysicalQuota


class MediaRepository(Protocol):
    def get_by_blob_id(
        self, workspace_id: UUID, blob_id: UUID
    ) -> StoredBlob | None: ...

    def find_by_identity(
        self, identity: WorkspaceBlobIdentity
    ) -> StoredBlob | None: ...

    def put(self, blob: StoredBlob) -> None: ...


class MediaUnitOfWork(Protocol):
    repository: MediaRepository
    physical_quota: PhysicalQuota
    outbox: Outbox

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class MediaUnitOfWorkFactory(Protocol):
    def __call__(self) -> MediaUnitOfWork: ...


TransactionHook = Callable[[], None]
