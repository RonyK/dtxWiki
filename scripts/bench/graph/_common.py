"""Shared primitives for the tracked Graph View benchmark runners.

The benchmark scripts intentionally retain every attempted sample.  A failed
or over-budget request remains in the denominator instead of disappearing from
latency percentiles and making a broken run look healthy.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.spi.types import ActorContext

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RESULT_SCHEMA_VERSION = "dtxwiki.graph-benchmark.v1"
BENCHMARK_TOKEN_NAMESPACE = UUID("18e22ba7-bbde-54cf-a7cb-7db64da82c04")

SampleStatus = Literal["completed", "error", "timeout"]


@dataclass(slots=True)
class OperationResult:
    """One successful operation before timeout classification."""

    elapsed_ms: float
    response_bytes: int
    gzip_bytes: int
    phases_ms: dict[str, float | None] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Sample:
    index: int
    status: SampleStatus
    elapsed_ms: float
    response_bytes: int | None = None
    gzip_bytes: int | None = None
    phases_ms: dict[str, float | None] = field(default_factory=dict)
    error: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "index": self.index,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 6),
            "response_bytes": self.response_bytes,
            "gzip_bytes": self.gzip_bytes,
            "phases_ms": {
                name: None if value is None else round(value, 6)
                for name, value in sorted(self.phases_ms.items())
            },
            "error": self.error,
        }


def compressed_size(payload: bytes) -> int:
    """Return a reproducible gzip size without a wall-clock header."""

    return len(gzip.compress(payload, mtime=0))


def nearest_rank(values: Sequence[float | int], percentile: float) -> float | int:
    """Return the nearest-rank percentile defined by ceil(p * N)."""

    if not values:
        raise ValueError("nearest_rank requires at least one value")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def distribution(values: Sequence[float | int]) -> dict[str, float | int] | None:
    if not values:
        return None
    return {
        "p50": nearest_rank(values, 0.50),
        "p95": nearest_rank(values, 0.95),
        "max": max(values),
    }


def run_benchmark(
    operation: Callable[[], OperationResult],
    *,
    runs: int,
    warmup: int,
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, object]:
    """Run warmup and measured attempts with one bounded worker pool."""

    if runs < 1:
        raise ValueError("runs must be at least 1")
    if warmup < 0:
        raise ValueError("warmup cannot be negative")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    # Keep the same workers alive across both batches.  In particular, this
    # makes the warmup exercise the same worker-local TestClient instances as
    # the measured batch instead of warming resources that are then discarded.
    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="graph-benchmark",
    ) as executor:
        warmup_started = time.perf_counter()
        warmup_samples = _run_attempts(
            executor,
            operation,
            attempts=warmup,
            timeout_seconds=timeout_seconds,
        )
        warmup_wall_ms = (time.perf_counter() - warmup_started) * 1_000

        measured_started = time.perf_counter()
        samples = _run_attempts(
            executor,
            operation,
            attempts=runs,
            timeout_seconds=timeout_seconds,
        )
        measured_wall_ms = (time.perf_counter() - measured_started) * 1_000

    measured = _summarize_samples(samples)
    warm = _summarize_samples(warmup_samples)
    eligible = runs >= 100 and warmup >= 10 and concurrency in {1, 4}
    passed = (
        eligible
        and measured["completed"] == runs
        and measured["errors"] == 0
        and measured["timeouts"] == 0
        and warm["completed"] == warmup
        and warm["errors"] == 0
        and warm["timeouts"] == 0
    )
    return {
        "runs": runs,
        "warmup_runs": warmup,
        "concurrency": concurrency,
        "timeout_seconds": timeout_seconds,
        "timeout_classification": {
            "method": "post_completion_observed_budget",
            "hard_deadline_enforced": False,
            "scope": (
                "A completed operation whose measured duration exceeds the "
                "configured budget is retained as a timeout sample."
            ),
            "limitation": (
                "In-process SQLAlchemy and TestClient calls cannot be safely "
                "cancelled from a Python worker thread; a hung call can stall "
                "the runner and needs an external process/job deadline."
            ),
        },
        "measurement_wall_ms": round(measured_wall_ms, 6),
        "warmup_wall_ms": round(warmup_wall_ms, 6),
        "summary": measured,
        "warmup": warm,
        "gate": {
            "eligible": eligible,
            "passed": passed,
            "reason": (
                None
                if passed
                else "smoke_only_requires_runs_100_warmup_10"
                if not eligible
                else "all_attempts_must_complete_without_error_or_timeout"
            ),
        },
        "samples": [sample.as_json() for sample in samples],
    }


def benchmark_has_failures(result: Mapping[str, object]) -> bool:
    """Return true when measured or warmup attempts failed or timed out."""

    for section_name in ("summary", "warmup"):
        section = result.get(section_name)
        if not isinstance(section, Mapping):
            return True
        if section.get("errors") or section.get("timeouts"):
            return True
    return False


def _run_attempts(
    executor: ThreadPoolExecutor,
    operation: Callable[[], OperationResult],
    *,
    attempts: int,
    timeout_seconds: float,
) -> list[Sample]:
    if attempts == 0:
        return []
    futures = [
        executor.submit(
            _run_one,
            operation,
            index=index,
            timeout_seconds=timeout_seconds,
        )
        for index in range(attempts)
    ]
    return [future.result() for future in futures]


def _run_one(
    operation: Callable[[], OperationResult],
    *,
    index: int,
    timeout_seconds: float,
) -> Sample:
    started = time.perf_counter()
    try:
        result = operation()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1_000
        return Sample(
            index=index,
            status="error",
            elapsed_ms=elapsed_ms,
            error=_error_text(exc),
        )
    elapsed_ms = result.elapsed_ms
    if elapsed_ms > timeout_seconds * 1_000:
        status: SampleStatus = "timeout"
        error = f"observed duration exceeded {timeout_seconds:g}s timeout budget"
    else:
        status = "completed"
        error = None
    return Sample(
        index=index,
        status=status,
        elapsed_ms=elapsed_ms,
        response_bytes=result.response_bytes,
        gzip_bytes=result.gzip_bytes,
        phases_ms=result.phases_ms,
        error=error,
        details=result.details,
    )


def _summarize_samples(samples: Sequence[Sample]) -> dict[str, object]:
    completed = [sample for sample in samples if sample.status == "completed"]
    errors = sum(sample.status == "error" for sample in samples)
    timeouts = sum(sample.status == "timeout" for sample in samples)
    attempted = len(samples)
    phase_names = sorted(
        {
            name
            for sample in completed
            for name, value in sample.phases_ms.items()
            if value is not None
        }
    )
    representative = completed[-1].details if completed else None
    return {
        "attempted": attempted,
        "completed": len(completed),
        "errors": errors,
        "timeouts": timeouts,
        "failure_rate": (
            round((errors + timeouts) / attempted, 8) if attempted else 0.0
        ),
        "latency_ms": _rounded_distribution(
            [sample.elapsed_ms for sample in completed]
        ),
        "response_bytes": distribution(
            [sample.response_bytes for sample in completed if sample.response_bytes]
        ),
        "gzip_bytes": distribution(
            [sample.gzip_bytes for sample in completed if sample.gzip_bytes]
        ),
        "phases_ms": {
            name: _rounded_distribution(
                [
                    value
                    for sample in completed
                    if (value := sample.phases_ms.get(name)) is not None
                ]
            )
            for name in phase_names
        },
        "representative_response": representative,
        "failures": [
            sample.as_json() for sample in samples if sample.status != "completed"
        ],
    }


def _rounded_distribution(
    values: Sequence[float],
) -> dict[str, float] | None:
    result = distribution(values)
    if result is None:
        return None
    return {name: round(float(value), 6) for name, value in result.items()}


def _error_text(exc: Exception) -> str:
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {first_line}"[:500]


def drop_benchmark_indexes(database_path: Path) -> list[str]:
    """Drop only explicitly disposable indexes whose names start ``bench_``."""

    with sqlite3.connect(database_path) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'index' AND name LIKE 'bench\\_%' ESCAPE '\\'
                ORDER BY name
                """
            )
        ]
        for name in names:
            quoted = f'"{name.replace(chr(34), chr(34) * 2)}"'
            connection.execute(f"DROP INDEX {quoted}")
        connection.commit()
    return names


