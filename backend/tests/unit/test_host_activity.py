"""Detached bundled hosts cannot join a replacement runtime after parent loss."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.process_tree import managed_host_environment, start_managed_host
from backend.app.runtime_resources import RuntimeResources
from backend.services.ops import runtime_lock
from backend.tests.utils.environment import child_env_without_dtxwiki_config

ROOT = Path(__file__).resolve().parents[3]


def _host(tmp_path):
    script = tmp_path / "backend" / "worker" / "converter_host.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from backend.app.process_tree import guard_managed_parent\n"
        "guard_managed_parent()\n"
        f"open({str(tmp_path / 'started')!r}, 'w').close()\n"
        "sys.stdin.buffer.read()\n"
    )
    return (sys.executable, str(script))


def test_host_eof_before_start_is_side_effect_free_and_retains_lease(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "root"))
    r = RuntimeResources.for_settings(
        SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'db.sqlite'}"),
        managed=True,
    )
    monkeypatch.setenv(
        "_DTXWIKI_RUNTIME_ACTIVITY", r.export_environment()["_DTXWIKI_RUNTIME_ACTIVITY"]
    )
    command = _host(tmp_path)
    env = managed_host_environment(command, child_env_without_dtxwiki_config())
    parent = r.acquire_parent(managed=True)
    child = subprocess.Popen(
        command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        assert child.stdout.readline() == b'{"status":"registered-host"}\n'
        parent.release()
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            r.acquire_parent(managed=True)
        child.stdin.close()
        assert child.wait(timeout=10) != 0
        assert not (tmp_path / "started").exists()
        with r.acquire_parent(managed=True):
            pass
    finally:
        parent.release()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_host_start_ack_is_local_and_does_not_pollute_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "root"))
    r = RuntimeResources.for_settings(
        SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'db.sqlite'}")
    )
    monkeypatch.setenv(
        "_DTXWIKI_RUNTIME_ACTIVITY", r.export_environment()["_DTXWIKI_RUNTIME_ACTIVITY"]
    )
    command = _host(tmp_path)
    env = managed_host_environment(command, child_env_without_dtxwiki_config())
    child = subprocess.Popen(
        command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        start_managed_host(child, env, 10)
        assert child.communicate(input=b"{}", timeout=10)[0] == b""
        assert child.returncode == 0
        assert (tmp_path / "started").exists()
        with r.acquire_activity("exclusive"):
            pass
        assert json.loads(r.export_environment()["_DTXWIKI_RUNTIME_ACTIVITY"])
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
