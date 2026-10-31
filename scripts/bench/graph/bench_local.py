#!/usr/bin/env python3
"""Benchmark Local Graph requests for hub, median, and isolated Context seeds."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.container import assemble  # noqa: E402
from backend.app.db import make_engine, make_session_factory  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.settings import Settings  # noqa: E402
from backend.models.context import (  # noqa: E402
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
)
from backend.policy.rest_scopes import require_token_scope  # noqa: E402
from backend.schemas.graph import (  # noqa: E402
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.services.db_read_snapshot import (  # noqa: E402
    acquire_consistent_read_snapshot,
)
from backend.services.graph_cache import bump_edge_version  # noqa: E402
from backend.services.graph_query_service import GraphQueryService  # noqa: E402
from backend.services.graph_service import GraphService  # noqa: E402
from backend.services.indexing.edge_registry import (  # noqa: E402
    refresh_tree_edge_projection,
)
from backend.services.workspace_graph_capability import (  # noqa: E402
    usage_relation_signals_enabled,
)
from backend.services.workspace_graph_service import WorkspaceGraphService  # noqa: E402
from scripts.bench.graph._common import (  # noqa: E402
    OperationResult,
    ThreadLocalResource,
    benchmark_has_failures,
    cold_measurement_contract,
    command_line,
    compressed_size,
    default_output_path,
    distribution,
    drop_benchmark_indexes,
    ensure_sqlite_database,
    http_benchmark_actor,
    load_corpus_metadata,
    resolve_benchmark_actor,
    result_document,
    run_benchmark,
    run_cold_subprocesses,
    validate_common_args,
    write_result,
)

DEGREE_PROFILES = ("hub", "median", "isolated")
EXPLICIT_PROFILE = "explicit"

# --add-hub-backlinks rows. The id is derived from the endpoints and both
# timestamps come from the corpus, not the wall clock, so two fresh copies of
# one corpus receive byte-identical rows.
HUB_BACKLINK_WEIGHTS = (1.0, 2.0, 3.0)
HUB_BACKLINK_SOURCE_REF = {"table": "bench_local"}
HUB_BACKLINK_ID_NAMESPACE = uuid5(NAMESPACE_URL, "dtxwiki:bench_local:hub_backlink")

# --sql-profile groups statements by this execution option. Untagged
# statements that name the edge_projection table form their own group, so the
# pre-F08 collector remains visible next to the tagged F08 phases.
SQL_PROFILE_REQUESTS = 20
SQL_PROFILE_PHASE_OPTION = "graph_rank_phase"
SQL_PROFILE_UNTAGGED_EDGE_PROJECTION = "edge_projection_untagged"
SQL_PROFILE_OTHER = "other"
# Replays per distinct statement; the fastest is its fetch-inclusive time.
SQL_PROFILE_REPLAY_REPEATS = 5
# Word boundaries keep index names and the Workspace.edge_projection_version
# column out of the edge_projection group.
_EDGE_PROJECTION_TABLE = re.compile(r"\bedge_projection\b")
# EXPLAIN QUERY PLAN names an aliased table by its alias alone ("SCAN d" for
# "edge_projection AS d"), so a full-scan check must know the aliases.
_EDGE_PROJECTION_ALIAS = re.compile(
    r"\bedge_projection\"?\s+(AS\s+)?\"?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# Words that may follow the table name without AS and are not an alias.
_NOT_AN_ALIAS = frozenset(
    {
        "CROSS",
        "EXCEPT",
        "FULL",
        "GROUP",
        "HAVING",
        "INDEXED",
        "INNER",
        "INTERSECT",
        "JOIN",
        "LEFT",
        "LIMIT",
        "NATURAL",
        "NOT",
        "ON",
        "ORDER",
        "OUTER",
        "RETURNING",
        "RIGHT",
        "SET",
        "UNION",
        "USING",
        "VALUES",
        "WHERE",
        "WINDOW",
    }
)
_SQL_PROFILE_STARTED = "bench_local_sql_profile_started"


class LocalHttpHarness:
    def __init__(
        self,
        *,
        app,
        workspace_id: UUID,
        request_json: dict[str, Any],
    ) -> None:
        self._client = TestClient(
            app, base_url="http://127.0.0.1", raise_server_exceptions=False
        )
        self._route = f"/api/v1/workspaces/{workspace_id}/graph/query"
        self._request_json = request_json

    def operation(self) -> OperationResult:
        started = time.perf_counter()
        response = self._client.post(self._route, json=self._request_json)
        elapsed_ms = (time.perf_counter() - started) * 1_000
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        payload_bytes = response.content
        data = response.json()["data"]
        return OperationResult(
            elapsed_ms=elapsed_ms,
            response_bytes=len(payload_bytes),
            gzip_bytes=compressed_size(payload_bytes),
            phases_ms={"encoding": None},
            details=_response_details(data),
        )

    def close(self) -> None:
        self._client.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
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
        "--cold-runs",
        type=int,
        default=0,
        help="fresh-process samples per seed and transport; 0 or at least 20",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--seed-page",
        type=UUID,
        help=(
            "bench only this actor-visible Context page, as profile 'explicit', "
            "instead of selecting hub, median and isolated seeds"
        ),
    )
    parser.add_argument(
        "--project-tree-edges",
        action="store_true",
        help="project tree EdgeProjection rows into the database before timing",
    )
    parser.add_argument(
        "--add-hub-backlinks",
        type=int,
        metavar="N",
        help="insert N wikilink backlinks into --seed-page before timing",
    )
    parser.add_argument(
        "--sql-profile",
        action="store_true",
        help=(
            f"after timing, group the statements of {SQL_PROFILE_REQUESTS} "
            "untimed direct requests per seed by graph_rank_phase and time "
            "each group execute-only and through fetch"
        ),
    )
    parser.add_argument(
        "--_cold-child",
        choices=("service_direct", "fastapi_testclient"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_cold-seed",
        choices=(*DEGREE_PROFILES, EXPLICIT_PROFILE),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.add_hub_backlinks is not None:
        if args.seed_page is None:
            parser.error("--add-hub-backlinks requires --seed-page")
        if args.add_hub_backlinks < 1:
            parser.error("--add-hub-backlinks must be at least 1")
    # The parent prepares the corpus once; a cold child only measures it.
    if args._cold_child is not None and (
        args.project_tree_edges
        or args.add_hub_backlinks is not None
        or args.sql_profile
    ):
        parser.error("a cold child neither modifies nor profiles the database")
    return args


def main() -> int:
    args = parse_args()
    validate_common_args(
        runs=args.runs,
        warmup=args.warmup,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
    )
    cold_measurement_contract(args.cold_runs)
    if (args._cold_child is None) != (args._cold_seed is None):
        raise ValueError("cold child transport and seed must be supplied together")
    database_path = ensure_sqlite_database(args.db)
    corpus = load_corpus_metadata(
        database_path,
        workspace_id=args.workspace_id,
    )
    removed_indexes = [] if args._cold_child else drop_benchmark_indexes(database_path)
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    session_factory = make_session_factory(engine)
    corpus_mutations = (
        None
        if args._cold_child
        else prepare_corpus(
            session_factory,
            args.workspace_id,
            project_tree_edges=args.project_tree_edges,
            seed_page=args.seed_page,
            hub_backlinks=args.add_hub_backlinks,
        )
    )
    actor, actor_metadata = resolve_benchmark_actor(
        session_factory,
        workspace_id=args.workspace_id,
        actor_name=args.actor,
    )
    http_app = (
        None
        if args._cold_child == "service_direct"
        else _build_http_app(session_factory=session_factory, actor=actor)
    )
    with session_factory() as session:
        seeds = resolve_seed_profiles(
            session,
            args.workspace_id,
            actor=actor,
            seed_page=args.seed_page,
        )

    selected_seeds = (
        {args._cold_seed: seeds[args._cold_seed]}
        if args._cold_seed is not None
        else seeds
    )
    benchmarks: dict[str, object] = {}
    direct_operations: dict[str, Callable[[], OperationResult]] = {}
    failed = False
    child_sample: dict[str, object] | None = None
    try:
        for profile_name, profile in selected_seeds.items():
            request = _local_request(profile["page_id"])
            request_json = request.model_dump(mode="json", by_alias=True)
            service = GraphQueryService(graph_service=GraphService())
            # Read once, outside the timed call, the way the route reads the
            # owner's switch before it queries.
            with session_factory() as session:
                usage_signals = usage_relation_signals_enabled(
                    session, args.workspace_id
                )

            def direct_operation(
                *,
                selected_request: GraphQueryRequest = request,
                selected_service: GraphQueryService = service,
                selected_usage_signals: bool = usage_signals,
            ) -> OperationResult:
                with session_factory() as session:
                    started = time.perf_counter()
                    # The route reads the scene in one snapshot (F08-T4), so
                    # the direct transport pays for the same BEGIN.
                    acquire_consistent_read_snapshot(session)
                    payload = selected_service.query(
                        session,
                        args.workspace_id,
                        request=selected_request,
                        actor=actor,
                        usage_signals_enabled=selected_usage_signals,
                    )
                    query_ms = (time.perf_counter() - started) * 1_000
                encoding_started = time.perf_counter()
                payload_bytes = payload.model_dump_json().encode("utf-8")
                encoding_ms = (time.perf_counter() - encoding_started) * 1_000
                return OperationResult(
                    elapsed_ms=query_ms,
                    response_bytes=len(payload_bytes),
                    gzip_bytes=compressed_size(payload_bytes),
                    phases_ms={"encoding": encoding_ms},
                    details=_response_details(payload.model_dump(mode="json")),
                )

            direct_operations[profile_name] = direct_operation
            http_resources: ThreadLocalResource | None = None
            direct: dict[str, object] | None = None
            http: dict[str, object] | None = None
            try:
                if args._cold_child != "fastapi_testclient":
                    direct = run_benchmark(
                        direct_operation,
                        runs=args.runs,
                        warmup=args.warmup,
                        concurrency=args.concurrency,
                        timeout_seconds=args.timeout_seconds,
                    )
                if args._cold_child != "service_direct":
                    if http_app is None:
                        raise RuntimeError("local HTTP application is unavailable")
                    http_resources = ThreadLocalResource(
                        lambda request_json=request_json: LocalHttpHarness(
                            app=http_app,
                            workspace_id=args.workspace_id,
                            request_json=request_json,
                        )
                    )
                    selected_resources = http_resources
                    if selected_resources is None:
                        raise RuntimeError("HTTP benchmark resources are unavailable")
                    http = run_benchmark(
                        lambda resources=selected_resources: (
                            resources.get().operation()
                        ),
                        runs=args.runs,
                        warmup=args.warmup,
                        concurrency=args.concurrency,
                        timeout_seconds=args.timeout_seconds,
                    )
            finally:
                if http_resources is not None:
                    http_resources.close()
            if args._cold_child is not None:
                child_result = direct if args._cold_child == "service_direct" else http
                if child_result is None:
                    raise RuntimeError("local cold child produced no result")
                child_sample = child_result["samples"][0]
                break
            benchmarks[profile_name] = {
                "seed": {
                    "kind": "context_page",
                    "id": str(profile["page_id"]),
                    "degree": profile["degree"],
                    "selection": profile["selection"],
                },
                "request": request_json,
                "service_direct": direct,
                "fastapi_testclient": http,
            }
            failed = failed or any(
                benchmark_has_failures(result)
                for result in (direct, http)
                if result is not None
            )
        # Untimed and after every warm measurement, so the listeners never sit
        # inside a timed interval.
        if args.sql_profile:
            for profile_name, operation in direct_operations.items():
                benchmarks[profile_name]["sql_profile"] = {
                    "transport": "service_direct",
                    **profile_sql_requests(
                        engine,
                        operation,
                        requests=SQL_PROFILE_REQUESTS,
                    ),
                }
    finally:
        engine.dispose()

    if args._cold_child is not None:
        if child_sample is None:
            raise RuntimeError("local cold child did not select a seed")
        print("DTX_GRAPH_COLD_SAMPLE=" + json.dumps(child_sample, ensure_ascii=False))
        return 0 if child_sample["status"] == "completed" else 2

    cold_by_seed = {
        profile_name: run_cold_subprocesses(
            lambda transport, _index, profile_name=profile_name: _cold_command(
                args,
                database_path=database_path,
                seed=profile_name,
                transport=transport,
            ),
            transports=("service_direct", "fastapi_testclient"),
            runs=args.cold_runs,
            process_timeout_seconds=max(60.0, args.timeout_seconds + 30.0),
        )
        for profile_name in seeds
    }
    for profile_name, cold_result in cold_by_seed.items():
        benchmarks[profile_name]["cold"] = cold_result
        if args.cold_runs:
            failed = failed or any(
                report["errors"] or report["timeouts"]
                for report in cold_result["transports"].values()
            )

    result = result_document(
        tool="bench_local",
        database_path=database_path,
        workspace_id=args.workspace_id,
        corpus=corpus,
        body={
            "command": command_line(),
            "actor": actor_metadata,
            "parameters": {
                "runs": args.runs,
                "warmup": args.warmup,
                "concurrency": args.concurrency,
                "timeout_seconds": args.timeout_seconds,
                "cold_runs": args.cold_runs,
                "seed_page": None if args.seed_page is None else str(args.seed_page),
                "project_tree_edges": args.project_tree_edges,
                "add_hub_backlinks": args.add_hub_backlinks,
                "sql_profile": args.sql_profile,
            },
            "removed_benchmark_indexes": removed_indexes,
            "lifecycle_status_mutated": False,
            "corpus_mutations": corpus_mutations,
            "seed_selection": _seed_selection_metadata(args.seed_page),
            "latency_measurement": {
                "service_direct": (
                    "GraphQueryService.query only; Pydantic encoding is reported "
                    "as an exact phase and excluded from direct latency"
                ),
                "fastapi_testclient": (
                    "complete TestClient POST including authorization, response "
                    "validation, and encoding"
                ),
            },
            "benchmarks": benchmarks,
            "cold": {
                **cold_measurement_contract(args.cold_runs),
                "scope": "each local seed and transport",
                "per_seed": cold_by_seed,
            },
        },
    )
    output_path = (args.out or default_output_path("local")).resolve()
    write_result(output_path, result)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "seeds": {
                    name: {
                        "id": str(profile["page_id"]),
                        "degree": profile["degree"],
                    }
                    for name, profile in seeds.items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 2 if failed else 0


def _cold_command(
    args: argparse.Namespace,
    *,
    database_path: Path,
    seed: str,
    transport: str,
) -> list[str]:
    return [
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
        *(() if args.seed_page is None else ("--seed-page", str(args.seed_page))),
        "--_cold-child",
        transport,
        "--_cold-seed",
        seed,
    ]


def resolve_seed_profiles(
    session: Session,
    workspace_id: UUID,
    *,
    actor,
    seed_page: UUID | None,
) -> dict[str, dict[str, object]]:
    """Return the named seed alone, or the hub, median and isolated seeds."""

    if seed_page is None:
        return select_seed_profiles(session, workspace_id, actor=actor)
    page_ids, degrees = _visible_context_degrees(session, workspace_id, actor=actor)
    if seed_page not in set(page_ids):
        raise RuntimeError(
            f"--seed-page {seed_page} is not an actor-visible published Context page"
        )
    return {
        EXPLICIT_PROFILE: {
            "page_id": seed_page,
            "degree": degrees[seed_page],
            "selection": "explicit",
        }
    }


def select_seed_profiles(
    session: Session,
    workspace_id: UUID,
    *,
    actor,
) -> dict[str, dict[str, object]]:
    page_ids, degrees = _visible_context_degrees(session, workspace_id, actor=actor)
    ordered = sorted(page_ids, key=lambda page_id: (degrees[page_id], page_id))
    isolated = next(
        (page_id for page_id in ordered if degrees[page_id] == 0),
        None,
    )
    if isolated is None:
        raise RuntimeError(
            "local graph benchmark requires at least one degree-zero Context page"
        )
    median = ordered[(len(ordered) - 1) // 2]
    maximum_degree = max(degrees[page_id] for page_id in page_ids)
    hub = next(page_id for page_id in ordered if degrees[page_id] == maximum_degree)
    return {
        "hub": {
            "page_id": hub,
            "degree": degrees[hub],
            "selection": "maximum_degree",
        },
        "median": {
            "page_id": median,
            "degree": degrees[median],
            "selection": "lower_nearest_rank_degree",
        },
        "isolated": {
            "page_id": isolated,
            "degree": 0,
            "selection": "degree_zero",
        },
    }


def _visible_context_degrees(
    session: Session,
    workspace_id: UUID,
    *,
    actor,
) -> tuple[tuple[UUID, ...], defaultdict[UUID, int]]:
    visible_documents = WorkspaceGraphService._load_documents(
        session,
        workspace_id,
        actor,
    )
    page_ids = tuple(
        sorted(
            document.entity_id
            for document in visible_documents.values()
            if document.entity_kind == "context"
        )
    )
    if not page_ids:
        raise RuntimeError("local graph benchmark requires a published Context page")
    page_set = set(page_ids)
    visible_endpoints: set[tuple[str, UUID]] = set()
    for document in visible_documents.values():
        if document.entity_kind == "context":
            visible_endpoints.add(("context_page", document.entity_id))
            visible_endpoints.add(("context_page_version", document.head_id))
        else:
            visible_endpoints.add(("library_item", document.entity_id))
            visible_endpoints.add(("library_item_version", document.head_id))
    degrees: defaultdict[UUID, int] = defaultdict(int)
    rows = session.execute(
        select(
            EdgeProjection.source_kind,
            EdgeProjection.source_id,
            EdgeProjection.target_kind,
            EdgeProjection.target_id,
        )
        .where(EdgeProjection.workspace_id == workspace_id)
        .execution_options(yield_per=5_000)
    )
    for source_kind, source_id, target_kind, target_id in rows:
        if (source_kind, source_id) not in visible_endpoints:
            continue
        if (target_kind, target_id) not in visible_endpoints:
            continue
        if source_kind == "context_page" and source_id in page_set:
            degrees[source_id] += 1
        if target_kind == "context_page" and target_id in page_set:
            degrees[target_id] += 1
    return page_ids, degrees


def _seed_selection_metadata(seed_page: UUID | None) -> dict[str, object]:
    degree_basis = (
        "actor-visible published Context pages with incident current "
        "EdgeProjection rows; ties use UUID order"
    )
    if seed_page is not None:
        return {
            "mode": "explicit",
            "seed_page": str(seed_page),
            "degree_basis": degree_basis,
            "note": "hub, median and isolated selection skipped",
        }
    return {
        "mode": "degree_profiles",
        "degree_basis": degree_basis,
        "median_method": "lower nearest rank after sorting (degree, UUID)",
    }


def prepare_corpus(
    session_factory: sessionmaker[Session],
    workspace_id: UUID,
    *,
    project_tree_edges: bool,
    seed_page: UUID | None,
    hub_backlinks: int | None,
) -> dict[str, object]:
    """Apply the requested corpus changes, each in its own committed transaction."""

    tree: dict[str, int] | None = None
    backlinks: dict[str, object] | None = None
    if project_tree_edges:
        with session_factory.begin() as session:
            tree = project_tree_edge_rows(session, workspace_id)
    if hub_backlinks is not None:
        if seed_page is None:
            raise ValueError("hub backlinks require a seed page")
        with session_factory.begin() as session:
            inserted = add_hub_backlinks(
                session,
                workspace_id,
                seed_page_id=seed_page,
                count=hub_backlinks,
            )
        backlinks = _hub_backlink_summary(seed_page, hub_backlinks, inserted)
    return {
        "edge_projection_mutated": tree is not None or backlinks is not None,
        "tree_edge_projection": tree,
        "hub_backlinks": backlinks,
    }


def project_tree_edge_rows(session: Session, workspace_id: UUID) -> dict[str, int]:
    """Project tree rows once; the synthetic builder writes none.

    Uses the whole-workspace reindex helper, which replaces only ``tree`` rows.
    """

    before = _edge_kind_row_count(session, workspace_id, "tree")
    refresh_tree_edge_projection(session, workspace_id)
    return {
        "tree_rows_before": before,
        "tree_rows": _edge_kind_row_count(session, workspace_id, "tree"),
    }


def _edge_kind_row_count(session: Session, workspace_id: UUID, edge_kind: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(EdgeProjection)
            .where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == edge_kind,
            )
        )
        or 0
    )


def add_hub_backlinks(
    session: Session,
    workspace_id: UUID,
    *,
    seed_page_id: UUID,
    count: int,
) -> list[tuple[UUID, float]]:
    """Insert ``count`` wikilink backlinks into one seed page.

    Sources are the lowest-UUID published Context pages that are neither the
    seed nor already linking to it, and their weights cycle 1, 2, 3. A row's
    ``created_at`` and ``last_seen_at`` are its source page's published
    version ``created_at``, the version that would carry the link, so no wall
    clock reaches the corpus. Returns the inserted ``(source_id, weight)``
    pairs in insertion order.
    """

    if count < 1:
        raise ValueError("hub backlink count must be at least 1")
    published: dict[UUID, datetime] = {
        page_id: published_at
        for page_id, published_at in session.execute(
            select(ContextPage.id, ContextPageVersion.created_at)
            .join(
                ContextPageVersion,
                ContextPageVersion.id == ContextPage.current_published_version_id,
            )
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status == "published",
            )
        )
    }
    if seed_page_id not in published:
        raise ValueError(f"hub seed {seed_page_id} is not a published Context page")
    linkers = set(
        session.scalars(
            select(EdgeProjection.source_id).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_kind == "context_page",
                EdgeProjection.target_kind == "context_page",
                EdgeProjection.target_id == seed_page_id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
    )
    candidates = sorted(published.keys() - linkers - {seed_page_id})
    if len(candidates) < count:
        raise ValueError(
            f"--add-hub-backlinks {count} needs {count} published Context pages "
            f"that do not already link to the seed; found {len(candidates)}"
        )
    inserted: list[tuple[UUID, float]] = []
    for index, source_id in enumerate(candidates[:count]):
        weight = HUB_BACKLINK_WEIGHTS[index % len(HUB_BACKLINK_WEIGHTS)]
        published_at = published[source_id]
        session.add(
            EdgeProjection(
                id=uuid5(
                    HUB_BACKLINK_ID_NAMESPACE,
                    f"{workspace_id}:{seed_page_id}:{source_id}",
                ),
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source_id,
                target_kind="context_page",
                target_id=seed_page_id,
                edge_kind="wikilink",
                provenance="curated",
                weight=weight,
                occurred_at=None,
                source_ref=dict(HUB_BACKLINK_SOURCE_REF),
                last_seen_at=published_at,
                created_at=published_at,
            )
        )
        inserted.append((source_id, weight))
    session.flush()
    bump_edge_version(session, workspace_id)
    return inserted


def _hub_backlink_summary(
    seed_page: UUID,
    requested: int,
    inserted: Sequence[tuple[UUID, float]],
) -> dict[str, object]:
    source_ids = [str(source_id) for source_id, _weight in inserted]
    return {
        "seed_page": str(seed_page),
        "requested": requested,
        "inserted": len(inserted),
        "edge_kind": "wikilink",
        "provenance": "curated",
        "source_ref": dict(HUB_BACKLINK_SOURCE_REF),
        "weights": {
            f"{weight:g}": count
            for weight, count in sorted(Counter(w for _, w in inserted).items())
        },
        "first_source_id": source_ids[0] if source_ids else None,
        "last_source_id": source_ids[-1] if source_ids else None,
        "source_ids_sha256": hashlib.sha256(
            "\n".join(source_ids).encode("utf-8")
        ).hexdigest(),
    }


def sql_statement_group(
    statement: str,
    execution_options: Mapping[str, Any],
) -> str:
    """Name the --sql-profile group of one DBAPI statement."""

    phase = execution_options.get(SQL_PROFILE_PHASE_OPTION)
    if phase:
        return str(phase)
    if _EDGE_PROJECTION_TABLE.search(statement):
        return SQL_PROFILE_UNTAGGED_EDGE_PROJECTION
    return SQL_PROFILE_OTHER


def profile_sql_requests(
    engine: Engine,
    operation: Callable[[], object],
    *,
    requests: int,
    replay_repeats: int = SQL_PROFILE_REPLAY_REPEATS,
) -> dict[str, object]:
    """Run ``operation`` ``requests`` times and group each run's statements.

    ``execute_only_ms`` spans ``before_cursor_execute`` to
    ``after_cursor_execute``: the DBAPI execute call alone. pysqlite returns
    from it at a SELECT's first row, so a streaming statement (an
    index-ordered GROUP BY, an IN-list hydration) shows a small part of its
    cost there, while a blocking sort shows most of its own.
    ``fetch_inclusive_ms`` closes that gap: after the runs, each distinct
    statement and parameter set that returned a result set is replayed on a
    raw DBAPI connection through ``fetchall``, and the fastest of
    ``replay_repeats`` replays is charged to every request that issued it.
    Each distinct edge_projection statement is also explained once, with the
    parameters of its first execution.
    """

    if requests < 1:
        raise ValueError("requests must be at least 1")
    if replay_repeats < 1:
        raise ValueError("replay_repeats must be at least 1")
    # One (group, execute ms, replay key) per statement of the running request.
    current: list[tuple[str, float, tuple[str, str, str] | None]] = []
    observed: list[list[tuple[str, float, tuple[str, str, str] | None]]] = []
    statements: dict[str, dict[str, Any]] = {}
    replays: dict[tuple[str, str, str], Any] = {}

    def before(conn, _cursor, _statement, _parameters, _context, _executemany):
        conn.info.setdefault(_SQL_PROFILE_STARTED, []).append(time.perf_counter())

    def after(conn, cursor, statement, parameters, context, executemany):
        finished = time.perf_counter()
        elapsed_ms = (finished - conn.info[_SQL_PROFILE_STARTED].pop()) * 1_000
        options = context.execution_options if context is not None else {}
        group = sql_statement_group(statement, options)
        replay_key = None
        # Only a statement with a result set is replayed; any other statement
        # is complete when execute returns.
        if not executemany and cursor.description is not None:
            replay_key = (group, statement, repr(parameters))
            replays.setdefault(replay_key, parameters)
        current.append((group, elapsed_ms, replay_key))
        if not executemany and _EDGE_PROJECTION_TABLE.search(statement):
            entry = statements.setdefault(
                statement,
                {"group": group, "parameters": parameters, "executions": 0},
            )
            entry["executions"] += 1

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        for _ in range(requests):
            current.clear()
            operation()
            observed.append(list(current))
    finally:
        event.remove(engine, "after_cursor_execute", after)
        event.remove(engine, "before_cursor_execute", before)

    replayed = _replay_statements(engine, replays, repeats=replay_repeats)
    groups = sorted({group for request in observed for group, _, _ in request})
    per_request = [
        _request_group_totals(request, groups, replayed) for request in observed
    ]
    return {
        "requests": requests,
        "phase_option": SQL_PROFILE_PHASE_OPTION,
        "replay_repeats": replay_repeats,
        "timing": {
            "execute_only_ms": (
                "DBAPI cursor execute per statement (before_cursor_execute to "
                "after_cursor_execute); pysqlite returns at a SELECT's first "
                "row, so row fetch and ORM loading are excluded"
            ),
            "fetch_inclusive_ms": (
                "each distinct statement and parameter set with a result set, "
                "replayed after the requests on a raw DBAPI connection: execute "
                f"plus fetchall, fastest of {replay_repeats}; a statement "
                "without a result set keeps its execute_only_ms; ORM loading is "
                "excluded"
            ),
            "rows": "rows fetched by those replays",
        },
        "groups": groups,
        "per_request": per_request,
        "summary": {
            group: {
                "statements": distribution(
                    [request[group]["statements"] for request in per_request]
                ),
                "execute_only_ms": _ms_distribution(
                    [request[group]["execute_only_ms"] for request in per_request]
                ),
                "fetch_inclusive_ms": _ms_distribution(
                    [request[group]["fetch_inclusive_ms"] for request in per_request]
                ),
                "rows": distribution(
                    [request[group]["rows"] for request in per_request]
                ),
            }
            for group in groups
        },
        "explain_query_plan": _explain_query_plans(engine, statements),
    }


def _replay_statements(
    engine: Engine,
    replays: Mapping[tuple[str, str, str], Any],
    *,
    repeats: int,
) -> dict[tuple[str, str, str], tuple[float, int]]:
    """Time each replay key through ``fetchall``: (fastest ms, rows)."""

    timings: dict[tuple[str, str, str], tuple[float, int]] = {}
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        sqlite = engine.dialect.name == "sqlite"
        # A replay only reads; SQLite refuses a write instead of repeating it.
        if sqlite:
            cursor.execute("PRAGMA query_only = ON")
        try:
            for replay_key, parameters in replays.items():
                fastest_ms = float("inf")
                rows = 0
                for _ in range(repeats):
                    started = time.perf_counter()
                    cursor.execute(replay_key[1], parameters)
                    rows = len(cursor.fetchall())
                    elapsed_ms = (time.perf_counter() - started) * 1_000
                    fastest_ms = min(fastest_ms, elapsed_ms)
                timings[replay_key] = (fastest_ms, rows)
        finally:
            if sqlite:
                cursor.execute("PRAGMA query_only = OFF")
            cursor.close()
    finally:
        connection.close()
    return timings


def _request_group_totals(
    executions: Sequence[tuple[str, float, tuple[str, str, str] | None]],
    groups: Sequence[str],
    replayed: Mapping[tuple[str, str, str], tuple[float, int]],
) -> dict[str, dict[str, object]]:
    """Sum one request's statements per group; absent groups are zero."""

    totals = {
        group: {"statements": 0, "execute_ms": 0.0, "fetch_ms": 0.0, "rows": 0}
        for group in groups
    }
    for group, execute_ms, replay_key in executions:
        total = totals[group]
        total["statements"] += 1
        total["execute_ms"] += execute_ms
        if replay_key is None:
            total["fetch_ms"] += execute_ms
        else:
            fetch_ms, rows = replayed[replay_key]
            total["fetch_ms"] += fetch_ms
            total["rows"] += rows
    return {
        group: {
            "statements": total["statements"],
            "execute_only_ms": round(total["execute_ms"], 6),
            "fetch_inclusive_ms": round(total["fetch_ms"], 6),
            "rows": total["rows"],
        }
        for group, total in totals.items()
    }


