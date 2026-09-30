"""Bounded JPEG/PNG Preview generation using the bundled Pillow dependency."""

from __future__ import annotations

import hashlib
import io
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from tempfile import SpooledTemporaryFile
from threading import RLock
from typing import Any, BinaryIO

from backend.spi.preview import (
    PREVIEW_PROVIDER_SPI_VERSION,
    PreviewOutputSink,
    PreviewProbeRequest,
    PreviewProbeResult,
    PreviewProviderDescriptor,
    PreviewProviderError,
    PreviewProviderPlugin,
    PreviewRequest,
    PreviewResult,
    UnsupportedPreviewError,
)

_PROVIDER_ID = "dtxwiki.pillow-image-preview"
_PINNED_PILLOW_VERSION = "12.3.0"
# SHA-256 of the canonical JSON form of Pillow's complete uv.lock package
# record, including every platform wheel/sdist URL and digest. This is lock
# provenance, not a claim that the currently installed wheel has this digest.
_PILLOW_LOCK_ENTRY_SHA256 = (
    "e46acdb351549097fe6bf9c30a4cda5313aa8b71f1cdb97e2e92398df5015868"
)
_MAX_INPUT_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_BYTES = 24 * 1024 * 1024
_MAX_DECODED_PIXELS = 40_000_000
_MAX_IMAGE_DIMENSION = 32_768
_MAX_IMAGE_FRAMES = 128
_MAX_PREVIEW_EDGE = 2_048
_COPY_CHUNK_BYTES = 64 * 1024
_MEMORY_SPOOL_BYTES = 8 * 1024 * 1024

_FORMAT_MEDIA_TYPES = {
    "jpeg": ("JPEG", "image/jpeg"),
    "png": ("PNG", "image/png"),
}

# Pillow exposes its decompression-bomb threshold as process-global state.
# Serializing every use in this provider makes the temporary stricter value and
# its restoration atomic for concurrent Preview calls.
_PILLOW_DECODE_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class _PillowRuntime:
    image: Any
    image_ops: Any
    unidentified_image_error: type[BaseException]
    decompression_bomb_error: type[BaseException]
    decompression_bomb_warning: type[Warning]


@dataclass(frozen=True, slots=True)
class _SourceFacts:
    width: int
    height: int
    frame_count: int
    pillow_format: str


class _ImageLimitExceeded(RuntimeError):
    pass


class _OutputLimitExceeded(OSError):
    pass


class _BoundedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, content: bytes) -> int:
        if type(content) is not bytes or self.tell() + len(content) > self._limit:
            raise _OutputLimitExceeded
        return super().write(content)


