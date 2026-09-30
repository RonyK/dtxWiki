"""Context-owned logical asset quota seam."""

from typing import Protocol
from uuid import UUID


class ContextLogicalQuotaExceededError(ValueError):
    error_code = "media_quota_exceeded"

    def __init__(self, *, limit_bytes: int) -> None:
        super().__init__(self.error_code)
        self.limit_bytes = limit_bytes


class ContextLogicalQuota(Protocol):
    def lock_workspace(self, workspace_id: UUID) -> None: ...

    def reserve(
        self, workspace_id: UUID, *, asset_id: UUID, byte_size: int
    ) -> None: ...


class ContextLogicalQuotaFactory(Protocol):
    def __call__(self, transaction: object) -> ContextLogicalQuota: ...


__all__ = [
    "ContextLogicalQuota",
    "ContextLogicalQuotaExceededError",
    "ContextLogicalQuotaFactory",
]
