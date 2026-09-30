"""Blob identity, incarnation, inspection, and security state contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _require_bounded_text(value: object, name: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-blank and bounded")
    return value


@dataclass(frozen=True, order=True)
class BlobEpoch:
    value: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value <= 0
        ):
            raise ValueError("blob epoch must be a positive integer")

    def next(self) -> BlobEpoch:
        return BlobEpoch(self.value + 1)


@dataclass(frozen=True)
class Sha256Digest:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError("sha256 must be a string")
        normalized = self.value.lower()
        if len(normalized) != 64 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True)
class WorkspaceBlobIdentity:
    workspace_id: UUID
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")


@dataclass(frozen=True)
class BlobReference:
    """The only transferable identity for one immutable blob incarnation."""

    workspace_id: UUID
    blob_id: UUID
    epoch: BlobEpoch

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.blob_id, "blob_id")
        if not isinstance(self.epoch, BlobEpoch):
            raise ValueError("epoch must be a BlobEpoch")


class BlobState(StrEnum):
    PENDING_INSTALL = "pending_install"
    AVAILABLE = "available"
    GC_PENDING = "gc_pending"
    TRASHED = "trashed"
    MISSING = "missing"
    PURGED = "purged"


_BLOB_TRANSITIONS = {
    BlobState.PENDING_INSTALL: frozenset({BlobState.AVAILABLE, BlobState.MISSING}),
    BlobState.AVAILABLE: frozenset({BlobState.GC_PENDING, BlobState.MISSING}),
    BlobState.GC_PENDING: frozenset(
        {BlobState.AVAILABLE, BlobState.TRASHED, BlobState.MISSING}
    ),
    BlobState.TRASHED: frozenset(
        {BlobState.AVAILABLE, BlobState.PURGED, BlobState.MISSING}
    ),
    BlobState.MISSING: frozenset({BlobState.PURGED}),
    BlobState.PURGED: frozenset(),
}


@dataclass(frozen=True)
class BlobIncarnation:
    workspace_id: UUID
    blob_id: UUID
    epoch: BlobEpoch
    byte_size: int
    storage_key: str
    state: BlobState = BlobState.PENDING_INSTALL
    publish_generation: int = 0
    security_generation: int = 0
    gc_generation: int = 0

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.blob_id, "blob_id")
        if not isinstance(self.epoch, BlobEpoch):
            raise ValueError("epoch must be a BlobEpoch")
        if not isinstance(self.state, BlobState):
            raise ValueError("state must be a BlobState")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise ValueError("byte_size must be a non-negative integer")
        if not isinstance(self.storage_key, str):
            raise ValueError("storage_key must be a string")
        segments = self.storage_key.split("/")
        has_windows_drive = (
            len(self.storage_key) >= 2
            and self.storage_key[0].isalpha()
            and self.storage_key[1] == ":"
        )
        if (
            not self.storage_key
            or self.storage_key.startswith("/")
            or "\\" in self.storage_key
            or has_windows_drive
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise ValueError("storage_key must be an opaque relative key")
        for name in ("publish_generation", "security_generation", "gc_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def transition(
        self, state: BlobState, *, generation: int | None = None
    ) -> BlobIncarnation:
        if state not in _BLOB_TRANSITIONS[self.state]:
            raise ValueError(f"invalid blob transition: {self.state} -> {state}")
        updates: dict[str, object] = {"state": state}
        if state is BlobState.AVAILABLE and self.state is BlobState.PENDING_INSTALL:
            if generation is None:
                raise ValueError("publish generation is required")
            updates["publish_generation"] = self._next_generation("publish", generation)
        elif state is BlobState.GC_PENDING:
            if generation is None:
                raise ValueError("gc generation is required")
            updates["gc_generation"] = self._next_generation("gc", generation)
        elif self.state in {BlobState.GC_PENDING, BlobState.TRASHED}:
            if generation is None or generation != self.gc_generation:
                raise ValueError("generation does not match the active operation")
        elif generation is not None:
            raise ValueError("transition does not accept an operation generation")
        return replace(self, **updates)

    def advance_security_generation(self) -> BlobIncarnation:
        return replace(self, security_generation=self.security_generation + 1)

    def _next_generation(self, kind: str, supplied: int | None) -> int:
        current = getattr(self, f"{kind}_generation")
        expected = current + 1
        if supplied is not None and supplied != expected:
            raise ValueError(f"{kind} generation must advance exactly once")
        return expected


@dataclass(frozen=True)
class StoredBlob:
    blob_id: UUID
    identity: WorkspaceBlobIdentity
    next_epoch: BlobEpoch = BlobEpoch(1)
    active: BlobIncarnation | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.blob_id, "blob_id")
        if not isinstance(self.identity, WorkspaceBlobIdentity):
            raise ValueError("identity must be a WorkspaceBlobIdentity")
        if not isinstance(self.next_epoch, BlobEpoch):
            raise ValueError("next_epoch must be a BlobEpoch")
        if self.active is None:
            return
        if (
            self.active.workspace_id != self.identity.workspace_id
            or self.active.blob_id != self.blob_id
        ):
            raise ValueError("active incarnation must belong to the aggregate identity")
        if self.active.state is BlobState.PURGED:
            raise ValueError("a purged incarnation cannot be active")
        if self.active.epoch.value >= self.next_epoch.value:
            raise ValueError("next_epoch must be greater than every allocated epoch")

    @property
    def active_epoch(self) -> BlobEpoch | None:
        return None if self.active is None else self.active.epoch

    def allocate(self, *, byte_size: int, storage_key: str) -> StoredBlob:
        if self.active is not None and self.active.state is not BlobState.PURGED:
            raise ValueError("an active incarnation already exists")
        incarnation = BlobIncarnation(
            workspace_id=self.identity.workspace_id,
            blob_id=self.blob_id,
            epoch=self.next_epoch,
            byte_size=byte_size,
            storage_key=storage_key,
        )
        return replace(self, next_epoch=self.next_epoch.next(), active=incarnation)

    def update_active(self, incarnation: BlobIncarnation) -> StoredBlob:
        if not isinstance(incarnation, BlobIncarnation):
            raise ValueError("incarnation must be a BlobIncarnation")
        if self.active is None or incarnation.epoch != self.active.epoch:
            raise ValueError("incarnation does not match active_epoch")
        if (
            incarnation.workspace_id != self.identity.workspace_id
            or incarnation.blob_id != self.blob_id
        ):
            raise ValueError("incarnation does not belong to this aggregate")
        current = self.active
        if (
            incarnation.byte_size != current.byte_size
            or incarnation.storage_key != current.storage_key
        ):
            raise ValueError("incarnation immutable storage facts cannot change")
        if incarnation.security_generation not in {
            current.security_generation,
            current.security_generation + 1,
        }:
            raise ValueError("security generation must advance at most once")
        state_changed = incarnation.state is not current.state
        if state_changed and incarnation.state not in _BLOB_TRANSITIONS[current.state]:
            raise ValueError(
                f"invalid blob transition: {current.state} -> {incarnation.state}"
            )
        expected_publish = current.publish_generation
        if (
            current.state is BlobState.PENDING_INSTALL
            and incarnation.state is BlobState.AVAILABLE
        ):
            expected_publish += 1
        if incarnation.publish_generation != expected_publish:
            raise ValueError("publish generation contradicts the state transition")
        expected_gc = current.gc_generation
        if (
            incarnation.state is BlobState.GC_PENDING
            and current.state is not BlobState.GC_PENDING
        ):
            expected_gc += 1
        if incarnation.gc_generation != expected_gc:
            raise ValueError("gc generation contradicts the state transition")
        return replace(
            self, active=None if incarnation.state is BlobState.PURGED else incarnation
        )


class ScannerVerdict(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    MALICIOUS = "malicious"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StoredBlobInspection:
    inspection_id: UUID
    workspace_id: UUID
    blob_id: UUID
    epoch: BlobEpoch
    registry_version: str
    detector_id: str
    detector_version: str
    format_id: str
    canonical_media_type: str
    validation_schema_version: str
    metadata: Mapping[str, str | int | bool]
    scanner_verdict: ScannerVerdict
    scanner_provider: str | None = None
    scanner_version: str | None = None
    ordinary_available: bool = True

    def __post_init__(self) -> None:
        _require_uuid(self.inspection_id, "inspection_id")
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.blob_id, "blob_id")
        if not isinstance(self.epoch, BlobEpoch):
            raise ValueError("epoch must be a BlobEpoch")
        if not isinstance(self.scanner_verdict, ScannerVerdict):
            raise ValueError("scanner_verdict must be a ScannerVerdict")
        for name in (
            "registry_version",
            "detector_id",
            "detector_version",
            "format_id",
            "canonical_media_type",
            "validation_schema_version",
        ):
            _require_bounded_text(getattr(self, name), name)
        if self.scanner_verdict is ScannerVerdict.NOT_CONFIGURED:
            if self.scanner_provider is not None or self.scanner_version is not None:
                raise ValueError("not_configured inspection cannot name a scanner")
        else:
            _require_bounded_text(self.scanner_provider, "scanner_provider")
            _require_bounded_text(self.scanner_version, "scanner_version")
        if not isinstance(self.ordinary_available, bool):
            raise ValueError("ordinary_available must be a boolean")
        if self.ordinary_available and self.scanner_verdict in {
            ScannerVerdict.SUSPICIOUS,
            ScannerVerdict.MALICIOUS,
            ScannerVerdict.UNAVAILABLE,
        }:
            raise ValueError("unsafe scanner verdict cannot be ordinarily available")
        if not isinstance(self.metadata, Mapping) or len(self.metadata) > 32:
            raise ValueError("metadata must be a bounded mapping")
        copied_metadata: dict[str, str | int | bool] = {}
        for key, value in self.metadata.items():
            _require_bounded_text(key, "metadata key", maximum=80)
            if (
                not isinstance(value, (str, int, bool))
                or isinstance(value, str)
                and len(value) > 512
            ):
                raise ValueError("metadata values must be bounded scalar values")
            copied_metadata[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(copied_metadata))


class SecurityState(StrEnum):
    BLOCKED_PENDING_MOVE = "blocked_pending_move"
    QUARANTINED = "quarantined"
    RESTORE_PENDING = "restore_pending"
    RESTORED_BLOCKED = "restored_blocked"


_SECURITY_TRANSITIONS = {
    SecurityState.BLOCKED_PENDING_MOVE: SecurityState.QUARANTINED,
    SecurityState.QUARANTINED: SecurityState.RESTORE_PENDING,
    SecurityState.RESTORE_PENDING: SecurityState.RESTORED_BLOCKED,
}


@dataclass(frozen=True)
class SecurityBlockedBlob:
    workspace_id: UUID
    blob_id: UUID
    epoch: BlobEpoch
    generation: int
    state: SecurityState
    reason_code: str

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.blob_id, "blob_id")
        if not isinstance(self.epoch, BlobEpoch):
            raise ValueError("epoch must be a BlobEpoch")
        if not isinstance(self.state, SecurityState):
            raise ValueError("state must be a SecurityState")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation <= 0
        ):
            raise ValueError("security generation must be positive")
        if (
            not isinstance(self.reason_code, str)
            or not self.reason_code
            or len(self.reason_code) > 80
            or not self.reason_code.replace("_", "").isalnum()
        ):
            raise ValueError("reason_code must be a bounded safe identifier")

    def transition(
        self, state: SecurityState, *, generation: int
    ) -> SecurityBlockedBlob:
        if generation != self.generation:
            raise ValueError("stale security generation")
        if _SECURITY_TRANSITIONS.get(self.state) is not state:
            raise ValueError(f"invalid security transition: {self.state} -> {state}")
        return replace(self, state=state)
