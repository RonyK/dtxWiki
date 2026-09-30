"""Exact-container and full-decoder admission for Context images."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from backend.context_media.domain.image_formats import (
    ContextImageFormat,
    context_image_format_for_media_type,
)
from backend.context_media.domain.policy import (
    MAX_CONTEXT_IMAGE_DIMENSION,
    MAX_CONTEXT_IMAGE_FRAMES,
    MAX_CONTEXT_IMAGE_PIXELS,
    MAX_CONTEXT_IMAGE_TOTAL_PIXELS,
)


class ContextImageValidationError(ValueError):
    error_code = "media_content_mismatch"

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ValidatedContextImage:
    canonical_media_type: str
    width: int
    height: int
    frame_count: int
    total_pixels: int


def validate_context_image(
    content: bytes,
    *,
    declared_media_type: str,
) -> ValidatedContextImage:
    """Validate an exact image container and fully decode every admitted frame."""

    image_format = context_image_format_for_media_type(declared_media_type)
    if image_format is None:
        raise ContextImageValidationError(
            "Only PNG, JPEG, GIF, and WebP Context media are supported.",
            reason_code="media_type_unsupported",
        )
    _reject_invalid_container_boundary(content, image_format=image_format)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as candidate:
                if candidate.format != image_format.pillow_format:
                    raise ContextImageValidationError(
                        "Uploaded bytes do not match the declared media type.",
                        reason_code="declared_type_mismatch",
                    )
                width, height = candidate.size
                _validate_geometry(width, height)
                frame_count = int(getattr(candidate, "n_frames", 1))
                if frame_count <= 0 or frame_count > MAX_CONTEXT_IMAGE_FRAMES:
                    raise ContextImageValidationError(
                        "Image frame count exceeds the supported limit.",
                        reason_code="frame_count_exceeded",
                    )
                if frame_count != 1 and not image_format.multi_frame_allowed:
                    raise ContextImageValidationError(
                        (
                            f"Animated {image_format.pillow_format} images are not "
                            "supported."
                        ),
                        reason_code="frame_count_exceeded",
                    )
                candidate.verify()

            # ``verify`` checks the container without necessarily decoding all
            # pixels. Reopen, seek, and load every frame so truncated entropy or
            # later animation frames cannot pass admission.
            total_pixels = 0
            with Image.open(BytesIO(content)) as decoded:
                if decoded.format != image_format.pillow_format:
                    raise ContextImageValidationError(
                        "Decoded image type changed during validation.",
                        reason_code="declared_type_mismatch",
                    )
                if int(getattr(decoded, "n_frames", 1)) != frame_count:
                    raise ContextImageValidationError(
                        "Decoded image frame count changed during validation.",
                        reason_code="frame_count_mismatch",
                    )
                for frame_index in range(frame_count):
                    decoded.seek(frame_index)
                    frame_width, frame_height = decoded.size
                    _validate_geometry(frame_width, frame_height)
                    total_pixels += frame_width * frame_height
                    if total_pixels > MAX_CONTEXT_IMAGE_TOTAL_PIXELS:
                        raise ContextImageValidationError(
                            "Image aggregate frame pixels exceed the supported limit.",
                            reason_code="total_pixel_limit_exceeded",
                        )
                    decoded.load()
                if decoded.size != (width, height):
                    raise ContextImageValidationError(
                        "Decoded image geometry changed during validation.",
                        reason_code="geometry_mismatch",
                    )
    except ContextImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ContextImageValidationError(
            "Image dimensions exceed the safe decoding limit.",
            reason_code="decompression_limit_exceeded",
        ) from exc
    except (EOFError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ContextImageValidationError(
            "The image container is truncated or invalid.",
            reason_code="decoder_rejected",
        ) from exc
    return ValidatedContextImage(
        image_format.media_type,
        width,
        height,
        frame_count,
        total_pixels,
    )


def _validate_geometry(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ContextImageValidationError(
            "Image dimensions must be positive.",
            reason_code="invalid_dimensions",
        )
    if width > MAX_CONTEXT_IMAGE_DIMENSION or height > MAX_CONTEXT_IMAGE_DIMENSION:
        raise ContextImageValidationError(
            "Image dimensions exceed the supported limit.",
            reason_code="dimension_limit_exceeded",
        )
    if width * height > MAX_CONTEXT_IMAGE_PIXELS:
        raise ContextImageValidationError(
            "Image pixel count exceeds the supported limit.",
            reason_code="pixel_limit_exceeded",
        )


def _reject_invalid_container_boundary(
    content: bytes,
    *,
    image_format: ContextImageFormat,
) -> None:
    validator = _EXACT_CONTAINER_BOUNDARIES[image_format.pillow_format]
    if validator(content) != len(content):
        raise ContextImageValidationError(
            (
                f"{image_format.pillow_format} data is truncated, structurally "
                "invalid, or has trailing payload bytes."
            ),
            reason_code="container_boundary_invalid",
        )


def _png_end_offset(content: bytes) -> int:
    signature = b"\x89PNG\r\n\x1a\n"
    if not content.startswith(signature):
        return -1
    offset = len(signature)
    while offset + 12 <= len(content):
        chunk_length = int.from_bytes(content[offset : offset + 4], "big")
        chunk_type = content[offset + 4 : offset + 8]
        end = offset + 12 + chunk_length
        if end > len(content):
            return -1
        if chunk_type == b"IEND":
            return end if chunk_length == 0 else -1
        offset = end
    return -1


def _jpeg_end_offset(content: bytes) -> int:
    """Return the first structural JPEG EOI boundary.

    Merely checking that a byte string starts with SOI and ends with EOI lets an
    attacker append a payload (or another complete JPEG) and add a second EOI.
    Walk length-delimited marker segments and entropy-coded scans so the EOI
    consumed by the JPEG container itself must also be the final byte.
    """

    if len(content) < 4 or content[:2] != b"\xff\xd8":
        return -1
    cursor = 2
    content_length = len(content)
    while cursor < content_length:
        if content[cursor] != 0xFF:
            return -1
        marker_start = cursor
        while cursor < content_length and content[cursor] == 0xFF:
            cursor += 1
        if cursor >= content_length:
            return -1
        marker = content[cursor]
        cursor += 1
        if marker == 0xD9:
            return cursor
        if marker in {0x00, 0xD8}:
            return -1
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if cursor + 2 > content_length:
            return -1
        segment_length = int.from_bytes(content[cursor : cursor + 2], "big")
        if segment_length < 2:
            return -1
        segment_end = cursor + segment_length
        if segment_end > content_length:
            return -1
        if marker != 0xDA:
            cursor = segment_end
            continue

        # The SOS header is length-delimited. Its entropy-coded payload ends at
        # the next unstuffed, non-restart marker; progressive JPEGs may then
        # continue with another marker segment and scan.
        cursor = segment_end
        while cursor < content_length:
            entropy_marker = content.find(b"\xff", cursor)
            if entropy_marker < 0:
                return -1
            cursor = entropy_marker + 1
            while cursor < content_length and content[cursor] == 0xFF:
                cursor += 1
            if cursor >= content_length:
                return -1
            marker = content[cursor]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                cursor += 1
                continue
            if marker == 0xD9:
                return cursor + 1
            cursor = entropy_marker
            break
        else:
            return -1
        if cursor == marker_start:
            return -1
    return -1


def _gif_end_offset(content: bytes) -> int:
    """Walk GIF blocks and return the exact trailer boundary."""

    if len(content) < 14 or content[:6] not in {b"GIF87a", b"GIF89a"}:
        return -1
    cursor = 13  # Header plus Logical Screen Descriptor.
    packed = content[10]
    if packed & 0x80:
        cursor = _consume_gif_color_table(content, cursor, packed)
        if cursor < 0:
            return -1

    while cursor < len(content):
        introducer = content[cursor]
        cursor += 1
        if introducer == 0x3B:  # Trailer.
            return cursor
        if introducer == 0x21:  # Extension block.
            if cursor >= len(content):
                return -1
            cursor += 1  # Extension label.
            cursor = _consume_gif_sub_blocks(content, cursor)
            if cursor < 0:
                return -1
            continue
        if introducer != 0x2C:  # Image descriptor.
            return -1
        if cursor + 9 > len(content):
            return -1
        image_packed = content[cursor + 8]
        cursor += 9
        if image_packed & 0x80:
            cursor = _consume_gif_color_table(content, cursor, image_packed)
            if cursor < 0:
                return -1
        if cursor >= len(content):
            return -1
        cursor += 1  # LZW minimum code size.
        cursor = _consume_gif_sub_blocks(content, cursor)
        if cursor < 0:
            return -1
    return -1


def _consume_gif_color_table(content: bytes, cursor: int, packed: int) -> int:
    table_bytes = 3 * (1 << ((packed & 0x07) + 1))
    end = cursor + table_bytes
    return end if end <= len(content) else -1


def _consume_gif_sub_blocks(content: bytes, cursor: int) -> int:
    while cursor < len(content):
        block_size = content[cursor]
        cursor += 1
        if block_size == 0:
            return cursor
        cursor += block_size
        if cursor > len(content):
            return -1
    return -1


def _webp_end_offset(content: bytes) -> int:
    """Walk a WebP RIFF container and return its exact declared boundary."""

    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        return -1
    declared_size = int.from_bytes(content[4:8], "little")
    if declared_size < 4 or declared_size + 8 != len(content):
        return -1

    cursor = 12
    while cursor < len(content):
        if cursor + 8 > len(content):
            return -1
        chunk_size = int.from_bytes(content[cursor + 4 : cursor + 8], "little")
        chunk_end = cursor + 8 + chunk_size
        padded_end = chunk_end + (chunk_size & 1)
        if padded_end > len(content):
            return -1
        cursor = padded_end
    return cursor if cursor == len(content) else -1


_EXACT_CONTAINER_BOUNDARIES: dict[str, Callable[[bytes], int]] = {
    "PNG": _png_end_offset,
    "JPEG": _jpeg_end_offset,
    "GIF": _gif_end_offset,
    "WEBP": _webp_end_offset,
}


__all__ = [
    "MAX_CONTEXT_IMAGE_DIMENSION",
    "MAX_CONTEXT_IMAGE_FRAMES",
    "MAX_CONTEXT_IMAGE_PIXELS",
    "MAX_CONTEXT_IMAGE_TOTAL_PIXELS",
    "ContextImageValidationError",
    "ValidatedContextImage",
    "validate_context_image",
]
