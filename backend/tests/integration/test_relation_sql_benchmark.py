from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from backend.tests.utils.environment import child_env_without_dtxwiki_config
from scripts.bench.graph.bench_relation_sql import (
    _row_sha256,
    build_latency_target,
    build_planner_ratio_target,
)

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("22000000-0000-0000-0000-000000000001")


def _database_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_manifest(path: Path) -> None:
    Path(f"{path}.manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": "test",
                "generator_version": "test",
                "arguments": {},
                "database": {"sha256": _database_sha256(path)},
                "workspaces": [{"id": str(WORKSPACE_ID)}],
            }
        ),
        encoding="utf-8",
    )


def _create_page_link_database(
    path: Path,
    *,
    include_graph_index: bool = True,
) -> None:
    page_a = UUID("22000000-0000-0000-0000-000000000010")
    page_b = UUID("22000000-0000-0000-0000-000000000011")
    version_a = UUID("22000000-0000-0000-0000-000000000020")
    version_b = UUID("22000000-0000-0000-0000-000000000021")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE workspace (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL
            );
            CREATE TABLE context_page (
                workspace_id TEXT NOT NULL,
                id TEXT NOT NULL,
                current_published_version_id TEXT,
                lifecycle_status TEXT NOT NULL,
                PRIMARY KEY (workspace_id, id)
            );
            CREATE TABLE page_links (
                workspace_id TEXT NOT NULL,
                source_page_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL,
                target_page_id TEXT,
                occurrence_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        if include_graph_index:
            connection.execute(
                "CREATE INDEX idx_page_links_graph_head ON page_links "
                "(workspace_id, source_page_id, source_version_id, "
                "target_page_id, occurrence_count, created_at)"
            )
        connection.execute(
            "INSERT INTO workspace (id, slug) VALUES (?, ?)",
            (WORKSPACE_ID.hex, "relation-sql"),
        )
        connection.executemany(
            "INSERT INTO context_page "
            "(workspace_id, id, current_published_version_id, lifecycle_status) "
            "VALUES (?, ?, ?, ?)",
            (
                (WORKSPACE_ID.hex, page_a.hex, version_a.hex, "published"),
                (WORKSPACE_ID.hex, page_b.hex, version_b.hex, "published"),
            ),
        )
        connection.executemany(
            "INSERT INTO page_links "
            "(workspace_id, source_page_id, source_version_id, target_page_id, "
            "occurrence_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    WORKSPACE_ID.hex,
                    page_a.hex,
                    version_a.hex,
                    page_b.hex,
                    1,
                    "2026-09-20 00:00:00.000000",
                ),
                (
                    WORKSPACE_ID.hex,
                    page_b.hex,
                    version_b.hex,
                    page_a.hex,
                    2,
                    "2026-09-20 00:00:01.000000",
                ),
            ),
        )