def load_corpus_metadata(
    database_path: Path,
    *,
    workspace_id: UUID,
) -> dict[str, object]:
    manifest_path = Path(f"{database_path}.manifest.json")
    metadata: dict[str, object] = {
        "database": str(database_path),
        "database_bytes_at_start": database_path.stat().st_size,
        "manifest": str(manifest_path) if manifest_path.exists() else None,
        "manifest_status": "present" if manifest_path.exists() else "absent",
    }
    if not manifest_path.exists():
        metadata.update(
            _unmanifested_corpus_metadata(
                database_path,
                workspace_id=workspace_id,
            )
        )
        return metadata
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    arguments = manifest.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError(f"corpus manifest has no arguments object: {manifest_path}")
    generator_version = manifest.get("generator_version")
    if not isinstance(generator_version, str) or not generator_version:
        raise ValueError(f"corpus manifest has no generator_version: {manifest_path}")
    workspaces = manifest.get("workspaces", [])
    workspace = next(
        (
            item
            for item in workspaces
            if isinstance(item, Mapping) and item.get("id") == str(workspace_id)
        ),
        None,
    )
    metadata.update(
        {
            "manifest_version": manifest.get("manifest_version"),
            "generator_version": generator_version,
            "arguments": dict(arguments),
            "arguments_status": "manifest",
            "fixture": None,
            "source": manifest.get("source"),
            "workspace": workspace,
            "workspace_listed": workspace is not None,
            "manifest_hashes_rechecked": False,
            "manifest_hash_note": (
                "The runner may drop disposable bench_ indexes before timing; "
                "the immutable corpus manifest remains provenance, not a "
                "post-mutation checksum."
            ),
        }
    )
    return metadata


