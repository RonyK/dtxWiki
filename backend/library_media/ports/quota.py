"""Library-owned logical quota seam."""

from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import UUID

LibraryLogicalQuotaKind = Literal["source", "derived"]


class LibraryLogicalQuotaExceededError(ValueError):
    error_code = "media_quota_exceeded"

    def __init__(self, *, limit_bytes: int) -> None:
        super().__init__(self.error_code)
        self.limit_bytes = limit_bytes


class LibraryLogicalQuota(Protocol):
    def ensure_capacity(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        additional_bytes: int,
    ) -> None: ...

    def reserve(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        associations: Mapping[UUID, int],
    ) -> None: ...

    def release(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        association_ids: set[UUID],
    ) -> None: ...


class LibraryLogicalQuotaFactory(Protocol):
    def __call__(self, transaction: object) -> LibraryLogicalQuota: ...


__all__ = [
    "LibraryLogicalQuota",
    "LibraryLogicalQuotaExceededError",
    "LibraryLogicalQuotaFactory",
    "LibraryLogicalQuotaKind",
]
