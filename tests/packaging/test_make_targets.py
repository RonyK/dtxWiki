"""Makefile targets operators and developers run against a real database."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from backend.app.bootstrap import current_alembic_head

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None, reason="make is unavailable"
)


def migrate_target_problems(makefile: Path, workdir: Path) -> list[str]:
    """Run `make migrate` against a fresh SQLite URL; report what went wrong.

    M-18(f): the target used to run `alembic upgrade head`, which migrates the
    URL in alembic.ini and silently ignores DTXWIKI_DATABASE_URL.
    """

    database = workdir / "dev.sqlite"
    env = {
        **os.environ,
        "DTXWIKI_DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "DTXWIKI_DATA_DIR": str(workdir / "data"),
    }
    result = subprocess.run(
        [
            "make", "-f", str(makefile), "--no-print-directory", "migrate",
            f"VENV={sys.prefix}",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        return [f"make migrate failed: {(result.stdout + result.stderr)[-2000:]}"]
    if not database.exists():
        return ["DTXWIKI_DATABASE_URL was not migrated"]
    with closing(sqlite3.connect(database)) as conn:
        try:
            revision = conn.execute(
                "select version_num from alembic_version"
            ).fetchone()
            version = conn.execute(
                "select value from app_metadata where key = 'dtxwiki_version'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            return [f"DTXWIKI_DATABASE_URL is not a dtxWiki database: {exc}"]
    problems = []
    if revision != (current_alembic_head(),):
        problems.append(f"database is at {revision}, not the code's head")
    if version is None:
        problems.append("the app version marker was not recorded (not a bootstrap)")
    return problems


def test_make_migrate_bootstraps_the_database_url_from_the_environment(
    tmp_path: Path,
):
    assert migrate_target_problems(REPO_ROOT / "Makefile", tmp_path) == []


@pytest.mark.counterexample(
    "a migrate recipe that does not honour DTXWIKI_DATABASE_URL is reported"
)
def test_a_migrate_target_that_ignores_the_database_url_is_reported(tmp_path: Path):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "VENV ?= .venv\n"
        "VENV_PYTHON := $(VENV)/bin/python\n"
        "migrate:\n"
        "\t$(VENV_PYTHON) -c 'print(\"migrated alembic.ini instead\")'\n",
        encoding="utf-8",
    )

    assert migrate_target_problems(makefile, tmp_path) == [
        "DTXWIKI_DATABASE_URL was not migrated"
    ]
