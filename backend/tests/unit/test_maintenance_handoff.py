"""A finalizer verifies real guard ownership independently of launcher ancestry."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.runtime_resources import RuntimeResources
from backend.services.ops import maintenance_handoff as handoff

ROOT = Path(__file__).resolve().parents[3]
DIRECT_PYTHON = (
    getattr(sys, "_base_executable", sys.executable)
    if os.name == "nt"
    else sys.executable
)
_VERIFY_CODE = """
import json, os, sys
from pathlib import Path
from backend.services.ops.maintenance_handoff import (
    HANDOFF_ENV, verify_finalizer_handoff,
)
from backend.services.ops.runtime_lock import acquire_path, RuntimeLockHeldError
paths = [Path(p) for p in json.loads(sys.argv[1])]
for path in paths:
    try:
        guard = acquire_path(path, 'shared')
    except RuntimeLockHeldError:
        pass
    else:
        guard.release()
        raise AssertionError('parent did not retain its exclusive guard')
assert verify_finalizer_handoff(paths) is True
assert HANDOFF_ENV not in os.environ
print(json.dumps({'pid': os.getpid(), 'ppid': os.getppid(), 'verified': True}))
"""


@pytest.fixture
def guards(tmp_path):
    resources = RuntimeResources(tmp_path / "root", tmp_path / "db.sqlite")
    with resources.acquire_activity("exclusive") as held:
        yield held


@pytest.mark.parametrize("intermediate_launcher", [False, True])
def test_real_finalizer_verifies_parent_guards_through_launcher(
    guards, intermediate_launcher
):
    # -S proves the helper and its challenge need only stdlib, including in a
    # newly-installed interpreter before application dependencies are imported.
    command = [
        DIRECT_PYTHON,
        "-S",
        "-c",
        _VERIFY_CODE,
        json.dumps(list(map(str, guards.paths))),
    ]
    if intermediate_launcher:
        launcher = """
import json, os, subprocess, sys
print(json.dumps({'launcher': os.getpid()}), flush=True)
raise SystemExit(subprocess.run(json.loads(sys.argv[1])).returncode)
"""
        command = [DIRECT_PYTHON, "-S", "-c", launcher, json.dumps(command)]
    with handoff.finalizer_handoff(guards) as metadata:
        result = subprocess.run(
            command,
            env={**os.environ, handoff.HANDOFF_ENV: metadata, "PYTHONPATH": str(ROOT)},
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        records = [json.loads(line) for line in result.stdout.splitlines()]
        assert records[-1]["verified"] is True
        assert guards.held
        if intermediate_launcher:
            assert records[-1]["ppid"] == records[0]["launcher"]
            assert records[-1]["ppid"] != os.getpid()
        else:
            assert records[-1]["ppid"] == os.getpid()


def test_missing_metadata_preserves_old_updater_fallback(monkeypatch):
    monkeypatch.delenv(handoff.HANDOFF_ENV, raising=False)
    assert handoff.verify_finalizer_handoff(()) is False


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "null",
        "[]",
        "{",
        json.dumps({"protocol": True, "port": 1, "token": "a" * 64}),
        json.dumps({"protocol": 1, "port": True, "token": "a" * 64}),
        json.dumps({"protocol": 1, "port": 65536, "token": "a" * 64}),
        json.dumps({"protocol": 1, "port": 1, "token": ""}),
        " " * 65_537,
    ],
)
def test_present_invalid_metadata_fails_closed_and_is_consumed(
    tmp_path, monkeypatch, metadata
):
    monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
    with pytest.raises(handoff.HandoffError):
        handoff.verify_finalizer_handoff([tmp_path / "guard"])
    assert handoff.HANDOFF_ENV not in os.environ


@pytest.mark.parametrize("wrong_resource", ["root", "database"])
def test_wrong_resource_cannot_use_another_updates_guards(
    guards, tmp_path, monkeypatch, wrong_resource
):
    paths = list(guards.paths)
    paths[0 if wrong_resource == "root" else 1] = tmp_path / "unrelated.lock"
    with handoff.finalizer_handoff(guards) as metadata:
        monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
        with pytest.raises(handoff.HandoffError):
            handoff.verify_finalizer_handoff(paths)
        monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
        assert handoff.verify_finalizer_handoff(guards.paths) is True


def test_invalid_token_does_not_consume_legitimate_challenge(guards, monkeypatch):
    with handoff.finalizer_handoff(guards) as metadata:
        invalid = json.loads(metadata)
        invalid["token"] = "a" * 64 if invalid["token"] != "a" * 64 else "b" * 64
        monkeypatch.setenv(handoff.HANDOFF_ENV, json.dumps(invalid))
        with pytest.raises(handoff.HandoffError) as caught:
            handoff.verify_finalizer_handoff(guards.paths)
        assert invalid["token"] not in str(caught.value)
        monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
        assert handoff.verify_finalizer_handoff(guards.paths) is True


def test_closed_listener_is_stale_not_an_old_updater(guards, monkeypatch):
    with handoff.finalizer_handoff(guards) as metadata:
        pass
    monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
    with pytest.raises(handoff.HandoffError):
        handoff.verify_finalizer_handoff(guards.paths)


def test_listener_checks_current_guard_ownership(guards, monkeypatch):
    paths = guards.paths
    with handoff.finalizer_handoff(guards) as metadata:
        guards.release()
        monkeypatch.setenv(handoff.HANDOFF_ENV, metadata)
        with pytest.raises(handoff.HandoffError):
            handoff.verify_finalizer_handoff(paths)


def test_handoff_refuses_unheld_or_missing_resource_guards(tmp_path):
    for held in (
        SimpleNamespace(paths=[tmp_path / "guard"], held=False),
        SimpleNamespace(paths=[], held=True),
        SimpleNamespace(held=True),
    ):
        with pytest.raises(handoff.HandoffError):
            with handoff.finalizer_handoff(held):
                pytest.fail("invalid guards must not produce a handoff")


def test_failed_listener_setup_is_reported(guards, monkeypatch):
    class UnavailableSocket:
        def bind(self, _address):
            raise OSError("listener unavailable")

        def close(self):
            pass

    monkeypatch.setattr(handoff.socket, "socket", lambda *_args: UnavailableSocket())
    with pytest.raises(handoff.HandoffError, match="listener could not start"):
        with handoff.finalizer_handoff(guards):
            pytest.fail("listener setup failure must not produce metadata")


def test_failed_socket_creation_is_reported(guards, monkeypatch):
    def unavailable(*_args):
        raise OSError("socket unavailable")

    monkeypatch.setattr(handoff.socket, "socket", unavailable)
    with pytest.raises(handoff.HandoffError, match="listener could not start"):
        with handoff.finalizer_handoff(guards):
            pytest.fail("socket creation failure must not produce metadata")


def test_unexpected_listener_failure_is_reported_on_context_exit(guards, monkeypatch):
    real_socket = socket.socket
    accepted = threading.Event()

    class FailedListener:
        def __init__(self):
            self.socket = real_socket(socket.AF_INET, socket.SOCK_STREAM)

        def __getattr__(self, name):
            return getattr(self.socket, name)

        def accept(self):
            accepted.set()
            raise OSError("listener broke")

    monkeypatch.setattr(handoff.socket, "socket", lambda *_args: FailedListener())
    with pytest.raises(handoff.HandoffError, match="listener failed"):
        with handoff.finalizer_handoff(guards):
            assert accepted.wait(timeout=2)
