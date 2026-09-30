"""Persistence seam for Library associations to shared blobs."""

from typing import Protocol

from backend.library_media.domain import LibraryBlobCandidate, LibraryBlobRecord
from backend.media.domain import BlobReference, WorkspaceBlobIdentity


class LibraryBlobRepository(Protocol):
    def find(self, identity: WorkspaceBlobIdentity) -> LibraryBlobRecord | None: ...

    def get(self, reference: BlobReference) -> LibraryBlobRecord | None: ...

    def add_available(self, candidate: LibraryBlobCandidate) -> LibraryBlobRecord: ...


class LibraryBlobRepositoryFactory(Protocol):
    def __call__(self, transaction: object) -> LibraryBlobRepository: ...
