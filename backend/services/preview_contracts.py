"""Closed presentation vocabulary shared by Preview generation and reads."""

from __future__ import annotations

from typing import Final

SAFE_PREVIEW_RENDERER_MEDIA_TYPES: Final[dict[str, frozenset[str]]] = {
    "core.markdown.v1": frozenset({"text/markdown"}),
    "core.text.v1": frozenset({"text/plain"}),
    "core.table.v1": frozenset({"application/json", "text/markdown"}),
    "core.pdf.v1": frozenset({"application/pdf"}),
    "core.image.v1": frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"}),
    "core.audio.v1": frozenset({"audio/mpeg", "audio/ogg", "audio/wav"}),
    "core.video.v1": frozenset({"video/mp4", "video/webm"}),
    "core.metadata.v1": frozenset({"application/json"}),
}

RANGE_PREVIEW_RENDERERS: Final[frozenset[str]] = frozenset(
    {"core.pdf.v1", "core.audio.v1", "core.video.v1"}
)
TEXT_PREVIEW_RENDERERS: Final[frozenset[str]] = frozenset(
    {"core.markdown.v1", "core.text.v1", "core.table.v1", "core.metadata.v1"}
)

PREVIEW_RENDERER_KINDS: Final[dict[str, frozenset[str]]] = {
    "core.markdown.v1": frozenset({"markdown"}),
    "core.text.v1": frozenset({"text"}),
    "core.table.v1": frozenset({"table"}),
    "core.pdf.v1": frozenset({"pdf"}),
    "core.image.v1": frozenset({"image"}),
    "core.audio.v1": frozenset({"audio"}),
    "core.video.v1": frozenset({"video"}),
    "core.metadata.v1": frozenset({"metadata"}),
}

PREVIEW_RENDERER_DELIVERIES: Final[dict[str, str]] = {
    **{renderer: "range" for renderer in RANGE_PREVIEW_RENDERERS},
    **{renderer: "text" for renderer in TEXT_PREVIEW_RENDERERS},
    "core.image.v1": "inline",
}


def renderer_accepts_media_type(renderer_contract: str, media_type: str) -> bool:
    return media_type in SAFE_PREVIEW_RENDERER_MEDIA_TYPES.get(
        renderer_contract,
        frozenset(),
    )


def renderer_accepts_variant(
    renderer_contract: str,
    *,
    kind: str,
    media_type: str,
    delivery: str,
) -> bool:
    return (
        renderer_accepts_media_type(renderer_contract, media_type)
        and kind in PREVIEW_RENDERER_KINDS.get(renderer_contract, frozenset())
        and delivery == PREVIEW_RENDERER_DELIVERIES.get(renderer_contract)
    )


__all__ = [
    "RANGE_PREVIEW_RENDERERS",
    "PREVIEW_RENDERER_DELIVERIES",
    "PREVIEW_RENDERER_KINDS",
    "SAFE_PREVIEW_RENDERER_MEDIA_TYPES",
    "TEXT_PREVIEW_RENDERERS",
    "renderer_accepts_media_type",
    "renderer_accepts_variant",
]