def _run_cli(
    database_path: Path,
    output_path: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    repository_root = Path(__file__).resolve().parents[3]
    environment = child_env_without_dtxwiki_config()
    environment["PYTHONHASHSEED"] = "0"
    return subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts/bench/graph/bench_relation_sql.py"),
            "--db",
            str(database_path),
            "--workspace-id",
            str(WORKSPACE_ID),
            "--runs",
            "2",
            "--warmup",
            "1",
            "--out",
            str(output_path),
            *extra,
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_relation_sql_cli_records_exact_scope_and_equal_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "relations.sqlite"
    output_path = tmp_path / "result.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)
    original_sha256 = _database_sha256(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--runs",
        "100",
        "--warmup",
        "10",
        "--max-p50-ms",
        "1000",
        "--max-planner-ratio",
        "1000000",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["tool"] == "bench_relation_sql"
    assert result["environment"]["commit"]
    assert result["measurement_contract"] == {
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
    assert result["database_provenance"]["access"] == {
        "mode": "read_only",
        "query_only_pragma": True,
        "sqlite_uri_mode": "ro",
    }
    assert result["database_provenance"]["sha256_before"] == original_sha256
    assert result["database_provenance"]["sha256_after"] == original_sha256
    assert result["database_provenance"]["unchanged"] is True
    file_sizes = result["database_provenance"]["file_sizes"]
    assert file_sizes["before"]["main"]["bytes"] == database_path.stat().st_size
    assert file_sizes["after"]["main"]["bytes"] == database_path.stat().st_size
    assert all(
        file_sizes[phase][role]["bytes"] == 0
        for phase in ("before", "after")
        for role in ("wal", "journal", "shm")
    )
    assert _database_sha256(database_path) == original_sha256
    assert result["runner_provenance"]["runner_sha256"]
    assert result["runner_provenance"]["sqlite_runtime_version"]

    modes = result["modes"]
    assert result["parameters"]["modes"] == [
        "pinned",
        "planner",
        "not-indexed",
    ]
    assert result["parameters"]["max_planner_ratio"] == 1000000.0
    assert set(modes) == {"pinned", "planner", "not-indexed"}
    assert {details["row_count"] for details in modes.values()} == {2}
    assert len({details["row_sha256"] for details in modes.values()}) == 1
    assert all(details["samples"] for details in modes.values())
    assert any(
        "USING COVERING INDEX idx_page_links_graph_head" in row["detail"]
        for row in modes["pinned"]["explain_query_plan"]
    )
    assert any(
        "SCAN page_links" in row["detail"]
        for row in modes["not-indexed"]["explain_query_plan"]
    )
    assert modes["pinned"]["explain_summary"] == {
        "automatic_index_details": [],
        "scan_details": [],
        "uses_graph_covering_index": True,
    }
    assert modes["not-indexed"]["explain_summary"]["uses_graph_covering_index"] is (
        False
    )
    assert result["run_integrity"]["passed"] is True
    assert result["run_integrity"]["durable_sidecars_empty_before"] is True
    assert result["run_integrity"]["durable_sidecars_empty_after"] is True
    assert result["run_integrity"]["row_count_equal"] is True
    assert result["run_integrity"]["row_identity_equal"] is True
    assert result["run_integrity"]["non_empty_result"] is True
    assert result["latency_target"]["configured"] is True
    assert result["latency_target"]["eligibility"]["eligible"] is True
    assert result["latency_target"]["metric"] == ("execute_fetchall_latency_ms.p50")
    assert result["latency_target"]["target_mode"] == "pinned"
    assert result["latency_target"]["passed"] is True
    planner_ratio_target = result["planner_ratio_target"]
    assert planner_ratio_target["configured"] is True
    assert planner_ratio_target["eligibility"]["eligible"] is True
    assert planner_ratio_target["max_planner_ratio"] == 1000000.0
    assert planner_ratio_target["target_mode"] == "pinned"
    assert planner_ratio_target["comparator_mode"] == "planner"
    assert planner_ratio_target["formula"] == (
        "pinned_p50_ms <= planner_p50_ms * max_planner_ratio"
    )
    assert (
        planner_ratio_target["observed"]["pinned_p50_ms"]
        == (modes["pinned"]["execute_fetchall_latency_ms"]["p50"])
    )
    assert (
        planner_ratio_target["observed"]["planner_p50_ms"]
        == (modes["planner"]["execute_fetchall_latency_ms"]["p50"])
    )
    assert planner_ratio_target["observed"]["pinned_to_planner_ratio"] == (
        planner_ratio_target["observed"]["pinned_p50_ms"]
        / planner_ratio_target["observed"]["planner_p50_ms"]
    )
    assert planner_ratio_target["passed"] is True
    assert result["corpus"]["manifest_status"] == "present"
    assert result["planner_provenance"]["sqlite_stat1"] == {
        "present": False,
        "row_count": 0,
    }
    assert result["planner_provenance"]["page_links_indexes"] == [
        {
            "name": "idx_page_links_graph_head",
            "columns": [
                "workspace_id",
                "source_page_id",
                "source_version_id",
                "target_page_id",
                "occurrence_count",
                "created_at",
            ],
            "unique": False,
            "origin": "c",
            "partial": False,
        }
    ]


def test_relation_sql_cli_allows_unmanifested_diagnostic_without_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "diagnostic.sqlite"
    output_path = tmp_path / "diagnostic.json"
    _create_page_link_database(database_path)

    completed = _run_cli(database_path, output_path, "--mode", "planner")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["corpus"]["manifest_status"] == "absent"
    assert result["latency_target"]["configured"] is False
    assert result["planner_ratio_target"]["configured"] is False


@pytest.mark.parametrize(
    "gate_args",
    [
        ("--max-p50-ms", "1000"),
        ("--max-planner-ratio", "1.3"),
    ],
)
def test_relation_sql_cli_requires_manifest_for_configured_gate(
    tmp_path: Path,
    gate_args: tuple[str, str],
) -> None:
    database_path = tmp_path / "missing-manifest.sqlite"
    output_path = tmp_path / "missing-manifest.json"
    _create_page_link_database(database_path)

    completed = _run_cli(database_path, output_path, *gate_args)

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error == {
        "status": "error",
        "code": "missing_corpus_manifest",
        "message": "configured latency gates require a valid companion manifest",
    }


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_relation_sql_cli_rejects_non_empty_durable_sidecar_before_open(
    tmp_path: Path,
    suffix: str,
) -> None:
    database_path = tmp_path / "uncheckpointed.sqlite"
    output_path = tmp_path / "uncheckpointed.json"
    _create_page_link_database(database_path)
    sidecar = Path(f"{database_path}{suffix}")
    sidecar.write_bytes(b"uncheckpointed")
    before = {
        database_path: _database_sha256(database_path),
        sidecar: _database_sha256(sidecar),
    }

    completed = _run_cli(database_path, output_path, "--mode", "planner")

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "uncheckpointed_database"
    assert str(sidecar.resolve()) in error["message"]
    assert {
        database_path: _database_sha256(database_path),
        sidecar: _database_sha256(sidecar),
    } == before


def test_relation_sql_cli_records_analyzed_planner_provenance(tmp_path: Path) -> None:
    database_path = tmp_path / "analyzed.sqlite"
    output_path = tmp_path / "analyzed.json"
    _create_page_link_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("ANALYZE")
    Path(f"{database_path}-wal").touch()

    completed = _run_cli(database_path, output_path, "--mode", "planner")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["planner_provenance"]["sqlite_stat1"]["present"] is True
    assert result["planner_provenance"]["sqlite_stat1"]["row_count"] >= 1
    assert result["database_provenance"]["file_sizes"]["before"]["wal"] == {
        "path": str(Path(f"{database_path}-wal").resolve()),
        "exists": True,
        "bytes": 0,
    }
    assert result["run_integrity"]["durable_sidecars_empty_after"] is True


def test_relation_sql_cli_fails_fast_when_pinned_index_is_missing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing-index.sqlite"
    output_path = tmp_path / "result.json"
    _create_page_link_database(database_path, include_graph_index=False)

    completed = _run_cli(database_path, output_path, "--mode", "pinned")

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["status"] == "error"
    assert error["code"] == "missing_required_index"
    assert "idx_page_links_graph_head" in error["message"]


def test_relation_sql_cli_rejects_target_without_pinned_before_query(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "empty.sqlite"
    output_path = tmp_path / "result.json"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")

    completed = _run_cli(
        database_path,
        output_path,
        "--mode",
        "planner",
        "--max-p50-ms",
        "1000",
    )

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "invalid_benchmark_input"
    assert error["message"].endswith("--max-p50-ms requires --mode pinned")


@pytest.mark.parametrize("mode", ["pinned", "planner"])
def test_relation_sql_cli_rejects_planner_ratio_without_required_modes_before_query(
    tmp_path: Path,
    mode: str,
) -> None:
    database_path = tmp_path / f"ratio-{mode}.sqlite"
    output_path = tmp_path / f"ratio-{mode}.json"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")

    completed = _run_cli(
        database_path,
        output_path,
        "--mode",
        mode,
        "--max-planner-ratio",
        "1.3",
    )

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "invalid_benchmark_input"
    assert error["message"].endswith(
        "--max-planner-ratio requires --mode pinned and --mode planner"
    )


@pytest.mark.parametrize("corruption", ["database_sha256", "workspace_entry"])
def test_relation_sql_cli_rejects_stale_companion_manifest(
    tmp_path: Path,
    corruption: str,
) -> None:
    database_path = tmp_path / f"manifest-{corruption}.sqlite"
    output_path = tmp_path / f"manifest-{corruption}.json"
    _create_page_link_database(database_path)
    database_sha256 = _database_sha256(database_path)
    manifest = {
        "manifest_version": "test",
        "generator_version": "test",
        "arguments": {},
        "database": {
            "sha256": ("0" * 64 if corruption == "database_sha256" else database_sha256)
        },
        "workspaces": (
            [] if corruption == "workspace_entry" else [{"id": str(WORKSPACE_ID)}]
        ),
    }
    Path(f"{database_path}.manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    completed = _run_cli(database_path, output_path, "--mode", "planner")

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "corpus_manifest_mismatch"
    assert "companion manifest" in error["message"]


@pytest.mark.parametrize(
    "malformed_field", ["arguments", "generator_version", "workspaces"]
)
def test_relation_sql_cli_rejects_malformed_companion_manifest(
    tmp_path: Path,
    malformed_field: str,
) -> None:
    database_path = tmp_path / f"malformed-{malformed_field}.sqlite"
    output_path = tmp_path / f"malformed-{malformed_field}.json"
    _create_page_link_database(database_path)
    manifest = {
        "manifest_version": "test",
        "generator_version": "test",
        "arguments": {},
        "database": {"sha256": _database_sha256(database_path)},
        "workspaces": [{"id": str(WORKSPACE_ID)}],
    }
    manifest[malformed_field] = None
    Path(f"{database_path}.manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    completed = _run_cli(database_path, output_path, "--mode", "planner")

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "invalid_corpus_manifest"
    assert "corpus manifest" in error["message"]


def test_relation_sql_cli_exits_three_only_when_pinned_target_fails(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "relations.sqlite"
    output_path = tmp_path / "result.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--runs",
        "100",
        "--warmup",
        "10",
        "--max-p50-ms",
        "0.000001",
    )

    assert completed.returncode == 3, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["run_integrity"]["passed"] is True
    assert result["latency_target"]["target_mode"] == "pinned"
    assert result["latency_target"]["passed"] is False
    assert result["latency_target"]["per_mode"]["pinned"]["passed"] is False
    assert result["latency_target"]["per_mode"]["planner"]["passed"] is None
    assert result["latency_target"]["per_mode"]["not-indexed"]["passed"] is None


def test_relation_sql_cli_marks_under_sampled_target_ineligible(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "under-sampled.sqlite"
    output_path = tmp_path / "under-sampled.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--max-p50-ms",
        "1000",
        "--max-planner-ratio",
        "1.3",
    )

    assert completed.returncode == 2, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["run_integrity"]["passed"] is True
    assert result["latency_target"]["status"] == "ineligible"
    assert result["latency_target"]["passed"] is None
    assert result["latency_target"]["eligibility"] == {
        "eligible": False,
        "observed": {"measured_runs": 2, "warmup_runs": 1},
        "reasons": [
            "measured runs 2 are below required 100",
            "warmup runs 1 are below required 10",
        ],
        "required": {"measured_runs": 100, "warmup_runs": 10},
    }
    assert result["planner_ratio_target"]["status"] == "ineligible"
    assert result["planner_ratio_target"]["passed"] is None
    assert (
        result["planner_ratio_target"]["eligibility"]
        == result["latency_target"]["eligibility"]
    )


def test_relation_sql_cli_requires_all_comparison_modes_for_latency_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pinned-only.sqlite"
    output_path = tmp_path / "pinned-only.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--mode",
        "pinned",
        "--runs",
        "100",
        "--warmup",
        "10",
        "--max-p50-ms",
        "1000",
    )

    assert completed.returncode == 2, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["run_integrity"]["passed"] is True
    assert result["latency_target"]["status"] == "ineligible"
    assert result["latency_target"]["passed"] is None
    assert result["latency_target"]["eligibility"]["reasons"] == [
        "missing required comparison modes: planner, not-indexed"
    ]


def test_relation_sql_cli_requires_not_indexed_mode_for_planner_ratio_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ratio-without-scan.sqlite"
    output_path = tmp_path / "ratio-without-scan.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--mode",
        "pinned",
        "--mode",
        "planner",
        "--runs",
        "100",
        "--warmup",
        "10",
        "--max-planner-ratio",
        "1.3",
    )

    assert completed.returncode == 2, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    target = result["planner_ratio_target"]
    assert target["status"] == "ineligible"
    assert target["passed"] is None
    assert target["observed"]["pinned_p50_ms"] is not None
    assert target["observed"]["planner_p50_ms"] is not None
    assert target["eligibility"]["reasons"] == [
        "missing required comparison modes: not-indexed"
    ]


def test_relation_sql_cli_rejects_empty_relation_result(tmp_path: Path) -> None:
    database_path = tmp_path / "empty-relations.sqlite"
    output_path = tmp_path / "empty-relations.json"
    _create_page_link_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM page_links")

    completed = _run_cli(database_path, output_path)

    assert completed.returncode == 2, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["run_integrity"]["passed"] is False
    assert result["run_integrity"]["non_empty_result"] is False
    assert result["run_integrity"]["shared_row_count"] == 0


@pytest.mark.parametrize("threshold", ["nan", "inf", "-inf"])
def test_relation_sql_cli_rejects_non_finite_latency_target(
    tmp_path: Path,
    threshold: str,
) -> None:
    database_path = tmp_path / f"threshold-{threshold}.sqlite"
    output_path = tmp_path / f"threshold-{threshold}.json"
    _create_page_link_database(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        f"--max-p50-ms={threshold}",
    )

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "invalid_benchmark_input"
    assert error["message"].endswith("--max-p50-ms must be a finite positive number")


@pytest.mark.parametrize("threshold", ["nan", "inf", "-inf", "0", "-1"])
def test_relation_sql_cli_rejects_invalid_planner_ratio(
    tmp_path: Path,
    threshold: str,
) -> None:
    database_path = tmp_path / f"ratio-threshold-{threshold}.sqlite"
    output_path = tmp_path / f"ratio-threshold-{threshold}.json"
    _create_page_link_database(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        f"--max-planner-ratio={threshold}",
    )

    assert completed.returncode == 2
    assert not output_path.exists()
    error = json.loads(completed.stderr)
    assert error["code"] == "invalid_benchmark_input"
    assert error["message"].endswith(
        "--max-planner-ratio must be a finite positive number"
    )


@pytest.mark.parametrize(
    ("max_p50_ms", "max_planner_ratio", "failed_target"),
    [
        ("1000", "0.000000001", "planner_ratio_target"),
        ("0.000001", "1000000000", "latency_target"),
    ],
)
def test_relation_sql_cli_composes_absolute_and_planner_ratio_failures(
    tmp_path: Path,
    max_p50_ms: str,
    max_planner_ratio: str,
    failed_target: str,
) -> None:
    database_path = tmp_path / f"combined-{failed_target}.sqlite"
    output_path = tmp_path / f"combined-{failed_target}.json"
    _create_page_link_database(database_path)
    _write_valid_manifest(database_path)

    completed = _run_cli(
        database_path,
        output_path,
        "--runs",
        "100",
        "--warmup",
        "10",
        "--max-p50-ms",
        max_p50_ms,
        "--max-planner-ratio",
        max_planner_ratio,
    )

    assert completed.returncode == 3, completed.stderr
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["run_integrity"]["passed"] is True
    assert result["latency_target"]["eligibility"]["eligible"] is True
    assert result["planner_ratio_target"]["eligibility"]["eligible"] is True
    assert result[failed_target]["passed"] is False
    other_target = (
        "planner_ratio_target"
        if failed_target == "latency_target"
        else "latency_target"
    )
    assert result[other_target]["passed"] is True


def test_latency_target_is_distinct_from_run_integrity() -> None:
    latency_by_mode = {
        "pinned": {"p50": 28.0, "p95": 31.0, "max": 35.0},
        "planner": {"p50": 42.0, "p95": 47.0, "max": 50.0},
        "not-indexed": {"p50": 51.0, "p95": 57.0, "max": 60.0},
    }

    target = build_latency_target(
        latency_by_mode,
        max_p50_ms=36.4,
        measured_runs=100,
        warmup_runs=10,
    )

    assert target == {
        "configured": True,
        "eligibility": {
            "eligible": True,
            "observed": {"measured_runs": 100, "warmup_runs": 10},
            "reasons": [],
            "required": {"measured_runs": 100, "warmup_runs": 10},
        },
        "max_p50_ms": 36.4,
        "metric": "execute_fetchall_latency_ms.p50",
        "target_mode": "pinned",
        "passed": True,
        "status": "passed",
        "per_mode": {
            "pinned": {
                "p50_ms": 28.0,
                "role": "latency_target",
                "passed": True,
            },
            "planner": {
                "p50_ms": 42.0,
                "role": "attribution_comparator",
                "passed": None,
            },
            "not-indexed": {
                "p50_ms": 51.0,
                "role": "attribution_comparator",
                "passed": None,
            },
        },
    }
    assert (
        build_latency_target(
            latency_by_mode,
            max_p50_ms=None,
            measured_runs=2,
            warmup_runs=1,
        )["passed"]
        is None
    )


def test_planner_ratio_target_records_calculation_and_eligibility() -> None:
    latency_by_mode = {
        "pinned": {"p50": 28.0, "p95": 31.0, "max": 35.0},
        "planner": {"p50": 20.0, "p95": 24.0, "max": 27.0},
        "not-indexed": {"p50": 51.0, "p95": 57.0, "max": 60.0},
    }

    target = build_planner_ratio_target(
        latency_by_mode,
        max_planner_ratio=1.3,
        measured_runs=100,
        warmup_runs=10,
    )

    assert target == {
        "configured": True,
        "eligibility": {
            "eligible": True,
            "observed": {"measured_runs": 100, "warmup_runs": 10},
            "reasons": [],
            "required": {"measured_runs": 100, "warmup_runs": 10},
        },
        "max_planner_ratio": 1.3,
        "metric": "execute_fetchall_latency_ms.p50",
        "target_mode": "pinned",
        "comparator_mode": "planner",
        "formula": "pinned_p50_ms <= planner_p50_ms * max_planner_ratio",
        "observed": {
            "pinned_p50_ms": 28.0,
            "planner_p50_ms": 20.0,
            "pinned_to_planner_ratio": 1.4,
            "allowed_pinned_p50_ms": 26.0,
        },
        "passed": False,
        "status": "failed",
    }
    assert (
        build_planner_ratio_target(
            latency_by_mode,
            max_planner_ratio=1.3,
            measured_runs=2,
            warmup_runs=1,
        )["passed"]
        is None
    )


def test_row_identity_is_order_independent_and_multiplicity_preserving() -> None:
    rows = [
        ("source-a", "target-a", 1),
        ("source-b", "target-b", 2),
        ("source-a", "target-a", 1),
    ]

    assert _row_sha256(rows) == _row_sha256(list(reversed(rows)))
    assert _row_sha256(rows) != _row_sha256(rows[:-1])
