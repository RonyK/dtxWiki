"""Real finalizers retain maintenance exclusion through intermediate launchers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.runtime_resources import RuntimeResources
from backend.app.settings import Settings
from backend.services.ops import runtime_lock
from backend.services.ops.maintenance_handoff import HANDOFF_ENV, finalizer_handoff
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.tests.utils.environment import child_env_without_dtxwiki_config

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


def test_actual_finalizer_through_intermediate_launcher(tmp_path, monkeypatch):
    database = tmp_path / "db.sqlite"
    url = copy_migrated_sqlite(database)
    root = tmp_path / "data"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(root))
    resources = RuntimeResources.for_settings(Settings(database_url=url))
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import json, os, subprocess, sys\n"
        "print(json.dumps({'launcher_pid': os.getpid()}), flush=True)\n"
        "raise SystemExit(subprocess.call(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    env = child_env_without_dtxwiki_config()
    env.update(
        DTXWIKI_DATA_DIR=str(root),
        DTXWIKI_DATABASE_URL=url,
        PYTHONPATH=str(ROOT),
    )

    with (
        resources.acquire_activity("exclusive") as held,
        finalizer_handoff(held) as token,
    ):
        env[HANDOFF_ENV] = token
        result = subprocess.run(
            [
                sys.executable,
                str(launcher),
                sys.executable,
                "-P",
                "-m",
                "backend.cli.main",
                "update",
                "--finalize-bootstrap",
                "--method",
                "venv",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert "finalize-bootstrap: migrated" in result.stdout
        launcher_pid = json.loads(result.stdout.splitlines()[0])["launcher_pid"]
        assert launcher_pid != os.getpid()
        # Finalization must not release or downgrade either parent-owned guard.
        for path in resources.activity_paths:
            with pytest.raises(runtime_lock.RuntimeLockHeldError):
                runtime_lock.acquire_path(path, "shared", strict=True)

    with resources.acquire_activity("exclusive"):
        pass


def test_actual_finalizer_refuses_expired_handoff_before_creating_database(
    tmp_path, monkeypatch
):
    database = tmp_path / "must-stay-absent.sqlite"
    url = f"sqlite:///{database}"
    root = tmp_path / "data"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(root))
    resources = RuntimeResources.for_settings(Settings(database_url=url))
    with resources.acquire_activity("exclusive") as held:
        with finalizer_handoff(held) as token:
            pass
        env = child_env_without_dtxwiki_config()
        env.update(
            DTXWIKI_DATA_DIR=str(root),
            DTXWIKI_DATABASE_URL=url,
            PYTHONPATH=str(ROOT),
        )
        env[HANDOFF_ENV] = token
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "backend.cli.main",
                "update",
                "--finalize-bootstrap",
                "--method",
                "venv",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 3, result.stderr + result.stdout
        assert "finalize-bootstrap refused" in result.stderr
        assert "finalize-bootstrap: migrated" not in result.stdout
        assert not database.exists()
        assert not (root / "session_secret").exists()
