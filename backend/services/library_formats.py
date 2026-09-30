"""Canonical Library format registry and bounded source detection.

Extensions and declared media types are retained as provenance only.  Strong
signatures and format-specific container checks decide whether an upload may
enter normal Library storage.
"""

from __future__ import annotations

import codecs
import json
import math
import mimetypes
import posixpath
import re
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Protocol
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from backend.utils.ooxml_security import contains_active_spreadsheet_formula
from backend.utils.text_security import TextLikeness, library_text_encoding
from backend.utils.xml_security import contains_forbidden_xml_declaration

Category = Literal["text", "document", "image", "audio", "video", "data"]
Admission = Literal["convert", "store_only", "deny"]
PreviewKind = Literal[
    "markdown", "pdf", "image", "audio", "video", "table", "metadata"
]

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
_HDF4_SIGNATURE = b"\x0e\x03\x13\x01"
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_SNIFF_BYTES = 128 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_DECLARED_UNCOMPRESSED = 512 * 1024 * 1024
_MAX_ZIP_RATIO = 200
_MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_XML_NODES = 100_000
_MAX_XML_DEPTH = 64
_MAX_XML_ATTRIBUTES = 200_000
_MAX_XML_ATTRIBUTES_PER_ELEMENT = 256
_MAX_XML_ATTRIBUTE_BYTES = 16 * 1024 * 1024
_MAX_XML_TEXT_BYTES = 32 * 1024 * 1024
_MAX_OOXML_XML_BYTES = 128 * 1024 * 1024
_MAX_OOXML_XML_NODES = 500_000
_MAX_OOXML_XML_ATTRIBUTES = 1_000_000
_MAX_OOXML_XML_ATTRIBUTE_BYTES = 64 * 1024 * 1024
_MAX_OOXML_XML_TEXT_BYTES = 128 * 1024 * 1024
_OOXML_DANGEROUS_MEMBER_PARTS = frozenset(
    {
        "activex",
        "ctrlprops",
        "customdata",
        "customui",
        "dataconnections",
        "embeddings",
        "externaldata",
        "externallinks",
        "macrosheets",
        "model",
        "oleobjects",
        "querytables",
        "webextensions",
    }
)
_OOXML_DANGEROUS_MEMBER_SUFFIXES = (
    "vbaproject.bin",
    "connections.xml",
    ".bat",
    ".cjs",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".hta",
    ".htm",
    ".html",
    ".jar",
    ".dqy",
    ".iqy",
    ".odc",
    ".oqy",
    ".rqy",
    ".cub",
    ".js",
    ".jse",
    ".mjs",
    ".msi",
    ".ps1",
    ".scr",
    ".svg",
    ".vbe",
    ".vbs",
    ".wsf",
    ".wsh",
    ".xhtml",
)
_OOXML_DANGEROUS_CONTENT_TYPE_MARKERS = (
    "macroenabled",
    "vbaproject",
    "activex",
    "java-archive",
    "javascript",
    "svg+xml",
    "text/html",
    "webextension",
    "customui",
    "connections+xml",
    "querytable+xml",
    "dataconnection",
    "datamodel",
    "datamashup",
    "customdata",
    "externaldata",
)
_OOXML_DANGEROUS_RELATIONSHIP_SUFFIXES = frozenset(
    {
        "attachedtemplate",
        "afchunk",
        "control",
        "connections",
        "customdata",
        "customui",
        "dataconnection",
        "datamodel",
        "externaldata",
        "externallink",
        "model",
        "oleobject",
        "package",
        "querytable",
        "vbaproject",
        "webquery",
        "webextension",
        "webextensiontaskpanes",
    }
)
_MAX_MEDIA_TAG_BYTES = 4 * 1024 * 1024
_MAX_MEDIA_CONTAINER_ENTRIES = 100_000
_MAX_MEDIA_DURATION_SECONDS = 24 * 60 * 60
MAX_SOURCE_PREVIEW_IMAGE_DIMENSION = 32_768
MAX_SOURCE_PREVIEW_IMAGE_PIXELS = 40_000_000
SOURCE_PREVIEW_IMAGE_PROFILES = {
    "jpeg": "jpeg-baseline-v1",
    "png": "png-static-v1",
}
SOURCE_PREVIEW_MEDIA_PROFILES = {
    "mp3": frozenset({"mpeg-audio-v1"}),
    "ogg": frozenset({"ogg-audio-v1"}),
    # wav-pcm-v1 is retained for already-admitted versions. New uploads use
    # the passive container profile so browser codec support stays a render
    # concern rather than an upload gate.
    "wav": frozenset({"wav-container-v1", "wav-pcm-v1"}),
    "mp4": frozenset({"iso-bmff-video-v1"}),
    "webm": frozenset({"webm-video-v1"}),
}
_MAX_MP4_BOX_DEPTH = 16
_MAX_MP4_METADATA_BYTES = 8 * 1024 * 1024
_MAX_WEBM_TRACKS_BYTES = 4 * 1024 * 1024
_MAX_PARQUET_METADATA_BYTES = 16 * 1024 * 1024
_MAX_PARQUET_COLLECTION_ITEMS = 100_000
_MAX_PARQUET_THRIFT_DEPTH = 32
_MAX_PARQUET_THRIFT_VALUES = 1_000_000
_MAX_PARQUET_SCHEMA_NAME_BYTES = 64 * 1024
_MAX_PARQUET_PAGE_HEADER_BYTES = 1024 * 1024
_MAX_PARQUET_PAGES_PER_CHUNK = 100_000
_MAX_HDF5_USERBLOCK_BYTES = 64 * 1024 * 1024
_MAX_HDF5_OBJECT_HEADER_BYTES = 1024 * 1024
_MAX_HDF5_OBJECT_MESSAGES = 100_000
_MAX_HDF5_OBJECTS = 10_000
_MAX_HDF5_BTREE_NODES = 100_000
_MAX_HDF5_BTREE_DEPTH = 32
_MAX_HDF5_VALIDATED_METADATA_BYTES = 64 * 1024 * 1024
_MAX_HDF5_LINK_NAME_BYTES = 64 * 1024
_HDF5_INTEGER_WIDTHS = {2, 4, 8, 16, 32}
_HDF5_ADMITTED_OBJECT_MESSAGE_TYPES = frozenset(
    {
        0x0000,  # NIL
        0x0001,  # dataspace
        0x0002,  # compact-group link info
        0x0003,  # bounded atomic datatype subset
        0x0004,  # legacy fill value
        0x0005,  # fill value
        0x0006,  # compact hard/soft link
        0x0008,  # compact/contiguous data layout
        0x000A,  # compact-group constants
        0x000C,  # compact attribute
        0x0010,  # object-header continuation
        0x0011,  # legacy group symbol table
        0x0015,  # compact-attribute info
    }
)
_HDF5_MESSAGE_FLAG_SHARED = 0x02
_HDF5_MESSAGE_FLAG_DO_NOT_SHARE = 0x04
_HDF5_MESSAGE_FLAG_MARK_IF_UNKNOWN = 0x10
_HDF5_MESSAGE_FLAG_WAS_UNKNOWN = 0x20
_HDF5_MESSAGE_FLAG_SHAREABLE = 0x40
_HDF5_SHAREABLE_MESSAGE_TYPES = frozenset(
    {0x0001, 0x0003, 0x0004, 0x0005, 0x000B, 0x000C}
)
_THRIFT_STOP = 0
_THRIFT_BOOLEAN_TRUE = 1
_THRIFT_BOOLEAN_FALSE = 2
_THRIFT_I8 = 3
_THRIFT_I16 = 4
_THRIFT_I32 = 5
_THRIFT_I64 = 6
_THRIFT_DOUBLE = 7
_THRIFT_BINARY = 8
_THRIFT_LIST = 9
_THRIFT_SET = 10
_THRIFT_MAP = 11
_THRIFT_STRUCT = 12
_THRIFT_UUID = 13
_THRIFT_VALUE_TYPES = frozenset(range(_THRIFT_BOOLEAN_TRUE, _THRIFT_UUID + 1))
_PARQUET_ENCODINGS = frozenset(range(10))
_PARQUET_CODECS = frozenset(range(8))
_TEXT_BACKED_FORMATS = {
    "markdown",
    "text",
    "html",
    "javascript",
    "csv",
    "tsv",
    "json",
    "yaml",
}
_CUSTOM_ACTIVE_MEDIA_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/java-archive",
        "application/vnd.microsoft.portable-executable",
        "application/x-dosexec",
        "application/x-executable",
        "application/x-httpd-php",
        "application/x-msdownload",
        "application/x-powershell",
        "application/x-sh",
        "image/svg+xml",
        "text/html",
        "text/javascript",
        "text/x-python",
        "text/x-script.python",
        "text/x-shellscript",
    }
)
_CUSTOM_ARCHIVE_MEDIA_TYPES = frozenset(
    {
        "application/gzip",
        "application/vnd.ms-cab-compressed",
        "application/vnd.rar",
        "application/x-7z-compressed",
        "application/x-bzip2",
        "application/x-compressed-tar",
        "application/x-cpio",
        "application/x-gzip",
        "application/x-rar-compressed",
        "application/x-iso9660-image",
        "application/x-lz4",
        "application/x-tar",
        "application/x-xz",
        "application/zstd",
        "application/zip",
    }
)
_CUSTOM_ARCHIVE_MAGICS = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",  # gzip
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"7z\xbc\xaf'\x1c",
    b"Rar!\x1a\x07\x00",
    b"Rar!\x1a\x07\x01\x00",
    b"MSCF",  # Microsoft cabinet
    b"!<arch>\n",  # Unix ar
    b"\x28\xb5\x2f\xfd",  # zstd frame
    b"\x04\x22\x4d\x18",  # LZ4 frame
    b"070701",  # cpio new ASCII
    b"070702",  # cpio new ASCII with CRC
    b"070707",  # cpio portable ASCII
)
_CUSTOM_EXECUTABLE_MAGICS = (
    b"MZ",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",  # Java class / Mach-O universal.
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\x00asm",  # WebAssembly.
    b"dex\n",  # Android Dalvik executable.
)
_WINDOWS_RESERVED_STEMS = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)),
}


class LibraryFormatError(ValueError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True, slots=True)
class FormatDescriptor:
    format_id: str
    extensions: tuple[str, ...]
    media_types: tuple[str, ...]
    category: Category
    admission: Admission
    preview_kind: PreviewKind
    default_limits_profile: str
    container_validator: str | None = None


@dataclass(frozen=True, slots=True)
class DetectionResult:
    format_id: str
    detected_media_type: str
    original_filename: str
    extension: str
    declared_media_type: str | None
    confidence: float
    detector: str = "dtxwiki-signature-v1"
    warnings: tuple[str, ...] = ()
    container: dict[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "detector": self.detector,
            "confidence": self.confidence,
            "extension": self.extension,
            "declared_media_type": self.declared_media_type,
            "detected_media_type": self.detected_media_type,
            "warnings": list(self.warnings),
            "container": dict(self.container),
        }


@dataclass(frozen=True, slots=True)
class DetectorMatch:
    """A bounded admission-detector result.

    Detector instances are composed explicitly by the application.  They are
    never discovered from uploaded files, Python entry points, or arbitrary
    directories because detector code runs on the upload trust boundary.
    """

    format_id: str | None
    detector_id: str
    confidence: float
    container: Mapping[str, object] = field(default_factory=dict)


class FormatDetector(Protocol):
    """Trusted, instance-allowlisted extension point for new binary formats."""

    detector_id: str
    priority: int

    def detect(
        self, path: Path, *, prefix: bytes, tail: bytes
    ) -> DetectorMatch | None: ...


class BuiltinSignatureDetector:
    detector_id = "dtxwiki-signature-v1"
    priority = 1_000

    def detect(self, path: Path, *, prefix: bytes, tail: bytes) -> DetectorMatch | None:
        format_id, container = _detect_builtin_signature(path, prefix, tail)
        if format_id is None and not container:
            return None
        return DetectorMatch(
            format_id=format_id,
            detector_id=self.detector_id,
            confidence=1.0 if format_id is not None else 0.9,
            container=container,
        )


@dataclass(frozen=True, slots=True)
class FormatCapability:
    format_id: str
    category: Category
    extensions: tuple[str, ...]
    media_types: tuple[str, ...]
    upload: bool
    conversion: Literal["available", "stored_unconverted", "denied"]
    preview_kind: PreviewKind
    max_bytes: int


DEFAULT_FORMATS: tuple[FormatDescriptor, ...] = (
    FormatDescriptor(
        "markdown",
        (".md", ".markdown"),
        ("text/markdown", "text/x-markdown"),
        "text",
        "convert",
        "markdown",
        "text",
    ),
    FormatDescriptor(
        "text",
        (".txt",),
        ("text/plain",),
        "text",
        "convert",
        "markdown",
        "text",
    ),
    FormatDescriptor(
        "html",
        (".html", ".htm", ".xhtml"),
        ("text/html", "application/xhtml+xml"),
        "text",
        "convert",
        "markdown",
        "text",
    ),
    FormatDescriptor(
        "javascript",
        (".js", ".mjs", ".cjs"),
        (
            "text/javascript",
            "application/javascript",
            "text/ecmascript",
            "application/ecmascript",
        ),
        "text",
        "convert",
        "markdown",
        "text",
    ),
    FormatDescriptor(
        "csv",
        (".csv",),
        ("text/csv", "application/csv"),
        "data",
        "convert",
        "table",
        "structured",
    ),
    FormatDescriptor(
        "tsv",
        (".tsv",),
        ("text/tab-separated-values",),
        "data",
        "convert",
        "table",
        "structured",
    ),
    FormatDescriptor(
        "json",
        (".json",),
        ("application/json", "text/json"),
        "data",
        "convert",
        "markdown",
        "structured",
    ),
    FormatDescriptor(
        "yaml",
        (".yaml", ".yml"),
        ("application/yaml", "text/yaml", "application/x-yaml"),
        "data",
        "convert",
        "markdown",
        "structured",
    ),
    FormatDescriptor(
        "pdf",
        (".pdf",),
        ("application/pdf",),
        "document",
        "convert",
        "pdf",
        "document",
        "pdf",
    ),
    FormatDescriptor(
        "docx",
        (".docx",),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        "document",
        "convert",
        "markdown",
        "document",
        "ooxml",
    ),
    FormatDescriptor(
        "pptx",
        (".pptx",),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
        "document",
        "convert",
        "markdown",
        "document",
        "ooxml",
    ),
    FormatDescriptor(
        "xlsx",
        (".xlsx",),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
        "document",
        "convert",
        "table",
        "document",
        "ooxml",
    ),
    # These legacy compound-file descriptors remain visible so capability
    # clients can explain the boundary, but signature-only OLE detection cannot
    # distinguish or structurally validate DOC/PPT/XLS.  Keep admission closed
    # until a bounded CFB validator proves the directory/FAT and expected streams.
    FormatDescriptor(
        "doc",
        (".doc",),
        ("application/msword",),
        "document",
        "deny",
        "metadata",
        "legacy_document",
        "ole",
    ),
    FormatDescriptor(
        "ppt",
        (".ppt",),
        ("application/vnd.ms-powerpoint",),
        "document",
        "deny",
        "metadata",
        "legacy_document",
        "ole",
    ),
    FormatDescriptor(
        "xls",
        (".xls",),
        ("application/vnd.ms-excel",),
        "document",
        "deny",
        "metadata",
        "legacy_document",
        "ole",
    ),
    FormatDescriptor(
        "jpeg",
        (".jpg", ".jpeg"),
        ("image/jpeg",),
        "image",
        "convert",
        "image",
        "image",
        "image",
    ),
    FormatDescriptor(
        "png",
        (".png",),
        ("image/png",),
        "image",
        "convert",
        "image",
        "image",
        "image",
    ),
    # Header/trailer checks alone do not prove these secondary image bitstreams.
    # Keep them visible but closed until bounded decoders validate complete data.
    FormatDescriptor(
        "gif",
        (".gif",),
        ("image/gif",),
        "image",
        "deny",
        "image",
        "image",
        "image",
    ),
    FormatDescriptor(
        "webp",
        (".webp",),
        ("image/webp",),
        "image",
        "deny",
        "image",
        "image",
        "image",
    ),
    FormatDescriptor(
        "tiff",
        (".tif", ".tiff"),
        ("image/tiff",),
        "image",
        "deny",
        "image",
        "image",
        "image",
    ),
    # Media admission preserves a validated passive original. Browser playback
    # remains a separate capability and is decided from an exact-version source
    # presentation plus browser/runtime facts, never from filename alone.
    FormatDescriptor(
        "mp3",
        (".mp3",),
        ("audio/mpeg",),
        "audio",
        "store_only",
        "audio",
        "audio",
        "mp3",
    ),
    FormatDescriptor(
        "ogg",
        (".ogg", ".oga"),
        ("audio/ogg", "application/ogg"),
        "audio",
        "store_only",
        "audio",
        "audio",
        "ogg",
    ),
    FormatDescriptor(
        "wav",
        (".wav",),
        ("audio/wav", "audio/x-wav"),
        "audio",
        "store_only",
        "audio",
        "audio",
        "wav",
    ),
    FormatDescriptor(
        "mp4",
        (".mp4", ".m4v"),
        ("video/mp4", "video/x-m4v"),
        "video",
        "store_only",
        "video",
        "video",
        "mp4",
    ),
    FormatDescriptor(
        "webm",
        (".webm",),
        ("video/webm",),
        "video",
        "store_only",
        "video",
        "video",
        "webm",
    ),
    FormatDescriptor(
        "hdf5",
        (".h5", ".hdf5", ".hdf"),
        ("application/x-hdf5", "application/x-hdf"),
        "data",
        "convert",
        "metadata",
        "scientific_data",
        "hdf5",
    ),
    # HDF4 is a distinct container and its four-byte signature is not structural
    # validation.  HDF5 remains admitted through its bounded superblock parser.
    FormatDescriptor(
        "hdf4",
        (".hdf4", ".hdf"),
        ("application/x-hdf",),
        "data",
        "deny",
        "metadata",
        "scientific_data",
        "hdf4",
    ),
    FormatDescriptor(
        "parquet",
        (".parquet",),
        ("application/vnd.apache.parquet", "application/x-parquet"),
        "data",
        "convert",
        "table",
        "scientific_data",
        "parquet",
    ),
    # Korean legacy/container formats also fail closed until their full format
    # structures can be validated rather than inferred from a suffix, an OLE
    # signature, one ZIP member, or arbitrary well-formed XML.
    FormatDescriptor(
        "hwp",
        (".hwp",),
        ("application/x-hwp", "application/haansofthwp"),
        "document",
        "deny",
        "metadata",
        "korean_document",
        "ole",
    ),
    FormatDescriptor(
        "hwpx",
        (".hwpx",),
        ("application/vnd.hancom.hwpx+zip",),
        "document",
        "deny",
        "metadata",
        "korean_document",
        "hwpx",
    ),
    FormatDescriptor(
        "hwpml",
        (".hwpml",),
        ("application/xml", "text/xml"),
        "document",
        "deny",
        "metadata",
        "korean_document",
        "xml",
    ),
)

# Workspace policy uses compact legacy IDs for the two original text formats.
# Every other policy ID is the canonical registry ID.  Keep this projection
# beside DEFAULT_FORMATS so adding an admitted descriptor automatically makes
# it part of the package-owned capability catalog; deployment-level feature
# gating, when required, belongs outside Workspace alias policy.
LIBRARY_FORMAT_POLICY_ALIASES = {
    "markdown": "md",
    "text": "txt",
}
LIBRARY_SUPPORTED_POLICY_FORMAT_IDS = frozenset(
    LIBRARY_FORMAT_POLICY_ALIASES.get(descriptor.format_id, descriptor.format_id)
    for descriptor in DEFAULT_FORMATS
)


