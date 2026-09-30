"""Bounded deterministic Markdown and plain-text converters."""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from types import MappingProxyType

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
from backend.utils.text_security import library_text_encoding, looks_like_binary_text

_FENCED_SOURCE_LANGUAGES = MappingProxyType(
    {
        "text": "text",
        "html": "html",
        "javascript": "javascript",
    }
)
_SUPPORTED_FORMATS = frozenset({"markdown", *_FENCED_SOURCE_LANGUAGES})


def decode_library_text(content: bytes) -> tuple[str, str]:
    encoding = library_text_encoding(content[:4])
    try:
        decoded = content.decode(encoding)
    except UnicodeError as exc:
        raise ConversionError(
            "unsupported_encoding",
            "Text conversion supports UTF-8 and BOM-marked UTF-16.",
            retryable=False,
            fallback_allowed=False,
        ) from exc
    if looks_like_binary_text(decoded):
        raise ConversionError(
            "unsupported_encoding",
            "The source is binary-like, not supported text.",
            retryable=False,
            fallback_allowed=False,
        )
    return decoded, encoding


class BuiltinTextConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.builtin.text",
            converter_version="1.1.0",
            spi_version=CONVERTER_SPI_VERSION,
            formats=_SUPPORTED_FORMATS,
            media_types=frozenset(
                {
                    "application/ecmascript",
                    "application/javascript",
                    "application/xhtml+xml",
                    "text/ecmascript",
                    "text/html",
                    "text/javascript",
                    "text/markdown",
                    "text/plain",
                    "text/x-markdown",
                }
            ),
            priority=100,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="AGPL-3.0-only OR Elastic-2.0",
            source_url="https://github.com/dtxsuite/dtxwiki",
            capabilities=frozenset({"markdown", "locations"}),
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id in _SUPPORTED_FORMATS)

    def convert(self, request: ConversionRequest) -> ConversionResult:
        language = None
        if request.format_id != "markdown":
            language = _FENCED_SOURCE_LANGUAGES.get(request.format_id)
            if language is None:
                raise ConversionError(
                    "unsupported",
                    "The text converter does not support the requested format.",
                    retryable=False,
                    fallback_allowed=False,
                )
        with request.source.open_binary() as stream:
            content = stream.read(request.limits.max_input_bytes + 1)
        if len(content) > request.limits.max_input_bytes:
            raise ConversionError(
                "input_limit_exceeded",
                "The source exceeds the converter input limit.",
                retryable=False,
                fallback_allowed=False,
            )
        text, encoding = decode_library_text(content)
        canonical = (
            unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
        )
        if language is not None:
            canonical, truncated = _bounded_fenced_text(
                canonical,
                language=language,
                max_bytes=request.limits.max_output_bytes,
            )
        elif canonical and not canonical.endswith("\n"):
            canonical += "\n"
            canonical, truncated = _bounded_text(
                canonical, request.limits.max_output_bytes
            )
        else:
            canonical, truncated = _bounded_text(
                canonical, request.limits.max_output_bytes
            )
        warnings = (
            (
                ConversionWarning(
                    code="output_truncated",
                    message=(
                        "The Markdown output was truncated at the configured limit."
                    ),
                ),
            )
            if truncated
            else ()
        )
        return ConversionResult(
            markdown=canonical,
            metadata={"source_encoding": encoding},
            locations=(
                SourceLocation(
                    kind="line_range",
                    ordinal=0,
                    data={"line_count": len(text.splitlines())},
                ),
            ),
            warnings=warnings,
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("1.000"),
        )


def _longest_backtick_run(text: str) -> int:
    longest = current = 0
    for character in text:
        if character == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _bounded_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    marker = "\n\n_(output truncated)_\n"
    marker_bytes = marker.encode("utf-8")
    budget = max(0, max_bytes - len(marker_bytes))
    prefix = encoded[:budget]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker, True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker[:max_bytes], True


def _bounded_fenced_text(
    content: str, *, language: str, max_bytes: int
) -> tuple[str, bool]:
    """Render a bounded code block without ever emitting an open-only fence."""

    fence = "`" * max(3, _longest_backtick_run(content) + 1)
    opening = f"{fence}{language}\n"
    closing = f"\n{fence}\n"
    complete = opening + content + closing
    if len(complete.encode("utf-8")) <= max_bytes:
        return complete, False

    outside_marker = "_(output truncated)_\n"
    fixed = (opening + closing + outside_marker).encode("utf-8")
    if len(fixed) > max_bytes:
        return _utf8_prefix(outside_marker, max_bytes), True

    body_budget = max_bytes - len(fixed)
    body = _utf8_prefix(content, body_budget)
    return opening + body + closing + outside_marker, True


def _utf8_prefix(text: str, max_bytes: int) -> str:
    prefix = text.encode("utf-8")[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return ""


__all__ = ["BuiltinTextConverter", "decode_library_text"]
