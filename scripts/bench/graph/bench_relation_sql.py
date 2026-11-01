#!/usr/bin/env python3
"""Benchmark the raw SQLite PageLink canonical-head join.

This comparator measures only ``sqlite3.Cursor.execute`` plus ``fetchall``.
It deliberately excludes ContextCitation reads and every service-layer step in
``WorkspaceGraphService._load_relation_universe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.bench.graph._common import (  # noqa: E402
    command_line,
    default_output_path,
    distribution,
    ensure_sqlite_database,
    result_document,
    write_result,
)

Mode = Literal["pinned", "planner", "not-indexed"]

MODES: tuple[Mode, ...] = ("pinned", "planner", "not-indexed")
MIN_GATE_RUNS = 100
MIN_GATE_WARMUP = 10
GRAPH_INDEX = "idx_page_links_graph_head"
GRAPH_INDEX_COLUMNS = (
    "workspace_id",
    "source_page_id",
    "source_version_id",
    "target_page_id",
    "occurrence_count",
    "created_at",
)
DATABASE_FILE_SUFFIXES = {
    "main": "",
    "wal": "-wal",
    "journal": "-journal",
    "shm": "-shm",
}
DURABLE_SIDECAR_ROLES = ("wal", "journal")
MEASUREMENT_CONTRACT = {
    "boundary": "sqlite3.Cursor.execute plus fetchall",
    "covered_query": "PageLink canonical-head join only",
    "excludes": [
        "ContextCitation reads",
        "actor-visible document loading",
        "SQLAlchemy type conversion and ORM hydration",
        "relation deduplication and _Relation construction",
        "relation sorting, graph merge, serialization, and HTTP",
    ],
    "full_relation_universe_covered": False,
    "metric": "execute_fetchall_latency_ms",
}


class BenchmarkInputError(RuntimeError):
    code = "benchmark_input_error"


class MissingRequiredIndexError(BenchmarkInputError):
    code = "missing_required_index"


class InvalidIndexDefinitionError(BenchmarkInputError):
    code = "invalid_index_definition"


class WorkspaceMissingError(BenchmarkInputError):
    code = "workspace_missing"


class QueryExecutionError(BenchmarkInputError):
    code = "query_execution_error"


class CorpusManifestMismatchError(BenchmarkInputError):
    code = "corpus_manifest_mismatch"


class InvalidCorpusManifestError(BenchmarkInputError):
    code = "invalid_corpus_manifest"


class MissingCorpusManifestError(BenchmarkInputError):
    code = "missing_corpus_manifest"


class UncheckpointedDatabaseError(BenchmarkInputError):
    code = "uncheckpointed_database"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument(
        "--mode",
        action="append",
        choices=MODES,
        dest="modes",
        help="mode to measure; repeat to select several (default: all)",
    )
    parser.add_argument(
        "--max-p50-ms",
        type=float,
        help=(
            "optional pinned-mode execute+fetchall p50 target; planner and "
            "not-indexed remain attribution comparators"
        ),
    )
    parser.add_argument(
        "--max-planner-ratio",
        type=float,
        help=(
            "optional pinned-to-planner p50 limit; requires pinned p50 to be "
            "no greater than planner p50 multiplied by this ratio"
        ),
    )
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def _build_gate_eligibility(
    latency_by_mode: Mapping[str, Mapping[str, float]],
    *,
    measured_runs: int,
    warmup_runs: int,
) -> dict[str, object]:
    reasons = []
    if measured_runs < MIN_GATE_RUNS:
        reasons.append(
            f"measured runs {measured_runs} are below required {MIN_GATE_RUNS}"
        )
    if warmup_runs < MIN_GATE_WARMUP:
        reasons.append(
            f"warmup runs {warmup_runs} are below required {MIN_GATE_WARMUP}"
        )
    missing_modes = [mode for mode in MODES if mode not in latency_by_mode]
    if missing_modes:
        reasons.append(f"missing required comparison modes: {', '.join(missing_modes)}")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "required": {
            "measured_runs": MIN_GATE_RUNS,
            "warmup_runs": MIN_GATE_WARMUP,
        },
        "observed": {
            "measured_runs": measured_runs,
            "warmup_runs": warmup_runs,
        },
    }


def build_latency_target(
    latency_by_mode: Mapping[str, Mapping[str, float]],
    *,
    max_p50_ms: float | None,
    measured_runs: int,
    warmup_runs: int,
) -> dict[str, object]:
    target_mode = "pinned"
    if max_p50_ms is not None and target_mode not in latency_by_mode:
        raise ValueError("--max-p50-ms requires --mode pinned")
    eligibility = _build_gate_eligibility(
        latency_by_mode,
        measured_runs=measured_runs,
        warmup_runs=warmup_runs,
    )
    eligible = bool(eligibility["eligible"])
    per_mode = {
        mode: {
            "p50_ms": float(latency["p50"]),
            "role": (
                "latency_target" if mode == target_mode else "attribution_comparator"
            ),
            "passed": (
                None
                if max_p50_ms is None or mode != target_mode or not eligible
                else float(latency["p50"]) <= max_p50_ms
            ),
        }
        for mode, latency in latency_by_mode.items()
    }
    passed = (
        None if max_p50_ms is None or not eligible else per_mode[target_mode]["passed"]
    )
    status = (
        "not_configured"
        if max_p50_ms is None
        else "ineligible"
        if not eligible
        else "passed"
        if passed is True
        else "failed"
    )
    return {
        "configured": max_p50_ms is not None,
        "eligibility": eligibility,
        "max_p50_ms": max_p50_ms,
        "metric": "execute_fetchall_latency_ms.p50",
        "target_mode": target_mode,
        "passed": passed,
        "status": status,
        "per_mode": per_mode,
    }


def build_planner_ratio_target(
    latency_by_mode: Mapping[str, Mapping[str, float]],
    *,
    max_planner_ratio: float | None,
    measured_runs: int,
    warmup_runs: int,
) -> dict[str, object]:
    target_mode = "pinned"
    comparator_mode = "planner"
    required_modes = (target_mode, comparator_mode)
    if max_planner_ratio is not None and any(
        mode not in latency_by_mode for mode in required_modes
    ):
        raise ValueError(
            "--max-planner-ratio requires --mode pinned and --mode planner"
        )
    eligibility = _build_gate_eligibility(
        latency_by_mode,
        measured_runs=measured_runs,
        warmup_runs=warmup_runs,
    )
    eligible = bool(eligibility["eligible"])
    pinned_p50_ms = (
        float(latency_by_mode[target_mode]["p50"])
        if target_mode in latency_by_mode
        else None
    )
    planner_p50_ms = (
        float(latency_by_mode[comparator_mode]["p50"])
        if comparator_mode in latency_by_mode
        else None
    )
    allowed_pinned_p50_ms = (
        planner_p50_ms * max_planner_ratio
        if planner_p50_ms is not None and max_planner_ratio is not None
        else None
    )
    observed_ratio = (
        pinned_p50_ms / planner_p50_ms
        if pinned_p50_ms is not None
        and planner_p50_ms is not None
        and planner_p50_ms > 0
        else None
    )
    passed = (
        None
        if max_planner_ratio is None or not eligible
        else pinned_p50_ms <= allowed_pinned_p50_ms
    )
    status = (
        "not_configured"
        if max_planner_ratio is None
        else "ineligible"
        if not eligible
        else "passed"
        if passed is True
        else "failed"
    )
    return {
        "configured": max_planner_ratio is not None,
        "eligibility": eligibility,
        "max_planner_ratio": max_planner_ratio,
        "metric": "execute_fetchall_latency_ms.p50",
        "target_mode": target_mode,
        "comparator_mode": comparator_mode,
        "formula": "pinned_p50_ms <= planner_p50_ms * max_planner_ratio",
        "observed": {
            "pinned_p50_ms": pinned_p50_ms,
            "planner_p50_ms": planner_p50_ms,
            "pinned_to_planner_ratio": observed_ratio,
            "allowed_pinned_p50_ms": allowed_pinned_p50_ms,
        },
        "passed": passed,
        "status": status,
    }


def _page_link_sql(mode: Mode) -> str:
    table_reference = {
        "pinned": f"page_links INDEXED BY {GRAPH_INDEX}",
        "planner": "page_links",
        "not-indexed": "page_links NOT INDEXED",
    }[mode]
    return f"""
