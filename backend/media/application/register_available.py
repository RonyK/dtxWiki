"""Idempotently register validated bytes as one available incarnation."""

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    WorkspaceBlobIdentity,
)
from backend.media.ports.available import AvailableBlobRepository


class RegisterAvailableBlob:
    def execute(
        self,
        repository: AvailableBlobRepository,
        candidate: AvailableBlobCandidate,
    ) -> AvailableBlobRecord:
        identity = WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256)
        existing = repository.find(identity)
        if existing is not None:
            if (
                existing.reference.workspace_id != candidate.workspace_id
                or existing.sha256 != candidate.sha256
                or existing.byte_size != candidate.byte_size
                or existing.storage_key != candidate.storage_key
                or existing.canonical_media_type != candidate.canonical_media_type
            ):
                raise ValueError("available blob identity conflict")
            return existing
        created = repository.add_available(candidate)
        if (
            created.reference.workspace_id != candidate.workspace_id
            or created.sha256 != candidate.sha256
            or created.byte_size != candidate.byte_size
            or created.storage_key != candidate.storage_key
            or created.canonical_media_type != candidate.canonical_media_type
        ):
            raise RuntimeError("available blob repository changed immutable facts")
        return created
