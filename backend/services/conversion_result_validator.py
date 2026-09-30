"""Validate and sanitize untrusted converter output before persistence."""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from urllib.parse import unquote

from backend.spi.conversion import (
    ConversionError,
    ConversionLimits,
    ConversionResult,
    ConversionWarning,
    ProducedAsset,
    SourceLocation,
)
from backend.utils.markdown_fence import fence_opener, is_fence_closer

SANITIZER_VERSION = "dtxwiki-markdown-v3"
_ANGLE_OR_BR = re.compile(r"<br[ \t]*/?>|[<>]", re.IGNORECASE)
_INLINE_DESTINATION_START = re.compile(r"\]\(")
_REFERENCE_DESTINATION_START = re.compile(r"(?m)^[ \t]{0,3}\[[^\]\r\n]{1,256}\]:[ \t]*")
_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ALLOWED_ASSET_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "text/plain",
    }
)
_ASSET_ROLES = frozenset({"preview", "asset", "metadata", "location_map"})
_DANGEROUS_SCHEMES = frozenset({"data", "javascript", "vbscript"})
_SENSITIVE_LOCATION_KEYS = frozenset(
    {
        "absolute_path",
        "file_path",
        "filepath",
        "host_path",
        "source_path",
        "storage_key",
        "storage_ref",
    }
)
_COMPLETENESS_VALUES = frozenset({"complete", "metadata_only", "truncated"})
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 1_000_000
_MAX_ORDINAL = 2**31 - 1
_MAX_WARNINGS = 10_000
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


@dataclass(slots=True)
class _JsonBudget:
    max_nodes: int
    max_depth: int
    max_bytes: int
    max_scalar_bytes: int
    max_label_bytes: int
    nodes: int = 0
    encoded_bytes: int = 0

    def consume_node(self, encoded_bytes: int = 0) -> None:
        self.nodes += 1
        self.encoded_bytes += encoded_bytes
        if self.nodes > self.max_nodes or self.encoded_bytes > self.max_bytes:
            _invalid_json()


def validate_conversion_result(
    result: ConversionResult,
    *,
    limits: ConversionLimits,
) -> ConversionResult:
    """Return a bounded, sanitized, recursively immutable result snapshot.

    Converter code is outside the trust boundary.  Every runtime value is
    therefore checked even though the SPI uses frozen, typed dataclasses.  Any
    unexpected provider exception is translated to one stable, safe error.
    """

    try:
        return _validate_conversion_result(result, limits=limits)
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(
            "converter_result_invalid",
            "The converter returned an invalid result.",
            retryable=False,
            fallback_allowed=True,
        ) from exc


