from __future__ import annotations

import hashlib
import io
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pytest
from PIL import Image, PngImagePlugin

import backend.preview_providers.pillow_image as pillow_provider_module
from backend.preview_providers import (
    PillowImagePreviewProvider,
    build_builtin_preview_providers,
)
from backend.services.preview_provider_registry import PreviewProviderRegistry
from backend.spi.preview import (
    JsonValue,
    PreviewDelivery,
    PreviewLimits,
    PreviewOutputSink,
    PreviewProbeRequest,
    PreviewProviderError,
    PreviewRequest,
    PreviewVariantKind,
    PreviewVariantReceipt,
    validate_descriptor_shape,
)


class _Blob:
    def __init__(self, content: bytes) -> None:
        self._content = content

    @property
    def byte_size(self) -> int:
        return len(self._content)

    def open_binary(self) -> BinaryIO:
        return io.BytesIO(self._content)

    def copy_to(self, _destination: Path) -> int:  # pragma: no cover - guard
        raise AssertionError("the provider must not request a filesystem path")


class _CapturingSink(PreviewOutputSink):
    def __init__(self) -> None:
        self.content: bytes | None = None
        self.metadata: Mapping[str, JsonValue] | None = None

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
    ) -> PreviewVariantReceipt:
        content = source.read()
        assert type(content) is bytes
        assert len(content) == expected_byte_size
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        self.content = content
        self.metadata = metadata
        return PreviewVariantReceipt(
            receipt_id=uuid4(),
            kind=kind,
            media_type=media_type,
            renderer_contract=renderer_contract,
            delivery=delivery,
            byte_size=expected_byte_size,
            sha256=expected_sha256,
            rank=rank,
            ordinal=ordinal,
            filename=filename,
            width=width,
            height=height,
            duration_ms=duration_ms,
            page_count=page_count,
            metadata=metadata or {},
        )


def _encoded_image(
    pillow_format: str,
    *,
    size: tuple[int, int] = (80, 40),
    color: tuple[int, ...] = (20, 80, 160),
    **save_options: object,
) -> bytes:
    mode = "RGBA" if len(color) == 4 else "RGB"
    image = Image.new(mode, size, color)
    output = io.BytesIO()
    try:
        image.save(output, format=pillow_format, **save_options)
        return output.getvalue()
    finally:
        image.close()


def _request(
    content: bytes,
    *,
    format_id: str,
    media_type: str,
    max_pixels: int = 1_000_000,
    max_variant_bytes: int = 24 * 1024 * 1024,
    input_sha256: str | None = None,
) -> PreviewRequest:
    return PreviewRequest(
        workspace_id=uuid4(),
        library_item_id=uuid4(),
        version_id=uuid4(),
        input_kind="source",
        input_conversion_attempt_id=None,
        input_artifact_id=None,
        source=_Blob(content),
        input_sha256=input_sha256 or hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        format_id=format_id,
        media_type=media_type,
        original_filename=f"image.{format_id}",
        limits=PreviewLimits(
            max_input_bytes=max(len(content), 1),
            max_variant_bytes=max_variant_bytes,
            max_total_output_bytes=24 * 1024 * 1024,
            max_pixels=max_pixels,
        ),
    )


@pytest.mark.parametrize(
    ("format_id", "media_type", "pillow_format", "size"),
    (
        ("jpeg", "image/jpeg", "JPEG", (2_500, 1_000)),
        ("png", "image/png", "PNG", (80, 40)),
    ),
)
def test_provider_reencodes_jpeg_and_png_as_bounded_png(
    format_id: str,
    media_type: str,
    pillow_format: str,
    size: tuple[int, int],
) -> None:
    content = _encoded_image(pillow_format, size=size)
    sink = _CapturingSink()

    result = PillowImagePreviewProvider().generate(
        _request(
            content,
            format_id=format_id,
            media_type=media_type,
            max_pixels=size[0] * size[1],
        ),
        sink,
    )

    assert result.completeness == "complete"
    assert sink.content is not None
    assert sink.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert sink.content != content
    with Image.open(io.BytesIO(sink.content), formats=["PNG"]) as preview:
        preview.load()
        assert max(preview.size) <= 2_048
        expected_size = (2_048, 819) if format_id == "jpeg" else size
        assert preview.size == expected_size
        preview_size = preview.size
    receipt = result.variants[0]
    assert receipt.media_type == "image/png"
    assert receipt.renderer_contract == "core.image.v1"
    assert receipt.sha256 == hashlib.sha256(sink.content).hexdigest()
    assert receipt.byte_size == len(sink.content)
    assert (receipt.width, receipt.height) == preview_size


