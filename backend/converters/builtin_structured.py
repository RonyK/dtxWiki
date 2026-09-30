"""Bounded CSV, TSV, JSON and YAML to Markdown conversion."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceStartEvent,
)

from backend.converters.builtin_text import (
    _bounded_fenced_text,
    _bounded_text,
    decode_library_text,
)
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

STRUCTURED_CONVERTER_MAX_INPUT_BYTES = 10_000_000


class BuiltinStructuredConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.builtin.structured",
            converter_version="1.0.0",
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"csv", "tsv", "json", "yaml"}),
            media_types=frozenset(
                {
                    "text/csv",
                    "text/tab-separated-values",
                    "application/json",
                    "application/yaml",
                }
            ),
            priority=100,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="AGPL-3.0-only OR Elastic-2.0",
            source_url="https://github.com/dtxsuite/dtxwiki",
            capabilities=frozenset({"markdown", "tables", "locations"}),
            max_input_bytes=STRUCTURED_CONVERTER_MAX_INPUT_BYTES,
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(
            supported=request.format_id in {"csv", "tsv", "json", "yaml"}
        )

    def convert(self, request: ConversionRequest) -> ConversionResult:
        max_input_bytes = min(
            request.limits.max_input_bytes,
            STRUCTURED_CONVERTER_MAX_INPUT_BYTES,
        )
        with request.source.open_binary() as stream:
            content = stream.read(max_input_bytes + 1)
        if len(content) > max_input_bytes:
            raise ConversionError(
                "input_limit_exceeded",
                "The source exceeds the converter input limit.",
                fallback_allowed=False,
            )
        text, encoding = decode_library_text(content)
        if request.format_id in {"csv", "tsv"}:
            result = self._convert_delimited(text, request)
        elif request.format_id == "json":
            result = self._convert_json(text, request)
        else:
            result = self._convert_yaml(text, request)
        return ConversionResult(
            markdown=result.markdown,
            assets=result.assets,
            metadata={**result.metadata, "source_encoding": encoding},
            locations=result.locations,
            warnings=result.warnings,
            completeness=result.completeness,
            quality_score=result.quality_score,
        )

    def _convert_delimited(
        self, text: str, request: ConversionRequest
    ) -> ConversionResult:
        delimiter = "\t" if request.format_id == "tsv" else ","
        if request.format_id == "csv" and text:
            try:
                delimiter = (
                    csv.Sniffer().sniff(text[:65_536], delimiters=",;\t|").delimiter
                )
            except csv.Error:
                pass
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        rows: list[list[str]] = []
        row_provenance: list[tuple[int, bool]] = []
        row_limit_reached = False
        column_limit_reached = False
        source_rows_examined = 0
        source_columns_max = 0
        try:
            for row_index, row in enumerate(reader):
                source_rows_examined = row_index + 1
                if row_index >= request.limits.max_rows:
                    row_limit_reached = True
                    break
                source_column_count = len(row)
                source_columns_max = max(source_columns_max, source_column_count)
                if any(
                    len(cell.encode("utf-8")) > request.limits.max_cell_bytes
                    for cell in row
                ):
                    raise ConversionError(
                        "cell_limit_exceeded",
                        "A table cell exceeds the conversion limit.",
                        fallback_allowed=False,
                    )
                row_is_partial = source_column_count > request.limits.max_columns
                if row_is_partial:
                    row = row[: request.limits.max_columns]
                    column_limit_reached = True
                rows.append(row)
                row_provenance.append((source_column_count, row_is_partial))
        except csv.Error as exc:
            raise ConversionError(
                "invalid_delimited",
                "The delimited document is invalid or exceeds parser limits.",
                fallback_allowed=False,
            ) from exc
        width = max((len(row) for row in rows), default=0)
        if width == 0:
            markdown = "_(empty table)_\n"
        else:
            normalized = [row + [""] * (width - len(row)) for row in rows]
            header = normalized[0]
            body = normalized[1:]
            lines = [
                "| " + " | ".join(_markdown_cell(cell) for cell in header) + " |",
                "| " + " | ".join("---" for _ in range(width)) + " |",
            ]
            lines.extend(
                "| " + " | ".join(_markdown_cell(cell) for cell in row) + " |"
                for row in body
            )
            markdown = "\n".join(lines) + "\n"
        markdown, output_truncated = _bounded_text(
            markdown, request.limits.max_output_bytes
        )
        truncated = row_limit_reached or column_limit_reached or output_truncated
        warnings = _delimited_warnings(
            row_limit_reached=row_limit_reached,
            column_limit_reached=column_limit_reached,
            output_limit_reached=output_truncated,
        )
        return ConversionResult(
            markdown=markdown,
            metadata={
                "delimiter": delimiter,
                "rows_rendered": len(rows),
                "columns_rendered": width,
                "source_rows_examined": source_rows_examined,
                "source_columns_max": source_columns_max,
                "row_limit_reached": row_limit_reached,
                "column_limit_reached": column_limit_reached,
                "output_limit_reached": output_truncated,
            },
            locations=tuple(
                SourceLocation(
                    kind="row",
                    ordinal=index,
                    data={
                        "row": index + 1,
                        "source_columns": source_columns,
                        "partial": partial,
                    },
                )
                for index, (source_columns, partial) in enumerate(row_provenance)
            ),
            warnings=warnings,
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("1.000"),
        )

    def _convert_json(self, text: str, request: ConversionRequest) -> ConversionResult:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConversionError(
                "invalid_json", "The JSON document is invalid.", fallback_allowed=False
            ) from exc
        nodes, depth = _json_shape(
            value,
            max_nodes=request.limits.max_nodes,
            max_string_bytes=request.limits.max_string_bytes,
        )
        if depth > request.limits.max_depth:
            raise ConversionError(
                "structure_depth_exceeded",
                "The JSON nesting depth exceeds the conversion limit.",
                fallback_allowed=False,
            )
        rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        markdown, truncated = _bounded_fenced_text(
            rendered,
            language="json",
            max_bytes=request.limits.max_output_bytes,
        )
        return ConversionResult(
            markdown=markdown,
            metadata={"nodes": nodes, "depth": depth},
            warnings=_truncation_warnings(truncated),
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("1.000"),
        )

    def _convert_yaml(self, text: str, request: ConversionRequest) -> ConversionResult:
        event_count, node_count, alias_count, depth, documents = _yaml_shape(
            text, request
        )
        markdown, truncated = _bounded_fenced_text(
            text,
            language="yaml",
            max_bytes=request.limits.max_output_bytes,
        )
        return ConversionResult(
            markdown=markdown,
            metadata={
                "events": event_count,
                "nodes": node_count,
                "alias_tokens": alias_count,
                "depth": depth,
                "documents": documents,
            },
            warnings=_truncation_warnings(truncated),
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("0.950"),
        )


def _json_shape(
    value: Any, *, max_nodes: int, max_string_bytes: int
) -> tuple[int, int]:
    nodes = 0
    maximum_depth = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ConversionError(
                "structure_node_limit_exceeded",
                "The structured document contains too many nodes.",
                fallback_allowed=False,
            )
        maximum_depth = max(maximum_depth, depth)
        if isinstance(current, str) and len(current.encode("utf-8")) > max_string_bytes:
            raise ConversionError(
                "string_limit_exceeded",
                "A JSON string exceeds the conversion limit.",
                fallback_allowed=False,
            )
        if isinstance(current, dict):
            if any(len(key.encode("utf-8")) > max_string_bytes for key in current):
                raise ConversionError(
                    "string_limit_exceeded",
                    "A JSON string exceeds the conversion limit.",
                    fallback_allowed=False,
                )
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return nodes, maximum_depth


def _yaml_shape(
    text: str, request: ConversionRequest
) -> tuple[int, int, int, int, int]:
    """Validate YAML syntax and limits by parsing events without construction."""

    event_count = 0
    node_count = 0
    alias_count = 0
    depth = 0
    maximum_depth = 0
    documents = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            event_count += 1
            if event_count > request.limits.max_yaml_events:
                raise ConversionError(
                    "yaml_event_limit_exceeded",
                    "The YAML event count exceeds the conversion limit.",
                    fallback_allowed=False,
                )
            if isinstance(
                event,
                (AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent),
            ):
                node_count += 1
                if node_count > request.limits.max_nodes:
                    raise ConversionError(
                        "structure_node_limit_exceeded",
                        "The YAML document contains too many nodes.",
                        fallback_allowed=False,
                    )
            if (
                isinstance(event, ScalarEvent)
                and len(event.value.encode("utf-8")) > request.limits.max_string_bytes
            ):
                raise ConversionError(
                    "string_limit_exceeded",
                    "A YAML scalar exceeds the conversion limit.",
                    fallback_allowed=False,
                )
            if isinstance(event, AliasEvent):
                alias_count += 1
                if alias_count > request.limits.max_yaml_aliases:
                    raise ConversionError(
                        "yaml_alias_limit_exceeded",
                        "The YAML alias count exceeds the conversion limit.",
                        fallback_allowed=False,
                    )
            if isinstance(event, CollectionStartEvent):
                depth += 1
                maximum_depth = max(maximum_depth, depth)
                if maximum_depth > request.limits.max_depth:
                    raise ConversionError(
                        "structure_depth_exceeded",
                        "The YAML nesting depth exceeds the conversion limit.",
                        fallback_allowed=False,
                    )
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            elif isinstance(event, DocumentStartEvent):
                documents += 1
    except ConversionError:
        raise
    except yaml.YAMLError as exc:
        raise ConversionError(
            "invalid_yaml",
            "The YAML document is invalid.",
            fallback_allowed=False,
        ) from exc
    return event_count, node_count, alias_count, maximum_depth, documents


def _markdown_cell(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _truncation_warnings(truncated: bool) -> tuple[ConversionWarning, ...]:
    if not truncated:
        return ()
    return (
        ConversionWarning(
            code="output_truncated",
            message="The Markdown output was truncated at the configured limit.",
        ),
    )


def _delimited_warnings(
    *,
    row_limit_reached: bool,
    column_limit_reached: bool,
    output_limit_reached: bool,
) -> tuple[ConversionWarning, ...]:
    warnings: list[ConversionWarning] = []
    if row_limit_reached:
        warnings.append(
            ConversionWarning(
                code="row_limit_reached",
                message="Rows after the configured table limit were omitted.",
            )
        )
    if column_limit_reached:
        warnings.append(
            ConversionWarning(
                code="column_limit_reached",
                message="Columns after the configured table limit were omitted.",
            )
        )
    if output_limit_reached:
        warnings.extend(_truncation_warnings(True))
    return tuple(warnings)


__all__ = ["BuiltinStructuredConverter"]
