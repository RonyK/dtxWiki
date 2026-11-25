import base64
import warnings
import zipfile
import zlib
from io import BytesIO

import pytest
from PIL import Image

import backend.services.library_formats as library_formats_module
from backend.services.library_formats import (
    DetectorMatch,
    FormatDescriptor,
    FormatRegistry,
    LibraryFormatError,
    _hdf5_lookup3_checksum,
    _ogg_crc32,
    normalize_library_filename,
)
from backend.tests.fixtures.library_binary_samples import (
    VALID_HDF5_V0_WITH_DATASET,
    VALID_HDF5_V0_WITH_GROUP,
    VALID_HDF5_V0_WITH_GROUP_OFFSETS4_LENGTHS8,
    VALID_HDF5_V0_WITH_GROUP_OFFSETS8_LENGTHS4,
    VALID_HDF5_V0_WITH_ORDERED_GROUPS,
    VALID_HDF5_V0_WITH_ROOT_ATTRIBUTES,
    VALID_HDF5_V0_WITH_UNWRITTEN_DATASET,
    VALID_HDF5_V0_WITH_UNWRITTEN_FILL_DATASET,
    VALID_HDF5_V3,
    VALID_HDF5_V3_128K_USERBLOCK_WITH_GROUP_SUFFIX,
    VALID_HDF5_V3_WITH_DATASET,
    VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE,
    VALID_HDF5_V3_WITH_FLOAT_DATASET,
    VALID_HDF5_V3_WITH_GROUP,
    VALID_HDF5_V3_WITH_TWO_DATASETS,
    VALID_HDF5_V3_WITH_UNWRITTEN_DATASET,
    VALID_HDF5_V3_WITH_UNWRITTEN_FILL_DATASET,
    VALID_HDF5_V3_WITH_V2_CONTINUATION,
    VALID_HDF5_V3_WITH_VLEN_ATTRIBUTE_ARRAY,
    VALID_PARQUET_DATA_PAGE_V2_CRC,
    VALID_PARQUET_ONE_ROW,
    VALID_PARQUET_TWO_ROW_GROUPS,
    VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP,
    VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP,
    VALID_PARQUET_ZERO_ROWS_ZERO_ROW_GROUPS,
)

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_with_ihdr(**overrides: int) -> bytes:
    payload = bytearray(_ONE_PIXEL_PNG)
    header = bytearray(payload[16:29])
    if "width" in overrides:
        header[0:4] = overrides["width"].to_bytes(4, "big")
    if "height" in overrides:
        header[4:8] = overrides["height"].to_bytes(4, "big")
    for name, index in (
        ("bit_depth", 8),
        ("color_type", 9),
        ("compression", 10),
        ("filter_method", 11),
        ("interlace", 12),
    ):
        if name in overrides:
            header[index] = overrides[name]
    payload[16:29] = header
    checksum = zlib.crc32(b"IHDR")
    checksum = zlib.crc32(header, checksum)
    payload[29:33] = (checksum & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(payload)


def _baseline_jpeg() -> bytes:
    image = Image.new("RGB", (24, 12), (20, 80, 160))
    output = BytesIO()
    try:
        image.save(output, format="JPEG", progressive=False)
        return output.getvalue()
    finally:
        image.close()


def _jpeg_with_frame(**overrides: int) -> bytes:
    payload = bytearray(_baseline_jpeg())
    marker = payload.index(b"\xff\xc0")
    frame = marker + 4
    if "marker" in overrides:
        payload[marker + 1] = overrides["marker"]
    if "precision" in overrides:
        payload[frame] = overrides["precision"]
    if "height" in overrides:
        payload[frame + 1 : frame + 3] = overrides["height"].to_bytes(2, "big")
    if "width" in overrides:
        payload[frame + 3 : frame + 5] = overrides["width"].to_bytes(2, "big")
    if "components" in overrides:
        payload[frame + 5] = overrides["components"]
    return bytes(payload)
_CONTENT_TYPES = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    '  <Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml" />\n'
    "</Types>\n"
)
_WORD_DOCUMENT = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" />
"""


def _latest_child_message_mutation(*, message_type: int, message_flags: int) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_GROUP)
    payload[186] = message_type
    payload[189] = message_flags
    payload[306:310] = _hdf5_lookup3_checksum(payload[179:306]).to_bytes(4, "little")
    return bytes(payload)


def _legacy_root_tree_sibling_mutation(
    *,
    left: int | None = None,
    right: int | None = None,
    child_left: int | None = None,
    child_right: int | None = None,
) -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    for offset, address in (
        (144, left),
        (152, right),
        (848, child_left),
        (856, child_right),
    ):
        if address is not None:
            payload[offset : offset + 8] = address.to_bytes(8, "little")
    return bytes(payload)


def _legacy_root_tree_key_mutation(
    *, first_key: int | None = None, final_key: int | None = None
) -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    assert payload[136:140] == b"TREE"
    assert payload[160:168] == (0).to_bytes(8, "little")
    assert payload[176:184] == (8).to_bytes(8, "little")
    if first_key is not None:
        payload[160:168] = first_key.to_bytes(8, "little")
    if final_key is not None:
        payload[176:184] = final_key.to_bytes(8, "little")
    return bytes(payload)


def _legacy_ordered_group_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_ORDERED_GROUPS)
    assert payload[136:140] == b"TREE"
    assert payload[176:184] == (24).to_bytes(8, "little")
    assert payload[1512:1520] == (8).to_bytes(8, "little")
    assert payload[1552:1560] == (16).to_bytes(8, "little")
    assert payload[1592:1600] == (24).to_bytes(8, "little")
    if mutation == "snod_duplicate":
        payload[1552:1560] = (8).to_bytes(8, "little")
    elif mutation == "snod_unsorted":
        payload[1512:1520] = (16).to_bytes(8, "little")
        payload[1552:1560] = (8).to_bytes(8, "little")
    elif mutation == "non_greatest_boundary":
        payload[176:184] = (16).to_bytes(8, "little")
    elif mutation == "duplicate_text_boundary_offset":
        # Offset 40 is unused heap space. Copy the same boundary bytes there
        # to prove that keys must identify the greatest SNOD entry by offset,
        # not merely resolve to equal text.
        payload[752:760] = b"charlie\x00"
        payload[176:184] = (40).to_bytes(8, "little")
    else:
        raise AssertionError(f"Unknown ordered-group mutation: {mutation}")
    return bytes(payload)


def _unequal_width_snod_name_mutation() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP_OFFSETS8_LENGTHS4)
    assert payload[13:15] == b"\x08\x04"
    assert payload[1204:1208] == b"SNOD"
    assert payload[1212:1216] == (8).to_bytes(4, "little")
    payload[1212:1216] = (0).to_bytes(4, "little")
    return bytes(payload)


def _legacy_empty_tree_unused_key_mutation() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    assert payload[840:848] == b"TREE\x00\x00\x00\x00"
    assert payload[864:872] == b"\x00" * 8
    payload[864:872] = b"unused!!"
    return bytes(payload)


def _legacy_heap_empty_sentinel_mutation() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    assert payload[712:720] == b"\x00" * 8
    payload[712:714] = b"a\x00"
    return bytes(payload)


def _v2_continuation_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[48:52] == b"OHDR"
    assert payload[125] == 0x10
    assert payload[129:137] == (310).to_bytes(8, "little")
    assert payload[137:145] == (67).to_bytes(8, "little")
    assert payload[310:314] == b"OCHK"

    if mutation == "bad_checksum":
        payload[320] ^= 1
    elif mutation == "bad_signature":
        payload[310] ^= 1
        payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(
            4, "little"
        )
    elif mutation == "root_overlap":
        payload[129:137] = (48).to_bytes(8, "little")
        payload[175:179] = _hdf5_lookup3_checksum(payload[48:175]).to_bytes(4, "little")
    elif mutation == "self_cycle":
        payload[314] = 0x10
        payload[315:317] = (16).to_bytes(2, "little")
        payload[318:326] = (310).to_bytes(8, "little")
        payload[326:334] = (67).to_bytes(8, "little")
        payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(
            4, "little"
        )
    elif mutation == "unknown_message":
        payload[314] = 0xFE
        payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(
            4, "little"
        )
    elif mutation == "short_chunk":
        payload[137:145] = (8).to_bytes(8, "little")
        payload[175:179] = _hdf5_lookup3_checksum(payload[48:175]).to_bytes(4, "little")
    else:
        raise AssertionError(f"Unknown mutation: {mutation}")
    return bytes(payload)


def _ochk_attribute_type_confusion(message_type: int) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[314] == 0x0C
    payload[314] = message_type
    payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(4, "little")
    return bytes(payload)


def _latest_chunked_layout_profile() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_DATASET)
    assert payload[237:239] == b"\x03\x01"
    payload[238] = 0x02
    payload[443:447] = _hdf5_lookup3_checksum(payload[179:443]).to_bytes(4, "little")
    return bytes(payload)


def _latest_dataset_raw_address_mutation(
    address: int, *, payload: bytes = VALID_HDF5_V3_WITH_DATASET
) -> bytes:
    mutated = bytearray(payload)
    assert mutated[237:239] == b"\x03\x01"
    mutated[239:247] = address.to_bytes(8, "little")
    mutated[443:447] = _hdf5_lookup3_checksum(mutated[179:443]).to_bytes(4, "little")
    return bytes(mutated)


def _earliest_dataset_raw_address_mutation(address: int) -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_DATASET)
    assert payload[888:890] == b"\x08\x00"
    assert payload[898:906] == (2048).to_bytes(8, "little")
    payload[898:906] = address.to_bytes(8, "little")
    return bytes(payload)


def _latest_second_dataset_raw_address_mutation(address: int) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_TWO_DATASETS)
    assert payload[505:507] == b"\x03\x01"
    assert payload[507:515] == (2072).to_bytes(8, "little")
    payload[507:515] = address.to_bytes(8, "little")
    payload[711:715] = _hdf5_lookup3_checksum(payload[447:711]).to_bytes(4, "little")
    return bytes(payload)


def _latest_unwritten_layout_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_UNWRITTEN_DATASET)
    assert payload[237:255] == b"\x03\x01" + (b"\xff" * 8) + (80).to_bytes(8, "little")
    if mutation == "logical_size_mismatch":
        payload[247:255] = (79).to_bytes(8, "little")
    elif mutation == "partial_undefined_address":
        payload[239] = 0xFE
    elif mutation == "metadata_address":
        payload[239:247] = (48).to_bytes(8, "little")
    else:
        raise AssertionError(f"Unknown unwritten layout mutation: {mutation}")
    payload[443:447] = _hdf5_lookup3_checksum(payload[179:443]).to_bytes(4, "little")
    return bytes(payload)


def _latest_float_datatype_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_FLOAT_DATASET)
    assert payload[215:235] == bytes.fromhex("11203f000800000000004000340b0034ff030000")
    if mutation == "exponent_mantissa_overlap":
        payload[227] = 0
    elif mutation == "sign_exponent_overlap":
        payload[217] = 52
    elif mutation == "sign_mantissa_overlap":
        payload[217] = 0
    elif mutation == "precision_excludes_fields":
        payload[225:227] = (1).to_bytes(2, "little")
    elif mutation == "bias_out_of_range":
        payload[231:235] = (1 << 11).to_bytes(4, "little")
    else:
        raise AssertionError(f"Unknown float mutation: {mutation}")
    payload[443:447] = _hdf5_lookup3_checksum(payload[179:443]).to_bytes(4, "little")
    return bytes(payload)


def _latest_superblock_extension_mutation() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_GROUP)
    assert payload[20:28] == b"\xff" * 8
    payload[20:28] = (48).to_bytes(8, "little")
    payload[44:48] = _hdf5_lookup3_checksum(payload[:44]).to_bytes(4, "little")
    return bytes(payload)


def _v2_attribute_name_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[327:333] == b"label\x00"
    if mutation == "invalid_encoding":
        payload[327] = 0xFF
    elif mutation == "invalid_charset":
        payload[326] = 2
    elif mutation == "interior_nul":
        payload[328] = 0
    elif mutation == "empty_name":
        payload[320:322] = (1).to_bytes(2, "little")
    else:
        raise AssertionError(f"Unknown attribute name mutation: {mutation}")
    payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(4, "little")
    return bytes(payload)


def _duplicate_legacy_attribute_name_mutation() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_ROOT_ATTRIBUTES)
    name_offset = payload.find(b"a1\x00")
    assert name_offset > 0 and payload.find(b"a1\x00", name_offset + 1) == -1
    payload[name_offset : name_offset + 2] = b"a0"
    return bytes(payload)


def _global_heap_mutation(mutation: str) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[2048:2052] == b"GCOL"
    if mutation == "undersized_collection":
        payload[2056:2064] = (40).to_bytes(8, "little")
    elif mutation == "corrupt_free_object":
        assert payload[2088:2090] == b"\x00\x00"
        payload[2092] = 1
    else:
        raise AssertionError(f"Unknown global heap mutation: {mutation}")
    return bytes(payload)


def _minimal_pdf() -> bytes:
    header = b"%PDF-1.4\n"
    catalog_offset = len(header)
    catalog = b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    xref_offset = catalog_offset + len(catalog)
    xref = (
        b"xref\n0 2\n0000000000 65535 f \n"
        + f"{catalog_offset:010d} 00000 n \n".encode()
    )
    trailer = (
        b"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return header + catalog + xref + trailer


def _minimal_wav(
    *,
    audio_format: int = 1,
    byte_rate: int = 16_000,
    block_align: int = 2,
    bits_per_sample: int = 16,
) -> bytes:
    samples = b"\0\0" * 80
    fmt = (
        audio_format.to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8_000).to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits_per_sample.to_bytes(2, "little")
    )
    body = b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    body += b"data" + len(samples).to_bytes(4, "little") + samples
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body


def _minimal_mp3() -> bytes:
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: one complete 417-byte frame.
    return bytes.fromhex("fffb9064") + b"\0" * 413


def _minimal_ogg(packet: bytes | None = None) -> bytes:
    packet = packet or (
        b"OpusHead"
        + bytes((1, 1))
        + b"\0\0"
        + (48_000).to_bytes(4, "little")
        + b"\0\0\0"
    )
    header = bytearray(
        b"OggS"
        + bytes((0, 0x06))
        + b"\0" * 8
        + (1).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + b"\0" * 4
        + bytes((1,))
    )
    page = header + bytes((len(packet),)) + packet
    page[22:26] = _ogg_crc32(page).to_bytes(4, "little")
    return bytes(page)


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _minimal_mp4(codec: bytes = b"avc1") -> bytes:
    ftyp = _mp4_box(b"ftyp", b"isom" + b"\0" * 4 + b"isom")
    handler = _mp4_box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 4)
    assert len(codec) == 4
    sample = (16).to_bytes(4, "big") + codec + b"\0" * 8
    sample_table = _mp4_box(
        b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + sample
    )
    track = _mp4_box(
        b"trak",
        _mp4_box(
            b"mdia",
            handler + _mp4_box(b"minf", _mp4_box(b"stbl", sample_table)),
        ),
    )
    return ftyp + _mp4_box(b"moov", track) + _mp4_box(b"mdat", b"\0")


def _ebml_element(identifier: bytes, payload: bytes) -> bytes:
    assert len(payload) < 127
    return identifier + bytes((0x80 | len(payload),)) + payload


def _minimal_webm(codec: bytes = b"V_VP9") -> bytes:
    header_payload = _ebml_element(bytes.fromhex("4282"), b"webm")
    header = bytes.fromhex("1a45dfa3") + bytes((0x80 | len(header_payload),))
    header += header_payload
    track = _ebml_element(bytes.fromhex("83"), b"\x01")
    track += _ebml_element(bytes.fromhex("86"), codec)
    tracks = _ebml_element(bytes.fromhex("ae"), track)
    segment_payload = _ebml_element(bytes.fromhex("1654ae6b"), tracks)
    segment_payload += _ebml_element(bytes.fromhex("1f43b675"), b"\0")
    segment = bytes.fromhex("18538067") + bytes((0x80 | len(segment_payload),))
    return header + segment + segment_payload


def _compact_unsigned_varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def _deeply_nested_parquet(depth: int) -> bytes:
    def group(name: bytes) -> bytes:
        return (
            b"\x35\x00"
            + b"\x18"
            + _compact_unsigned_varint(len(name))
            + name
            + b"\x15\x02\x00"
        )

    root = b"\x48\x06schema\x15\x02\x00"
    nested = b"".join(group(f"g{index}".encode()) for index in range(depth))
    leaf = b"\x15\x02\x25\x00\x18\x01v\x00"
    schema_size = depth + 2
    schema_header = b"\xfc" + _compact_unsigned_varint(schema_size)
    metadata = (
        b"\x15\x02"
        + b"\x19"
        + schema_header
        + root
        + nested
        + leaf
        + b"\x16\x00"
        + b"\x19\x0c"
        + b"\x00"
    )
    return b"PAR1" + metadata + len(metadata).to_bytes(4, "little") + b"PAR1"


def test_registry_advertises_requested_document_image_and_data_formats():
    registry = FormatRegistry()

    assert {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "markdown",
        "text",
        "jpeg",
        "png",
        "mp3",
        "ogg",
        "wav",
        "mp4",
        "webm",
        "csv",
        "json",
        "yaml",
        "hdf5",
        "parquet",
    } <= {descriptor.format_id for descriptor in registry.descriptors()}


@pytest.mark.parametrize(
    "format_id",
    [
        "doc",
        "ppt",
        "xls",
        "gif",
        "hdf4",
        "hwp",
        "hwpx",
        "hwpml",
        "tiff",
        "webp",
    ],
)
def test_formats_without_structural_validators_are_fail_closed(format_id):
    registry = FormatRegistry()

    assert registry.descriptor(format_id).admission == "deny"
    capability = next(
        item
        for item in registry.capabilities(
            converter_formats={format_id},
            max_bytes=1_000_000,
        )
        if item.format_id == format_id
    )
    assert capability.upload is False
    assert capability.conversion == "denied"


@pytest.mark.parametrize(
    ("filename", "media_type", "payload", "format_id", "validator"),
    [
        ("sample.mp3", "audio/mpeg", _minimal_mp3(), "mp3", "mpeg-audio-v1"),
        ("sample.ogg", "audio/ogg", _minimal_ogg(), "ogg", "ogg-audio-v1"),
        ("sample.wav", "audio/wav", _minimal_wav(), "wav", "wav-container-v1"),
        ("sample.mp4", "video/mp4", _minimal_mp4(), "mp4", "iso-bmff-video-v1"),
        ("sample.webm", "video/webm", _minimal_webm(), "webm", "webm-video-v1"),
    ],
)
def test_bounded_media_validators_enable_store_only_admission(
    tmp_path,
    filename,
    media_type,
    payload,
    format_id,
    validator,
):
    source = tmp_path / filename
    source.write_bytes(payload)

    result = FormatRegistry().detect(
        source,
        filename=filename,
        declared_media_type=media_type,
    )

    assert result.format_id == format_id
    assert result.container["validator"] == validator
    assert result.container["valid"] is True
    assert FormatRegistry().descriptor(format_id).admission == "store_only"


def test_m4v_declared_mime_alias_preserves_canonical_detected_media_type(tmp_path):
    source = tmp_path / "recording.m4v"
    source.write_bytes(_minimal_mp4())

    result = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type="video/x-m4v",
    )

    assert result.format_id == "mp4"
    assert result.declared_media_type == "video/x-m4v"
    assert result.detected_media_type == "video/mp4"
    assert result.container["validator"] == "iso-bmff-video-v1"


@pytest.mark.parametrize(
    ("filename", "media_type", "payload", "expected_codec"),
    [
        (
            "compressed.wav",
            "audio/wav",
            _minimal_wav(
                audio_format=0x55,
                byte_rate=8_000,
                block_align=1,
                bits_per_sample=0,
            ),
            "wave-format-0x0055",
        ),
        ("other.ogg", "audio/ogg", _minimal_ogg(b"Speex   \x01"), "unknown"),
        ("other.mp4", "video/mp4", _minimal_mp4(b"zzzz"), ["zzzz"]),
        ("other.webm", "video/webm", _minimal_webm(b"V_TEST"), ["V_TEST"]),
    ],
)
def test_media_admission_does_not_use_browser_codec_allowlist(
    tmp_path,
    filename,
    media_type,
    payload,
    expected_codec,
):
    source = tmp_path / filename
    source.write_bytes(payload)

    result = FormatRegistry().detect(
        source,
        filename=filename,
        declared_media_type=media_type,
    )

    detected_codec = result.container.get("codec", result.container.get("codecs"))
    assert detected_codec == expected_codec
    assert result.container["valid"] is True


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("truncated.mp3", _minimal_mp3()[:-1]),
        ("truncated.ogg", _minimal_ogg()[:-1]),
        ("truncated.wav", _minimal_wav()[:-1]),
        ("truncated.mp4", _minimal_mp4()[:-1]),
        ("truncated.webm", _minimal_webm()[:-1]),
    ],
)
def test_media_validators_reject_truncated_containers(tmp_path, filename, payload):
    source = tmp_path / filename
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=filename,
            declared_media_type="application/octet-stream",
        )

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [_minimal_mp3(), _minimal_ogg(), _minimal_wav(), _minimal_mp4(), _minimal_webm()],
)
def test_valid_media_cannot_be_spoofed_as_text(tmp_path, payload):
    source = tmp_path / "spoofed.txt"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="text/plain",
        )

    assert raised.value.code == "library_format_mismatch"


def test_detector_accepts_text_and_records_declared_mime_warning(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")

    result = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type="application/octet-stream",
    )

    assert result.format_id == "markdown"
    assert result.detected_media_type == "text/markdown"
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("filename", "declared_media_type", "expected_format", "detected_media_type"),
    [
        ("source.html", "text/html", "html", "text/html"),
        ("source.htm", "text/html", "html", "text/html"),
        (
            "source.xhtml",
            "application/xhtml+xml",
            "html",
            "text/html",
        ),
        ("source.js", "text/javascript", "javascript", "text/javascript"),
        ("source.mjs", "application/javascript", "javascript", "text/javascript"),
        ("source.cjs", "text/javascript", "javascript", "text/javascript"),
    ],
)
def test_active_source_extensions_are_explicit_text_backed_builtins(
    tmp_path,
    filename,
    declared_media_type,
    expected_format,
    detected_media_type,
):
    source = tmp_path / filename
    source.write_bytes(
        b"<script>notExecuted()</script>\n```mermaid\ngraph TD;A-->B\n```\n"
    )

    result = FormatRegistry().detect(
        source,
        filename=filename,
        declared_media_type=declared_media_type,
    )

    assert result.format_id == expected_format
    assert result.detected_media_type == detected_media_type
    assert result.detector == "dtxwiki-text-v1"
    assert result.container == {}


@pytest.mark.parametrize(
    "filename",
    ["fake.parquet", "fake.h5", "fake.hdf5", "fake.hdf4", "fake.hdf"],
)
def test_binary_data_formats_require_their_real_signature(tmp_path, filename):
    source = tmp_path / filename
    source.write_bytes(b"hello world")

    with pytest.raises(LibraryFormatError):
        FormatRegistry().detect(
            source,
            filename=filename,
            declared_media_type="application/octet-stream",
        )


def test_ambiguous_hdf_extension_routes_only_after_signature(tmp_path):
    hdf5 = tmp_path / "data.hdf"
    hdf5.write_bytes(VALID_HDF5_V3)
    hdf4 = tmp_path / "legacy.hdf"
    hdf4.write_bytes(b"\x0e\x03\x13\x01" + b"\0" * 32)

    registry = FormatRegistry()
    assert (
        registry.detect(hdf5, filename=hdf5.name, declared_media_type=None).format_id
        == "hdf5"
    )
    assert (
        registry.detect(hdf4, filename=hdf4.name, declared_media_type=None).format_id
        == "hdf4"
    )


@pytest.mark.parametrize(
    ("filename", "payload", "format_id"),
    [
        ("earliest-group.h5", VALID_HDF5_V0_WITH_GROUP, "hdf5"),
        (
            "earliest-group-offsets8-lengths4.h5",
            VALID_HDF5_V0_WITH_GROUP_OFFSETS8_LENGTHS4,
            "hdf5",
        ),
        (
            "earliest-group-offsets4-lengths8.h5",
            VALID_HDF5_V0_WITH_GROUP_OFFSETS4_LENGTHS8,
            "hdf5",
        ),
        ("earliest-ordered-groups.h5", VALID_HDF5_V0_WITH_ORDERED_GROUPS, "hdf5"),
        ("earliest-dataset.h5", VALID_HDF5_V0_WITH_DATASET, "hdf5"),
        (
            "earliest-unwritten-dataset.h5",
            VALID_HDF5_V0_WITH_UNWRITTEN_DATASET,
            "hdf5",
        ),
        (
            "earliest-unwritten-fill-dataset.h5",
            VALID_HDF5_V0_WITH_UNWRITTEN_FILL_DATASET,
            "hdf5",
        ),
        ("earliest-continuation.h5", VALID_HDF5_V0_WITH_ROOT_ATTRIBUTES, "hdf5"),
        ("valid.h5", VALID_HDF5_V3, "hdf5"),
        ("latest-group.h5", VALID_HDF5_V3_WITH_GROUP, "hdf5"),
        ("latest-dataset.h5", VALID_HDF5_V3_WITH_DATASET, "hdf5"),
        (
            "latest-unwritten-dataset.h5",
            VALID_HDF5_V3_WITH_UNWRITTEN_DATASET,
            "hdf5",
        ),
        (
            "latest-unwritten-fill-dataset.h5",
            VALID_HDF5_V3_WITH_UNWRITTEN_FILL_DATASET,
            "hdf5",
        ),
        (
            "latest-dataset-root-attribute.h5",
            VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE,
            "hdf5",
        ),
        ("latest-two-datasets.h5", VALID_HDF5_V3_WITH_TWO_DATASETS, "hdf5"),
        ("latest-float-dataset.h5", VALID_HDF5_V3_WITH_FLOAT_DATASET, "hdf5"),
        (
            "latest-v2-continuation.h5",
            VALID_HDF5_V3_WITH_V2_CONTINUATION,
            "hdf5",
        ),
        ("valid.parquet", VALID_PARQUET_ONE_ROW, "parquet"),
        (
            "zero-rows-zero-row-groups.parquet",
            VALID_PARQUET_ZERO_ROWS_ZERO_ROW_GROUPS,
            "parquet",
        ),
        (
            "zero-rows-empty-row-group.parquet",
            VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP,
            "parquet",
        ),
        (
            "zero-rows-dictionary-only-row-group.parquet",
            VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP,
            "parquet",
        ),
        ("data-page-v2-crc.parquet", VALID_PARQUET_DATA_PAGE_V2_CRC, "parquet"),
        ("row-groups.parquet", VALID_PARQUET_TWO_ROW_GROUPS, "parquet"),
    ],
)
def test_detector_accepts_real_structurally_valid_binary_data(
    tmp_path, filename, payload, format_id
):
    source = tmp_path / filename
    source.write_bytes(payload)

    result = FormatRegistry().detect(
        source, filename=filename, declared_media_type=None
    )

    assert result.format_id == format_id


def test_hdf5_detector_supports_bounded_direct_userblock_probes(tmp_path):
    source = tmp_path / "userblock.h5"
    source.write_bytes(
        (b"\x00" * (128 * 1024)) + VALID_HDF5_V3_128K_USERBLOCK_WITH_GROUP_SUFFIX
    )

    result = FormatRegistry().detect(
        source, filename=source.name, declared_media_type=None
    )

    assert result.format_id == "hdf5"


def test_hdf5_detector_parses_each_global_heap_collection_once(tmp_path, monkeypatch):
    source = tmp_path / "vlen-attribute-array.h5"
    source.write_bytes(VALID_HDF5_V3_WITH_VLEN_ATTRIBUTE_ARRAY)
    parse_calls = 0
    original_parse = library_formats_module._parse_hdf5_global_heap_collection

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(
        library_formats_module,
        "_parse_hdf5_global_heap_collection",
        counted_parse,
    )

    result = FormatRegistry().detect(
        source, filename=source.name, declared_media_type=None
    )

    assert result.format_id == "hdf5"
    assert parse_calls == 1


def test_hdf5_detector_ignores_undefined_key_storage_in_empty_legacy_tree(tmp_path):
    source = tmp_path / "empty-tree-unused-key.h5"
    source.write_bytes(_legacy_empty_tree_unused_key_mutation())

    result = FormatRegistry().detect(
        source, filename=source.name, declared_media_type=None
    )

    assert result.format_id == "hdf5"


@pytest.mark.parametrize(
    "payload",
    [
        b"\x89HDF\r\n\x1a\n\x03",
        VALID_HDF5_V3[:-1],
        VALID_HDF5_V3[:44] + bytes([VALID_HDF5_V3[44] ^ 1]) + VALID_HDF5_V3[45:],
    ],
)
def test_hdf5_detector_rejects_tiny_truncated_and_bad_checksum_payloads(
    tmp_path, payload
):
    source = tmp_path / "attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        VALID_HDF5_V3[:48] + (b"\x00" * (len(VALID_HDF5_V3) - 48)),
        VALID_HDF5_V0_WITH_GROUP[:96]
        + (b"\x00" * (len(VALID_HDF5_V0_WITH_GROUP) - 96)),
        VALID_HDF5_V0_WITH_GROUP[:120]
        + (b"\x00" * 16)
        + VALID_HDF5_V0_WITH_GROUP[136:],
    ],
)
def test_hdf5_detector_rejects_zeroed_root_headers_and_symbol_table_pointers(
    tmp_path, payload
):
    source = tmp_path / "root-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_hdf5_detector_requires_a_group_anchor_after_valid_root_checksum(tmp_path):
    payload = bytearray(VALID_HDF5_V3)
    payload[55:175] = b"\x00" * 120
    payload[175:179] = _hdf5_lookup3_checksum(payload[48:175]).to_bytes(4, "little")
    source = tmp_path / "checksum-repaired-nil-root.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        VALID_HDF5_V3_WITH_GROUP[:179]
        + (b"\x00" * (len(VALID_HDF5_V3_WITH_GROUP) - 179)),
        VALID_HDF5_V0_WITH_GROUP[:800]
        + (b"\x00" * 40)
        + VALID_HDF5_V0_WITH_GROUP[840:],
    ],
)
def test_hdf5_detector_rejects_zeroed_reachable_child_object_headers(tmp_path, payload):
    source = tmp_path / "linked-child-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    ("message_type", "message_flags"),
    [
        (0xFE, 0x00),
        (0xFE, 0x80),
        (0x02, 0x02),
        (0x02, 0x20),
        (0x02, 0x40),
        (0x02, 0x44),
    ],
)
def test_hdf5_detector_rejects_unknown_or_invalid_reachable_message_headers(
    tmp_path, message_type, message_flags
):
    source = tmp_path / "child-message-attack.h5"
    source.write_bytes(
        _latest_child_message_mutation(
            message_type=message_type, message_flags=message_flags
        )
    )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        _latest_child_message_mutation(message_type=0x01, message_flags=0x00),
        _ochk_attribute_type_confusion(0x01),
        _ochk_attribute_type_confusion(0x03),
        _ochk_attribute_type_confusion(0x08),
    ],
)
def test_hdf5_detector_rejects_checksum_repaired_known_type_payload_confusion(
    tmp_path, payload
):
    source = tmp_path / "known-type-payload-confusion.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize("message_type", [0x04, 0x07, 0x0B, 0x0D, 0x16, 0x17])
def test_hdf5_detector_rejects_known_but_unvalidated_message_profiles(
    tmp_path, message_type
):
    source = tmp_path / "unvalidated-known-message.h5"
    source.write_bytes(
        _latest_child_message_mutation(
            message_type=message_type,
            message_flags=0x00,
        )
    )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_hdf5_detector_fail_closes_chunked_layout_until_index_traversal_exists(
    tmp_path,
):
    source = tmp_path / "chunked-profile.h5"
    source.write_bytes(_latest_chunked_layout_profile())

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("raw-over-superblock.h5", _latest_dataset_raw_address_mutation(1)),
        ("raw-over-root-ohdr.h5", _latest_dataset_raw_address_mutation(48)),
        ("raw-over-child-ohdr.h5", _latest_dataset_raw_address_mutation(179)),
        (
            "raw-over-continuation.h5",
            _latest_dataset_raw_address_mutation(
                447, payload=VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE
            ),
        ),
        (
            "raw-over-global-heap.h5",
            _latest_dataset_raw_address_mutation(
                2072, payload=VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE
            ),
        ),
        ("raw-over-tree-tail.h5", _earliest_dataset_raw_address_mutation(200)),
        ("raw-over-snod-tail.h5", _earliest_dataset_raw_address_mutation(1200)),
        ("raw-over-raw.h5", _latest_second_dataset_raw_address_mutation(2048)),
    ],
)
def test_hdf5_detector_rejects_raw_storage_allocation_overlaps(
    tmp_path, filename, payload
):
    source = tmp_path / filename
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "mutation",
    ["logical_size_mismatch", "partial_undefined_address", "metadata_address"],
)
def test_hdf5_detector_rejects_invalid_unwritten_contiguous_layouts(tmp_path, mutation):
    source = tmp_path / f"unwritten-{mutation}.h5"
    source.write_bytes(_latest_unwritten_layout_mutation(mutation))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        "exponent_mantissa_overlap",
        "sign_exponent_overlap",
        "sign_mantissa_overlap",
        "precision_excludes_fields",
        "bias_out_of_range",
    ],
)
def test_hdf5_detector_rejects_overlapping_or_out_of_range_float_fields(
    tmp_path, mutation
):
    source = tmp_path / f"float-{mutation}.h5"
    source.write_bytes(_latest_float_datatype_mutation(mutation))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        _latest_superblock_extension_mutation(),
        _global_heap_mutation("undersized_collection"),
        _global_heap_mutation("corrupt_free_object"),
        *(
            _v2_attribute_name_mutation(mutation)
            for mutation in (
                "invalid_encoding",
                "invalid_charset",
                "interior_nul",
                "empty_name",
            )
        ),
        _duplicate_legacy_attribute_name_mutation(),
    ],
)
def test_hdf5_detector_rejects_unvalidated_extension_heap_and_attribute_profiles(
    tmp_path, payload
):
    source = tmp_path / "metadata-profile-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        _legacy_root_tree_sibling_mutation(left=96),
        _legacy_root_tree_sibling_mutation(left=136),
        _legacy_root_tree_sibling_mutation(left=840),
        _legacy_root_tree_sibling_mutation(left=840, child_right=136),
        _legacy_root_tree_sibling_mutation(right=840, child_left=136),
    ],
)
def test_hdf5_detector_rejects_forged_legacy_root_tree_siblings(tmp_path, payload):
    source = tmp_path / "tree-sibling-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        *(_legacy_root_tree_key_mutation(first_key=key) for key in (7, 8, 88)),
        *(_legacy_root_tree_key_mutation(final_key=key) for key in (0, 7, 9, 80, 88)),
        *(
            _legacy_ordered_group_mutation(mutation)
            for mutation in (
                "snod_duplicate",
                "snod_unsorted",
                "non_greatest_boundary",
                "duplicate_text_boundary_offset",
            )
        ),
        _unequal_width_snod_name_mutation(),
        _legacy_heap_empty_sentinel_mutation(),
    ],
)
def test_hdf5_detector_rejects_forged_legacy_group_tree_keys_and_snod_order(
    tmp_path, payload
):
    source = tmp_path / "legacy-tree-key-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_hdf5_detector_preserves_known_fail_if_unknown_message_flag(tmp_path):
    source = tmp_path / "known-message-fail-if-unknown.h5"
    source.write_bytes(
        _latest_child_message_mutation(message_type=0x02, message_flags=0x80)
    )

    result = FormatRegistry().detect(
        source, filename=source.name, declared_media_type=None
    )

    assert result.format_id == "hdf5"


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_checksum",
        "bad_signature",
        "root_overlap",
        "self_cycle",
        "unknown_message",
        "short_chunk",
    ],
)
def test_hdf5_detector_rejects_invalid_v2_object_header_continuations(
    tmp_path, mutation
):
    source = tmp_path / f"v2-continuation-{mutation}.h5"
    source.write_bytes(_v2_continuation_mutation(mutation))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    ("message_type", "message_flags"),
    [(0x00FE, 0x00), (0x0011, 0x02)],
)
def test_hdf5_detector_validates_v1_child_message_type_and_flags(
    tmp_path, message_type, message_flags
):
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    payload[816:818] = message_type.to_bytes(2, "little")
    payload[820] = message_flags
    source = tmp_path / "v1-child-message-attack.h5"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_parquet_detector_rejects_13_byte_stop_only_footer(tmp_path):
    source = tmp_path / "attack.parquet"
    source.write_bytes(b"PAR1\x00\x01\x00\x00\x00PAR1")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_parquet_detector_rejects_zeroed_referenced_page_stream(tmp_path):
    payload = bytearray(VALID_PARQUET_ONE_ROW)
    payload[4:54] = b"\x00" * 50
    source = tmp_path / "zero-pages.parquet"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_parquet_detector_verifies_page_crc_when_writer_supplies_it(tmp_path):
    payload = bytearray(VALID_PARQUET_DATA_PAGE_V2_CRC)
    metadata_start = len(payload) - 8 - int.from_bytes(payload[-8:-4], "little")
    payload[metadata_start - 1] ^= 1
    source = tmp_path / "bad-page-crc.parquet"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_parquet_detector_rejects_unknown_page_encoding_enum(tmp_path):
    payload = bytearray(VALID_PARQUET_ONE_ROW)
    payload[14] = 0x7E
    source = tmp_path / "unknown-dictionary-encoding.parquet"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        "nonzero_empty_chunk",
        "rows_without_groups",
        "nonzero_empty_dictionary",
    ],
)
def test_parquet_detector_rejects_malformed_empty_metadata(tmp_path, mutation):
    if mutation == "nonzero_empty_chunk":
        payload = bytearray(VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP)
        assert payload[57:59] == b"\x16\x00"
        payload[58] = 0x02
    elif mutation == "rows_without_groups":
        payload = bytearray(VALID_PARQUET_ZERO_ROWS_ZERO_ROW_GROUPS)
        assert payload[33:35] == b"\x16\x00"
        payload[34] = 0x02
    else:
        payload = bytearray(VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP)
        assert payload[11:13] == b"\x15\x00"
        payload[12] = 0x02
    source = tmp_path / f"{mutation}.parquet"
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize("mutation", ["schema_path", "schema_type", "chunk_overlap"])
def test_parquet_detector_rejects_cross_structure_metadata_conflicts(
    tmp_path, mutation
):
    if mutation == "chunk_overlap":
        payload = bytearray(VALID_PARQUET_TWO_ROW_GROUPS)
        metadata_start = len(payload) - 8 - int.from_bytes(payload[-8:-4], "little")
        marker = b"\x26\xf8\x03"
        marker_offsets = [
            offset
            for offset in range(metadata_start, len(payload) - 8)
            if payload.startswith(marker, offset)
        ]
        assert len(marker_offsets) == 2
        # Move the second row group's chunk start from 252 to 240 while
        # preserving its two-byte Compact Thrift varint. It then overlaps the
        # first chunk's [4, 252) range without crossing the footer boundary.
        payload[marker_offsets[0] + 1 : marker_offsets[0] + 3] = b"\xe0\x03"
    else:
        payload = bytearray(VALID_PARQUET_ONE_ROW)
        if mutation == "schema_path":
            schema_name_offset = payload.index(b"value")
            payload[schema_name_offset] = ord("x")
        else:
            schema_marker = b"\x15\x04\x25\x02\x18\x05value"
            schema_marker_offset = payload.index(schema_marker)
            payload[schema_marker_offset + 1] = 0x02

    source = tmp_path / f"{mutation}.parquet"
    source.write_bytes(payload)
    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_parquet_detector_bounds_schema_group_depth(tmp_path):
    source = tmp_path / "deep-schema.parquet"
    source.write_bytes(_deeply_nested_parquet(40))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_detector_rejects_signature_extension_mismatch(tmp_path):
    source = tmp_path / "pretend.pdf"
    source.write_bytes(_ONE_PIXEL_PNG)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source, filename=source.name, declared_media_type="application/pdf"
        )

    assert raised.value.code == "library_format_mismatch"


def test_png_admission_persists_bounded_source_preview_facts(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(_ONE_PIXEL_PNG)

    result = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type="image/png",
    )

    assert result.container == {
        "validator": "image",
        "valid": True,
        "width": 1,
        "height": 1,
        "pixel_count": 1,
        "source_preview_profile": "png-static-v1",
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {"width": 32_769},
        {"width": 10_000, "height": 5_000},
        {"bit_depth": 4, "color_type": 2},
        {"compression": 1},
        {"filter_method": 1},
        {"interlace": 1},
    ),
)
def test_png_admission_rejects_unsafe_source_preview_profiles(
    tmp_path,
    overrides: dict[str, int],
):
    source = tmp_path / "unsafe.png"
    source.write_bytes(_png_with_ihdr(**overrides))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="image/png",
        )

    assert raised.value.code == "library_container_invalid"


def test_jpeg_admission_persists_bounded_source_preview_facts(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(_baseline_jpeg())

    result = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type="image/jpeg",
    )

    assert result.container == {
        "validator": "image",
        "valid": True,
        "width": 24,
        "height": 12,
        "pixel_count": 288,
        "source_preview_profile": "jpeg-baseline-v1",
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {"width": 32_769},
        {"width": 10_000, "height": 5_000},
        {"precision": 12},
        {"components": 4},
        {"marker": 0xC2},
        {"marker": 0xC9},
    ),
)
def test_jpeg_admission_rejects_unsafe_source_preview_profiles(
    tmp_path,
    overrides: dict[str, int],
):
    source = tmp_path / "unsafe.jpg"
    source.write_bytes(_jpeg_with_frame(**overrides))

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="image/jpeg",
        )

    assert raised.value.code == "library_container_invalid"


def test_ooxml_requires_expected_container_members(tmp_path):
    valid = tmp_path / "valid.docx"
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
    generic = tmp_path / "generic.docx"
    with zipfile.ZipFile(generic, "w") as archive:
        archive.writestr("payload.txt", "not OOXML")

    result = FormatRegistry().detect(
        valid,
        filename=valid.name,
        declared_media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )
    assert result.format_id == "docx"
    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            generic,
            filename=generic.name,
            declared_media_type="application/zip",
        )
    assert raised.value.code == "unsupported_library_archive"


def test_capability_conversion_reflects_installed_provider_set():
    capabilities = FormatRegistry().capabilities(
        converter_formats={"markdown", "docx"}, max_bytes=1234
    )
    by_id = {capability.format_id: capability for capability in capabilities}

    assert by_id["markdown"].conversion == "available"
    assert by_id["docx"].conversion == "available"
    assert by_id["pdf"].conversion == "stored_unconverted"
    assert by_id["pdf"].upload is True
    assert by_id["pdf"].max_bytes == 1234


def test_filename_normalization_strips_paths_and_rejects_controls():
    assert normalize_library_filename(r"C:\fake\notes.md") == "notes.md"
    with pytest.raises(LibraryFormatError):
        normalize_library_filename("bad\x00name.md")


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("fake.pdf", b"%PDF-not-a-real-document"),
        ("fake.jpg", b"\xff\xd8\xff"),
        ("fake.parquet", b"PAR1PAR1"),
    ],
)
def test_detector_rejects_truncated_magic_only_payloads(tmp_path, filename, payload):
    source = tmp_path / filename
    source.write_bytes(payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=filename,
            declared_media_type="application/octet-stream",
        )

    assert raised.value.code == "library_container_invalid"


def test_detector_accepts_bounded_structurally_valid_pdf(tmp_path):
    source = tmp_path / "document.pdf"
    source.write_bytes(_minimal_pdf())

    result = FormatRegistry().detect(
        source, filename=source.name, declared_media_type="application/pdf"
    )

    assert result.format_id == "pdf"
    assert result.container == {"validator": "pdf", "valid": True}


def test_detector_rejects_declared_media_type_conflict(tmp_path):
    source = tmp_path / "document.pdf"
    source.write_bytes(_minimal_pdf())

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="text/plain",
        )

    assert raised.value.code == "library_format_mismatch"


def test_text_requires_utf8_or_bom_marked_utf16(tmp_path):
    source = tmp_path / "binary.txt"
    source.write_bytes(b"\x80\x81")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_text_encoding_unsupported"

    source.write_bytes("hello".encode("utf-16"))
    assert (
        FormatRegistry()
        .detect(source, filename=source.name, declared_media_type="text/plain")
        .format_id
        == "text"
    )


@pytest.mark.parametrize(
    "content",
    [
        b"\x01\x02\x03",
        b"\xef\xbb\xbf\x01\x02\x03",
        "\x01\x02\x03".encode("utf-16"),
    ],
)
def test_text_admission_rejects_binary_like_controls_in_every_encoding(
    tmp_path, content
):
    source = tmp_path / "binary-like.txt"
    source.write_bytes(content)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_format_mismatch"


@pytest.mark.parametrize("member", ["../outside.txt", "/absolute.txt", "C:drive.txt"])
def test_ooxml_rejects_unsafe_archive_members(tmp_path, member):
    source = tmp_path / "unsafe.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(member, "unsafe")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(source, filename=source.name, declared_media_type=None)

    assert raised.value.code == "library_container_invalid"


def test_ooxml_rejects_invalid_xml_and_external_relationships(tmp_path):
    invalid = tmp_path / "invalid.docx"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", "<w:document")

    with pytest.raises(LibraryFormatError):
        FormatRegistry().detect(
            invalid, filename=invalid.name, declared_media_type=None
        )

    external = tmp_path / "external.docx"
    with zipfile.ZipFile(external, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" Type="test" '
            'Target="https://example.test" TargetMode="External" />'
            "</Relationships>",
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            external, filename=external.name, declared_media_type=None
        )

    assert raised.value.code == "library_container_invalid"


def test_xml_admission_rejects_utf16_dtd_and_entity_declarations(tmp_path):
    unsafe_xml = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE document [<!ENTITY payload "expanded">]>'
        "<document>&payload;</document>"
    ).encode("utf-16")
    standalone = tmp_path / "unsafe.hwpml"
    standalone.write_bytes(unsafe_xml)

    with pytest.raises(LibraryFormatError) as standalone_error:
        FormatRegistry().detect(
            standalone,
            filename=standalone.name,
            declared_media_type="application/xml",
        )

    assert standalone_error.value.code == "library_container_invalid"
    assert str(standalone_error.value) == (
        "The XML document contains unsafe declarations."
    )

    packaged = tmp_path / "unsafe-utf16.docx"
    with zipfile.ZipFile(packaged, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", unsafe_xml)

    with pytest.raises(LibraryFormatError) as packaged_error:
        FormatRegistry().detect(
            packaged,
            filename=packaged.name,
            declared_media_type=None,
        )

    assert packaged_error.value.code == "library_container_invalid"
    assert str(packaged_error.value) == "The document XML is unsafe."


def test_xml_admission_rejects_unknown_encoding_fail_closed(tmp_path):
    unknown_encoding = b'<?xml version="1.0" encoding="x-unknown"?><document />'
    standalone = tmp_path / "unknown-encoding.hwpml"
    standalone.write_bytes(unknown_encoding)

    with pytest.raises(LibraryFormatError) as standalone_error:
        FormatRegistry().detect(
            standalone,
            filename=standalone.name,
            declared_media_type="application/xml",
        )

    assert standalone_error.value.code == "library_container_invalid"
    assert str(standalone_error.value) == "The HWPML document is not valid XML."

    packaged = tmp_path / "unknown-encoding.docx"
    with zipfile.ZipFile(packaged, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", unknown_encoding)

    with pytest.raises(LibraryFormatError) as packaged_error:
        FormatRegistry().detect(
            packaged,
            filename=packaged.name,
            declared_media_type=None,
        )

    assert packaged_error.value.code == "library_container_invalid"
    assert str(packaged_error.value) == "The document XML is invalid."


def test_hwpml_admission_rejects_oversized_xml_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(library_formats_module, "_MAX_XML_BYTES", 32)
    source = tmp_path / "oversized.hwpml"
    source.write_text(f"<document>{'x' * 32}</document>", encoding="utf-8")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="application/xml",
        )

    assert raised.value.code == "library_container_limit"
    assert str(raised.value) == "The XML document exceeds the safety limit."


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload"),
    [
        ("_MAX_XML_DEPTH", 3, "<document><a><b><c /></b></a></document>"),
        ("_MAX_XML_NODES", 3, "<document><a /><b /><c /></document>"),
        ("_MAX_XML_ATTRIBUTES", 2, '<document a="1" b="2" c="3" />'),
        (
            "_MAX_XML_ATTRIBUTES_PER_ELEMENT",
            2,
            '<document a="1" b="2" c="3" />',
        ),
        (
            "_MAX_XML_ATTRIBUTE_BYTES",
            12,
            '<document attribute="oversized" />',
        ),
        ("_MAX_XML_TEXT_BYTES", 5, "<document>abc<child />def</document>"),
    ],
    ids=[
        "deep-tree",
        "wide-tree",
        "attribute-count",
        "wide-attribute-element",
        "attribute-name-and-value-bytes",
        "text-and-tail-bytes",
    ],
)
def test_hwpml_admission_rejects_xml_structure_limits(
    tmp_path,
    monkeypatch,
    limit_name: str,
    limit: int,
    payload: str,
):
    monkeypatch.setattr(library_formats_module, limit_name, limit)
    source = tmp_path / "hostile.hwpml"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="application/xml",
        )

    assert raised.value.code == "library_container_limit"
    assert str(raised.value) == "The XML document exceeds the safety limit."


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload"),
    [
        ("_MAX_XML_DEPTH", 3, "<styles><a><b><c /></b></a></styles>"),
        ("_MAX_XML_NODES", 3, "<styles><a /><b /><c /></styles>"),
        (
            "_MAX_XML_ATTRIBUTES",
            4,
            '<styles a="1" b="2" c="3" d="4" e="5" />',
        ),
        (
            "_MAX_XML_ATTRIBUTE_BYTES",
            512,
            f'<styles attribute="{"x" * 512}" />',
        ),
        ("_MAX_XML_TEXT_BYTES", 8, "<styles>abcde<child />fghi</styles>"),
    ],
    ids=[
        "deep-tree",
        "wide-tree",
        "attribute-count",
        "attribute-name-and-value-bytes",
        "text-and-tail-bytes",
    ],
)
def test_ooxml_admission_rejects_unreferenced_xml_structure_limits(
    tmp_path,
    monkeypatch,
    limit_name: str,
    limit: int,
    payload: str,
):
    monkeypatch.setattr(library_formats_module, limit_name, limit)
    source = tmp_path / "hostile-part.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", payload)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_limit"
    assert str(raised.value) == (
        "A document XML component exceeds the safety limit."
    )


def test_ooxml_admission_rejects_oversized_unreferenced_xml_part(
    tmp_path,
    monkeypatch,
):
    member_limit = max(len(_CONTENT_TYPES.encode()), len(_WORD_DOCUMENT.encode()))
    monkeypatch.setattr(library_formats_module, "_MAX_XML_BYTES", member_limit)
    source = tmp_path / "oversized-part.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(
            "word/styles.xml",
            f"<styles>{'x' * member_limit}</styles>",
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_limit"
    assert str(raised.value) == (
        "A document XML component exceeds the safety limit."
    )


@pytest.mark.parametrize("budget", ["bytes", "nodes"])
def test_ooxml_admission_enforces_cumulative_package_xml_budget(
    tmp_path,
    monkeypatch,
    budget: str,
):
    extra_xml = "<styles><style /><style /></styles>"
    if budget == "bytes":
        required_bytes = len(_CONTENT_TYPES.encode()) + len(_WORD_DOCUMENT.encode())
        monkeypatch.setattr(
            library_formats_module,
            "_MAX_OOXML_XML_BYTES",
            required_bytes,
        )
    else:
        monkeypatch.setattr(library_formats_module, "_MAX_OOXML_XML_NODES", 5)

    source = tmp_path / f"package-{budget}-limit.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", extra_xml)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_limit"
    assert str(raised.value) == (
        "The Office document XML exceeds the package safety limit."
    )


def test_ooxml_admission_preserves_benign_unreferenced_xml_parts(tmp_path):
    source = tmp_path / "benign-extra-xml.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(
            "word/styles.xml",
            '<styles xmlns="urn:example"><style id="normal">Visible</style></styles>',
        )

    detected = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type=None,
    )

    assert detected.format_id == "docx"


@pytest.mark.parametrize(
    "member",
    [
        "word/externalLinks/externalLink1.xml",
        "xl/connections.xml",
        "xl/queryTables/queryTable1.xml",
        "xl/model/model.xml",
        "xl/customData/customData1.xml",
        "xl/macroSheets/macroSheet1.xml",
        "word/ctrlProps/ctrlProp1.xml",
        "word/webExtensions/webExtension1.xml",
        "word/customUI/customUI.xml",
        "word/payload.vbs",
        "word/payload.exe",
        "word/payload.dll",
        "word/payload.js",
        "word/payload.html",
        "word/payload.svg",
        "word/payload.jar",
        "xl/query.iqy",
    ],
)
def test_ooxml_admission_rejects_active_member_parts_and_suffixes(tmp_path, member):
    source = tmp_path / "active-member.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(member, b"not executed")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == ("Embedded executable Office content is not supported.")


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.ms-word.document.macroEnabled.main+xml",
        "application/vnd.ms-office.vbaProject",
        "application/vnd.ms-office.activeX+xml",
        "application/javascript",
        "image/svg+xml",
        "text/html",
        "application/vnd.ms-office.webextension+xml",
    ],
)
def test_ooxml_admission_rejects_active_content_types(tmp_path, content_type):
    manifest = _CONTENT_TYPES.replace(
        "</Types>",
        '  <Override PartName="/word/active.bin" '
        f'ContentType="{content_type}" />\n</Types>',
    )
    source = tmp_path / "active-content-type.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", manifest)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/active.bin", b"not executed")

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == "The Office document content type is invalid."


@pytest.mark.parametrize(
    ("member_name", "member_xml"),
    [
        (
            "xl/workbook.xml",
            """
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <definedNames><definedName name="ExternalData">
                WEBSERVICE(&quot;https://example.invalid/data&quot;)
              </definedName></definedNames>
            </workbook>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c><f>RTD(&quot;unsafe.progid&quot;,,&quot;topic&quot;)</f></c></row></sheetData>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c><f>cmd|'/c calc'!A0</f></c></row></sheetData>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c><f>_xlfn.IMAGE(&quot;https://example.invalid/image&quot;)</f></c></row></sheetData>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c><f>CALL(&quot;native&quot;,&quot;entry&quot;,&quot;J&quot;)</f></c></row></sheetData>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c><f>SUM('[Book.xlsx]Sheet1'!A1)</f></c></row></sheetData>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <dataValidations count="1"><dataValidation type="custom" sqref="A1">
                <formula1>WEBSERVICE(&quot;https://example.invalid/data&quot;)</formula1>
              </dataValidation></dataValidations>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <dataValidations count="1"><dataValidation type="custom" sqref="A1">
                <formula2>_xlfn.IMAGE(&quot;https://example.invalid/image&quot;)</formula2>
              </dataValidation></dataValidations>
            </worksheet>
            """,
        ),
        (
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <conditionalFormatting sqref="A1">
                <cfRule type="expression" priority="1">
                  <formula>RTD(&quot;unsafe.progid&quot;,,&quot;topic&quot;)</formula>
                </cfRule>
              </conditionalFormatting>
            </worksheet>
            """,
        ),
    ],
    ids=[
        "defined-name-webservice",
        "cell-rtd",
        "cell-dde",
        "cell-image",
        "cell-native-call",
        "cell-external-workbook",
        "data-validation-formula1",
        "data-validation-formula2",
        "conditional-format-formula",
    ],
)
def test_xlsx_admission_rejects_external_capable_formulas(
    tmp_path, member_name, member_xml
):
    manifest = """
        <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Override PartName="/xl/workbook.xml"
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
        </Types>
    """
    workbook = """
        <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" />
    """
    source = tmp_path / "external-formula.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", manifest)
        archive.writestr(
            "xl/workbook.xml",
            member_xml if member_name == "xl/workbook.xml" else workbook,
        )
        if member_name != "xl/workbook.xml":
            archive.writestr(member_name, member_xml)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == (
        "External-capable spreadsheet formulas are not supported."
    )


