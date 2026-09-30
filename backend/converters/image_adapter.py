"""Optional Pillow image metadata and safe preview derivative converter."""

from __future__ import annotations

import io
import math
import warnings as warnings_module
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version

from backend.converters.builtin_text import _bounded_text
from backend.spi.conversion import (
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionRequest,
    ConversionResult,
    ConversionWarning,
    ConverterDescriptor,
    ProbeRequest,
    ProbeResult,
    ProducedAsset,
    SourceLocation,
)

_PREVIEW_MAX_EDGE = 2048
_PILLOW_FORMATS = {
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
    "webp": "WEBP",
    "tiff": "TIFF",
}


class _AssetLimitExceeded(OSError):
    pass


class _BoundedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, content: bytes) -> int:
        if self.tell() + len(content) > self._limit:
            raise _AssetLimitExceeded
        return super().write(content)


class ImageMetadataConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.pillow-image",
            converter_version=_package_version("Pillow"),
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"jpeg", "png", "gif", "webp", "tiff"}),
            media_types=frozenset(
                {"image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff"}
            ),
            priority=100,
            execution_mode="subprocess",
            network_required=False,
            deterministic=True,
            license_expression="HPND",
            source_url="https://github.com/python-pillow/Pillow",
            capabilities=frozenset({"markdown", "assets", "locations"}),
            distribution_digest="installed-environment-sbom",
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id in self.descriptor().formats)

    def convert(self, request: ConversionRequest) -> ConversionResult:
        try:
            from PIL import Image, UnidentifiedImageError
            from PIL.Image import DecompressionBombError, DecompressionBombWarning
        except ImportError as exc:
            raise ConversionError(
                "converter_dependency_missing", "The image converter is not installed."
            ) from exc

        expected_format = _PILLOW_FORMATS.get(request.format_id)
        if expected_format is None:
            raise ConversionError("unsupported", "Unsupported image format.")
        previous_pixel_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = min(
            request.limits.max_image_pixels,
            request.limits.max_rendered_pixels,
        )
        try:
            with warnings_module.catch_warnings():
                warnings_module.simplefilter("error", DecompressionBombWarning)
                with request.source.open_binary() as stream:
                    with Image.open(stream, formats=[expected_format]) as inspected:
                        width, height = inspected.size
                        frame_count = int(getattr(inspected, "n_frames", 1))
                        original_mode = inspected.mode
                        original_format = inspected.format
                        exif_present = bool(inspected.getexif())
                        inspected.verify()
            if width <= 0 or height <= 0:
                raise ConversionError(
                    "invalid_image", "The image dimensions are invalid."
                )
            if (
                width > request.limits.max_image_width
                or height > request.limits.max_image_height
                or frame_count <= 0
                or frame_count > request.limits.max_image_frames
                or width * height > request.limits.max_image_pixels
                or width * height * frame_count > request.limits.max_rendered_pixels
            ):
                raise ConversionError(
                    "image_decode_limit",
                    "The decoded image exceeds the pixel or frame limit.",
                    fallback_allowed=False,
                )
            with request.source.open_binary() as stream:
                with Image.open(stream, formats=[expected_format]) as image:
                    image.seek(0)
                    preview_edge = min(
                        _PREVIEW_MAX_EDGE,
                        request.limits.max_image_width,
                        request.limits.max_image_height,
                        max(1, math.isqrt(request.limits.max_rendered_pixels)),
                    )
                    image.thumbnail((preview_edge, preview_edge))
                    converted = image.convert(
                        "RGBA" if "A" in image.getbands() else "RGB"
                    )
                    converted.load()
                    derivative = Image.new(converted.mode, converted.size)
                    derivative.paste(converted)
                    output = _BoundedBytesIO(request.limits.max_asset_bytes)
                    derivative.save(output, format="PNG", optimize=True)
                    preview = output.getvalue()
        except ConversionError:
            raise
        except _AssetLimitExceeded as exc:
            raise ConversionError(
                "converter_asset_bytes_exceeded",
                "The safe image preview exceeds the asset limit.",
            ) from exc
        except (
            DecompressionBombError,
            DecompressionBombWarning,
            OSError,
            ValueError,
            UnidentifiedImageError,
        ) as exc:
            raise ConversionError(
                "invalid_image", "The image is corrupt or unsupported."
            ) from exc
        finally:
            Image.MAX_IMAGE_PIXELS = previous_pixel_limit

        conversion_warnings = list(
            (
                ConversionWarning(
                    code="first_frame_only",
                    message="Only the first frame is included in the safe preview.",
                ),
            )
            if frame_count > 1
            else ()
        )
        markdown = (
            f"# {_markdown_text(request.original_filename)}\n\n"
            f"- Format: {original_format or request.format_id}\n"
            f"- Dimensions: {width} x {height}\n"
            f"- Frames: {frame_count}\n"
            f"- Color mode: {original_mode}\n"
        )
        markdown, markdown_truncated = _bounded_text(
            markdown, request.limits.max_output_bytes
        )
        if markdown_truncated:
            conversion_warnings.append(
                ConversionWarning(
                    code="output_truncated",
                    message="The image metadata reached the configured output limit.",
                )
            )
        return ConversionResult(
            markdown=markdown,
            assets=(
                ProducedAsset(
                    role="preview",
                    media_type="image/png",
                    content=preview,
                    filename="preview.png",
                    ordinal=0,
                ),
            ),
            metadata={
                "width": width,
                "height": height,
                "frame_count": frame_count,
                "mode": original_mode,
                "source_format": original_format,
                "exif_present": exif_present,
                "derivative_metadata_stripped": True,
            },
            locations=(SourceLocation(kind="frame", ordinal=0, label="Frame 1"),),
            warnings=tuple(conversion_warnings),
            completeness="truncated" if markdown_truncated else "complete",
            quality_score=Decimal("1.000"),
        )


def _markdown_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not-installed"


__all__ = ["ImageMetadataConverter"]
