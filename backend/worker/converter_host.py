"""Allowlisted subprocess host for bundled Library converters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    # Execute from the same checked-in/installed package as the dispatcher,
    # independently of cwd, user site packages, and another editable worktree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.converters.external_protocol import conversion_result_to_payload
from backend.spi.conversion import (
    CONVERSION_OUTPUT_SCHEMA_VERSION,
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConverterPlugin,
)

_REQUEST_LIMIT = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_REQUEST_KEYS = frozenset(
    {
        "protocol",
        "source_path",
        "source_sha256",
        "byte_size",
        "format_id",
        "media_type",
        "original_filename",
        "limits",
        "options",
        "trace_id",
    }
)
_HEALTH_REQUEST_KEYS = frozenset({"protocol", "operation"})
_LIMIT_KEYS = frozenset(field.name for field in fields(ConversionLimits))


class _HostBlobHandle:
    def __init__(self, path: Path, byte_size: int) -> None:
        self.path = path
        self.byte_size = byte_size

    def open_binary(self):
        return self.path.open("rb")

    def copy_to(self, destination: Path) -> int:
        raise RuntimeError("nested converter source copies are not supported")


def _plugin(converter_id: str) -> ConverterPlugin:
    if converter_id == "dtxwiki.ooxml":
        from backend.converters.ooxml_adapter import OOXMLConverter

        return OOXMLConverter()
    if converter_id == "dtxwiki.pypdf":
        from backend.converters.pdf_adapter import PdfTextConverter

        return PdfTextConverter()
    if converter_id == "dtxwiki.pillow-image":
        from backend.converters.image_adapter import ImageMetadataConverter

        return ImageMetadataConverter()
    if converter_id == "dtxwiki.hdf5":
        from backend.converters.hdf5_adapter import Hdf5Converter

        return Hdf5Converter()
    if converter_id == "dtxwiki.parquet":
        from backend.converters.parquet_adapter import ParquetConverter

        return ParquetConverter()
    raise ConversionError(
        "converter_not_allowlisted",
        "The requested converter is not allowlisted by this host.",
        fallback_allowed=False,
    )


def _load_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_REQUEST_LIMIT + 1)
    if len(raw) > _REQUEST_LIMIT:
        raise ConversionError(
            "converter_request_limit", "The converter request is too large."
        )
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConversionError(
            "converter_request_invalid", "The converter request is invalid."
        ) from exc
    if not isinstance(value, dict) or value.get("protocol") != CONVERTER_SPI_VERSION:
        raise ConversionError(
            "converter_protocol_mismatch",
            "The converter request protocol is incompatible.",
        )
    keys = set(value)
    if keys not in {_REQUEST_KEYS, _HEALTH_REQUEST_KEYS}:
        raise ConversionError(
            "converter_protocol_mismatch",
            "The converter request protocol is incompatible.",
        )
    return value


def _conversion_request(payload: dict[str, Any]) -> ConversionRequest:
    if not isinstance(payload, dict) or set(payload) != _REQUEST_KEYS:
        raise ConversionError(
            "converter_request_invalid", "The converter request envelope is invalid."
        )
    source_name = payload.get("source_path")
    if source_name != "source.bin":
        raise ConversionError(
            "converter_source_invalid", "The converter source path is invalid."
        )
    source_path = (Path.cwd() / source_name).resolve()
    if (
        source_path.parent != Path.cwd().resolve()
        or source_path.is_symlink()
        or not source_path.is_file()
    ):
        raise ConversionError(
            "converter_source_invalid", "The converter source is unavailable."
        )
    raw_byte_size = payload.get("byte_size")
    raw_limits = payload.get("limits")
    if (
        type(raw_byte_size) is not int
        or raw_byte_size < 0
        or not isinstance(raw_limits, dict)
        or set(raw_limits) != _LIMIT_KEYS
    ):
        raise ConversionError(
            "converter_request_invalid", "The converter request limits are invalid."
        )
    try:
        limits = ConversionLimits(**raw_limits)
    except (TypeError, ValueError) as exc:
        raise ConversionError(
            "converter_request_invalid", "The converter request limits are invalid."
        ) from exc
    byte_size = raw_byte_size
    if byte_size > limits.max_input_bytes:
        raise ConversionError(
            "converter_request_limit", "The converter input exceeds its size limit."
        )
    if source_path.stat().st_size != byte_size:
        raise ConversionError(
            "converter_source_integrity", "The converter source size changed."
        )
    digest = hashlib.sha256()
    with source_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    source_sha256 = payload.get("source_sha256")
    if (
        not isinstance(source_sha256, str)
        or not _SHA256.fullmatch(source_sha256)
        or digest.hexdigest() != source_sha256
    ):
        raise ConversionError(
            "converter_source_integrity", "The converter source hash changed."
        )
    format_id = _bounded_string(
        payload.get("format_id"),
        "format_id",
        min(128, limits.max_string_bytes),
    )
    media_type = _bounded_string(
        payload.get("media_type"),
        "media_type",
        min(255, limits.max_string_bytes),
    )
    if not format_id or not _MEDIA_TYPE.fullmatch(media_type):
        raise ConversionError(
            "converter_request_invalid", "The converter media type is invalid."
        )
    original_filename = _bounded_string(
        payload.get("original_filename"),
        "original_filename",
        min(4096, limits.max_string_bytes),
    )
    if (
        original_filename in {".", ".."}
        or "/" in original_filename
        or "\\" in original_filename
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in original_filename
        )
    ):
        raise ConversionError(
            "converter_request_invalid", "The converter filename is invalid."
        )
    trace_id = _bounded_string(
        payload.get("trace_id"),
        "trace_id",
        min(4096, limits.max_string_bytes),
    )
    options = payload.get("options")
    if not isinstance(options, dict):
        raise ConversionError(
            "converter_request_invalid", "The converter options are invalid."
        )
    _validate_json_tree(options, limits)
    return ConversionRequest(
        source=_HostBlobHandle(source_path, byte_size),
        source_sha256=digest.hexdigest(),
        byte_size=byte_size,
        format_id=format_id,
        media_type=media_type,
        original_filename=original_filename,
        limits=limits,
        options=options,
        trace_id=trace_id,
    )


def main(argv: list[str] | None = None) -> int:
    from backend.app.process_tree import guard_managed_parent

    guard_managed_parent()
    parser = argparse.ArgumentParser()
    parser.add_argument("--converter", required=True)
    args = parser.parse_args(argv)
    try:
        plugin = _plugin(args.converter)
        descriptor = plugin.descriptor()
        request_payload = _load_request()
        if request_payload.get("operation") == "health":
            payload = {
                "protocol": CONVERTER_SPI_VERSION,
                "operation": "health",
                "status": "ready",
                "converter_id": descriptor.converter_id,
                "converter_version": descriptor.converter_version,
                "formats": sorted(descriptor.formats),
                "media_types": sorted(descriptor.media_types),
                "output_schema_version": CONVERSION_OUTPUT_SCHEMA_VERSION,
            }
            _write_payload(payload)
            return 0
        request = _conversion_request(request_payload)
        if (
            request.format_id not in descriptor.formats
            or request.media_type not in descriptor.media_types
        ):
            raise ConversionError(
                "unsupported", "The converter does not support this format."
            )
        payload = conversion_result_to_payload(plugin.convert(request))
    except ConversionError as exc:
        payload = {
            "protocol": CONVERTER_SPI_VERSION,
            "error": {
                "code": exc.code,
                "message": exc.safe_message,
                "retryable": exc.retryable,
                "fallback_allowed": exc.fallback_allowed,
            },
        }
    except Exception:
        payload = {
            "protocol": CONVERTER_SPI_VERSION,
            "error": {
                "code": "converter_internal_error",
                "message": "The converter failed unexpectedly.",
                "retryable": False,
                "fallback_allowed": True,
            },
        }
    _write_payload(payload)
    return 0


def _write_payload(payload: dict[str, Any]) -> None:
    """Write one JSON frame as UTF-8 bytes, independent of process locale."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _bounded_string(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ConversionError(
            "converter_request_invalid", f"The converter {field} is invalid."
        )
    return value


def _validate_json_tree(value: Any, limits: ConversionLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise ConversionError(
                "converter_request_invalid",
                "The converter options exceed structure limits.",
            )
        if current is None or type(current) in {bool, int}:
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ConversionError(
                    "converter_request_invalid",
                    "The converter options contain a non-finite number.",
                )
            continue
        if isinstance(current, str):
            if len(current.encode("utf-8")) > limits.max_string_bytes:
                raise ConversionError(
                    "converter_request_invalid",
                    "The converter options contain an oversized string.",
                )
            continue
        if isinstance(current, dict):
            for key, child in current.items():
                if (
                    not isinstance(key, str)
                    or len(key.encode("utf-8")) > limits.max_string_bytes
                ):
                    raise ConversionError(
                        "converter_request_invalid",
                        "The converter options contain a non-string key.",
                    )
                stack.append((child, depth + 1))
            continue
        if isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
            continue
        raise ConversionError(
            "converter_request_invalid", "The converter options are invalid."
        )


if __name__ == "__main__":
    raise SystemExit(main())
