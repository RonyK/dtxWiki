#!/usr/bin/env python3
"""Measure Chats list service queries against disposable synthetic SQLite data.

Run from a checkout with ``.venv/bin/python scripts/bench/chats_list.py``.
The default corpus contains 50 chats reading one published page 100 times each,
plus 100,000 unrelated calls in the same workspace. This measures list service
execution and response construction, excluding seeding, HTTP, authentication,
browser rendering, and migration execution. No existing database is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

import backend.models  # noqa: E402, F401 - register the complete ORM schema
from backend.models.base import Base  # noqa: E402
from backend.models.context import ContextPage, ContextPageVersion  # noqa: E402
from backend.models.mcp_chat import (  # noqa: E402
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall  # noqa: E402
from backend.models.workspace import MCPClient, Workspace  # noqa: E402
from backend.services.mcp_chat_service import McpChatService  # noqa: E402
from backend.spi.types import ActorContext  # noqa: E402


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chats", type=_positive, default=50)
    parser.add_argument("--reads-per-chat", type=_nonnegative, default=100)
    parser.add_argument("--outside-calls", type=_nonnegative, default=100_000)
    parser.add_argument("--limit", type=_positive, default=50)
    parser.add_argument("--repeat", type=_positive, default=2)
    parser.add_argument(
        "--actor", choices=("editor", "internal", "owner"), default="editor"
    )
    args = parser.parse_args()
    if args.limit > 100:
        parser.error("--limit must be at most 100, matching the REST API")
    return args


def _source_state() -> dict:
    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=REPOSITORY_ROOT, text=True
        ).rstrip("\n")

    source_paths = (
        "backend/services/mcp_chat_service.py",
        "backend/repositories/mcp_chat_projection.py",
        "backend/models/mcp_trace.py",
        "scripts/bench/chats_list.py",
    )
    return {
        "commit": git("rev-parse", "HEAD"),
        "committed_tree": git("rev-parse", "HEAD^{tree}"),
        "branch": git("branch", "--show-current"),
        "working_tree_status": git("status", "--porcelain").splitlines(),
        "source_sha256": {
            name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest()
            for name in source_paths
            if (REPOSITORY_ROOT / name).is_file()
        },
    }


def _seed(factory, args):
    workspace_id, client_id, page_id, version_id = (uuid4() for _ in range(4))
    base = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)
    with factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="chats-benchmark",
                title="Chats benchmark",
                name="Chats benchmark",
            )
        )
        session.add(
            MCPClient(
                id=client_id,
                workspace_id=workspace_id,
                name="Benchmark client",
                client_type="system_managed",
                scopes=["*"],
                status="active",
            )
        )
        session.add(
            ContextPage(
                id=page_id,
                workspace_id=workspace_id,
                slug="shared-page",
                title="Shared published page",
                page_type="overview",
                lifecycle_status="published",
            )
        )
        session.flush()
        session.add(
            ContextPageVersion(
                id=version_id,
                workspace_id=workspace_id,
                page_id=page_id,
                version_no=1,
                body_md="# Shared page\n\nSynthetic benchmark content.",
                content_hash="chats-benchmark-v1",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="benchmark",
                version_status="published_snapshot",
            )
        )
        session.flush()
        page = session.get(ContextPage, page_id)
        page.current_version_id = page.current_published_version_id = version_id
        session.flush()

        def call_row(session_id, *, call_id=None, started_at=base):
            return {
                "id": call_id or uuid4(),
                "workspace_id": workspace_id,
                "client_id": client_id,
                "mcp_session_id": session_id,
                "request_id": uuid4(),
                "tool_name": "read",
                "input_summary": {
                    "target_type": "context_page",
                    "target_id": str(page_id),
                },
                "result_status": "succeeded",
                "started_at": started_at,
            }

        for chat_index in range(args.chats):
            chat_id, session_id = uuid4(), uuid4()
            session.execute(
                insert(McpChatSession),
                [
                    {
                        "id": chat_id,
                        "workspace_id": workspace_id,
                        "client_id": client_id,
                        "mcp_session_id": str(session_id),
                        "started_at": base,
                        "last_activity_at": base + timedelta(seconds=chat_index),
                        "tool_call_count": args.reads_per_chat,
                        "write_count": 0,
                        "error_count": 0,
                    }
                ],
            )
            calls, edges = [], []
            for call_index in range(args.reads_per_chat):
                call_id = uuid4()
                occurred_at = base + timedelta(seconds=call_index)
                calls.append(
                    call_row(session_id, call_id=call_id, started_at=occurred_at)
                )
                edges.append(
                    {
                        "id": uuid4(),
                        "workspace_id": workspace_id,
                        "chat_session_id": chat_id,
                        "mcp_tool_call_id": call_id,
                        "resource_kind": "context_page",
                        "resource_id": page_id,
                        "verb": "referenced",
                        "occurred_at": occurred_at,
                    }
                )
            if calls:
                session.execute(insert(MCPToolCall), calls)
                session.execute(insert(McpChatToolCallArtifact), edges)
                session.execute(
                    insert(McpChatArtifactLink),
                    [
                        {
                            "id": uuid4(),
                            "workspace_id": workspace_id,
                            "chat_session_id": chat_id,
                            "resource_kind": "context_page",
                            "resource_id": page_id,
                            "verb": "referenced",
                            "first_occurred_at": base,
                            "last_occurred_at": occurred_at,
                            "occurrence_count": args.reads_per_chat,
                            "last_mcp_tool_call_id": call_id,
                        }
                    ],
                )
        # Bulk inserts keep seed time and memory separate from list measurements.
        for offset in range(0, args.outside_calls, 5_000):
            session.execute(
                insert(MCPToolCall),
                [
                    call_row(uuid4())
                    for _ in range(min(5_000, args.outside_calls - offset))
                ],
            )
    return workspace_id


@dataclass
class _FetchCounter:
    enabled: bool = False
    select_rows_fetched: int = 0


class _CountingCursor(sqlite3.Cursor):
    _select_result = False

    def execute(self, statement, parameters=()):
        result = super().execute(statement, parameters)
        first_word = statement.lstrip().split(None, 1)[0].upper()
        self._select_result = first_word in {"SELECT", "WITH"}
        return result

    def executemany(self, statement, parameters):
        self._select_result = False
        return super().executemany(statement, parameters)

    def _count(self, count: int) -> None:
        counter = self.connection.fetch_counter
        if counter.enabled and self._select_result:
            counter.select_rows_fetched += count

    def fetchone(self):
        row = super().fetchone()
        self._count(int(row is not None))
        return row

    def fetchmany(self, size=None):
        rows = super().fetchmany() if size is None else super().fetchmany(size)
        self._count(len(rows))
        return rows

    def fetchall(self):
        rows = super().fetchall()
        self._count(len(rows))
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = super().__next__()
        self._count(1)
        return row


class _CountingConnection(sqlite3.Connection):
    fetch_counter: _FetchCounter

    def cursor(self, factory=_CountingCursor):
        return super().cursor(factory)


class _MeasuredChatService(McpChatService):
    def __init__(self):
        super().__init__()
        self.projection_row_counts = []

    def _load_projection(self, *args, **kwargs):
        projection = super()._load_projection(*args, **kwargs)
        self.projection_row_counts.append(projection.row_counts)
        return projection


def _measure(factory, workspace_id, args, fetch_counter: _FetchCounter) -> dict:
    service = _MeasuredChatService()
    sql_count = 0
    sql_seconds = 0.0
    query_tables = Counter()
    orm_rows = Counter()
    reader_id = uuid4()
    actor = (
        None
        if args.actor == "internal"
        else ActorContext(
            actor_kind="human",
            actor_id=str(reader_id),
            user_id=reader_id,
            token_kind="session",
            scopes=("read", "write", "admin")
            if args.actor == "owner"
            else ("read", "write"),
            workspace_id=workspace_id,
            workspace_role=args.actor,
            role=args.actor,
            provenance="browser_session",
        )
    )

    def before(_connection, _cursor, statement, _parameters, context, _many):
        nonlocal sql_count
        sql_count += 1
        context._chats_benchmark_started = time.perf_counter()
        match = re.search(r"\bFROM\s+([a-z_]+)", statement, re.IGNORECASE)
        query_tables[match.group(1) if match else statement.split()[0]] += 1

    def after(_connection, _cursor, _statement, _parameters, context, _many):
        nonlocal sql_seconds
        sql_seconds += time.perf_counter() - context._chats_benchmark_started

    def loaded(_session, instance):
        orm_rows[type(instance).__name__] += 1

    with factory() as session:
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        event.listen(session, "loaded_as_persistent", loaded)
        fetch_counter.select_rows_fetched = 0
        fetch_counter.enabled = True
        started = time.perf_counter()
        try:
            result = service.list_chats(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
                limit=args.limit,
            )
        finally:
            wall_seconds = time.perf_counter() - started
            fetch_counter.enabled = False
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "after_cursor_execute", after)
            event.remove(session, "loaded_as_persistent", loaded)
    return {
        "wall_seconds": round(wall_seconds, 6),
        "sql_count": sql_count,
        "sql_execute_seconds": round(sql_seconds, 6),
        "dbapi_select_rows_fetched": fetch_counter.select_rows_fetched,
        "sql_queries_by_first_from_table": dict(query_tables),
        "loaded_orm_rows": dict(orm_rows),
        "projection_row_counts": service.projection_row_counts,
        "returned_items": len(result.items),
        "next_cursor": result.next_cursor,
    }


def main() -> int:
    args = _parse_args()
    fetch_counter = _FetchCounter()

    def open_database():
        connection = sqlite3.connect(":memory:", factory=_CountingConnection)
        connection.fetch_counter = fetch_counter
        return connection

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", creator=open_database, poolclass=StaticPool
    )
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        workspace_id = _seed(factory, args)
        result = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "source": _source_state(),
            "runtime": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "sqlalchemy": sqlalchemy.__version__,
                "platform": platform.platform(),
            },
            "corpus": {
                "chats": args.chats,
                "reads_per_chat": args.reads_per_chat,
                "outside_calls": args.outside_calls,
                "total_trace_rows": args.chats * args.reads_per_chat
                + args.outside_calls,
                "pages": 1,
                "actor": args.actor,
                "limit": args.limit,
            },
            "measurement_boundary": "list service and response construction",
            "sql_timing_boundary": "cursor execute; excludes fetching and conversion",
            "row_count_boundary": "DBAPI SELECT/CTE rows fetched during measurement",
            "database": "disposable in-memory SQLite with current ORM schema",
            "measurements": [
                _measure(factory, workspace_id, args, fetch_counter)
                for _ in range(args.repeat)
            ],
        }
        print(json.dumps(result, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
