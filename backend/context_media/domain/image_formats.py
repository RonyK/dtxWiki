"""Canonical Context image format capabilities.

This framework-free table is the single source for domain admission, decoder
selection, Shared Media format identifiers, and public wire media types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

type ContextImageMediaType = Literal[
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
]


@dataclass(frozen=True, slots=True)
class ContextImageFormat:
    media_type: ContextImageMediaType
    format_id: str
    pillow_format: str
    extensions: tuple[str, ...]
    multi_frame_allowed: bool


CONTEXT_IMAGE_FORMATS: Mapping[str, ContextImageFormat] = MappingProxyType(
    {
        "image/png": ContextImageFormat("image/png", "png", "PNG", (".png",), False),
        "image/jpeg": ContextImageFormat(
            "image/jpeg", "jpeg", "JPEG", (".jpg", ".jpeg"), False
        ),
        "image/gif": ContextImageFormat("image/gif", "gif", "GIF", (".gif",), True),
        "image/webp": ContextImageFormat(
            "image/webp", "webp", "WEBP", (".webp",), True
        ),
    }
)

ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES = frozenset(CONTEXT_IMAGE_FORMATS)


def context_image_format_for_media_type(
    media_type: str,
) -> ContextImageFormat | None:
    return CONTEXT_IMAGE_FORMATS.get(media_type)


__all__ = [
    "ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES",
    "CONTEXT_IMAGE_FORMATS",
    "ContextImageFormat",
    "ContextImageMediaType",
    "context_image_format_for_media_type",
]