class FormatRegistry:
    def __init__(
        self,
        descriptors: tuple[FormatDescriptor, ...] = DEFAULT_FORMATS,
        *,
        detectors: tuple[FormatDetector, ...] = (),
    ):
        by_id: dict[str, FormatDescriptor] = {}
        by_extension: dict[str, list[FormatDescriptor]] = {}
        by_media_type: dict[str, list[FormatDescriptor]] = {}
        for descriptor in descriptors:
            if descriptor.format_id in by_id:
                raise ValueError(f"duplicate format id: {descriptor.format_id}")
            by_id[descriptor.format_id] = descriptor
            for extension in descriptor.extensions:
                normalized = extension.lower()
                by_extension.setdefault(normalized, []).append(descriptor)
            for media_type in descriptor.media_types:
                by_media_type.setdefault(media_type.lower(), []).append(descriptor)
        self._descriptors = by_id
        self._by_extension = by_extension
        self._by_media_type = by_media_type
        configured_detectors: tuple[FormatDetector, ...] = (
            BuiltinSignatureDetector(),
            *detectors,
        )
        detector_ids: set[str] = set()
        detector_priorities: set[int] = set()
        for detector in configured_detectors:
            if not detector.detector_id or detector.detector_id in detector_ids:
                raise ValueError(f"duplicate format detector: {detector.detector_id}")
            if detector.priority in detector_priorities:
                raise ValueError(
                    f"duplicate format detector priority: {detector.priority}"
                )
            detector_ids.add(detector.detector_id)
            detector_priorities.add(detector.priority)
        self._detectors = tuple(
            sorted(configured_detectors, key=lambda item: item.priority, reverse=True)
        )

    def descriptor(
        self,
        format_id: str,
        *,
        custom_descriptors: tuple[FormatDescriptor, ...] = (),
        builtin_descriptor_overrides: tuple[FormatDescriptor, ...] = (),
    ) -> FormatDescriptor:
        effective_descriptors = _validated_builtin_descriptor_overrides(
            builtin_descriptor_overrides,
            built_in_descriptors=self._descriptors,
        )
        try:
            return effective_descriptors[format_id]
        except KeyError as exc:
            effective_extensions = frozenset(
                extension
                for descriptor in effective_descriptors.values()
                for extension in descriptor.extensions
            )
            custom = {
                descriptor.format_id: descriptor
                for descriptor in _validated_custom_descriptors(
                    custom_descriptors,
                    built_in_extensions=effective_extensions,
                    built_in_ids=frozenset(self._descriptors),
                )
            }.get(format_id)
            if custom is not None:
                return custom
            raise LibraryFormatError(
                "unsupported_library_upload", "Unsupported Library file format."
            ) from exc

    def descriptors(
        self,
        *,
        builtin_descriptor_overrides: tuple[FormatDescriptor, ...] = (),
    ) -> tuple[FormatDescriptor, ...]:
        return tuple(
            _validated_builtin_descriptor_overrides(
                builtin_descriptor_overrides,
                built_in_descriptors=self._descriptors,
            ).values()
        )

    def accepted_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_extension))

    def accepted_media_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_media_type))

    def capabilities(
        self,
        *,
        converter_formats: set[str],
        max_bytes: int,
    ) -> tuple[FormatCapability, ...]:
        capabilities = []
        for descriptor in self._descriptors.values():
            if descriptor.admission == "deny":
                conversion: Literal["available", "stored_unconverted", "denied"] = (
                    "denied"
                )
            elif (
                descriptor.admission == "convert"
                and descriptor.format_id in converter_formats
            ):
                conversion = "available"
            else:
                conversion = "stored_unconverted"
            capabilities.append(
                FormatCapability(
                    format_id=descriptor.format_id,
                    category=descriptor.category,
                    extensions=descriptor.extensions,
                    media_types=descriptor.media_types,
                    upload=descriptor.admission != "deny",
                    conversion=conversion,
                    preview_kind=descriptor.preview_kind,
                    max_bytes=max_bytes,
                )
            )
        return tuple(capabilities)

    def detect(
        self,
        path: Path,
        *,
        filename: str,
        declared_media_type: str | None,
        custom_descriptors: tuple[FormatDescriptor, ...] = (),
        builtin_descriptor_overrides: tuple[FormatDescriptor, ...] = (),
    ) -> DetectionResult:
        clean_name = normalize_library_filename(filename)
        extension = Path(clean_name).suffix.lower()
        effective_descriptors = _validated_builtin_descriptor_overrides(
            builtin_descriptor_overrides,
            built_in_descriptors=self._descriptors,
        )
        by_extension: dict[str, list[FormatDescriptor]] = {}
        for descriptor in effective_descriptors.values():
            for effective_extension in descriptor.extensions:
                by_extension.setdefault(effective_extension, []).append(descriptor)
        extension_candidates = by_extension.get(extension, [])
        custom_by_extension: dict[str, FormatDescriptor] = {}
        for descriptor in _validated_custom_descriptors(
            custom_descriptors,
            built_in_extensions=frozenset(by_extension),
            built_in_ids=frozenset(self._descriptors),
        ):
            for custom_extension in descriptor.extensions:
                if custom_extension in custom_by_extension:
                    raise ValueError(
                        f"duplicate custom format extension: {custom_extension}"
                    )
                custom_by_extension[custom_extension] = descriptor
        custom_descriptor = custom_by_extension.get(extension)
        if not extension_candidates and custom_descriptor is None:
            raise LibraryFormatError(
                "unsupported_library_upload",
                "This file extension is not supported by Library.",
            )

        prefix, tail = _read_signature_windows(path)
        custom_declared_media_type = (
            _normalize_media_type(declared_media_type)
            if custom_descriptor is not None
            else None
        )
        if custom_descriptor is not None:
            _validate_custom_store_only_content(
                prefix,
                declared_media_type=custom_declared_media_type,
            )
        detector_match = self._detect_with_allowlisted_detectors(path, prefix, tail)
        signature_format = detector_match.format_id if detector_match else None
        container = dict(detector_match.container) if detector_match else {}
        if custom_descriptor is not None:
            if extension_candidates:
                raise ValueError(
                    f"custom extension conflicts with server catalog: {extension}"
                )
            if signature_format is not None or container:
                raise LibraryFormatError(
                    "library_custom_format_content_conflict",
                    "The file contains a known or structured format and cannot be "
                    "stored under this custom extension.",
                )
            return DetectionResult(
                format_id=custom_descriptor.format_id,
                detected_media_type="application/octet-stream",
                original_filename=clean_name,
                extension=extension,
                declared_media_type=custom_declared_media_type,
                confidence=0.5,
                detector="workspace-extension-store-only-v1",
                warnings=("custom_format_store_only",),
                container={
                    "validator": "workspace-extension-store-only-v1",
                    "valid": True,
                    "presentation": "attachment_only",
                },
            )
        detector_id = (
            detector_match.detector_id if detector_match else "dtxwiki-text-v1"
        )
        extension_ids = {candidate.format_id for candidate in extension_candidates}

        if signature_format is not None and signature_format not in extension_ids:
            raise LibraryFormatError(
                "library_format_mismatch",
                "The file contents do not match the filename extension.",
            )

        if signature_format is not None:
            descriptor = self.descriptor(
                signature_format,
                builtin_descriptor_overrides=builtin_descriptor_overrides,
            )
            confidence = detector_match.confidence if detector_match else 1.0
        elif container.get("validator") == "ole" and all(
            item.container_validator == "ole" for item in extension_candidates
        ):
            # The compound-file signature is strong, but distinguishing legacy
            # Office from HWP requires a full parser.  The validated extension
            # chooses only among the explicit OLE allowlist.
            descriptor = extension_candidates[0]
            confidence = 0.9
        elif extension_ids <= _TEXT_BACKED_FORMATS:
            _validate_text_file(path)
            descriptor = extension_candidates[0]
            confidence = 0.85
        elif extension == ".hwpml":
            _validate_xml_file(path)
            descriptor = self.descriptor(
                "hwpml",
                builtin_descriptor_overrides=builtin_descriptor_overrides,
            )
            confidence = 0.9
        else:
            raise LibraryFormatError(
                "library_container_invalid",
                "The file container or signature is invalid.",
            )

        declared = _normalize_media_type(declared_media_type)
        if declared and declared not in {
            "application/octet-stream",
            *descriptor.media_types,
        }:
            raise LibraryFormatError(
                "library_format_mismatch",
                "The declared media type does not match the validated file contents.",
            )

        return DetectionResult(
            format_id=descriptor.format_id,
            detected_media_type=descriptor.media_types[0]
            if descriptor.media_types
            else mimetypes.guess_type(clean_name)[0] or "application/octet-stream",
            original_filename=clean_name,
            extension=extension,
            declared_media_type=declared,
            confidence=confidence,
            detector=detector_id,
            warnings=(),
            container=container,
        )

    def _detect_with_allowlisted_detectors(
        self, path: Path, prefix: bytes, tail: bytes
    ) -> DetectorMatch | None:
        collected: list[DetectorMatch] = []
        for detector in self._detectors:
            match = detector.detect(path, prefix=prefix, tail=tail)
            if match is None:
                continue
            if (
                match.detector_id != detector.detector_id
                or not math.isfinite(match.confidence)
                or not 0.0 <= match.confidence <= 1.0
            ):
                raise LibraryFormatError(
                    "library_detector_invalid",
                    "A configured format detector returned invalid evidence.",
                )
            try:
                container_payload = json.dumps(
                    dict(match.container),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise LibraryFormatError(
                    "library_detector_invalid",
                    "A configured format detector returned invalid evidence.",
                ) from exc
            if len(container_payload) > 32 * 1024:
                raise LibraryFormatError(
                    "library_detector_invalid",
                    "A configured format detector returned excessive evidence.",
                )
            collected.append(match)
        matches = tuple(collected)
        detected_formats = {
            match.format_id for match in matches if match.format_id is not None
        }
        if len(detected_formats) > 1:
            raise LibraryFormatError(
                "library_format_conflict",
                "Multiple format detectors reported conflicting file contents.",
            )
        if not matches:
            return None
        if detected_formats:
            return next(match for match in matches if match.format_id is not None)
        return matches[0]


def normalize_library_filename(filename: str) -> str:
    normalized = unicodedata.normalize("NFC", filename or "").strip()
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not normalized or normalized in {".", ".."}:
        raise LibraryFormatError("invalid_library_filename", "A filename is required.")
    if _CONTROL_CHARACTERS.search(normalized):
        raise LibraryFormatError(
            "invalid_library_filename", "The filename contains control characters."
        )
    if (
        ":" in normalized
        or normalized.endswith(".")
        or any(character in _BIDI_CONTROLS for character in normalized)
        or len(normalized.encode("utf-8")) > 255
        or normalized.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS
    ):
        raise LibraryFormatError(
            "invalid_library_filename", "The filename is not safe for Library."
        )
    return normalized


def _normalize_media_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.split(";", 1)[0].strip().lower()
    return normalized or None


def _validated_builtin_descriptor_overrides(
    descriptors: tuple[FormatDescriptor, ...],
    *,
    built_in_descriptors: Mapping[str, FormatDescriptor],
) -> dict[str, FormatDescriptor]:
    """Apply extension-only overrides without widening package capabilities."""

    effective = dict(built_in_descriptors)
    seen_ids: set[str] = set()
    for descriptor in descriptors:
        package = built_in_descriptors.get(descriptor.format_id)
        if (
            package is None
            or descriptor.format_id in seen_ids
            or not descriptor.extensions
            or len(descriptor.extensions) != len(set(descriptor.extensions))
            or any(
                not extension.startswith(".")
                or extension != extension.casefold()
                for extension in descriptor.extensions
            )
            or descriptor
            != FormatDescriptor(
                format_id=package.format_id,
                extensions=descriptor.extensions,
                media_types=package.media_types,
                category=package.category,
                admission=package.admission,
                preview_kind=package.preview_kind,
                default_limits_profile=package.default_limits_profile,
                container_validator=package.container_validator,
            )
        ):
            raise ValueError("invalid built-in format descriptor override")
        seen_ids.add(descriptor.format_id)
        effective[descriptor.format_id] = descriptor

    package_owners: dict[str, set[str]] = {}
    effective_owners: dict[str, set[str]] = {}
    for format_id, package in built_in_descriptors.items():
        for extension in package.extensions:
            package_owners.setdefault(extension, set()).add(format_id)
        for extension in effective[format_id].extensions:
            effective_owners.setdefault(extension, set()).add(format_id)
    for extension, owners in effective_owners.items():
        if len(owners) > 1 and not owners.issubset(
            package_owners.get(extension, set())
        ):
            raise ValueError("conflicting built-in format descriptor override")
    return effective


def _validated_custom_descriptors(
    descriptors: tuple[FormatDescriptor, ...],
    *,
    built_in_extensions: frozenset[str],
    built_in_ids: frozenset[str],
) -> tuple[FormatDescriptor, ...]:
    seen_ids: set[str] = set()
    seen_extensions: set[str] = set()
    for descriptor in descriptors:
        if (
            not descriptor.format_id.startswith("custom-")
            or descriptor.format_id in built_in_ids
            or descriptor.format_id in seen_ids
            or descriptor.admission != "store_only"
            or descriptor.preview_kind != "metadata"
            or descriptor.category != "data"
            or descriptor.media_types != ("application/octet-stream",)
            or descriptor.container_validator != "workspace-extension-store-only"
            or not descriptor.extensions
        ):
            raise ValueError("invalid Workspace custom format descriptor")
        for extension in descriptor.extensions:
            if (
                extension in built_in_extensions
                or extension in seen_extensions
                or not extension.startswith(".")
                or extension != extension.casefold()
            ):
                raise ValueError("invalid Workspace custom format extension")
            seen_extensions.add(extension)
        seen_ids.add(descriptor.format_id)
    return descriptors


def _validate_custom_store_only_content(
    prefix: bytes,
    *,
    declared_media_type: str | None,
) -> None:
    """Reject executable/active bytes before opaque attachment-only storage.

    This intentionally does not claim to identify the custom format. It only
    establishes a conservative passive envelope after all known built-in
    signatures and structured containers have already been ruled out.
    """

    if declared_media_type in _CUSTOM_ARCHIVE_MEDIA_TYPES:
        raise LibraryFormatError(
            "library_custom_format_archive",
            "Archive content cannot be registered as a custom Library format.",
        )
    if any(prefix.startswith(magic) for magic in _CUSTOM_ARCHIVE_MAGICS) or (
        len(prefix) >= 262 and prefix[257:262] == b"ustar"
    ) or (
        len(prefix) >= 32_774 and prefix[32_769:32_774] == b"CD001"
    ):
        raise LibraryFormatError(
            "library_custom_format_archive",
            "Archive content cannot be registered as a custom Library format.",
        )
    if declared_media_type in _CUSTOM_ACTIVE_MEDIA_TYPES:
        raise LibraryFormatError(
            "library_custom_format_active_content",
            "Executable, scripted, or active Web content cannot be registered as "
            "a custom Library format.",
        )
    if any(prefix.startswith(magic) for magic in _CUSTOM_EXECUTABLE_MAGICS):
        raise LibraryFormatError(
            "library_custom_format_executable",
            "Executable content cannot be registered as a custom Library format.",
        )
    probe = prefix[:8192]
    if probe.startswith(codecs.BOM_UTF8):
        probe = probe[len(codecs.BOM_UTF8) :]
    folded = probe.replace(b"\x00", b"").lstrip().lower()
    if folded.startswith(b"#!"):
        raise LibraryFormatError(
            "library_custom_format_executable",
            "Script content cannot be registered as a custom Library format.",
        )
    active_markers = (
        b"<!doctype html",
        b"<html",
        b"<script",
        b"<svg",
    )
    if any(folded.startswith(marker) for marker in active_markers):
        raise LibraryFormatError(
            "library_custom_format_active_content",
            "Active Web content cannot be registered as a custom Library format.",
        )


def _read_signature_windows(path: Path) -> tuple[bytes, bytes]:
    with path.open("rb") as stream:
        prefix = stream.read(_MAX_SNIFF_BYTES)
        if path.stat().st_size <= 4096:
            return prefix, prefix
        stream.seek(-4096, 2)
        return prefix, stream.read(4096)


def _find_hdf5_signature(path: Path) -> int | None:
    file_size = path.stat().st_size
    offset = 0
    with path.open("rb") as stream:
        while (
            offset <= _MAX_HDF5_USERBLOCK_BYTES
            and offset + len(_HDF5_SIGNATURE) <= file_size
        ):
            stream.seek(offset)
            if stream.read(len(_HDF5_SIGNATURE)) == _HDF5_SIGNATURE:
                return offset
            offset = 512 if offset == 0 else offset * 2
    return None


def _detect_builtin_signature(
    path: Path, prefix: bytes, tail: bytes
) -> tuple[str | None, dict[str, object]]:
    if prefix.startswith(b"%PDF-"):
        _validate_pdf(path, prefix, tail)
        return "pdf", {"validator": "pdf", "valid": True}
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", _validate_png(path)
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpeg", _validate_jpeg(path, prefix, tail)
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        _validate_gif(path, prefix, tail)
        return "gif", {"validator": "image", "valid": True}
    if prefix.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        _validate_tiff(path, prefix)
        return "tiff", {"validator": "image", "valid": True}
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        _validate_webp(path, prefix)
        return "webp", {"validator": "image", "valid": True}
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE":
        return "wav", _validate_wav(path)
    if prefix.startswith(b"OggS"):
        return "ogg", _validate_ogg(path)
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return "webm", _validate_webm(path)
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "mp4", _validate_mp4(path)
    if prefix.startswith(b"ID3") or (
        len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    ):
        # MPEG frame sync is not unique (for example UTF-16LE starts FF FE).
        # Treat it as a match only after the complete frame stream validates.
        try:
            return "mp3", _validate_mp3(path)
        except LibraryFormatError:
            return None, {}
    if (hdf5_signature_offset := _find_hdf5_signature(path)) is not None:
        _validate_hdf5(path, hdf5_signature_offset)
        return "hdf5", {"validator": "hdf5", "valid": True}
    if prefix.startswith(_HDF4_SIGNATURE):
        if path.stat().st_size < 8:
            raise LibraryFormatError(
                "library_container_invalid", "The HDF4 header is truncated."
            )
        return "hdf4", {"validator": "hdf4", "valid": True}
    if prefix.startswith(b"PAR1"):
        _validate_parquet(path, tail)
        return "parquet", {"validator": "parquet", "valid": True}
    if prefix.startswith(_OLE_SIGNATURE):
        if path.stat().st_size < 512:
            raise LibraryFormatError(
                "library_container_invalid",
                "The compound document header is truncated.",
            )
        return None, {"validator": "ole", "valid": True}
    if prefix.startswith(b"PK\x03\x04"):
        return _detect_zip_container(path)
    return None, {}


def _validate_wav(path: Path) -> dict[str, object]:
    """Validate a bounded RIFF/WAVE envelope without gating on its codec."""

    file_size = path.stat().st_size
    if file_size < 44:
        raise LibraryFormatError(
            "library_container_invalid", "The WAV container is truncated."
        )
    chunk_count = 0
    fmt: tuple[int, int, int, int, int, int] | None = None
    data_bytes = 0
    with path.open("rb") as stream:
        header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise LibraryFormatError(
                "library_container_invalid", "The WAV signature is invalid."
            )
        if int.from_bytes(header[4:8], "little") + 8 != file_size:
            raise LibraryFormatError(
                "library_container_invalid", "The WAV RIFF size is invalid."
            )
        cursor = 12
        while cursor < file_size:
            chunk_header = stream.read(8)
            if len(chunk_header) != 8:
                raise LibraryFormatError(
                    "library_container_invalid", "The WAV chunk table is truncated."
                )
            chunk_count += 1
            if chunk_count > _MAX_MEDIA_CONTAINER_ENTRIES:
                raise LibraryFormatError(
                    "library_container_limit", "The WAV file has too many chunks."
                )
            chunk_id = chunk_header[:4]
            chunk_size = int.from_bytes(chunk_header[4:8], "little")
            cursor += 8
            padded_size = chunk_size + (chunk_size & 1)
            if chunk_size > file_size - cursor or padded_size > file_size - cursor:
                raise LibraryFormatError(
                    "library_container_invalid", "The WAV chunk size is invalid."
                )
            if chunk_id == b"fmt ":
                if fmt is not None or chunk_size < 16 or chunk_size > 64 * 1024:
                    raise LibraryFormatError(
                        "library_container_invalid", "The WAV format chunk is invalid."
                    )
                payload = stream.read(chunk_size)
                audio_format = int.from_bytes(payload[0:2], "little")
                channels = int.from_bytes(payload[2:4], "little")
                sample_rate = int.from_bytes(payload[4:8], "little")
                byte_rate = int.from_bytes(payload[8:12], "little")
                block_align = int.from_bytes(payload[12:14], "little")
                bits_per_sample = int.from_bytes(payload[14:16], "little")
                if (
                    audio_format == 0
                    or not 1 <= channels <= 32
                    or not 1_000 <= sample_rate <= 768_000
                    or not 0 < byte_rate <= 1_000_000_000
                    or not 0 < block_align <= 65_535
                    or not 0 <= bits_per_sample <= 64
                ):
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "The WAV format parameters are invalid.",
                    )
                if audio_format in {1, 3}:
                    allowed_bits = {8, 16, 24, 32} if audio_format == 1 else {32, 64}
                    expected_align = channels * ((bits_per_sample + 7) // 8)
                    if (
                        bits_per_sample not in allowed_bits
                        or block_align != expected_align
                        or byte_rate != sample_rate * block_align
                    ):
                        raise LibraryFormatError(
                            "library_container_invalid",
                            "The WAV PCM parameters are inconsistent.",
                        )
                fmt = (
                    audio_format,
                    channels,
                    sample_rate,
                    byte_rate,
                    block_align,
                    bits_per_sample,
                )
            elif chunk_id == b"data":
                data_bytes += chunk_size
                stream.seek(chunk_size, 1)
            else:
                stream.seek(chunk_size, 1)
            if chunk_size & 1:
                if stream.read(1) != b"\x00":
                    raise LibraryFormatError(
                        "library_container_invalid", "The WAV chunk padding is invalid."
                    )
            cursor += padded_size
    if fmt is None or data_bytes == 0:
        raise LibraryFormatError(
            "library_container_invalid", "The WAV audio payload is invalid."
        )
    if fmt[0] in {1, 3} and data_bytes % fmt[4]:
        raise LibraryFormatError(
            "library_container_invalid", "The WAV PCM payload is misaligned."
        )
    duration = data_bytes / fmt[3]
    if duration > _MAX_MEDIA_DURATION_SECONDS:
        raise LibraryFormatError(
            "library_container_limit", "The WAV duration exceeds the safety limit."
        )
    return {
        "validator": "wav-container-v1",
        "valid": True,
        "codec": (
            "pcm"
            if fmt[0] == 1
            else "ieee-float"
            if fmt[0] == 3
            else f"wave-format-0x{fmt[0]:04x}"
        ),
        "channels": fmt[1],
        "sample_rate": fmt[2],
        "duration_seconds": round(duration, 3),
    }


_MP3_BITRATES = {
    ("1", 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    ("1", 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    ("1", 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    ("2", 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    ("2", 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    ("2", 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}


def _validate_mp3(path: Path) -> dict[str, object]:
    """Validate an ID3 + constant-profile MPEG audio frame stream."""

    file_size = path.stat().st_size
    if file_size < 4:
        raise LibraryFormatError(
            "library_container_invalid", "The MP3 stream is truncated."
        )
    offset = 0
    end = file_size
    with path.open("rb") as stream:
        first = stream.read(10)
        if first.startswith(b"ID3"):
            if (
                len(first) != 10
                or first[3] not in {2, 3, 4}
                or first[4] == 0xFF
                or any(byte & 0x80 for byte in first[6:10])
            ):
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 ID3 header is invalid."
                )
            tag_size = sum(
                byte << shift
                for byte, shift in zip(
                    first[6:10],
                    (21, 14, 7, 0),
                    strict=True,
                )
            )
            if tag_size > _MAX_MEDIA_TAG_BYTES:
                raise LibraryFormatError(
                    "library_container_limit", "The MP3 metadata exceeds the limit."
                )
            offset = 10 + tag_size + (10 if first[3] == 4 and first[5] & 0x10 else 0)
            if offset >= file_size:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 audio payload is missing."
                )
        if file_size >= 128:
            stream.seek(file_size - 128)
            if stream.read(3) == b"TAG":
                end -= 128

        frame_count = 0
        duration = 0.0
        profile: tuple[str, int, int] | None = None
        while offset < end:
            stream.seek(offset)
            header_bytes = stream.read(4)
            if len(header_bytes) != 4:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 frame is truncated."
                )
            header = int.from_bytes(header_bytes, "big")
            if header >> 21 != 0x7FF:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 frame sync is invalid."
                )
            version_bits = (header >> 19) & 0x3
            layer_bits = (header >> 17) & 0x3
            bitrate_index = (header >> 12) & 0xF
            sample_index = (header >> 10) & 0x3
            padding = (header >> 9) & 0x1
            emphasis = header & 0x3
            if (
                version_bits == 1
                or layer_bits == 0
                or bitrate_index in {0, 15}
                or sample_index == 3
                or emphasis == 2
            ):
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The MP3 codec parameters are not in the approved profile.",
                )
            version = {3: "1", 2: "2", 0: "2.5"}[version_bits]
            layer = {3: 1, 2: 2, 1: 3}[layer_bits]
            bitrate_group = "1" if version == "1" else "2"
            bitrate = _MP3_BITRATES[(bitrate_group, layer)][bitrate_index] * 1000
            sample_rate = (44_100, 48_000, 32_000)[sample_index]
            if version == "2":
                sample_rate //= 2
            elif version == "2.5":
                sample_rate //= 4
            if layer == 1:
                frame_size = ((12 * bitrate) // sample_rate + padding) * 4
                samples = 384
            elif layer == 3 and version != "1":
                frame_size = (72 * bitrate) // sample_rate + padding
                samples = 576
            else:
                frame_size = (144 * bitrate) // sample_rate + padding
                samples = 1152
            if frame_size < 4 or frame_size > end - offset:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 frame length is invalid."
                )
            current_profile = (version, layer, sample_rate)
            if profile is not None and current_profile != profile:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP3 frame profile changes."
                )
            profile = current_profile
            frame_count += 1
            if frame_count > _MAX_MEDIA_CONTAINER_ENTRIES:
                raise LibraryFormatError(
                    "library_container_limit", "The MP3 stream has too many frames."
                )
            duration += samples / sample_rate
            if duration > _MAX_MEDIA_DURATION_SECONDS:
                raise LibraryFormatError(
                    "library_container_limit", "The MP3 duration exceeds the limit."
                )
            offset += frame_size
    if not frame_count or offset != end or profile is None:
        raise LibraryFormatError(
            "library_container_invalid", "The MP3 frame stream is invalid."
        )
    return {
        "validator": "mpeg-audio-v1",
        "valid": True,
        "codec": f"mpeg-{profile[0]}-layer-{profile[1]}",
        "sample_rate": profile[2],
        "frame_count": frame_count,
        "duration_seconds": round(duration, 3),
    }


def _build_ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = (
                ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if value & 0x80000000
                else (value << 1) & 0xFFFFFFFF
            )
        table.append(value)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


def _ogg_crc32(payload: bytes | bytearray) -> int:
    crc = 0
    for byte in payload:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _validate_ogg(path: Path) -> dict[str, object]:
    file_size = path.stat().st_size
    if file_size < 28:
        raise LibraryFormatError(
            "library_container_invalid", "The Ogg container is truncated."
        )
    page_count = 0
    serial: int | None = None
    expected_sequence = 0
    packet_continues = False
    first_packet = bytearray()
    codec: str | None = None
    saw_eos = False
    cursor = 0
    with path.open("rb") as stream:
        while cursor < file_size:
            header = stream.read(27)
            if len(header) != 27 or header[:4] != b"OggS" or header[4] != 0:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg page header is invalid."
                )
            flags = header[5]
            if flags & ~0x07:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg page flags are invalid."
                )
            page_serial = int.from_bytes(header[14:18], "little")
            sequence = int.from_bytes(header[18:22], "little")
            if serial is None:
                serial = page_serial
                if not flags & 0x02 or flags & 0x01:
                    raise LibraryFormatError(
                        "library_container_invalid", "The Ogg BOS page is invalid."
                    )
            elif page_serial != serial or flags & 0x02:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "Chained or multiplexed Ogg streams are not supported.",
                )
            if sequence != expected_sequence or bool(flags & 0x01) != packet_continues:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg page sequence is invalid."
                )
            expected_sequence += 1
            segment_count = header[26]
            lacing = stream.read(segment_count)
            if len(lacing) != segment_count:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg segment table is truncated."
                )
            body_size = sum(lacing)
            body = stream.read(body_size)
            if len(body) != body_size:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg page body is truncated."
                )
            page = bytearray(header + lacing + body)
            expected_crc = int.from_bytes(page[22:26], "little")
            page[22:26] = b"\x00\x00\x00\x00"
            if _ogg_crc32(page) != expected_crc:
                raise LibraryFormatError(
                    "library_container_invalid", "The Ogg page checksum is invalid."
                )
            page_count += 1
            if page_count > _MAX_MEDIA_CONTAINER_ENTRIES:
                raise LibraryFormatError(
                    "library_container_limit", "The Ogg stream has too many pages."
                )
            if codec is None:
                body_cursor = 0
                for lace in lacing:
                    if len(first_packet) + lace > 64 * 1024:
                        raise LibraryFormatError(
                            "library_container_limit",
                            "The Ogg codec header is too large.",
                        )
                    first_packet.extend(body[body_cursor : body_cursor + lace])
                    body_cursor += lace
                    if lace < 255:
                        if (
                            first_packet.startswith(b"OpusHead")
                            and len(first_packet) >= 19
                        ):
                            codec = "opus"
                        elif (
                            first_packet.startswith(b"\x01vorbis")
                            and len(first_packet) >= 30
                        ):
                            codec = "vorbis"
                        else:
                            # The page table, CRC, sequence and single-stream
                            # envelope remain authoritative admission checks.
                            # Unknown codec payloads are stored and left to the
                            # browser playback probe instead of being rejected.
                            codec = "unknown"
                        break
            packet_continues = bool(lacing and lacing[-1] == 255)
            saw_eos = bool(flags & 0x04)
            cursor += len(page)
            if saw_eos and cursor != file_size:
                raise LibraryFormatError(
                    "library_container_invalid", "Data follows the Ogg EOS page."
                )
    if codec is None or not saw_eos or packet_continues or cursor != file_size:
        raise LibraryFormatError(
            "library_container_invalid", "The Ogg stream is incomplete."
        )
    return {
        "validator": "ogg-audio-v1",
        "valid": True,
        "codec": codec,
        "page_count": page_count,
    }


