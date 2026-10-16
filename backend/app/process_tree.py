"""Keep Windows worker descendants tied to their owning runtime."""

from __future__ import annotations

import ctypes
import os
import signal
import threading
from collections.abc import Callable


class WorkerTree:
    def __init__(self, close: Callable[[], None] | None = None) -> None:
        self._close = close

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


def guard_managed_parent() -> None:
    """Stop a detached bundled converter when its managed worker disappears.

    Converter transports create their own sessions for time/resource bounds;
    their groups therefore outlive a killed worker unless guarded separately.
    Windows descendants already belong to the runtime's kernel job object.
    """
    expected = os.environ.get("_DTXWIKI_MANAGED_WORKER_PID")
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


def _attach_windows_worker_tree(pid: int) -> WorkerTree:
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

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
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
        # AssignProcessToJobObject requires SET_QUOTA and TERMINATE rights.
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    finally:
        if process:
            kernel.CloseHandle(process)
    return WorkerTree(lambda: kernel.CloseHandle(job))
