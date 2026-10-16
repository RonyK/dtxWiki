"""Real CLI acceptance coverage for the server's managed worker lifecycle."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.tests.utils.environment import child_env_without_dtxwiki_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux process lifecycle proof"),
]

REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPLETION_TIMEOUT = 30.0


@dataclass
class _Runtime:
    process: subprocess.Popen
    client: httpx.Client
    log_path: Path
    worker_pid: int | None = None

    def logs(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def assert_running(self) -> None:
        assert self.process.poll() is None, self.logs()


@contextmanager
def _running_cli(
    tmp_path: Path, free_tcp_port: int, command: tuple[str, ...]
) -> Iterator[tuple[_Runtime, Path, Path, Path]]:
    selected_db = tmp_path / "selected.sqlite"
    selected_url = copy_migrated_sqlite(selected_db)
    ignored_db = tmp_path / "ambient-database-must-not-be-opened.sqlite"
    blob_root = tmp_path / "configured-library-blobs"
    env = child_env_without_dtxwiki_config()
    env.update(
        PYTHONUNBUFFERED="1",
        DTXWIKI_DATA_DIR=str(tmp_path / "data-dir"),
        # The explicit CLI --db must reach the managed worker too.
        DTXWIKI_DATABASE_URL=f"sqlite:///{ignored_db}",
        DTXWIKI_AUTO_BOOTSTRAP="false",
        DTXWIKI_LIBRARY_BLOB_ROOT=str(blob_root),
        DTXWIKI_SESSION_SECRET="managed-runtime-acceptance-session-secret",
    )
    log_path = tmp_path / "runtime.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "backend.cli.main",
                *command,
                "--no-open",
                "--host",
                "127.0.0.1",
                "--port",
                str(free_tcp_port),
                "--db",
                selected_url,
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{free_tcp_port}",
            timeout=2.0,
            trust_env=False,
        ) as client:
            runtime = _Runtime(process, client, log_path)
            try:
                yield runtime, selected_db, ignored_db, blob_root
            finally:
                # A failed assertion must not leave the server, its worker or
                # converter children running in the developer's workspace.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                if runtime.worker_pid is not None:
                    try:
                        os.killpg(runtime.worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def _await_api(runtime: _Runtime) -> None:
    deadline = time.monotonic() + _COMPLETION_TIMEOUT
    while time.monotonic() < deadline:
        runtime.assert_running()
        try:
            response = runtime.client.get("/api/v1/auth/capabilities")
        except httpx.ConnectError:
            # Wait only for the real asynchronous socket bind, without
            # repeating a failed assertion or restarting either process.
            time.sleep(0.05)
            continue
        assert response.status_code == 200, response.text
        return
    pytest.fail(f"CLI did not become ready:\n{runtime.logs()}")


def _managed_worker_pid(runtime: _Runtime) -> int:
    children = Path(f"/proc/{runtime.process.pid}/task/{runtime.process.pid}/children")
    deadline = time.monotonic() + _COMPLETION_TIMEOUT
    while time.monotonic() < deadline:
        runtime.assert_running()
        matches = []
        for raw_pid in children.read_text().split():
            try:
                command = Path(f"/proc/{raw_pid}/cmdline").read_bytes()
            except FileNotFoundError:
                continue
            if b"backend.worker.managed" in command:
                matches.append(int(raw_pid))
        assert len(matches) <= 1, f"Multiple managed workers: {matches}"
        if matches:
            runtime.worker_pid = matches[0]
            return runtime.worker_pid
        time.sleep(0.05)
    pytest.fail(f"CLI did not start its managed worker:\n{runtime.logs()}")


def _upload_and_wait_for_conversion(runtime: _Runtime) -> tuple[str, str]:
    signup = runtime.client.post(
        "/api/v1/auth/signup",
        json={"email": "managed-runtime@example.com", "password": "runtime-password"},
    )
    assert signup.status_code == 201, signup.text
    workspaces = runtime.client.get("/api/v1/workspaces")
    assert workspaces.status_code == 200, workspaces.text
    workspace_id = workspaces.json()["data"]["personal_workspace_id"]
    assert workspace_id
    content = b"# Managed runtime\n\nThis exact document was converted by the worker.\n"
    collection_url = f"/api/v1/workspaces/{workspace_id}/library/items"
    uploaded = runtime.client.post(
        collection_url,
        files={"file": ("managed-runtime.md", content, "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    uploaded_data = uploaded.json()["data"]
    version_id = uploaded_data["current_version"]["id"]
    detail_url = f"{collection_url}/{uploaded_data['id']}"
    deadline = time.monotonic() + _COMPLETION_TIMEOUT
    detail = uploaded_data
    while time.monotonic() < deadline:
        runtime.assert_running()
        response = runtime.client.get(detail_url)
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        current = detail["current_version"]
        assert current["processing_status"] not in {
            "failed",
            "timed_out",
            "stored_unconverted",
        }, f"{detail}\n{runtime.logs()}"
        if current["processing_status"] == "ready":
            assert detail["current_ready_version"]["id"] == version_id
            assert current["selected_conversion_attempt_id"]
            markdown = [
                artifact
                for artifact in detail["artifacts"]
                if artifact["stage"] == "conversion" and artifact["role"] == "markdown"
            ]
            assert len(markdown) == 1, detail
            converted = runtime.client.get(markdown[0]["content_url"])
            assert converted.status_code == 200, converted.text
            assert (
                b"This exact document was converted by the worker." in converted.content
            )
            return workspace_id, version_id
        time.sleep(0.05)
    pytest.fail(f"Uploaded document never became ready: {detail}\n{runtime.logs()}")


@pytest.mark.parametrize("command", [(), ("serve",)], ids=["dtxwiki", "dtxwiki-serve"])
def test_one_cli_converts_upload_with_explicit_database_and_cleans_up_on_sigterm(
    tmp_path: Path, free_tcp_port: int, command: tuple[str, ...]
) -> None:
    with _running_cli(tmp_path, free_tcp_port, command) as (
        runtime,
        selected_db,
        ignored_db,
        blob_root,
    ):
        _await_api(runtime)
        worker_pid = _managed_worker_pid(runtime)
        workspace_id, version_id = _upload_and_wait_for_conversion(runtime)

        with sqlite3.connect(selected_db) as connection:
            jobs = connection.execute(
                "SELECT job_type, status, target_ref FROM job WHERE workspace_id = ?",
                (workspace_id.replace("-", ""),),
            ).fetchall()
        conversion_jobs = [
            (job_type, status)
            for job_type, status, target_ref in jobs
            if json.loads(target_ref).get("library_item_version_id") == version_id
            and job_type == "library_conversion"
        ]
        assert conversion_jobs == [("library_conversion", "completed")], jobs
        assert blob_root.is_dir()
        assert any(blob_root.rglob("*"))
        assert not ignored_db.exists(), "Managed worker opened the ambient database"

        runtime.process.send_signal(signal.SIGTERM)
        returncode = runtime.process.wait(timeout=15)
        assert returncode == 0, runtime.logs()
        assert not Path(f"/proc/{worker_pid}").exists(), runtime.logs()


def test_managed_worker_death_stops_server_with_failure(
    tmp_path: Path, free_tcp_port: int
) -> None:
    with _running_cli(tmp_path, free_tcp_port, ("serve",)) as (
        runtime,
        _selected_db,
        _ignored_db,
        _blob_root,
    ):
        _await_api(runtime)
        worker_pid = _managed_worker_pid(runtime)
        os.kill(worker_pid, signal.SIGKILL)

        assert runtime.process.wait(timeout=15) != 0, runtime.logs()
        assert not Path(f"/proc/{worker_pid}").exists(), runtime.logs()
        with pytest.raises(httpx.ConnectError):
            runtime.client.get("/api/v1/auth/capabilities")


def test_parent_sigkill_does_not_leave_a_running_worker(
    tmp_path: Path, free_tcp_port: int
) -> None:
    with _running_cli(tmp_path, free_tcp_port, ("serve",)) as (
        runtime,
        _selected_db,
        _ignored_db,
        _blob_root,
    ):
        _await_api(runtime)
        worker_pid = _managed_worker_pid(runtime)
        runtime.process.kill()
        assert runtime.process.wait(timeout=5) == -signal.SIGKILL

        deadline = time.monotonic() + 15.0
        status_path = Path(f"/proc/{worker_pid}/stat")
        while time.monotonic() < deadline:
            try:
                status = status_path.read_text().split(") ", 1)[1].split()[0]
            except FileNotFoundError:
                break
            # Only the OS's new parent can reap an orphaned process. A zombie
            # has exited and cannot hold locks, execute jobs or keep children.
            if status == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"Worker survived its parent's SIGKILL:\n{runtime.logs()}")


def test_port_bind_failure_cleans_up_the_started_worker(
    tmp_path: Path, free_tcp_port: int
) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", free_tcp_port))
        occupied.listen()
        with _running_cli(tmp_path, free_tcp_port, ("serve",)) as (
            runtime,
            _selected_db,
            _ignored_db,
            _blob_root,
        ):
            worker_pid = _managed_worker_pid(runtime)
            assert runtime.process.wait(timeout=15) != 0, runtime.logs()
            assert not Path(f"/proc/{worker_pid}").exists(), runtime.logs()
            assert "HTTP server could not start" in runtime.logs()
