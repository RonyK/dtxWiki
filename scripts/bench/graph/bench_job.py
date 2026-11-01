#!/usr/bin/env python3
"""Benchmark the F16 regroup job and concurrent publication commit wait.

F00 lands before the F16 storage and worker contracts exist.  Until those
contracts are present this runner emits a structured skip instead of guessing
at a service name or accidentally benchmarking an unrelated job path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.bench.graph._common import (  # noqa: E402
    cold_measurement_contract,
    command_line,
    default_output_path,
    ensure_sqlite_database,
    load_corpus_metadata,
    result_document,
    write_result,
)

F16_MODULE = "backend.services.workspace_graph_grouping"
F16_REQUIRED_TABLES = (
    "workspace_graph_generation",
    "workspace_graph_grouping_state",
    "workspace_graph_group",
    "workspace_graph_group_member",
    "workspace_graph_group_edge",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument(
        "--actor",
        choices=("viewer", "owner", "restricted_reader"),
        default="owner",
    )
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--concurrency", type=int, choices=(1, 4), default=1)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--cold-runs",
        type=int,
        default=0,
        help="reserved for the F16 adapter; must be 0 or at least 20",
    )
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    cold = cold_measurement_contract(args.cold_runs)
    database_path = ensure_sqlite_database(args.db)
    _require_workspace(database_path, args.workspace_id)
    corpus = load_corpus_metadata(
        database_path,
        workspace_id=args.workspace_id,
    )
    readiness = _f16_readiness(database_path)
    reason = (
        "f16_grouping_contract_not_available"
        if not readiness["ready"]
        else "f16_benchmark_adapter_not_implemented"
    )
    result = result_document(
        tool="bench_job",
        database_path=database_path,
        workspace_id=args.workspace_id,
        corpus=corpus,
        body={
            "command": command_line(),
            "actor": args.actor,
            "parameters": {
                "runs": args.runs,
                "warmup": args.warmup,
                "concurrency": args.concurrency,
                "timeout_seconds": args.timeout_seconds,
                "cold_runs": args.cold_runs,
            },
            "status": "skipped",
            "skip_reason": reason,
            "phase_dependency": "F16",
            "readiness": readiness,
            "measurements": {
                "compute_ms": None,
                "store_ms": None,
                "publish_commit_wait_ms": None,
                "completed": 0,
                "errors": 0,
                "timeouts": 0,
                "failure_rate": None,
                "reason": reason,
            },
            "measurement_contract": {
                "job": (
                    "Run the real workspace_graph_regroup worker path and report "
                    "calculation and chunked storage separately."
                ),
                "publish_wait": (
                    "Start one real Context publication after grouping calculation "
                    "begins and before storage completes; time its database commit "
                    "wait without modifying the source corpus in place."
                ),
                "database_copy_required": True,
                "note": (
                    "F16 must replace this explicit skip with a typed adapter to "
                    "the final worker/service contract; reflection or guessed "
                    "private calls are intentionally rejected."
                ),
            },
            "cold": {
                **cold,
                "status": "skipped",
                "reason": reason,
            },
        },
    )
    output_path = (args.out or default_output_path("job")).resolve()
    write_result(output_path, result)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "status": "skipped",
                "reason": reason,
                "readiness": readiness,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _require_workspace(database_path: Path, workspace_id: UUID) -> None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM workspace WHERE id IN (?, ?) LIMIT 1",
            (workspace_id.hex, str(workspace_id)),
        ).fetchone()
    if row is None:
        raise ValueError(f"workspace is absent from benchmark database: {workspace_id}")


def _f16_readiness(database_path: Path) -> dict[str, object]:
    module_available = importlib.util.find_spec(F16_MODULE) is not None
    with sqlite3.connect(database_path) as connection:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
    missing_tables = sorted(set(F16_REQUIRED_TABLES) - existing_tables)
    return {
        "ready": module_available and not missing_tables,
        "module": F16_MODULE,
        "module_available": module_available,
        "required_tables": list(F16_REQUIRED_TABLES),
        "missing_tables": missing_tables,
    }


if __name__ == "__main__":
    raise SystemExit(main())
