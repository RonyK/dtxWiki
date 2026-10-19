"""Private start authority is exercised with real pipes and child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.runtime_resources import RuntimeResources
from backend.services.ops import runtime_lock
from backend.tests.utils.environment import child_env_without_dtxwiki_config

ROOT = Path(__file__).resolve().parents[3]


def _launch(tmp_path, settings):
    env = child_env_without_dtxwiki_config()
    env["DTXWIKI_DATA_DIR"] = str(tmp_path / "root")
    env["PYTHONPATH"] = str(ROOT)
    child = subprocess.Popen(
        [sys.executable, "-P", "-m", "backend.app.runtime_init"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    child.stdin.write(
        json.dumps({"protocol": 2, "role": "init", "settings": settings}) + "\n"
    )
    child.stdin.flush()
    return child


def _cleanup(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


def test_registered_init_owns_activity_but_cannot_touch_db_without_start(
    tmp_path, monkeypatch
):
    db = tmp_path / "data.sqlite"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "root"))
    settings = {"database_url": f"sqlite:///{db}", "worker_mode": "managed"}
    child = _launch(tmp_path, settings)
    try:
        assert json.loads(child.stdout.readline()) == {
            "status": "registered",
            "role": "init",
        }
        assert not db.exists()
        r = RuntimeResources.for_settings(type("Config", (), settings)(), managed=True)
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            r.acquire_parent(managed=True)
        # Even a queued start is forbidden after parent-pipe EOF.
        child.stdin.close()
        child.stdin = None
        _stdout, stderr = child.communicate(timeout=10)
        assert child.returncode != 0, stderr
        assert not db.exists()
        with r.acquire_parent(managed=True):
            pass
    finally:
        _cleanup(child)


@pytest.mark.parametrize(
    "packet",
    [
        {"protocol": 1, "role": "init", "settings": {}},
        {"protocol": 2, "role": "worker", "settings": {}},
        {"protocol": 2, "role": "init", "settings": []},
    ],
)
def test_invalid_private_configuration_exits_without_data(tmp_path, packet):
    env = child_env_without_dtxwiki_config()
    env["DTXWIKI_DATA_DIR"] = str(tmp_path / "root")
    result = subprocess.run(
        [sys.executable, "-P", "-m", "backend.app.runtime_init"],
        input=json.dumps(packet) + "\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "root").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipe loss containment")
def test_oversized_or_malformed_packet_stops_before_backend_import(tmp_path):
    result = subprocess.run(
        [sys.executable, "-P", "-m", "backend.app.runtime_init"],
        input="[" * 2000 + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        env=child_env_without_dtxwiki_config(),
    )
    assert result.returncode != 0
    assert '"status": "registered"' not in result.stdout
