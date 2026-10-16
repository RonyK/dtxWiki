"""Bounded JSON protocol for allowlisted subprocess converter providers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.spi.conversion import (
    CONVERSION_OUTPUT_SCHEMA_VERSION,
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConversionResult,
    ConversionWarning,
    ConverterDescriptor,
    ProbeRequest,
    ProbeResult,
    ProducedAsset,
    SourceLocation,
)

_REQUEST_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_RESULT_KEYS = frozenset(
    {
        "protocol",
        "markdown",
        "assets",
        "metadata",
        "locations",
        "warnings",
        "completeness",
        "quality_score",
        "error",
    }
)
_HEALTH_RESULT_KEYS = frozenset(
    {
        "protocol",
        "operation",
        "status",
        "converter_id",
        "converter_version",
        "formats",
        "media_types",
        "output_schema_version",
    }
)
_ENV_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
    "_DTXWIKI_MANAGED_WORKER_PID",
}


class ExternalProcessConverter:
    """Execute one fixed command without a shell in a private scratch folder.

    This adapter enforces transport bounds and timeouts.  It does not claim to
    provide an OS memory/network sandbox; registry policy must keep high-risk
    providers disabled unless the operator configured that boundary.
    """

    def __init__(
        self,
        descriptor: ConverterDescriptor,
        *,
        command: tuple[str, ...],
        preserve_executable_path: bool = False,
    ) -> None:
        if descriptor.spi_version != CONVERTER_SPI_VERSION:
            raise ValueError("converter SPI version mismatch")
        if not command or not all(
            isinstance(token, str) and token for token in command
        ):
            raise ValueError("converter command must contain non-empty string tokens")
        executable = Path(command[0])
        if not executable.is_absolute():
            raise ValueError("converter command executable must be an absolute path")
        if any("\x00" in token or "\r" in token or "\n" in token for token in command):
            raise ValueError("converter command contains a control character")
        try:
            resolved_executable = executable.resolve(strict=True)
        except OSError as exc:
            raise ValueError("converter command executable does not exist") from exc
        if not resolved_executable.is_file():
            raise ValueError("converter command executable must be a file")
        self._descriptor = descriptor
        # Resolving a virtual-environment Python launcher changes interpreter
        # identity: executing the symlink target directly bypasses pyvenv.cfg
        # and therefore the installed dtxwiki package. Bundled providers keep
        # the validated absolute launcher path; operator manifests retain the
        # stricter historical behavior of executing the resolved target.
        command_executable = (
            executable if preserve_executable_path else resolved_executable
        )
        self._command = (str(command_executable), *command[1:])

    def descriptor(self) -> ConverterDescriptor:
        return self._descriptor

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id in self._descriptor.formats)

    def healthcheck(self, *, timeout_seconds: float = 5.0) -> tuple[bool, str | None]:
        """Run the versioned provider handshake without supplying source bytes.

        Manifest admission proves only that an operator approved a pinned
        provider.  This handshake separately proves that the fixed command can
        start now and speaks the descriptor/schema version that the registry
        snapshotted.  It intentionally does not claim to replace signature,
        SBOM, licence, or OS-sandbox verification performed at installation.
        """

        payload = {
            "protocol": CONVERTER_SPI_VERSION,
            "operation": "health",
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            with tempfile.TemporaryDirectory(
                prefix="dtxwiki-converter-health-"
            ) as raw_dir:
                response = _run_bounded(
                    self._command,
                    cwd=Path(raw_dir),
                    stdin=encoded,
                    stdin_limit=_REQUEST_LIMIT,
                    stdout_limit=64 * 1024,
                    timeout_seconds=timeout_seconds,
                )
            self._validate_health_response(response)
        except (ConversionError, KeyError, TypeError, ValueError):
            return False, "converter_health_unavailable"
        return True, None

    def _validate_health_response(self, payload: dict[str, Any]) -> None:
        descriptor = self._descriptor
        if set(payload) != _HEALTH_RESULT_KEYS:
            raise ValueError("converter health envelope is invalid")
        if (
            payload.get("protocol") != CONVERTER_SPI_VERSION
            or payload.get("operation") != "health"
            or payload.get("status") != "ready"
            or payload.get("converter_id") != descriptor.converter_id
            or payload.get("converter_version") != descriptor.converter_version
            or payload.get("output_schema_version") != CONVERSION_OUTPUT_SCHEMA_VERSION
        ):
            raise ValueError("converter health identity does not match")
        formats = payload.get("formats")
        media_types = payload.get("media_types")
        if (
            type(formats) is not list
            or type(media_types) is not list
            or len(formats) != len(set(formats))
            or len(media_types) != len(set(media_types))
            or frozenset(formats) != descriptor.formats
            or frozenset(media_types) != descriptor.media_types
            or not all(type(value) is str for value in (*formats, *media_types))
        ):
            raise ValueError("converter health capabilities do not match")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        _validate_request(request)
        if (
            request.format_id not in self._descriptor.formats
            or request.media_type not in self._descriptor.media_types
        ):
            raise ConversionError(
                "unsupported",
                "The converter does not support the requested input type.",
                fallback_allowed=True,
            )
        with tempfile.TemporaryDirectory(prefix="dtxwiki-converter-") as raw_dir:
            scratch = Path(raw_dir)
            source_path = scratch / "source.bin"
            copied = request.source.copy_to(source_path)
            if (
                type(copied) is not int
                or copied != request.byte_size
                or source_path.stat().st_size != request.byte_size
            ):
                raise ConversionError(
                    "converter_source_integrity",
                    "The converter source size changed while it was staged.",
                    fallback_allowed=False,
                )
            if _sha256_file(source_path) != request.source_sha256:
                raise ConversionError(
                    "converter_source_integrity",
                    "The converter source hash changed while it was staged.",
                    fallback_allowed=False,
                )
            source_path.chmod(0o444)
            payload = _request_payload(request, source_path.name)
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _REQUEST_LIMIT:
                raise ConversionError(
                    "converter_request_limit",
                    "The converter request exceeds the transport limit.",
                    fallback_allowed=False,
                )
            asset_transport = ((request.limits.max_asset_bytes + 2) // 3) * 4
            metadata_transport = min(
                request.limits.max_nodes * min(request.limits.max_string_bytes, 4096),
                16 * 1024 * 1024,
            )
            output_limit = (
                request.limits.max_output_bytes
                + asset_transport
                + metadata_transport
                + 1024 * 1024
            )
            response = _run_bounded(
                self._command,
                cwd=scratch,
                stdin=encoded,
                stdin_limit=_REQUEST_LIMIT,
                stdout_limit=output_limit,
                timeout_seconds=request.limits.timeout_seconds,
            )
        return conversion_result_from_payload(response, limits=request.limits)


def _request_payload(request: ConversionRequest, source_name: str) -> dict[str, Any]:
    return {
        "protocol": CONVERTER_SPI_VERSION,
        "source_path": source_name,
        "source_sha256": request.source_sha256,
        "byte_size": request.byte_size,
        "format_id": request.format_id,
        "media_type": request.media_type,
        "original_filename": request.original_filename,
        "limits": asdict(request.limits),
        "options": dict(request.options),
        "trace_id": request.trace_id,
    }


def conversion_result_to_payload(result: ConversionResult) -> dict[str, Any]:
    return {
        "protocol": CONVERTER_SPI_VERSION,
        "markdown": result.markdown,
        "assets": [
            {
                "role": asset.role,
                "media_type": asset.media_type,
                "content_base64": base64.b64encode(asset.content).decode("ascii"),
                "filename": asset.filename,
                "ordinal": asset.ordinal,
            }
            for asset in result.assets
        ],
        "metadata": dict(result.metadata),
        "locations": [
            {
                "kind": location.kind,
                "ordinal": location.ordinal,
                "label": location.label,
                "data": dict(location.data),
            }
            for location in result.locations
        ],
        "warnings": [
            {
                "code": warning.code,
                "message": warning.message,
                "location": (
                    {
                        "kind": warning.location.kind,
                        "ordinal": warning.location.ordinal,
                        "label": warning.location.label,
                        "data": dict(warning.location.data),
                    }
                    if warning.location is not None
                    else None
                ),
            }
            for warning in result.warnings
        ],
        "completeness": result.completeness,
        "quality_score": (
            str(result.quality_score) if result.quality_score is not None else None
        ),
    }


def conversion_result_from_payload(
    payload: dict[str, Any], *, limits: ConversionLimits
) -> ConversionResult:
    if not isinstance(payload, dict) or set(payload) - _RESULT_KEYS:
        raise ConversionError(
            "converter_result_invalid", "The converter returned an invalid envelope."
        )
    if payload.get("protocol") != CONVERTER_SPI_VERSION:
        raise ConversionError(
            "converter_protocol_mismatch",
            "The converter returned an incompatible protocol version.",
        )
    error = payload.get("error")
    if error is not None:
        if (
            not isinstance(error, dict)
            or set(error) - {"code", "message", "retryable", "fallback_allowed"}
            or any(key in payload for key in _RESULT_KEYS - {"protocol", "error"})
        ):
            raise ConversionError(
                "converter_result_invalid",
                "The converter returned an invalid error envelope.",
            )
        code = error.get("code") or "converter_failed"
        message = error.get("message") or "The converter failed."
        retryable = error.get("retryable", False)
        fallback_allowed = error.get("fallback_allowed", True)
        if (
            not isinstance(code, str)
            or not _ERROR_CODE.fullmatch(code)
            or not isinstance(message, str)
            or len(code.encode("utf-8")) > 128
            or len(message.encode("utf-8")) > min(limits.max_string_bytes, 4096)
            or type(retryable) is not bool
            or type(fallback_allowed) is not bool
        ):
            raise ConversionError(
                "converter_result_invalid",
                "The converter returned an invalid error envelope.",
            )
        raise ConversionError(
            code,
            "The converter reported a failure.",
            retryable=retryable,
            fallback_allowed=fallback_allowed,
        )
    markdown = payload.get("markdown")
    if markdown is not None and (
        not isinstance(markdown, str)
        or len(markdown.encode("utf-8")) > limits.max_output_bytes
    ):
        raise ConversionError(
            "converter_result_invalid",
            "The converter returned invalid or oversized Markdown.",
        )
    raw_assets = payload.get("assets", [])
    if not isinstance(raw_assets, list) or len(raw_assets) > limits.max_assets:
        raise ConversionError(
            "converter_result_invalid", "The converter returned invalid assets."
        )
    assets: list[ProducedAsset] = []
    total_assets = 0
    try:
        for raw_asset in raw_assets:
            if not isinstance(raw_asset, dict) or set(raw_asset) - {
                "role",
                "media_type",
                "content_base64",
                "filename",
                "ordinal",
            }:
                raise ValueError("invalid asset envelope")
            encoded_content = raw_asset["content_base64"]
            if not isinstance(encoded_content, str):
                raise ValueError("invalid asset encoding")
            remaining_bytes = limits.max_asset_bytes - total_assets
            if len(encoded_content) > ((remaining_bytes + 2) // 3) * 4 + 4:
                raise ConversionError(
                    "converter_asset_bytes_exceeded",
                    "The converter assets exceed the configured limit.",
                )
            content = base64.b64decode(encoded_content, validate=True)
            total_assets += len(content)
            if total_assets > limits.max_asset_bytes:
                raise ConversionError(
                    "converter_asset_bytes_exceeded",
                    "The converter assets exceed the configured limit.",
                )
            role = raw_asset["role"]
            media_type = raw_asset["media_type"]
            filename = raw_asset.get("filename")
            ordinal = raw_asset.get("ordinal", 0)
            if role not in {"preview", "asset", "metadata", "location_map"}:
                raise ValueError("invalid asset role")
            if not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type):
                raise ValueError("invalid asset media type")
            if filename is not None:
                _validate_leaf_name(filename, limits=limits)
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("invalid asset ordinal")
            assets.append(
                ProducedAsset(
                    role=role,
                    media_type=media_type,
                    content=content,
                    filename=filename,
                    ordinal=ordinal,
                )
            )
        metadata = payload.get("metadata", {})
        raw_locations = payload.get("locations", [])
        raw_warnings = payload.get("warnings", [])
        if not isinstance(metadata, dict):
            raise ValueError("invalid metadata")
        if (
            not isinstance(raw_locations, list)
            or not isinstance(raw_warnings, list)
            or len(raw_locations) > limits.max_nodes
            or len(raw_warnings) > limits.max_nodes
        ):
            raise ValueError("invalid locations or warnings")
        _validate_json_tree(metadata, limits=limits)
        locations = tuple(
            _location_from_payload(item, limits=limits) for item in raw_locations
        )
        parsed_warnings: list[ConversionWarning] = []
        for item in raw_warnings:
            _validate_warning_envelope(item)
            parsed_warnings.append(
                ConversionWarning(
                    code=_required_bounded_string(item, "code", limits=limits, cap=128),
                    message=_required_bounded_string(
                        item, "message", limits=limits, cap=4096
                    ),
                    location=(
                        _location_from_payload(item["location"], limits=limits)
                        if item.get("location") is not None
                        else None
                    ),
                )
            )
        score = payload.get("quality_score")
        quality_score = Decimal(str(score)) if score is not None else None
        if quality_score is not None and (
            not quality_score.is_finite() or quality_score < 0 or quality_score > 1
        ):
            raise ValueError("invalid quality score")
        completeness = payload.get("completeness", "complete")
        if completeness not in {"complete", "truncated", "metadata_only"}:
            raise ValueError("invalid completeness")
        return ConversionResult(
            markdown=markdown,
            assets=tuple(assets),
            metadata=metadata,
            locations=locations,
            warnings=tuple(parsed_warnings),
            completeness=completeness,
            quality_score=quality_score,
        )
    except ConversionError:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ConversionError(
            "converter_result_invalid", "The converter returned an invalid result."
        ) from exc


def _location_from_payload(
    payload: dict[str, Any], *, limits: ConversionLimits
) -> SourceLocation:
    if not isinstance(payload, dict) or set(payload) - {
        "kind",
        "ordinal",
        "label",
        "data",
    }:
        raise ValueError("invalid location envelope")
    kind = _required_bounded_string(payload, "kind", limits=limits, cap=128)
    ordinal = payload.get("ordinal")
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("invalid location ordinal")
    label = payload.get("label")
    if label is not None:
        _validate_bounded_string(label, limits=limits, cap=4096)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("invalid location data")
    _validate_json_tree(data, limits=limits)
    return SourceLocation(
        kind=kind,
        ordinal=ordinal,
        label=label,
        data=data,
    )


def _run_bounded(
    command: tuple[str, ...],
    *,
    cwd: Path,
    stdin: bytes,
    stdin_limit: int = _REQUEST_LIMIT,
    stdout_limit: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if len(stdin) > stdin_limit:
        raise ConversionError(
            "converter_request_limit",
            "The converter request exceeds the transport limit.",
            fallback_allowed=False,
        )
    environment = {
        key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST
    }
    environment["TEMP"] = str(cwd)
    environment["TMP"] = str(cwd)
    # Defense in depth for third-party commands that still use text streams.
    # Protocol correctness does not depend on these values: bundled hosts and
    # this parent exchange explicitly encoded UTF-8 bytes.
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        raise ConversionError(
            "converter_process_unavailable",
            "The converter process could not be started.",
            retryable=False,
            fallback_allowed=True,
        ) from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            stdout_future = pool.submit(_read_limited, process.stdout, stdout_limit)
            stderr_future = pool.submit(_read_limited, process.stderr, _STDERR_LIMIT)
            stdin_future = pool.submit(_write_input, process.stdin, stdin)
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise ConversionError(
                    "converter_timed_out",
                    "The converter exceeded its time limit.",
                    # An interrupted process has no trustworthy completion.
                    retryable=False,
                    fallback_allowed=False,
                ) from exc
            try:
                stdin_future.result()
            except (BrokenPipeError, OSError, ValueError):
                pass
            stdout, stdout_exceeded = stdout_future.result()
            _stderr, stderr_exceeded = stderr_future.result()
        except BaseException:
            # Kill before leaving the pool context, whose exit waits for the
            # pipe readers. Otherwise a shutdown signal can hang indefinitely.
            _terminate_process(process)
            raise
    if stdout_exceeded:
        raise ConversionError(
            "converter_transport_output_limit",
            "The converter response exceeds the transport limit.",
        )
    if return_code != 0:
        raise ConversionError(
            "converter_process_failed",
            "The converter process failed.",
            retryable=False,
            fallback_allowed=True,
        )
    if stderr_exceeded:
        raise ConversionError(
            "converter_transport_stderr_limit",
            "The converter diagnostic output exceeds the transport limit.",
        )
    try:
        decoded = stdout.decode("utf-8", errors="strict")
        parsed = json.loads(
            decoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ConversionError(
            "converter_result_invalid", "The converter returned invalid JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise ConversionError(
            "converter_result_invalid", "The converter returned an invalid result."
        )
    return parsed


def _read_limited(stream, limit: int) -> tuple[bytes, bool]:
    stored = bytearray()
    exceeded = False
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        remaining = max(0, limit + 1 - len(stored))
        if remaining:
            stored.extend(chunk[:remaining])
        if len(stored) > limit or len(chunk) > remaining:
            exceeded = True
    return bytes(stored[:limit]), exceeded


def _write_input(stream, content: bytes) -> None:
    try:
        stream.write(content)
        stream.flush()
    finally:
        stream.close()


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        else:
            system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
            taskkill = (
                Path(system_root) / "System32" / "taskkill.exe" if system_root else None
            )
            if taskkill is not None and taskkill.is_file():
                try:
                    subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _validate_request(request: ConversionRequest) -> None:
    if (
        type(request.byte_size) is not int
        or request.byte_size < 0
        or request.byte_size > request.limits.max_input_bytes
        or request.source.byte_size != request.byte_size
        or not isinstance(request.source_sha256, str)
        or not _SHA256.fullmatch(request.source_sha256)
    ):
        raise ConversionError(
            "converter_source_integrity",
            "The converter source metadata is invalid.",
            fallback_allowed=False,
        )
    try:
        _validate_leaf_name(request.original_filename, limits=request.limits)
        for value in (request.format_id, request.media_type, request.trace_id):
            _validate_bounded_string(value, limits=request.limits, cap=4096)
        if not request.format_id:
            raise ValueError("format id is empty")
        _validate_json_tree(dict(request.options), limits=request.limits)
    except (TypeError, ValueError) as exc:
        raise ConversionError(
            "converter_request_invalid",
            "The converter request metadata is invalid.",
            fallback_allowed=False,
        ) from exc
    if not _MEDIA_TYPE.fullmatch(request.media_type):
        raise ConversionError(
            "converter_request_invalid",
            "The converter media type is invalid.",
            fallback_allowed=False,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_leaf_name(value: Any, *, limits: ConversionLimits) -> None:
    _validate_bounded_string(value, limits=limits, cap=4096)
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("filename must be a leaf name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("filename contains a control character")


def _validate_bounded_string(value: Any, *, limits: ConversionLimits, cap: int) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > min(
        limits.max_string_bytes, cap
    ):
        raise ValueError("invalid or oversized string")


def _required_bounded_string(
    payload: dict[str, Any],
    key: str,
    *,
    limits: ConversionLimits,
    cap: int,
) -> str:
    value = payload.get(key)
    _validate_bounded_string(value, limits=limits, cap=cap)
    return value


def _validate_warning_envelope(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) - {
        "code",
        "message",
        "location",
    }:
        raise ValueError("invalid warning envelope")
    return True


def _validate_json_tree(value: Any, *, limits: ConversionLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise ValueError("JSON structure exceeds configured limits")
        if current is None or type(current) in {bool, int}:
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("JSON number is not finite")
            continue
        if isinstance(current, str):
            _validate_bounded_string(
                current, limits=limits, cap=limits.max_string_bytes
            )
            continue
        if isinstance(current, dict):
            for key, child in current.items():
                _validate_bounded_string(
                    key, limits=limits, cap=limits.max_string_bytes
                )
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        raise ValueError("value is not JSON-compatible")


__all__ = [
    "ExternalProcessConverter",
    "conversion_result_from_payload",
    "conversion_result_to_payload",
]
