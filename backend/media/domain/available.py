"""Validated immutable bytes admitted as one available blob incarnation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from .blob import BlobReference, Sha256Digest


@dataclass(frozen=True, slots=True)
class AvailableBlobCandidate:
    workspace_id: UUID
    sha256: Sha256Digest
    byte_size: int
    storage_key: str
    canonical_media_type: str
    format_id: str
    detector_id: str
    detector_version: str
    registry_version: str
    validation_schema_version: str
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
        for name in (
            "storage_key",
            "canonical_media_type",
            "format_id",
            "detector_id",
            "detector_version",
            "registry_version",
            "validation_schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 255:
                raise ValueError(f"{name} must be non-blank and bounded")
        if not isinstance(self.metadata, Mapping) or len(self.metadata) > 32:
            raise ValueError("metadata must be a bounded mapping")
        normalized: dict[str, str | int | bool] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key or len(key) > 80:
                raise ValueError("metadata keys must be non-blank and bounded")
            if not isinstance(value, (str, int, bool)):
                raise ValueError("metadata values must be scalar")
            if isinstance(value, str) and len(value) > 512:
                raise ValueError("metadata string values must be bounded")
            normalized[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class AvailableBlobRecord:
    reference: BlobReference
    sha256: Sha256Digest
    byte_size: int
    storage_key: str
    canonical_media_type: str
    inspection_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.reference, BlobReference):
            raise ValueError("reference must be a BlobReference")
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")
        if self.byte_size < 0:
            raise ValueError("byte_size must be non-negative")
        if not self.storage_key or not self.canonical_media_type:
            raise ValueError("available blob facts must be non-blank")
        if not isinstance(self.inspection_id, UUID):
            raise ValueError("inspection_id must be a UUID")
