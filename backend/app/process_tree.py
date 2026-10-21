"""Keep Windows worker descendants tied to their owning runtime."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future

_host_activity_guard = None


def managed_host_environment(command, environment: dict[str, str]) -> dict[str, str]:
    """Enable a local start barrier only for our bundled protocol hosts."""
    activity = os.environ.get("_DTXWIKI_RUNTIME_ACTIVITY")
    bundled = any(
        str(arg)
        .replace("\\", "/")
        .endswith(
            ("backend/worker/converter_host.py", "backend/worker/preview_host.py")
        )
        or str(arg) in {"converter-host", "preview-host"}
        for arg in command[1:]
    )
    if activity and bundled:
        environment.update(
            _DTXWIKI_RUNTIME_ACTIVITY=activity,
            _DTXWIKI_HOST_BARRIER="1",
            _DTXWIKI_MANAGED_WORKER_PID=str(os.getpid()),
        )
    return environment


def start_managed_host(process, environment, timeout_seconds: float) -> None:
    """Wait for the host's independent lease before permitting file access."""
    if environment.get("_DTXWIKI_HOST_BARRIER") != "1":
        return
    reply = Future()

    def read() -> None:
        try:
            reply.set_result(process.stdout.readline(1024))
        except BaseException as exc:
            reply.set_exception(exc)

    threading.Thread(target=read, name="dtxwiki-host-registration", daemon=True).start()
    message = reply.result(timeout=max(timeout_seconds, 0.001))
    if message != b'{"status":"registered-host"}\n':
        raise RuntimeError("bundled host could not register its activity lease")
    process.stdin.write(b'{"command":"start-host"}\n')
    process.stdin.flush()


class WorkerTree:
    def __init__(
        self,
        close: Callable[[], None] | None = None,
        attach: Callable[[int], None] | None = None,
        name: str | None = None,
    ) -> None:
        self._close = close
        self._attach = attach
        self.name = name

    def attach(self, pid: int) -> None:
        if self._attach is not None:
            self._attach(pid)

    def environment(self) -> dict[str, str]:
        return {"_DTXWIKI_RUNTIME_JOB_NAME": self.name} if self.name else {}

    def close(self) -> None:
        close, self._close = self._close, None
        if close is not None:
            close()


def attach_worker_tree(pid: int) -> WorkerTree:
    """Attach before sending configuration, while the child waits on stdin.

    POSIX runtimes use a separate process group. On Windows an unshared job
    handle belongs to the parent: closing it, including on parent death, kills
    the worker and its converters. Assignment failures must abort startup.
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    if os.name != "nt":
        return WorkerTree()
    return _attach_windows_worker_tree(pid)


def prepare_worker_tree() -> WorkerTree:
    """Create containment before a Windows venv launcher spawns Python."""
    if os.name != "nt":
        return WorkerTree()
    return _attach_windows_worker_tree(None, name="dtxwiki-" + uuid.uuid4().hex)


def join_runtime_job() -> None:
    """The real interpreter joins before receiving configuration or importing app code.

    A Windows venv redirector can create this interpreter before its launcher
    is assigned to the parent's job. Self-registration closes that launch gap.
    Only the parent retains a job handle while the child waits for start.
    """
    name = os.environ.get("_DTXWIKI_RUNTIME_JOB_NAME")
    if os.name != "nt" or not name:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenJobObjectW.restype = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.IsProcessInJob.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    kernel.IsProcessInJob.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    job = kernel.OpenJobObjectW(0x0001 | 0x0004, False, name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        process = kernel.GetCurrentProcess()
        present = ctypes.c_int()
        if not kernel.IsProcessInJob(process, job, ctypes.byref(present)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value and not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(job)


def guard_managed_parent() -> None:
    """Stop a detached bundled converter when its managed worker disappears.

    Converter transports create their own sessions for time/resource bounds;
    their groups therefore outlive a killed worker unless guarded separately.
    Windows descendants already belong to the runtime's kernel job object.
    """
    global _host_activity_guard
    # API-owned hosts have a private parent-owned job too; worker-owned hosts
    # may join a nested job. Register the real venv interpreter before work.
    join_runtime_job()
    expected = os.environ.get("_DTXWIKI_MANAGED_WORKER_PID")
    if os.environ.get("_DTXWIKI_HOST_BARRIER") == "1":
        import sys

        from backend.app.runtime_resources import acquire_paths

        paths = json.loads(os.environ["_DTXWIKI_RUNTIME_ACTIVITY"])
        if not isinstance(paths, list) or not paths:
            raise RuntimeError("invalid host activity resources")
        _host_activity_guard = acquire_paths(paths, "shared")
        sys.stdout.buffer.write(b'{"status":"registered-host"}\n')
        sys.stdout.buffer.flush()
        if sys.stdin.buffer.readline(1024) != b'{"command":"start-host"}\n':
            raise SystemExit(1)
    if os.name == "nt" or expected is None:
        return
    parent_pid = int(expected)

    def watch() -> None:
        interval = threading.Event()
        while os.getppid() == parent_pid:
            interval.wait(0.1)
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(1)

    threading.Thread(
        target=watch,
        name="dtxwiki-converter-parent-watch",
        daemon=True,
    ).start()


def _attach_windows_worker_tree(
    pid: int | None, *, name: str | None = None
) -> WorkerTree:
    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel.SetInformationJobObject.restype = ctypes.c_int
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int

    job = kernel.CreateJobObjectW(None, name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    def attach(target_pid: int) -> None:
        process = kernel.OpenProcess(0x0100 | 0x0001, False, target_pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process)

    try:
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            job,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if pid is not None:
            attach(pid)
    except BaseException:
        kernel.CloseHandle(job)
        raise
    return WorkerTree(lambda: kernel.CloseHandle(job), attach, name)
