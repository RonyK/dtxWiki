"""Versioned worker-side contract for Library Preview providers.

Providers receive path-free, read-only input capabilities and must emit output
through a caller-owned bounded sink.  The HTTP process never imports provider
implementations and no provider can publish a storage path or URL.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Literal, Protocol
from uuid import UUID

PREVIEW_PROVIDER_SPI_VERSION = "1.0"
PREVIEW_OUTPUT_SCHEMA_VERSION = "1.0"
PREVIEW_RENDERER_CONTRACT_VERSION = "1.0"

type PreviewInputKind = Literal["source", "conversion_artifact"]
type PreviewVariantKind = Literal[
    "markdown",
    "text",
    "table",
    "pdf",
    "image",
    "audio",
    "video",
    "metadata",
]
type PreviewDelivery = Literal["inline", "range", "text"]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_RENDERER_CONTRACT = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+){2,7}$")


def _freeze_json(value: object, *, path: str) -> JsonValue:
    if value is None or type(value) in {str, bool, int}:
        return value  # type: ignore[return-value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError(f"{path} is too large")
        frozen: dict[str, JsonValue] = {}
        for key, child in value.items():
            if type(key) is not str or not key or len(key.encode("utf-8")) > 128:
                raise ValueError(f"{path} contains an invalid key")
            frozen[key] = _freeze_json(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > 1024:
            raise ValueError(f"{path} is too large")
        return tuple(
            _freeze_json(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        )
    raise TypeError(f"{path} contains a non-JSON value")


def thaw_json(value: JsonValue) -> object:
    """Return a plain JSON-compatible snapshot for persistence or hashing."""

    if isinstance(value, Mapping):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


class PreviewReadOnlyBlobHandle(Protocol):
    """Capability-style access to one immutable input without a storage path."""

    @property
    def byte_size(self) -> int: ...

    def open_binary(self) -> BinaryIO: ...

    def copy_to(self, destination: Path) -> int: ...


@dataclass(frozen=True, slots=True)
class PreviewLimits:
    max_input_bytes: int
    max_variant_bytes: int = 32 * 1024 * 1024
    max_total_output_bytes: int = 64 * 1024 * 1024
    max_variants: int = 16
    max_pages: int = 1_000
    max_pixels: int = 100_000_000
    max_duration_seconds: int = 14_400
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name in (
            "max_input_bytes",
            "max_variant_bytes",
            "max_total_output_bytes",
            "max_variants",
            "max_pages",
            "max_pixels",
            "max_duration_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")


@dataclass(frozen=True, slots=True)
class PreviewProviderDescriptor:
    provider_id: str
    provider_version: str
    spi_version: str
    formats: frozenset[str]
    media_types: frozenset[str]
    input_kinds: frozenset[PreviewInputKind]
    output_renderer_contracts: frozenset[str]
    output_media_types: frozenset[str]
    priority: int
    execution_mode: Literal["builtin", "subprocess", "sidecar"]
    network_required: bool
    deterministic: bool
    license_expression: str
    source_url: str
    distribution_digest: str | None = None
    sbom_sha256: str | None = None
    signature_identity: str | None = None
    sandbox_profile: str | None = None
    max_input_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class PreviewProbeRequest:
    format_id: str
    media_type: str
    input_kind: PreviewInputKind
    byte_size: int

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.format_id)
            or not _MEDIA_TYPE.fullmatch(self.media_type)
            or self.input_kind not in {"source", "conversion_artifact"}
            or type(self.byte_size) is not int
            or self.byte_size < 0
        ):
            raise ValueError("preview probe request is invalid")


@dataclass(frozen=True, slots=True)
class PreviewProbeResult:
    supported: bool
    confidence: Decimal = Decimal("1.000")
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.supported) is not bool or not (
            self.confidence.is_finite()
            and Decimal("0") <= self.confidence <= Decimal("1")
        ):
            raise ValueError("preview probe result is invalid")
        if self.reason_code is not None and not _IDENTIFIER.fullmatch(self.reason_code):
            raise ValueError("preview probe reason_code is invalid")


@dataclass(frozen=True, slots=True)
class PreviewVariantReceipt:
    """Opaque acknowledgement created only by the caller-owned output sink."""

    receipt_id: UUID
    kind: PreviewVariantKind
    media_type: str
    renderer_contract: str
    delivery: PreviewDelivery
    byte_size: int
    sha256: str
    rank: int
    ordinal: int = 0
    filename: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    page_count: int | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.receipt_id, UUID)
            or self.kind
            not in {
                "markdown",
                "text",
                "table",
                "pdf",
                "image",
                "audio",
                "video",
                "metadata",
            }
            or not _MEDIA_TYPE.fullmatch(self.media_type)
            or not _RENDERER_CONTRACT.fullmatch(self.renderer_contract)
            or self.delivery not in {"inline", "range", "text"}
            or type(self.byte_size) is not int
            or self.byte_size <= 0
            or not _SHA256.fullmatch(self.sha256)
            or type(self.rank) is not int
            or self.rank < 0
            or type(self.ordinal) is not int
            or self.ordinal < 0
        ):
            raise ValueError("preview variant receipt is invalid")
        for name in ("width", "height", "duration_ms", "page_count"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"preview variant {name} is invalid")
        if self.filename is not None and (
            not self.filename
            or len(self.filename.encode("utf-8")) > 255
            or any(character in self.filename for character in ("/", "\\", "\x00"))
        ):
            raise ValueError("preview variant filename is invalid")
        object.__setattr__(
            self,
            "metadata",
            _freeze_json(self.metadata, path="variant.metadata"),
        )


class PreviewOutputSink(Protocol):
    """Bound and retain one provider output without exposing its destination."""

    def write_variant(
        self,
        *,
        source: BinaryIO,
        expected_sha256: str,
        expected_byte_size: int,
        kind: PreviewVariantKind,
        media_type: str,
        renderer_contract: str,
        delivery: PreviewDelivery,
        rank: int,
        ordinal: int = 0,
        filename: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        page_count: int | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> PreviewVariantReceipt: ...


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    workspace_id: UUID
    library_item_id: UUID
    version_id: UUID
    input_kind: PreviewInputKind
    input_conversion_attempt_id: UUID | None
    input_artifact_id: UUID | None
    source: PreviewReadOnlyBlobHandle
    input_sha256: str
    byte_size: int
    format_id: str
    media_type: str
    original_filename: str
    limits: PreviewLimits
    recipe: Mapping[str, JsonValue] = field(default_factory=dict)
    trace_id: str = ""

    def __post_init__(self) -> None:
        artifact_input = self.input_kind == "conversion_artifact"
        if (
            not all(
                isinstance(value, UUID)
                for value in (self.workspace_id, self.library_item_id, self.version_id)
            )
            or self.input_kind not in {"source", "conversion_artifact"}
            or artifact_input
            != (
                self.input_conversion_attempt_id is not None
                and self.input_artifact_id is not None
            )
            or (not artifact_input and self.input_conversion_attempt_id is not None)
            or (not artifact_input and self.input_artifact_id is not None)
            or not _SHA256.fullmatch(self.input_sha256)
            or type(self.byte_size) is not int
            or not 0 < self.byte_size <= self.limits.max_input_bytes
            or self.source.byte_size != self.byte_size
            or not _IDENTIFIER.fullmatch(self.format_id)
            or not _MEDIA_TYPE.fullmatch(self.media_type)
            or not self.original_filename
            or len(self.original_filename.encode("utf-8")) > 255
            or len(self.trace_id.encode("utf-8")) > 256
        ):
            raise ValueError("preview request is invalid")
        object.__setattr__(
            self,
            "recipe",
            _freeze_json(self.recipe, path="preview.recipe"),
        )


@dataclass(frozen=True, slots=True)
class PreviewResult:
    variants: tuple[PreviewVariantReceipt, ...]
    completeness: Literal["complete", "partial"] = "complete"
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            type(self.variants) is not tuple
            or not self.variants
            or not all(type(value) is PreviewVariantReceipt for value in self.variants)
            or len({value.receipt_id for value in self.variants}) != len(self.variants)
            or self.completeness not in {"complete", "partial"}
        ):
            raise ValueError("preview result is invalid")
        object.__setattr__(
            self,
            "metadata",
            _freeze_json(self.metadata, path="preview.result.metadata"),
        )


class PreviewProviderPlugin(Protocol):
    def descriptor(self) -> PreviewProviderDescriptor: ...

    def probe(self, request: PreviewProbeRequest) -> PreviewProbeResult: ...

    def generate(
        self,
        request: PreviewRequest,
        sink: PreviewOutputSink,
    ) -> PreviewResult: ...


class PreviewProviderError(RuntimeError):
    """Safe provider failure with deterministic retry/fallback semantics."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        fallback_allowed: bool = True,
    ) -> None:
        if (
            not _IDENTIFIER.fullmatch(code)
            or not safe_message
            or len(safe_message.encode("utf-8")) > 4096
            or type(retryable) is not bool
            or type(fallback_allowed) is not bool
        ):
            raise ValueError("preview provider error envelope is invalid")
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed


