"""Bounded, no-macro OOXML text/table converter for the worker host."""

from __future__ import annotations

import io
import posixpath
import re
import stat
import zipfile
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from backend.converters.builtin_text import _bounded_text
from backend.spi.conversion import (
    CONVERTER_SPI_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConversionResult,
    ConversionWarning,
    ConverterDescriptor,
    ProbeRequest,
    ProbeResult,
    SourceLocation,
)
from backend.utils.ooxml_security import contains_active_spreadsheet_formula
from backend.utils.xml_security import contains_forbidden_xml_declaration

_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MAIN_CONTENT_TYPES = {
    "docx": (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml",
    ),
    "pptx": (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml",
    ),
    "xlsx": (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ),
}
_DANGEROUS_MEMBER_PARTS = {
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
_DANGEROUS_MEMBER_SUFFIXES = (
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
_DANGEROUS_CONTENT_TYPE_MARKERS = (
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
_DANGEROUS_RELATIONSHIP_SUFFIXES = {
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
_MARKDOWN_PUNCTUATION = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]")
_INTRA_TEXT_LINE_BREAK = " ⏎ "
_FORMULA_OMITTED_TEXT = "Formula omitted"
_PPTX_SHAPE_NAMES = frozenset({"sp", "grpSp", "graphicFrame", "cxnSp", "pic"})
_XLSX_CELL_REFERENCE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
_XLSX_MAX_COLUMNS = 16_384
_XLSX_MAX_ROWS = 1_048_576


class OOXMLConverter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.ooxml",
            converter_version="1.1.0",
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"docx", "pptx", "xlsx"}),
            media_types=frozenset(
                {
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
            ),
            priority=100,
            execution_mode="subprocess",
            network_required=False,
            deterministic=True,
            license_expression="AGPL-3.0-only OR Elastic-2.0",
            source_url="https://github.com/dtxsuite/dtxwiki",
            capabilities=frozenset({"markdown", "tables", "locations"}),
            distribution_digest="bundled:dtxwiki",
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id in self.descriptor().formats)

    def convert(self, request: ConversionRequest) -> ConversionResult:
        with request.source.open_binary() as source:
            try:
                with zipfile.ZipFile(source) as archive:
                    _validate_archive(archive, request.limits)
                    _validate_ooxml_package(archive, request.format_id, request.limits)
                    if request.format_id == "docx":
                        result = _convert_docx(archive, request)
                    elif request.format_id == "pptx":
                        result = _convert_pptx(archive, request)
                    elif request.format_id == "xlsx":
                        result = _convert_xlsx(archive, request)
                    else:
                        raise ConversionError(
                            "unsupported", "Unsupported OOXML format."
                        )
            except ConversionError:
                raise
            except (
                NotImplementedError,
                RuntimeError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
            ) as exc:
                raise ConversionError(
                    "invalid_ooxml", "The OOXML document is corrupt."
                ) from exc
        markdown, truncated_by_bytes = _bounded_text(
            result.markdown or "", request.limits.max_output_bytes
        )
        warnings = list(result.warnings)
        if truncated_by_bytes:
            warnings.append(
                ConversionWarning(
                    code="output_truncated",
                    message="The Markdown output reached the configured limit.",
                )
            )
        return ConversionResult(
            markdown=markdown,
            metadata=result.metadata,
            locations=result.locations,
            warnings=tuple(warnings),
            completeness=(
                "truncated"
                if truncated_by_bytes or result.completeness == "truncated"
                else result.completeness
            ),
            quality_score=result.quality_score,
        )


def _validate_archive(archive: zipfile.ZipFile, limits: ConversionLimits) -> None:
    entries = archive.infolist()
    if len(entries) > limits.max_archive_entries:
        raise ConversionError(
            "container_entry_limit", "The document contains too many files."
        )
    total = sum(entry.file_size for entry in entries)
    compressed = sum(max(entry.compress_size, 1) for entry in entries)
    if total > limits.max_archive_uncompressed_bytes or (
        total / max(compressed, 1) > limits.max_archive_expansion_ratio
    ):
        raise ConversionError(
            "container_expansion_limit", "The document expands beyond the safety limit."
        )
    seen: set[str] = set()
    for entry in entries:
        normalized_filename = entry.filename.replace("\\", "/")
        path = PurePosixPath(normalized_filename)
        normalized_name = str(path).casefold()
        mode = (entry.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if (
            not entry.filename
            or normalized_filename != entry.filename
            or "\x00" in entry.filename
            or ":" in entry.filename
            or path.is_absolute()
            or normalized_filename.startswith("//")
            or ".." in path.parts
            or "." in path.parts
            or len(path.parts) > limits.max_archive_depth
            or normalized_name in seen
            or stat.S_ISLNK(mode)
            or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise ConversionError(
                "container_path_invalid", "The document contains an unsafe path."
            )
        seen.add(normalized_name)
        if entry.flag_bits & 0x1:
            raise ConversionError(
                "encrypted_document",
                "Encrypted OOXML components are not supported.",
                fallback_allowed=False,
            )
        if entry.file_size > limits.max_archive_member_bytes:
            raise ConversionError(
                "container_member_limit",
                "A document component exceeds the safety limit.",
            )
        if entry.file_size / max(entry.compress_size, 1) > (
            limits.max_archive_expansion_ratio
        ):
            raise ConversionError(
                "container_expansion_limit",
                "A document component expands beyond the safety limit.",
            )


def _validate_ooxml_package(
    archive: zipfile.ZipFile,
    format_id: str,
    limits: ConversionLimits,
) -> None:
    try:
        main_member, expected_content_type = _MAIN_CONTENT_TYPES[format_id]
    except KeyError as exc:
        raise ConversionError("unsupported", "Unsupported OOXML format.") from exc
    names = set(archive.namelist())
    if main_member not in names:
        raise ConversionError(
            "invalid_ooxml", "The OOXML document is missing required content."
        )
    for name in names:
        lowered_parts = {part.casefold() for part in PurePosixPath(name).parts}
        if (
            name.casefold().endswith(_DANGEROUS_MEMBER_SUFFIXES)
            or lowered_parts & _DANGEROUS_MEMBER_PARTS
        ):
            raise ConversionError(
                "active_content_denied",
                "The document contains active or embedded content.",
                fallback_allowed=False,
            )
    content_types = _parse_xml(
        _read_member(archive, "[Content_Types].xml", limits), limits
    )
    if _local_name(content_types.tag) != "Types":
        raise ConversionError(
            "invalid_ooxml", "The OOXML content-type manifest is invalid."
        )
    override_types = {
        node.attrib.get("PartName", "").lstrip("/"): node.attrib.get("ContentType", "")
        for node in content_types
        if _local_name(node.tag) == "Override"
    }
    all_types = " ".join(
        node.attrib.get("ContentType", "") for node in content_types
    ).casefold()
    if override_types.get(main_member) != expected_content_type or any(
        marker in all_types for marker in _DANGEROUS_CONTENT_TYPE_MARKERS
    ):
        raise ConversionError(
            "active_content_denied",
            "The document content type is unsupported or contains active content.",
            fallback_allowed=False,
        )
    for relationship_name in sorted(
        name for name in names if name.casefold().endswith(".rels")
    ):
        _validate_relationships(archive, relationship_name, limits, names)
    if format_id == "xlsx":
        spreadsheet_xml_names = [main_member]
        spreadsheet_xml_names.extend(
            name
            for name in names
            if name.casefold().startswith("xl/worksheets/")
            and name.casefold().endswith(".xml")
        )
        for name in sorted(set(spreadsheet_xml_names)):
            document = _parse_xml(_read_member(archive, name, limits), limits)
            if contains_active_spreadsheet_formula(document):
                raise ConversionError(
                    "active_content_denied",
                    "The spreadsheet contains an external-capable formula.",
                    fallback_allowed=False,
                )


def _validate_relationships(
    archive: zipfile.ZipFile,
    relationship_name: str,
    limits: ConversionLimits,
    names: set[str],
) -> None:
    root = _parse_xml(_read_member(archive, relationship_name, limits), limits)
    if _local_name(root.tag) != "Relationships":
        raise ConversionError(
            "invalid_ooxml", "The document relationship root element is invalid."
        )
    for node in root:
        if _local_name(node.tag) != "Relationship":
            continue
        target = node.attrib.get("Target", "")
        target_mode = node.attrib.get("TargetMode", "Internal")
        relationship_type = node.attrib.get("Type", "").rstrip("/").rsplit("/", 1)[-1]
        if target_mode not in {"Internal", "External"}:
            raise ConversionError(
                "invalid_ooxml", "The document relationship mode is invalid."
            )
        if (
            target_mode == "External"
            or relationship_type.casefold() in _DANGEROUS_RELATIONSHIP_SUFFIXES
        ):
            raise ConversionError(
                "external_relationship_denied",
                "The document contains an external or active relationship.",
                fallback_allowed=False,
            )
        resolved = _resolve_relationship_target(relationship_name, target)
        if resolved not in names:
            raise ConversionError(
                "invalid_ooxml", "The document relationship target is missing."
            )


def _resolve_relationship_target(relationship_name: str, target: str) -> str:
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
        raise ConversionError(
            "external_relationship_denied",
            "The document contains an unsafe relationship target.",
            fallback_allowed=False,
        )
    rel_path = PurePosixPath(relationship_name)
    if rel_path.parent.name != "_rels" or not rel_path.name.endswith(".rels"):
        raise ConversionError(
            "invalid_ooxml", "The document relationship path is invalid."
        )
    source_parent = rel_path.parent.parent
    normalized = posixpath.normpath(str(source_parent / decoded))
    if normalized == ".." or normalized.startswith("../"):
        raise ConversionError(
            "external_relationship_denied",
            "The document relationship escapes the package.",
            fallback_allowed=False,
        )
    return normalized


def _convert_docx(
    archive: zipfile.ZipFile, request: ConversionRequest
) -> ConversionResult:
    root = _parse_xml(
        _read_member(archive, "word/document.xml", request.limits),
        request.limits,
    )
    if _local_name(root.tag) != "document":
        raise ConversionError(
            "invalid_ooxml", "The Word document root element is invalid."
        )
    body = next((node for node in root.iter() if _local_name(node.tag) == "body"), root)
    blocks: list[str] = []
    locations: list[SourceLocation] = []
    truncated = False
    hidden_runs_omitted = 0
    output_bytes = 0
    for child in list(body):
        name = _local_name(child.tag)
        if len(locations) >= request.limits.max_nodes:
            truncated = True
            break
        if name == "p":
            text, text_truncated, paragraph_hidden_runs = _word_node_text(
                child, request.limits.max_string_bytes
            )
            hidden_runs_omitted += paragraph_hidden_runs
            truncated = truncated or text_truncated
            if not text:
                continue
            style = next(
                (
                    value
                    for node in child.iter()
                    if _local_name(node.tag) == "pStyle"
                    for key, value in node.attrib.items()
                    if _local_name(key) == "val"
                ),
                "",
            )
            heading = re.search(r"heading\s*([1-6])", style, re.IGNORECASE)
            safe_text = _markdown_inline_text(text)
            block = (
                f"{'#' * int(heading.group(1))} {safe_text}" if heading else safe_text
            )
            remaining = max(0, request.limits.max_output_bytes - output_bytes)
            block, block_truncated = _utf8_prefix(block, remaining)
            if not block:
                truncated = True
                break
            blocks.append(block)
            output_bytes += len(block.encode("utf-8")) + 2
            truncated = truncated or block_truncated
            locations.append(SourceLocation(kind="paragraph", ordinal=len(locations)))
            if block_truncated:
                break
        elif name == "tbl":
            table, table_truncated, table_hidden_runs = _word_table(
                child,
                request,
                max(0, request.limits.max_output_bytes - output_bytes),
            )
            hidden_runs_omitted += table_hidden_runs
            if table:
                blocks.append(table)
                output_bytes += len(table.encode("utf-8")) + 2
                locations.append(SourceLocation(kind="table", ordinal=len(locations)))
            truncated = truncated or table_truncated
            if table_truncated:
                break
    return ConversionResult(
        markdown=(
            "\n\n".join(blocks).rstrip() + "\n" if blocks else "_(empty document)_\n"
        ),
        metadata={
            "paragraph_or_table_count": len(locations),
            "macros_executed": False,
            "hidden_runs_omitted": hidden_runs_omitted,
        },
        locations=tuple(locations),
        warnings=_conversion_warnings(
            truncated=truncated, hidden_content_omitted=hidden_runs_omitted
        ),
        completeness="truncated" if truncated else "complete",
        quality_score=Decimal("0.900"),
    )


def _word_table(
    node, request: ConversionRequest, output_budget: int
) -> tuple[str, bool, int]:
    rows: list[list[str]] = []
    truncated = False
    hidden_runs_omitted = 0
    estimated_bytes = 0
    for row_node in (child for child in node.iter() if _local_name(child.tag) == "tr"):
        if len(rows) >= request.limits.max_rows:
            truncated = True
            break
        cells: list[str] = []
        for cell in row_node:
            if _local_name(cell.tag) != "tc":
                continue
            if len(cells) >= request.limits.max_columns:
                truncated = True
                break
            text, cell_truncated, cell_hidden_runs = _word_node_text(
                cell, request.limits.max_cell_bytes
            )
            hidden_runs_omitted += cell_hidden_runs
            cell_bytes = len(_cell(text).encode("utf-8")) + 7
            if estimated_bytes + cell_bytes > output_budget:
                truncated = True
                break
            cells.append(text)
            estimated_bytes += cell_bytes
            truncated = truncated or cell_truncated
        if truncated and not cells:
            break
        rows.append(cells)
    table, output_truncated = _bounded_text(_markdown_table(rows), output_budget)
    return table, truncated or output_truncated, hidden_runs_omitted


def _convert_pptx(
    archive: zipfile.ZipFile, request: ConversionRequest
) -> ConversionResult:
    slide_names = _pptx_slides(archive, request)
    blocks: list[str] = []
    locations: list[SourceLocation] = []
    truncated = False
    hidden_slides_omitted = 0
    hidden_shapes_omitted = 0
    output_bytes = 0
    for index, name in enumerate(slide_names, start=1):
        root = _parse_xml(_read_member(archive, name, request.limits), request.limits)
        if _local_name(root.tag) != "sld":
            raise ConversionError(
                "invalid_ooxml", "A presentation slide root element is invalid."
            )
        if _attribute_is_false(root, "show"):
            hidden_slides_omitted += 1
            continue
        if len(locations) >= request.limits.max_pages:
            truncated = True
            break
        text_nodes, slide_hidden_shapes = _pptx_visible_text_nodes(root)
        hidden_shapes_omitted += slide_hidden_shapes
        texts: list[str] = []
        slide_text_bytes = 0
        for node in text_nodes:
            if not (node.text or "").strip():
                continue
            if len(texts) >= request.limits.max_nodes:
                truncated = True
                break
            text, text_truncated = _utf8_prefix(
                (node.text or "").strip(), request.limits.max_string_bytes
            )
            text, output_truncated = _utf8_prefix(
                text,
                max(
                    0,
                    request.limits.max_output_bytes
                    - output_bytes
                    - slide_text_bytes
                    - 32,
                ),
            )
            if not text:
                truncated = True
                break
            safe_text = _markdown_inline_text(text)
            texts.append(safe_text)
            slide_text_bytes += len(safe_text.encode("utf-8")) + 3
            truncated = truncated or text_truncated or output_truncated
            if output_truncated:
                break
        block = [f"## Slide {index}"]
        block.extend(f"- {text}" for text in texts)
        rendered = "\n".join(block)
        rendered, block_truncated = _utf8_prefix(
            rendered,
            max(0, request.limits.max_output_bytes - output_bytes),
        )
        if not rendered:
            truncated = True
            break
        blocks.append(rendered)
        output_bytes += len(rendered.encode("utf-8")) + 2
        truncated = truncated or block_truncated
        locations.append(
            SourceLocation(kind="slide", ordinal=index - 1, label=f"Slide {index}")
        )
        if block_truncated:
            break
    return ConversionResult(
        markdown=(
            "\n\n".join(blocks).rstrip() + "\n"
            if blocks
            else "_(empty presentation)_\n"
        ),
        metadata={
            "slides_rendered": len(locations),
            "macros_executed": False,
            "hidden_slides_omitted": hidden_slides_omitted,
            "hidden_shapes_omitted": hidden_shapes_omitted,
        },
        locations=tuple(locations),
        warnings=_conversion_warnings(
            truncated=truncated,
            hidden_content_omitted=hidden_slides_omitted + hidden_shapes_omitted,
        ),
        completeness="truncated" if truncated else "complete",
        quality_score=Decimal("0.850"),
    )


def _pptx_visible_text_nodes(root) -> tuple[list[ElementTree.Element], int]:
    text_nodes: list[ElementTree.Element] = []
    hidden_shapes_omitted = 0

    def visit(node) -> None:
        nonlocal hidden_shapes_omitted
        if _pptx_shape_is_hidden(node):
            hidden_shapes_omitted += 1
            return
        if _local_name(node.tag) == "t":
            text_nodes.append(node)
            return
        for child in node:
            visit(child)

    visit(root)
    return text_nodes, hidden_shapes_omitted


def _pptx_shape_is_hidden(node) -> bool:
    if _local_name(node.tag) not in _PPTX_SHAPE_NAMES:
        return False
    return any(
        _local_name(property_node.tag) == "cNvPr"
        and _attribute_is_true(property_node, "hidden")
        for non_visual_properties in node
        if _local_name(non_visual_properties.tag).startswith("nv")
        for property_node in non_visual_properties
    )


def _pptx_slides(archive: zipfile.ZipFile, request: ConversionRequest) -> list[str]:
    presentation = _parse_xml(
        _read_member(archive, "ppt/presentation.xml", request.limits),
        request.limits,
    )
    if _local_name(presentation.tag) != "presentation":
        raise ConversionError(
            "invalid_ooxml", "The presentation root element is invalid."
        )
    relationships = _parse_xml(
        _read_member(archive, "ppt/_rels/presentation.xml.rels", request.limits),
        request.limits,
    )
    targets = {
        node.attrib.get("Id"): node.attrib.get("Target")
        for node in relationships
        if node.attrib.get("TargetMode", "Internal") != "External"
        and node.attrib.get("Type", "").rsplit("/", 1)[-1].casefold() == "slide"
    }
    slides: list[str] = []
    for node in presentation.iter():
        if _local_name(node.tag) != "sldId":
            continue
        relationship_id = node.attrib.get(f"{{{_RELATIONSHIP_NS}}}id")
        target = targets.get(relationship_id)
        if not target:
            raise ConversionError(
                "invalid_ooxml", "A presentation slide relationship is missing."
            )
        normalized = _resolve_relationship_target(
            "ppt/_rels/presentation.xml.rels", target
        )
        if normalized not in archive.namelist():
            raise ConversionError(
                "invalid_ooxml", "A presentation slide target is missing."
            )
        slides.append(normalized)
    return slides


def _convert_xlsx(
    archive: zipfile.ZipFile, request: ConversionRequest
) -> ConversionResult:
    shared_strings, strings_truncated = _xlsx_shared_strings(archive, request)
    sheets, hidden_sheets_omitted = _xlsx_sheets(archive, request)
    blocks: list[str] = []
    locations: list[SourceLocation] = []
    truncated = strings_truncated
    formula_cells_omitted = 0
    hidden_rows_omitted = 0
    hidden_columns_omitted = 0
    output_bytes = 0
    for sheet_index, (title, member_name) in enumerate(sheets):
        if sheet_index >= request.limits.max_pages:
            truncated = True
            break
        root = _parse_xml(
            _read_member(archive, member_name, request.limits), request.limits
        )
        if _local_name(root.tag) != "worksheet":
            raise ConversionError(
                "invalid_ooxml", "A worksheet root element is invalid."
            )
        hidden_column_ranges, sheet_hidden_columns = _xlsx_hidden_column_ranges(root)
        hidden_columns_omitted += sheet_hidden_columns
        rows: list[list[str]] = []
        table_estimated_bytes = 0
        row_nodes = [node for node in root.iter() if _local_name(node.tag) == "row"]
        hidden_rows_omitted += sum(
            1 for row_node in row_nodes if _attribute_is_true(row_node, "hidden")
        )
        for row_node in row_nodes:
            if _attribute_is_true(row_node, "hidden"):
                continue
            if len(rows) >= request.limits.max_rows:
                truncated = True
                break
            cells: list[str] = []
            cell_nodes = [child for child in row_node if _local_name(child.tag) == "c"]
            for sequential_column, cell in enumerate(cell_nodes, start=1):
                column = _xlsx_cell_column(cell, fallback=sequential_column)
                if _xlsx_column_is_hidden(column, hidden_column_ranges):
                    continue
                if len(cells) >= request.limits.max_columns:
                    truncated = True
                    break
                value, formula_omitted, cell_truncated = _xlsx_cell_value(
                    cell,
                    shared_strings,
                    max_bytes=request.limits.max_cell_bytes,
                )
                cell_bytes = len(_cell(value).encode("utf-8")) + 7
                if output_bytes + table_estimated_bytes + cell_bytes > (
                    request.limits.max_output_bytes
                ):
                    truncated = True
                    break
                cells.append(value)
                table_estimated_bytes += cell_bytes
                formula_cells_omitted += int(formula_omitted)
                truncated = truncated or cell_truncated
            if truncated and not cells:
                break
            rows.append(cells)
        sheet = (
            f"## {_markdown_inline_text(title)}\n\n"
            f"{_markdown_table(rows) or '_(empty sheet)_'}"
        )
        sheet, sheet_truncated = _utf8_prefix(
            sheet,
            max(0, request.limits.max_output_bytes - output_bytes),
        )
        if not sheet:
            truncated = True
            break
        blocks.append(sheet)
        output_bytes += len(sheet.encode("utf-8")) + 2
        truncated = truncated or sheet_truncated
        locations.append(SourceLocation(kind="sheet", ordinal=sheet_index, label=title))
        if sheet_truncated:
            break
    return ConversionResult(
        markdown=(
            "\n\n".join(blocks).rstrip() + "\n" if blocks else "_(empty workbook)_\n"
        ),
        metadata={
            "sheets_rendered": len(locations),
            "formulas_executed": False,
            "formula_cells_omitted": formula_cells_omitted,
            "hidden_sheets_omitted": hidden_sheets_omitted,
            "hidden_rows_omitted": hidden_rows_omitted,
            "hidden_columns_omitted": hidden_columns_omitted,
        },
        locations=tuple(locations),
        warnings=_conversion_warnings(
            truncated=truncated,
            hidden_content_omitted=(
                hidden_sheets_omitted + hidden_rows_omitted + hidden_columns_omitted
            ),
        ),
        completeness="truncated" if truncated else "complete",
        quality_score=Decimal("0.850"),
    )


def _xlsx_shared_strings(
    archive: zipfile.ZipFile, request: ConversionRequest
) -> tuple[list[str], bool]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return [], False
    root = _parse_xml(
        _read_member(archive, "xl/sharedStrings.xml", request.limits),
        request.limits,
    )
    if _local_name(root.tag) != "sst":
        raise ConversionError(
            "invalid_ooxml", "The shared-string table root element is invalid."
        )
    strings: list[str] = []
    truncated = False
    stored_bytes = 0
    for node in root:
        if _local_name(node.tag) != "si":
            continue
        if len(strings) >= request.limits.max_nodes:
            truncated = True
            break
        text, text_truncated = _node_text(node, request.limits.max_cell_bytes)
        remaining = max(0, request.limits.max_output_bytes - stored_bytes)
        text, output_truncated = _utf8_prefix(text, remaining)
        strings.append(text)
        stored_bytes += len(text.encode("utf-8"))
        truncated = truncated or text_truncated or output_truncated
    return strings, truncated


def _xlsx_hidden_column_ranges(root) -> tuple[tuple[tuple[int, int], ...], int]:
    ranges: list[tuple[int, int]] = []
    for columns in root:
        if _local_name(columns.tag) != "cols":
            continue
        for node in columns:
            if _local_name(node.tag) != "col" or not _attribute_is_true(node, "hidden"):
                continue
            start = _xlsx_column_bound(node.attrib.get("min"))
            end = _xlsx_column_bound(node.attrib.get("max"))
            if start > end:
                raise ConversionError(
                    "invalid_ooxml",
                    "The worksheet contains an invalid hidden-column range.",
                    fallback_allowed=False,
                )
            ranges.append((start, end))

    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return tuple(merged), sum(end - start + 1 for start, end in merged)


def _xlsx_column_bound(value: str | None) -> int:
    if value is None or len(value) > 5 or re.fullmatch(r"[0-9]+", value) is None:
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an invalid hidden-column range.",
            fallback_allowed=False,
        )
    column = int(value)
    if not 1 <= column <= _XLSX_MAX_COLUMNS:
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an invalid hidden-column range.",
            fallback_allowed=False,
        )
    return column


def _xlsx_cell_column(node, *, fallback: int) -> int:
    reference = node.attrib.get("r")
    if reference is None:
        if fallback <= _XLSX_MAX_COLUMNS:
            return fallback
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an out-of-range cell.",
            fallback_allowed=False,
        )
    match = _XLSX_CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an invalid cell reference.",
            fallback_allowed=False,
        )
    if int(match.group(2)) > _XLSX_MAX_ROWS:
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an out-of-range cell.",
            fallback_allowed=False,
        )
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - ord("A") + 1
    if column > _XLSX_MAX_COLUMNS:
        raise ConversionError(
            "invalid_ooxml",
            "The worksheet contains an out-of-range cell.",
            fallback_allowed=False,
        )
    return column