def _mp4_box_bounds(data: bytes, offset: int, end: int) -> tuple[bytes, int, int]:
    if end - offset < 8:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 box header is truncated."
        )
    size = int.from_bytes(data[offset : offset + 4], "big")
    box_type = data[offset + 4 : offset + 8]
    header_size = 8
    if size == 1:
        if end - offset < 16:
            raise LibraryFormatError(
                "library_container_invalid", "The MP4 large box is truncated."
            )
        size = int.from_bytes(data[offset + 8 : offset + 16], "big")
        header_size = 16
    if size == 0 or size < header_size or size > end - offset:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 box size is invalid."
        )
    return box_type, offset + header_size, offset + size


_MP4_CONTAINER_BOXES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"edts",
    b"dinf",
    b"mvex",
}
_MP4_FORBIDDEN_BOXES = {b"encv", b"enca", b"sinf", b"schm", b"pssh"}


def _mp4_find_payloads(
    data: bytes,
    target: bytes,
    *,
    start: int = 0,
    end: int | None = None,
    depth: int = 0,
) -> list[bytes]:
    if depth > _MAX_MP4_BOX_DEPTH:
        raise LibraryFormatError(
            "library_container_limit", "The MP4 box nesting exceeds the limit."
        )
    end = len(data) if end is None else end
    offset = start
    found: list[bytes] = []
    count = 0
    while offset < end:
        box_type, payload_start, box_end = _mp4_box_bounds(data, offset, end)
        count += 1
        if count > _MAX_MEDIA_CONTAINER_ENTRIES or box_type in _MP4_FORBIDDEN_BOXES:
            raise LibraryFormatError(
                "library_container_invalid", "The MP4 box profile is not supported."
            )
        payload = data[payload_start:box_end]
        if box_type == target:
            found.append(payload)
        if box_type in _MP4_CONTAINER_BOXES:
            found.extend(
                _mp4_find_payloads(
                    data,
                    target,
                    start=payload_start,
                    end=box_end,
                    depth=depth + 1,
                )
            )
        offset = box_end
    if offset != end:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 box table is invalid."
        )
    return found


def _mp4_sample_entry_types(stsd: bytes) -> set[bytes]:
    if len(stsd) < 8:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 sample table is truncated."
        )
    entry_count = int.from_bytes(stsd[4:8], "big")
    if not 1 <= entry_count <= 1_024:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 sample count is invalid."
        )
    cursor = 8
    codecs: set[bytes] = set()
    for _ in range(entry_count):
        box_type, _, box_end = _mp4_box_bounds(stsd, cursor, len(stsd))
        if box_type in _MP4_FORBIDDEN_BOXES:
            raise LibraryFormatError(
                "library_container_invalid", "Encrypted MP4 samples are unsupported."
            )
        codecs.add(box_type)
        cursor = box_end
    if cursor != len(stsd):
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 sample table has trailing data."
        )
    return codecs


def _validate_mp4(path: Path) -> dict[str, object]:
    file_size = path.stat().st_size
    if file_size < 32:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 container is truncated."
        )
    top_count = 0
    saw_ftyp = False
    saw_mdat = False
    moov_payload: bytes | None = None
    cursor = 0
    with path.open("rb") as stream:
        while cursor < file_size:
            stream.seek(cursor)
            header = stream.read(16)
            if len(header) < 8:
                raise LibraryFormatError(
                    "library_container_invalid", "The MP4 box header is truncated."
                )
            size = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    raise LibraryFormatError(
                        "library_container_invalid", "The MP4 large box is truncated."
                    )
                size = int.from_bytes(header[8:16], "big")
                header_size = 16
            if size == 0 or size < header_size or size > file_size - cursor:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The MP4 top-level box size is invalid.",
                )
            top_count += 1
            if (
                top_count > _MAX_MEDIA_CONTAINER_ENTRIES
                or box_type in _MP4_FORBIDDEN_BOXES
            ):
                raise LibraryFormatError(
                    "library_container_invalid", "The MP4 box profile is unsupported."
                )
            if top_count == 1 and box_type != b"ftyp":
                raise LibraryFormatError(
                    "library_container_invalid", "The MP4 file type box must be first."
                )
            payload_size = size - header_size
            stream.seek(cursor + header_size)
            if box_type == b"ftyp":
                payload = stream.read(payload_size)
                brands = {
                    payload[:4],
                    *(
                        payload[index : index + 4]
                        for index in range(8, len(payload), 4)
                    ),
                }
                if (
                    len(payload) < 8
                    or len(payload) % 4
                    or not all(
                        brand != b"\0\0\0\0"
                        and all(0x20 <= byte <= 0x7E for byte in brand)
                        for brand in brands
                    )
                ):
                    raise LibraryFormatError(
                        "library_container_invalid", "The MP4 file type box is invalid."
                    )
                saw_ftyp = True
            elif box_type == b"moov":
                if moov_payload is not None or payload_size > _MAX_MP4_METADATA_BYTES:
                    raise LibraryFormatError(
                        "library_container_limit", "The MP4 metadata exceeds the limit."
                    )
                moov_payload = stream.read(payload_size)
                if len(moov_payload) != payload_size:
                    raise LibraryFormatError(
                        "library_container_invalid", "The MP4 metadata is truncated."
                    )
            elif box_type == b"mdat":
                saw_mdat = payload_size > 0
            cursor += size
    if not saw_ftyp or not saw_mdat or moov_payload is None or cursor != file_size:
        raise LibraryFormatError(
            "library_container_invalid", "The MP4 media structure is incomplete."
        )
    handlers = {
        payload[8:12]
        for payload in _mp4_find_payloads(moov_payload, b"hdlr")
        if len(payload) >= 12
    }
    codecs = set().union(
        *(
            _mp4_sample_entry_types(payload)
            for payload in _mp4_find_payloads(moov_payload, b"stsd")
        )
    )
    if b"vide" not in handlers or not codecs:
        raise LibraryFormatError(
            "library_container_invalid",
            "The MP4 video track is missing.",
        )
    return {
        "validator": "iso-bmff-video-v1",
        "valid": True,
        "codecs": sorted(_media_fourcc(codec) for codec in codecs),
    }


def _media_fourcc(value: bytes) -> str:
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return f"0x{value.hex()}"
    if all(0x20 <= byte <= 0x7E for byte in value):
        return decoded
    return f"0x{value.hex()}"


def _read_ebml_vint(stream: BinaryIO, *, identifier: bool) -> tuple[int, bool]:
    first = stream.read(1)
    if not first or first == b"\x00":
        raise LibraryFormatError(
            "library_container_invalid", "The WebM element header is invalid."
        )
    first_value = first[0]
    length = 1
    marker = 0x80
    while not first_value & marker:
        marker >>= 1
        length += 1
        if marker == 0 or length > (4 if identifier else 8):
            raise LibraryFormatError(
                "library_container_invalid", "The WebM variable integer is invalid."
            )
    rest = stream.read(length - 1)
    if len(rest) != length - 1:
        raise LibraryFormatError(
            "library_container_invalid", "The WebM element header is truncated."
        )
    raw = first + rest
    if identifier:
        return int.from_bytes(raw, "big"), False
    value = int.from_bytes(bytes([first_value & (marker - 1)]) + rest, "big")
    unknown = value == (1 << (7 * length)) - 1
    return value, unknown


def _ebml_elements(payload: bytes) -> list[tuple[int, bytes]]:
    elements: list[tuple[int, bytes]] = []
    stream = BytesIO(payload)
    while stream.tell() < len(payload):
        element_id, _ = _read_ebml_vint(stream, identifier=True)
        size, unknown = _read_ebml_vint(stream, identifier=False)
        if unknown or size > len(payload) - stream.tell():
            raise LibraryFormatError(
                "library_container_invalid", "The WebM element size is invalid."
            )
        element_payload = stream.read(size)
        if len(element_payload) != size:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM element is truncated."
            )
        elements.append((element_id, element_payload))
        if len(elements) > _MAX_MEDIA_CONTAINER_ENTRIES:
            raise LibraryFormatError(
                "library_container_limit", "The WebM file has too many elements."
            )
    return elements


def _validate_webm(path: Path) -> dict[str, object]:
    file_size = path.stat().st_size
    if file_size < 32:
        raise LibraryFormatError(
            "library_container_invalid", "The WebM container is truncated."
        )
    with path.open("rb") as stream:
        header_id, _ = _read_ebml_vint(stream, identifier=True)
        header_size, header_unknown = _read_ebml_vint(stream, identifier=False)
        if header_id != 0x1A45DFA3 or header_unknown or header_size > 64 * 1024:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM EBML header is invalid."
            )
        header = stream.read(header_size)
        header_fields = dict(_ebml_elements(header))
        if header_fields.get(0x4282) != b"webm":
            raise LibraryFormatError(
                "library_container_invalid", "The EBML document is not WebM."
            )
        segment_id, _ = _read_ebml_vint(stream, identifier=True)
        segment_size, segment_unknown = _read_ebml_vint(stream, identifier=False)
        if segment_id != 0x18538067:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM Segment is missing."
            )
        segment_start = stream.tell()
        segment_end = file_size if segment_unknown else segment_start + segment_size
        if segment_end != file_size:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM Segment size is invalid."
            )
        tracks_payload: bytes | None = None
        saw_cluster = False
        element_count = 0
        allowed_top_level = {
            0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675,
            0x1C53BB6B, 0x1043A770, 0x1254C367, 0xEC, 0xBF,
        }
        while stream.tell() < segment_end:
            element_id, _ = _read_ebml_vint(stream, identifier=True)
            size, unknown = _read_ebml_vint(stream, identifier=False)
            if (
                unknown
                or element_id not in allowed_top_level
                or size > segment_end - stream.tell()
            ):
                raise LibraryFormatError(
                    "library_container_invalid", "The WebM Segment element is invalid."
                )
            element_count += 1
            if element_count > _MAX_MEDIA_CONTAINER_ENTRIES:
                raise LibraryFormatError(
                    "library_container_limit", "The WebM file has too many elements."
                )
            if element_id == 0x1654AE6B:
                if tracks_payload is not None or size > _MAX_WEBM_TRACKS_BYTES:
                    raise LibraryFormatError(
                        "library_container_limit",
                        "The WebM Tracks element exceeds the limit.",
                    )
                tracks_payload = stream.read(size)
            else:
                if element_id == 0x1F43B675:
                    saw_cluster = size > 0
                stream.seek(size, 1)
        if stream.tell() != segment_end or tracks_payload is None or not saw_cluster:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM media structure is incomplete."
            )
    codecs: set[str] = set()
    for element_id, track_payload in _ebml_elements(tracks_payload):
        if element_id != 0xAE:
            continue
        fields = dict(_ebml_elements(track_payload))
        if 0x6D80 in fields:
            raise LibraryFormatError(
                "library_container_invalid", "Encrypted WebM tracks are unsupported."
            )
        track_type = int.from_bytes(fields.get(0x83, b""), "big")
        codec_bytes = fields.get(0x86, b"")
        try:
            codec = codec_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LibraryFormatError(
                "library_container_invalid", "The WebM codec identifier is invalid."
            ) from exc
        if track_type == 1:
            if not codec or len(codec) > 128 or any(ord(char) < 0x20 for char in codec):
                raise LibraryFormatError(
                    "library_container_invalid", "The WebM codec identifier is invalid."
                )
            codecs.add(codec)
    if not codecs:
        raise LibraryFormatError(
            "library_container_invalid", "The WebM video track is missing."
        )
    return {
        "validator": "webm-video-v1",
        "valid": True,
        "codecs": sorted(codecs),
    }


def _validate_text_file(path: Path) -> None:
    with path.open("rb") as stream:
        first = stream.read(4)
        stream.seek(0)
        encoding = library_text_encoding(first)
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        likeness = TextLikeness()
        try:
            while chunk := stream.read(64 * 1024):
                likeness.add(decoder.decode(chunk, final=False))
            likeness.add(decoder.decode(b"", final=True))
        except UnicodeError as exc:
            raise LibraryFormatError(
                "library_text_encoding_unsupported",
                "Library text uploads must use UTF-8 or BOM-marked UTF-16.",
            ) from exc
        if likeness.binary_like:
            raise LibraryFormatError(
                "library_format_mismatch",
                "The file is binary, not supported text.",
            )


@dataclass(slots=True)
class _XmlPackageBudget:
    xml_bytes: int = 0
    nodes: int = 0
    attributes: int = 0
    attribute_bytes: int = 0
    text_bytes: int = 0

    def consume(
        self,
        *,
        xml_bytes: int = 0,
        nodes: int = 0,
        attributes: int = 0,
        attribute_bytes: int = 0,
        text_bytes: int = 0,
    ) -> None:
        next_xml_bytes = self.xml_bytes + xml_bytes
        next_nodes = self.nodes + nodes
        next_attributes = self.attributes + attributes
        next_attribute_bytes = self.attribute_bytes + attribute_bytes
        next_text_bytes = self.text_bytes + text_bytes
        if (
            next_xml_bytes > _MAX_OOXML_XML_BYTES
            or next_nodes > _MAX_OOXML_XML_NODES
            or next_attributes > _MAX_OOXML_XML_ATTRIBUTES
            or next_attribute_bytes > _MAX_OOXML_XML_ATTRIBUTE_BYTES
            or next_text_bytes > _MAX_OOXML_XML_TEXT_BYTES
        ):
            raise LibraryFormatError(
                "library_container_limit",
                "The Office document XML exceeds the package safety limit.",
            )
        self.xml_bytes = next_xml_bytes
        self.nodes = next_nodes
        self.attributes = next_attributes
        self.attribute_bytes = next_attribute_bytes
        self.text_bytes = next_text_bytes


def _xml_utf8_bytes(value: str | None) -> int:
    return len(value.encode("utf-8")) if value is not None else 0


def _parse_bounded_xml(
    content: bytes,
    *,
    limit_message: str,
    invalid_message: str,
    unsafe_message: str,
    package_budget: _XmlPackageBudget | None = None,
) -> ElementTree.Element:
    if len(content) > _MAX_XML_BYTES:
        raise LibraryFormatError("library_container_limit", limit_message)
    if contains_forbidden_xml_declaration(content):
        raise LibraryFormatError("library_container_invalid", unsafe_message)
    if package_budget is not None:
        package_budget.consume(xml_bytes=len(content))

    # This bounds in-process parsing work and retained tree size. It is not an
    # operating-system sandbox for hostile document processing.
    try:
        events = ElementTree.iterparse(
            BytesIO(content),
            events=("start", "end", "start-ns", "comment", "pi"),
        )
        depth = 0
        nodes = 0
        attributes = 0
        attribute_bytes = 0
        text_bytes = 0
        pending_namespace_attributes = 0
        for event, item in events:
            if event == "start-ns":
                prefix, namespace = item
                namespace_name = "xmlns" if not prefix else f"xmlns:{prefix}"
                namespace_bytes = _xml_utf8_bytes(namespace_name) + _xml_utf8_bytes(
                    namespace
                )
                pending_namespace_attributes += 1
                attributes += 1
                attribute_bytes += namespace_bytes
                if package_budget is not None:
                    package_budget.consume(
                        attributes=1,
                        attribute_bytes=namespace_bytes,
                    )
                if (
                    attributes > _MAX_XML_ATTRIBUTES
                    or attribute_bytes > _MAX_XML_ATTRIBUTE_BYTES
                ):
                    raise LibraryFormatError("library_container_limit", limit_message)
                continue

            if event in {"comment", "pi"}:
                nodes += 1
                node_text_bytes = _xml_utf8_bytes(item.text) + _xml_utf8_bytes(
                    item.tail
                )
                text_bytes += node_text_bytes
                if package_budget is not None:
                    package_budget.consume(nodes=1, text_bytes=node_text_bytes)
                if nodes > _MAX_XML_NODES or text_bytes > _MAX_XML_TEXT_BYTES:
                    raise LibraryFormatError("library_container_limit", limit_message)
                continue

            if event == "start":
                depth += 1
                nodes += 1
                element_attributes = len(item.attrib)
                element_attribute_bytes = sum(
                    _xml_utf8_bytes(name) + _xml_utf8_bytes(value)
                    for name, value in item.attrib.items()
                )
                attributes += element_attributes
                attribute_bytes += element_attribute_bytes
                if package_budget is not None:
                    package_budget.consume(
                        nodes=1,
                        attributes=element_attributes,
                        attribute_bytes=element_attribute_bytes,
                    )
                if (
                    depth > _MAX_XML_DEPTH
                    or nodes > _MAX_XML_NODES
                    or element_attributes + pending_namespace_attributes
                    > _MAX_XML_ATTRIBUTES_PER_ELEMENT
                    or attributes > _MAX_XML_ATTRIBUTES
                    or attribute_bytes > _MAX_XML_ATTRIBUTE_BYTES
                ):
                    raise LibraryFormatError("library_container_limit", limit_message)
                pending_namespace_attributes = 0
                continue

            node_text_bytes = _xml_utf8_bytes(item.text) + _xml_utf8_bytes(item.tail)
            text_bytes += node_text_bytes
            if package_budget is not None:
                package_budget.consume(text_bytes=node_text_bytes)
            if text_bytes > _MAX_XML_TEXT_BYTES:
                raise LibraryFormatError("library_container_limit", limit_message)
            depth -= 1
        parsed = events.root
    except LibraryFormatError:
        raise
    except (ElementTree.ParseError, LookupError, UnicodeError) as exc:
        raise LibraryFormatError("library_container_invalid", invalid_message) from exc

    if parsed is None or depth != 0:
        raise LibraryFormatError("library_container_invalid", invalid_message)
    return parsed


def _validate_xml_file(path: Path) -> None:
    _validate_text_file(path)
    if path.stat().st_size > _MAX_XML_BYTES:
        raise LibraryFormatError(
            "library_container_limit", "The XML document exceeds the safety limit."
        )
    content = path.read_bytes()
    _parse_bounded_xml(
        content,
        limit_message="The XML document exceeds the safety limit.",
        invalid_message="The HWPML document is not valid XML.",
        unsafe_message="The XML document contains unsafe declarations.",
    )


def _detect_zip_container(path: Path) -> tuple[str, dict[str, object]]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ZIP_ENTRIES:
                raise LibraryFormatError(
                    "library_container_limit", "The document contains too many files."
                )
            total_uncompressed = sum(entry.file_size for entry in entries)
            total_compressed = sum(max(entry.compress_size, 1) for entry in entries)
            if total_uncompressed > _MAX_ZIP_DECLARED_UNCOMPRESSED:
                raise LibraryFormatError(
                    "library_container_limit", "The document expands beyond the limit."
                )
            if total_uncompressed / max(total_compressed, 1) > _MAX_ZIP_RATIO:
                raise LibraryFormatError(
                    "library_container_limit",
                    "The document compression ratio exceeds the safety limit.",
                )
            names: set[str] = set()
            casefolded_names: set[str] = set()
            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                member_path = PurePosixPath(normalized_name)
                unix_mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if (
                    not normalized_name
                    or normalized_name != entry.filename
                    or normalized_name.startswith("/")
                    or normalized_name.startswith("//")
                    or bool(re.match(r"^[A-Za-z]:", normalized_name))
                    or ".." in member_path.parts
                    or "." in member_path.parts
                    or stat.S_ISLNK(unix_mode)
                    or bool(entry.flag_bits & 0x1)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "The document contains an unsafe archive member.",
                    )
                if (
                    entry.file_size > _MAX_ZIP_MEMBER_BYTES
                    or entry.file_size / max(entry.compress_size, 1) > _MAX_ZIP_RATIO
                ):
                    raise LibraryFormatError(
                        "library_container_limit",
                        "A document member exceeds the safety limit.",
                    )
                folded_name = normalized_name.casefold()
                if normalized_name in names or folded_name in casefolded_names:
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "The document contains duplicate archive members.",
                    )
                names.add(normalized_name)
                casefolded_names.add(folded_name)
    except LibraryFormatError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise LibraryFormatError(
            "library_container_invalid", "The ZIP-based document is corrupt."
        ) from exc

    format_id: str | None = None
    if "[Content_Types].xml" in names and "word/document.xml" in names:
        _validate_ooxml_package(
            archive_path=path,
            names=names,
            main_part="/word/document.xml",
            main_content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document.main+xml"
            ),
            main_root="document",
        )
        format_id = "docx"
    elif "[Content_Types].xml" in names and "ppt/presentation.xml" in names:
        _validate_ooxml_package(
            archive_path=path,
            names=names,
            main_part="/ppt/presentation.xml",
            main_content_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation.main+xml"
            ),
            main_root="presentation",
        )
        format_id = "pptx"
    elif "[Content_Types].xml" in names and "xl/workbook.xml" in names:
        _validate_ooxml_package(
            archive_path=path,
            names=names,
            main_part="/xl/workbook.xml",
            main_content_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet.main+xml"
            ),
            main_root="workbook",
        )
        format_id = "xlsx"
    elif "Contents/content.hpf" in names or "content.hpf" in names:
        content_name = (
            "Contents/content.hpf" if "Contents/content.hpf" in names else "content.hpf"
        )
        _validate_xml_member(
            archive_path=path,
            name=content_name,
            root=None,
        )
        format_id = "hwpx"
    if format_id is None:
        raise LibraryFormatError(
            "unsupported_library_archive",
            "Generic archive uploads are not supported by Library.",
        )
    return format_id, {
        "validator": "zip-container",
        "valid": True,
        "entry_count": len(names),
        "declared_uncompressed_bytes": total_uncompressed,
    }


