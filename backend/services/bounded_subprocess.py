"""Bounded, shell-free child-process execution with process-tree teardown."""

from __future__ import annotations

import math
import os
import signal
import subprocess
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Literal

_COPY_CHUNK_BYTES = 64 * 1024
_ENV_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}

type BoundedSubprocessFailure = Literal[
    "input_limit",
    "output_limit",
    "stderr_limit",
    "start_failed",
    "process_failed",
    "timed_out",
]


class BoundedSubprocessError(RuntimeError):
    """Stable transport failure that never exposes child diagnostics."""

    def __init__(self, reason: BoundedSubprocessFailure) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BoundedSubprocessResult:
    stdout: bytes
    return_code: int


def run_bounded_subprocess(
    command: tuple[str, ...],
    *,
    cwd: Path,
    stdin: bytes,
    stdin_limit: int,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float,
) -> BoundedSubprocessResult:
    """Run fixed argv in a private cwd and bound every transport dimension.

    The child starts in a new POSIX session or Windows process group. A
    wall-clock timeout terminates the complete process tree before control is
    returned to the caller.
    """

    _validate_arguments(
        command=command,
        cwd=cwd,
        stdin_limit=stdin_limit,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        timeout_seconds=timeout_seconds,
    )
    if len(stdin) > stdin_limit:
        raise BoundedSubprocessError("input_limit")

    environment = {
        key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST
    }
    environment["TEMP"] = str(cwd)
    environment["TMP"] = str(cwd)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    deadline = monotonic() + float(timeout_seconds)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        raise BoundedSubprocessError("start_failed") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_future = _submit_daemon(_read_limited, process.stdout, stdout_limit)
    stderr_future = _submit_daemon(_read_limited, process.stderr, stderr_limit)
    stdin_future = _submit_daemon(_write_input, process.stdin, stdin)
    try:
        return_code = process.wait(timeout=_remaining(deadline))
    except (subprocess.TimeoutExpired, FutureTimeoutError) as exc:
        _terminate_process_tree(process)
        raise BoundedSubprocessError("timed_out") from exc

    # A provider leader must not escape the deadline by exiting while a child
    # retains its standard handles. Tear down the process group/tree before
    # waiting on any still-pending transport operation. Daemon I/O threads plus
    # deadline-bounded Future reads ensure even a failed platform tree kill can
    # never hold the worker indefinitely.
    if not all(
        future.done() for future in (stdin_future, stdout_future, stderr_future)
    ):
        _terminate_process_tree(process)
    try:
        try:
            stdin_future.result(timeout=_remaining(deadline))
        except (BrokenPipeError, OSError, ValueError):
            pass
        stdout, stdout_exceeded = stdout_future.result(timeout=_remaining(deadline))
        _stderr, stderr_exceeded = stderr_future.result(timeout=_remaining(deadline))
    except FutureTimeoutError as exc:
        _terminate_process_tree(process)
        raise BoundedSubprocessError("timed_out") from exc
    except (OSError, ValueError) as exc:
        raise BoundedSubprocessError("process_failed") from exc

    if stdout_exceeded:
        raise BoundedSubprocessError("output_limit")
    if stderr_exceeded:
        raise BoundedSubprocessError("stderr_limit")
    if return_code != 0:
        raise BoundedSubprocessError("process_failed")
    return BoundedSubprocessResult(stdout=stdout, return_code=return_code)


def _validate_arguments(
    *,
    command: tuple[str, ...],
    cwd: Path,
    stdin_limit: int,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float,
) -> None:
    if (
        type(command) is not tuple
        or not command
        or not all(type(part) is str and part for part in command)
        or not Path(command[0]).is_absolute()
        or not Path(command[0]).is_file()
        or not cwd.is_absolute()
        or not cwd.is_dir()
        or any(
            type(limit) is not int or limit <= 0
            for limit in (stdin_limit, stdout_limit, stderr_limit)
        )
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("bounded subprocess arguments are invalid")


def _read_limited(stream, limit: int) -> tuple[bytes, bool]:
    stored = bytearray()
    exceeded = False
    try:
        while True:
            chunk = stream.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            remaining = max(0, limit + 1 - len(stored))
            if remaining:
                stored.extend(chunk[:remaining])
            if len(stored) > limit or len(chunk) > remaining:
                exceeded = True
        return bytes(stored[:limit]), exceeded
    finally:
        stream.close()


def _write_input(stream, content: bytes) -> None:
    try:
        stream.write(content)
        stream.flush()
    finally:
        stream.close()


def _submit_daemon(function, *args) -> Future:
    future: Future = Future()

    def invoke() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = function(*args)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    Thread(target=invoke, daemon=True).start()
    return future


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise FutureTimeoutError
    return remaining


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Best-effort hard stop for the process and all of its descendants."""

    try:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        else:
            system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
            taskkill = (
                Path(system_root) / "System32" / "taskkill.exe"
                if system_root
                else None
            )
            if taskkill is not None and taskkill.is_file():
                try:
                    subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass


__all__ = [
    "BoundedSubprocessError",
    "BoundedSubprocessResult",
    "run_bounded_subprocess",
]