def _ms_distribution(values: Sequence[float]) -> dict[str, float]:
    return {
        name: round(float(value), 6)
        for name, value in (distribution(values) or {}).items()
    }


def edge_projection_aliases(statement: str) -> list[str]:
    """Return the aliases ``statement`` gives the edge_projection table."""

    aliases: list[str] = []
    for match in _EDGE_PROJECTION_ALIAS.finditer(statement):
        explicit, alias = match.groups()
        if explicit is None and alias.upper() in _NOT_AN_ALIAS:
            continue
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def plan_scans_edge_projection(
    plan_details: Sequence[str],
    aliases: Sequence[str],
) -> bool:
    """Whether a plan line reads all of edge_projection, by name or alias.

    ``SCAN x USING INDEX`` and ``USING COVERING INDEX`` still visit every
    index entry, and ``SEARCH x USING AUTOMATIC INDEX`` reads the whole table
    to build that index, so all of them count; any other ``SEARCH`` does not.
    Names compare without case or schema (``main.edge_projection``).
    """

    names = {name.lower() for name in ("edge_projection", *aliases)}
    for detail in plan_details:
        words = detail.split()
        # SQLite before 3.36 printed "SCAN TABLE edge_projection AS d".
        if words[1:2] == ["TABLE"]:
            words = [words[0], *words[2:]]
        if len(words) < 2:
            continue
        table = words[1].rsplit(".", 1)[-1].strip('"').lower()
        if table not in names:
            continue
        if words[0] == "SCAN":
            return True
        if words[0] == "SEARCH" and words[2:4] == ["USING", "AUTOMATIC"]:
            return True
    return False


