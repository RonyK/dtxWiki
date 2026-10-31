"""Bounded harness contracts; installed-wheel runtime is a separate smoke."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def smoke():
    path = Path(__file__).with_name("packaged_browser_smoke.py")
    spec = importlib.util.spec_from_file_location("packaged_browser_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_env_cannot_restore_checkout_imports_or_operational_config(
    smoke, monkeypatch, tmp_path
):
    monkeypatch.setenv("PYTHONPATH", "/checkout/backend")
    monkeypatch.setenv("PYTHONHOME", "/developer/python")
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", "sqlite:///operator.sqlite")
    monkeypatch.setenv("DTXWIKI_DATA_DIR", "/operator/data")
    monkeypatch.setenv("DTXWIKI_FORCE_SECURITY_HEADERS", "false")
    monkeypatch.setenv("DTXWIKI_APP_MODE", "remote")
    monkeypatch.setenv("PACKAGED_SMOKE_TEST_SENTINEL", "preserved")
    env = smoke.isolated_server_env(tmp_path / "data")
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert {
        name: value for name, value in env.items() if name.startswith("DTXWIKI_")
    } == {
        "DTXWIKI_DATA_DIR": str(tmp_path / "data"),
        "DTXWIKI_FORCE_SECURITY_HEADERS": "true",
    }
    assert env["PACKAGED_SMOKE_TEST_SENTINEL"] == "preserved"


def test_installed_backend_provenance_is_checked_outside_checkout(
    smoke, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        smoke, "run", lambda command, **kwargs: calls.append((command, kwargs))
    )
    python = tmp_path / "venv/bin/python"
    env = {"DTXWIKI_DATA_DIR": str(tmp_path / "data")}
    smoke.verify_installed_backend(python, cwd=tmp_path, env=env)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:3] == [str(python), "-I", "-c"]
    assert "backend.__file__" in command[3]
    assert "backend.cli.main.__file__" in command[3]
    assert command[3].count("is_relative_to(prefix)") == 2
    assert kwargs == {"cwd": tmp_path, "env": env}


@pytest.mark.counterexample(
    reason="installed provenance must reject either backend or CLI outside its venv"
)
@pytest.mark.parametrize("outside_module", ["backend", "cli"])
def test_installed_backend_provenance_rejects_checkout_module_paths(
    smoke, monkeypatch, tmp_path, outside_module
):
    original_run = smoke.run
    prefix = Path(sys.prefix).resolve()
    backend_path = prefix / "backend/__init__.py"
    cli_path = prefix / "backend/cli/main.py"
    if outside_module == "backend":
        backend_path = tmp_path / "checkout/backend/__init__.py"
    else:
        cli_path = tmp_path / "checkout/backend/cli/main.py"

    def run_with_wrong_provenance(command, **kwargs):
        # Execute the harness's real location assertions in an isolated child.
        # Deliberately invalid module metadata models a checkout/PYTHONPATH
        # escape; no installed module or operator file is changed.
        injected_modules = (
            "import sys, types; "
            "backend = types.ModuleType('backend'); "
            "backend.__path__ = []; "
            f"backend.__file__ = {str(backend_path)!r}; "
            "cli = types.ModuleType('backend.cli'); cli.__path__ = []; "
            "main = types.ModuleType('backend.cli.main'); "
            f"main.__file__ = {str(cli_path)!r}; "
            "backend.cli = cli; cli.main = main; "
            "sys.modules.update({'backend': backend, 'backend.cli': cli, "
            "'backend.cli.main': main}); "
        )
        return original_run(
            [*command[:3], injected_modules + command[3]],
            **kwargs,
            capture_output=True,
        )

    monkeypatch.setattr(smoke, "run", run_with_wrong_provenance)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        smoke.verify_installed_backend(
            Path(sys.executable),
            cwd=tmp_path,
            env=smoke.isolated_server_env(tmp_path / "data"),
        )
    assert caught.value.returncode == 1
    assert "AssertionError" in caught.value.stderr


def test_installed_console_server_uses_disposable_cwd(smoke, monkeypatch, tmp_path):
    calls = []
    process = object()

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        # The fake process does not inherit the descriptor like a real Popen.
        kwargs["stdout"].close()
        return process

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    env = {"DTXWIKI_DATA_DIR": str(tmp_path / "data")}
    assert (
        smoke.start_server(["/installed/bin/dtxwiki"], env, tmp_path / "server.log")
        is process
    )
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["env"] is env


def test_browser_script_keeps_real_rest_and_corrected_worker_runtime_oracles(
    smoke, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        smoke, "run", lambda command, **kwargs: calls.append((command, kwargs))
    )
    smoke.run_packaged_browser_check(24567)
    command, kwargs = calls[0]
    assert command[:3] == ["node", "--input-type=module", "-e"]
    script = command[3]
    assert "page.request.fetch" in script
    assert "/submit-for-review" in script
    assert "expected_content_hash: submitted.content_hash" in script
    assert "key: 'workspace_map_enabled', value: true" in script
    assert "edge.relation_count === 1" in script
    assert "data-renderer-status', 'ready'" in script
    assert "data-map-physics', 'force-atlas2-worker'" in script
    assert "state.phase === 'contact'" in script
    assert "state.iterations > state.structureIterations" in script
    assert "forceAtlas2Collision\\.worker-" in script
    assert "response.status === 200" in script
    assert "content-security-policy" in script
    assert "consoleErrors.length > 0" in script
    assert "finally {\n  await browser.close();" in script
    assert "not 2K capacity or device acceptance" in script
    assert "page.route(" not in script
    assert "DTXWIKI_SMOKE_PORT" in kwargs["env"]
    assert kwargs["env"]["DTXWIKI_SMOKE_PORT"] == "24567"
