"""One corpus CLI traversal verifies the published database and manifest."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from backend.app.bootstrap import current_alembic_head
from backend.tests.utils.environment import child_env_without_dtxwiki_config
from backend.tests.utils.graph_bench_manifest import (
    file_sha256,
    sqlite_content_digest,
    sqlite_schema_digest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_corpus_cli_publishes_a_migrated_database_bound_to_its_manifest(tmp_path):
    # One SQLite configuration and one CLI execution. Real migrations and the
    # persisted database are needed to detect a stale or pre-commit manifest;
    # the A-owned planner/materializer tests do not publish CLI artifacts.
    database = tmp_path / "corpus.db"
    environment = child_env_without_dtxwiki_config()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            str(REPOSITORY_ROOT / "scripts/bench/graph/build_corpus.py"),
            "--documents",
            "40",
            "--profile",
            "synthetic",
            "--seed",
            "260917",
            "--out",
            str(database),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    manifest_path = Path(f"{database}.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert summary["database"] == str(database)
    assert summary["manifest"] == str(manifest_path)
    assert manifest["application"]["alembic_revision"] == current_alembic_head()
    assert manifest["database"]["sha256"] == file_sha256(database)
    assert manifest["database"]["schema_sha256"] == sqlite_schema_digest(database)
    assert manifest["database"]["content_sha256"] == sqlite_content_digest(database)
    assert manifest["database"]["validation"] == {
        "foreign_key_violation_count": 0,
        "integrity_check": "ok",
    }
    assert len(manifest["workspaces"]) == 1
    workspace = manifest["workspaces"][0]
    assert workspace["contexts"] + workspace["library"] == 40
    assert [principal["name"] for principal in workspace["principals"]] == [
        "owner",
        "viewer",
        "restricted_reader",
    ]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 0
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == manifest["application"]["alembic_revision"]
        )