class PillowImagePreviewProvider:
    """Re-encode admitted JPEG/PNG inputs into metadata-free PNG variants."""

    def descriptor(self) -> PreviewProviderDescriptor:
        return PreviewProviderDescriptor(
            provider_id=_PROVIDER_ID,
            provider_version=_PINNED_PILLOW_VERSION,
            spi_version=PREVIEW_PROVIDER_SPI_VERSION,
            formats=frozenset(_FORMAT_MEDIA_TYPES),
            media_types=frozenset(
                media_type for _, media_type in _FORMAT_MEDIA_TYPES.values()
            ),
            input_kinds=frozenset({"source"}),
            output_renderer_contracts=frozenset({"core.image.v1"}),
            output_media_types=frozenset({"image/png"}),
            priority=1_000,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="HPND",
            source_url="https://github.com/python-pillow/Pillow",
            distribution_digest=_bundled_distribution_digest(),
            max_input_bytes=_MAX_INPUT_BYTES,
        )

    def health(self) -> bool:
        """Check pinned installation metadata without loading decoder code.

        Native Pillow modules are imported and API-checked only inside the
        killable child host during ``generate``. This HTTP-safe readiness check
        intentionally avoids executing decoder/native initialization.
        """

        return _pillow_installation_available()

    def probe(self, request: PreviewProbeRequest) -> PreviewProbeResult:
        expected = _FORMAT_MEDIA_TYPES.get(request.format_id)
        if request.input_kind != "source" or expected is None:
            return PreviewProbeResult(
                supported=False,
                confidence=Decimal("1.000"),
                reason_code="unsupported",
            )
        if request.media_type != expected[1]:
            return PreviewProbeResult(
                supported=False,
                confidence=Decimal("1.000"),
                reason_code="media_type_mismatch",
            )
        if request.byte_size <= 0 or request.byte_size > _MAX_INPUT_BYTES:
            return PreviewProbeResult(
                supported=False,
                confidence=Decimal("1.000"),
                reason_code="input_limit",
            )
        return PreviewProbeResult(supported=True, confidence=Decimal("1.000"))

    def generate(
        self,
        request: PreviewRequest,
        sink: PreviewOutputSink,
    ) -> PreviewResult:
        expected = _FORMAT_MEDIA_TYPES.get(request.format_id)
        if (
            request.input_kind != "source"
            or expected is None
            or request.media_type != expected[1]
        ):
            raise UnsupportedPreviewError(
                "The bundled image Preview supports matching JPEG and PNG sources."
            )
        if request.byte_size > _MAX_INPUT_BYTES:
            raise PreviewProviderError(
                "preview_input_limit",
                "The image exceeds the bundled Preview input limit.",
                fallback_allowed=False,
            )
        try:
            runtime = _load_pillow()
        except Exception as exc:
            raise PreviewProviderError(
                "provider_dependency_missing",
                "The bundled image Preview dependency is unavailable.",
                fallback_allowed=False,
            ) from exc

        source = _copy_and_verify_source(request)
        try:
            pixel_budget = min(request.limits.max_pixels, _MAX_DECODED_PIXELS)
            with _bounded_pillow_decode(runtime, pixel_budget):
                facts = _verify_image(
                    runtime,
                    source,
                    expected_format=expected[0],
                    pixel_budget=pixel_budget,
                )
                output, output_width, output_height = _decode_derivative(
                    runtime,
                    source,
                    expected_format=expected[0],
                    output_limit=min(
                        request.limits.max_variant_bytes,
                        request.limits.max_total_output_bytes,
                        _MAX_OUTPUT_BYTES,
                    ),
                )
        except (
            _ImageLimitExceeded,
            runtime.decompression_bomb_error,
            runtime.decompression_bomb_warning,
            MemoryError,
        ) as exc:
            raise PreviewProviderError(
                "image_decode_limit",
                "The image exceeds the bundled Preview decode limits.",
                fallback_allowed=False,
            ) from exc
        except _OutputLimitExceeded as exc:
            raise PreviewProviderError(
                "preview_output_limit",
                "The re-encoded image exceeds the Preview output limit.",
                fallback_allowed=False,
            ) from exc
        except (
            runtime.unidentified_image_error,
            EOFError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise PreviewProviderError(
                "invalid_image",
                "The image is corrupt, truncated, or unsafe to decode.",
                fallback_allowed=False,
            ) from exc
        finally:
            source.close()

        output_digest = hashlib.sha256(output).hexdigest()
        receipt = sink.write_variant(
            source=io.BytesIO(output),
            expected_sha256=output_digest,
            expected_byte_size=len(output),
            kind="image",
            media_type="image/png",
            renderer_contract="core.image.v1",
            delivery="inline",
            rank=0,
            ordinal=0,
            filename="preview.png",
            width=output_width,
            height=output_height,
            metadata={
                "source_width": facts.width,
                "source_height": facts.height,
                "source_frame_count": facts.frame_count,
                "source_format": facts.pillow_format,
                "first_frame_only": facts.frame_count > 1,
                "metadata_stripped": True,
            },
        )
        return PreviewResult(
            variants=(receipt,),
            completeness="partial" if facts.frame_count > 1 else "complete",
            metadata={
                "source_frame_count": facts.frame_count,
                "metadata_stripped": True,
            },
        )


def _copy_and_verify_source(request: PreviewRequest) -> BinaryIO:
    copied = SpooledTemporaryFile(
        max_size=min(request.byte_size, _MEMORY_SPOOL_BYTES),
        mode="w+b",
    )
    digest = hashlib.sha256()
    remaining = request.byte_size
    try:
        source = request.source.open_binary()
    except Exception as exc:
        copied.close()
        raise PreviewProviderError(
            "invalid_image_source",
            "The image source failed bounded integrity validation.",
            fallback_allowed=False,
        ) from exc
    try:
        while remaining:
            requested = min(remaining, _COPY_CHUNK_BYTES)
            chunk = source.read(requested)
            if type(chunk) is not bytes or not chunk or len(chunk) > requested:
                raise PreviewProviderError(
                    "invalid_image_source",
                    "The image source failed bounded integrity validation.",
                    fallback_allowed=False,
                )
            copied.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        trailing = source.read(1)
        if type(trailing) is not bytes or trailing:
            raise PreviewProviderError(
                "invalid_image_source",
                "The image source failed bounded integrity validation.",
                fallback_allowed=False,
            )
    except PreviewProviderError:
        copied.close()
        raise
    except (EOFError, OSError, TypeError, ValueError) as exc:
        copied.close()
        raise PreviewProviderError(
            "invalid_image_source",
            "The image source failed bounded integrity validation.",
            fallback_allowed=False,
        ) from exc
    finally:
        source.close()
    if digest.hexdigest() != request.input_sha256:
        copied.close()
        raise PreviewProviderError(
            "invalid_image_source",
            "The image source failed bounded integrity validation.",
            fallback_allowed=False,
        )
    copied.seek(0)
    return copied


@contextmanager
def _bounded_pillow_decode(
    runtime: _PillowRuntime,
    pixel_budget: int,
) -> Iterator[None]:
    with _PILLOW_DECODE_LOCK:
        previous_limit = runtime.image.MAX_IMAGE_PIXELS
        runtime.image.MAX_IMAGE_PIXELS = pixel_budget
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", runtime.decompression_bomb_warning)
                yield
        finally:
            runtime.image.MAX_IMAGE_PIXELS = previous_limit


def _verify_image(
    runtime: _PillowRuntime,
    source: BinaryIO,
    *,
    expected_format: str,
    pixel_budget: int,
) -> _SourceFacts:
    source.seek(0)
    with runtime.image.open(source, formats=[expected_format]) as image:
        width, height = image.size
        frame_count = int(getattr(image, "n_frames", 1))
        pillow_format = image.format
        _validate_source_shape(
            width=width,
            height=height,
            frame_count=frame_count,
            pixel_budget=pixel_budget,
        )
        if pillow_format != expected_format:
            raise ValueError("decoded image format did not match the admitted format")
        image.verify()
    return _SourceFacts(
        width=width,
        height=height,
        frame_count=frame_count,
        pillow_format=pillow_format,
    )


def _validate_source_shape(
    *,
    width: int,
    height: int,
    frame_count: int,
    pixel_budget: int,
) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or type(frame_count) is not int
        or width <= 0
        or height <= 0
        or frame_count <= 0
        or width > _MAX_IMAGE_DIMENSION
        or height > _MAX_IMAGE_DIMENSION
        or frame_count > _MAX_IMAGE_FRAMES
        or width * height > pixel_budget
        or width * height * frame_count > pixel_budget
    ):
        raise _ImageLimitExceeded


