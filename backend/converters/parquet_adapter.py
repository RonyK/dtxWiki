"""Optional bounded PyArrow Parquet schema/sample converter."""

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


class ParquetConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.parquet",
            converter_version=_package_version("pyarrow"),
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"parquet"}),
            media_types=frozenset(
                {"application/vnd.apache.parquet", "application/x-parquet"}
            ),
            priority=100,
            execution_mode="subprocess",
            network_required=False,
            deterministic=True,
            license_expression="Apache-2.0",
            source_url="https://github.com/apache/arrow",
            capabilities=frozenset({"markdown", "tables", "locations"}),
            distribution_digest="installed-environment-sbom",
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id == "parquet")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise ConversionError(
                "converter_dependency_missing",
                "The Parquet converter is not installed.",
            ) from exc

        try:
            with request.source.open_binary() as stream:
                _validate_parquet_envelope(stream, request.byte_size)
                try:
                    source = parquet.ParquetFile(
                        stream,
                        thrift_string_size_limit=min(
                            request.limits.max_string_bytes, 2_147_483_647
                        ),
                        thrift_container_size_limit=min(
                            request.limits.max_nodes, 2_147_483_647
                        ),
                        arrow_extensions_enabled=False,
                        memory_map=False,
                        buffer_size=0,
                        pre_buffer=False,
                        page_checksum_verification=True,
                    )
                except TypeError as exc:
                    raise ConversionError(
                        "converter_version_unsupported",
                        "The installed PyArrow lacks required metadata limits.",
                        fallback_allowed=False,
                    ) from exc
                metadata = source.metadata
                schema = source.schema_arrow
                raw_columns = list(schema.names[: request.limits.max_columns])
                columns: list[str] = []
                display_columns: list[str] = []
                header_bytes = 0
                for column in raw_columns:
                    display = _utf8_prefix(str(column), request.limits.max_cell_bytes)
                    column_bytes = len(_cell(display).encode("utf-8")) + 7
                    if header_bytes + column_bytes > (request.limits.max_output_bytes):
                        break
                    columns.append(column)
                    display_columns.append(display)
                    header_bytes += column_bytes
                sample_rows: list[list[str]] = []
                decoded_bytes = 0
                sample_output_bytes = header_bytes
                sample_output_limited = len(columns) < len(raw_columns)
                sample_limit = min(
                    request.limits.max_rows,
                    request.limits.max_nodes,
                )
                decoded_limit = min(
                    request.limits.max_archive_uncompressed_bytes,
                    request.limits.max_output_bytes
                    + request.limits.max_cell_bytes * max(1, len(columns)),
                )
                row_group = _safe_sample_row_group(
                    metadata,
                    decoded_limit=decoded_limit,
                    max_row_groups=min(
                        request.limits.max_nodes,
                        request.limits.max_pages,
                    ),
                )
                if row_group is None:
                    sample_output_limited = metadata.num_rows > 0
                batches = (
                    ()
                    if row_group is None or not columns
                    else source.iter_batches(
                        batch_size=1,
                        columns=columns,
                        use_threads=False,
                        row_groups=[row_group],
                    )
                )
                for batch in batches:
                    batch_bytes = int(getattr(batch, "nbytes", 0))
                    if (
                        batch_bytes < 0
                        or decoded_bytes + batch_bytes > decoded_limit
                        or batch_bytes
                        > request.limits.max_cell_bytes * max(1, len(columns))
                    ):
                        break
                    decoded_bytes += batch_bytes
                    values = batch.to_pylist()
                    for value in values:
                        row = [
                            _bounded_value(
                                value.get(column),
                                max_bytes=request.limits.max_cell_bytes,
                                max_depth=request.limits.max_depth,
                                max_nodes=request.limits.max_nodes,
                            )
                            for column in columns
                        ]
                        row_bytes = sum(
                            len(_cell(cell).encode("utf-8")) + 3 for cell in row
                        )
                        if sample_output_bytes + row_bytes > (
                            request.limits.max_output_bytes
                        ):
                            sample_output_limited = True
                            break
                        sample_rows.append(row)
                        sample_output_bytes += row_bytes
                        if len(sample_rows) >= sample_limit:
                            break
                    if len(sample_rows) >= sample_limit or sample_output_limited:
                        break
        except ConversionError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConversionError(
                "invalid_parquet", "The Parquet file is corrupt or unsupported."
            ) from exc

        schema_lines = ["## Schema", ""]
        schema_bytes = len("\n".join(schema_lines).encode("utf-8"))
        schema_truncated = False
        schema_fields_rendered = 0
        for field in schema:
            if schema_fields_rendered >= request.limits.max_columns:
                schema_truncated = True
                break
            line = _bounded_schema_field(
                field,
                min(
                    request.limits.max_string_bytes,
                    request.limits.max_output_bytes,
                ),
            )
            line_bytes = len(line.encode("utf-8")) + 1
            if schema_bytes + line_bytes > request.limits.max_output_bytes:
                schema_truncated = True
                break
            schema_lines.append(line)
            schema_bytes += line_bytes
            schema_fields_rendered += 1
        sample = (
            _markdown_table([display_columns, *sample_rows])
            if display_columns
            else "_(no columns)_\n"
        )
        markdown = "\n".join(schema_lines) + "\n\n## Bounded sample\n\n" + sample
        markdown, output_truncated = _bounded_text(
            markdown, request.limits.max_output_bytes
        )
        structurally_truncated = (
            len(schema.names) > len(columns)
            or metadata.num_rows > len(sample_rows)
            and len(sample_rows) < sample_limit
            or sample_output_limited
            or schema_truncated
        )
        truncated = structurally_truncated or output_truncated
        return ConversionResult(
            markdown=markdown,
            metadata={
                "row_count": metadata.num_rows,
                "row_group_count": metadata.num_row_groups,
                "column_count": metadata.num_columns,
                "sample_rows": len(sample_rows),
                "schema_fields_rendered": schema_fields_rendered,
                "decoded_sample_bytes": decoded_bytes,
                "arrow_extensions_enabled": False,
            },
            locations=tuple(
                SourceLocation(kind="row_group", ordinal=index)
                for index in range(
                    min(
                        metadata.num_row_groups,
                        request.limits.max_nodes,
                        request.limits.max_pages,
                    )
                )
            ),
            warnings=_truncation_warning(truncated),
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("0.950"),
        )