@pytest.mark.parametrize(
    "relationship_suffix",
    [
        "attachedTemplate",
        "aFChunk",
        "control",
        "customUI",
        "externalLink",
        "oleObject",
        "package",
        "vbaProject",
        "webExtension",
    ],
)
def test_ooxml_admission_rejects_active_relationship_types(
    tmp_path, relationship_suffix
):
    source = tmp_path / "active-relationship.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", "<styles />")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/{relationship_suffix}" Target="styles.xml" />'
            "</Relationships>",
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == (
        "External Office document relationships are not supported."
    )


@pytest.mark.parametrize(
    "target",
    [
        "https%253A%252F%252Fexample.test%252Fpayload",
        "//example.test/payload",
        "styles.xml?download=1",
        "styles.xml#fragment",
        "styles.xml%2500ignored",
        "../../escape.xml",
        "..%252F..%252Fescape.xml",
        "..\\..\\escape.xml",
        "/absolute.xml",
    ],
    ids=[
        "double-encoded-url",
        "network-path",
        "query",
        "fragment",
        "double-encoded-nul",
        "root-escape",
        "double-encoded-root-escape",
        "backslash-root-escape",
        "absolute-path",
    ],
)
def test_ooxml_admission_rejects_unsafe_internal_relationship_targets(tmp_path, target):
    source = tmp_path / "unsafe-internal-target.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", "<styles />")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/styles" Target="{target}" TargetMode="Internal" />'
            "</Relationships>",
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == ("The Office document relationship target is invalid.")


