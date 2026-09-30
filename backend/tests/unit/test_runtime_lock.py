"""The runtime lock that lets maintenance commands see a live dtxWiki (C10)."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.services.ops import runtime_lock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="flock semantics are POSIX-only"
)


def test_shared_holders_coexist_and_block_the_exclusive_lock(tmp_path: Path):
    server = runtime_lock.acquire_shared(tmp_path)
    worker = runtime_lock.acquire_shared(tmp_path)
    assert server is not None and worker is not None

    with pytest.raises(runtime_lock.RuntimeLockHeldError) as refused:
        runtime_lock.acquire_exclusive(tmp_path)
    assert "stop dtxWiki (server and worker) first" in str(refused.value)
    assert refused.value.mode == "exclusive"

    server.release()
    with pytest.raises(runtime_lock.RuntimeLockHeldError):
        runtime_lock.acquire_exclusive(tmp_path)

    worker.release()
    with runtime_lock.acquire_exclusive(tmp_path) as maintenance:
        assert maintenance.held
    assert not maintenance.held


def test_a_maintenance_holder_refuses_a_starting_runtime(tmp_path: Path):
    maintenance = runtime_lock.acquire_exclusive(tmp_path)

    with pytest.raises(runtime_lock.RuntimeLockHeldError) as refused:
        runtime_lock.acquire_shared(tmp_path)

    assert "update, rollback or migrate-data" in str(refused.value)
    assert refused.value.mode == "shared"
    maintenance.release()
    assert runtime_lock.acquire_shared(tmp_path) is not None


def test_release_is_idempotent_and_collection_releases(tmp_path: Path):
    lock = runtime_lock.acquire_shared(tmp_path)
    assert lock is not None
    lock.release()
    lock.release()
    assert not lock.held

    forgotten = runtime_lock.acquire_shared(tmp_path)
    assert forgotten is not None
    del forgotten
    gc.collect()
    runtime_lock.acquire_exclusive(tmp_path).release()


def test_the_lock_is_visible_across_processes(tmp_path: Path):
    """The real use: `dtxwiki update` in one process, the server in another."""

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys
                from backend.services.ops import runtime_lock
                lock = runtime_lock.acquire_shared({str(tmp_path)!r})
                print("held" if lock is not None else "none", flush=True)
                sys.stdin.read()
                """
            ),
        ],
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            runtime_lock.acquire_exclusive(tmp_path)
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)

    runtime_lock.acquire_exclusive(tmp_path).release()


def test_a_lock_file_another_account_created_is_still_lockable(tmp_path: Path):
    """root may create it during `dtxwiki update`; the service user reads it."""

    path = runtime_lock.lock_path(tmp_path)
    path.write_bytes(b"")
    path.chmod(0o444)

    shared = runtime_lock.acquire_shared(tmp_path)

    assert shared is not None
    with pytest.raises(runtime_lock.RuntimeLockHeldError):
        runtime_lock.acquire_exclusive(tmp_path)
    shared.release()


def test_a_created_lock_file_is_readable_by_every_account(tmp_path: Path):
    old_umask = os.umask(0o077)
    try:
        runtime_lock.acquire_exclusive(tmp_path).release()
    finally:
        os.umask(old_umask)

    assert runtime_lock.lock_path(tmp_path).stat().st_mode & 0o777 == 0o644


def test_an_unusable_data_dir_degrades_the_shared_lock_but_not_the_exclusive(
    tmp_path: Path,
):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")

    assert runtime_lock.acquire_shared(not_a_directory) is None
    with pytest.raises(OSError):
        runtime_lock.acquire_exclusive(not_a_directory)