def _validate_conversion_result(
    result: ConversionResult,
    *,
    limits: ConversionLimits,
) -> ConversionResult:
    if type(result) is not ConversionResult or type(limits) is not ConversionLimits:
        _invalid_result()
    if type(result.assets) is not tuple:
        _invalid_result()
    if type(result.locations) is not tuple:
        _invalid_result()
    if type(result.warnings) is not tuple:
        _invalid_result()
    if (
        type(result.completeness) is not str
        or result.completeness not in _COMPLETENESS_VALUES
    ):
        _invalid_result()
    _validate_quality_score(result.quality_score)

    markdown = result.markdown
    if markdown is not None and type(markdown) is not str:
        _invalid_result()
    if markdown is not None:
        _bounded_utf8(markdown, maximum=limits.max_output_bytes)
        sanitized = _sanitize_markdown(markdown)
        # Escaping '<' and '>' expands the byte representation, so the bound
        # must be enforced on the persisted value as well as provider output.
        _bounded_utf8(sanitized, maximum=limits.max_output_bytes)
    else:
        sanitized = None

    budget = _JsonBudget(
        max_nodes=min(limits.max_nodes, _MAX_JSON_NODES),
        max_depth=min(limits.max_depth, _MAX_JSON_DEPTH),
        max_bytes=limits.max_output_bytes,
        max_scalar_bytes=min(limits.max_string_bytes, limits.max_output_bytes),
        max_label_bytes=min(
            limits.max_cell_bytes,
            limits.max_output_bytes,
            4 * 1024,
        ),
    )
    metadata = _snapshot_json_mapping(
        result.metadata,
        budget=budget,
        forbidden_keys=_SENSITIVE_LOCATION_KEYS,
    )
    assets = _validate_assets(result.assets, limits=limits, budget=budget)

    if len(result.locations) > limits.max_nodes:
        raise ConversionError(
            "converter_location_limit_exceeded",
            "The converter produced too many source locations.",
            retryable=False,
            fallback_allowed=True,
        )
    locations: list[SourceLocation] = []
    location_ordinals: set[tuple[str, int]] = set()
    for location in result.locations:
        validated = _validate_location(location, budget=budget)
        key = (validated.kind, validated.ordinal)
        if key in location_ordinals:
            raise ConversionError(
                "converter_location_ordinal_duplicate",
                "The converter returned duplicate source locations.",
                retryable=False,
                fallback_allowed=True,
            )
        location_ordinals.add(key)
        locations.append(validated)

    warning_limit = min(limits.max_nodes, _MAX_WARNINGS)
    if len(result.warnings) > warning_limit:
        raise ConversionError(
            "converter_warning_limit_exceeded",
            "The converter produced too many warnings.",
            retryable=False,
            fallback_allowed=True,
        )
    warnings = [
        _validate_warning(warning, budget=budget) for warning in result.warnings
    ]
    if sanitized != markdown:
        warnings.append(
            ConversionWarning(
                code="unsafe_markup_sanitized",
                message="Unsafe inline markup or a dangerous URL was neutralized.",
            )
        )
    if len(warnings) > warning_limit:
        raise ConversionError(
            "converter_warning_limit_exceeded",
            "The converter produced too many warnings.",
            retryable=False,
            fallback_allowed=True,
        )

    if sanitized is None and not assets and not metadata and not locations:
        raise ConversionError(
            "converter_result_empty",
            "The converter did not produce a persistent result.",
            retryable=False,
            fallback_allowed=True,
        )
    return ConversionResult(
        markdown=sanitized,
        assets=tuple(assets),
        metadata=metadata,
        locations=tuple(locations),
        warnings=tuple(warnings),
        completeness=result.completeness,
        quality_score=result.quality_score,
    )


def _validate_assets(
    assets: tuple[ProducedAsset, ...],
    *,
    limits: ConversionLimits,
    budget: _JsonBudget,
) -> list[ProducedAsset]:
    if len(assets) > limits.max_assets:
        raise ConversionError(
            "converter_asset_count_exceeded",
            "The converter produced too many assets.",
            retryable=False,
            fallback_allowed=True,
        )
    total_asset_bytes = 0
    validated_assets: list[ProducedAsset] = []
    ordinals: set[tuple[str, int]] = set()
    for asset in assets:
        if type(asset) is not ProducedAsset:
            _invalid_result()
        if type(asset.role) is not str or asset.role not in _ASSET_ROLES:
            _invalid_result()
        if (
            type(asset.media_type) is not str
            or asset.media_type not in _ALLOWED_ASSET_MEDIA_TYPES
        ):
            raise ConversionError(
                "converter_asset_media_type_denied",
                "The converter produced an unsupported asset type.",
                retryable=False,
                fallback_allowed=True,
            )
        if type(asset.content) is not bytes:
            _invalid_result()
        total_asset_bytes += len(asset.content)
        if total_asset_bytes > limits.max_asset_bytes:
            raise ConversionError(
                "converter_asset_bytes_exceeded",
                "The converter assets exceed the configured limit.",
                retryable=False,
                fallback_allowed=True,
            )
        if type(asset.ordinal) is not int or not 0 <= asset.ordinal <= _MAX_ORDINAL:
            _invalid_result()
        key = (asset.role, asset.ordinal)
        if key in ordinals:
            raise ConversionError(
                "converter_asset_ordinal_duplicate",
                "The converter returned duplicate asset ordinals.",
                retryable=False,
                fallback_allowed=True,
            )
        ordinals.add(key)
        filename = _validated_asset_filename(asset.filename)
        _validate_asset_content(asset.media_type, asset.content, budget=budget)
        validated_assets.append(
            replace(asset, filename=filename, content=bytes(asset.content))
        )
    return validated_assets