def _validate_ooxml_package(
    *,
    archive_path: Path,
    names: set[str],
    main_part: str,
    main_content_type: str,
    main_root: str,
) -> None:
    for name in names:
        folded_name = name.casefold()
        lowered_parts = {part.casefold() for part in PurePosixPath(name).parts}
        if folded_name.endswith(_OOXML_DANGEROUS_MEMBER_SUFFIXES) or (
            lowered_parts & _OOXML_DANGEROUS_MEMBER_PARTS
        ):
            raise LibraryFormatError(
                "library_container_invalid",
                "Embedded executable Office content is not supported.",
            )

    package_budget = _XmlPackageBudget()
    validated_xml_names: set[str] = set()

    def validate_xml(name: str, *, root: str | None) -> ElementTree.Element:
        parsed = _validate_xml_member(
            archive_path=archive_path,
            name=name,
            root=root,
            package_budget=package_budget,
        )
        validated_xml_names.add(name)
        return parsed

    content_types = validate_xml("[Content_Types].xml", root="Types")
    overrides = {
        child.attrib.get("PartName"): child.attrib.get("ContentType")
        for child in content_types
        if child.tag.rsplit("}", 1)[-1] == "Override"
    }
    all_content_types = " ".join(
        child.attrib.get("ContentType", "") for child in content_types
    ).casefold()
    if overrides.get(main_part) != main_content_type or any(
        marker in all_content_types for marker in _OOXML_DANGEROUS_CONTENT_TYPE_MARKERS
    ):
        raise LibraryFormatError(
            "library_container_invalid",
            "The Office document content type is invalid.",
        )
    main_document = validate_xml(main_part.removeprefix("/"), root=main_root)
    is_spreadsheet = main_part == "/xl/workbook.xml"
    if is_spreadsheet and contains_active_spreadsheet_formula(main_document):
        raise LibraryFormatError(
            "library_container_invalid",
            "External-capable spreadsheet formulas are not supported.",
        )
    for relationship_name in sorted(
        name for name in names if name.casefold().endswith(".rels")
    ):
        relationships = validate_xml(relationship_name, root="Relationships")
        for child in relationships:
            if child.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            target_mode = child.attrib.get("TargetMode", "Internal")
            if target_mode not in {"Internal", "External"}:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The Office document relationship mode is invalid.",
                )
            relationship_suffix = (
                child.attrib.get("Type", "").rstrip("/").rsplit("/", 1)[-1].casefold()
            )
            if (
                target_mode == "External"
                or relationship_suffix in _OOXML_DANGEROUS_RELATIONSHIP_SUFFIXES
            ):
                raise LibraryFormatError(
                    "library_container_invalid",
                    "External Office document relationships are not supported.",
                )
            resolved_target = _resolve_ooxml_relationship_target(
                relationship_name,
                child.attrib.get("Target", ""),
            )
            if resolved_target not in names:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The Office document relationship target is missing.",
                )

    for xml_name in sorted(
        name
        for name in names
        if name not in validated_xml_names
        and name.casefold().endswith((".xml", ".rels"))
    ):
        document = validate_xml(xml_name, root=None)
        if is_spreadsheet and contains_active_spreadsheet_formula(document):
            raise LibraryFormatError(
                "library_container_invalid",
                "External-capable spreadsheet formulas are not supported.",
            )


def _resolve_ooxml_relationship_target(
    relationship_name: str,
    target: str,
) -> str:
    decoded = unquote(unquote(target)).replace("\\", "/")
    parsed = urlsplit(decoded)
    if (
        not decoded
        or "\x00" in decoded
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or decoded.startswith("/")
    ):
        raise LibraryFormatError(
            "library_container_invalid",
            "The Office document relationship target is invalid.",
        )
    relationship_path = PurePosixPath(relationship_name)
    if relationship_path.parent.name != "_rels" or not relationship_path.name.endswith(
        ".rels"
    ):
        raise LibraryFormatError(
            "library_container_invalid",
            "The Office document relationship target is invalid.",
        )
    source_parent = relationship_path.parent.parent
    normalized = posixpath.normpath(str(source_parent / decoded))
    if normalized == ".." or normalized.startswith("../"):
        raise LibraryFormatError(
            "library_container_invalid",
            "The Office document relationship target is invalid.",
        )
    return normalized


def _validate_pdf(path: Path, prefix: bytes, tail: bytes) -> None:
    if not re.match(rb"%PDF-(?:1\.[0-9]|2\.0)", prefix[:8]):
        raise LibraryFormatError(
            "library_container_invalid", "The PDF header or trailer is invalid."
        )
    eof_match = tuple(
        re.finditer(rb"startxref\s+(\d+)\s+%%EOF\s*$", tail, flags=re.DOTALL)
    )
    if not eof_match:
        raise LibraryFormatError(
            "library_container_invalid", "The PDF cross-reference trailer is invalid."
        )
    xref_offset = int(eof_match[-1].group(1))
    if xref_offset >= path.stat().st_size:
        raise LibraryFormatError(
            "library_container_invalid", "The PDF cross-reference offset is invalid."
        )
    with path.open("rb") as stream:
        stream.seek(xref_offset)
        xref = stream.read(4096)
    xref_stream = bool(re.match(rb"\d+\s+\d+\s+obj\b", xref))
    if not xref.startswith(b"xref") and not (
        xref_stream and re.search(rb"/Type\s*/XRef\b", xref)
    ):
        raise LibraryFormatError(
            "library_container_invalid", "The PDF cross-reference data is invalid."
        )
    trailer_evidence = tail + xref
    if not re.search(rb"/Root\s+\d+\s+\d+\s+R\b", trailer_evidence):
        raise LibraryFormatError(
            "library_container_invalid", "The PDF document catalog is missing."
        )
    if re.search(rb"/Encrypt\b", trailer_evidence):
        raise LibraryFormatError(
            "library_encrypted_unsupported",
            "Encrypted PDF uploads are not supported by this profile.",
        )


def _validate_png(path: Path) -> dict[str, object]:
    saw_header = False
    saw_data = False
    saw_palette = False
    data_ended = False
    total_data_bytes = 0
    chunk_count = 0
    width = 0
    height = 0
    bit_depth = 0
    color_type = 0
    palette_entries = 0
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise LibraryFormatError(
                "library_container_invalid", "The PNG signature is invalid."
            )
        while True:
            chunk_header = stream.read(8)
            if len(chunk_header) != 8:
                raise LibraryFormatError(
                    "library_container_invalid", "The PNG image is truncated."
                )
            chunk_length = int.from_bytes(chunk_header[:4], "big")
            chunk_type = chunk_header[4:]
            chunk_count += 1
            if (
                chunk_count > 100_000
                or chunk_length > path.stat().st_size
                or any(
                    not (65 <= value <= 90 or 97 <= value <= 122)
                    for value in chunk_type
                )
                or chunk_type[2] & 0x20
            ):
                raise LibraryFormatError(
                    "library_container_limit", "The PNG chunk table is invalid."
                )
            if chunk_type == b"IHDR" and (
                saw_header or chunk_count != 1 or chunk_length != 13
            ):
                raise LibraryFormatError(
                    "library_container_invalid", "The PNG header is invalid."
                )
            if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "Animated PNG is outside the admitted source Preview profile.",
                )
            if chunk_type[0] & 0x20 == 0 and chunk_type not in {
                b"IHDR",
                b"PLTE",
                b"IDAT",
                b"IEND",
            }:
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The PNG contains an unsupported critical chunk.",
                )
            checksum = zlib.crc32(chunk_type)
            remaining = chunk_length
            header_content = bytearray()
            palette_content = bytearray()
            while remaining:
                data = stream.read(min(remaining, 64 * 1024))
                if not data:
                    raise LibraryFormatError(
                        "library_container_invalid", "The PNG image is truncated."
                    )
                if chunk_type == b"IHDR":
                    header_content.extend(data)
                elif chunk_type == b"PLTE":
                    palette_content.extend(data)
                checksum = zlib.crc32(data, checksum)
                remaining -= len(data)
            expected_checksum = stream.read(4)
            if (
                len(expected_checksum) != 4
                or int.from_bytes(expected_checksum, "big") != checksum & 0xFFFFFFFF
            ):
                raise LibraryFormatError(
                    "library_container_invalid", "The PNG chunk checksum is invalid."
                )
            if chunk_type == b"IHDR":
                width = int.from_bytes(header_content[:4], "big")
                height = int.from_bytes(header_content[4:8], "big")
                bit_depth = header_content[8]
                color_type = header_content[9]
                legal_depths = {
                    0: {1, 2, 4, 8, 16},
                    2: {8, 16},
                    3: {1, 2, 4, 8},
                    4: {8, 16},
                    6: {8, 16},
                }
                if not (
                    len(header_content) == 13
                    and 0 < width <= MAX_SOURCE_PREVIEW_IMAGE_DIMENSION
                    and 0 < height <= MAX_SOURCE_PREVIEW_IMAGE_DIMENSION
                    and width * height <= MAX_SOURCE_PREVIEW_IMAGE_PIXELS
                    and bit_depth in legal_depths.get(color_type, set())
                    and header_content[10] == 0
                    and header_content[11] == 0
                    and header_content[12] == 0
                ):
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "The PNG header is outside the admitted source Preview "
                        "profile.",
                    )
                saw_header = True
            elif chunk_type == b"PLTE":
                if (
                    not saw_header
                    or saw_palette
                    or saw_data
                    or color_type in {0, 4}
                    or chunk_length == 0
                    or chunk_length > 768
                    or chunk_length % 3
                ):
                    raise LibraryFormatError(
                        "library_container_invalid", "The PNG palette is invalid."
                    )
                palette_entries = len(palette_content) // 3
                if color_type == 3 and palette_entries > 2**bit_depth:
                    raise LibraryFormatError(
                        "library_container_invalid", "The PNG palette is invalid."
                    )
                saw_palette = True
            elif chunk_type == b"IDAT":
                if not saw_header or data_ended:
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "The PNG image data ordering is invalid.",
                    )
                saw_data = True
                total_data_bytes += chunk_length
            elif chunk_type == b"IEND":
                if (
                    chunk_length != 0
                    or not saw_header
                    or not saw_data
                    or total_data_bytes == 0
                    or (color_type == 3 and not saw_palette)
                ):
                    raise LibraryFormatError(
                        "library_container_invalid", "The PNG trailer is invalid."
                    )
                if stream.read(1):
                    raise LibraryFormatError(
                        "library_container_invalid",
                        "Trailing data after the PNG image is not supported.",
                    )
                return {
                    "validator": "image",
                    "valid": True,
                    "width": width,
                    "height": height,
                    "pixel_count": width * height,
                    "source_preview_profile": "png-static-v1",
                }
            elif saw_data:
                data_ended = True


def _validate_jpeg(path: Path, prefix: bytes, tail: bytes) -> dict[str, object]:
    if path.stat().st_size < 20 or not tail.endswith(b"\xff\xd9"):
        raise LibraryFormatError(
            "library_container_invalid", "The JPEG image is truncated or invalid."
        )
    cursor = 2
    saw_frame = False
    saw_huffman_table = False
    saw_quantization_table = False
    width = 0
    height = 0
    frame_component_ids: set[int] = set()
    frame_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while cursor < len(prefix):
        if prefix[cursor] != 0xFF:
            raise LibraryFormatError(
                "library_container_invalid", "The JPEG marker stream is invalid."
            )
        while cursor < len(prefix) and prefix[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(prefix):
            break
        marker = prefix[cursor]
        cursor += 1
        if marker == 0x00:
            raise LibraryFormatError(
                "library_container_invalid", "The JPEG marker stream is invalid."
            )
        if marker == 0xDA:
            if cursor + 2 > len(prefix):
                break
            segment_length = int.from_bytes(prefix[cursor : cursor + 2], "big")
            if segment_length < 2 or cursor + segment_length > len(prefix):
                break
            scan = prefix[cursor + 2 : cursor + segment_length]
            scan_components = scan[0] if scan else 0
            if (
                not saw_frame
                or not saw_huffman_table
                or not saw_quantization_table
                or scan_components != len(frame_component_ids)
                or segment_length != 6 + 2 * scan_components
                or len(scan) != 4 + 2 * scan_components
                or len({scan[index] for index in range(1, 1 + scan_components * 2, 2)})
                != scan_components
                or any(
                    scan[index] not in frame_component_ids
                    or scan[index + 1] >> 4 > 3
                    or scan[index + 1] & 0x0F > 3
                    for index in range(1, 1 + scan_components * 2, 2)
                )
                or scan[-3:] != b"\x00\x3f\x00"
            ):
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The JPEG scan is outside the admitted baseline profile.",
                )
            _validate_jpeg_entropy_stream(path, cursor + segment_length)
            return {
                "validator": "image",
                "valid": True,
                "width": width,
                "height": height,
                "pixel_count": width * height,
                "source_preview_profile": "jpeg-baseline-v1",
            }
        if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
            raise LibraryFormatError(
                "library_container_invalid", "The JPEG marker stream is invalid."
            )
        if cursor + 2 > len(prefix):
            break
        segment_length = int.from_bytes(prefix[cursor : cursor + 2], "big")
        if segment_length < 2 or cursor + segment_length > len(prefix):
            break
        if marker == 0xCC or marker in frame_markers - {0xC0}:
            raise LibraryFormatError(
                "library_container_invalid",
                "Progressive, lossless, and arithmetic JPEG are outside the "
                "admitted source Preview profile.",
            )
        if marker == 0xC0:
            frame = prefix[cursor + 2 : cursor + segment_length]
            component_count = frame[5] if len(frame) >= 6 else 0
            components = frame[6:]
            width = int.from_bytes(frame[3:5], "big") if len(frame) >= 5 else 0
            height = int.from_bytes(frame[1:3], "big") if len(frame) >= 3 else 0
            if (
                saw_frame
                or len(frame) != 6 + 3 * component_count
                or segment_length != 8 + 3 * component_count
                or frame[0] != 8
                or component_count not in {1, 3}
                or not 0 < width <= MAX_SOURCE_PREVIEW_IMAGE_DIMENSION
                or not 0 < height <= MAX_SOURCE_PREVIEW_IMAGE_DIMENSION
                or width * height > MAX_SOURCE_PREVIEW_IMAGE_PIXELS
            ):
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The JPEG frame is outside the admitted baseline profile.",
                )
            frame_component_ids = {
                components[index] for index in range(0, len(components), 3)
            }
            if len(frame_component_ids) != component_count or any(
                components[index + 1] >> 4 not in range(1, 5)
                or components[index + 1] & 0x0F not in range(1, 5)
                or components[index + 2] > 3
                for index in range(0, len(components), 3)
            ):
                raise LibraryFormatError(
                    "library_container_invalid",
                    "The JPEG frame components are invalid.",
                )
            saw_frame = True
        elif marker == 0xC4:
            saw_huffman_table = True
        elif marker == 0xDB:
            saw_quantization_table = True
        cursor += segment_length
    raise LibraryFormatError(
        "library_container_invalid",
        "The JPEG scan header is missing or exceeds the detector window.",
    )


def _validate_jpeg_entropy_stream(path: Path, offset: int) -> None:
    saw_entropy = False
    pending_marker = False
    with path.open("rb") as stream:
        stream.seek(offset)
        while chunk := stream.read(64 * 1024):
            for index, value in enumerate(chunk):
                if not pending_marker:
                    if value == 0xFF:
                        pending_marker = True
                    else:
                        saw_entropy = True
                    continue
                if value == 0xFF:
                    continue
                if value == 0x00:
                    pending_marker = False
                    saw_entropy = True
                    continue
                if 0xD0 <= value <= 0xD7:
                    pending_marker = False
                    continue
                if value == 0xD9:
                    if not saw_entropy or index + 1 != len(chunk) or stream.read(1):
                        raise LibraryFormatError(
                            "library_container_invalid",
                            "The JPEG end marker or trailing data is invalid.",
                        )
                    return
                raise LibraryFormatError(
                    "library_container_invalid",
                    "Multiple scans and embedded JPEG markers are outside the "
                    "admitted baseline profile.",
                )
    raise LibraryFormatError(
        "library_container_invalid", "The JPEG entropy stream is truncated."
    )


def _validate_gif(path: Path, prefix: bytes, tail: bytes) -> None:
    if (
        path.stat().st_size < 14
        or prefix[6:10] == b"\x00\x00\x00\x00"
        or not tail.endswith(b";")
    ):
        raise LibraryFormatError(
            "library_container_invalid", "The GIF image is truncated or invalid."
        )


def _validate_tiff(path: Path, prefix: bytes) -> None:
    if path.stat().st_size < 8:
        raise LibraryFormatError(
            "library_container_invalid", "The TIFF image is truncated."
        )
    byteorder = "little" if prefix.startswith(b"II") else "big"
    offset = int.from_bytes(prefix[4:8], byteorder)
    if offset < 8 or offset >= path.stat().st_size:
        raise LibraryFormatError(
            "library_container_invalid", "The TIFF image directory is invalid."
        )


def _validate_webp(path: Path, prefix: bytes) -> None:
    if path.stat().st_size < 20:
        raise LibraryFormatError(
            "library_container_invalid", "The WebP image is truncated."
        )
    declared_size = int.from_bytes(prefix[4:8], "little") + 8
    if declared_size != path.stat().st_size or prefix[12:16] not in {
        b"VP8 ",
        b"VP8L",
        b"VP8X",
    }:
        raise LibraryFormatError(
            "library_container_invalid", "The WebP container is invalid."
        )