def _unmanifested_corpus_metadata(
    database_path: Path,
    *,
    workspace_id: UUID,
) -> dict[str, object]:
    """Record observable counts while retaining unknown generator inputs."""

    workspace_keys = (workspace_id.hex, str(workspace_id))
    with sqlite3.connect(database_path) as connection:
        workspace = connection.execute(
            "SELECT slug FROM workspace WHERE id IN (?, ?) LIMIT 1",
            workspace_keys,
        ).fetchone()
        if workspace is None:
            raise ValueError(
                f"workspace is absent from benchmark database: {workspace_id}"
            )
        context_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM context_page WHERE workspace_id IN (?, ?)",
                workspace_keys,
            ).fetchone()[0]
        )
        library_count, restricted_count = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN trust_level = 'restricted' THEN 1 ELSE 0 END)
            FROM library_item
            WHERE workspace_id IN (?, ?)
            """,
            workspace_keys,
        ).fetchone()
        workspace_count = int(
            connection.execute("SELECT COUNT(*) FROM workspace").fetchone()[0]
        )

    document_count = context_count + int(library_count)
    restricted_pct = (
        round(int(restricted_count or 0) * 100 / int(library_count), 6)
        if library_count
        else 0.0
    )
    return {
        "generator_version": None,
        "arguments": {
            "documents": document_count,
            "profile": "unknown",
            "workspaces": workspace_count,
            "restricted_library_pct": restricted_pct,
            "relation_multiplier": None,
            "seed": None,
        },
        "arguments_status": "inferred_incomplete",
        "arguments_missing": ["profile", "relation_multiplier", "seed"],
        "arguments_note": (
            "The companion manifest is absent. Counts were read from the supplied "
            "database; generator-only inputs cannot be reconstructed."
        ),
        "fixture": None,
        "source": {"kind": "unmanifested_database"},
        "workspace": {
            "id": str(workspace_id),
            "slug": str(workspace[0]),
            "contexts": context_count,
            "library": int(library_count),
            "restricted_library": int(restricted_count or 0),
        },
        "workspace_listed": True,
    }


def resolve_benchmark_actor(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: UUID,
    actor_name: Literal["viewer", "owner", "restricted_reader"],
) -> tuple[ActorContext, dict[str, object]]:
    """Load one persisted F00 principal and reproduce its benchmark channel."""

    email_prefix = f"graph-{actor_name.replace('_', '-')}+"
    with session_factory() as session:
        rows = session.execute(
            select(User, WorkspaceMembership)
            .join(
                WorkspaceMembership,
                WorkspaceMembership.user_id == User.id,
            )
            .where(WorkspaceMembership.workspace_id == workspace_id)
            .order_by(User.email, User.id)
        ).all()
    matching = [row for row in rows if row[0].email.startswith(email_prefix)]
    if len(matching) != 1:
        raise RuntimeError(
            f"workspace must contain exactly one {actor_name!r} F00 principal; "
            f"found {len(matching)}"
        )
    user, membership = matching[0]
    restricted = actor_name == "restricted_reader"
    # A synthetic restricted reader models a PAT with the ordinary read
    # umbrella plus the capability the current product cannot issue yet.
    # ``mcp:read`` satisfies the workspace access matrix; the restricted
    # scope is consumed by ``library_item_visible``.
    scopes = ("mcp:read", "library:restricted-read") if restricted else ()
    token_kind = "api_token" if restricted else "session"
    token_id = (
        uuid5(BENCHMARK_TOKEN_NAMESPACE, f"{workspace_id}:{actor_name}")
        if restricted
        else None
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user.id),
        user_id=user.id,
        scopes=scopes,
        workspace_role=membership.role,
        role=membership.role,
        workspace_id=workspace_id,
        token_kind=token_kind,
        token_id=token_id,
        token_workspace_ids=(workspace_id,) if restricted else (),
        provenance="personal_access_token" if restricted else "browser_session",
    )
    return actor, {
        "name": actor_name,
        "user_id": str(user.id),
        "email": user.email,
        "workspace_role": membership.role,
        "auth_adapter": (
            "direct_synthetic_pat_http_synthetic_session_override"
            if restricted
            else "session_actor_dependency_override"
        ),
        "scopes": list(scopes),
    }


def http_benchmark_actor(actor: ActorContext) -> ActorContext:
    """Avoid inventing a persisted PAT grant for the synthetic capability.

    F00's restricted principal is a real viewer membership, but the product
    cannot issue ``library:restricted-read`` yet.  Direct service calls retain
    the PAT-shaped actor.  The HTTP dependency override uses the same user and
    membership as a session actor with the synthetic capability, avoiding a
    fake ApiToken/credential-replica row while exercising the real workspace
    access dependency and visibility policy.
    """

    if "library:restricted-read" not in actor.scopes:
        return actor
    return replace(
        actor,
        token_kind="session",
        token_id=None,
        token_workspace_ids=(),
        provenance="browser_session",
    )


def environment_fingerprint() -> dict[str, object]:
    return {
        "commit": _command_value(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
        ),
        "git_dirty": bool(
            _command_value(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=REPOSITORY_ROOT,
            )
        ),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
        "gpu": {
            "value": os.environ.get("DTXWIKI_BENCH_GPU"),
            "method": (
                "DTXWIKI_BENCH_GPU"
                if os.environ.get("DTXWIKI_BENCH_GPU")
                else "not_probed_by_backend_runner"
            ),
        },
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "node_version": _command_value(["node", "--version"]),
        "browser_version": {
            "value": os.environ.get("DTXWIKI_BENCH_BROWSER_VERSION"),
            "method": (
                "DTXWIKI_BENCH_BROWSER_VERSION"
                if os.environ.get("DTXWIKI_BENCH_BROWSER_VERSION")
                else "not_applicable_backend_runner"
            ),
        },
        "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
    }


def result_document(
    *,
    tool: str,
    database_path: Path,
    workspace_id: UUID,
    corpus: Mapping[str, object],
    body: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "tool": tool,
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": environment_fingerprint(),
        "database": str(database_path),
        "workspace_id": str(workspace_id),
        "corpus": dict(corpus),
        **body,
    }


def default_output_path(tool: str) -> Path:
    date = datetime.now(UTC).strftime("%y%m%d")
    stamp = datetime.now(UTC).strftime("%H%M%S-%f")
    return REPOSITORY_ROOT / "doc" / "bench" / "graph" / date / f"{tool}-{stamp}.json"


def write_result(path: Path, result: Mapping[str, object]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(f"refusing to replace benchmark result: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            temporary.rename(path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def cold_measurement_contract(requested_runs: int) -> dict[str, object]:
    if requested_runs and requested_runs < 20:
        raise ValueError("cold_runs must be 0 or at least 20")
    return {
        "requested_runs": requested_runs,
        "minimum_valid_runs": 20,
        "process_restart_required": True,
        "status": "not_measured" if requested_runs == 0 else "pending",
        "method": (
            "Invoke each cold sample in a fresh Python process; process startup "
            "and request latency must be reported separately."
        ),
    }


def run_cold_subprocesses(
    command_factory: Callable[[str, int], Sequence[str]],
    *,
    transports: Sequence[str],
    runs: int,
    process_timeout_seconds: float,
) -> dict[str, object]:
    """Run exactly one request in each new child process.

    Children print a final ``DTX_GRAPH_COLD_SAMPLE=...`` line.  Request time is
    measured inside the child; parent wall time records process startup,
    imports, database/app setup, the request, and shutdown.  The residual is
    reported as startup/setup/shutdown rather than mislabeled request latency.
    """

    contract = cold_measurement_contract(runs)
    if runs == 0:
        return contract
    results: dict[str, object] = {}
    for transport in transports:
        samples: list[dict[str, object]] = []
        for index in range(runs):
            command = list(command_factory(transport, index))
            process_started = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=process_timeout_seconds,
                )
                process_wall_ms = (time.perf_counter() - process_started) * 1_000
                child_sample = _parse_cold_sample(completed.stdout)
                if child_sample is None:
                    sample = {
                        "index": index,
                        "status": "error",
                        "request_elapsed_ms": None,
                        "process_wall_ms": round(process_wall_ms, 6),
                        "startup_setup_shutdown_ms": None,
                        "response_bytes": None,
                        "gzip_bytes": None,
                        "phases_ms": {},
                        "error": (
                            f"child exit {completed.returncode}: "
                            f"{(completed.stderr or completed.stdout)[-500:]}"
                        ),
                    }
                else:
                    request_elapsed = child_sample.get("elapsed_ms")
                    request_ms = (
                        float(request_elapsed)
                        if isinstance(request_elapsed, int | float)
                        else None
                    )
                    sample = {
                        "index": index,
                        "status": child_sample.get("status", "error"),
                        "request_elapsed_ms": request_ms,
                        "process_wall_ms": round(process_wall_ms, 6),
                        "startup_setup_shutdown_ms": (
                            round(max(0.0, process_wall_ms - request_ms), 6)
                            if request_ms is not None
                            else None
                        ),
                        "response_bytes": child_sample.get("response_bytes"),
                        "gzip_bytes": child_sample.get("gzip_bytes"),
                        "phases_ms": child_sample.get("phases_ms", {}),
                        "error": child_sample.get("error"),
                    }
            except subprocess.TimeoutExpired as exc:
                process_wall_ms = (time.perf_counter() - process_started) * 1_000
                sample = {
                    "index": index,
                    "status": "timeout",
                    "request_elapsed_ms": None,
                    "process_wall_ms": round(process_wall_ms, 6),
                    "startup_setup_shutdown_ms": None,
                    "response_bytes": None,
                    "gzip_bytes": None,
                    "phases_ms": {},
                    "error": (
                        "child process exceeded "
                        f"{process_timeout_seconds:g}s: {type(exc).__name__}"
                    ),
                }
            samples.append(sample)
        results[transport] = _summarize_cold_samples(samples)
    return {
        **contract,
        "status": "completed",
        "process_timeout_seconds": process_timeout_seconds,
        "transports": results,
    }


def _parse_cold_sample(stdout: str) -> dict[str, object] | None:
    prefix = "DTX_GRAPH_COLD_SAMPLE="
    for line in reversed(stdout.splitlines()):
        if line.startswith(prefix):
            payload = json.loads(line.removeprefix(prefix))
            return payload if isinstance(payload, dict) else None
    return None


def _summarize_cold_samples(
    samples: Sequence[dict[str, object]],
) -> dict[str, object]:
    completed = [sample for sample in samples if sample["status"] == "completed"]
    errors = sum(sample["status"] == "error" for sample in samples)
    timeouts = sum(sample["status"] == "timeout" for sample in samples)

    def numeric(name: str) -> list[float]:
        return [
            float(value)
            for sample in completed
            if isinstance((value := sample.get(name)), int | float)
        ]

    return {
        "attempted": len(samples),
        "completed": len(completed),
        "errors": errors,
        "timeouts": timeouts,
        "failure_rate": round((errors + timeouts) / len(samples), 8),
        "request_latency_ms": _rounded_distribution(numeric("request_elapsed_ms")),
        "process_wall_ms": _rounded_distribution(numeric("process_wall_ms")),
        "startup_setup_shutdown_ms": _rounded_distribution(
            numeric("startup_setup_shutdown_ms")
        ),
        "response_bytes": distribution(numeric("response_bytes")),
        "gzip_bytes": distribution(numeric("gzip_bytes")),
        "samples": list(samples),
    }


def _command_value(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
) -> str | None:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or result.stderr.strip() or None


def ensure_sqlite_database(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"SQLite benchmark database does not exist: {resolved}")
    header = resolved.read_bytes()[:16]
    if header != b"SQLite format 3\x00":
        raise ValueError(f"benchmark database is not SQLite: {resolved}")
    return resolved


def validate_common_args(
    *,
    runs: int,
    warmup: int,
    concurrency: int,
    timeout_seconds: float,
) -> None:
    if runs < 1:
        raise ValueError("--runs must be at least 1")
    if warmup < 0:
        raise ValueError("--warmup cannot be negative")
    if concurrency not in {1, 4}:
        raise ValueError("--concurrency must be 1 or 4")
    if timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")


def close_all(resources: Sequence[Any]) -> None:
    """Best-effort deterministic close for per-thread TestClient harnesses."""

    for resource in resources:
        close = getattr(resource, "close", None)
        if callable(close):
            close()


class ThreadLocalResource:
    """Create one non-thread-safe resource for each benchmark worker."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._local = threading.local()
        self._resources: list[Any] = []
        self._lock = threading.Lock()

    def get(self) -> Any:
        resource = getattr(self._local, "resource", None)
        if resource is None:
            resource = self._factory()
            self._local.resource = resource
            with self._lock:
                self._resources.append(resource)
        return resource

    def close(self) -> None:
        close_all(self._resources)
        self._resources.clear()


def json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"request JSON must contain one object: {path}")
    return payload


def encode_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def session_factory_engine(factory: sessionmaker[Session]):
    engine = factory.kw.get("bind")
    if engine is None:
        raise RuntimeError("benchmark session factory has no bound engine")
    return engine


def command_line() -> list[str]:
    return [sys.executable, *sys.argv]
