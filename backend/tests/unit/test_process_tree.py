"""Worker ownership must stop detached descendants without leaking resources."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.process_tree import (
    _attach_windows_worker_tree,
    attach_worker_tree,
)
from backend.tests.utils.environment import child_env_without_dtxwiki_config

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Function:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


@pytest.mark.parametrize("failure", ["limits", "open", "assign", None])
def test_windows_job_resource_ownership(monkeypatch, failure):
    closed = []

    def set_limits(job, information_class, pointer, size):
        assert job == 101
        assert information_class == 9
        assert pointer._obj.BasicLimitInformation.LimitFlags == 0x2000
        assert size == ctypes.sizeof(pointer._obj)
        return failure != "limits"

    def open_process(access, inherit, pid):
        assert access == 0x0101
        assert not inherit
        assert pid == 12345
        return 0 if failure == "open" else 202

    kernel = SimpleNamespace(
        CreateJobObjectW=_Function(lambda security, name: 101),
        SetInformationJobObject=_Function(set_limits),
        OpenProcess=_Function(open_process),
        AssignProcessToJobObject=_Function(lambda job, process: failure != "assign"),
        CloseHandle=_Function(lambda handle: closed.append(handle) or True),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda code: OSError(code, "denied"),
        raising=False,
    )
    if failure is not None:
        with pytest.raises(OSError, match="denied"):
            _attach_windows_worker_tree(12345)
        assert closed == ([101, 202] if failure == "assign" else [101])
    else:
        tree = _attach_windows_worker_tree(12345)
        # The process handle is never retained by the supervisor.
        assert closed == [202]
        tree.close()
        tree.close()
        assert closed == [202, 101]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows job objects")
def test_closing_job_terminates_a_real_worker():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        tree = attach_worker_tree(process.pid)
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        tree.close()
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


def _is_running(pid: int) -> bool:
    if sys.platform == "linux":
        try:
            return (
                Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
                != "Z"
            )
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX sessions and signals")
def test_detached_host_guard_stops_its_group_after_managed_parent_sigkill(tmp_path):
    """No stdio pipe closes to rescue a missing parent-death guard.

    The host and its grandchild both wait on independent threading.Events.
    Their stdin/stdout/stderr all point at DEVNULL before the parent dies.
    """

    ready_path = tmp_path / "host-ready.json"
    host_pid_path = tmp_path / "host.pid"
    grandchild_source = (
        "import os, sys, threading; "
        "os.write(int(sys.argv[1]), b'R'); os.close(int(sys.argv[1])); "
        "threading.Event().wait()"
    )
    host_source = f"""
import json, os, subprocess, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from backend.app.process_tree import guard_managed_parent
guard_managed_parent()
read_fd, write_fd = os.pipe()
grandchild = subprocess.Popen(
    [sys.executable, '-c', {grandchild_source!r}, str(write_fd)],
    pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
os.close(write_fd)
assert os.read(read_fd, 1) == b'R'
os.close(read_fd)
ready = Path(sys.argv[1])
staged = ready.with_suffix('.tmp')
staged.write_text(json.dumps({{
    'host': os.getpid(), 'grandchild': grandchild.pid,
    'parent': os.getppid(), 'group': os.getpgrp(),
}}))
staged.replace(ready)
threading.Event().wait()
"""
    parent_source = """
import os, subprocess, sys, threading
from pathlib import Path
os.environ['_DTXWIKI_MANAGED_WORKER_PID'] = str(os.getpid())
host = subprocess.Popen(
    [sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[4]],
    start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
Path(sys.argv[3]).write_text(str(host.pid))
threading.Event().wait()
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            parent_source,
            host_source,
            str(ready_path),
            str(host_pid_path),
            str(REPO_ROOT),
        ],
        cwd=tmp_path,
        env=child_env_without_dtxwiki_config(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists():
            assert parent.poll() is None, "Managed parent exited before host readiness"
            if time.monotonic() >= deadline:
                pytest.fail(
                    "Detached host did not initialize its guarded process group"
                )
            time.sleep(0.02)
        ready = json.loads(ready_path.read_text())
        assert ready["parent"] == parent.pid
        assert ready["group"] == ready["host"]
        assert os.getpgid(ready["grandchild"]) == ready["host"]
        assert _is_running(ready["host"])
        assert _is_running(ready["grandchild"])

        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        deadline = time.monotonic() + 5
        while any(_is_running(ready[key]) for key in ("host", "grandchild")):
            if time.monotonic() >= deadline:
                pytest.fail("Detached host or grandchild survived managed parent death")
            time.sleep(0.02)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if host_pid_path.exists():
            try:
                os.killpg(int(host_pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
