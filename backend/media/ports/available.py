"""Persistence seam for registering already-validated immutable bytes."""

from typing import Protocol

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    WorkspaceBlobIdentity,
)


class AvailableBlobRepository(Protocol):
    def find(self, identity: WorkspaceBlobIdentity) -> AvailableBlobRecord | None: ...

    def add_available(
        self, candidate: AvailableBlobCandidate
    ) -> AvailableBlobRecord: ...
