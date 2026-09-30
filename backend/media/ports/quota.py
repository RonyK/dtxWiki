"""Workspace-physical quota capabilities owned by the media kernel."""

from typing import Protocol
from uuid import UUID

from backend.media.ports.contracts import (
    DeletionReceipt,
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    StorageKey,
)


class PhysicalQuota(Protocol):
    def reserve(
        self,
        workspace_id: UUID,
        *,
        charge_id: UUID,
        target: PhysicalChargeTarget,
        storage_key: StorageKey,
        bytes_: int,
    ) -> None: ...

    def release_after_deletion(
        self,
        workspace_id: UUID,
        *,
        charge_id: UUID,
        deletion: PhysicalDeleteResult,
    ) -> DeletionReceipt:
        """Atomically persist deletion evidence and release its exact charge."""
        ...

    def charged_bytes(self, workspace_id: UUID) -> int: ...