def test_provider_strips_exif_icc_and_png_text_metadata() -> None:
    exif = Image.Exif()
    exif[0x010E] = "private description"
    jpeg = _encoded_image(
        "JPEG",
        exif=exif,
        icc_profile=b"not-a-real-profile-but-still-metadata",
    )
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("private-key", "private-value")
    png = _encoded_image("PNG", pnginfo=png_info)

    for content, format_id, media_type in (
        (jpeg, "jpeg", "image/jpeg"),
        (png, "png", "image/png"),
    ):
        sink = _CapturingSink()
        result = PillowImagePreviewProvider().generate(
            _request(content, format_id=format_id, media_type=media_type),
            sink,
        )

        assert sink.content is not None
        with Image.open(io.BytesIO(sink.content), formats=["PNG"]) as preview:
            preview.load()
            assert not preview.getexif()
            assert "exif" not in preview.info
            assert "icc_profile" not in preview.info
            assert "private-key" not in preview.info
        assert result.metadata["metadata_stripped"] is True
        assert sink.metadata is not None
        assert sink.metadata["metadata_stripped"] is True


@pytest.mark.parametrize(
    ("content", "format_id", "media_type"),
    (
        (b"\x89PNG\r\n\x1a\ncorrupt", "png", "image/png"),
        (_encoded_image("JPEG")[:-24], "jpeg", "image/jpeg"),
    ),
)
def test_provider_rejects_corrupt_and_truncated_images_safely(
    content: bytes,
    format_id: str,
    media_type: str,
) -> None:
    with pytest.raises(PreviewProviderError) as raised:
        PillowImagePreviewProvider().generate(
            _request(content, format_id=format_id, media_type=media_type),
            _CapturingSink(),
        )

    assert raised.value.code == "invalid_image"
    assert raised.value.retryable is False
    assert raised.value.fallback_allowed is False


def test_provider_enforces_total_decoded_pixel_limit() -> None:
    content = _encoded_image("PNG", size=(20, 20))

    with pytest.raises(PreviewProviderError) as raised:
        PillowImagePreviewProvider().generate(
            _request(
                content,
                format_id="png",
                media_type="image/png",
                max_pixels=399,
            ),
            _CapturingSink(),
        )

    assert raised.value.code == "image_decode_limit"
    assert raised.value.fallback_allowed is False


def test_provider_enforces_output_byte_limit_before_calling_the_sink() -> None:
    content = _encoded_image("PNG")
    sink = _CapturingSink()

    with pytest.raises(PreviewProviderError) as raised:
        PillowImagePreviewProvider().generate(
            _request(
                content,
                format_id="png",
                media_type="image/png",
                max_variant_bytes=16,
            ),
            sink,
        )

    assert raised.value.code == "preview_output_limit"
    assert sink.content is None


