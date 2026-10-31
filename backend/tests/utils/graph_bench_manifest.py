"""Manifest primitives shared by tracked Graph View benchmark scripts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy
from sqlalchemy import create_engine, inspect

GRAPH_BENCH_MANIFEST_VERSION = "dtxwiki.graph-bench-corpus.v1"


def sqlite_schema_records(database_path: Path) -> list[dict[str, object]]:
    """Return a canonical semantic inventory of the SQLite schema.

    Raw ``CREATE TABLE`` text is not stable across fresh processes: SQLAlchemy
    may emit equivalent named constraints in a different clause order when
    hash randomization changes.  Inspector records retain every relevant
    column, constraint, foreign key, and index while sorting unordered groups.
    """

    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        records: list[dict[str, object]] = []
        for table_name in sorted(inspector.get_table_names()):
            if table_name.startswith("sqlite_"):
                continue
            records.append(
                {
                    "type": "table",
                    "name": table_name,
                    "columns": [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": bool(column["nullable"]),
                            "default": column.get("default"),
                        }
                        for column in inspector.get_columns(table_name)
                    ],
                    "primary_key": inspector.get_pk_constraint(table_name).get(
                        "constrained_columns", []
                    ),
                    "unique_constraints": _sorted_named_records(
                        inspector.get_unique_constraints(table_name)
                    ),
                    "check_constraints": _sorted_named_records(
                        inspector.get_check_constraints(table_name)
                    ),
                    "foreign_keys": _sorted_named_records(
                        inspector.get_foreign_keys(table_name)
                    ),
                    "indexes": _sorted_named_records(inspector.get_indexes(table_name)),
                }
            )
        with closing(sqlite3.connect(database_path)) as connection:
            for name, table_name, sql in connection.execute(
                """
                SELECT name, tbl_name, sql
                FROM sqlite_schema
                WHERE type = 'table'
                  AND lower(ltrim(COALESCE(sql, ''))) LIKE 'create virtual table%'
                ORDER BY name, tbl_name
                """
            ):
                records.append(
                    {
                        "type": "virtual_table_definition",
                        "name": name,
                        "table": table_name,
                        "sql": " ".join(sql.split()),
                    }
                )
            table_names = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]
            for table_name in table_names:
                index_records = []
                quoted_table = _quote_identifier(table_name)
                for index_row in connection.execute(
                    f"PRAGMA index_list({quoted_table})"
                ):
                    index_name = str(index_row[1])
                    origin = str(index_row[3])
                    sql_row = connection.execute(
                        "SELECT sql FROM sqlite_schema "
                        "WHERE type = 'index' AND name = ?",
                        (index_name,),
                    ).fetchone()
                    raw_sql = None if sql_row is None else sql_row[0]
                    index_records.append(
                        {
                            "type": "index_semantics",
                            "table": table_name,
                            "name": index_name if origin == "c" else None,
                            "unique": bool(index_row[2]),
                            "origin": origin,
                            "partial": bool(index_row[4]),
                            "key_columns": [
                                {
                                    "column": xinfo[2],
                                    "descending": bool(xinfo[3]),
                                    "collation": xinfo[4],
                                }
                                for xinfo in connection.execute(
                                    "PRAGMA index_xinfo("
                                    f"{_quote_identifier(index_name)})"
                                )
                                if bool(xinfo[5])
                            ],
                            "sql": (
                                " ".join(str(raw_sql).split())
                                if origin == "c" and raw_sql is not None
                                else None
                            ),
                        }
                    )
                records.extend(
                    sorted(index_records, key=lambda record: _canonical_json(record))
                )
            for object_type, name, table_name, sql in connection.execute(
                """
                SELECT type, name, tbl_name, COALESCE(sql, '')
                FROM sqlite_schema
                WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%'
                ORDER BY type, name, tbl_name
                """
            ):
                records.append(
                    {
                        "type": object_type,
                        "name": name,
                        "table": table_name,
                        "sql": " ".join(sql.split()),
                    }
                )
        return records
    finally:
        engine.dispose()


def sqlite_schema_digest(database_path: Path) -> str:
    encoded = _canonical_json(sqlite_schema_records(database_path)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sqlite_content_digest(database_path: Path) -> str:
    """Hash logical table contents independently of SQLite page/DDL ordering."""

    digest = hashlib.sha256()
    with closing(sqlite3.connect(database_path)) as connection:
        virtual_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table'
                  AND lower(ltrim(COALESCE(sql, ''))) LIKE 'create virtual table%'
                """
            )
        }
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
            if not any(
                str(row[0]) != root and str(row[0]).startswith(f"{root}_")
                for root in virtual_tables
            )
        ]
        for table_name in table_names:
            quoted_table = _quote_identifier(table_name)
            column_records = list(
                connection.execute(f"PRAGMA table_info({quoted_table})")
            )
            columns = [str(record[1]) for record in column_records]
            if not columns:
                continue
            primary_key_columns = [
                str(record[1])
                for record in sorted(
                    (record for record in column_records if int(record[5]) > 0),
                    key=lambda record: int(record[5]),
                )
            ]
            order_columns = primary_key_columns or columns
            quoted_columns = ", ".join(_quote_identifier(name) for name in columns)
            quoted_order = ", ".join(_quote_identifier(name) for name in order_columns)
            digest.update(table_name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_canonical_json(columns).encode("utf-8"))
            digest.update(b"\n")
            rows = connection.execute(
                f"SELECT {quoted_columns} FROM {quoted_table} ORDER BY {quoted_order}"
            )
            for row in rows:
                digest.update(
                    _canonical_json([_sqlite_value(value) for value in row]).encode(
                        "utf-8"
                    )
                )
                digest.update(b"\n")
    return digest.hexdigest()


