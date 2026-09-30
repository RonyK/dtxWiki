"""Library-owned provenance carried beside a shared blob reference."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from backend.media.domain import BlobReference, Sha256Digest


@dataclass(frozen=True, slots=True)
class LibraryBlobCandidate:
    workspace_id: UUID
    sha256: Sha256Digest
    byte_size: int
    storage_key: str
    media_type: str
    format_id: str
    detector_id: str
    metadata: Mapping[str, str | int | bool]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise ValueError("byte_size must be a non-negative integer")
        for name in ("storage_key", "media_type", "format_id", "detector_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 255:
                raise ValueError(f"{name} must be non-blank and bounded")
        if not isinstance(self.metadata, Mapping) or len(self.metadata) > 32:
            raise ValueError("metadata must be a bounded mapping")
        normalized: dict[str, str | int | bool] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key or len(key) > 80:
                raise ValueError("metadata keys must be non-blank and bounded")
            if isinstance(value, str) and len(value) > 512:
                raise ValueError("metadata string values must be bounded")
            if not isinstance(value, (str, int, bool)):
                raise ValueError("metadata values must be scalar")
            normalized[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class LibraryBlobRecord:
    reference: BlobReference
    sha256: Sha256Digest
    byte_size: int
    storage_key: str
    media_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, BlobReference):
            raise ValueError("reference must be a BlobReference")
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")
        if self.byte_size < 0:
            raise ValueError("byte_size must be non-negative")