def _rotate32(value: int, count: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def _hdf5_lookup3_mix(a: int, b: int, c: int) -> tuple[int, int, int]:
    mask = 0xFFFFFFFF
    a = (a - c) & mask
    a ^= _rotate32(c, 4)
    c = (c + b) & mask
    b = (b - a) & mask
    b ^= _rotate32(a, 6)
    a = (a + c) & mask
    c = (c - b) & mask
    c ^= _rotate32(b, 8)
    b = (b + a) & mask
    a = (a - c) & mask
    a ^= _rotate32(c, 16)
    c = (c + b) & mask
    b = (b - a) & mask
    b ^= _rotate32(a, 19)
    a = (a + c) & mask
    c = (c - b) & mask
    c ^= _rotate32(b, 4)
    b = (b + a) & mask
    return a, b, c


def _hdf5_lookup3_final(a: int, b: int, c: int) -> int:
    mask = 0xFFFFFFFF
    c ^= b
    c = (c - _rotate32(b, 14)) & mask
    a ^= c
    a = (a - _rotate32(c, 11)) & mask
    b ^= a
    b = (b - _rotate32(a, 25)) & mask
    c ^= b
    c = (c - _rotate32(b, 16)) & mask
    a ^= c
    a = (a - _rotate32(c, 4)) & mask
    b ^= a
    b = (b - _rotate32(a, 14)) & mask
    c ^= b
    return (c - _rotate32(b, 24)) & mask


def _hdf5_lookup3_checksum(data: bytes) -> int:
    """Return the lookup3 checksum used by HDF5 metadata blocks."""

    mask = 0xFFFFFFFF
    initial = (0xDEADBEEF + len(data)) & mask
    a = b = c = initial
    offset = 0
    remaining = len(data)
    while remaining > 12:
        a = (a + int.from_bytes(data[offset : offset + 4], "little")) & mask
        b = (b + int.from_bytes(data[offset + 4 : offset + 8], "little")) & mask
        c = (c + int.from_bytes(data[offset + 8 : offset + 12], "little")) & mask
        a, b, c = _hdf5_lookup3_mix(a, b, c)
        offset += 12
        remaining -= 12
    if remaining:
        final_block = data[offset:].ljust(12, b"\x00")
        a = (a + int.from_bytes(final_block[0:4], "little")) & mask
        b = (b + int.from_bytes(final_block[4:8], "little")) & mask
        c = (c + int.from_bytes(final_block[8:12], "little")) & mask
        c = _hdf5_lookup3_final(a, b, c)
    return c


def _hdf5_address(data: bytes, offset: int, width: int) -> int:
    return int.from_bytes(data[offset : offset + width], "little")


def _hdf5_address_is_defined(address: int, width: int) -> bool:
    return address != (1 << (width * 8)) - 1


def _raise_invalid_hdf5() -> None:
    raise LibraryFormatError(
        "library_container_invalid", "The HDF5 container is invalid."
    )


def _validate_hdf5_object_message_header(message_type: int, message_flags: int) -> None:
    if message_type not in _HDF5_ADMITTED_OBJECT_MESSAGE_TYPES:
        _raise_invalid_hdf5()
    # All eight flag bits are assigned by the HDF5 format. Shared-message
    # wrappers require a separate bounded object/heap traversal and therefore
    # are outside this admission profile. Do-not-share and shareable are also
    # contradictory states for a single serialized message.
    if (
        message_flags & _HDF5_MESSAGE_FLAG_SHARED
        or (
            message_flags & _HDF5_MESSAGE_FLAG_DO_NOT_SHARE
            and message_flags & _HDF5_MESSAGE_FLAG_SHAREABLE
        )
        or (
            message_flags & _HDF5_MESSAGE_FLAG_WAS_UNKNOWN
            and not message_flags & _HDF5_MESSAGE_FLAG_MARK_IF_UNKNOWN
        )
        or (
            message_flags & _HDF5_MESSAGE_FLAG_SHAREABLE
            and message_type not in _HDF5_SHAREABLE_MESSAGE_TYPES
        )
    ):
        _raise_invalid_hdf5()


def _hdf5_metadata_address_is_valid(
    address: int,
    *,
    width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
) -> bool:
    return (
        _hdf5_address_is_defined(address, width)
        and 0 < address < address_space_size
        and signature_offset + address < stored_eof
    )


def _hdf5_message_payload(data: bytes, *, required_size: int, padded: bool) -> bytes:
    if len(data) < required_size or (
        (not padded and len(data) != required_size)
        or (padded and any(data[required_size:]))
    ):
        _raise_invalid_hdf5()
    return data[:required_size]


@dataclass(frozen=True, slots=True)
class _Hdf5DataspaceInfo:
    point_count: int


@dataclass(frozen=True, slots=True)
class _Hdf5DatatypeInfo:
    element_size: int
    kind: Literal["fixed", "float", "fixed_string", "vlen_string"]


@dataclass(frozen=True, slots=True)
class _Hdf5LayoutInfo:
    storage_class: Literal["compact", "contiguous"]
    storage_address: int | None
    storage_size: int


@dataclass(slots=True)
class _Hdf5ObjectMessageState:
    counts: dict[int, int] = field(default_factory=dict)
    dataspace: _Hdf5DataspaceInfo | None = None
    datatype: _Hdf5DatatypeInfo | None = None
    old_fill_size: int | None = None
    fill_size: int | None = None
    layout: _Hdf5LayoutInfo | None = None
    attribute_names: set[bytes] = field(default_factory=set)

    def record(self, message_type: int) -> None:
        count = self.counts.get(message_type, 0) + 1
        self.counts[message_type] = count
        if message_type not in {0x0000, 0x0006, 0x000C, 0x0010} and count > 1:
            _raise_invalid_hdf5()


@dataclass(frozen=True, slots=True)
class _Hdf5GlobalHeapCollectionInfo:
    collection_size: int
    object_sizes: dict[int, int]


@dataclass(slots=True)
class _Hdf5RangeLedger:
    metadata_ranges: list[tuple[int, int]] = field(default_factory=list)
    raw_ranges: list[tuple[int, int]] = field(default_factory=list)
    _metadata_range_set: set[tuple[int, int]] = field(default_factory=set)
    global_heap_collections: dict[int, _Hdf5GlobalHeapCollectionInfo] = field(
        default_factory=dict
    )

    def add_metadata(self, start: int, end: int) -> None:
        if start >= end:
            _raise_invalid_hdf5()
        candidate = (start, end)
        if candidate in self._metadata_range_set:
            return
        self.metadata_ranges.append(candidate)
        self._metadata_range_set.add(candidate)

    def add_raw(self, start: int, end: int) -> None:
        if start == end:
            return
        if start > end:
            _raise_invalid_hdf5()
        self.raw_ranges.append((start, end))

    def validate_non_overlapping(self) -> None:
        # Traversal can discover tens of thousands of allocations. Collecting
        # ranges and sorting once keeps admission O(N log N), while an overlap
        # check on every insertion would make the configured bounds quadratic.
        allocations = sorted((*self.metadata_ranges, *self.raw_ranges))
        for previous, current in zip(allocations, allocations[1:], strict=False):
            if current[0] < previous[1]:
                _raise_invalid_hdf5()


def _hdf5_point_count(dimensions: tuple[int, ...]) -> int:
    count = 1
    for dimension in dimensions:
        if dimension == 0:
            return 0
        if dimension >= 1 << 63 or count > ((1 << 63) - 1) // dimension:
            _raise_invalid_hdf5()
        count *= dimension
    return count


def _validate_hdf5_dataspace_message(
    data: bytes, *, length_width: int, padded: bool
) -> _Hdf5DataspaceInfo:
    if not data:
        _raise_invalid_hdf5()
    version = data[0]
    if version == 1:
        if len(data) < 8 or data[1] > 32 or data[2] & ~0x01 or data[3:8] != b"\x00" * 5:
            _raise_invalid_hdf5()
        rank = data[1]
        flags = data[2]
        required_size = 8 + rank * length_width * (2 if flags & 0x01 else 1)
        payload = _hdf5_message_payload(
            data, required_size=required_size, padded=padded
        )
        cursor = 8
        dimensions = tuple(
            int.from_bytes(
                payload[
                    cursor + index * length_width : cursor + (index + 1) * length_width
                ],
                "little",
            )
            for index in range(rank)
        )
        cursor += rank * length_width
        if flags & 0x01:
            unlimited = (1 << (length_width * 8)) - 1
            for index, dimension in enumerate(dimensions):
                maximum = int.from_bytes(
                    payload[
                        cursor + index * length_width : cursor
                        + (index + 1) * length_width
                    ],
                    "little",
                )
                if maximum != unlimited and maximum < dimension:
                    _raise_invalid_hdf5()
        return _Hdf5DataspaceInfo(point_count=_hdf5_point_count(dimensions))
    if version == 2:
        if len(data) < 4 or data[1] > 32 or data[2] & ~0x01 or data[3] > 2:
            _raise_invalid_hdf5()
        rank = data[1]
        flags = data[2]
        dataspace_type = data[3]
        if (dataspace_type == 1) != (rank > 0) or (dataspace_type != 1 and flags):
            _raise_invalid_hdf5()
        required_size = 4 + rank * length_width * (2 if flags & 0x01 else 1)
        payload = _hdf5_message_payload(
            data, required_size=required_size, padded=padded
        )
        cursor = 4
        dimensions = tuple(
            int.from_bytes(
                payload[
                    cursor + index * length_width : cursor + (index + 1) * length_width
                ],
                "little",
            )
            for index in range(rank)
        )
        cursor += rank * length_width
        if flags & 0x01:
            unlimited = (1 << (length_width * 8)) - 1
            for index, dimension in enumerate(dimensions):
                maximum = int.from_bytes(
                    payload[
                        cursor + index * length_width : cursor
                        + (index + 1) * length_width
                    ],
                    "little",
                )
                if maximum != unlimited and maximum < dimension:
                    _raise_invalid_hdf5()
        point_count = (
            1
            if dataspace_type == 0
            else 0
            if dataspace_type == 2
            else _hdf5_point_count(dimensions)
        )
        return _Hdf5DataspaceInfo(point_count=point_count)
    _raise_invalid_hdf5()


def _parse_hdf5_datatype(
    data: bytes, *, depth: int = 0
) -> tuple[_Hdf5DatatypeInfo, int]:
    if depth > 8 or len(data) < 8:
        _raise_invalid_hdf5()
    version = data[0] >> 4
    datatype_class = data[0] & 0x0F
    class_bits = int.from_bytes(data[1:4], "little")
    element_size = int.from_bytes(data[4:8], "little")
    if (
        version not in {1, 2, 3}
        or not 0 < element_size <= _MAX_HDF5_OBJECT_HEADER_BYTES
    ):
        _raise_invalid_hdf5()
    if datatype_class == 0:
        if len(data) < 12 or class_bits & ~0x0F:
            _raise_invalid_hdf5()
        bit_offset = int.from_bytes(data[8:10], "little")
        bit_precision = int.from_bytes(data[10:12], "little")
        if bit_precision == 0 or bit_offset + bit_precision > element_size * 8:
            _raise_invalid_hdf5()
        return _Hdf5DatatypeInfo(element_size, "fixed"), 12
    if datatype_class == 1:
        if (
            len(data) < 20
            or class_bits & ~0x00FF7F
            or ((class_bits >> 4) & 0x03) == 0x03
            or (class_bits & 0x41) == 0x40
        ):
            _raise_invalid_hdf5()
        bit_offset = int.from_bytes(data[8:10], "little")
        bit_precision = int.from_bytes(data[10:12], "little")
        exponent_location, exponent_size, mantissa_location, mantissa_size = data[12:16]
        sign_location = (class_bits >> 8) & 0xFF
        exponent_bias = int.from_bytes(data[16:20], "little")
        precision_end = bit_offset + bit_precision
        bit_fields = (
            (sign_location, sign_location + 1),
            (exponent_location, exponent_location + exponent_size),
            (mantissa_location, mantissa_location + mantissa_size),
        )
        if (
            bit_precision == 0
            or precision_end > element_size * 8
            or exponent_size == 0
            or mantissa_size == 0
            or any(
                start < bit_offset or end > precision_end for start, end in bit_fields
            )
            or any(
                left[0] < right[1] and right[0] < left[1]
                for index, left in enumerate(bit_fields)
                for right in bit_fields[index + 1 :]
            )
            or (exponent_size < 32 and exponent_bias >= 1 << exponent_size)
        ):
            _raise_invalid_hdf5()
        return _Hdf5DatatypeInfo(element_size, "float"), 20
    if datatype_class == 3:
        if class_bits >> 8 or (class_bits & 0x0F) > 2 or ((class_bits >> 4) & 0x0F) > 1:
            _raise_invalid_hdf5()
        return _Hdf5DatatypeInfo(element_size, "fixed_string"), 8
    if datatype_class == 9:
        vlen_type = class_bits & 0x0F
        padding = (class_bits >> 4) & 0x0F
        charset = (class_bits >> 8) & 0x0F
        if vlen_type != 1 or padding > 2 or charset > 1 or class_bits >> 12:
            _raise_invalid_hdf5()
        parent, consumed = _parse_hdf5_datatype(data[8:], depth=depth + 1)
        if parent.kind != "fixed" or parent.element_size != 1:
            _raise_invalid_hdf5()
        return _Hdf5DatatypeInfo(element_size, "vlen_string"), 8 + consumed
    _raise_invalid_hdf5()


def _validate_hdf5_datatype_message(data: bytes, *, padded: bool) -> _Hdf5DatatypeInfo:
    datatype, consumed = _parse_hdf5_datatype(data)
    _hdf5_message_payload(data, required_size=consumed, padded=padded)
    return datatype


def _validate_hdf5_fill_value_message(data: bytes, *, padded: bool) -> int | None:
    if len(data) < 2:
        _raise_invalid_hdf5()
    version = data[0]
    if version in {1, 2}:
        if len(data) < 4 or data[1] not in {1, 2, 3} or data[2] > 2 or data[3] > 1:
            _raise_invalid_hdf5()
        if not data[3] and version == 2:
            _hdf5_message_payload(data, required_size=4, padded=padded)
            return None
        if len(data) < 8:
            _raise_invalid_hdf5()
        fill_size = int.from_bytes(data[4:8], "little")
        _hdf5_message_payload(data, required_size=8 + fill_size, padded=padded)
        return fill_size
    if version == 3:
        flags = data[1]
        if (
            flags & 0xC0
            or flags & 0x30 == 0x30
            or flags & 0x03 not in {1, 2, 3}
            or (flags >> 2) & 0x03 > 2
        ):
            _raise_invalid_hdf5()
        if not flags & 0x20:
            _hdf5_message_payload(data, required_size=2, padded=padded)
            return None
        if len(data) < 6:
            _raise_invalid_hdf5()
        fill_size = int.from_bytes(data[2:6], "little")
        _hdf5_message_payload(data, required_size=6 + fill_size, padded=padded)
        return fill_size
    _raise_invalid_hdf5()


def _validate_hdf5_old_fill_value_message(data: bytes, *, padded: bool) -> int:
    if len(data) < 4:
        _raise_invalid_hdf5()
    fill_size = int.from_bytes(data[:4], "little")
    _hdf5_message_payload(data, required_size=4 + fill_size, padded=padded)
    return fill_size


def _validate_hdf5_layout_message(
    data: bytes,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    padded: bool,
    range_ledger: _Hdf5RangeLedger,
) -> _Hdf5LayoutInfo:
    if len(data) < 2 or data[0] != 3 or data[1] not in {0, 1}:
        # Chunk indexes, filters, external raw storage, and virtual datasets
        # require their own bounded traversals and remain fail-closed.
        _raise_invalid_hdf5()
    if data[1] == 0:
        if len(data) < 4:
            _raise_invalid_hdf5()
        storage_size = int.from_bytes(data[2:4], "little")
        _hdf5_message_payload(data, required_size=4 + storage_size, padded=padded)
        return _Hdf5LayoutInfo("compact", None, storage_size)
    required_size = 2 + offset_width + length_width
    payload = _hdf5_message_payload(data, required_size=required_size, padded=padded)
    address = _hdf5_address(payload, 2, offset_width)
    storage_size = int.from_bytes(payload[2 + offset_width : required_size], "little")
    if _hdf5_address_is_defined(address, offset_width):
        if (
            address <= 0
            or address >= address_space_size
            or signature_offset + address + storage_size > stored_eof
        ):
            _raise_invalid_hdf5()
        storage_address: int | None = address
        range_ledger.add_raw(
            signature_offset + address,
            signature_offset + address + storage_size,
        )
    else:
        storage_address = None
    return _Hdf5LayoutInfo("contiguous", storage_address, storage_size)


def _parse_hdf5_global_heap_collection(
    stream: BinaryIO,
    *,
    length_width: int,
    physical_address: int,
    stored_eof: int,
) -> _Hdf5GlobalHeapCollectionInfo:
    collection_header_size = 8 + length_width
    stream.seek(physical_address)
    header = stream.read(collection_header_size)
    if (
        len(header) != collection_header_size
        or header[:4] != b"GCOL"
        or header[4] != 1
        or header[5:8] != b"\x00" * 3
    ):
        _raise_invalid_hdf5()
    collection_size = int.from_bytes(header[8:], "little")
    if (
        collection_size < 4096
        or collection_size % 8
        or collection_size > _MAX_HDF5_VALIDATED_METADATA_BYTES
        or physical_address + collection_size > stored_eof
    ):
        _raise_invalid_hdf5()
    # The global heap collection is one metadata allocation. Its object bodies
    # are embedded in that allocation, so registering them separately as raw
    # ranges would create a false self-overlap.
    object_header_size = 8 + length_width
    cursor = collection_header_size
    seen: set[int] = set()
    object_sizes: dict[int, int] = {}
    while cursor < collection_size:
        remaining = collection_size - cursor
        if remaining < object_header_size:
            # A terminal fragment too small for object zero has an implied
            # free-space header. Keep its bytes within the collection range.
            cursor = collection_size
            break
        stream.seek(physical_address + cursor)
        object_header = stream.read(object_header_size)
        index = int.from_bytes(object_header[0:2], "little")
        reference_count = int.from_bytes(object_header[2:4], "little")
        object_size = int.from_bytes(object_header[8:], "little")
        if object_header[4:8] != b"\x00" * 4 or index in seen:
            _raise_invalid_hdf5()
        seen.add(index)
        if len(seen) > _MAX_HDF5_OBJECT_MESSAGES:
            _raise_invalid_hdf5()
        if index == 0:
            # h5py writes the free-space object's size as the complete
            # remaining allocation, including this header.
            if reference_count != 0 or object_size != remaining:
                _raise_invalid_hdf5()
            cursor = collection_size
            break
        allocated_size = (object_size + 7) & ~7
        object_end = cursor + object_header_size + allocated_size
        if object_end > collection_size:
            _raise_invalid_hdf5()
        object_sizes[index] = object_size
        # The published format requires a positive reference count here, but
        # HDF5/h5py 3.16 writes zero for live variable-length string objects.
        # Accept that standard-writer compatibility profile while validating
        # every allocation boundary and the terminal free-space object.
        cursor = object_end
    if cursor != collection_size:
        _raise_invalid_hdf5()
    return _Hdf5GlobalHeapCollectionInfo(collection_size, object_sizes)


def _validate_hdf5_global_heap_object(
    stream: BinaryIO,
    *,
    collection_address: int,
    object_index: int,
    expected_size: int,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    range_ledger: _Hdf5RangeLedger,
) -> int:
    if object_index == 0 or not _hdf5_metadata_address_is_valid(
        collection_address,
        width=offset_width,
        address_space_size=address_space_size,
        signature_offset=signature_offset,
        stored_eof=stored_eof,
    ):
        _raise_invalid_hdf5()
    physical_address = signature_offset + collection_address
    collection = range_ledger.global_heap_collections.get(physical_address)
    if collection is None:
        collection = _parse_hdf5_global_heap_collection(
            stream,
            length_width=length_width,
            physical_address=physical_address,
            stored_eof=stored_eof,
        )
        range_ledger.add_metadata(
            physical_address, physical_address + collection.collection_size
        )
        range_ledger.global_heap_collections[physical_address] = collection
        validated_bytes = collection.collection_size
    else:
        validated_bytes = 0
    if collection.object_sizes.get(object_index) != expected_size:
        _raise_invalid_hdf5()
    return validated_bytes


def _validate_hdf5_attribute_message(
    stream: BinaryIO,
    data: bytes,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    padded: bool,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[bytes, int]:
    if len(data) < 8 or data[0] not in {1, 2, 3}:
        _raise_invalid_hdf5()
    version = data[0]
    if data[1] & ~0x03 or data[1] & 0x03:
        # Nested shared datatype/dataspace wrappers are not traversed.
        _raise_invalid_hdf5()
    header_size = 9 if version == 3 else 8
    if len(data) < header_size or (version == 1 and data[1] != 0):
        _raise_invalid_hdf5()
    if version == 3 and data[8] not in {0, 1}:
        _raise_invalid_hdf5()
    name_size = int.from_bytes(data[2:4], "little")
    datatype_size = int.from_bytes(data[4:6], "little")
    dataspace_size = int.from_bytes(data[6:8], "little")
    if (
        not 1 < name_size <= _MAX_HDF5_LINK_NAME_BYTES + 1
        or datatype_size < 8
        or dataspace_size < 4
    ):
        _raise_invalid_hdf5()
    align = (lambda size: (size + 7) & ~7) if version == 1 else (lambda size: size)
    name_storage_size = align(name_size)
    datatype_storage_size = align(datatype_size)
    dataspace_storage_size = align(dataspace_size)
    name_start = header_size
    datatype_start = name_start + name_storage_size
    dataspace_start = datatype_start + datatype_storage_size
    raw_start = dataspace_start + dataspace_storage_size
    if raw_start > len(data):
        _raise_invalid_hdf5()
    name_field = data[name_start : name_start + name_size]
    if (
        name_field[-1:] != b"\x00"
        or b"\x00" in name_field[:-1]
        or any(data[name_start + name_size : datatype_start])
        or any(data[datatype_start + datatype_size : dataspace_start])
        or any(data[dataspace_start + dataspace_size : raw_start])
    ):
        _raise_invalid_hdf5()
    try:
        name_field[:-1].decode(
            "utf-8" if version == 3 and data[8] == 1 else "ascii",
            errors="strict",
        )
    except UnicodeDecodeError:
        _raise_invalid_hdf5()
    datatype = _validate_hdf5_datatype_message(
        data[datatype_start : datatype_start + datatype_size], padded=False
    )
    dataspace = _validate_hdf5_dataspace_message(
        data[dataspace_start : dataspace_start + dataspace_size],
        length_width=length_width,
        padded=False,
    )
    if (
        dataspace.point_count
        and datatype.element_size
        > _MAX_HDF5_OBJECT_HEADER_BYTES // dataspace.point_count
    ):
        _raise_invalid_hdf5()
    raw_size = datatype.element_size * dataspace.point_count
    payload = _hdf5_message_payload(
        data, required_size=raw_start + raw_size, padded=padded
    )
    validated_bytes = 0
    if datatype.kind == "vlen_string":
        if datatype.element_size != offset_width + 8:
            _raise_invalid_hdf5()
        for cursor in range(raw_start, raw_start + raw_size, datatype.element_size):
            string_size = int.from_bytes(payload[cursor : cursor + 4], "little")
            collection_address = _hdf5_address(payload, cursor + 4, offset_width)
            object_index = int.from_bytes(
                payload[cursor + 4 + offset_width : cursor + datatype.element_size],
                "little",
            )
            if string_size > _MAX_HDF5_OBJECT_HEADER_BYTES:
                _raise_invalid_hdf5()
            validated_bytes += _validate_hdf5_global_heap_object(
                stream,
                collection_address=collection_address,
                object_index=object_index,
                expected_size=string_size,
                offset_width=offset_width,
                length_width=length_width,
                address_space_size=address_space_size,
                signature_offset=signature_offset,
                stored_eof=stored_eof,
                range_ledger=range_ledger,
            )
    return name_field[:-1], validated_bytes


def _validate_hdf5_attribute_info_message(
    data: bytes, *, offset_width: int, padded: bool
) -> None:
    if (
        len(data) < 2
        or data[0] != 0
        or data[1] & ~0x03
        or data[1] & 0x02
        and not data[1] & 0x01
    ):
        _raise_invalid_hdf5()
    flags = data[1]
    address_start = 2 + (8 if flags & 0x01 else 0)
    address_count = 2 + (1 if flags & 0x02 else 0)
    payload = _hdf5_message_payload(
        data,
        required_size=address_start + address_count * offset_width,
        padded=padded,
    )
    for index in range(address_count):
        address = _hdf5_address(
            payload, address_start + index * offset_width, offset_width
        )
        if _hdf5_address_is_defined(address, offset_width):
            # Dense attributes live in a fractal heap and v2 B-trees. They are
            # excluded until all attribute messages can be walked there.
            _raise_invalid_hdf5()


def _validate_hdf5_link_info_message(
    data: bytes,
    *,
    offset_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    padded: bool,
) -> None:
    if len(data) < 2 or data[0] != 0:
        _raise_invalid_hdf5()
    flags = data[1]
    if flags & ~0x03 or (flags & 0x02 and not flags & 0x01):
        _raise_invalid_hdf5()
    address_start = 2 + (8 if flags & 0x01 else 0)
    address_count = 2 + (1 if flags & 0x02 else 0)
    required_size = address_start + (address_count * offset_width)
    payload = _hdf5_message_payload(data, required_size=required_size, padded=padded)
    has_dense_storage = False
    for index in range(address_count):
        address = _hdf5_address(
            payload, address_start + (index * offset_width), offset_width
        )
        if _hdf5_address_is_defined(address, offset_width):
            has_dense_storage = True
            if not _hdf5_metadata_address_is_valid(
                address,
                width=offset_width,
                address_space_size=address_space_size,
                signature_offset=signature_offset,
                stored_eof=stored_eof,
            ):
                _raise_invalid_hdf5()
    if has_dense_storage:
        # Dense groups store links in a fractal heap plus v2 B-trees. Until
        # those indexes are walked, accepting the object would leave linked
        # object headers outside the validated graph.
        _raise_invalid_hdf5()


def _validate_hdf5_group_info_message(data: bytes, *, padded: bool) -> None:
    if len(data) < 2 or data[0] != 0 or data[1] & ~0x03:
        _raise_invalid_hdf5()
    required_size = 2 + (4 if data[1] & 0x01 else 0) + (4 if data[1] & 0x02 else 0)
    _hdf5_message_payload(data, required_size=required_size, padded=padded)


def _validate_hdf5_link_message(
    data: bytes,
    *,
    offset_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    padded: bool,
) -> int | None:
    if len(data) < 3 or data[0] != 1 or data[1] & ~0x1F:
        _raise_invalid_hdf5()
    flags = data[1]
    cursor = 2
    link_type = 0
    if flags & 0x08:
        if cursor >= len(data):
            _raise_invalid_hdf5()
        link_type = data[cursor]
        cursor += 1
    if flags & 0x04:
        cursor += 8
    if flags & 0x10:
        if cursor >= len(data) or data[cursor] not in {0, 1}:
            _raise_invalid_hdf5()
        cursor += 1
    name_length_width = 1 << (flags & 0x03)
    if cursor + name_length_width > len(data):
        _raise_invalid_hdf5()
    name_length = int.from_bytes(data[cursor : cursor + name_length_width], "little")
    cursor += name_length_width
    name_end = cursor + name_length
    if (
        not 0 < name_length <= _MAX_HDF5_LINK_NAME_BYTES
        or name_end > len(data)
        or b"\x00" in data[cursor:name_end]
    ):
        _raise_invalid_hdf5()
    cursor = name_end

    if link_type == 0:
        payload = _hdf5_message_payload(
            data, required_size=cursor + offset_width, padded=padded
        )
        address = _hdf5_address(payload, cursor, offset_width)
        if not _hdf5_metadata_address_is_valid(
            address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        ):
            _raise_invalid_hdf5()
        return address
    if link_type == 1:
        if cursor + 2 > len(data):
            _raise_invalid_hdf5()
        value_length = int.from_bytes(data[cursor : cursor + 2], "little")
        if not 0 < value_length <= _MAX_HDF5_LINK_NAME_BYTES:
            _raise_invalid_hdf5()
        _hdf5_message_payload(
            data,
            required_size=cursor + 2 + value_length,
            padded=padded,
        )
        return None
    # External and user-defined links need format-specific payload validation.
    _raise_invalid_hdf5()


def _read_hdf5_local_heap(
    stream: BinaryIO,
    heap_address: int,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[int, int, int]:
    header_size = 8 + (2 * length_width) + offset_width
    physical_address = signature_offset + heap_address
    if physical_address + header_size > stored_eof:
        _raise_invalid_hdf5()
    stream.seek(physical_address)
    header = stream.read(header_size)
    if (
        len(header) != header_size
        or header[:4] != b"HEAP"
        or header[4] != 0
        or header[5:8] != b"\x00" * 3
    ):
        _raise_invalid_hdf5()
    data_size = int.from_bytes(header[8 : 8 + length_width], "little")
    free_offset = int.from_bytes(
        header[8 + length_width : 8 + (2 * length_width)], "little"
    )
    data_address = _hdf5_address(header, 8 + (2 * length_width), offset_width)
    if (
        data_size <= 0
        or data_size > address_space_size
        or (
            _hdf5_address_is_defined(free_offset, length_width)
            and free_offset >= data_size
        )
        or not _hdf5_metadata_address_is_valid(
            data_address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        )
        or signature_offset + data_address + data_size > stored_eof
    ):
        _raise_invalid_hdf5()
    range_ledger.add_metadata(physical_address, physical_address + header_size)
    range_ledger.add_metadata(
        signature_offset + data_address,
        signature_offset + data_address + data_size,
    )
    return data_address, data_size, header_size


def _validate_hdf5_heap_name(
    stream: BinaryIO,
    *,
    name_offset: int,
    data_address: int,
    data_size: int,
    signature_offset: int,
    allow_empty: bool = False,
) -> bytes:
    if name_offset >= data_size:
        _raise_invalid_hdf5()
    available = min(data_size - name_offset, _MAX_HDF5_LINK_NAME_BYTES + 1)
    stream.seek(signature_offset + data_address + name_offset)
    name = stream.read(available)
    terminator = name.find(b"\x00")
    if (
        terminator < 0
        or terminator > _MAX_HDF5_LINK_NAME_BYTES
        or (terminator == 0 and not allow_empty)
    ):
        _raise_invalid_hdf5()
    return name[:terminator]


@dataclass(frozen=True, slots=True)
class _Hdf5GroupTreeNodeInfo:
    level: int
    fixed_header: bytes
    key_offsets: tuple[int, ...]
    key_names: tuple[bytes, ...]
    child_addresses: tuple[int, ...]


def _read_hdf5_group_tree_header(
    stream: BinaryIO,
    node_address: int,
    *,
    expected_level: int | None,
    offset_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    group_leaf_node_k: int,
    group_internal_node_k: int,
) -> bytes:
    fixed_size = 8 + (2 * offset_width)
    physical_address = signature_offset + node_address
    if (
        not _hdf5_metadata_address_is_valid(
            node_address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        )
        or physical_address + fixed_size > stored_eof
    ):
        _raise_invalid_hdf5()
    stream.seek(physical_address)
    fixed = stream.read(fixed_size)
    if (
        len(fixed) != fixed_size
        or fixed[:4] != b"TREE"
        or fixed[4] != 0
        or fixed[5] > _MAX_HDF5_BTREE_DEPTH
        or (expected_level is not None and fixed[5] != expected_level)
    ):
        _raise_invalid_hdf5()
    level = fixed[5]
    entries = int.from_bytes(fixed[6:8], "little")
    # Group Internal Node K controls v1 group B-tree nodes at every level;
    # Group Leaf Node K controls the SNOD entries reached by level-zero nodes.
    max_entries = 2 * group_internal_node_k
    if entries > max_entries or (level > 0 and entries == 0):
        _raise_invalid_hdf5()
    for sibling_offset in (8, 8 + offset_width):
        sibling = _hdf5_address(fixed, sibling_offset, offset_width)
        if _hdf5_address_is_defined(sibling, offset_width) and (
            sibling == node_address
            or not _hdf5_metadata_address_is_valid(
                sibling,
                width=offset_width,
                address_space_size=address_space_size,
                signature_offset=signature_offset,
                stored_eof=stored_eof,
            )
        ):
            _raise_invalid_hdf5()
    return fixed


def _collect_hdf5_symbol_table_links(
    stream: BinaryIO,
    *,
    btree_address: int,
    heap_address: int,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    group_leaf_node_k: int,
    group_internal_node_k: int,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[set[int], int]:
    data_address, data_size, validated_bytes = _read_hdf5_local_heap(
        stream,
        heap_address,
        offset_width=offset_width,
        length_width=length_width,
        address_space_size=address_space_size,
        signature_offset=signature_offset,
        stored_eof=stored_eof,
        range_ledger=range_ledger,
    )
    if _validate_hdf5_heap_name(
        stream,
        name_offset=0,
        data_address=data_address,
        data_size=data_size,
        signature_offset=signature_offset,
        allow_empty=True,
    ):
        # Group TREE offset zero is the canonical empty-string left sentinel.
        _raise_invalid_hdf5()
    object_addresses: set[int] = set()
    pending_nodes: list[tuple[int, int | None]] = [(btree_address, None)]
    visited_nodes: set[int] = set()
    visited_symbol_nodes: set[int] = set()
    tree_nodes: dict[int, _Hdf5GroupTreeNodeInfo] = {}
    subtree_bounds: dict[int, tuple[tuple[int, bytes], tuple[int, bytes]]] = {}
    while pending_nodes:
        node_address, expected_level = pending_nodes.pop()
        if node_address in visited_nodes:
            _raise_invalid_hdf5()
        visited_nodes.add(node_address)
        if len(visited_nodes) > _MAX_HDF5_BTREE_NODES:
            _raise_invalid_hdf5()
        fixed_size = 8 + (2 * offset_width)
        fixed = _read_hdf5_group_tree_header(
            stream,
            node_address,
            expected_level=expected_level,
            offset_width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            group_leaf_node_k=group_leaf_node_k,
            group_internal_node_k=group_internal_node_k,
        )
        physical_address = signature_offset + node_address
        level = fixed[5]
        entries = int.from_bytes(fixed[6:8], "little")
        if node_address != btree_address and entries == 0:
            _raise_invalid_hdf5()
        if node_address == btree_address and any(
            _hdf5_address_is_defined(
                _hdf5_address(fixed, sibling_offset, offset_width), offset_width
            )
            for sibling_offset in (8, 8 + offset_width)
        ):
            # The root is the only node at the highest B-tree level and cannot
            # participate in that level's sibling list.
            _raise_invalid_hdf5()

        node_data_size = (
            ((entries + 1) * length_width) + (entries * offset_width) if entries else 0
        )
        max_entries = 2 * group_internal_node_k
        allocated_node_data_size = ((max_entries + 1) * length_width) + (
            max_entries * offset_width
        )
        if physical_address + fixed_size + allocated_node_data_size > stored_eof:
            _raise_invalid_hdf5()
        node_data = stream.read(node_data_size)
        if len(node_data) != node_data_size:
            _raise_invalid_hdf5()
        validated_bytes += fixed_size + allocated_node_data_size
        range_ledger.add_metadata(
            physical_address,
            physical_address + fixed_size + allocated_node_data_size,
        )
        if entries == 0:
            tree_nodes[node_address] = _Hdf5GroupTreeNodeInfo(
                level=level,
                fixed_header=fixed,
                key_offsets=(),
                key_names=(),
                child_addresses=(),
            )
            continue
        cursor = 0
        key_offsets: list[int] = []
        child_addresses: list[int] = []
        for _ in range(entries):
            key_offsets.append(
                int.from_bytes(node_data[cursor : cursor + length_width], "little")
            )
            cursor += length_width
            child_address = _hdf5_address(node_data, cursor, offset_width)
            cursor += offset_width
            if not _hdf5_metadata_address_is_valid(
                child_address,
                width=offset_width,
                address_space_size=address_space_size,
                signature_offset=signature_offset,
                stored_eof=stored_eof,
            ):
                _raise_invalid_hdf5()
            child_addresses.append(child_address)
        key_offsets.append(
            int.from_bytes(node_data[cursor : cursor + length_width], "little")
        )
        key_names = tuple(
            _validate_hdf5_heap_name(
                stream,
                name_offset=key_offset,
                data_address=data_address,
                data_size=data_size,
                signature_offset=signature_offset,
                allow_empty=True,
            )
            for key_offset in key_offsets
        )
        if (
            (node_address == btree_address and key_offsets[0] != 0)
            or any(not key for key in key_names[1:])
            or any(
                left >= right
                for left, right in zip(key_names, key_names[1:], strict=False)
            )
        ):
            _raise_invalid_hdf5()
        tree_nodes[node_address] = _Hdf5GroupTreeNodeInfo(
            level=level,
            fixed_header=fixed,
            key_offsets=tuple(key_offsets),
            key_names=key_names,
            child_addresses=tuple(child_addresses),
        )
        if level:
            pending_nodes.extend(
                (child_address, level - 1) for child_address in child_addresses
            )
            continue

        leaf_first: tuple[int, bytes] | None = None
        leaf_last: tuple[int, bytes] | None = None
        for child_index, child_address in enumerate(child_addresses):
            if child_address in visited_symbol_nodes:
                _raise_invalid_hdf5()
            visited_symbol_nodes.add(child_address)
            symbol_header_size = 8
            stream.seek(signature_offset + child_address)
            symbol_header = stream.read(symbol_header_size)
            if (
                len(symbol_header) != symbol_header_size
                or symbol_header[:4] != b"SNOD"
                or symbol_header[4] != 1
                or symbol_header[5] != 0
            ):
                _raise_invalid_hdf5()
            symbol_count = int.from_bytes(symbol_header[6:8], "little")
            if symbol_count == 0 or symbol_count > 2 * group_leaf_node_k:
                _raise_invalid_hdf5()
            # H5G_SIZEOF_ENTRY/H5G_ent_decode use Size of Lengths for the
            # local-heap name offset and Size of Offsets for the object header
            # address. Some published format tables incorrectly mark both O.
            symbol_entry_size = length_width + offset_width + 24
            allocated_symbol_size = (2 * group_leaf_node_k) * symbol_entry_size
            if (
                signature_offset
                + child_address
                + symbol_header_size
                + allocated_symbol_size
                > stored_eof
            ):
                _raise_invalid_hdf5()
            stream.seek(signature_offset + child_address + symbol_header_size)
            symbols = stream.read(symbol_count * symbol_entry_size)
            if len(symbols) != symbol_count * symbol_entry_size:
                _raise_invalid_hdf5()
            validated_bytes += symbol_header_size + allocated_symbol_size
            range_ledger.add_metadata(
                signature_offset + child_address,
                signature_offset
                + child_address
                + symbol_header_size
                + allocated_symbol_size,
            )
            symbol_names: list[bytes] = []
            symbol_name_offsets: list[int] = []
            for symbol_index in range(symbol_count):
                symbol = symbols[
                    symbol_index * symbol_entry_size : (symbol_index + 1)
                    * symbol_entry_size
                ]
                name_offset = _hdf5_address(symbol, 0, length_width)
                symbol_name_offsets.append(name_offset)
                symbol_names.append(
                    _validate_hdf5_heap_name(
                        stream,
                        name_offset=name_offset,
                        data_address=data_address,
                        data_size=data_size,
                        signature_offset=signature_offset,
                    )
                )
                cache_offset = length_width + offset_width
                cache_type = int.from_bytes(
                    symbol[cache_offset : cache_offset + 4], "little"
                )
                if (
                    cache_type not in {0, 1, 2}
                    or symbol[cache_offset + 4 : cache_offset + 8] != b"\x00" * 4
                ):
                    _raise_invalid_hdf5()
                if cache_type != 2:
                    object_address = _hdf5_address(symbol, length_width, offset_width)
                    if not _hdf5_metadata_address_is_valid(
                        object_address,
                        width=offset_width,
                        address_space_size=address_space_size,
                        signature_offset=signature_offset,
                        stored_eof=stored_eof,
                    ):
                        _raise_invalid_hdf5()
                    object_addresses.add(object_address)
            if (
                any(
                    left >= right
                    for left, right in zip(symbol_names, symbol_names[1:], strict=False)
                )
                or key_names[child_index] >= symbol_names[0]
                or key_names[child_index + 1] != symbol_names[-1]
                or key_offsets[child_index + 1] != symbol_name_offsets[-1]
            ):
                _raise_invalid_hdf5()
            if leaf_first is None:
                leaf_first = (symbol_name_offsets[0], symbol_names[0])
            leaf_last = (symbol_name_offsets[-1], symbol_names[-1])
        if entries:
            if leaf_first is None or leaf_last is None:
                _raise_invalid_hdf5()
            subtree_bounds[node_address] = (leaf_first, leaf_last)

    max_level = max(node.level for node in tree_nodes.values())
    for current_level in range(1, max_level + 1):
        for node_address, node in tuple(tree_nodes.items()):
            if node.level != current_level:
                continue
            child_bounds: list[tuple[tuple[int, bytes], tuple[int, bytes]]] = []
            for child_index, child_address in enumerate(node.child_addresses):
                child_node = tree_nodes.get(child_address)
                bounds = subtree_bounds.get(child_address)
                if (
                    child_node is None
                    or bounds is None
                    or child_node.level != current_level - 1
                    or (
                        child_node.key_offsets[0],
                        child_node.key_names[0],
                    )
                    != (
                        node.key_offsets[child_index],
                        node.key_names[child_index],
                    )
                    or (
                        child_node.key_offsets[-1],
                        child_node.key_names[-1],
                    )
                    != (
                        node.key_offsets[child_index + 1],
                        node.key_names[child_index + 1],
                    )
                    or bounds[0][1] <= node.key_names[child_index]
                    or bounds[1]
                    != (
                        node.key_offsets[child_index + 1],
                        node.key_names[child_index + 1],
                    )
                ):
                    _raise_invalid_hdf5()
                child_bounds.append(bounds)
            if not child_bounds:
                _raise_invalid_hdf5()
            subtree_bounds[node_address] = (
                child_bounds[0][0],
                child_bounds[-1][1],
            )

    nodes_by_level: dict[int, set[int]] = {}
    for node_address, node in tuple(tree_nodes.items()):
        nodes_by_level.setdefault(node.level, set()).add(node_address)
        for sibling_offset, backlink_offset, is_right in (
            (8, 8 + offset_width, False),
            (8 + offset_width, 8, True),
        ):
            sibling_address = _hdf5_address(
                node.fixed_header, sibling_offset, offset_width
            )
            if not _hdf5_address_is_defined(sibling_address, offset_width):
                continue
            sibling_node = tree_nodes.get(sibling_address)
            if (
                sibling_node is None
                or sibling_node.level != node.level
                or _hdf5_address(
                    sibling_node.fixed_header, backlink_offset, offset_width
                )
                != node_address
            ):
                _raise_invalid_hdf5()
            left_node, right_node = (
                (node, sibling_node) if is_right else (sibling_node, node)
            )
            if (
                left_node.key_offsets[-1],
                left_node.key_names[-1],
            ) != (right_node.key_offsets[0], right_node.key_names[0]):
                _raise_invalid_hdf5()

    for level_addresses in nodes_by_level.values():
        leftmost = [
            address
            for address in level_addresses
            if not _hdf5_address_is_defined(
                _hdf5_address(tree_nodes[address].fixed_header, 8, offset_width),
                offset_width,
            )
        ]
        rightmost = [
            address
            for address in level_addresses
            if not _hdf5_address_is_defined(
                _hdf5_address(
                    tree_nodes[address].fixed_header, 8 + offset_width, offset_width
                ),
                offset_width,
            )
        ]
        if len(leftmost) != 1 or len(rightmost) != 1:
            _raise_invalid_hdf5()
        visited_level: set[int] = set()
        node_address = leftmost[0]
        while True:
            if node_address in visited_level:
                _raise_invalid_hdf5()
            visited_level.add(node_address)
            sibling_address = _hdf5_address(
                tree_nodes[node_address].fixed_header,
                8 + offset_width,
                offset_width,
            )
            if not _hdf5_address_is_defined(sibling_address, offset_width):
                break
            node_address = sibling_address
        if node_address != rightmost[0] or visited_level != level_addresses:
            _raise_invalid_hdf5()
    return object_addresses, validated_bytes


def _validate_hdf5_symbol_table_message(
    stream: BinaryIO,
    data: bytes,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    root_cache_addresses: tuple[int, ...],
    group_leaf_node_k: int,
    group_internal_node_k: int,
    padded: bool,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[set[int], int]:
    payload = _hdf5_message_payload(data, required_size=2 * offset_width, padded=padded)
    addresses = (
        _hdf5_address(payload, 0, offset_width),
        _hdf5_address(payload, offset_width, offset_width),
    )
    if root_cache_addresses and addresses != root_cache_addresses:
        _raise_invalid_hdf5()
    if any(
        not _hdf5_metadata_address_is_valid(
            address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        )
        for address in addresses
    ):
        _raise_invalid_hdf5()
    for address, signature in zip(addresses, (b"TREE", b"HEAP"), strict=True):
        stream.seek(signature_offset + address)
        if stream.read(4) != signature:
            _raise_invalid_hdf5()
    return _collect_hdf5_symbol_table_links(
        stream,
        btree_address=addresses[0],
        heap_address=addresses[1],
        offset_width=offset_width,
        length_width=length_width,
        address_space_size=address_space_size,
        signature_offset=signature_offset,
        stored_eof=stored_eof,
        group_leaf_node_k=group_leaf_node_k,
        group_internal_node_k=group_internal_node_k,
        range_ledger=range_ledger,
    )


def _validate_hdf5_root_message(
    stream: BinaryIO,
    message_type: int,
    data: bytes,
    *,
    object_state: _Hdf5ObjectMessageState,
    range_ledger: _Hdf5RangeLedger,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    root_cache_addresses: tuple[int, ...],
    group_leaf_node_k: int,
    group_internal_node_k: int,
    padded: bool,
) -> tuple[bool, set[int], int]:
    if message_type == 0x0010:
        # Object-header continuation chunks need their own overlap, cycle, and
        # checksum validation. This admission profile rejects them until that
        # complete traversal is implemented instead of trusting an address.
        _raise_invalid_hdf5()
    if message_type == 0x0000:
        if any(data):
            _raise_invalid_hdf5()
        return False, set(), 0
    if message_type == 0x0001:
        object_state.dataspace = _validate_hdf5_dataspace_message(
            data, length_width=length_width, padded=padded
        )
        return False, set(), 0
    if message_type == 0x0002:
        _validate_hdf5_link_info_message(
            data,
            offset_width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            padded=padded,
        )
        return True, set(), 0
    if message_type == 0x0003:
        object_state.datatype = _validate_hdf5_datatype_message(data, padded=padded)
        return False, set(), 0
    if message_type == 0x0004:
        object_state.old_fill_size = _validate_hdf5_old_fill_value_message(
            data, padded=padded
        )
        return False, set(), 0
    if message_type == 0x0005:
        object_state.fill_size = _validate_hdf5_fill_value_message(data, padded=padded)
        return False, set(), 0
    if message_type == 0x0006:
        linked_address = _validate_hdf5_link_message(
            data,
            offset_width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            padded=padded,
        )
        return False, ({linked_address} if linked_address is not None else set()), 0
    if message_type == 0x0008:
        object_state.layout = _validate_hdf5_layout_message(
            data,
            offset_width=offset_width,
            length_width=length_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            padded=padded,
            range_ledger=range_ledger,
        )
        return False, set(), 0
    if message_type == 0x000A:
        _validate_hdf5_group_info_message(data, padded=padded)
        return True, set(), 0
    if message_type == 0x0011:
        linked_addresses, validated_bytes = _validate_hdf5_symbol_table_message(
            stream,
            data,
            offset_width=offset_width,
            length_width=length_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            root_cache_addresses=root_cache_addresses,
            group_leaf_node_k=group_leaf_node_k,
            group_internal_node_k=group_internal_node_k,
            padded=padded,
            range_ledger=range_ledger,
        )
        return True, linked_addresses, validated_bytes
    if message_type == 0x000C:
        attribute_name, validated_bytes = _validate_hdf5_attribute_message(
            stream,
            data,
            offset_width=offset_width,
            length_width=length_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
            padded=padded,
            range_ledger=range_ledger,
        )
        if attribute_name in object_state.attribute_names:
            _raise_invalid_hdf5()
        object_state.attribute_names.add(attribute_name)
        return False, set(), validated_bytes
    if message_type == 0x0015:
        _validate_hdf5_attribute_info_message(
            data, offset_width=offset_width, padded=padded
        )
        return False, set(), 0
    _raise_invalid_hdf5()


def _validate_hdf5_object_composition(
    object_state: _Hdf5ObjectMessageState, *, require_group: bool
) -> None:
    counts = object_state.counts
    dataset_types = {0x0001, 0x0003, 0x0004, 0x0005, 0x0008}
    dataset_core_types = {0x0001, 0x0003, 0x0005, 0x0008}
    group_types = {0x0002, 0x0006, 0x000A, 0x0011}
    has_dataset_message = any(counts.get(item, 0) for item in dataset_types)
    has_group_message = any(counts.get(item, 0) for item in group_types)
    if has_dataset_message:
        if (
            require_group
            or has_group_message
            or any(counts.get(item, 0) != 1 for item in dataset_core_types)
            or object_state.dataspace is None
            or object_state.datatype is None
            or object_state.layout is None
            or object_state.datatype.kind == "vlen_string"
        ):
            _raise_invalid_hdf5()
        expected_storage_size = (
            object_state.dataspace.point_count * object_state.datatype.element_size
        )
        layout = object_state.layout
        if (
            object_state.fill_size not in {None, 0, object_state.datatype.element_size}
            or object_state.old_fill_size
            not in {None, 0, object_state.datatype.element_size}
            or (
                object_state.old_fill_size is not None
                and object_state.fill_size is not None
                and object_state.old_fill_size != object_state.fill_size
            )
            or layout.storage_size != expected_storage_size
        ):
            _raise_invalid_hdf5()
        return
    if not has_group_message:
        _raise_invalid_hdf5()
    legacy_group = counts.get(0x0011, 0) == 1
    compact_group = (
        counts.get(0x0002, 0) == 1
        and counts.get(0x000A, 0) == 1
        and counts.get(0x0011, 0) == 0
    )
    if legacy_group == compact_group or (
        legacy_group and any(counts.get(item, 0) for item in {2, 6, 10})
    ):
        _raise_invalid_hdf5()


def _parse_hdf5_v1_continuation(
    data: bytes,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
) -> tuple[int, int]:
    payload = _hdf5_message_payload(
        data,
        required_size=offset_width + length_width,
        padded=True,
    )
    address = _hdf5_address(payload, 0, offset_width)
    length = int.from_bytes(
        payload[offset_width : offset_width + length_width], "little"
    )
    if (
        not _hdf5_metadata_address_is_valid(
            address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        )
        or not 0 < length <= _MAX_HDF5_OBJECT_HEADER_BYTES
        or length % 8
        or signature_offset + address + length > stored_eof
    ):
        _raise_invalid_hdf5()
    return signature_offset + address, length


def _parse_hdf5_v2_continuation(
    data: bytes,
    *,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    message_prefix_size: int,
) -> tuple[int, int]:
    payload = _hdf5_message_payload(
        data,
        required_size=offset_width + length_width,
        padded=False,
    )
    address = _hdf5_address(payload, 0, offset_width)
    length = int.from_bytes(
        payload[offset_width : offset_width + length_width], "little"
    )
    if (
        not _hdf5_metadata_address_is_valid(
            address,
            width=offset_width,
            address_space_size=address_space_size,
            signature_offset=signature_offset,
            stored_eof=stored_eof,
        )
        or length < 4 + message_prefix_size + 4
        or length > _MAX_HDF5_OBJECT_HEADER_BYTES
        or signature_offset + address + length > stored_eof
    ):
        _raise_invalid_hdf5()
    return signature_offset + address, length


def _validate_hdf5_v1_root_object(
    stream: BinaryIO,
    *,
    physical_address: int,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    root_cache_addresses: tuple[int, ...],
    group_leaf_node_k: int,
    group_internal_node_k: int,
    require_group_anchor: bool,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[set[int], int]:
    stream.seek(physical_address)
    prefix = stream.read(16)
    if (
        len(prefix) != 16
        or prefix[0] != 1
        or prefix[1] != 0
        or prefix[12:16] != b"\x00" * 4
    ):
        _raise_invalid_hdf5()
    message_count = int.from_bytes(prefix[2:4], "little")
    reference_count = int.from_bytes(prefix[4:8], "little")
    header_size = int.from_bytes(prefix[8:12], "little")
    if (
        not 0 < message_count <= _MAX_HDF5_OBJECT_MESSAGES
        or reference_count == 0
        or not 0 < header_size <= _MAX_HDF5_OBJECT_HEADER_BYTES
        or header_size % 8
        or physical_address + 16 + header_size > stored_eof
    ):
        _raise_invalid_hdf5()
    pending_areas = [(physical_address + 16, header_size)]
    occupied_ranges = [(physical_address, physical_address + 16 + header_size)]
    range_ledger.add_metadata(physical_address, physical_address + 16 + header_size)
    total_validated_bytes = 16 + header_size
    parsed_messages = 0
    has_group_anchor = False
    has_meaningful_message = False
    object_state = _Hdf5ObjectMessageState()
    linked_object_addresses: set[int] = set()
    while pending_areas:
        area_address, area_size = pending_areas.pop(0)
        stream.seek(area_address)
        message_area = stream.read(area_size)
        if len(message_area) != area_size:
            _raise_invalid_hdf5()
        cursor = 0
        while cursor < area_size:
            parsed_messages += 1
            if parsed_messages > message_count or cursor + 8 > area_size:
                _raise_invalid_hdf5()
            message_type = int.from_bytes(message_area[cursor : cursor + 2], "little")
            message_flags = message_area[cursor + 4]
            _validate_hdf5_object_message_header(message_type, message_flags)
            object_state.record(message_type)
            data_size = int.from_bytes(message_area[cursor + 2 : cursor + 4], "little")
            if message_area[cursor + 5 : cursor + 8] != b"\x00" * 3 or data_size % 8:
                _raise_invalid_hdf5()
            data_start = cursor + 8
            data_end = data_start + data_size
            if data_end > area_size:
                _raise_invalid_hdf5()
            message_data = message_area[data_start:data_end]
            if message_type == 0x0010:
                continuation_address, continuation_size = _parse_hdf5_v1_continuation(
                    message_data,
                    offset_width=offset_width,
                    length_width=length_width,
                    address_space_size=address_space_size,
                    signature_offset=signature_offset,
                    stored_eof=stored_eof,
                )
                continuation_end = continuation_address + continuation_size
                if any(
                    continuation_address < end and start < continuation_end
                    for start, end in occupied_ranges
                ):
                    _raise_invalid_hdf5()
                occupied_ranges.append((continuation_address, continuation_end))
                range_ledger.add_metadata(continuation_address, continuation_end)
                pending_areas.append((continuation_address, continuation_size))
                total_validated_bytes += continuation_size
            else:
                is_group_anchor, linked_addresses, validated_bytes = (
                    _validate_hdf5_root_message(
                        stream,
                        message_type,
                        message_data,
                        object_state=object_state,
                        range_ledger=range_ledger,
                        offset_width=offset_width,
                        length_width=length_width,
                        address_space_size=address_space_size,
                        signature_offset=signature_offset,
                        stored_eof=stored_eof,
                        root_cache_addresses=root_cache_addresses,
                        group_leaf_node_k=group_leaf_node_k,
                        group_internal_node_k=group_internal_node_k,
                        padded=True,
                    )
                )
                has_group_anchor |= is_group_anchor
                has_meaningful_message |= message_type != 0
                linked_object_addresses.update(linked_addresses)
                total_validated_bytes += validated_bytes
            if total_validated_bytes > _MAX_HDF5_VALIDATED_METADATA_BYTES:
                _raise_invalid_hdf5()
            cursor = data_end
    if (
        parsed_messages != message_count
        or not has_meaningful_message
        or (require_group_anchor and not has_group_anchor)
    ):
        _raise_invalid_hdf5()
    _validate_hdf5_object_composition(object_state, require_group=require_group_anchor)
    return linked_object_addresses, total_validated_bytes


def _validate_hdf5_v2_root_object(
    stream: BinaryIO,
    *,
    physical_address: int,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    signature_offset: int,
    stored_eof: int,
    root_cache_addresses: tuple[int, ...],
    group_leaf_node_k: int,
    group_internal_node_k: int,
    require_group_anchor: bool,
    range_ledger: _Hdf5RangeLedger,
) -> tuple[set[int], int]:
    stream.seek(physical_address)
    fixed_prefix = stream.read(6)
    if len(fixed_prefix) != 6 or fixed_prefix[:4] != b"OHDR" or fixed_prefix[4] != 2:
        _raise_invalid_hdf5()
    flags = fixed_prefix[5]
    if flags & 0xC0 or (flags & 0x08 and not flags & 0x04):
        _raise_invalid_hdf5()
    chunk_size_width = 1 << (flags & 0x03)
    prefix_size = (
        6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + chunk_size_width
    )
    stream.seek(physical_address)
    prefix = stream.read(prefix_size)
    if len(prefix) != prefix_size:
        _raise_invalid_hdf5()
    chunk_size = int.from_bytes(prefix[-chunk_size_width:], "little")
    object_size = prefix_size + chunk_size + 4
    if (
        chunk_size < 4
        or object_size > _MAX_HDF5_OBJECT_HEADER_BYTES
        or physical_address + object_size > stored_eof
    ):
        _raise_invalid_hdf5()
    stream.seek(physical_address)
    object_header = stream.read(object_size)
    if len(object_header) != object_size or int.from_bytes(
        object_header[-4:], "little"
    ) != _hdf5_lookup3_checksum(object_header[:-4]):
        _raise_invalid_hdf5()

    message_prefix_size = 4 + (2 if flags & 0x04 else 0)
    message_count = 0
    has_group_anchor = False
    has_meaningful_message = False
    object_state = _Hdf5ObjectMessageState()
    linked_object_addresses: set[int] = set()
    total_validated_bytes = object_size
    pending_areas = [
        (object_header, prefix_size, prefix_size + chunk_size),
    ]
    occupied_ranges = [(physical_address, physical_address + object_size)]
    range_ledger.add_metadata(physical_address, physical_address + object_size)
    while pending_areas:
        message_area, cursor, message_end = pending_areas.pop(0)
        while cursor < message_end:
            if message_end - cursor < message_prefix_size:
                if any(message_area[cursor:message_end]):
                    _raise_invalid_hdf5()
                cursor = message_end
                break
            message_count += 1
            if message_count > _MAX_HDF5_OBJECT_MESSAGES:
                _raise_invalid_hdf5()
            message_type = message_area[cursor]
            message_flags = message_area[cursor + 3]
            _validate_hdf5_object_message_header(message_type, message_flags)
            object_state.record(message_type)
            data_size = int.from_bytes(message_area[cursor + 1 : cursor + 3], "little")
            data_start = cursor + message_prefix_size
            data_end = data_start + data_size
            if data_end > message_end:
                _raise_invalid_hdf5()
            message_data = message_area[data_start:data_end]
            if message_type == 0x0010:
                continuation_address, continuation_size = _parse_hdf5_v2_continuation(
                    message_data,
                    offset_width=offset_width,
                    length_width=length_width,
                    address_space_size=address_space_size,
                    signature_offset=signature_offset,
                    stored_eof=stored_eof,
                    message_prefix_size=message_prefix_size,
                )
                continuation_end = continuation_address + continuation_size
                if any(
                    continuation_address < end and start < continuation_end
                    for start, end in occupied_ranges
                ):
                    _raise_invalid_hdf5()
                if (
                    total_validated_bytes + continuation_size
                    > _MAX_HDF5_VALIDATED_METADATA_BYTES
                ):
                    _raise_invalid_hdf5()
                stream.seek(continuation_address)
                continuation = stream.read(continuation_size)
                if (
                    len(continuation) != continuation_size
                    or continuation[:4] != b"OCHK"
                    or int.from_bytes(continuation[-4:], "little")
                    != _hdf5_lookup3_checksum(continuation[:-4])
                ):
                    _raise_invalid_hdf5()
                occupied_ranges.append((continuation_address, continuation_end))
                range_ledger.add_metadata(continuation_address, continuation_end)
                pending_areas.append((continuation, 4, continuation_size - 4))
                total_validated_bytes += continuation_size
            else:
                is_group_anchor, linked_addresses, validated_bytes = (
                    _validate_hdf5_root_message(
                        stream,
                        message_type,
                        message_data,
                        object_state=object_state,
                        range_ledger=range_ledger,
                        offset_width=offset_width,
                        length_width=length_width,
                        address_space_size=address_space_size,
                        signature_offset=signature_offset,
                        stored_eof=stored_eof,
                        root_cache_addresses=root_cache_addresses,
                        group_leaf_node_k=group_leaf_node_k,
                        group_internal_node_k=group_internal_node_k,
                        padded=False,
                    )
                )
                has_group_anchor |= is_group_anchor
                has_meaningful_message |= message_type != 0
                linked_object_addresses.update(linked_addresses)
                total_validated_bytes += validated_bytes
            if total_validated_bytes > _MAX_HDF5_VALIDATED_METADATA_BYTES:
                _raise_invalid_hdf5()
            cursor = data_end
    if (
        not has_meaningful_message
        or (require_group_anchor and not has_group_anchor)
        or total_validated_bytes > _MAX_HDF5_VALIDATED_METADATA_BYTES
    ):
        _raise_invalid_hdf5()
    _validate_hdf5_object_composition(object_state, require_group=require_group_anchor)
    return linked_object_addresses, total_validated_bytes


def _validate_hdf5_root_object(
    path: Path,
    *,
    signature_offset: int,
    root_address: int,
    stored_eof: int,
    offset_width: int,
    length_width: int,
    address_space_size: int,
    root_cache_addresses: tuple[int, ...],
    group_leaf_node_k: int,
    group_internal_node_k: int,
    range_ledger: _Hdf5RangeLedger,
) -> None:
    with path.open("rb") as stream:
        pending_objects = [root_address]
        visited_objects: set[int] = set()
        total_validated_bytes = 0
        while pending_objects:
            object_address = pending_objects.pop()
            if object_address in visited_objects:
                continue
            if len(
                visited_objects
            ) >= _MAX_HDF5_OBJECTS or not _hdf5_metadata_address_is_valid(
                object_address,
                width=offset_width,
                address_space_size=address_space_size,
                signature_offset=signature_offset,
                stored_eof=stored_eof,
            ):
                _raise_invalid_hdf5()
            visited_objects.add(object_address)
            physical_address = signature_offset + object_address
            stream.seek(physical_address)
            marker = stream.read(4)
            common_arguments = {
                "physical_address": physical_address,
                "offset_width": offset_width,
                "length_width": length_width,
                "address_space_size": address_space_size,
                "signature_offset": signature_offset,
                "stored_eof": stored_eof,
                "root_cache_addresses": (
                    root_cache_addresses if object_address == root_address else ()
                ),
                "group_leaf_node_k": group_leaf_node_k,
                "group_internal_node_k": group_internal_node_k,
                "require_group_anchor": object_address == root_address,
                "range_ledger": range_ledger,
            }
            if marker == b"OHDR":
                linked_addresses, validated_bytes = _validate_hdf5_v2_root_object(
                    stream, **common_arguments
                )
            elif marker[:1] == b"\x01":
                linked_addresses, validated_bytes = _validate_hdf5_v1_root_object(
                    stream, **common_arguments
                )
            else:
                _raise_invalid_hdf5()
            total_validated_bytes += validated_bytes
            if total_validated_bytes > _MAX_HDF5_VALIDATED_METADATA_BYTES:
                _raise_invalid_hdf5()
            pending_objects.extend(sorted(linked_addresses, reverse=True))


def _validate_hdf5(path: Path, signature_offset: int) -> None:
    file_size = path.stat().st_size
    if file_size < signature_offset + 16:
        _raise_invalid_hdf5()

    with path.open("rb") as stream:
        stream.seek(signature_offset)
        fixed_header = stream.read(16)
        if len(fixed_header) != 16 or not fixed_header.startswith(_HDF5_SIGNATURE):
            _raise_invalid_hdf5()
        version = fixed_header[8]
        if version not in {0, 1, 2, 3}:
            _raise_invalid_hdf5()

        if version < 2:
            offset_width = fixed_header[13]
            length_width = fixed_header[14]
            address_start = 24 if version == 0 else 28
            # The root symbol-table entry has the same canonical L+O layout as
            # SNOD entries: heap-name length, object address, then 24 cache bytes.
            superblock_size = address_start + (5 * offset_width) + length_width + 24
        else:
            offset_width = fixed_header[9]
            length_width = fixed_header[10]
            address_start = 12
            superblock_size = address_start + (4 * offset_width) + 4
        if (
            offset_width not in _HDF5_INTEGER_WIDTHS
            or length_width not in _HDF5_INTEGER_WIDTHS
            or file_size < signature_offset + superblock_size
        ):
            _raise_invalid_hdf5()

        stream.seek(signature_offset)
        superblock = stream.read(superblock_size)
        if len(superblock) != superblock_size:
            _raise_invalid_hdf5()

    root_cache_addresses: tuple[int, ...] = ()
    if version < 2:
        group_leaf_node_k = int.from_bytes(superblock[16:18], "little")
        group_internal_node_k = int.from_bytes(superblock[18:20], "little")
        if (
            superblock[9:13] != b"\x00\x00\x00\x00"
            or superblock[15] != 0
            or group_leaf_node_k == 0
            or group_internal_node_k == 0
        ):
            _raise_invalid_hdf5()
        status_flags = int.from_bytes(superblock[20:24], "little")
        if status_flags != 0:
            _raise_invalid_hdf5()
        if version == 1 and (
            int.from_bytes(superblock[24:26], "little") == 0
            or superblock[26:28] != b"\x00\x00"
        ):
            _raise_invalid_hdf5()

        base_address = _hdf5_address(superblock, address_start, offset_width)
        extension_address = _hdf5_address(
            superblock, address_start + offset_width, offset_width
        )
        stored_eof = _hdf5_address(
            superblock, address_start + (2 * offset_width), offset_width
        )
        driver_address = _hdf5_address(
            superblock, address_start + (3 * offset_width), offset_width
        )
        root_entry = address_start + (4 * offset_width)
        root_link_name_offset = _hdf5_address(superblock, root_entry, length_width)
        root_address = _hdf5_address(
            superblock, root_entry + length_width, offset_width
        )
        root_cache_offset = root_entry + length_width + offset_width
        root_cache_type = int.from_bytes(
            superblock[root_cache_offset : root_cache_offset + 4], "little"
        )
        root_reserved = superblock[root_cache_offset + 4 : root_cache_offset + 8]
        root_scratch = superblock[root_cache_offset + 8 : root_cache_offset + 24]
        if (
            root_link_name_offset != 0
            or root_cache_type not in {0, 1}
            or root_reserved != b"\x00" * 4
            or (root_cache_type == 0 and root_scratch != b"\x00" * 16)
        ):
            _raise_invalid_hdf5()
        if root_cache_type == 1:
            if offset_width > 8:
                _raise_invalid_hdf5()
            root_cache_addresses = (
                _hdf5_address(root_scratch, 0, offset_width),
                _hdf5_address(root_scratch, offset_width, offset_width),
            )
            if root_scratch[2 * offset_width :] != b"\x00" * (16 - (2 * offset_width)):
                _raise_invalid_hdf5()
        optional_addresses = (extension_address, driver_address)
    else:
        group_leaf_node_k = 4
        group_internal_node_k = 16
        if superblock[11] != 0:
            _raise_invalid_hdf5()
        base_address = _hdf5_address(superblock, address_start, offset_width)
        extension_address = _hdf5_address(
            superblock, address_start + offset_width, offset_width
        )
        stored_eof = _hdf5_address(
            superblock, address_start + (2 * offset_width), offset_width
        )
        root_address = _hdf5_address(
            superblock, address_start + (3 * offset_width), offset_width
        )
        optional_addresses = (extension_address,)
        stored_checksum = int.from_bytes(superblock[-4:], "little")
        if stored_checksum != _hdf5_lookup3_checksum(superblock[:-4]):
            _raise_invalid_hdf5()

    address_space_size = stored_eof - base_address
    if (
        not _hdf5_address_is_defined(base_address, offset_width)
        or not _hdf5_address_is_defined(stored_eof, offset_width)
        or not _hdf5_address_is_defined(root_address, offset_width)
        or base_address != signature_offset
        or base_address >= stored_eof
        or stored_eof > file_size
        or root_address == 0
        or root_address >= address_space_size
        or signature_offset + root_address >= file_size
    ):
        _raise_invalid_hdf5()
    if version < 2 and any(
        _hdf5_address_is_defined(address, offset_width)
        for address in optional_addresses
    ):
        _raise_invalid_hdf5()
    if any(
        not _hdf5_address_is_defined(address, offset_width)
        or address == 0
        or address >= address_space_size
        or signature_offset + address >= file_size
        for address in root_cache_addresses
    ):
        _raise_invalid_hdf5()
    for address in optional_addresses:
        if _hdf5_address_is_defined(address, offset_width) and address >= stored_eof:
            _raise_invalid_hdf5()
    if version >= 2 and _hdf5_address_is_defined(extension_address, offset_width):
        # A superblock extension is itself a reachable object header. Until
        # its extension-message graph is traversed, accepting it would leave
        # metadata allocations outside the overlap ledger.
        _raise_invalid_hdf5()
    range_ledger = _Hdf5RangeLedger()
    range_ledger.add_metadata(signature_offset, signature_offset + superblock_size)
    _validate_hdf5_root_object(
        path,
        signature_offset=signature_offset,
        root_address=root_address,
        stored_eof=stored_eof,
        offset_width=offset_width,
        length_width=length_width,
        address_space_size=address_space_size,
        root_cache_addresses=root_cache_addresses,
        group_leaf_node_k=group_leaf_node_k,
        group_internal_node_k=group_internal_node_k,
        range_ledger=range_ledger,
    )
    range_ledger.validate_non_overlapping()


class _ParquetStructureError(ValueError):
    pass


class _ParquetCompactReader:
    """Bounded, seek-based reader for one Compact Thrift metadata region."""

    def __init__(self, stream: BinaryIO, *, start: int, end: int) -> None:
        if start < 0 or end < start:
            raise _ParquetStructureError
        self._stream = stream
        self._stream.seek(start)
        self.remaining = end - start
        self._values = 0

    @property
    def position(self) -> int:
        return self._stream.tell()

    def charge(self) -> None:
        self._values += 1
        if self._values > _MAX_PARQUET_THRIFT_VALUES:
            raise _ParquetStructureError

    def read_byte(self) -> int:
        self.charge()
        if self.remaining < 1:
            raise _ParquetStructureError
        value = self._stream.read(1)
        if len(value) != 1:
            raise _ParquetStructureError
        self.remaining -= 1
        return value[0]

    def read_exact(self, length: int) -> bytes:
        self.charge()
        if length < 0 or length > self.remaining:
            raise _ParquetStructureError
        value = self._stream.read(length)
        if len(value) != length:
            raise _ParquetStructureError
        self.remaining -= length
        return value

    def skip_exact(self, length: int) -> None:
        self.charge()
        if length < 0 or length > self.remaining:
            raise _ParquetStructureError
        self._stream.seek(length, 1)
        self.remaining -= length

    def read_crc32(self, length: int) -> int:
        self.charge()
        if length < 0 or length > self.remaining:
            raise _ParquetStructureError
        checksum = 0
        unread = length
        while unread:
            block = self._stream.read(min(unread, 64 * 1024))
            if not block:
                raise _ParquetStructureError
            checksum = zlib.crc32(block, checksum)
            unread -= len(block)
            self.remaining -= len(block)
        return checksum

    def read_unsigned_varint(self, bits: int) -> int:
        value = 0
        max_bytes = (bits + 6) // 7
        for index in range(max_bytes):
            byte = self.read_byte()
            value |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                if value >= 1 << bits:
                    raise _ParquetStructureError
                return value
        raise _ParquetStructureError

    def read_zigzag(self, bits: int) -> int:
        encoded = self.read_unsigned_varint(bits)
        return (encoded >> 1) ^ -(encoded & 1)

    def read_binary(self, *, max_bytes: int) -> bytes:
        length = self.read_unsigned_varint(32)
        if length > max_bytes:
            raise _ParquetStructureError
        return self.read_exact(length)

    def read_collection_header(self) -> tuple[int, int]:
        header = self.read_byte()
        size = header >> 4
        value_type = header & 0x0F
        if size == 15:
            size = self.read_unsigned_varint(32)
        if (
            size > _MAX_PARQUET_COLLECTION_ITEMS
            or value_type not in _THRIFT_VALUE_TYPES
        ):
            raise _ParquetStructureError
        return size, value_type


def _read_thrift_field_header(
    reader: _ParquetCompactReader, previous_field_id: int
) -> tuple[int, int] | None:
    header = reader.read_byte()
    field_type = header & 0x0F
    if field_type == _THRIFT_STOP:
        return None
    if field_type not in _THRIFT_VALUE_TYPES:
        raise _ParquetStructureError
    delta = header >> 4
    field_id = previous_field_id + delta if delta else reader.read_zigzag(16)
    if field_id <= 0 or field_id > 32767:
        raise _ParquetStructureError
    return field_id, field_type


def _skip_thrift_struct(reader: _ParquetCompactReader, *, depth: int) -> None:
    if depth > _MAX_PARQUET_THRIFT_DEPTH:
        raise _ParquetStructureError
    previous_field_id = 0
    seen: set[int] = set()
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen:
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        _skip_thrift_value(reader, field_type, depth=depth + 1)


def _skip_thrift_value(
    reader: _ParquetCompactReader,
    value_type: int,
    *,
    depth: int,
    collection_value: bool = False,
) -> None:
    if depth > _MAX_PARQUET_THRIFT_DEPTH:
        raise _ParquetStructureError
    reader.charge()
    if value_type in {_THRIFT_BOOLEAN_TRUE, _THRIFT_BOOLEAN_FALSE}:
        if collection_value and reader.read_byte() not in {
            _THRIFT_BOOLEAN_TRUE,
            _THRIFT_BOOLEAN_FALSE,
        }:
            raise _ParquetStructureError
    elif value_type == _THRIFT_I8:
        reader.skip_exact(1)
    elif value_type == _THRIFT_I16:
        reader.read_zigzag(16)
    elif value_type == _THRIFT_I32:
        reader.read_zigzag(32)
    elif value_type == _THRIFT_I64:
        reader.read_zigzag(64)
    elif value_type == _THRIFT_DOUBLE:
        reader.skip_exact(8)
    elif value_type == _THRIFT_BINARY:
        reader.skip_exact(reader.read_unsigned_varint(32))
    elif value_type in {_THRIFT_LIST, _THRIFT_SET}:
        size, item_type = reader.read_collection_header()
        for _ in range(size):
            _skip_thrift_value(
                reader,
                item_type,
                depth=depth + 1,
                collection_value=True,
            )
    elif value_type == _THRIFT_MAP:
        size = reader.read_unsigned_varint(32)
        if size > _MAX_PARQUET_COLLECTION_ITEMS:
            raise _ParquetStructureError
        if size:
            types = reader.read_byte()
            key_type = types >> 4
            item_type = types & 0x0F
            if (
                key_type not in _THRIFT_VALUE_TYPES
                or item_type not in _THRIFT_VALUE_TYPES
            ):
                raise _ParquetStructureError
            for _ in range(size):
                _skip_thrift_value(
                    reader,
                    key_type,
                    depth=depth + 1,
                    collection_value=True,
                )
                _skip_thrift_value(
                    reader,
                    item_type,
                    depth=depth + 1,
                    collection_value=True,
                )
    elif value_type == _THRIFT_STRUCT:
        _skip_thrift_struct(reader, depth=depth + 1)
    elif value_type == _THRIFT_UUID:
        reader.skip_exact(16)
    else:
        raise _ParquetStructureError


def _read_parquet_utf8(reader: _ParquetCompactReader) -> str:
    raw = reader.read_binary(max_bytes=_MAX_PARQUET_SCHEMA_NAME_BYTES)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ParquetStructureError from exc


def _parse_parquet_schema_element(
    reader: _ParquetCompactReader,
) -> tuple[str, int | None, int | None, int | None]:
    previous_field_id = 0
    seen: set[int] = set()
    physical_type: int | None = None
    type_length: int | None = None
    repetition_type: int | None = None
    name: str | None = None
    child_count: int | None = None
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_I32,
        3: _THRIFT_I32,
        4: _THRIFT_BINARY,
        5: _THRIFT_I32,
        6: _THRIFT_I32,
        7: _THRIFT_I32,
        8: _THRIFT_I32,
        9: _THRIFT_I32,
        10: _THRIFT_STRUCT,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            physical_type = reader.read_zigzag(32)
            if physical_type not in range(8):
                raise _ParquetStructureError
        elif field_id == 2:
            type_length = reader.read_zigzag(32)
        elif field_id == 3:
            repetition_type = reader.read_zigzag(32)
            if repetition_type not in {0, 1, 2}:
                raise _ParquetStructureError
        elif field_id == 4:
            name = _read_parquet_utf8(reader)
            if not name:
                raise _ParquetStructureError
        elif field_id == 5:
            child_count = reader.read_zigzag(32)
            if not 0 <= child_count <= _MAX_PARQUET_COLLECTION_ITEMS:
                raise _ParquetStructureError
        else:
            _skip_thrift_value(reader, field_type, depth=1)
    if name is None or (
        physical_type == 7 and (type_length is None or type_length <= 0)
    ):
        raise _ParquetStructureError
    return name, physical_type, repetition_type, child_count


@dataclass(frozen=True, slots=True)
class _ParquetLeaf:
    path: tuple[str, ...]
    physical_type: int


@dataclass(frozen=True, slots=True)
class _ParquetChunk:
    leaf: _ParquetLeaf
    start: int
    end: int
    compressed_size: int
    uncompressed_size: int
    num_values: int
    data_page_offset: int
    dictionary_page_offset: int | None
    index_page_offset: int | None
    encodings: frozenset[int]
    codec: int

    @property
    def is_empty(self) -> bool:
        return (
            self.num_values == 0
            and self.compressed_size == 0
            and self.uncompressed_size == 0
            and self.start == 0
            and self.end == 0
            and self.data_page_offset == 0
            and self.dictionary_page_offset in {None, 0}
            and self.index_page_offset in {None, 0}
        )


@dataclass(frozen=True, slots=True)
class _ParquetPageDetails:
    num_values: int | None = None
    definition_level_bytes: int = 0
    repetition_level_bytes: int = 0
    data_encoding: int | None = None
    definition_encoding: int | None = None
    repetition_encoding: int | None = None
    dictionary_encoding: int | None = None


@dataclass(frozen=True, slots=True)
class _ParquetPage:
    page_type: int
    compressed_size: int
    uncompressed_size: int
    crc: int | None
    num_values: int | None
    definition_level_bytes: int
    repetition_level_bytes: int
    data_encoding: int | None
    definition_encoding: int | None
    repetition_encoding: int | None
    dictionary_encoding: int | None


def _parse_parquet_data_page_header(
    reader: _ParquetCompactReader,
) -> tuple[int, int, int, int]:
    previous_field_id = 0
    seen: set[int] = set()
    required: dict[int, int] = {}
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_I32,
        3: _THRIFT_I32,
        4: _THRIFT_I32,
        5: _THRIFT_STRUCT,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id in {1, 2, 3, 4}:
            value = reader.read_zigzag(32)
            if value < 0 or (field_id != 1 and value not in _PARQUET_ENCODINGS):
                raise _ParquetStructureError
            required[field_id] = value
        else:
            _skip_thrift_value(reader, field_type, depth=1)
    if required.keys() != {1, 2, 3, 4}:
        raise _ParquetStructureError
    if required[3] not in {3, 4} or required[4] not in {3, 4}:
        raise _ParquetStructureError
    return required[1], required[2], required[3], required[4]


def _parse_parquet_dictionary_page_header(
    reader: _ParquetCompactReader,
) -> tuple[int, int]:
    previous_field_id = 0
    seen: set[int] = set()
    required: dict[int, int] = {}
    expected_types = {1: _THRIFT_I32, 2: _THRIFT_I32}
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        is_boolean = field_type in {_THRIFT_BOOLEAN_TRUE, _THRIFT_BOOLEAN_FALSE}
        if (
            field_id in seen
            or (field_id in expected_types and field_type != expected_types[field_id])
            or (field_id == 3 and not is_boolean)
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id in {1, 2}:
            value = reader.read_zigzag(32)
            if value < 0 or (field_id == 2 and value not in _PARQUET_ENCODINGS):
                raise _ParquetStructureError
            required[field_id] = value
        elif field_id != 3:
            _skip_thrift_value(reader, field_type, depth=1)
    if required.keys() != {1, 2}:
        raise _ParquetStructureError
    if required[2] != 0:
        raise _ParquetStructureError
    return required[1], required[2]


def _parse_parquet_data_page_v2_header(
    reader: _ParquetCompactReader,
) -> tuple[int, int, int, int]:
    previous_field_id = 0
    seen: set[int] = set()
    required: dict[int, int] = {}
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_I32,
        3: _THRIFT_I32,
        4: _THRIFT_I32,
        5: _THRIFT_I32,
        6: _THRIFT_I32,
        8: _THRIFT_STRUCT,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        is_boolean = field_type in {_THRIFT_BOOLEAN_TRUE, _THRIFT_BOOLEAN_FALSE}
        if (
            field_id in seen
            or (field_id in expected_types and field_type != expected_types[field_id])
            or (field_id == 7 and not is_boolean)
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id in {1, 2, 3, 4, 5, 6}:
            value = reader.read_zigzag(32)
            if value < 0 or (field_id == 4 and value not in _PARQUET_ENCODINGS):
                raise _ParquetStructureError
            required[field_id] = value
        elif field_id != 7:
            _skip_thrift_value(reader, field_type, depth=1)
    if required.keys() != {1, 2, 3, 4, 5, 6}:
        raise _ParquetStructureError
    if required[2] > required[1] or required[3] > required[1]:
        raise _ParquetStructureError
    return required[1], required[5], required[6], required[4]


def _parse_parquet_index_page_header(reader: _ParquetCompactReader) -> None:
    if _read_thrift_field_header(reader, 0) is not None:
        raise _ParquetStructureError


def _parse_parquet_page_header(reader: _ParquetCompactReader) -> _ParquetPage:
    header_start = reader.position
    previous_field_id = 0
    seen: set[int] = set()
    page_type: int | None = None
    uncompressed_size: int | None = None
    compressed_size: int | None = None
    crc: int | None = None
    subheaders: dict[int, _ParquetPageDetails] = {}
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_I32,
        3: _THRIFT_I32,
        4: _THRIFT_I32,
        5: _THRIFT_STRUCT,
        6: _THRIFT_STRUCT,
        7: _THRIFT_STRUCT,
        8: _THRIFT_STRUCT,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            page_type = reader.read_zigzag(32)
        elif field_id == 2:
            uncompressed_size = reader.read_zigzag(32)
        elif field_id == 3:
            compressed_size = reader.read_zigzag(32)
        elif field_id == 4:
            crc = reader.read_zigzag(32)
        elif field_id == 5:
            values, data_encoding, definition_encoding, repetition_encoding = (
                _parse_parquet_data_page_header(reader)
            )
            subheaders[field_id] = _ParquetPageDetails(
                num_values=values,
                data_encoding=data_encoding,
                definition_encoding=definition_encoding,
                repetition_encoding=repetition_encoding,
            )
        elif field_id == 6:
            _parse_parquet_index_page_header(reader)
            subheaders[field_id] = _ParquetPageDetails()
        elif field_id == 7:
            dictionary_values, dictionary_encoding = (
                _parse_parquet_dictionary_page_header(reader)
            )
            subheaders[field_id] = _ParquetPageDetails(
                num_values=dictionary_values,
                dictionary_encoding=dictionary_encoding,
            )
        elif field_id == 8:
            values, definition_bytes, repetition_bytes, data_encoding = (
                _parse_parquet_data_page_v2_header(reader)
            )
            subheaders[field_id] = _ParquetPageDetails(
                num_values=values,
                definition_level_bytes=definition_bytes,
                repetition_level_bytes=repetition_bytes,
                data_encoding=data_encoding,
            )
        else:
            _skip_thrift_value(reader, field_type, depth=1)

    expected_subheader = {0: 5, 1: 6, 2: 7, 3: 8}.get(page_type)
    if (
        expected_subheader is None
        or uncompressed_size is None
        or compressed_size is None
        or uncompressed_size < 0
        or compressed_size < 0
        or subheaders.keys() != {expected_subheader}
        or reader.position - header_start > _MAX_PARQUET_PAGE_HEADER_BYTES
    ):
        raise _ParquetStructureError
    details = subheaders[expected_subheader]
    if (
        details.definition_level_bytes + details.repetition_level_bytes
        > compressed_size
        or details.definition_level_bytes + details.repetition_level_bytes
        > uncompressed_size
    ):
        raise _ParquetStructureError
    return _ParquetPage(
        page_type=page_type,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        crc=crc,
        num_values=details.num_values,
        definition_level_bytes=details.definition_level_bytes,
        repetition_level_bytes=details.repetition_level_bytes,
        data_encoding=details.data_encoding,
        definition_encoding=details.definition_encoding,
        repetition_encoding=details.repetition_encoding,
        dictionary_encoding=details.dictionary_encoding,
    )


def _validate_parquet_chunk_pages(stream: BinaryIO, chunk: _ParquetChunk) -> None:
    if chunk.is_empty:
        return
    reader = _ParquetCompactReader(stream, start=chunk.start, end=chunk.end)
    page_count = 0
    data_page_values = 0
    total_uncompressed_size = 0
    first_data_page_offset: int | None = None
    dictionary_page_offset: int | None = None
    dictionary_page_values: int | None = None
    index_page_offset: int | None = None
    while reader.remaining:
        page_offset = reader.position
        page = _parse_parquet_page_header(reader)
        header_size = reader.position - page_offset
        page_count += 1
        if page_count > _MAX_PARQUET_PAGES_PER_CHUNK:
            raise _ParquetStructureError
        page_encodings = {
            encoding
            for encoding in (
                page.data_encoding,
                page.definition_encoding,
                page.repetition_encoding,
                page.dictionary_encoding,
            )
            if encoding is not None
        }
        if not page_encodings <= chunk.encodings:
            raise _ParquetStructureError

        if page.page_type == 2:
            if (
                dictionary_page_offset is not None
                or first_data_page_offset is not None
                or page.num_values is None
                or page.num_values > chunk.num_values
            ):
                raise _ParquetStructureError
            dictionary_page_offset = page_offset
            dictionary_page_values = page.num_values
        elif page.page_type in {0, 3}:
            if first_data_page_offset is None:
                first_data_page_offset = page_offset
            if page.num_values is None:
                raise _ParquetStructureError
            if page.data_encoding in {2, 8} and dictionary_page_offset is None:
                raise _ParquetStructureError
            data_page_values += page.num_values
            if data_page_values >= 1 << 63:
                raise _ParquetStructureError
        else:
            if index_page_offset is not None:
                raise _ParquetStructureError
            index_page_offset = page_offset

        total_uncompressed_size += header_size + page.uncompressed_size
        if total_uncompressed_size > chunk.uncompressed_size:
            raise _ParquetStructureError
        if page.crc is None:
            # Page headers and exact chunk arithmetic remain enforceable, but
            # a checksum-free compressed payload cannot be authenticated
            # without adding the corresponding codec implementation.
            reader.skip_exact(page.compressed_size)
        elif reader.read_crc32(page.compressed_size) != page.crc & 0xFFFFFFFF:
            raise _ParquetStructureError

    is_dictionary_only_empty = (
        chunk.num_values == 0
        and chunk.data_page_offset == 0
        and page_count == 1
        and first_data_page_offset is None
        and dictionary_page_offset is not None
        and dictionary_page_values == 0
        and index_page_offset is None
    )
    if (
        page_count == 0
        or data_page_values != chunk.num_values
        or total_uncompressed_size != chunk.uncompressed_size
        or (
            first_data_page_offset != chunk.data_page_offset
            and not is_dictionary_only_empty
        )
        or dictionary_page_offset != chunk.dictionary_page_offset
        or index_page_offset != chunk.index_page_offset
    ):
        raise _ParquetStructureError


def _parse_parquet_schema(
    reader: _ParquetCompactReader,
) -> tuple[_ParquetLeaf, ...]:
    size, item_type = reader.read_collection_header()
    if size == 0 or item_type != _THRIFT_STRUCT:
        raise _ParquetStructureError
    pending_nodes = 1
    leaves: list[_ParquetLeaf] = []
    groups: list[tuple[int, tuple[str, ...]]] = []
    for index in range(size):
        if pending_nodes <= 0:
            raise _ParquetStructureError
        name, physical_type, repetition_type, child_count = (
            _parse_parquet_schema_element(reader)
        )
        pending_nodes -= 1
        if index == 0:
            if (
                physical_type is not None
                or repetition_type not in {None, 0}
                or child_count is None
            ):
                raise _ParquetStructureError
            groups.append((child_count, ()))
        else:
            while groups and groups[-1][0] == 0:
                groups.pop()
            if not groups:
                raise _ParquetStructureError
            remaining_children, parent_path = groups[-1]
            groups[-1] = (remaining_children - 1, parent_path)
            path = (*parent_path, name)
            if repetition_type is None or (physical_type is None) == (
                child_count is None
            ):
                raise _ParquetStructureError
            if physical_type is None and child_count == 0:
                raise _ParquetStructureError
        if child_count is not None:
            pending_nodes += child_count
            if index != 0:
                if len(groups) >= _MAX_PARQUET_THRIFT_DEPTH:
                    raise _ParquetStructureError
                groups.append((child_count, path))
        else:
            if physical_type is None:
                raise _ParquetStructureError
            leaves.append(_ParquetLeaf(path=path, physical_type=physical_type))
    if pending_nodes != 0:
        raise _ParquetStructureError
    return tuple(leaves)


def _parse_parquet_column_metadata(
    reader: _ParquetCompactReader, *, data_end: int
) -> _ParquetChunk:
    previous_field_id = 0
    seen: set[int] = set()
    physical_type: int | None = None
    encodings: set[int] = set()
    path: tuple[str, ...] | None = None
    codec: int | None = None
    num_values: int | None = None
    total_uncompressed_size: int | None = None
    total_compressed_size: int | None = None
    data_page_offset: int | None = None
    index_page_offset: int | None = None
    dictionary_page_offset: int | None = None
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_LIST,
        3: _THRIFT_LIST,
        4: _THRIFT_I32,
        5: _THRIFT_I64,
        6: _THRIFT_I64,
        7: _THRIFT_I64,
        8: _THRIFT_LIST,
        9: _THRIFT_I64,
        10: _THRIFT_I64,
        11: _THRIFT_I64,
        12: _THRIFT_STRUCT,
        13: _THRIFT_LIST,
        14: _THRIFT_I64,
        15: _THRIFT_I32,
        16: _THRIFT_STRUCT,
        17: _THRIFT_STRUCT,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            physical_type = reader.read_zigzag(32)
            if physical_type not in range(8):
                raise _ParquetStructureError
        elif field_id == 2:
            size, item_type = reader.read_collection_header()
            if size == 0 or item_type != _THRIFT_I32:
                raise _ParquetStructureError
            for _ in range(size):
                encoding = reader.read_zigzag(32)
                if encoding not in _PARQUET_ENCODINGS:
                    raise _ParquetStructureError
                encodings.add(encoding)
        elif field_id == 3:
            size, item_type = reader.read_collection_header()
            if size == 0 or item_type != _THRIFT_BINARY:
                raise _ParquetStructureError
            path_parts = []
            for _ in range(size):
                part = _read_parquet_utf8(reader)
                if not part:
                    raise _ParquetStructureError
                path_parts.append(part)
            path = tuple(path_parts)
        elif field_id == 4:
            codec = reader.read_zigzag(32)
            if codec not in _PARQUET_CODECS:
                raise _ParquetStructureError
        elif field_id == 5:
            num_values = reader.read_zigzag(64)
            if num_values < 0:
                raise _ParquetStructureError
        elif field_id == 6:
            total_uncompressed_size = reader.read_zigzag(64)
            if total_uncompressed_size < 0:
                raise _ParquetStructureError
        elif field_id == 7:
            total_compressed_size = reader.read_zigzag(64)
            if total_compressed_size < 0:
                raise _ParquetStructureError
        elif field_id == 9:
            data_page_offset = reader.read_zigzag(64)
        elif field_id == 10:
            index_page_offset = reader.read_zigzag(64)
        elif field_id == 11:
            dictionary_page_offset = reader.read_zigzag(64)
        elif field_id in {14, 15}:
            # Bloom filter ranges are not admitted until their header and
            # paired range semantics are validated by this core detector.
            raise _ParquetStructureError
        else:
            _skip_thrift_value(reader, field_type, depth=1)

    if (
        physical_type is None
        or not encodings
        or path is None
        or codec is None
        or num_values is None
        or total_uncompressed_size is None
        or total_compressed_size is None
        or data_page_offset is None
    ):
        raise _ParquetStructureError
    is_empty = (
        num_values == 0
        and total_uncompressed_size == 0
        and total_compressed_size == 0
        and data_page_offset == 0
        and dictionary_page_offset in {None, 0}
        and index_page_offset in {None, 0}
    )
    if is_empty:
        return _ParquetChunk(
            leaf=_ParquetLeaf(path=path, physical_type=physical_type),
            start=0,
            end=0,
            compressed_size=0,
            uncompressed_size=0,
            num_values=0,
            data_page_offset=0,
            dictionary_page_offset=dictionary_page_offset,
            index_page_offset=index_page_offset,
            encodings=frozenset(encodings),
            codec=codec,
        )
    is_dictionary_only_empty = (
        num_values == 0
        and data_page_offset == 0
        and dictionary_page_offset is not None
        and dictionary_page_offset >= 4
        and index_page_offset in {None, 0}
        and total_compressed_size > 0
    )
    if (data_page_offset < 4 and not is_dictionary_only_empty) or any(
        offset is not None and offset < 4
        for offset in (dictionary_page_offset, index_page_offset)
    ):
        raise _ParquetStructureError
    first_page_offset = dictionary_page_offset or data_page_offset
    chunk_end = first_page_offset + total_compressed_size
    offsets = tuple(
        offset
        for offset in (data_page_offset, dictionary_page_offset, index_page_offset)
        if offset not in {None, 0}
    )
    if (
        first_page_offset < 4
        or first_page_offset >= data_end
        or chunk_end > data_end
        or (
            dictionary_page_offset is not None
            and data_page_offset != 0
            and dictionary_page_offset > data_page_offset
        )
        or any(not first_page_offset <= offset < chunk_end for offset in offsets)
    ):
        raise _ParquetStructureError
    return _ParquetChunk(
        leaf=_ParquetLeaf(path=path, physical_type=physical_type),
        start=first_page_offset,
        end=chunk_end,
        compressed_size=total_compressed_size,
        uncompressed_size=total_uncompressed_size,
        num_values=num_values,
        data_page_offset=data_page_offset,
        dictionary_page_offset=dictionary_page_offset,
        index_page_offset=index_page_offset,
        encodings=frozenset(encodings),
        codec=codec,
    )


def _parse_parquet_column_chunk(
    reader: _ParquetCompactReader, *, data_end: int
) -> _ParquetChunk:
    previous_field_id = 0
    seen: set[int] = set()
    file_path: str | None = None
    file_offset: int | None = None
    column_metadata: _ParquetChunk | None = None
    index_ranges: dict[str, int] = {}
    expected_types = {
        1: _THRIFT_BINARY,
        2: _THRIFT_I64,
        3: _THRIFT_STRUCT,
        4: _THRIFT_I64,
        5: _THRIFT_I32,
        6: _THRIFT_I64,
        7: _THRIFT_I32,
        8: _THRIFT_STRUCT,
        9: _THRIFT_BINARY,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            file_path = _read_parquet_utf8(reader)
        elif field_id == 2:
            file_offset = reader.read_zigzag(64)
        elif field_id == 3:
            column_metadata = _parse_parquet_column_metadata(reader, data_end=data_end)
        elif field_id == 4:
            index_ranges["offset_index_offset"] = reader.read_zigzag(64)
        elif field_id == 5:
            index_ranges["offset_index_length"] = reader.read_zigzag(32)
        elif field_id == 6:
            index_ranges["column_index_offset"] = reader.read_zigzag(64)
        elif field_id == 7:
            index_ranges["column_index_length"] = reader.read_zigzag(32)
        elif field_id in {8, 9}:
            # Only plaintext, single-blob Parquet belongs to this core
            # admission profile. Other variants require a separately reviewed
            # provider and storage contract.
            raise _ParquetStructureError
        else:
            _skip_thrift_value(reader, field_type, depth=1)
    if file_path or file_offset != 0 or column_metadata is None:
        raise _ParquetStructureError
    for prefix in ("offset_index", "column_index"):
        offset = index_ranges.get(f"{prefix}_offset")
        length = index_ranges.get(f"{prefix}_length")
        if (offset is None) != (length is None):
            raise _ParquetStructureError
        if offset is not None and (
            offset < 4 or length is None or length <= 0 or offset + length > data_end
        ):
            raise _ParquetStructureError
    return column_metadata


def _parse_parquet_row_group(
    reader: _ParquetCompactReader, *, data_end: int
) -> tuple[int, tuple[_ParquetChunk, ...]]:
    previous_field_id = 0
    seen: set[int] = set()
    column_count: int | None = None
    column_chunks: list[_ParquetChunk] = []
    total_byte_size: int | None = None
    row_count: int | None = None
    file_offset: int | None = None
    total_compressed_size: int | None = None
    expected_types = {
        1: _THRIFT_LIST,
        2: _THRIFT_I64,
        3: _THRIFT_I64,
        4: _THRIFT_LIST,
        5: _THRIFT_I64,
        6: _THRIFT_I64,
        7: _THRIFT_I16,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            column_count, item_type = reader.read_collection_header()
            if item_type != _THRIFT_STRUCT:
                raise _ParquetStructureError
            for _ in range(column_count):
                column_chunks.append(
                    _parse_parquet_column_chunk(reader, data_end=data_end)
                )
        elif field_id == 2:
            total_byte_size = reader.read_zigzag(64)
            if total_byte_size < 0:
                raise _ParquetStructureError
        elif field_id == 3:
            row_count = reader.read_zigzag(64)
            if row_count < 0:
                raise _ParquetStructureError
        elif field_id == 5:
            file_offset = reader.read_zigzag(64)
        elif field_id == 6:
            total_compressed_size = reader.read_zigzag(64)
        else:
            _skip_thrift_value(reader, field_type, depth=1)
    if column_count is None or total_byte_size is None or row_count is None:
        raise _ParquetStructureError
    first_page_offset = min((chunk.start for chunk in column_chunks), default=None)
    compressed_size = sum(chunk.compressed_size for chunk in column_chunks)
    uncompressed_size = sum(chunk.uncompressed_size for chunk in column_chunks)
    is_empty = (
        row_count == 0
        and total_byte_size == 0
        and compressed_size == 0
        and file_offset in {None, 0}
        and total_compressed_size in {None, 0}
        and all(chunk.is_empty for chunk in column_chunks)
    )
    if is_empty:
        return row_count, tuple(column_chunks)
    if (
        total_byte_size != uncompressed_size
        or any(chunk.is_empty for chunk in column_chunks)
        or (file_offset is not None and file_offset != first_page_offset)
        or (
            total_compressed_size is not None
            and total_compressed_size != compressed_size
        )
    ):
        raise _ParquetStructureError
    return row_count, tuple(column_chunks)


def _parse_parquet_row_groups(
    reader: _ParquetCompactReader, *, data_end: int
) -> tuple[int, tuple[tuple[_ParquetChunk, ...], ...]]:
    size, item_type = reader.read_collection_header()
    if item_type != _THRIFT_STRUCT:
        raise _ParquetStructureError
    total_rows = 0
    row_group_chunks = []
    for _ in range(size):
        row_count, chunks = _parse_parquet_row_group(reader, data_end=data_end)
        total_rows += row_count
        if total_rows >= 1 << 63:
            raise _ParquetStructureError
        row_group_chunks.append(chunks)
    return total_rows, tuple(row_group_chunks)


def _parse_parquet_file_metadata(
    reader: _ParquetCompactReader, *, data_end: int
) -> tuple[_ParquetChunk, ...]:
    previous_field_id = 0
    seen: set[int] = set()
    version: int | None = None
    schema_leaves: tuple[_ParquetLeaf, ...] | None = None
    row_count: int | None = None
    row_group_rows: int | None = None
    row_group_chunks: tuple[tuple[_ParquetChunk, ...], ...] = ()
    expected_types = {
        1: _THRIFT_I32,
        2: _THRIFT_LIST,
        3: _THRIFT_I64,
        4: _THRIFT_LIST,
        5: _THRIFT_LIST,
        6: _THRIFT_BINARY,
        7: _THRIFT_LIST,
        8: _THRIFT_STRUCT,
        9: _THRIFT_BINARY,
    }
    while (header := _read_thrift_field_header(reader, previous_field_id)) is not None:
        field_id, field_type = header
        if field_id in seen or (
            field_id in expected_types and field_type != expected_types[field_id]
        ):
            raise _ParquetStructureError
        seen.add(field_id)
        previous_field_id = field_id
        if field_id == 1:
            version = reader.read_zigzag(32)
            if version not in {1, 2}:
                raise _ParquetStructureError
        elif field_id == 2:
            schema_leaves = _parse_parquet_schema(reader)
        elif field_id == 3:
            row_count = reader.read_zigzag(64)
            if row_count < 0:
                raise _ParquetStructureError
        elif field_id == 4:
            row_group_rows, row_group_chunks = _parse_parquet_row_groups(
                reader, data_end=data_end
            )
        elif field_id == 6:
            _read_parquet_utf8(reader)
        elif field_id in {8, 9}:
            raise _ParquetStructureError
        else:
            _skip_thrift_value(reader, field_type, depth=1)
    if (
        version is None
        or schema_leaves is None
        or row_count is None
        or row_group_rows is None
        or row_group_rows != row_count
        or any(
            tuple(chunk.leaf for chunk in chunks) != schema_leaves
            for chunks in row_group_chunks
        )
        or reader.remaining != 0
    ):
        raise _ParquetStructureError
    ordered_chunks = sorted(
        (chunk for chunks in row_group_chunks for chunk in chunks),
        key=lambda chunk: chunk.start,
    )
    if any(
        previous.end > current.start
        for previous, current in zip(ordered_chunks, ordered_chunks[1:], strict=False)
    ):
        raise _ParquetStructureError
    return tuple(ordered_chunks)


def _validate_parquet(path: Path, tail: bytes) -> None:
    file_size = path.stat().st_size
    if file_size < 12 or not tail.endswith(b"PAR1"):
        raise LibraryFormatError(
            "library_container_invalid", "The Parquet footer is missing."
        )
    footer_size = int.from_bytes(tail[-8:-4], "little")
    if footer_size <= 0 or footer_size > file_size - 12:
        raise LibraryFormatError(
            "library_container_invalid", "The Parquet footer length is invalid."
        )
    if footer_size > _MAX_PARQUET_METADATA_BYTES:
        raise LibraryFormatError(
            "library_container_limit", "The Parquet metadata exceeds the safety limit."
        )

    metadata_start = file_size - 8 - footer_size
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"PAR1":
                raise _ParquetStructureError
            reader = _ParquetCompactReader(
                stream, start=metadata_start, end=file_size - 8
            )
            chunks = _parse_parquet_file_metadata(reader, data_end=metadata_start)
            for chunk in chunks:
                _validate_parquet_chunk_pages(stream, chunk)
    except _ParquetStructureError as exc:
        raise LibraryFormatError(
            "library_container_invalid", "The Parquet container is invalid."
        ) from exc


def _validate_xml_member(
    *,
    archive_path: Path,
    name: str,
    root: str | None,
    package_budget: _XmlPackageBudget | None = None,
) -> ElementTree.Element:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            info = archive.getinfo(name)
            if info.file_size > _MAX_XML_BYTES:
                raise LibraryFormatError(
                    "library_container_limit",
                    "A document XML component exceeds the safety limit.",
                )
            with archive.open(info) as stream:
                content = stream.read(_MAX_XML_BYTES + 1)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise LibraryFormatError(
            "library_container_invalid", "The document XML is unavailable."
        ) from exc
    parsed = _parse_bounded_xml(
        content,
        limit_message="A document XML component exceeds the safety limit.",
        invalid_message="The document XML is invalid.",
        unsafe_message="The document XML is unsafe.",
        package_budget=package_budget,
    )
    if root is not None and parsed.tag.rsplit("}", 1)[-1] != root:
        raise LibraryFormatError(
            "library_container_invalid", "The document XML root is invalid."
        )
    return parsed


__all__ = [
    "DEFAULT_FORMATS",
    "DetectionResult",
    "DetectorMatch",
    "FormatDetector",
    "FormatCapability",
    "FormatDescriptor",
    "FormatRegistry",
    "LibraryFormatError",
    "LIBRARY_FORMAT_POLICY_ALIASES",
    "LIBRARY_SUPPORTED_POLICY_FORMAT_IDS",
    "MAX_SOURCE_PREVIEW_IMAGE_DIMENSION",
    "MAX_SOURCE_PREVIEW_IMAGE_PIXELS",
    "SOURCE_PREVIEW_IMAGE_PROFILES",
    "SOURCE_PREVIEW_MEDIA_PROFILES",
    "normalize_library_filename",
]
