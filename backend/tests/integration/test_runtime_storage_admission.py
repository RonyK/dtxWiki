"""Real processes must agree about storage ownership and surviving activity."""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx
import pytest

from backend.tests.integration.test_managed_runtime import (
    REPO_ROOT,
    _await_api,
    _managed_worker_pid,
    _Runtime,
)
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.tests.utils.environment import child_env_without_dtxwiki_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux process/lock proof"),
]


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _launcher(log_directory: Path, root: Path, url: str):
    log_directory.mkdir(parents=True, exist_ok=True)
    env = child_env_without_dtxwiki_config()
    env.update(
        DTXWIKI_DATA_DIR=str(root),
        DTXWIKI_AUTO_BOOTSTRAP="false",
        # -P omits cwd; shared editable environments may name another worktree.
        PYTHONPATH=str(REPO_ROOT),
        PYTHONUNBUFFERED="1",
    )
    port = _port()
    path = log_directory / "runtime.log"
    with path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "backend.cli.main",
                "--no-open",
                "--port",
                str(port),
                "--db",
                url,
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=2.0, trust_env=False
        ) as client:
            runtime = _Runtime(process, client, path)
            try:
                yield runtime
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait(timeout=5)
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                if runtime.worker_pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(runtime.worker_pid, signal.SIGCONT)
                        os.killpg(runtime.worker_pid, signal.SIGKILL)


def _assert_rejected_before_init(runtime: _Runtime, *, root: Path) -> None:
    assert runtime.process.wait(timeout=15) != 0, runtime.logs()
    assert "already running or storage is still active" in runtime.logs()
    assert "worker-starting" not in runtime.logs()
    # Owner lock files are permitted, serving credentials are init side
    # effects. A rejected root must not have run that initialization phase.
    assert not (root / "oauth-jwt.pem").exists()
    assert not (root / "session_secret").exists()


def test_aliases_roots_and_independent_storage_use_real_process_admission(tmp_path):
    database = tmp_path / "shared.sqlite"
    base_url = copy_migrated_sqlite(database)
    primary_root = tmp_path / "primary-root"
    with _launcher(tmp_path / "primary", primary_root, base_url) as primary:
        _await_api(primary)
        primary_worker = _managed_worker_pid(primary)
        # Different driver spelling and harmless connect options identify the
        # same physical database, even when its configured data root differs.
        aliases = (
            f"sqlite+pysqlite:///{database}",
            f"sqlite:///{database}?timeout=600",
            f"sqlite+pysqlite:///{database}?timeout=12",
        )
        for index, alias in enumerate(aliases):
            root = tmp_path / f"alias-root-{index}"
            with _launcher(tmp_path / f"alias-{index}", root, alias) as rejected:
                _assert_rejected_before_init(rejected, root=root)
            assert _managed_worker_pid(primary) == primary_worker
            primary.assert_running()

        # The root's library/credentials also form one independently owned
        # resource: choosing another database must not bypass that owner.
        other_database = tmp_path / "must-stay-absent.sqlite"
        with _launcher(
            tmp_path / "same-root", primary_root, f"sqlite:///{other_database}"
        ) as rejected:
            assert rejected.process.wait(timeout=15) != 0, rejected.logs()
            assert "already running or storage is still active" in rejected.logs()
            assert not other_database.exists()
        assert _managed_worker_pid(primary) == primary_worker

        independent_database = tmp_path / "independent.sqlite"
        independent_url = copy_migrated_sqlite(independent_database)
        with _launcher(
            tmp_path / "independent", tmp_path / "independent-root", independent_url
        ) as independent:
            _await_api(independent)
            independent_worker = _managed_worker_pid(independent)
            assert independent_worker != primary_worker
            assert independent.client.get("/health/ready").status_code == 200
            assert primary.client.get("/health/ready").status_code == 200


@contextmanager
def _collect_orphans():
    """Let this fixture reap the real worker after killing its API parent."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def _await_stopped(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = Path(f"/proc/{pid}/status").read_text()
        if "State:\tT" in status:
            return
        time.sleep(0.01)
    pytest.fail(f"worker {pid} did not acknowledge SIGSTOP")


def _reap_orphan(pid: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return
        time.sleep(0.01)
    pytest.fail(f"worker {pid} did not exit after its parent pipe closed")


def test_parent_death_cannot_admit_restart_while_real_worker_holds_activity(tmp_path):
    database = tmp_path / "shared.sqlite"
    url = copy_migrated_sqlite(database)
    root = tmp_path / "data-root"
    worker = None
    reaped = False
    with _collect_orphans():
        try:
            with _launcher(tmp_path / "primary", root, url) as primary:
                _await_api(primary)
                worker = _managed_worker_pid(primary)
                # Freeze the actual consumer after it acquired its own SH and
                # consumer guards. This fixes the exact parent-death boundary;
                # no sleep races a watcher against a replacement launcher.
                os.kill(worker, signal.SIGSTOP)
                _await_stopped(worker)
                primary.process.kill()
                assert primary.process.wait(timeout=5) == -signal.SIGKILL

                with _launcher(tmp_path / "held-restart", root, url) as rejected:
                    assert rejected.process.wait(timeout=15) != 0, rejected.logs()
                    assert "storage is still active" in rejected.logs()
                    assert Path(f"/proc/{worker}").exists()

                # Once resumed, the independent parent-loss watcher sees EOF
                # and the consumer exits. Reap it before allowing new work.
                os.kill(worker, signal.SIGCONT)
                _reap_orphan(worker)
                reaped = True
                with _launcher(tmp_path / "clean-restart", root, url) as restarted:
                    _await_api(restarted)
                    restarted_worker = _managed_worker_pid(restarted)
                    assert restarted_worker != worker
                    assert restarted.client.get("/health/ready").status_code == 200
        finally:
            if worker is not None and not reaped:
                with suppress(ProcessLookupError):
                    os.kill(worker, signal.SIGCONT)
                    os.killpg(worker, signal.SIGKILL)
                with suppress(ChildProcessError):
                    os.waitpid(worker, 0)
