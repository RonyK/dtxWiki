#!/usr/bin/env python3
"""Benchmark the Workspace Knowledge Map service and HTTP boundary."""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.container import assemble  # noqa: E402
from backend.app.db import make_engine, make_session_factory  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.settings import Settings  # noqa: E402
from backend.policy.rest_scopes import require_token_scope  # noqa: E402
from backend.schemas.workspace_graph import (  # noqa: E402
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphScenePayload,
)
from backend.services.db_read_snapshot import (  # noqa: E402
    acquire_consistent_read_snapshot,
)
from backend.services.workspace_graph_service import (  # noqa: E402
    WorkspaceGraphService,
)
from scripts.bench.graph._common import (  # noqa: E402
    OperationResult,
    ThreadLocalResource,
    benchmark_has_failures,
    cold_measurement_contract,
    command_line,
    compressed_size,
    default_output_path,
    drop_benchmark_indexes,
    ensure_sqlite_database,
    http_benchmark_actor,
    json_object,
    load_corpus_metadata,
    resolve_benchmark_actor,
    result_document,
    run_benchmark,
    run_cold_subprocesses,
    validate_common_args,
    write_result,
)

_VISIBILITY_METHODS = (
    "_load_documents",
    "_load_relation_universe",
)
_NORMALIZATION_METHODS = (
    "_apply_document_filters",
    "_scene_relations",
    "_apply_relation_time_filter",
    "_apply_metrics",
    "_quick_filter_counts",
    "_apply_quick_filters",
)
_MERGE_METHODS = (
    "_render",
    "_facet_counts",
)
_HTTP_REQUEST_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "workspace_graph_http_benchmark_request",
    default=None,
)


@dataclass(slots=True)
class PhaseRecord:
    query_ms: float = 0.0
    sql_execute_ms: float = 0.0
    visibility_ms: float = 0.0
    normalization_ms: float = 0.0
    merge_ms: float = 0.0
    _sql_started: list[float] = field(default_factory=list)
    _depths: dict[str, int] = field(default_factory=dict)

    def phases(self, *, encoding_ms: float | None) -> dict[str, float | None]:
        measured = (
            self.sql_execute_ms
            + self.visibility_ms
            + self.normalization_ms
            + self.merge_ms
        )
        return {
            "visibility": self.visibility_ms,
            "normalization": self.normalization_ms,
            "candidate_retrieval": self.sql_execute_ms,
            "merge": self.merge_ms,
            "encoding": encoding_ms,
            "service_unattributed": max(0.0, self.query_ms - measured),
        }


