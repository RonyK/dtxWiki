"""Immutable Workspace-physical quota facts owned by the media kernel."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


def _uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


class PhysicalQuotaKind(StrEnum):
    STAGING = "staging"
    PENDING = "pending"
    AVAILABLE = "available"
    BLOCKED = "blocked"
    TRASH = "trash"
    DERIVED = "derived"
    STAGING_ORPHAN = "staging_orphan"
    QUARANTINE = "quarantine"


def _bytes(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class PhysicalQuotaFacts:
    workspace_id: UUID
    kind: PhysicalQuotaKind
    reserved_bytes: int
    deletion_confirmed: bool = False

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "workspace_id")
        if not isinstance(self.kind, PhysicalQuotaKind):
            raise ValueError("kind must be a PhysicalQuotaKind")
        _bytes(self.reserved_bytes, "reserved_bytes")
        if not isinstance(self.deletion_confirmed, bool):
            raise ValueError("deletion_confirmed must be a boolean")

    @property
    def charge_bytes(self) -> int:
        return 0 if self.deletion_confirmed else self.reserved_bytes
