"""Versioned service-provider contract for Library conversion plugins.

The API process depends only on these value objects.  Concrete third-party
parsers are discovered and executed by the worker-side registry, never by an
HTTP route.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

CONVERTER_SPI_VERSION = "1.0"
CONVERSION_OUTPUT_SCHEMA_VERSION = "1.0"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)

type JsonScalar = str | int | float | bool | None
type JsonValue = (
    JsonScalar | list[JsonValue] | tuple[JsonValue, ...] | dict[str, JsonValue]
)


class FrozenJsonDict(dict[str, JsonValue]):
    """A JSON-compatible mapping whose snapshot cannot be mutated normally.

    ``MappingProxyType`` would provide the same write protection but is not
    serializable by the standard JSON encoder.  Keeping a ``dict`` subclass
    lets the worker protocol serialize the value without a custom encoder,
    while every nested mapping and sequence is frozen at the SPI boundary.
    """

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("JSON snapshot is immutable")

    __delitem__ = _immutable
    __ior__ = _immutable
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_json(value: object, *, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(child, path=f"{path}.{key}")
        return FrozenJsonDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        )
    raise TypeError(f"{path} contains a non-JSON value")


def _freeze_json_mapping(
    value: Mapping[str, JsonValue], *, path: str
) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, path=path)
    if not isinstance(frozen, FrozenJsonDict):  # pragma: no cover - type boundary
        raise TypeError(f"{path} must be a mapping")
    return frozen


class ReadOnlyBlobHandle(Protocol):
    """Capability-style access to one immutable source blob.

    The handle deliberately does not expose the backing storage path.  An
    executor that needs a path must copy the bytes into its own isolated scratch
    directory.
    """

    @property
    def byte_size(self) -> int: ...

    def open_binary(self) -> BinaryIO: ...

    def copy_to(self, destination: Path) -> int: ...


@dataclass(frozen=True, slots=True)
class ConversionLimits:
    max_input_bytes: int
    max_output_bytes: int = 8 * 1024 * 1024
    max_asset_bytes: int = 16 * 1024 * 1024
    max_assets: int = 64
    max_rows: int = 2_000
    max_columns: int = 200
    max_cell_bytes: int = 64 * 1024
    max_string_bytes: int = 1024 * 1024
    max_nodes: int = 100_000
    max_depth: int = 64
    max_pages: int = 1_000
    max_pdf_objects: int = 100_000
    max_rendered_pixels: int = 100_000_000
    max_archive_entries: int = 10_000
    max_archive_depth: int = 4
    max_archive_member_bytes: int = 64 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 512 * 1024 * 1024
    max_archive_expansion_ratio: float = 200.0
    max_image_width: int = 50_000
    max_image_height: int = 50_000
    max_image_pixels: int = 100_000_000
    max_image_frames: int = 32
    max_yaml_aliases: int = 1_000
    max_yaml_events: int = 200_000
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        integer_limits = (
            "max_input_bytes",
            "max_output_bytes",
            "max_asset_bytes",
            "max_assets",
            "max_rows",
            "max_columns",
            "max_cell_bytes",
            "max_string_bytes",
            "max_nodes",
            "max_depth",
            "max_pages",
            "max_pdf_objects",
            "max_rendered_pixels",
            "max_archive_entries",
            "max_archive_depth",
            "max_archive_member_bytes",
            "max_archive_uncompressed_bytes",
            "max_image_width",
            "max_image_height",
            "max_image_pixels",
            "max_image_frames",
            "max_yaml_aliases",
            "max_yaml_events",
        )
        for name in integer_limits:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_archive_expansion_ratio", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a positive finite number")
            try:
                valid = math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{name} must be a positive finite number")


@dataclass(frozen=True, slots=True)
class ConverterDescriptor:
    converter_id: str
    converter_version: str
    spi_version: str
    formats: frozenset[str]
    media_types: frozenset[str]
    priority: int
    execution_mode: Literal["builtin", "subprocess", "sidecar"]
    network_required: bool
    deterministic: bool
    license_expression: str
    source_url: str
    capabilities: frozenset[str]
    distribution_digest: str | None = None
    sbom_sha256: str | None = None
    signature_identity: str | None = None
    sandbox_profile: str | None = None
    # Immutable provider-version safety ceiling. Workspace policy may lower
    # this value but can never raise it.
    max_input_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    source: ReadOnlyBlobHandle
    source_sha256: str
    byte_size: int
    format_id: str
    media_type: str
    original_filename: str
    limits: ConversionLimits
    options: Mapping[str, JsonValue] = field(default_factory=dict)
    trace_id: str = ""

    def __post_init__(self) -> None:
        if (
            not _SHA256.fullmatch(self.source_sha256)
            or isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 0 <= self.byte_size <= self.limits.max_input_bytes
            or self.source.byte_size != self.byte_size
        ):
            raise ValueError("conversion source identity is invalid")
        if not _IDENTIFIER.fullmatch(self.format_id):
            raise ValueError("conversion format_id is invalid")
        if not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("conversion media_type is invalid")
        if (
            not self.original_filename
            or len(self.original_filename.encode("utf-8")) > 255
            or any(ord(character) < 32 for character in self.original_filename)
            or len(self.trace_id.encode("utf-8")) > 256
        ):
            raise ValueError("conversion request metadata is invalid")
        object.__setattr__(
            self,
            "options",
            _freeze_json_mapping(self.options, path="options"),
        )


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    format_id: str
    media_type: str
    original_filename: str
    byte_size: int

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.format_id)
            or not _MEDIA_TYPE.fullmatch(self.media_type)
            or not self.original_filename
            or len(self.original_filename.encode("utf-8")) > 255
            or isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise ValueError("converter probe request is invalid")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    supported: bool
    confidence: Decimal = Decimal("1.000")
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.supported) is not bool or not (
            self.confidence.is_finite()
            and Decimal("0") <= self.confidence <= Decimal("1")
        ):
            raise ValueError("converter probe result is invalid")
        if self.reason_code is not None and not _IDENTIFIER.fullmatch(self.reason_code):
            raise ValueError("converter probe reason_code is invalid")


@dataclass(frozen=True, slots=True)
class ProducedAsset:
    role: Literal["preview", "asset", "metadata", "location_map"]
    media_type: str
    content: bytes
    filename: str | None = None
    ordinal: int = 0

    def __post_init__(self) -> None:
        if self.role not in {"preview", "asset", "metadata", "location_map"}:
            raise ValueError("produced asset role is invalid")
        if (
            not _MEDIA_TYPE.fullmatch(self.media_type)
            or type(self.content) is not bytes
        ):
            raise ValueError("produced asset content is invalid")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("produced asset ordinal is invalid")
        if self.ordinal < 0:
            raise ValueError("produced asset ordinal is invalid")
        if self.filename is not None and (
            not self.filename
            or len(self.filename.encode("utf-8")) > 255
            or any(character in self.filename for character in ("/", "\\", "\x00"))
        ):
            raise ValueError("produced asset filename is invalid")


@dataclass(frozen=True, slots=True)
class SourceLocation:
    kind: str
    ordinal: int
    label: str | None = None
    data: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.kind)
            or isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
            or (self.label is not None and len(self.label.encode("utf-8")) > 4 * 1024)
        ):
            raise ValueError("source location is invalid")
        object.__setattr__(
            self,
            "data",
            _freeze_json_mapping(self.data, path="location.data"),
        )


@dataclass(frozen=True, slots=True)
class ConversionWarning:
    code: str
    message: str
    location: SourceLocation | None = None

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.code)
            or not self.message
            or len(self.message.encode("utf-8")) > 4 * 1024
        ):
            raise ValueError("conversion warning is invalid")


@dataclass(frozen=True, slots=True)
class ConversionResult:
    markdown: str | None
    assets: tuple[ProducedAsset, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    locations: tuple[SourceLocation, ...] = ()
    warnings: tuple[ConversionWarning, ...] = ()
    completeness: Literal["complete", "truncated", "metadata_only"] = "complete"
    quality_score: Decimal | None = None

    def __post_init__(self) -> None:
        if self.markdown is not None and not isinstance(self.markdown, str):
            raise TypeError("conversion markdown must be text or null")
        if (
            type(self.assets) is not tuple
            or not all(type(asset) is ProducedAsset for asset in self.assets)
            or type(self.locations) is not tuple
            or not all(type(location) is SourceLocation for location in self.locations)
            or type(self.warnings) is not tuple
            or not all(type(warning) is ConversionWarning for warning in self.warnings)
            or self.completeness not in {"complete", "truncated", "metadata_only"}
        ):
            raise ValueError("conversion result envelope is invalid")
        if self.quality_score is not None and not (
            isinstance(self.quality_score, Decimal)
            and self.quality_score.is_finite()
            and Decimal("0") <= self.quality_score <= Decimal("1")
        ):
            raise ValueError("conversion result quality_score is invalid")
        object.__setattr__(
            self,
            "metadata",
            _freeze_json_mapping(self.metadata, path="result.metadata"),
        )


class ConverterPlugin(Protocol):
    def descriptor(self) -> ConverterDescriptor: ...

    def probe(self, request: ProbeRequest) -> ProbeResult: ...

    def convert(self, request: ConversionRequest) -> ConversionResult: ...


class ConversionError(RuntimeError):
    """Safe converter failure with deterministic retry/fallback semantics."""

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
            or len(safe_message.encode("utf-8")) > 4 * 1024
            or type(retryable) is not bool
            or type(fallback_allowed) is not bool
        ):
            raise ValueError("conversion error envelope is invalid")
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed


class UnsupportedConversionError(ConversionError):
    def __init__(self, safe_message: str = "converter does not support this input"):
        super().__init__(
            "unsupported",
            safe_message,
            retryable=False,
            fallback_allowed=True,
        )


__all__ = [
    "CONVERSION_OUTPUT_SCHEMA_VERSION",
    "CONVERTER_SPI_VERSION",
    "ConversionError",
    "ConversionLimits",
    "ConversionRequest",
    "ConversionResult",
    "ConversionWarning",
    "ConverterDescriptor",
    "ConverterPlugin",
    "JsonValue",
    "ProbeRequest",
    "ProbeResult",
    "ProducedAsset",
    "ReadOnlyBlobHandle",
    "SourceLocation",
    "UnsupportedConversionError",
]