class SqlExecutionProbe:
    """Measure DBAPI cursor execution for the currently timed service call."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._current: contextvars.ContextVar[PhaseRecord | None] = (
            contextvars.ContextVar("workspace_graph_phase_record", default=None)
        )
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)

    def activate(self, record: PhaseRecord):
        return self._current.set(record)

    def reset(self, token: contextvars.Token[PhaseRecord | None]) -> None:
        self._current.reset(token)

    def close(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before)
        event.remove(self._engine, "after_cursor_execute", self._after)

    def _before(self, *_args: object, **_kwargs: object) -> None:
        record = self._current.get()
        if record is not None:
            record._sql_started.append(time.perf_counter())

    def _after(self, *_args: object, **_kwargs: object) -> None:
        record = self._current.get()
        if record is not None and record._sql_started:
            record.sql_execute_ms += (
                time.perf_counter() - record._sql_started.pop()
            ) * 1_000


class TimedWorkspaceGraphService:
    """Create an isolated service instance and time stable method boundaries."""

    def __init__(self, probe: SqlExecutionProbe) -> None:
        self._probe = probe

    def query(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        request: WorkspaceGraphOverviewRequest,
        actor,
    ) -> tuple[WorkspaceGraphScenePayload, PhaseRecord]:
        record = PhaseRecord()
        service = WorkspaceGraphService(cursor_secret="graph-benchmark-secret")
        for method_name in _VISIBILITY_METHODS:
            self._wrap(service, method_name, "visibility_ms", record)
        for method_name in _NORMALIZATION_METHODS:
            self._wrap(service, method_name, "normalization_ms", record)
        for method_name in _MERGE_METHODS:
            self._wrap(service, method_name, "merge_ms", record)

        token = self._probe.activate(record)
        started = time.perf_counter()
        try:
            payload = service.query(
                session,
                workspace_id,
                request=request,
                actor=actor,
            )
        finally:
            record.query_ms = (time.perf_counter() - started) * 1_000
            self._probe.reset(token)
        return payload, record

    @staticmethod
    def _wrap(
        service: WorkspaceGraphService,
        method_name: str,
        phase_name: str,
        record: PhaseRecord,
    ) -> None:
        original = getattr(service, method_name)

        def timed(*args: object, **kwargs: object):
            depth = record._depths.get(phase_name, 0)
            record._depths[phase_name] = depth + 1
            sql_before = record.sql_execute_ms
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                record._depths[phase_name] -= 1
                if depth == 0:
                    wall_ms = (time.perf_counter() - started) * 1_000
                    sql_ms = record.sql_execute_ms - sql_before
                    current = getattr(record, phase_name)
                    setattr(record, phase_name, current + max(0.0, wall_ms - sql_ms))

        setattr(service, method_name, timed)


class HttpWorkspaceGraphService:
    """Route adapter that makes the service phase record observable to a client."""

    def __init__(self, timed: TimedWorkspaceGraphService) -> None:
        self._timed = timed
        self._records: dict[str, PhaseRecord] = {}
        self._condition = threading.Condition()

    def query(self, *args: object, **kwargs: object) -> WorkspaceGraphScenePayload:
        payload, record = self._timed.query(*args, **kwargs)
        request_key = _HTTP_REQUEST_KEY.get()
        if request_key is None:
            raise RuntimeError("HTTP benchmark request key is missing")
        with self._condition:
            self._records[request_key] = record
            self._condition.notify_all()
        return payload

    def take_record(self, request_key: str) -> PhaseRecord:
        deadline = time.monotonic() + 5
        with self._condition:
            while request_key not in self._records:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("HTTP request produced no service phase record")
                self._condition.wait(remaining)
            return self._records.pop(request_key)

    def discard_record(self, request_key: str) -> None:
        with self._condition:
            self._records.pop(request_key, None)


class HttpHarness:
    def __init__(
        self,
        *,
        app,
        timed_service: HttpWorkspaceGraphService,
        workspace_id: UUID,
        request_json: dict[str, Any],
    ) -> None:
        self._client = TestClient(
            app, base_url="http://127.0.0.1", raise_server_exceptions=False
        )
        self._service = timed_service
        self._route = f"/api/v1/workspaces/{workspace_id}/graph/overview/query"
        self._request_json = request_json

    def operation(self) -> OperationResult:
        request_key = uuid4().hex
        started = time.perf_counter()
        response = self._client.post(
            self._route,
            json=self._request_json,
            headers={"X-Dtxwiki-Graph-Benchmark": request_key},
        )
        elapsed_ms = (time.perf_counter() - started) * 1_000
        if response.status_code != 200:
            self._service.discard_record(request_key)
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        record = self._service.take_record(request_key)
        payload_bytes = response.content
        encoding_residual_ms = max(0.0, elapsed_ms - record.query_ms)
        data = response.json()["data"]
        return OperationResult(
            elapsed_ms=elapsed_ms,
            response_bytes=len(payload_bytes),
            gzip_bytes=compressed_size(payload_bytes),
            phases_ms=record.phases(encoding_ms=encoding_residual_ms),
            details=_response_details(data),
        )

    def close(self) -> None:
        self._client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--actor",
        choices=("viewer", "owner", "restricted_reader"),
        default="viewer",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--only-relation-read",
        action="store_true",
        help=(
            "time only _load_relation_universe after preparing the visible "
            "document set outside the measured interval"
        ),
    )
    parser.add_argument(
        "--cold-runs",
        type=int,
        default=0,
        help="fresh-process samples; must be 0 or at least 20",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--_cold-child",
        choices=("service_direct", "fastapi_testclient", "relation_read"),
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_common_args(
        runs=args.runs,
        warmup=args.warmup,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
    )
    cold_measurement_contract(args.cold_runs)
    database_path = ensure_sqlite_database(args.db)
    if args.request is None:
        if not args.only_relation_read:
            raise ValueError("--request is required unless --only-relation-read is set")
        request_path = None
        request = WorkspaceGraphOverviewRequest()
    else:
        request_path = args.request.expanduser().resolve()
        request = WorkspaceGraphOverviewRequest.model_validate(
            json_object(request_path)
        )
    canonical_request = request.model_dump(mode="json", by_alias=True)
    corpus = load_corpus_metadata(
        database_path,
        workspace_id=args.workspace_id,
    )
    removed_indexes = [] if args._cold_child else drop_benchmark_indexes(database_path)

    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    session_factory = make_session_factory(engine)
    actor, actor_metadata = resolve_benchmark_actor(
        session_factory,
        workspace_id=args.workspace_id,
        actor_name=args.actor,
    )
    probe = SqlExecutionProbe(engine)
    timed_service = TimedWorkspaceGraphService(probe)

    def direct_operation() -> OperationResult:
        with session_factory() as session:
            started = time.perf_counter()
            acquire_consistent_read_snapshot(session)
            payload, record = timed_service.query(
                session,
                args.workspace_id,
                request=request,
                actor=actor,
            )
            record.query_ms = (time.perf_counter() - started) * 1_000
        encoding_started = time.perf_counter()
        payload_bytes = payload.model_dump_json().encode("utf-8")
        encoding_ms = (time.perf_counter() - encoding_started) * 1_000
        return OperationResult(
            elapsed_ms=record.query_ms,
            response_bytes=len(payload_bytes),
            gzip_bytes=compressed_size(payload_bytes),
            phases_ms=record.phases(encoding_ms=encoding_ms),
            details=_response_details(payload.model_dump(mode="json")),
        )

    def relation_read_operation() -> OperationResult:
        with session_factory() as session:
            # Documents and relations must describe one state; snapshot setup
            # and visible-document loading remain outside this SQL slice.
            acquire_consistent_read_snapshot(session)
            documents = WorkspaceGraphService._load_documents(
                session,
                args.workspace_id,
                actor,
            )
            record = PhaseRecord()
            token = probe.activate(record)
            started = time.perf_counter()
            try:
                relations = WorkspaceGraphService._load_relation_universe(
                    session,
                    args.workspace_id,
                    documents,
                )
            finally:
                record.query_ms = (time.perf_counter() - started) * 1_000
                probe.reset(token)
        relation_counts = Counter(relation.kind for relation in relations)
        details = {
            "visible_documents": len(documents),
            "relations": len(relations),
            "relation_kinds": dict(sorted(relation_counts.items())),
        }
        payload_bytes = json.dumps(
            details,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        visibility_proxy = max(0.0, record.query_ms - record.sql_execute_ms)
        return OperationResult(
            elapsed_ms=record.query_ms,
            response_bytes=len(payload_bytes),
            gzip_bytes=compressed_size(payload_bytes),
            phases_ms={
                "visibility": visibility_proxy,
                "normalization": None,
                "candidate_retrieval": record.sql_execute_ms,
                "merge": None,
                "encoding": None,
                "service_unattributed": 0.0,
            },
            details=details,
        )

    http_resources: ThreadLocalResource | None = None
    direct_result: dict[str, object] | None = None
    http_result: dict[str, object] | None = None
    relation_result: dict[str, object] | None = None
    try:
        if args.only_relation_read:
            if args._cold_child not in {None, "relation_read"}:
                raise ValueError(
                    "--only-relation-read cold child must use relation_read"
                )
            relation_result = run_benchmark(
                relation_read_operation,
                runs=args.runs,
                warmup=args.warmup,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
            )
        elif args._cold_child == "relation_read":
            raise ValueError("relation_read cold child requires --only-relation-read")
        elif args._cold_child == "service_direct":
            direct_result = run_benchmark(
                direct_operation,
                runs=args.runs,
                warmup=args.warmup,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            if args._cold_child not in {None, "fastapi_testclient"}:
                raise ValueError(f"unsupported cold child: {args._cold_child}")
            http_app, http_timed_service = _build_http_app(
                session_factory=session_factory,
                actor=actor,
                probe=probe,
            )
            http_resources = ThreadLocalResource(
                lambda: HttpHarness(
                    app=http_app,
                    timed_service=http_timed_service,
                    workspace_id=args.workspace_id,
                    request_json=canonical_request,
                )
            )
            if args._cold_child is None:
                direct_result = run_benchmark(
                    direct_operation,
                    runs=args.runs,
                    warmup=args.warmup,
                    concurrency=args.concurrency,
                    timeout_seconds=args.timeout_seconds,
                )
            http_result = run_benchmark(
                lambda: http_resources.get().operation(),
                runs=args.runs,
                warmup=args.warmup,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
            )
    finally:
        if http_resources is not None:
            http_resources.close()
        probe.close()
        engine.dispose()

    if args._cold_child is not None:
        child_result = {
            "service_direct": direct_result,
            "fastapi_testclient": http_result,
            "relation_read": relation_result,
        }[args._cold_child]
        if child_result is None:
            raise RuntimeError("cold child did not produce a benchmark result")
        sample = child_result["samples"][0]
        print("DTX_GRAPH_COLD_SAMPLE=" + json.dumps(sample, ensure_ascii=False))
        return 0 if sample["status"] == "completed" else 2

    cold = run_cold_subprocesses(
        lambda transport, _index: _cold_command(
            args,
            database_path=database_path,
            request_path=request_path,
            transport=transport,
        ),
        transports=(
            ("relation_read",)
            if args.only_relation_read
            else ("service_direct", "fastapi_testclient")
        ),
        runs=args.cold_runs,
        process_timeout_seconds=max(60.0, args.timeout_seconds + 30.0),
    )

    benchmarks = (
        {"relation_read": relation_result}
        if args.only_relation_read
        else {
            "service_direct": direct_result,
            "fastapi_testclient": http_result,
        }
    )

    result = result_document(
        tool="bench_overview",
        database_path=database_path,
        workspace_id=args.workspace_id,
        corpus=corpus,
        body={
            "command": command_line(),
            "request_file": str(request_path) if request_path is not None else None,
            "request": canonical_request,
            "actor": actor_metadata,
            "parameters": {
                "runs": args.runs,
                "warmup": args.warmup,
                "concurrency": args.concurrency,
                "timeout_seconds": args.timeout_seconds,
                "only_relation_read": args.only_relation_read,
                "cold_runs": args.cold_runs,
            },
            "removed_benchmark_indexes": removed_indexes,
            "lifecycle_status_mutated": False,
            "phase_measurement": _phase_measurement_contract(),
            "relation_read_measurement": (
                {
                    "enabled": True,
                    "method": "exact_private_method_boundary",
                    "scope": "WorkspaceGraphService._load_relation_universe",
                    "setup_excluded": (
                        "_load_documents prepares the actor-visible head set before "
                        "the measured interval"
                    ),
                }
                if args.only_relation_read
                else {"enabled": False}
            ),
            "benchmarks": benchmarks,
            "cold": cold,
        },
    )
    output_path = (args.out or default_output_path("overview")).resolve()
    write_result(output_path, result)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "benchmarks": {
                    name: benchmark["summary"]
                    for name, benchmark in benchmarks.items()
                    if benchmark is not None
                },
            },
            ensure_ascii=False,
        )
    )
    failed = any(
        benchmark_has_failures(benchmark)
        for benchmark in benchmarks.values()
        if benchmark is not None
    )
    if args.cold_runs:
        failed = failed or any(
            report["errors"] or report["timeouts"]
            for report in cold["transports"].values()
        )
    return 2 if failed else 0


def _cold_command(
    args: argparse.Namespace,
    *,
    database_path: Path,
    request_path: Path | None,
    transport: str,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--db",
        str(database_path),
        "--workspace-id",
        str(args.workspace_id),
        "--runs",
        "1",
        "--warmup",
        "0",
        "--concurrency",
        "1",
        "--actor",
        args.actor,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--_cold-child",
        transport,
    ]
    if request_path is not None:
        command.extend(("--request", str(request_path)))
    if args.only_relation_read:
        command.append("--only-relation-read")
    return command


def _database_url(session_factory: sessionmaker[Session]) -> str:
    engine = session_factory.kw.get("bind")
    if engine is None:
        raise RuntimeError("benchmark session factory has no engine")
    return str(engine.url)


def _build_http_app(
    *,
    session_factory: sessionmaker[Session],
    actor,
    probe: SqlExecutionProbe,
):
    settings = Settings.model_construct(
        database_url=_database_url(session_factory),
        session_secret="graph-benchmark-session-secret-0001",
        auto_bootstrap=False,
    )
    container = assemble(settings, session_factory=session_factory)
    timed_service = HttpWorkspaceGraphService(TimedWorkspaceGraphService(probe))
    container.services.__dict__["workspace_graph"] = timed_service
    app = create_app(
        settings=settings,
        container=container,
        include_llms_txt=False,
    )
    resolved_http_actor = http_benchmark_actor(actor)
    app.dependency_overrides[require_token_scope] = lambda: resolved_http_actor

    @app.middleware("http")
    async def bind_benchmark_request(request, call_next):
        token = _HTTP_REQUEST_KEY.set(request.headers.get("X-Dtxwiki-Graph-Benchmark"))
        try:
            return await call_next(request)
        finally:
            _HTTP_REQUEST_KEY.reset(token)

    return app, timed_service


def _response_details(data: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": data["schema_version"],
        "counts": data["counts"],
        "representation": data["representation"],
        "completeness": data["completeness"],
        "truncated": data["truncated"],
        "omission_reasons": data["omission_reasons"],
    }


def _phase_measurement_contract() -> dict[str, object]:
    return {
        "additive_partition": False,
        "note": (
            "These are explicit method-boundary or transport proxies. They do "
            "not form an exhaustive additive decomposition of total latency."
        ),
        "visibility": {
            "method": "proxy",
            "scope": ("non-DBAPI wall time inside " + ", ".join(_VISIBILITY_METHODS)),
            "excludes": (
                "DBAPI cursor execution; includes ORM hydration and policy checks"
            ),
        },
        "normalization": {
            "method": "proxy",
            "scope": "non-DBAPI wall time inside " + ", ".join(_NORMALIZATION_METHODS),
        },
        "candidate_retrieval": {
            "method": "proxy",
            "scope": "SQLAlchemy DBAPI cursor execute callbacks during service query",
            "excludes": "ORM row hydration and Python visibility processing",
        },
        "merge": {
            "method": "proxy",
            "scope": "non-DBAPI wall time inside " + ", ".join(_MERGE_METHODS),
        },
        "encoding": {
            "service_direct": {
                "method": "exact",
                "scope": "WorkspaceGraphScenePayload.model_dump_json",
                "included_in_latency": False,
            },
            "fastapi_testclient": {
                "method": "proxy",
                "scope": "HTTP elapsed minus timed service query",
                "includes": "routing, authorization, response validation, and encoding",
            },
        },
        "service_unattributed": {
            "method": "residual",
            "scope": "service query minus the non-overlapping measured proxies",
        },
        "timeout": {
            "method": "observed_budget",
            "scope": (
                "A completed operation whose measured duration exceeds the timeout "
                "budget is retained and classified as timeout; Python threads are "
                "not forcibly terminated."
            ),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