def _xlsx_column_is_hidden(column: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    low = 0
    high = len(ranges)
    while low < high:
        middle = (low + high) // 2
        start, end = ranges[middle]
        if column < start:
            high = middle
        elif column > end:
            low = middle + 1
        else:
            return True
    return False


def _xlsx_sheets(
    archive: zipfile.ZipFile, request: ConversionRequest
) -> tuple[list[tuple[str, str]], int]:
    workbook = _parse_xml(
        _read_member(archive, "xl/workbook.xml", request.limits), request.limits
    )
    if _local_name(workbook.tag) != "workbook":
        raise ConversionError("invalid_ooxml", "The workbook root element is invalid.")
    relationships = _parse_xml(
        _read_member(archive, "xl/_rels/workbook.xml.rels", request.limits),
        request.limits,
    )
    targets = {
        node.attrib.get("Id"): node.attrib.get("Target")
        for node in relationships
        if node.attrib.get("TargetMode", "Internal") != "External"
    }
    sheets: list[tuple[str, str]] = []
    hidden_sheets_omitted = 0
    for node in workbook.iter():
        if _local_name(node.tag) != "sheet":
            continue
        if node.attrib.get("state", "visible").casefold() in {"hidden", "veryhidden"}:
            hidden_sheets_omitted += 1
            continue
        relationship_id = node.attrib.get(f"{{{_RELATIONSHIP_NS}}}id")
        target = targets.get(relationship_id)
        if not target:
            continue
        normalized = _resolve_relationship_target("xl/_rels/workbook.xml.rels", target)
        if normalized not in archive.namelist():
            raise ConversionError(
                "invalid_ooxml", "The workbook sheet target is missing."
            )
        title, _ = _utf8_prefix(
            node.attrib.get("name") or "Sheet", request.limits.max_string_bytes
        )
        sheets.append((title, normalized))
    return sheets, hidden_sheets_omitted


def _xlsx_cell_value(
    node, shared_strings: list[str], *, max_bytes: int
) -> tuple[str, bool, bool]:
    cell_type = node.attrib.get("t")
    formula = next(
        ((child.text or "") for child in node if _local_name(child.tag) == "f"),
        None,
    )
    if formula is not None:
        rendered, truncated = _utf8_prefix(_FORMULA_OMITTED_TEXT, max_bytes)
        return rendered, True, truncated
    if cell_type == "inlineStr":
        rendered, truncated = _node_text(node, max_bytes)
        return rendered, False, truncated
    value = next(
        ((child.text or "") for child in node if _local_name(child.tag) == "v"),
        "",
    )
    if cell_type == "s" and value.isdigit():
        index = int(value)
        if 0 <= index < len(shared_strings):
            rendered, truncated = _utf8_prefix(shared_strings[index], max_bytes)
            return rendered, False, truncated
    rendered, truncated = _utf8_prefix(value, max_bytes)
    return rendered, False, truncated


def _read_member(
    archive: zipfile.ZipFile, name: str, limits: ConversionLimits
) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ConversionError(
            "invalid_ooxml", "The OOXML document is missing required content."
        ) from exc
    if info.is_dir() or info.file_size > limits.max_archive_member_bytes:
        raise ConversionError(
            "container_member_limit", "A document component exceeds the safety limit."
        )
    with archive.open(info) as stream:
        content = stream.read(limits.max_archive_member_bytes + 1)
    if len(content) > limits.max_archive_member_bytes:
        raise ConversionError(
            "container_member_limit", "A document component exceeds the safety limit."
        )
    return content


def _parse_xml(content: bytes, limits: ConversionLimits):
    if contains_forbidden_xml_declaration(content):
        raise ConversionError(
            "xml_entity_denied", "The document contains a forbidden XML declaration."
        )
    try:
        events = ElementTree.iterparse(io.BytesIO(content), events=("start", "end"))
        depth = 0
        nodes = 0
        for event, node in events:
            if event == "start":
                depth += 1
                nodes += 1
                if depth > limits.max_depth or nodes > limits.max_nodes:
                    raise ConversionError(
                        "xml_structure_limit",
                        "The document XML exceeds a configured structure limit.",
                        fallback_allowed=False,
                    )
                string_values = (*node.attrib.keys(), *node.attrib.values())
                for value in string_values:
                    if value is not None and len(value.encode("utf-8")) > (
                        limits.max_string_bytes
                    ):
                        raise ConversionError(
                            "xml_string_limit",
                            "The document XML contains an oversized string.",
                            fallback_allowed=False,
                        )
            else:
                for value in (node.text, node.tail):
                    if value is not None and len(value.encode("utf-8")) > (
                        limits.max_string_bytes
                    ):
                        raise ConversionError(
                            "xml_string_limit",
                            "The document XML contains an oversized string.",
                            fallback_allowed=False,
                        )
                depth -= 1
        return events.root
    except ElementTree.ParseError as exc:
        raise ConversionError(
            "invalid_ooxml", "The OOXML document contains invalid XML."
        ) from exc


def _node_text(node, max_bytes: int) -> tuple[str, bool]:
    parts: list[str] = []
    used = 0
    truncated = False
    for child in node.iter():
        name = _local_name(child.tag)
        if name not in {"t", "tab", "br"}:
            continue
        value = "\t" if name == "tab" else "\n" if name == "br" else child.text or ""
        encoded = value.encode("utf-8")
        if used + len(encoded) > max_bytes:
            prefix, _ = _utf8_prefix(value, max_bytes - used)
            parts.append(prefix)
            truncated = True
            break
        parts.append(value)
        used += len(encoded)
    return "".join(parts).strip(), truncated


def _word_node_text(node, max_bytes: int) -> tuple[str, bool, int]:
    parts: list[str] = []
    used = 0
    truncated = False
    hidden_runs_omitted = 0

    def visit(current) -> None:
        nonlocal hidden_runs_omitted, truncated, used
        if truncated:
            return
        if _local_name(current.tag) == "r" and _word_run_is_hidden(current):
            hidden_runs_omitted += 1
            return
        name = _local_name(current.tag)
        if name in {"t", "tab", "br"}:
            value = (
                "\t" if name == "tab" else "\n" if name == "br" else current.text or ""
            )
            encoded = value.encode("utf-8")
            if used + len(encoded) > max_bytes:
                prefix, _ = _utf8_prefix(value, max_bytes - used)
                parts.append(prefix)
                truncated = True
                return
            parts.append(value)
            used += len(encoded)
        for child in current:
            visit(child)

    visit(node)
    return "".join(parts).strip(), truncated, hidden_runs_omitted


def _word_run_is_hidden(node) -> bool:
    run_properties = next(
        (child for child in node if _local_name(child.tag) == "rPr"), None
    )
    if run_properties is None:
        return False
    return any(
        _local_name(property_node.tag) in {"vanish", "webHidden"}
        and _on_off_property_enabled(property_node)
        for property_node in run_properties
    )


def _on_off_property_enabled(node) -> bool:
    value = next(
        (
            attribute_value
            for attribute_name, attribute_value in node.attrib.items()
            if _local_name(attribute_name) == "val"
        ),
        None,
    )
    return value is None or value.strip().casefold() not in {"0", "false", "off", "no"}


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _markdown_inline_text(value: str) -> str:
    """Serialize attacker-controlled Office text as one inert Markdown fragment."""

    normalized = (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", "    ")
        .replace("\n", _INTRA_TEXT_LINE_BREAK)
    )
    return _MARKDOWN_PUNCTUATION.sub(lambda match: f"\\{match.group(0)}", normalized)


def _markdown_table(rows: list[list[str]]) -> str:
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        "| " + " | ".join(_cell(value) for value in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_cell(value) for value in row) + " |"
        for row in normalized[1:]
    )
    return "\n".join(lines)


def _cell(value: str) -> str:
    return _markdown_inline_text(value)


def _attribute_is_true(node, name: str) -> bool:
    value = node.attrib.get(name)
    return value is not None and value.strip().casefold() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _attribute_is_false(node, name: str) -> bool:
    value = node.attrib.get(name)
    return value is not None and value.strip().casefold() in {
        "0",
        "false",
        "off",
        "no",
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _truncated_warning(truncated: bool) -> tuple[ConversionWarning, ...]:
    if not truncated:
        return ()
    return (
        ConversionWarning(
            code="structure_truncated",
            message="The document was truncated at a configured structure limit.",
        ),
    )


def _conversion_warnings(
    *, truncated: bool, hidden_content_omitted: int
) -> tuple[ConversionWarning, ...]:
    warnings = list(_truncated_warning(truncated))
    if hidden_content_omitted:
        warnings.append(
            ConversionWarning(
                code="hidden_content_omitted",
                message="Hidden Office content was omitted from the conversion.",
            )
        )
    return tuple(warnings)


__all__ = ["OOXMLConverter"]