def test_ooxml_admission_rejects_dangling_internal_relationship_target(tmp_path):
    source = tmp_path / "dangling-internal-target.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="missing.xml" TargetMode="Internal" />'
            "</Relationships>",
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == ("The Office document relationship target is missing.")


@pytest.mark.parametrize("target_mode", ["internal", "EXTERNAL", "Remote", ""])
def test_ooxml_admission_rejects_invalid_relationship_mode(tmp_path, target_mode):
    source = tmp_path / "invalid-relationship-mode.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", "<styles />")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="styles.xml" '
            f'TargetMode="{target_mode}" /></Relationships>',
        )

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type=None,
        )

    assert raised.value.code == "library_container_invalid"
    assert str(raised.value) == ("The Office document relationship mode is invalid.")


def test_ooxml_admission_preserves_benign_internal_relationships(tmp_path):
    source = tmp_path / "benign-relationship.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _WORD_DOCUMENT)
        archive.writestr("word/styles.xml", "<styles />")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="styles.xml" />'
            "</Relationships>",
        )

    detected = FormatRegistry().detect(
        source,
        filename=source.name,
        declared_media_type=None,
    )

    assert detected.format_id == "docx"


def test_ooxml_rejects_duplicate_members(tmp_path):
    duplicate = tmp_path / "duplicate.docx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("word/document.xml", _WORD_DOCUMENT)
            archive.writestr("word/document.xml", _WORD_DOCUMENT)

    with pytest.raises(LibraryFormatError) as raised:
        FormatRegistry().detect(
            duplicate, filename=duplicate.name, declared_media_type=None
        )

    assert raised.value.code == "library_container_invalid"


