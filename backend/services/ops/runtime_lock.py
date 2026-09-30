"""Advisory lock that tells maintenance commands a dtxWiki runtime is alive.

``dtxwiki serve`` and the worker hold a *shared* lock on ``.runtime.lock`` in
the data directory for as long as they run. ``dtxwiki update``, ``dtxwiki
rollback`` and ``dtxwiki migrate-data`` take an *exclusive* non-blocking lock
and refuse while any runtime still holds its shared one. The SQLite write-lock
probe could not answer that question: an idle server holds no database lock at
all, so the probe passed while a live process kept the old code and the old
schema in memory.

The lock is advisory and per open file description. POSIX uses ``flock``;
Windows uses ``LockFileEx`` on one byte through ``ctypes``. The Windows path is
best effort by design: any failure other than a lock that is provably held by
another process is logged and treated as "no lock", so it can never stop the
desktop engine from starting or break this module's import.
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Literal

logger = logging.getLogger("dtxwiki.runtime_lock")

LOCK_FILENAME = ".runtime.lock"

LockMode = Literal["shared", "exclusive"]


class RuntimeLockHeldError(RuntimeError):
    """The lock is held in a conflicting mode by another process."""

    def __init__(self, message: str, *, path: Path, mode: LockMode) -> None:
        super().__init__(message)
        self.path = path
        self.mode = mode


class RuntimeLock:
    """One acquired lock. ``release`` is idempotent and also runs on collection."""

    def __init__(
        self,
        *,
        path: Path,
        mode: LockMode,
        release: Callable[[], None],
    ) -> None:
        self.path = path
        self.mode = mode
        # The finalizer owns the descriptor: releasing explicitly, garbage
        # collection, and interpreter exit all go through the same one-shot
        # callback, so the descriptor is closed exactly once.
        self._finalizer = weakref.finalize(self, release)

    @property
    def held(self) -> bool:
        return self._finalizer.alive

    def release(self) -> None:
        self._finalizer()

    def __enter__(self) -> RuntimeLock:
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()


def lock_path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from backend.app.data_dir import data_dir as resolve_data_dir

        data_dir = resolve_data_dir()
    return Path(data_dir) / LOCK_FILENAME


def acquire_shared(data_dir: Path | None = None) -> RuntimeLock | None:
    """Hold the runtime lock in shared mode for a serving process or worker.

    Raises :class:`RuntimeLockHeldError` while a maintenance command holds the
    exclusive lock. Returns ``None`` when the lock file cannot be created or
    locked for any other reason (read-only data directory, unsupported
    platform primitive): the runtime then starts without the guard and says so
    in the log instead of refusing to run.
    """

    path = lock_path(data_dir)
    try:
        return _acquire(path, "shared")
    except RuntimeLockHeldError:
        raise
    except OSError as exc:
        logger.warning(
            "runtime lock unavailable at %s (%s); maintenance commands cannot "
            "detect this process",
            path,
            exc,
        )
        return None


def acquire_exclusive(data_dir: Path | None = None) -> RuntimeLock:
    """Take the runtime lock exclusively or refuse.

    Unlike :func:`acquire_shared`, an I/O failure here propagates: a
    maintenance command that cannot prove no runtime is alive must not proceed
    as if it had.
    """

    return _acquire(lock_path(data_dir), "exclusive")


def _acquire(path: Path, mode: LockMode) -> RuntimeLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _open_lock_file(path)
    try:
        if sys.platform == "win32":
            acquired = _lock_windows(descriptor, mode)
        else:
            acquired = _lock_posix(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise
    if not acquired:
        os.close(descriptor)
        raise RuntimeLockHeldError(_held_message(path, mode), path=path, mode=mode)

    def release(descriptor: int = descriptor) -> None:
        try:
            # Closing the last descriptor of the open file description drops
            # the lock on every platform; no explicit unlock is needed.
            os.close(descriptor)
        except OSError:
            pass

    return RuntimeLock(path=path, mode=mode, release=release)


def _open_lock_file(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError:
        # A lock file created by another account (root running ``dtxwiki
        # update`` before the service user ever started) is still lockable
        # through a read-only descriptor: flock does not need write access.
        return os.open(path, os.O_RDONLY)
    try:
        # Readable by the service account whichever account created it, so a
        # root-run update never locks the service out of its own data dir.
        os.chmod(path, 0o644)
    except OSError:
        pass
    return descriptor


def _held_message(path: Path, mode: LockMode) -> str:
    if mode == "exclusive":
        return (
            "stop dtxWiki (server and worker) first: a running dtxWiki process "
            f"holds {path}. Stop the server and the worker (for example "
            "'systemctl stop dtxwiki dtxwiki-worker' or Ctrl+C in the "
            "terminal running 'dtxwiki serve'), then re-run this command."
        )
    return (
        "dtxWiki refuses startup: an update, rollback or migrate-data "
        f"operation holds {path}. Wait for it to finish, then start dtxWiki "
        "again."
    )


def _lock_posix(descriptor: int, mode: LockMode) -> bool:
    import fcntl

    operation = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH
    try:
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
            return False
        raise
    return True


_ERROR_LOCK_VIOLATION = 33


def _lock_windows(descriptor: int, mode: LockMode) -> bool:
    """Best-effort ``LockFileEx`` on byte zero.

    Only ``ERROR_LOCK_VIOLATION`` is reported as a held lock. Every other
    failure (missing ``ctypes``, an unexpected API error) is logged and the
    lock is treated as acquired, so the desktop engine never fails to start
    because of this guard.
    """

    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lock_file_ex = kernel32.LockFileEx
        lock_file_ex.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        ]
        lock_file_ex.restype = wintypes.BOOL
        lockfile_fail_immediately = 0x1
        lockfile_exclusive_lock = 0x2
        flags = lockfile_fail_immediately
        if mode == "exclusive":
            flags |= lockfile_exclusive_lock
        overlapped = _Overlapped()
        handle = msvcrt.get_osfhandle(descriptor)
        if lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            return True
        if ctypes.get_last_error() == _ERROR_LOCK_VIOLATION:
            return False
        logger.warning(
            "runtime lock: LockFileEx failed with error %s; continuing unguarded",
            ctypes.get_last_error(),
        )
        return True
    except Exception as exc:  # pragma: no cover - exercised on Windows only
        logger.warning("runtime lock unavailable on this platform (%s)", exc)
        return True


__all__ = [
    "LOCK_FILENAME",
    "RuntimeLock",
    "RuntimeLockHeldError",
    "acquire_exclusive",
    "acquire_shared",
    "lock_path",
]
