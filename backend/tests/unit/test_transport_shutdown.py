"""Real worker interruption must tear down each detached transport group."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.tests.utils.environment import child_env_without_dtxwiki_config

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="requires POSIX worker interruption and process groups"
)
REPO_ROOT = Path(__file__).resolve().parents[3]


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


@pytest.mark.parametrize("transport", ["preview", "converter"])
def test_worker_sigterm_interrupts_transport_and_kills_detached_group(
    tmp_path: Path, transport: str
) -> None:
    ready_path = tmp_path / "transport-ready.json"
    result_path = tmp_path / "worker-result.txt"
    log_path = tmp_path / "worker.log"
    grandchild_source = (
        "import os, sys, threading; "
        "os.write(int(sys.argv[1]), b'R'); os.close(int(sys.argv[1])); "
        "threading.Event().wait()"
    )
    host_source = f"""
import json, os, subprocess, sys, threading
from pathlib import Path
read_fd, write_fd = os.pipe()
grandchild = subprocess.Popen(
    [sys.executable, '-c', {grandchild_source!r}, str(write_fd)],
    pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
)
os.close(write_fd)
assert os.read(read_fd, 1) == b'R'
os.close(read_fd)
ready = Path(sys.argv[1])
staged = ready.with_suffix('.tmp')
staged.write_text(json.dumps({{
    'host': os.getpid(), 'grandchild': grandchild.pid, 'group': os.getpgrp(),
    'managed_worker': os.environ.get('_DTXWIKI_MANAGED_WORKER_PID'),
}}))
staged.replace(ready)
threading.Event().wait()
"""
    worker_source = """
import os, sys
from pathlib import Path
from backend.worker.entry import _ShutdownSignals
from backend.worker.runtime import WorkerShutdownRequested
os.environ['_DTXWIKI_MANAGED_WORKER_PID'] = str(os.getpid())
command = (sys.executable, '-c', sys.argv[2], sys.argv[3])
shutdown = _ShutdownSignals()
try:
    with shutdown.installed(), shutdown.interruptible():
        if sys.argv[1] == 'preview':
            from backend.services.bounded_subprocess import run_bounded_subprocess
            run_bounded_subprocess(
                command, cwd=Path(sys.argv[5]), stdin=b'', stdin_limit=1024,
                stdout_limit=1024, stderr_limit=1024, timeout_seconds=30,
            )
        else:
            from backend.converters.external_protocol import _run_bounded
            _run_bounded(
                command, cwd=Path(sys.argv[5]), stdin=b'',
                stdout_limit=1024, timeout_seconds=30,
            )
except WorkerShutdownRequested:
    Path(sys.argv[4]).write_text('interrupted')
else:
    raise AssertionError('Transport swallowed the worker interruption')
"""
    with log_path.open("w") as log:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_source,
                transport,
                host_source,
                str(ready_path),
                str(result_path),
                str(tmp_path),
            ],
            cwd=REPO_ROOT,
            env=child_env_without_dtxwiki_config(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        ready = None
        try:
            deadline = time.monotonic() + 10
            while not ready_path.exists():
                assert worker.poll() is None, log_path.read_text()
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"Transport host did not become ready:\n{log_path.read_text()}"
                    )
                time.sleep(0.02)
            ready = json.loads(ready_path.read_text())
            assert ready["managed_worker"] == str(worker.pid)
            assert ready["group"] == ready["host"]
            assert os.getpgid(ready["grandchild"]) == ready["host"]
            assert _is_running(ready["host"])
            assert _is_running(ready["grandchild"])

            worker.send_signal(signal.SIGTERM)
            assert worker.wait(timeout=5) == 0, log_path.read_text()
            assert result_path.read_text() == "interrupted"
            assert not _is_running(ready["host"])
            deadline = time.monotonic() + 5
            while _is_running(ready["grandchild"]):
                if time.monotonic() >= deadline:
                    pytest.fail("Transport interruption left its grandchild running")
                time.sleep(0.02)
        finally:
            if ready is not None:
                try:
                    os.killpg(ready["host"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