def _validate_parquet_envelope(stream: Any, byte_size: int) -> None:
    if byte_size < 12:
        raise ConversionError("invalid_parquet", "The Parquet file is corrupt.")
    stream.seek(0)
    header = stream.read(4)
    stream.seek(byte_size - 8)
    footer = stream.read(8)
    stream.seek(0)
    footer_size = int.from_bytes(footer[:4], "little", signed=False)
    if header != b"PAR1" or footer[4:] != b"PAR1" or footer_size > byte_size - 12:
        raise ConversionError("invalid_parquet", "The Parquet file is corrupt.")


def _safe_sample_row_group(
    metadata: Any, *, decoded_limit: int, max_row_groups: int
) -> int | None:
    for row_group_index in range(min(metadata.num_row_groups, max_row_groups)):
        row_group = metadata.row_group(row_group_index)
        total_uncompressed = 0
        for column_index in range(row_group.num_columns):
            size = int(row_group.column(column_index).total_uncompressed_size)
            if size < 0 or total_uncompressed + size > decoded_limit:
                break
            total_uncompressed += size
        else:
            return row_group_index
    return None


def _bounded_schema_field(field: Any, max_bytes: int) -> str:
    name = _utf8_prefix(str(field.name), max_bytes)
    field_type = _utf8_prefix(str(field.type), max_bytes)
    return f"- `{_markdown_code(name)}`: `{_markdown_code(field_type)}`"


def _bounded_value(
    value: Any,
    *,
    max_bytes: int,
    max_depth: int,
    max_nodes: int,
) -> str:
    if _nested_value_exceeds_limits(value, max_depth=max_depth, max_nodes=max_nodes):
        return "[nested value omitted]"
    return _utf8_prefix(repr(value), max_bytes)


def _nested_value_exceeds_limits(value: Any, *, max_depth: int, max_nodes: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return True
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif isinstance(current, (list, tuple)):
            stack.extend((item, depth + 1) for item in current)
    return False


def _utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    if max_bytes < 3:
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _markdown_code(value: str) -> str:
    return value.replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows or not rows[0]:
        return "_(empty table)_\n"
    header = rows[0]
    lines = [
        "| " + " | ".join(_cell(value) for value in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_cell(value) for value in row) + " |" for row in rows[1:]
    )
    return "\n".join(lines) + "\n"


def _cell(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\n", "<br>")
    )


def _truncation_warning(truncated: bool) -> tuple[ConversionWarning, ...]:
    if not truncated:
        return ()
    return (
        ConversionWarning(
            code="output_truncated",
            message="The Parquet projection reached a configured limit.",
        ),
    )


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not-installed"


__all__ = ["ParquetConverter"]
