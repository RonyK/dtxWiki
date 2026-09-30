from io import BytesIO

import pytest
from PIL import Image

from backend.context_media.domain import MAX_CONTEXT_IMAGE_DIMENSION
from backend.services import context_image_validation as image_validation
from backend.services.context_image_validation import (
    ContextImageValidationError,
    validate_context_image,
)


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    (
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
    ),
)
def test_context_image_validation_decodes_complete_static_format_matrix(
    image_format: str, media_type: str
) -> None:
    content = _image_bytes(image_format, size=(7, 5))

    validated = validate_context_image(content, declared_media_type=media_type)

    assert validated.canonical_media_type == media_type
    assert (validated.width, validated.height) == (7, 5)
    assert validated.frame_count == 1
    assert validated.total_pixels == 35


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    (
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
    ),
)
def test_context_image_validation_rejects_truncation_and_trailing_polyglot(
    image_format: str,
    media_type: str,
) -> None:
    content = _image_bytes(image_format)

    for invalid in (content[:-2], content + b"<script>polyglot</script>"):
        with pytest.raises(ContextImageValidationError) as captured:
            validate_context_image(invalid, declared_media_type=media_type)
        assert captured.value.reason_code == "container_boundary_invalid"


def test_context_image_validation_rejects_jpeg_payload_hidden_before_terminal_eoi() -> (
    None
):
    content = _image_bytes("JPEG")

    for invalid in (
        content + b"<script>polyglot</script>\xff\xd9",
        content + content,
    ):
        with pytest.raises(ContextImageValidationError) as captured:
            validate_context_image(invalid, declared_media_type="image/jpeg")

        assert captured.value.reason_code == "container_boundary_invalid"


def test_context_image_validation_rejects_invalid_webp_chunk_boundary() -> None:
    content = bytearray(_image_bytes("WEBP"))
    first_chunk_size = int.from_bytes(content[16:20], "little")
    content[16:20] = (first_chunk_size + 4).to_bytes(4, "little")

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(bytes(content), declared_media_type="image/webp")

    assert captured.value.reason_code == "container_boundary_invalid"


def test_context_image_validation_rejects_declared_type_mismatch() -> None:
    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(_image_bytes("PNG"), declared_media_type="image/jpeg")

    assert captured.value.reason_code == "container_boundary_invalid"


def test_context_image_validation_rejects_dimension_limit_before_pixel_load() -> None:
    content = _image_bytes("PNG", size=(MAX_CONTEXT_IMAGE_DIMENSION + 1, 1))

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(content, declared_media_type="image/png")

    assert captured.value.reason_code == "dimension_limit_exceeded"


def test_context_image_validation_rejects_animated_png() -> None:
    content = _animated_image_bytes("PNG")

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(content, declared_media_type="image/png")

    assert captured.value.reason_code == "frame_count_exceeded"


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    (("GIF", "image/gif"), ("WEBP", "image/webp")),
)
def test_context_image_validation_fully_decodes_supported_animations(
    image_format: str,
    media_type: str,
) -> None:
    validated = validate_context_image(
        _animated_image_bytes(image_format),
        declared_media_type=media_type,
    )

    assert validated.frame_count == 2
    assert validated.total_pixels == 8


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    (("GIF", "image/gif"), ("WEBP", "image/webp")),
)
def test_context_image_validation_rejects_corrupt_later_frame_with_valid_boundary(
    image_format: str,
    media_type: str,
) -> None:
    content = _animated_image_bytes(image_format, size=(4, 3))
    if image_format == "GIF":
        corrupted = _corrupt_second_gif_frame(content)
        boundary = image_validation._gif_end_offset(corrupted)
    else:
        corrupted = _corrupt_second_webp_frame(content)
        boundary = image_validation._webp_end_offset(corrupted)

    # Container lengths, chunks/blocks, and terminal boundary remain exact.
    assert boundary == len(corrupted)
    with Image.open(BytesIO(corrupted)) as candidate:
        assert candidate.n_frames == 2
        candidate.verify()

    # The first frame still decodes; corruption is isolated to the later frame.
    with Image.open(BytesIO(corrupted)) as decoded:
        decoded.seek(0)
        decoded.load()
        decoded.seek(1)
        with pytest.raises(OSError):
            decoded.load()

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(corrupted, declared_media_type=media_type)

    assert captured.value.reason_code == "decoder_rejected"