def _explain_query_plans(
    engine: Engine,
    statements: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    if engine.dialect.name != "sqlite":
        return []
    plans: list[dict[str, object]] = []
    with engine.connect() as connection:
        for statement, entry in statements.items():
            parameters = entry["parameters"]
            if isinstance(parameters, list):
                parameters = tuple(parameters)
            rows = connection.exec_driver_sql(
                f"EXPLAIN QUERY PLAN {statement}",
                parameters,
            ).all()
            aliases = edge_projection_aliases(statement)
            plans.append(
                {
                    "group": entry["group"],
                    "executions": entry["executions"],
                    "statement": statement,
                    "edge_projection_aliases": aliases,
                    "edge_projection_scan": plan_scans_edge_projection(
                        [row[3] for row in rows], aliases
                    ),
                    "plan": [
                        {"id": row[0], "parent": row[1], "detail": row[3]}
                        for row in rows
                    ],
                }
            )
    return plans


def _local_request(page_id: object) -> GraphQueryRequest:
    if not isinstance(page_id, UUID):
        raise TypeError("local graph seed must be a UUID")
    return GraphQueryRequest(
        seeds=[GraphSeed(kind="context_page", id=page_id)],
        time=GraphTimeQuery(mode="appearance"),
        budget=GraphBudget(nodes=50, edges=150),
    )


def _database_url(session_factory: sessionmaker[Session]) -> str:
    engine = session_factory.kw.get("bind")
    if engine is None:
        raise RuntimeError("benchmark session factory has no engine")
    return str(engine.url)


def _build_http_app(*, session_factory: sessionmaker[Session], actor):
    settings = Settings.model_construct(
        database_url=_database_url(session_factory),
        session_secret="graph-benchmark-session-secret-0001",
        auto_bootstrap=False,
    )
    container = assemble(settings, session_factory=session_factory)
    app = create_app(
        settings=settings,
        container=container,
        include_llms_txt=False,
    )
    resolved_http_actor = http_benchmark_actor(actor)
    app.dependency_overrides[require_token_scope] = lambda: resolved_http_actor
    return app


def _response_details(data: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": data["schema_version"],
        "nodes": len(data["nodes"]),
        "edges": len(data["edges"]),
        "edges_by_kind": dict(
            sorted(Counter(edge["edge_kind"] for edge in data["edges"]).items())
        ),
        "completeness": data["completeness"],
        "truncated": data["truncated"],
        "warnings": data["warnings"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