def test_provider_processes_only_a_bounded_first_apng_frame() -> None:
    first = Image.new("RGB", (16, 8), (255, 0, 0))
    second = Image.new("RGB", (16, 8), (0, 255, 0))
    output = io.BytesIO()
    try:
        first.save(
            output,
            format="PNG",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )
    finally:
        first.close()
        second.close()
    content = output.getvalue()
    sink = _CapturingSink()

    result = PillowImagePreviewProvider().generate(
        _request(
            content,
            format_id="png",
            media_type="image/png",
            max_pixels=16 * 8 * 2,
        ),
        sink,
    )

    assert result.completeness == "partial"
    assert result.metadata["source_frame_count"] == 2
    assert sink.metadata is not None
    assert sink.metadata["first_frame_only"] is True
    assert sink.content is not None
    with Image.open(io.BytesIO(sink.content), formats=["PNG"]) as preview:
        assert preview.n_frames == 1
        assert preview.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_provider_validates_the_entire_path_free_source_digest() -> None:
    content = _encoded_image("PNG")

    with pytest.raises(PreviewProviderError) as raised:
        PillowImagePreviewProvider().generate(
            _request(
                content,
                format_id="png",
                media_type="image/png",
                input_sha256="0" * 64,
            ),
            _CapturingSink(),
        )

    assert raised.value.code == "invalid_image_source"


def test_descriptor_builder_and_registry_expose_only_the_trusted_contract() -> None:
    providers = build_builtin_preview_providers()
    assert len(providers) == 1
    provider = providers[0]
    descriptor = provider.descriptor()
    validate_descriptor_shape(descriptor)

    assert descriptor.formats == frozenset({"jpeg", "png"})
    assert descriptor.input_kinds == frozenset({"source"})
    assert descriptor.execution_mode == "builtin"
    assert descriptor.network_required is False
    assert descriptor.deterministic is True
    assert descriptor.output_renderer_contracts == frozenset({"core.image.v1"})
    assert descriptor.output_media_types == frozenset({"image/png"})
    assert descriptor.distribution_digest is not None
    assert len(descriptor.distribution_digest) == 64
    # No wheel signature or SBOM is claimed when this build cannot verify one.
    assert descriptor.sbom_sha256 is None
    assert descriptor.signature_identity is None

    lock = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "uv.lock").read_text(encoding="utf-8")
    )
    pillow_lock = next(
        package for package in lock["package"] if package["name"] == "pillow"
    )
    canonical_lock = json.dumps(
        pillow_lock,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert pillow_lock["version"] == descriptor.provider_version
    assert hashlib.sha256(canonical_lock).hexdigest() == (
        pillow_provider_module._PILLOW_LOCK_ENTRY_SHA256
    )

    registry = PreviewProviderRegistry(providers)
    chain = registry.provider_chain_snapshot(
        format_id="jpeg",
        media_type="image/jpeg",
        input_kind="source",
    )
    assert [snapshot["provider_id"] for snapshot in chain] == [
        "dtxwiki.pillow-image-preview"
    ]
    assert chain[0]["distribution_digest"] == descriptor.distribution_digest
    assert registry.health(descriptor.provider_id).ready is True


def test_health_and_generation_fail_closed_when_pillow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _encoded_image("PNG")

    def missing_pillow():
        raise ImportError("Pillow unavailable")

    monkeypatch.setattr(pillow_provider_module, "_load_pillow", missing_pillow)
    monkeypatch.setattr(
        pillow_provider_module,
        "_pillow_installation_available",
        lambda: False,
    )
    provider = PillowImagePreviewProvider()

    assert provider.health() is False
    health = PreviewProviderRegistry((provider,)).health(
        provider.descriptor().provider_id
    )
    assert health.ready is False
    assert health.reason_code == "provider_unhealthy"
    with pytest.raises(PreviewProviderError) as raised:
        provider.generate(
            _request(content, format_id="png", media_type="image/png"),
            _CapturingSink(),
        )
    assert raised.value.code == "provider_dependency_missing"
    assert raised.value.fallback_allowed is False


def test_probe_rejects_crossed_format_media_type_pairs() -> None:
    result = PillowImagePreviewProvider().probe(
        PreviewProbeRequest(
            format_id="jpeg",
            media_type="image/png",
            input_kind="source",
            byte_size=100,
        )
    )

    assert result.supported is False
    assert result.reason_code == "media_type_mismatch"