@pytest.mark.parametrize(
    "filename", ["NUL.txt", "notes.txt.", "stream:payload.txt", "safe\u202efile.txt"]
)
def test_filename_rejects_windows_and_bidi_ambiguities(filename):
    with pytest.raises(LibraryFormatError):
        normalize_library_filename(filename)


def test_instance_allowlisted_detector_extends_registry_without_core_dispatch(
    tmp_path,
):
    class ExampleDetector:
        detector_id = "example.detector.v1"
        priority = 10

        def detect(self, path, *, prefix, tail):
            del path, tail
            if prefix.startswith(b"EXAMPLE\0"):
                return DetectorMatch(
                    format_id="example",
                    detector_id=self.detector_id,
                    confidence=1.0,
                    container={"validator": "example", "valid": True},
                )
            return None

    descriptor = FormatDescriptor(
        "example",
        (".example",),
        ("application/x-example",),
        "data",
        "store_only",
        "metadata",
        "example",
        "example",
    )
    source = tmp_path / "sample.example"
    source.write_bytes(b"EXAMPLE\0payload")

    result = FormatRegistry((descriptor,), detectors=(ExampleDetector(),)).detect(
        source, filename=source.name, declared_media_type=None
    )

    assert result.format_id == "example"
    assert result.detector == "example.detector.v1"
