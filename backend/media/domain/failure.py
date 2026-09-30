"""Safe media failure taxonomy suitable for outer adapters and audit linking."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class MediaFailureCode(StrEnum):
    TYPE_UNSUPPORTED = "media_type_unsupported"
    CONTENT_MISMATCH = "media_content_mismatch"
    CONTAINER_INVALID = "media_container_invalid"
    TOO_LARGE = "media_too_large"
    QUOTA_EXCEEDED = "media_quota_exceeded"
    SCAN_UNAVAILABLE = "media_scan_unavailable"
    SCAN_REJECTED = "media_scan_rejected"
    IN_USE = "media_in_use"
    USED_IN_HISTORY = "media_used_in_history"
    STATE_CHANGED = "media_state_changed"


class MediaFailurePhase(StrEnum):
    ADMISSION = "admission"
    INSTALL = "install"
    SECURITY = "security"
    GC = "gc"
    CONVERSION = "conversion"
    RENDER = "render"


class FailureDisposition(StrEnum):
    REJECT = "reject"
    RETRY = "retry"
    HOLD = "hold"
    SECURITY_BLOCK = "security_block"


class MediaStagingError(ValueError):
    """Stable staging admission failure exposed to outer API adapters."""

    error_code: str

    def __init__(self) -> None:
        super().__init__(self.error_code)


class MediaStagingLimitExceededError(MediaStagingError):
    error_code = "media_staging_limit_exceeded"


class MediaStagingBytesExceededError(MediaStagingError):
    error_code = "media_staging_bytes_exceeded"


_FORBIDDEN_SAFE_DETAIL_PARTS = frozenset(
    {
        "path",
        "storage_key",
        "storagekey",
        "digest",
        "hash",
        "scanner",
        "provider",
        "foreign_id",
        "foreignid",
        "audit_id",
        "auditid",
    }
)


def _safe_identifier(value: str, name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.replace("_", "").isalnum()
    ):
        raise ValueError(f"{name} must be a bounded stable identifier")


@dataclass(frozen=True)
class MediaFailure:
    code: MediaFailureCode
    phase: MediaFailurePhase
    reason_code: str
    disposition: FailureDisposition
    retryable: bool
    safe_details: Mapping[str, str | int | bool]
    internal_causal_audit_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.code, MediaFailureCode):
            raise ValueError("code must be a MediaFailureCode")
        if not isinstance(self.phase, MediaFailurePhase):
            raise ValueError("phase must be a MediaFailurePhase")
        if not isinstance(self.disposition, FailureDisposition):
            raise ValueError("disposition must be a FailureDisposition")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        if self.disposition is FailureDisposition.RETRY and not self.retryable:
            raise ValueError("retry disposition must be retryable")
        if (
            self.disposition
            in {FailureDisposition.REJECT, FailureDisposition.SECURITY_BLOCK}
            and self.retryable
        ):
            raise ValueError("terminal failure disposition cannot be retryable")
        if not isinstance(self.internal_causal_audit_id, UUID):
            raise ValueError("internal_causal_audit_id must be a UUID")
        _safe_identifier(self.reason_code, "reason_code", maximum=80)
        if not isinstance(self.safe_details, Mapping) or len(self.safe_details) > 8:
            raise ValueError("safe_details contains too many fields")
        copied: dict[str, str | int | bool] = {}
        for key, value in self.safe_details.items():
            _safe_identifier(key, "safe_details key", maximum=40)
            normalized = key.lower()
            if any(part in normalized for part in _FORBIDDEN_SAFE_DETAIL_PARTS):
                raise ValueError("safe_details key may expose sensitive internals")
            if (
                not isinstance(value, (str, int, bool))
                or isinstance(value, str)
                and len(value) > 160
            ):
                raise ValueError("safe_details values must be bounded scalar values")
            copied[key] = value
        object.__setattr__(self, "safe_details", MappingProxyType(copied))