class UnsupportedPreviewError(PreviewProviderError):
    def __init__(self, safe_message: str = "provider does not support this input"):
        super().__init__(
            "unsupported",
            safe_message,
            retryable=False,
            fallback_allowed=True,
        )


def validate_descriptor_shape(descriptor: PreviewProviderDescriptor) -> None:
    """Validate transport-neutral descriptor fields shared by registries/tests."""

    if type(descriptor) is not PreviewProviderDescriptor:
        raise ValueError("preview provider descriptor has an invalid type")
    if (
        not _IDENTIFIER.fullmatch(descriptor.provider_id)
        or not _VERSION.fullmatch(descriptor.provider_version)
        or descriptor.spi_version != PREVIEW_PROVIDER_SPI_VERSION
        or type(descriptor.formats) is not frozenset
        or not descriptor.formats
        or not all(_IDENTIFIER.fullmatch(value) for value in descriptor.formats)
        or type(descriptor.media_types) is not frozenset
        or not descriptor.media_types
        or not all(_MEDIA_TYPE.fullmatch(value) for value in descriptor.media_types)
        or type(descriptor.input_kinds) is not frozenset
        or not descriptor.input_kinds
        or not descriptor.input_kinds <= {"source", "conversion_artifact"}
        or type(descriptor.output_renderer_contracts) is not frozenset
        or not descriptor.output_renderer_contracts
        or not all(
            _RENDERER_CONTRACT.fullmatch(value)
            for value in descriptor.output_renderer_contracts
        )
        or type(descriptor.output_media_types) is not frozenset
        or not descriptor.output_media_types
        or not all(
            _MEDIA_TYPE.fullmatch(value) for value in descriptor.output_media_types
        )
        or type(descriptor.priority) is not int
        or not -1_000_000 <= descriptor.priority <= 1_000_000
        or descriptor.execution_mode not in {"builtin", "subprocess", "sidecar"}
        or type(descriptor.network_required) is not bool
        or type(descriptor.deterministic) is not bool
        or not descriptor.license_expression.strip()
        or not descriptor.source_url.startswith("https://")
    ):
        raise ValueError("preview provider descriptor is invalid")
    if descriptor.max_input_bytes is not None and (
        type(descriptor.max_input_bytes) is not int or descriptor.max_input_bytes <= 0
    ):
        raise ValueError("preview provider max_input_bytes is invalid")
    for digest in (descriptor.distribution_digest, descriptor.sbom_sha256):
        if digest is not None and not _SHA256.fullmatch(digest):
            raise ValueError("preview provider digest is invalid")
    # Ensure the frozen shape is JSON-serializable after thawing; this catches
    # accidental future non-wire values at the SPI boundary.
    json.dumps(
        {
            "formats": sorted(descriptor.formats),
            "media_types": sorted(descriptor.media_types),
        },
        separators=(",", ":"),
    )


__all__ = [
    "PREVIEW_OUTPUT_SCHEMA_VERSION",
    "PREVIEW_PROVIDER_SPI_VERSION",
    "PREVIEW_RENDERER_CONTRACT_VERSION",
    "PreviewDelivery",
    "PreviewInputKind",
    "PreviewLimits",
    "PreviewOutputSink",
    "PreviewProbeRequest",
    "PreviewProbeResult",
    "PreviewProviderDescriptor",
    "PreviewProviderError",
    "PreviewProviderPlugin",
    "PreviewReadOnlyBlobHandle",
    "PreviewRequest",
    "PreviewResult",
    "PreviewVariantKind",
    "PreviewVariantReceipt",
    "UnsupportedPreviewError",
    "thaw_json",
    "validate_descriptor_shape",
]