def sqlite_row_counts(
    database_path: Path,
    *,
    workspace_ids: Sequence[str],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Count every application table and every workspace-scoped table."""

    totals: dict[str, int] = {}
    by_workspace = {workspace_id: {} for workspace_id in workspace_ids}
    with closing(sqlite3.connect(database_path)) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        for table_name in table_names:
            quoted_table = _quote_identifier(table_name)
            totals[table_name] = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            )
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            if "workspace_id" not in columns:
                continue
            for workspace_id in workspace_ids:
                stored_workspace_ids = (UUID(workspace_id).hex, workspace_id)
                by_workspace[workspace_id][table_name] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {quoted_table} "
                        "WHERE workspace_id IN (?, ?)",
                        stored_workspace_ids,
                    ).fetchone()[0]
                )
    return totals, by_workspace


def checkpoint_sqlite(database_path: Path) -> None:
    """Move committed WAL pages into the main file before hashing it."""

    with closing(sqlite3.connect(database_path)) as connection:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None or result[0] != 0 or result[1] != result[2]:
        raise RuntimeError(f"benchmark corpus WAL checkpoint failed: {result!r}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sqlite_integrity(database_path: Path) -> dict[str, object]:
    """Fail closed unless SQLite structure and foreign keys are intact."""

    with closing(sqlite3.connect(database_path)) as connection:
        integrity_rows = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_key_violations = list(connection.execute("PRAGMA foreign_key_check"))
    if integrity_rows != ["ok"]:
        raise RuntimeError(
            "benchmark corpus failed SQLite integrity_check: "
            + "; ".join(integrity_rows[:10])
        )
    if foreign_key_violations:
        raise RuntimeError(
            "benchmark corpus failed SQLite foreign_key_check: "
            f"{len(foreign_key_violations)} violation(s)"
        )
    return {
        "integrity_check": "ok",
        "foreign_key_violation_count": 0,
    }


def git_fingerprint(
    repository_root: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, object]:
    commit = _git(repository_root, "rev-parse", "HEAD")
    tracked_digest, tracked_bytes = _git_sha256(
        repository_root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "HEAD",
        "--",
    )
    excluded_git_paths = {
        relative_path
        for path in excluded_paths
        if (relative_path := _git_relative_path(repository_root, path)) is not None
    }
    untracked_paths = sorted(
        path
        for path in _git_bytes(
            repository_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split(b"\0")
        if path and path not in excluded_git_paths
    )
    untracked_digest = hashlib.sha256()
    for encoded_path in untracked_paths:
        path = repository_root / os.fsdecode(encoded_path)
        entry_digest = hashlib.sha256()
        if path.is_symlink():
            entry_kind = b"symlink"
            entry_digest.update(os.fsencode(os.readlink(path)))
        else:
            entry_kind = b"file"
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    entry_digest.update(chunk)
        _hash_framed(untracked_digest, encoded_path)
        _hash_framed(untracked_digest, entry_kind)
        _hash_framed(untracked_digest, entry_digest.digest())
    untracked_sha256 = untracked_digest.hexdigest()
    worktree_digest = hashlib.sha256()
    _hash_framed(worktree_digest, tracked_digest.encode("ascii"))
    _hash_framed(worktree_digest, untracked_sha256.encode("ascii"))
    return {
        "commit": commit,
        "dirty": bool(tracked_bytes or untracked_paths),
        "worktree_sha256": worktree_digest.hexdigest(),
        "tracked_diff_sha256": tracked_digest,
        "untracked_sha256": untracked_sha256,
        "untracked_file_count": len(untracked_paths),
    }


def runtime_fingerprint() -> dict[str, str]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
        "sqlite": sqlite3.sqlite_version,
        "sqlalchemy": sqlalchemy.__version__,
    }


def build_corpus_manifest(
    *,
    database_path: Path,
    database_name: str | None = None,
    repository_root: Path,
    generator_version: str,
    arguments: Mapping[str, object],
    app_version: str,
    alembic_head: str,
    alembic_revision: str,
    workspaces: Sequence[Mapping[str, Any]],
    source_fingerprint: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a self-verifying manifest after corpus transactions commit."""

    checkpoint_sqlite(database_path)
    workspace_ids = [str(workspace["id"]) for workspace in workspaces]
    row_counts, workspace_row_counts = sqlite_row_counts(
        database_path,
        workspace_ids=workspace_ids,
    )
    workspace_records = []
    for workspace in workspaces:
        workspace_id = str(workspace["id"])
        workspace_records.append(
            {
                **workspace,
                "row_counts": workspace_row_counts[workspace_id],
            }
        )
    return {
        "manifest_version": GRAPH_BENCH_MANIFEST_VERSION,
        "generator_version": generator_version,
        "arguments": dict(arguments),
        "source": dict(source_fingerprint or git_fingerprint(repository_root)),
        "runtime": runtime_fingerprint(),
        "application": {
            "version": app_version,
            "alembic_head": alembic_head,
            "alembic_revision": alembic_revision,
        },
        "database": {
            "path": database_name or database_path.name,
            "bytes": database_path.stat().st_size,
            "sha256": file_sha256(database_path),
            "schema_sha256": sqlite_schema_digest(database_path),
            "content_sha256": sqlite_content_digest(database_path),
            "row_counts": row_counts,
            "validation": verify_sqlite_integrity(database_path),
        },
        "workspaces": workspace_records,
    }


def write_corpus_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sorted_named_records(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized = [
        {
            key: _json_safe(value)
            for key, value in record.items()
            if key != "duplicates_constraint"
        }
        for record in records
    ]
    return sorted(
        normalized,
        key=lambda record: (
            str(record.get("name") or ""),
            _canonical_json(record),
        ),
    )


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sqlite_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _git(repository_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(repository_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _git_sha256(repository_root: Path, *arguments: str) -> tuple[str, int]:
    command = ["git", *arguments]
    process = subprocess.Popen(
        command,
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
        byte_count += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(
            return_code,
            command,
            stderr=stderr,
        )
    return digest.hexdigest(), byte_count


def _git_relative_path(repository_root: Path, path: Path) -> bytes | None:
    try:
        relative = path.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return None
    return os.fsencode(relative.as_posix())


def _hash_framed(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)
