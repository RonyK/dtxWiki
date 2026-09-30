"""Optional pypdf provider, executed only by the converter subprocess host."""

from __future__ import annotations

from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any

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
    SourceLocation,
)


class PdfTextConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.pypdf",
            converter_version=_package_version("pypdf"),
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"pdf"}),
            media_types=frozenset({"application/pdf"}),
            priority=100,
            execution_mode="subprocess",
            network_required=False,
            deterministic=True,
            license_expression="BSD-3-Clause",
            source_url="https://github.com/py-pdf/pypdf",
            capabilities=frozenset({"markdown", "locations"}),
            distribution_digest="installed-environment-sbom",
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id == "pdf")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ImportError as exc:
            raise ConversionError(
                "converter_dependency_missing", "The PDF converter is not installed."
            ) from exc

        blocks: list[str] = []
        locations: list[SourceLocation] = []
        text_truncated = False
        try:
            with request.source.open_binary() as stream:
                _validate_pdf_envelope(stream, request.byte_size)
                reader = PdfReader(stream, strict=True)
                if reader.is_encrypted:
                    raise ConversionError(
                        "encrypted_document",
                        "Password-protected PDF conversion is not supported.",
                        fallback_allowed=False,
                    )
                object_count = _pdf_object_count(reader)
                if object_count > request.limits.max_pdf_objects:
                    raise ConversionError(
                        "pdf_object_limit",
                        "The PDF contains too many indirect objects.",
                        fallback_allowed=False,
                    )
                page_count = len(reader.pages)
                rendered_pages = min(page_count, request.limits.max_pages)
                used_output_bytes = 0
                for index in range(rendered_pages):
                    page = reader.pages[index]
                    try:
                        text = page.extract_text(extraction_mode="layout") or ""
                    except TypeError:
                        text = page.extract_text() or ""
                    text, page_truncated = _utf8_prefix(
                        text.strip(), request.limits.max_string_bytes
                    )
                    text_truncated = text_truncated or page_truncated
                    block = f"## Page {index + 1}\n\n{text}"
                    block_bytes = len(block.encode("utf-8")) + 2
                    if (
                        used_output_bytes + block_bytes
                        > request.limits.max_output_bytes
                    ):
                        text_truncated = True
                        break
                    blocks.append(block)
                    used_output_bytes += block_bytes
                    locations.append(
                        SourceLocation(
                            kind="page", ordinal=index, label=f"Page {index + 1}"
                        )
                    )
                metadata = {
                    "page_count": page_count,
                    "pages_rendered": len(locations),
                    "pdf_object_count": object_count,
                    "encrypted": False,
                    "embedded_files_extracted": False,
                    "network_resources_fetched": False,
                }
        except ConversionError:
            raise
        except (OSError, PdfReadError, RecursionError, RuntimeError, ValueError) as exc:
            raise ConversionError(
                "invalid_pdf", "The PDF document is corrupt or unsupported."
            ) from exc

        markdown, output_truncated = _bounded_text(
            "\n\n".join(blocks).rstrip() + "\n",
            request.limits.max_output_bytes,
        )
        truncated = page_count > len(locations) or text_truncated or output_truncated
        warnings = (
            (
                ConversionWarning(
                    code="output_truncated",
                    message="The PDF conversion reached a configured limit.",
                ),
            )
            if truncated
            else ()
        )
        return ConversionResult(
            markdown=markdown,
            metadata=metadata,
            locations=tuple(locations),
            warnings=warnings,
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("0.800"),
        )


def _validate_pdf_envelope(stream: Any, byte_size: int) -> None:
    if byte_size < 8:
        raise ConversionError("invalid_pdf", "The PDF document is corrupt.")
    stream.seek(0)
    header = stream.read(min(byte_size, 1024))
    stream.seek(max(0, byte_size - 4096))
    trailer = stream.read(4096)
    stream.seek(0)
    if not header.lstrip().startswith(b"%PDF-") or b"%%EOF" not in trailer:
        raise ConversionError("invalid_pdf", "The PDF document is corrupt.")


def _pdf_object_count(reader: Any) -> int:
    xref = getattr(reader, "xref", None)
    if not isinstance(xref, dict):
        raise ConversionError(
            "converter_version_unsupported",
            "The installed PDF converter does not expose bounded object metadata.",
            fallback_allowed=False,
        )
    count = 0
    for generation in xref.values():
        if not isinstance(generation, dict):
            raise ConversionError(
                "invalid_pdf", "The PDF cross-reference table is invalid."
            )
        count += len(generation)
    xref_obj_stream = getattr(reader, "xref_objStm", {})
    if isinstance(xref_obj_stream, dict):
        count += len(xref_obj_stream)
    return count


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not-installed"


__all__ = ["PdfTextConverter"]
