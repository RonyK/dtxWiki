"""Fail-closed execution boundaries for Library Preview providers."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, Protocol

from backend.services.bounded_subprocess import (
    BoundedSubprocessError,
    run_bounded_subprocess,
)
from backend.spi.preview import (
    PREVIEW_OUTPUT_SCHEMA_VERSION,
    PREVIEW_PROVIDER_SPI_VERSION,
    PreviewOutputSink,
    PreviewProbeRequest,
    PreviewProviderDescriptor,
    PreviewProviderError,
    PreviewProviderPlugin,
    PreviewRequest,
    PreviewResult,
    UnsupportedPreviewError,
    thaw_json,
)

_REQUEST_LIMIT = 1024 * 1024
_RESPONSE_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_OUTPUT_PATH = re.compile(r"^variant-[0-9]{3}\.bin$")
_SUCCESS_KEYS = frozenset(
    {
        "protocol",
        "provider_id",
        "provider_version",
        "distribution_digest",
        "output_schema_version",
        "completeness",
        "metadata",
        "variants",
    }
)
_ERROR_KEYS = frozenset({"protocol", "error"})
_ERROR_VALUE_KEYS = frozenset(
    {"code", "message", "retryable", "fallback_allowed"}
)
_VARIANT_KEYS = frozenset(
    {
        "path",
        "kind",
        "media_type",
        "renderer_contract",
        "delivery",
        "byte_size",
        "sha256",
        "rank",
        "ordinal",
        "filename",
        "width",
        "height",
        "duration_ms",
        "page_count",
        "metadata",
    }
)


class PreviewExecutionBoundary(Protocol):
    """Narrow worker-side capability used by the Preview orchestrator."""

    def can_execute(self, descriptor: PreviewProviderDescriptor) -> bool: ...

    def execute(
        self,
        *,
        plugin: PreviewProviderPlugin,
        descriptor: PreviewProviderDescriptor,
        request: PreviewRequest,
        sink: PreviewOutputSink,
    ) -> PreviewResult: ...


class TrustedBuiltinPreviewExecutionBoundary:
    """In-process boundary reserved for bounded tests and trusted composition.

    This implementation only performs a post-hoc wall-clock check and cannot
    preempt native decoder code. Production composition must route every
    decoder through a killable boundary instead.
    """

    def can_execute(self, descriptor: PreviewProviderDescriptor) -> bool:
        return (
            descriptor.execution_mode == "builtin" and not descriptor.network_required
        )

    def execute(
        self,
        *,
        plugin: PreviewProviderPlugin,
        descriptor: PreviewProviderDescriptor,
        request: PreviewRequest,
        sink: PreviewOutputSink,
    ) -> PreviewResult:
        if not self.can_execute(descriptor):
            raise _executor_unavailable()
        deadline = monotonic() + float(request.limits.timeout_seconds)
        try:
            probe = plugin.probe(
                PreviewProbeRequest(
                    format_id=request.format_id,
                    media_type=request.media_type,
                    input_kind=request.input_kind,
                    byte_size=request.byte_size,
                )
            )
            if monotonic() > deadline:
                raise _timeout_error()
            if not probe.supported:
                raise UnsupportedPreviewError()
            result = plugin.generate(request, sink)
            if monotonic() > deadline:
                raise _timeout_error()
            return result
        except PreviewProviderError:
            raise
        except Exception as exc:
            raise PreviewProviderError(
                "preview_provider_failed",
                "The Preview provider failed safely.",
                fallback_allowed=True,
            ) from exc


class BundledSubprocessPreviewExecutionBoundary:
    """Execute explicitly routed bundled providers in killable child trees.

    Commands are supplied only by application composition, never by Workspace
    settings or provider metadata. The child receives one immutable staged
    input and can return only bounded files from its private scratch directory.
    """

    def __init__(
        self,
        commands: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        configured = dict(commands or _bundled_provider_commands())
        if not configured:
            raise ValueError("at least one bundled Preview command is required")
        for provider_id, command in configured.items():
            if (
                type(provider_id) is not str
                or not provider_id
                or type(command) is not tuple
                or not command
                or not Path(command[0]).is_absolute()
                or not Path(command[0]).is_file()
            ):
                raise ValueError("bundled Preview command is invalid")
        self._commands = configured

    def can_execute(self, descriptor: PreviewProviderDescriptor) -> bool:
        return (
            descriptor.execution_mode == "builtin"
            and not descriptor.network_required
            and descriptor.provider_id in self._commands
        )

    def execute(
        self,
        *,
        plugin: PreviewProviderPlugin,
        descriptor: PreviewProviderDescriptor,
        request: PreviewRequest,
        sink: PreviewOutputSink,
    ) -> PreviewResult:
        if not self.can_execute(descriptor):
            raise _executor_unavailable()
        try:
            if plugin.descriptor() != descriptor:
                raise _executor_unavailable()
        except PreviewProviderError:
            raise
        except Exception as exc:
            raise _executor_unavailable() from exc

        # The production registry does not enable this native boundary without
        # an operator-provided OS sandbox.  Still, cleanup must never mask the
        # bounded transport error if a platform cannot reap a detached child
        # that retained the private cwd after its leader exited.
        with TemporaryDirectory(
            prefix="dtxwiki-preview-", ignore_cleanup_errors=True
        ) as raw_dir:
            scratch = Path(raw_dir).resolve()
            source_path = scratch / "source.bin"
            _stage_source(request, source_path)
            payload = _request_payload(request, descriptor=descriptor)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                response = run_bounded_subprocess(
                    self._commands[descriptor.provider_id],
                    cwd=scratch,
                    stdin=encoded,
                    stdin_limit=_REQUEST_LIMIT,
                    stdout_limit=_RESPONSE_LIMIT,
                    stderr_limit=_STDERR_LIMIT,
                    timeout_seconds=request.limits.timeout_seconds,
                )
            except BoundedSubprocessError as exc:
                raise _transport_error(exc) from exc
            return _result_from_response(
                response.stdout,
                scratch=scratch,
                descriptor=descriptor,
                request=request,
                sink=sink,
            )


def _bundled_provider_commands() -> dict[str, tuple[str, ...]]:
    host = Path(__file__).resolve().parents[1] / "worker" / "preview_host.py"
    executable = Path(sys.executable)
    if not executable.is_absolute():
        executable = executable.absolute()
    if getattr(sys, "frozen", False):
        return {
            "dtxwiki.pillow-image-preview": (
                str(executable),
                "preview-host",
                "--provider",
                "dtxwiki.pillow-image-preview",
            )
        }
    return {
        "dtxwiki.pillow-image-preview": (
            str(executable),
            "-I",
            "-s",
            str(host.resolve(strict=True)),
            "--provider",
            "dtxwiki.pillow-image-preview",
        )
    }


def _stage_source(request: PreviewRequest, destination: Path) -> None:
    try:
        copied = request.source.copy_to(destination)
        if (
            type(copied) is not int
            or copied != request.byte_size
            or destination.is_symlink()
            or not destination.is_file()
            or destination.stat().st_size != request.byte_size
            or _sha256_file(destination) != request.input_sha256
        ):
            raise ValueError("source changed")
        destination.chmod(0o444)
    except Exception as exc:
        raise PreviewProviderError(
            "preview_source_integrity",
            "The Preview source failed bounded integrity validation.",
            fallback_allowed=False,
        ) from exc


def _request_payload(
    request: PreviewRequest,
    *,
    descriptor: PreviewProviderDescriptor,
) -> dict[str, Any]:
    return {
        "protocol": PREVIEW_PROVIDER_SPI_VERSION,
        "provider_id": descriptor.provider_id,
        "provider_version": descriptor.provider_version,
        "distribution_digest": descriptor.distribution_digest,
        "source_path": "source.bin",
        "workspace_id": str(request.workspace_id),
        "library_item_id": str(request.library_item_id),
        "version_id": str(request.version_id),
        "input_kind": request.input_kind,
        "input_conversion_attempt_id": (
            str(request.input_conversion_attempt_id)
            if request.input_conversion_attempt_id is not None
            else None
        ),
        "input_artifact_id": (
            str(request.input_artifact_id)
            if request.input_artifact_id is not None
            else None
        ),
        "input_sha256": request.input_sha256,
        "byte_size": request.byte_size,
        "format_id": request.format_id,
        "media_type": request.media_type,
        "original_filename": request.original_filename,
        "limits": asdict(request.limits),
        "recipe": thaw_json(request.recipe),
        "trace_id": request.trace_id,
    }


def _result_from_response(
    raw: bytes,
    *,
    scratch: Path,
    descriptor: PreviewProviderDescriptor,
    request: PreviewRequest,
    sink: PreviewOutputSink,
) -> PreviewResult:
    try:
        payload = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _invalid_result() from exc
    if not isinstance(payload, dict) or payload.get(
        "protocol"
    ) != PREVIEW_PROVIDER_SPI_VERSION:
        raise _invalid_result()
    if set(payload) == _ERROR_KEYS:
        raise _provider_reported_error(payload.get("error"))
    if (
        set(payload) != _SUCCESS_KEYS
        or payload.get("provider_id") != descriptor.provider_id
        or payload.get("provider_version") != descriptor.provider_version
        or payload.get("distribution_digest") != descriptor.distribution_digest
        or payload.get("output_schema_version") != PREVIEW_OUTPUT_SCHEMA_VERSION
        or payload.get("completeness") not in {"complete", "partial"}
        or not isinstance(payload.get("metadata"), dict)
        or not isinstance(payload.get("variants"), list)
    ):
        raise _invalid_result()
    variants = payload["variants"]
    if not 0 < len(variants) <= request.limits.max_variants:
        raise _invalid_result()

    receipts = []
    relative_paths: set[str] = set()
    declared_total = 0
    try:
        for variant in variants:
            if not isinstance(variant, dict) or set(variant) != _VARIANT_KEYS:
                raise ValueError("variant envelope")
            relative_path = variant.get("path")
            byte_size = variant.get("byte_size")
            if (
                type(relative_path) is not str
                or not _OUTPUT_PATH.fullmatch(relative_path)
                or relative_path in relative_paths
                or type(byte_size) is not int
                or not 0 < byte_size <= request.limits.max_variant_bytes
            ):
                raise ValueError("variant path or size")
            output_path = (scratch / relative_path).resolve()
            if (
                output_path.parent != scratch
                or output_path.is_symlink()
                or not output_path.is_file()
                or output_path.stat().st_size != byte_size
            ):
                raise ValueError("variant file")
            declared_total += byte_size
            if declared_total > request.limits.max_total_output_bytes:
                raise ValueError("variant total")
            relative_paths.add(relative_path)
            metadata = variant.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError("variant metadata")
            with output_path.open("rb") as source:
                receipts.append(
                    sink.write_variant(
                        source=source,
                        expected_sha256=variant.get("sha256"),
                        expected_byte_size=byte_size,
                        kind=variant.get("kind"),
                        media_type=variant.get("media_type"),
                        renderer_contract=variant.get("renderer_contract"),
                        delivery=variant.get("delivery"),
                        rank=variant.get("rank"),
                        ordinal=variant.get("ordinal"),
                        filename=variant.get("filename"),
                        width=variant.get("width"),
                        height=variant.get("height"),
                        duration_ms=variant.get("duration_ms"),
                        page_count=variant.get("page_count"),
                        metadata=metadata,
                    )
                )
        return PreviewResult(
            variants=tuple(receipts),
            completeness=payload["completeness"],
            metadata=payload["metadata"],
        )
    except PreviewProviderError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _invalid_result() from exc


def _provider_reported_error(value: object) -> PreviewProviderError:
    if not isinstance(value, dict) or set(value) != _ERROR_VALUE_KEYS:
        return _invalid_result()
    try:
        return PreviewProviderError(
            value.get("code"),
            "The Preview provider reported a failure.",
            retryable=value.get("retryable"),
            fallback_allowed=value.get("fallback_allowed"),
        )
    except (TypeError, ValueError):
        return _invalid_result()


def _transport_error(error: BoundedSubprocessError) -> PreviewProviderError:
    if error.reason == "timed_out":
        return _timeout_error()
    if error.reason == "input_limit":
        return PreviewProviderError(
            "preview_request_limit",
            "The Preview provider request exceeded its transport limit.",
            fallback_allowed=False,
        )
    if error.reason == "start_failed":
        return _executor_unavailable()
    return PreviewProviderError(
        "preview_provider_failed",
        "The Preview provider process failed safely.",
        fallback_allowed=True,
    )


def _executor_unavailable() -> PreviewProviderError:
    return PreviewProviderError(
        "preview_executor_unavailable",
        "The Preview provider has no approved execution boundary.",
        fallback_allowed=False,
    )


def _timeout_error() -> PreviewProviderError:
    return PreviewProviderError(
        "preview_timeout",
        "The Preview provider exceeded its execution deadline.",
        retryable=True,
        fallback_allowed=True,
    )


def _invalid_result() -> PreviewProviderError:
    return PreviewProviderError(
        "preview_output_invalid",
        "The Preview provider returned an invalid bounded result.",
        fallback_allowed=True,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "BundledSubprocessPreviewExecutionBoundary",
    "PreviewExecutionBoundary",
    "TrustedBuiltinPreviewExecutionBoundary",
]