def test_context_image_validation_enforces_frame_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_validation, "MAX_CONTEXT_IMAGE_FRAMES", 2)

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(
            _animated_image_bytes("GIF", frame_count=3),
            declared_media_type="image/gif",
        )

    assert captured.value.reason_code == "frame_count_exceeded"


def test_context_image_validation_enforces_aggregate_frame_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_validation, "MAX_CONTEXT_IMAGE_TOTAL_PIXELS", 7)

    with pytest.raises(ContextImageValidationError) as captured:
        validate_context_image(
            _animated_image_bytes("WEBP"),
            declared_media_type="image/webp",
        )

    assert captured.value.reason_code == "total_pixel_limit_exceeded"


def _image_bytes(image_format: str, *, size: tuple[int, int] = (3, 2)) -> bytes:
    buffer = BytesIO()
    mode = "RGB" if image_format in {"JPEG", "WEBP"} else "RGBA"
    save_options = {"lossless": True} if image_format == "WEBP" else {}
    Image.new(mode, size, "teal").save(buffer, format=image_format, **save_options)
    return buffer.getvalue()


def _animated_image_bytes(
    image_format: str,
    *,
    frame_count: int = 2,
    size: tuple[int, int] = (2, 2),
) -> bytes:
    mode = "RGB" if image_format == "WEBP" else "RGBA"
    colors = ("red", "blue", "green")
    frames = [
        Image.new(mode, size, colors[index % len(colors)])
        for index in range(frame_count)
    ]
    buffer = BytesIO()
    save_options = {"lossless": True} if image_format == "WEBP" else {}
    frames[0].save(
        buffer,
        format=image_format,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        **save_options,
    )
    return buffer.getvalue()


def _corrupt_second_gif_frame(content: bytes) -> bytes:
    corrupted = bytearray(content)
    cursor = 13
    packed = corrupted[10]
    if packed & 0x80:
        cursor += 3 * (1 << ((packed & 0x07) + 1))

    frame_count = 0
    while cursor < len(corrupted):
        introducer = corrupted[cursor]
        cursor += 1
        if introducer == 0x21:
            cursor += 1  # Extension label.
            cursor = _skip_gif_sub_blocks(corrupted, cursor)
            continue
        if introducer == 0x3B:
            break
        assert introducer == 0x2C
        image_packed = corrupted[cursor + 8]
        cursor += 9
        if image_packed & 0x80:
            cursor += 3 * (1 << ((image_packed & 0x07) + 1))
        cursor += 1  # LZW minimum code size.
        frame_count += 1
        if frame_count == 2:
            data_size = corrupted[cursor]
            assert data_size > 0
            data_start = cursor + 1
            corrupted[data_start : data_start + data_size] = b"\xff" * data_size
            return bytes(corrupted)
        cursor = _skip_gif_sub_blocks(corrupted, cursor)
    raise AssertionError("generated GIF did not contain a second frame")


def _skip_gif_sub_blocks(content: bytes | bytearray, cursor: int) -> int:
    while True:
        block_size = content[cursor]
        cursor += 1
        if block_size == 0:
            return cursor
        cursor += block_size


def _corrupt_second_webp_frame(content: bytes) -> bytes:
    corrupted = bytearray(content)
    cursor = 12
    frame_count = 0
    while cursor < len(corrupted):
        chunk_type = bytes(corrupted[cursor : cursor + 4])
        chunk_size = int.from_bytes(corrupted[cursor + 4 : cursor + 8], "little")
        if chunk_type == b"ANMF":
            frame_count += 1
            if frame_count == 2:
                nested_chunk = cursor + 8 + 16
                assert bytes(corrupted[nested_chunk : nested_chunk + 4]) == b"VP8L"
                nested_size = int.from_bytes(
                    corrupted[nested_chunk + 4 : nested_chunk + 8], "little"
                )
                assert nested_size > 6
                payload_start = nested_chunk + 8
                corrupt_start = payload_start + (nested_size // 3)
                payload_end = payload_start + nested_size
                corrupted[corrupt_start:payload_end] = b"\xff" * (
                    payload_end - corrupt_start
                )
                return bytes(corrupted)
        cursor += 8 + chunk_size + (chunk_size & 1)
    raise AssertionError("generated WebP did not contain a second frame")