def _decode_derivative(
    runtime: _PillowRuntime,
    source: BinaryIO,
    *,
    expected_format: str,
    output_limit: int,
) -> tuple[bytes, int, int]:
    if output_limit <= 0:
        raise _OutputLimitExceeded
    source.seek(0)
    with runtime.image.open(source, formats=[expected_format]) as image:
        image.seek(0)
        image.load()
        image.thumbnail(
            (_MAX_PREVIEW_EDGE, _MAX_PREVIEW_EDGE),
            resample=runtime.image.Resampling.LANCZOS,
            reducing_gap=2.0,
        )
        oriented = runtime.image_ops.exif_transpose(image)
        try:
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            converted = oriented.convert("RGBA" if has_alpha else "RGB")
            try:
                converted.load()
                derivative = runtime.image.new(converted.mode, converted.size)
                try:
                    derivative.paste(converted)
                    output = _BoundedBytesIO(output_limit)
                    try:
                        derivative.save(
                            output,
                            format="PNG",
                            optimize=False,
                            compress_level=9,
                        )
                        content = output.getvalue()
                        return content, derivative.width, derivative.height
                    finally:
                        output.close()
                finally:
                    derivative.close()
            finally:
                converted.close()
        finally:
            if oriented is not image:
                oriented.close()


def _load_pillow() -> _PillowRuntime:
    from PIL import Image, ImageOps, UnidentifiedImageError
    from PIL.Image import DecompressionBombError, DecompressionBombWarning

    return _PillowRuntime(
        image=Image,
        image_ops=ImageOps,
        unidentified_image_error=UnidentifiedImageError,
        decompression_bomb_error=DecompressionBombError,
        decompression_bomb_warning=DecompressionBombWarning,
    )


def _pillow_version() -> str:
    try:
        return version("Pillow")
    except PackageNotFoundError:
        return "not-installed"


def _pillow_installation_available() -> bool:
    try:
        return (
            _pillow_version() == _PINNED_PILLOW_VERSION
            and find_spec("PIL") is not None
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


@lru_cache(maxsize=1)
def _bundled_distribution_digest() -> str:
    """Hash the exact first-party execution closure and pinned lock record.

    This deliberately does not pretend to be a wheel signature or SBOM. The
    digest proves the checked-in provider/host/protocol bytes plus the exact
    Pillow uv.lock package record identified above; ``health`` separately
    fails closed unless the installed Pillow version matches that record.
    """

    backend_root = Path(__file__).resolve().parents[1]
    paths = {
        Path(__file__).resolve(),
        (backend_root / "preview_providers" / "__init__.py").resolve(),
        (backend_root / "services" / "bounded_subprocess.py").resolve(),
        (backend_root / "services" / "preview_contracts.py").resolve(),
        (backend_root / "services" / "preview_execution.py").resolve(),
        (backend_root / "services" / "preview_provider_registry.py").resolve(),
        (backend_root / "spi" / "preview.py").resolve(),
        (backend_root / "worker" / "preview_host.py").resolve(),
    }
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.as_posix()):
        relative = path.relative_to(backend_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    for label, value in (
        ("pillow-lock-version", _PINNED_PILLOW_VERSION),
        ("pillow-lock-entry-sha256", _PILLOW_LOCK_ENTRY_SHA256),
    ):
        encoded_label = label.encode("ascii")
        encoded_value = value.encode("ascii")
        digest.update(len(encoded_label).to_bytes(4, "big"))
        digest.update(encoded_label)
        digest.update(len(encoded_value).to_bytes(4, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


def build_pillow_image_preview_providers() -> tuple[PreviewProviderPlugin, ...]:
    """Return this module's trusted provider contribution."""

    return (PillowImagePreviewProvider(),)


__all__ = [
    "PillowImagePreviewProvider",
    "build_pillow_image_preview_providers",
]