def _validated_asset_filename(filename: object) -> str | None:
    if filename is None:
        return None
    if type(filename) is not str:
        _invalid_result()
    normalized = unicodedata.normalize("NFC", filename)
    windows_stem = normalized.split(".", 1)[0].casefold()
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.endswith((" ", "."))
        or windows_stem in _WINDOWS_RESERVED_NAMES
        or len(normalized.encode("utf-8")) > 255
        or "/" in normalized
        or "\\" in normalized
        or ":" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ConversionError(
            "converter_asset_filename_invalid",
            "The converter returned an unsafe asset filename.",
            retryable=False,
            fallback_allowed=True,
        )
    return normalized


def _validate_asset_content(
    media_type: str,
    content: bytes,
    *,
    budget: _JsonBudget,
) -> None:
    valid = True
    if media_type == "image/png":
        valid = (
            len(content) >= 45
            and content.startswith(b"\x89PNG\r\n\x1a\n")
            and b"IEND" in content[-32:]
        )
    elif media_type == "image/jpeg":
        valid = (
            len(content) >= 4
            and content.startswith(b"\xff\xd8")
            and content.endswith(b"\xff\xd9")
        )
    elif media_type == "image/gif":
        valid = (
            len(content) >= 14
            and content[:6] in {b"GIF87a", b"GIF89a"}
            and content.endswith(b";")
        )
    elif media_type == "image/webp":
        valid = (
            len(content) >= 20
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    elif media_type == "image/tiff":
        valid = len(content) >= 8 and content[:4] in {
            b"II*\x00",
            b"MM\x00*",
            b"II+\x00",
            b"MM\x00+",
        }
    elif media_type == "text/plain":
        try:
            decoded = content.decode("utf-8")
            valid = "\x00" not in decoded
        except UnicodeDecodeError:
            valid = False
    elif media_type == "application/json":
        try:
            decoded = content.decode("utf-8")
            parsed = json.loads(
                decoded,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_json_object_without_duplicates,
            )
            _snapshot_json(parsed, budget=budget, depth=0, active=set())
        except (UnicodeError, ValueError, TypeError, RecursionError):
            valid = False
    if not valid:
        raise ConversionError(
            "converter_asset_content_invalid",
            "The converter asset does not match its declared media type.",
            retryable=False,
            fallback_allowed=True,
        )


def _validate_location(
    location: object,
    *,
    budget: _JsonBudget,
) -> SourceLocation:
    if type(location) is not SourceLocation:
        _invalid_result()
    if type(location.kind) is not str or not _CODE.fullmatch(location.kind):
        _invalid_result()
    if type(location.ordinal) is not int or not 0 <= location.ordinal <= _MAX_ORDINAL:
        _invalid_result()
    label = location.label
    if label is not None:
        if type(label) is not str:
            _invalid_result()
        _bounded_utf8(label, maximum=budget.max_label_bytes)
    data = _snapshot_json_mapping(
        location.data,
        budget=budget,
        forbidden_keys=_SENSITIVE_LOCATION_KEYS,
    )
    return SourceLocation(
        kind=location.kind,
        ordinal=location.ordinal,
        label=label,
        data=data,
    )


def _validate_warning(
    warning: object,
    *,
    budget: _JsonBudget,
) -> ConversionWarning:
    if type(warning) is not ConversionWarning:
        _invalid_result()
    if type(warning.code) is not str or not _CODE.fullmatch(warning.code):
        _invalid_result()
    if type(warning.message) is not str or not warning.message:
        _invalid_result()
    _bounded_utf8(warning.message, maximum=budget.max_label_bytes)
    location = (
        _validate_location(warning.location, budget=budget)
        if warning.location is not None
        else None
    )
    return ConversionWarning(
        code=warning.code,
        message=warning.message,
        location=location,
    )


def _validate_quality_score(score: object) -> None:
    if score is None:
        return
    if (
        type(score) is not Decimal
        or not score.is_finite()
        or score < Decimal("0")
        or score > Decimal("1")
    ):
        _invalid_result()


def _snapshot_json_mapping(
    value: object,
    *,
    budget: _JsonBudget,
    forbidden_keys: frozenset[str],
) -> dict[str, object]:
    snapshot = _snapshot_json(
        value,
        budget=budget,
        depth=0,
        active=set(),
        forbidden_keys=forbidden_keys,
    )
    if type(snapshot) is not dict:
        _invalid_json()
    return snapshot


def _snapshot_json(
    value: object,
    *,
    budget: _JsonBudget,
    depth: int,
    active: set[int],
    forbidden_keys: frozenset[str] = frozenset(),
) -> object:
    if depth > budget.max_depth:
        _invalid_json()
    if value is None or type(value) is bool:
        budget.consume_node()
        return value
    if type(value) is int:
        encoded_bytes = max(1, (abs(value).bit_length() + 7) // 8)
        if encoded_bytes > budget.max_scalar_bytes:
            _invalid_json()
        budget.consume_node(encoded_bytes)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _invalid_json()
        budget.consume_node(8)
        return value
    if type(value) is str:
        encoded = _bounded_utf8(value, maximum=budget.max_scalar_bytes)
        budget.consume_node(encoded)
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            _invalid_json()
        active.add(identity)
        try:
            if len(value) > budget.max_nodes - budget.nodes:
                _invalid_json()
            budget.consume_node()
            snapshot: dict[str, object] = {}
            for key, child in value.items():
                if type(key) is not str or key in snapshot:
                    _invalid_json()
                if key.casefold() in forbidden_keys:
                    raise ConversionError(
                        "converter_location_path_denied",
                        "The converter returned a forbidden path reference.",
                        retryable=False,
                        fallback_allowed=True,
                    )
                if any(ord(character) < 32 for character in key):
                    _invalid_json()
                encoded = _bounded_utf8(key, maximum=budget.max_scalar_bytes)
                budget.consume_node(encoded)
                snapshot[key] = _snapshot_json(
                    child,
                    budget=budget,
                    depth=depth + 1,
                    active=active,
                    forbidden_keys=forbidden_keys,
                )
            return snapshot
        finally:
            active.discard(identity)

    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            _invalid_json()
        active.add(identity)
        try:
            if len(value) > budget.max_nodes - budget.nodes:
                _invalid_json()
            budget.consume_node()
            return tuple(
                _snapshot_json(
                    child,
                    budget=budget,
                    depth=depth + 1,
                    active=active,
                    forbidden_keys=forbidden_keys,
                )
                for child in value
            )
        finally:
            active.discard(identity)
    _invalid_json()


def _sanitize_markdown(markdown: str) -> str:
    nul_replaced = markdown.replace("\x00", "\ufffd")
    code_body_ranges = _fenced_code_body_ranges(nul_replaced)
    if not code_body_ranges:
        return _sanitize_non_code_markdown(nul_replaced)

    output: list[str] = []
    cursor = 0
    for start, end in code_body_ranges:
        output.append(_sanitize_non_code_markdown(nul_replaced[cursor:start]))
        output.append(nul_replaced[start:end])
        cursor = end
    output.append(_sanitize_non_code_markdown(nul_replaced[cursor:]))
    return "".join(output)


def _sanitize_non_code_markdown(markdown: str) -> str:
    neutralized = _neutralize_markdown_destinations(markdown)
    return _ANGLE_OR_BR.sub(
        lambda match: (
            "<br>"
            if match.group(0).casefold().startswith("<br")
            else ("&lt;" if match.group(0) == "<" else "&gt;")
        ),
        neutralized,
    )


def _fenced_code_body_ranges(markdown: str) -> tuple[tuple[int, int], ...]:
    """Return CommonMark fenced-code body ranges with one bounded linear scan."""

    ranges: list[tuple[int, int]] = []
    open_fence: tuple[str, int] | None = None
    body_start = 0
    line_start = 0
    markdown_length = len(markdown)

    while line_start < markdown_length:
        line_end = line_start
        while line_end < markdown_length and markdown[line_end] not in "\r\n":
            line_end += 1
        next_line = line_end
        if next_line < markdown_length:
            if (
                markdown[next_line] == "\r"
                and next_line + 1 < markdown_length
                and markdown[next_line + 1] == "\n"
            ):
                next_line += 2
            else:
                next_line += 1

        line = markdown[line_start:line_end]
        if open_fence is None:
            open_fence = fence_opener(line)
            if open_fence is not None:
                body_start = next_line
        elif is_fence_closer(line, marker=open_fence[0], minimum=open_fence[1]):
            ranges.append((body_start, line_start))
            open_fence = None
        line_start = next_line

    if open_fence is not None:
        ranges.append((body_start, markdown_length))
    return tuple(ranges)


def _neutralize_markdown_destinations(markdown: str) -> str:
    ranges: list[tuple[int, int]] = []
    for match in _INLINE_DESTINATION_START.finditer(markdown):
        start = _skip_whitespace(markdown, match.end())
        end = _destination_end(markdown, start, inline=True)
        if _destination_has_dangerous_scheme(markdown[start:end]):
            ranges.append((start, end))
    for match in _REFERENCE_DESTINATION_START.finditer(markdown):
        start = _skip_horizontal_whitespace(markdown, match.end())
        end = _destination_end(markdown, start, inline=False)
        if _destination_has_dangerous_scheme(markdown[start:end]):
            ranges.append((start, end))
    if not ranges:
        return markdown
    output = markdown
    previous_start = len(markdown) + 1
    for start, end in sorted(set(ranges), reverse=True):
        if end <= start or end > previous_start:
            continue
        output = f"{output[:start]}blocked:{output[end:]}"
        previous_start = start
    return output


def _skip_whitespace(value: str, index: int) -> int:
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _skip_horizontal_whitespace(value: str, index: int) -> int:
    while index < len(value) and value[index] in " \t":
        index += 1
    return index


def _destination_end(value: str, start: int, *, inline: bool) -> int:
    maximum = min(len(value), start + 2048)
    if start < maximum and value[start] == "<":
        close = value.find(">", start + 1, maximum)
        return close + 1 if close >= 0 else maximum
    # Continue across whitespace for inline destinations.  Some renderers
    # discard embedded controls/newlines while resolving a URI, so stopping at
    # the first line break would miss ``java\nscript:`` obfuscation.
    delimiters = ")" if inline else "\r\n"
    for index in range(start, maximum):
        if value[index] in delimiters:
            return index
    return maximum


def _destination_has_dangerous_scheme(raw_destination: str) -> bool:
    candidate = raw_destination.strip().removeprefix("<").removesuffix(">")
    for _attempt in range(3):
        decoded = html.unescape(unquote(candidate))
        if decoded == candidate:
            break
        candidate = decoded
    colon = candidate.find(":")
    if colon < 0:
        return False
    raw_scheme = candidate[:colon]
    scheme = "".join(
        character
        for character in raw_scheme
        if not character.isspace()
        and not unicodedata.category(character).startswith("C")
    ).casefold()
    return scheme in _DANGEROUS_SCHEMES


def _bounded_utf8(value: str, *, maximum: int) -> int:
    encoded = len(value.encode("utf-8"))
    if encoded > maximum:
        raise ConversionError(
            "converter_output_limit_exceeded",
            "The converter output exceeds the configured limit.",
            retryable=False,
            fallback_allowed=True,
        )
    return encoded


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _invalid_json() -> None:
    raise ConversionError(
        "converter_json_limit_exceeded",
        "The converter returned invalid or excessive structured data.",
        retryable=False,
        fallback_allowed=True,
    )


def _invalid_result() -> None:
    raise ConversionError(
        "converter_result_invalid",
        "The converter returned an invalid result.",
        retryable=False,
        fallback_allowed=True,
    )


__all__ = ["SANITIZER_VERSION", "validate_conversion_result"]
