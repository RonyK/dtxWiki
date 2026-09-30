"""Idempotently bind one Library payload to a shared-media incarnation."""

from backend.library_media.domain import LibraryBlobCandidate, LibraryBlobRecord
from backend.library_media.ports import LibraryBlobRepository
from backend.media.domain import WorkspaceBlobIdentity


class RegisterLibraryBlob:
    def execute(
        self,
        repository: LibraryBlobRepository,
        candidate: LibraryBlobCandidate,
    ) -> LibraryBlobRecord:
        identity = WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256)
        existing = repository.find(identity)
        if existing is not None:
            if (
                existing.reference.workspace_id != candidate.workspace_id
                or existing.sha256 != candidate.sha256
                or existing.byte_size != candidate.byte_size
            ):
                raise ValueError("Library blob identity conflict")
            # Storage keys are incarnation facts, not part of the
            # workspace-scoped digest identity.  A Context upload or a prior
            # conversion may already own the canonical physical object.
            return existing
        created = repository.add_available(candidate)
        if (
            created.reference.workspace_id != candidate.workspace_id
            or created.sha256 != candidate.sha256
            or created.byte_size != candidate.byte_size
            or created.storage_key != candidate.storage_key
        ):
            raise RuntimeError("Library blob repository changed immutable facts")
        return created