SELECT
    page_links.source_page_id,
    page_links.source_version_id,
    page_links.target_page_id,
    page_links.occurrence_count,
    page_links.created_at
FROM {table_reference}
JOIN context_page
  ON context_page.id = page_links.source_page_id
 AND context_page.workspace_id = page_links.workspace_id
 AND context_page.current_published_version_id = page_links.source_version_id
WHERE page_links.workspace_id = ?
  AND context_page.workspace_id = ?
  AND context_page.lifecycle_status != ?
  AND page_links.target_page_id IS NOT NULL
""".strip()


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_workspace(
    connection: sqlite3.Connection,
    workspace_id: UUID,
) -> None:
    row = connection.execute(
        "SELECT 1 FROM workspace WHERE id IN (?, ?) LIMIT 1",
        (workspace_id.hex, str(workspace_id)),
    ).fetchone()
    if row is None:
        raise WorkspaceMissingError(
            f"workspace is absent from benchmark database: {workspace_id}"
        )


def _validate_pinned_index(connection: sqlite3.Connection) -> None:
    columns = tuple(
        str(row[2]) for row in connection.execute(f"PRAGMA index_info({GRAPH_INDEX})")
    )
    if not columns:
        raise MissingRequiredIndexError(
            f"pinned mode requires SQLite index {GRAPH_INDEX}; "
            "apply the current migration before benchmarking"
        )
    if columns != GRAPH_INDEX_COLUMNS:
        raise InvalidIndexDefinitionError(
            f"{GRAPH_INDEX} columns differ from the production contract: "
            f"expected {GRAPH_INDEX_COLUMNS!r}, found {columns!r}"
        )


def _quote_sqlite_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _planner_provenance(connection: sqlite3.Connection) -> dict[str, object]:
    stat1_present = (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'sqlite_stat1'"
        ).fetchone()
        is not None
    )
    stat1_row_count = (
        int(connection.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0])
        if stat1_present
        else 0
    )
    page_link_indexes = []
    for row in connection.execute("PRAGMA index_list(page_links)"):
        name = str(row[1])
        columns = [
            column[2]
            for column in connection.execute(
                f"PRAGMA index_info({_quote_sqlite_identifier(name)})"
            )
        ]
        page_link_indexes.append(
            {
                "name": name,
                "columns": columns,
                "unique": bool(row[2]),
                "origin": str(row[3]),
                "partial": bool(row[4]),
            }
        )
    return {
        "sqlite_stat1": {
            "present": stat1_present,
            "row_count": stat1_row_count,
        },
        "page_links_indexes": sorted(
            page_link_indexes,
            key=lambda index: str(index["name"]),
        ),
    }


def _explain_query_plan(
    connection: sqlite3.Connection,
    *,
    sql: str,
    parameters: tuple[str, str, str],
) -> list[dict[str, object]]:
    return [
        {
            "select_id": int(row[0]),
            "parent_id": int(row[1]),
            "not_used": int(row[2]),
            "detail": str(row[3]),
        }
        for row in connection.execute(
            f"EXPLAIN QUERY PLAN {sql}",
            parameters,
        )
    ]


def _row_sha256(rows: Sequence[tuple[object, ...]]) -> str:
    digest = hashlib.sha256()
    encoded_rows = sorted(
        json.dumps(
            row,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        for row in rows
    )
    for encoded in encoded_rows:
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _execute_fetchall(
    connection: sqlite3.Connection,
    *,
    mode: Mode,
    sql: str,
    parameters: tuple[str, str, str],
    phase: str,
    index: int,
) -> tuple[float, list[tuple[object, ...]]]:
    cursor = connection.cursor()
    started = time.perf_counter()
    try:
        cursor.execute(sql, parameters)
        rows = cursor.fetchall()
    except sqlite3.Error as exc:
        raise QueryExecutionError(
            f"{mode} query failed during {phase} sample {index}: {exc}"
        ) from exc
    finally:
        cursor.close()
    return (time.perf_counter() - started) * 1_000, rows


def _rounded_distribution(values: Sequence[float]) -> dict[str, float]:
    measured = distribution(values)
    if measured is None:
        raise ValueError("at least one measured sample is required")
    return {name: round(float(value), 6) for name, value in measured.items()}


def _run_mode(
    connection: sqlite3.Connection,
    *,
    mode: Mode,
    parameters: tuple[str, str, str],
    runs: int,
    warmup: int,
) -> dict[str, object]:
    sql = _page_link_sql(mode)
    explain = _explain_query_plan(
        connection,
        sql=sql,
        parameters=parameters,
    )
    explain_details = [str(row["detail"]) for row in explain]
    warmup_samples: list[dict[str, object]] = []
    measured_samples: list[dict[str, object]] = []
    observed_counts: set[int] = set()
    observed_hashes: set[str] = set()

    for phase, attempts, samples in (
        ("warmup", warmup, warmup_samples),
        ("measured", runs, measured_samples),
    ):
        for index in range(attempts):
            elapsed_ms, rows = _execute_fetchall(
                connection,
                mode=mode,
                sql=sql,
                parameters=parameters,
                phase=phase,
                index=index,
            )
            row_count = len(rows)
            row_sha256 = _row_sha256(rows)
            observed_counts.add(row_count)
            observed_hashes.add(row_sha256)
            samples.append(
                {
                    "index": index,
                    "elapsed_ms": round(elapsed_ms, 6),
                    "row_count": row_count,
                }
            )

    if len(observed_counts) != 1 or len(observed_hashes) != 1:
        raise QueryExecutionError(
            f"{mode} returned unstable rows across warmup and measured samples"
        )
    row_count = next(iter(observed_counts))
    row_sha256 = next(iter(observed_hashes))
    latency = _rounded_distribution(
        [float(sample["elapsed_ms"]) for sample in measured_samples]
    )
    warmup_latency = (
        _rounded_distribution(
            [float(sample["elapsed_ms"]) for sample in warmup_samples]
        )
        if warmup_samples
        else None
    )
    return {
        "sql": sql,
        "index_directive": {
            "pinned": f"INDEXED BY {GRAPH_INDEX}",
            "planner": None,
            "not-indexed": "NOT INDEXED",
        }[mode],
        "explain_query_plan": explain,
        "explain_summary": {
            "automatic_index_details": [
                detail for detail in explain_details if "AUTOMATIC" in detail.upper()
            ],
            "scan_details": [
                detail for detail in explain_details if "SCAN " in detail.upper()
            ],
            "uses_graph_covering_index": any(
                f"USING COVERING INDEX {GRAPH_INDEX}" in detail
                for detail in explain_details
            ),
        },
        "row_count": row_count,
        "row_sha256": row_sha256,
        "execute_fetchall_latency_ms": latency,
        "warmup_execute_fetchall_latency_ms": warmup_latency,
        "samples": measured_samples,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_file_sizes(database_path: Path) -> dict[str, dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    for role, suffix in DATABASE_FILE_SUFFIXES.items():
        path = Path(f"{database_path}{suffix}")
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            files[role] = {
                "path": str(path),
                "exists": False,
                "bytes": 0,
            }
        else:
            files[role] = {
                "path": str(path),
                "exists": True,
                "bytes": size,
            }
    return files


def _non_empty_durable_sidecars(
    files: Mapping[str, Mapping[str, object]],
) -> list[str]:
    return [
        str(files[role]["path"])
        for role in DURABLE_SIDECAR_ROLES
        if int(files[role]["bytes"]) > 0
    ]


def _validate_checkpointed_database(
    files: Mapping[str, Mapping[str, object]],
) -> None:
    non_empty_sidecars = _non_empty_durable_sidecars(files)
    if non_empty_sidecars:
        raise UncheckpointedDatabaseError(
            "refusing a database with a non-empty SQLite WAL or journal: "
            + ", ".join(non_empty_sidecars)
        )


def _manifest_provenance(
    database_path: Path,
    *,
    workspace_id: UUID,
    database_sha256: str,
) -> dict[str, object]:
    manifest_path = Path(f"{database_path}.manifest.json")
    if not manifest_path.exists():
        return {
            "manifest": None,
            "manifest_status": "absent",
            "arguments_status": "unknown",
            "source": {"kind": "unmanifested_database"},
            "workspace": None,
            "workspace_listed": None,
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise InvalidCorpusManifestError(
            f"corpus manifest must contain one object: {manifest_path}"
        )
    arguments = manifest.get("arguments")
    if not isinstance(arguments, Mapping):
        raise InvalidCorpusManifestError(
            f"corpus manifest has no arguments object: {manifest_path}"
        )
    generator_version = manifest.get("generator_version")
    if not isinstance(generator_version, str) or not generator_version:
        raise InvalidCorpusManifestError(
            f"corpus manifest has no generator_version: {manifest_path}"
        )
    workspaces = manifest.get("workspaces")
    if not isinstance(workspaces, list) or not all(
        isinstance(item, Mapping) for item in workspaces
    ):
        raise InvalidCorpusManifestError(
            f"corpus manifest workspaces must be an array of objects: {manifest_path}"
        )
    database = manifest.get("database")
    declared_sha256 = database.get("sha256") if isinstance(database, Mapping) else None
    if (
        not isinstance(declared_sha256, str)
        or len(declared_sha256) != 64
        or any(
            character not in "0123456789abcdefABCDEF" for character in declared_sha256
        )
    ):
        raise InvalidCorpusManifestError(
            f"corpus manifest has no valid database SHA-256: {manifest_path}"
        )
    declared_sha256 = declared_sha256.casefold()
    workspace = next(
        (
            item
            for item in workspaces
            if isinstance(item, Mapping) and item.get("id") == str(workspace_id)
        ),
        None,
    )
    return {
        "manifest": str(manifest_path),
        "manifest_status": "present",
        "manifest_sha256": _file_sha256(manifest_path),
        "manifest_version": manifest.get("manifest_version"),
        "generator_version": generator_version,
        "arguments": dict(arguments),
        "arguments_status": "manifest",
        "application": manifest.get("application"),
        "source": manifest.get("source"),
        "workspace": workspace,
        "workspace_listed": workspace is not None,
        "declared_database_sha256": declared_sha256,
        "declared_database_sha256_matches": declared_sha256 == database_sha256,
    }


def _validate_manifest_provenance(
    corpus: Mapping[str, object],
    *,
    required_for_gate: bool,
) -> None:
    if corpus.get("manifest_status") != "present":
        if required_for_gate:
            raise MissingCorpusManifestError(
                "configured latency gates require a valid companion manifest"
            )
        return
    problems: list[str] = []
    if corpus.get("workspace_listed") is not True:
        problems.append("workspace is absent from the companion manifest")
    if corpus.get("declared_database_sha256_matches") is not True:
        problems.append("database SHA-256 differs from the companion manifest")
    if problems:
        raise CorpusManifestMismatchError("; ".join(problems))


def _build_run_integrity(
    mode_results: Mapping[str, Mapping[str, object]],
    *,
    expected_runs: int,
    expected_warmup: int,
    database_unchanged: bool,
    durable_sidecars_empty_after: bool,
) -> dict[str, object]:
    row_counts = {int(details["row_count"]) for details in mode_results.values()}
    row_hashes = {str(details["row_sha256"]) for details in mode_results.values()}
    shared_row_count = next(iter(row_counts)) if len(row_counts) == 1 else None
    samples_complete = all(
        len(details["samples"]) == expected_runs for details in mode_results.values()
    )
    return {
        "database_unchanged": database_unchanged,
        "durable_sidecars_empty_before": True,
        "durable_sidecars_empty_after": durable_sidecars_empty_after,
        "expected_measured_runs_per_mode": expected_runs,
        "expected_warmup_runs_per_mode": expected_warmup,
        "measured_runs_per_mode": {
            mode: len(details["samples"]) for mode, details in mode_results.items()
        },
        "modes": list(mode_results),
        "passed": (
            database_unchanged
            and durable_sidecars_empty_after
            and len(row_counts) == 1
            and len(row_hashes) == 1
            and shared_row_count is not None
            and shared_row_count > 0
            and samples_complete
        ),
        "row_count_equal": len(row_counts) == 1,
        "row_identity_equal": len(row_hashes) == 1,
        "non_empty_result": shared_row_count is not None and shared_row_count > 0,
        "shared_row_count": shared_row_count,
    }


def run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")
    if args.max_p50_ms is not None and (
        not math.isfinite(args.max_p50_ms) or args.max_p50_ms <= 0
    ):
        raise ValueError("--max-p50-ms must be a finite positive number")
    if args.max_planner_ratio is not None and (
        not math.isfinite(args.max_planner_ratio) or args.max_planner_ratio <= 0
    ):
        raise ValueError("--max-planner-ratio must be a finite positive number")

    database_path = args.db.expanduser().resolve()
    modes = tuple(dict.fromkeys(args.modes or MODES))
    if args.max_p50_ms is not None and "pinned" not in modes:
        raise ValueError("--max-p50-ms requires --mode pinned")
    if args.max_planner_ratio is not None and any(
        mode not in modes for mode in ("pinned", "planner")
    ):
        raise ValueError(
            "--max-planner-ratio requires --mode pinned and --mode planner"
        )
    gate_configured = args.max_p50_ms is not None or args.max_planner_ratio is not None
    database_files_before = _database_file_sizes(database_path)
    _validate_checkpointed_database(database_files_before)
    database_path = ensure_sqlite_database(database_path)
    sha256_before = _file_sha256(database_path)
    corpus = _manifest_provenance(
        database_path,
        workspace_id=args.workspace_id,
        database_sha256=sha256_before,
    )
    _validate_manifest_provenance(
        corpus,
        required_for_gate=gate_configured,
    )
    parameters = (
        args.workspace_id.hex,
        args.workspace_id.hex,
        "archived",
    )

    with _open_read_only(database_path) as connection:
        query_only = bool(connection.execute("PRAGMA query_only").fetchone()[0])
        _validate_workspace(connection, args.workspace_id)
        if "pinned" in modes:
            _validate_pinned_index(connection)
        planner_provenance = _planner_provenance(connection)
        mode_results = {
            mode: _run_mode(
                connection,
                mode=mode,
                parameters=parameters,
                runs=args.runs,
                warmup=args.warmup,
            )
            for mode in modes
        }

    sha256_after = _file_sha256(database_path)
    database_files_after = _database_file_sizes(database_path)
    database_unchanged = sha256_before == sha256_after
    durable_sidecars_empty_after = not _non_empty_durable_sidecars(database_files_after)
    run_integrity = _build_run_integrity(
        mode_results,
        expected_runs=args.runs,
        expected_warmup=args.warmup,
        database_unchanged=database_unchanged,
        durable_sidecars_empty_after=durable_sidecars_empty_after,
    )
    latency_by_mode = {
        mode: details["execute_fetchall_latency_ms"]
        for mode, details in mode_results.items()
    }
    latency_target = build_latency_target(
        latency_by_mode,
        max_p50_ms=args.max_p50_ms,
        measured_runs=args.runs,
        warmup_runs=args.warmup,
    )
    planner_ratio_target = build_planner_ratio_target(
        latency_by_mode,
        max_planner_ratio=args.max_planner_ratio,
        measured_runs=args.runs,
        warmup_runs=args.warmup,
    )
    result = result_document(
        tool="bench_relation_sql",
        database_path=database_path,
        workspace_id=args.workspace_id,
        corpus=corpus,
        body={
            "command": command_line(),
            "parameters": {
                "modes": list(modes),
                "runs": args.runs,
                "warmup": args.warmup,
                "max_p50_ms": args.max_p50_ms,
                "max_planner_ratio": args.max_planner_ratio,
            },
            "measurement_contract": MEASUREMENT_CONTRACT,
            "runner_provenance": {
                "runner_sha256": _file_sha256(Path(__file__).resolve()),
                "sqlite_runtime_version": sqlite3.sqlite_version,
                "sqlite_runtime_version_info": list(sqlite3.sqlite_version_info),
            },
            "database_provenance": {
                "access": {
                    "mode": "read_only",
                    "query_only_pragma": query_only,
                    "sqlite_uri_mode": "ro",
                },
                "bytes": database_files_before["main"]["bytes"],
                "file_sizes": {
                    "before": database_files_before,
                    "after": database_files_after,
                },
                "sha256_before": sha256_before,
                "sha256_after": sha256_after,
                "unchanged": database_unchanged,
            },
            "planner_provenance": planner_provenance,
            "query_contract": {
                "selected_columns": list(GRAPH_INDEX_COLUMNS[1:]),
                "head_join": (
                    "context_page.id = page_links.source_page_id AND "
                    "context_page.workspace_id = page_links.workspace_id AND "
                    "context_page.current_published_version_id = "
                    "page_links.source_version_id"
                ),
                "pinned_index": GRAPH_INDEX,
                "pinned_index_columns": list(GRAPH_INDEX_COLUMNS),
                "historical_baseline_differences": [
                    "selects source_version_id in addition to the historical "
                    "four columns",
                    "enforces the ContextPage workspace predicate",
                    "excludes archived ContextPage rows",
                ],
            },
            "modes": mode_results,
            "run_integrity": run_integrity,
            "latency_target": latency_target,
            "planner_ratio_target": planner_ratio_target,
        },
    )
    exit_code = (
        2
        if (
            not run_integrity["passed"]
            or (
                latency_target["configured"]
                and not latency_target["eligibility"]["eligible"]
            )
            or (
                planner_ratio_target["configured"]
                and not planner_ratio_target["eligibility"]["eligible"]
            )
        )
        else 3
        if latency_target["passed"] is False or planner_ratio_target["passed"] is False
        else 0
    )
    return result, exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result, exit_code = run(args)
    output_path = (args.out or default_output_path("relation-sql")).resolve()
    write_result(output_path, result)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "run_integrity": result["run_integrity"],
                "latency_target": result["latency_target"],
                "planner_ratio_target": result["planner_ratio_target"],
                "modes": {
                    mode: details["execute_fetchall_latency_ms"]
                    for mode, details in result["modes"].items()
                },
            },
            ensure_ascii=False,
        )
    )
    return exit_code


def cli(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except BenchmarkInputError as exc:
        code = exc.code
        message = str(exc)
    except FileExistsError as exc:
        code = "output_exists"
        message = str(exc)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        code = "invalid_benchmark_input"
        message = f"{type(exc).__name__}: {exc}"
    print(
        json.dumps(
            {
                "status": "error",
                "code": code,
                "message": message,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
