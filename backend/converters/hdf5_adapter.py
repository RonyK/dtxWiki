"""Optional bounded HDF5 metadata/sample converter for the subprocess host."""

from __future__ import annotations

from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
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

_BUILTIN_HDF5_FILTER_IDS = frozenset({1, 2, 3, 4, 5, 6})


class Hdf5Converter:
    def descriptor(self) -> ConverterDescriptor:
        return ConverterDescriptor(
            converter_id="dtxwiki.hdf5",
            converter_version=_package_version("h5py"),
            spi_version=CONVERTER_SPI_VERSION,
            formats=frozenset({"hdf5"}),
            media_types=frozenset({"application/x-hdf5", "application/x-hdf"}),
            priority=100,
            execution_mode="subprocess",
            network_required=False,
            deterministic=True,
            license_expression="BSD-3-Clause",
            source_url="https://github.com/h5py/h5py",
            capabilities=frozenset({"markdown", "tables", "locations"}),
            distribution_digest="installed-environment-sbom",
        )

    def probe(self, request: ProbeRequest) -> ProbeResult:
        return ProbeResult(supported=request.format_id == "hdf5")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        try:
            import h5py
        except ImportError as exc:
            raise ConversionError(
                "converter_dependency_missing", "The HDF5 converter is not installed."
            ) from exc

        rows: list[list[str]] = []
        locations: list[SourceLocation] = []
        truncated = False
        visited: set[int] = set()
        remaining_output = max(0, request.limits.max_output_bytes - 128)

        def append_row(row: list[str]) -> bool:
            nonlocal remaining_output, truncated
            row_bytes = sum(len(_cell(value).encode("utf-8")) + 3 for value in row)
            if row_bytes > remaining_output:
                truncated = True
                return False
            rows.append(row)
            remaining_output -= row_bytes
            return True

        def walk(group, path: str, depth: int) -> None:
            nonlocal truncated
            if depth > request.limits.max_depth:
                truncated = True
                return
            remaining = min(
                request.limits.max_nodes - len(rows),
                request.limits.max_rows - len(rows),
            )
            if remaining <= 0:
                truncated = True
                return
            names = list(islice(group.keys(), remaining + 1))
            if len(names) > remaining:
                names = names[:remaining]
                truncated = True
            for name in sorted(names):
                if len(rows) >= min(request.limits.max_nodes, request.limits.max_rows):
                    truncated = True
                    return
                if len(str(name).encode("utf-8")) > request.limits.max_string_bytes:
                    if not append_row(
                        ["[oversized name]", "unreadable", "", "", "skipped"]
                    ):
                        return
                    truncated = True
                    continue
                link = group.get(name, getlink=True)
                child_path = f"{path.rstrip('/')}/{name}"
                if len(child_path.encode("utf-8")) > request.limits.max_string_bytes:
                    child_path = "[oversized path]"
                    truncated = True
                if isinstance(link, (h5py.ExternalLink, h5py.SoftLink)):
                    if not append_row(
                        [child_path, type(link).__name__, "", "", "not followed"]
                    ):
                        return
                    continue
                try:
                    child = group.get(name, getlink=False)
                except (KeyError, OSError):
                    if not append_row([child_path, "unreadable", "", "", "skipped"]):
                        return
                    continue
                if child is None:
                    if not append_row([child_path, "unreadable", "", "", "skipped"]):
                        return
                    continue
                identity = hash(child.id)
                if identity in visited:
                    if not append_row(
                        [child_path, "hard-link", "", "", "already visited"]
                    ):
                        return
                    continue
                visited.add(identity)
                if isinstance(child, h5py.Group):
                    if not append_row([child_path, "group", "", "", ""]):
                        return
                    locations.append(
                        SourceLocation(
                            kind="group",
                            ordinal=len(locations),
                            label=child_path,
                        )
                    )
                    walk(child, child_path, depth + 1)
                elif isinstance(child, h5py.Dataset):
                    sample, sample_truncated = _safe_dataset_sample(
                        child,
                        max_rows=request.limits.max_rows,
                        max_bytes=max(
                            1,
                            min(
                                request.limits.max_cell_bytes,
                                remaining_output,
                            ),
                        ),
                        max_decoded_bytes=min(
                            request.limits.max_archive_uncompressed_bytes,
                            request.limits.max_output_bytes
                            + request.limits.max_cell_bytes,
                        ),
                    )
                    truncated = truncated or sample_truncated
                    if not append_row(
                        [
                            child_path,
                            "dataset",
                            str(tuple(child.shape)),
                            str(child.dtype),
                            sample,
                        ]
                    ):
                        return
                    locations.append(
                        SourceLocation(
                            kind="dataset", ordinal=len(locations), label=child_path
                        )
                    )

        try:
            with request.source.open_binary() as stream, h5py.File(stream, "r") as root:
                visited.add(hash(root.id))
                walk(root, "/", 0)
        except ConversionError:
            raise
        except (
            AttributeError,
            OSError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise ConversionError(
                "invalid_hdf5", "The HDF5 file is corrupt or unsupported."
            ) from exc

        markdown = _markdown_table(
            [["Path", "Kind", "Shape", "Dtype", "Bounded sample"], *rows]
        )
        markdown, output_truncated = _bounded_text(
            markdown, request.limits.max_output_bytes
        )
        truncated = truncated or output_truncated
        return ConversionResult(
            markdown=markdown,
            metadata={
                "entries_rendered": len(rows),
                "external_and_soft_links_followed": False,
            },
            locations=tuple(locations),
            warnings=_truncation_warning(truncated),
            completeness="truncated" if truncated else "complete",
            quality_score=Decimal("0.950"),
        )


def _safe_dataset_sample(
    dataset,
    *,
    max_rows: int,
    max_bytes: int,
    max_decoded_bytes: int,
) -> tuple[str, bool]:
    try:
        if dataset.size == 0 or dataset.dtype.hasobject:
            return "", False
        if dataset.external or bool(getattr(dataset, "is_virtual", False)):
            return "external/virtual data not read", False
        if not _filters_are_builtin(dataset):
            return "external filter data not read", False
        item_size = int(dataset.dtype.itemsize or 0)
        if item_size <= 0 or item_size > max_bytes:
            return "", item_size > max_bytes
        chunks = dataset.chunks
        if chunks is not None:
            chunk_elements = 1
            for dimension in chunks:
                chunk_elements *= int(dimension)
                if chunk_elements * item_size > max_decoded_bytes:
                    return "chunk exceeds decoded-data limit", True
        if dataset.shape == ():
            if item_size > max_bytes:
                return "", True
            value = dataset[()]
        else:
            sample_rows = min(int(dataset.shape[0]), max_rows)
            if sample_rows <= 0:
                return "", False
            slices = [slice(0, sample_rows)]
            slices.extend(slice(0, 1) for _ in dataset.shape[1:])
            estimated = item_size * sample_rows
            if estimated > max_bytes:
                sample_rows = max(1, max_bytes // item_size)
                slices[0] = slice(0, sample_rows)
                estimated = item_size * sample_rows
            if estimated > max_bytes:
                return "", True
            value = dataset[tuple(slices)]
        rendered, truncated = _bounded_repr(value, max_bytes=max_bytes)
    except (
        AttributeError,
        OSError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return "sample unavailable", False
    return rendered, truncated


def _filters_are_builtin(dataset) -> bool:
    try:
        creation = dataset.id.get_create_plist()
        for index in range(creation.get_nfilters()):
            filter_id = int(creation.get_filter(index)[0])
            if filter_id not in _BUILTIN_HDF5_FILTER_IDS:
                return False
    except (AttributeError, RuntimeError, ValueError):
        return False
    return True


def _bounded_repr(value: Any, *, max_bytes: int) -> tuple[str, bool]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    rendered = repr(value)
    encoded = rendered.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return rendered, False
    suffix = "..." if max_bytes >= 3 else ""
    prefix = encoded[: max(0, max_bytes - len(suffix))].decode("utf-8", errors="ignore")
    return prefix + suffix, True


def _markdown_table(rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(_cell(value) for value in rows[0]) + " |",
        "| " + " | ".join("---" for _ in rows[0]) + " |",
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
            message="The HDF5 metadata reached a configured limit.",
        ),
    )


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not-installed"


__all__ = ["Hdf5Converter"]
