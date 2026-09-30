"""Tests for `prod_setup.Runner` failure handling (clean abort, no traceback)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import backend.cli.main as cli_main
from backend.cli import prod_setup

runner = CliRunner()


def _collecting_runner(*, dry_run: bool = False):
    lines: list[str] = []
    return prod_setup.Runner(dry_run=dry_run, echo=lines.append), lines


def test_runner_shell_nonzero_exit_aborts_cleanly():
    r, lines = _collecting_runner()
    with pytest.raises(SystemExit) as excinfo:
        r.shell(
            "exit nonzero", [sys.executable, "-c", "import sys; sys.exit(7)"]
        )
    assert excinfo.value.code == 1
    assert any("ERROR: step failed — exit nonzero" in line for line in lines)
    assert any("Setup aborted" in line for line in lines)
    assert any("--reconfigure" in line for line in lines)


def test_runner_shell_missing_binary_aborts_cleanly(tmp_path: Path):
    r, lines = _collecting_runner()
    with pytest.raises(SystemExit) as excinfo:
        r.shell("run missing tool", [str(tmp_path / "missing-binary"), "--x"])
    assert excinfo.value.code == 1
    assert any("ERROR: step failed — run missing tool" in line for line in lines)


def test_runner_action_permission_error_hints_sudo():
    r, lines = _collecting_runner()

    def denied() -> None:
        raise PermissionError("[Errno 13] Permission denied: '/etc/dtxwiki/jwt.pem'")

    with pytest.raises(SystemExit) as excinfo:
        r.action("write /etc/dtxwiki/jwt.pem", denied)
    assert excinfo.value.code == 1
    assert any("ERROR: step failed — write /etc/dtxwiki" in line for line in lines)
    assert any("re-run with sudo" in line for line in lines)


def test_runner_dry_run_never_executes_or_aborts(tmp_path: Path):
    r, _lines = _collecting_runner(dry_run=True)

    def boom() -> None:
        raise AssertionError("must not run in dry-run")

    r.shell("would run", [str(tmp_path / "missing-binary")])
    r.action("would write", boom)
    assert [step.ran for step in r.results] == [False, False]


def test_runner_success_records_results():
    r, _lines = _collecting_runner()
    done: list[int] = []
    r.shell("noop", [sys.executable, "-c", "pass"])
    r.action("note", lambda: done.append(1))
    assert done == [1]
    assert [step.ran for step in r.results] == [True, True]


def test_setup_production_step_failure_exits_clean(tmp_path: Path, monkeypatch):
    # A PermissionError in an OS step (e.g. writing /etc/dtxwiki without sudo)
    # must produce the curated abort message + exit 1, not a raw traceback.
    def denied(path, **_kwargs):
        raise PermissionError(f"[Errno 13] Permission denied: '{path}'")

    monkeypatch.setattr(prod_setup, "generate_jwt_key", denied)
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--domain", "wiki.example.com",
            "--env-file", str(tmp_path / "dtxwiki.env"),
            "--jwt-key", str(tmp_path / "jwt.pem"),
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke",
            "--no-admin", "--no-bootstrap",
        ],
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # clean abort, no traceback
    assert "ERROR: step failed" in result.stdout
    assert "re-run with sudo" in result.stdout
    assert "Setup aborted" in result.stdout
