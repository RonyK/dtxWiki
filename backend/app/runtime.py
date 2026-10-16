"""Serve one dtxWiki instance and supervise its required background worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

import uvicorn
from sqlalchemy.engine import make_url

from backend.app.data_dir import data_dir
from backend.app.process_tree import attach_worker_tree
from backend.app.settings import Settings
from backend.services.ops import runtime_lock

logger = logging.getLogger("dtxwiki.runtime")
WORKER_START_TIMEOUT_SECONDS = 60.0
WORKER_STOP_TIMEOUT_SECONDS = 10.0


class RuntimeStartupError(RuntimeError):
    """The required worker or HTTP server could not start or keep running."""


def _instance_lock_dir(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database not in {None, ":memory:"}:
        url = url.set(database=str(Path(url.database).expanduser().resolve()))
    identity = f"{data_dir().resolve()}\n{url.render_as_string(hide_password=False)}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return data_dir() / ".managed-runtime" / digest


class _RuntimeServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.bound = asyncio.Event()
        self.stopping = asyncio.Event()
        self._runtime_loop = asyncio.get_running_loop()
        self._signals_installed = False

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        loop = getattr(self, "_runtime_loop", None)
        if value and loop is not None and not loop.is_closed():
            # The desktop stdin watcher requests shutdown from another thread.
            loop.call_soon_threadsafe(self.stopping.set)

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if getattr(self.lifespan, "should_exit", False):
            raise RuntimeStartupError("dtxWiki application startup failed")
        if self.started and not self.should_exit:
            self.bound.set()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        except SystemExit as exc:
            # SystemExit inside an asyncio task escapes the event loop before
            # other tasks finish. Keep bind failures inside supervisor cleanup.
            raise RuntimeStartupError("dtxWiki HTTP server could not start") from exc

    def handle_exit(self, sig, frame) -> None:
        super().handle_exit(sig, frame)
        self.stopping.set()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Cover worker startup as well as serving, and defer process exit until
        # the child has been reaped. Uvicorn's default context re-raises SIGTERM
        # before an outer supervisor can clean up its child.
        if (
            self._signals_installed
            or threading.current_thread() is not threading.main_thread()
        ):
            yield
            return
        self._signals_installed = True
        signals = (signal.SIGINT, signal.SIGTERM)
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in signals}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self._signals_installed = False


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _stop_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None and process.stdin is not None:
        try:
            process.stdin.write(b"shutdown\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), WORKER_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.error("background worker did not stop; terminating it")
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    finally:
        if os.name != "nt":
            # Reap members of the worker's own process group. Bundled converter
            # and preview hosts use separate sessions with guard_managed_parent;
            # detached third-party converters need their own parent-death guard.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


async def _drain_output(stream: asyncio.StreamReader) -> None:
    # stdout is reserved for the handshake; drain unexpected native-library
    # output too, so it can never fill the pipe and block a running job.
    while await stream.read(65536):
        pass


async def _start_worker(settings: Settings, command: Sequence[str] | None):
    environment = {
        **os.environ,
        "DTXWIKI_DATA_DIR": str(data_dir()),
        "PYTHONUNBUFFERED": "1",
        # Resolve the child from this same checkout/wheel even if the launch
        # directory contains an unrelated backend package.
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONSAFEPATH": "1",
    }
    process = await asyncio.create_subprocess_exec(
        *(command or (sys.executable, "-P", "-m", "backend.worker.managed")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=os.name != "nt",
        **(
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {}
        ),
    )
    tree = None
    try:
        tree = attach_worker_tree(process.pid)
        packet = {"protocol": 1, "settings": settings.model_dump(mode="json")}
        assert process.stdin is not None
        process.stdin.write((json.dumps(packet) + "\n").encode())
        await process.stdin.drain()
    except BaseException:
        await _stop_worker(process)
        if tree is not None:
            tree.close()
        raise
    return process, tree


async def serve_runtime(
    application,
    settings: Settings,
    *,
    host: str,
    port: int,
    log_level: str,
    proxy_headers: bool = False,
    forwarded_allow_ips: str | None = None,
    on_started: Callable[[], None] | None = None,
    on_server: Callable[[uvicorn.Server], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    worker_command: Sequence[str] | None = None,
    bootstrap: Callable[[], None] | None = None,
) -> None:
    """Serve a bootstrapped app, managing one worker unless explicitly external.

    ``bootstrap`` runs under the managed instance lock, before starting a child.
    ``on_server`` lets the desktop parent watcher request server shutdown.
    ``on_started`` runs only after worker readiness and actual socket binding.
    ``on_progress`` reports the worker-starting and worker-ready phases once.
    An unexpected worker exit stops HTTP serving and raises RuntimeStartupError.
    """
    mode = settings.worker_mode
    application.state.worker_mode = mode
    application.state.worker_ready = False
    server = _RuntimeServer(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level=log_level,
            proxy_headers=proxy_headers,
            forwarded_allow_ips=forwarded_allow_ips,
            access_log=False,
            timeout_graceful_shutdown=10.0,
        )
    )
    process = None
    worker_tree = None
    owner_lock = None
    server_task = ready_task = exit_task = bound_task = stop_task = drain_task = None
    try:
        with server.capture_signals():
            if on_server is not None:
                on_server(server)
            try:
                if mode == "managed":
                    try:
                        owner_lock = runtime_lock.acquire_exclusive(
                            _instance_lock_dir(settings)
                        )
                    except runtime_lock.RuntimeLockHeldError as exc:
                        raise RuntimeStartupError(
                            "dtxWiki is already running for this "
                            "data directory and database"
                        ) from exc
                if bootstrap is not None:
                    bootstrap()
                if server.should_exit:
                    return
                if mode == "managed":
                    if on_progress is not None:
                        on_progress("worker-starting")
                    try:
                        process, worker_tree = await _start_worker(
                            settings, worker_command
                        )
                    except OSError as exc:
                        raise RuntimeStartupError(
                            "dtxWiki background worker could not be launched"
                        ) from exc
                    assert process.stdout is not None
                    ready_task = asyncio.create_task(process.stdout.readline())
                    stop_task = asyncio.create_task(server.stopping.wait())
                    done, _ = await asyncio.wait(
                        (ready_task, stop_task),
                        timeout=WORKER_START_TIMEOUT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if server.should_exit:
                        return
                    if ready_task not in done:
                        raise RuntimeStartupError(
                            "dtxWiki background worker did not become ready"
                        )
                    try:
                        ready = json.loads(ready_task.result())
                    except (ValueError, TypeError) as exc:
                        raise RuntimeStartupError(
                            "dtxWiki background worker could not start; "
                            "see the preceding error"
                        ) from exc
                    if ready != {"status": "ready"} or process.returncode is not None:
                        raise RuntimeStartupError(
                            "dtxWiki background worker could not start; "
                            "see the preceding error"
                        )
                    application.state.worker_ready = True
                    if on_progress is not None:
                        on_progress("worker-ready")
                    drain_task = asyncio.create_task(_drain_output(process.stdout))
                    exit_task = asyncio.create_task(process.wait())
                server_task = asyncio.create_task(server.serve())
                bound_task = asyncio.create_task(server.bound.wait())
                pending = {server_task, bound_task}
                if exit_task is not None:
                    pending.add(exit_task)
                while pending:
                    done, _ = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    if exit_task is not None and exit_task in done:
                        application.state.worker_ready = False
                        server.should_exit = True
                        await server_task
                        raise RuntimeStartupError(
                            "dtxWiki background worker stopped unexpectedly "
                            f"(exit code {exit_task.result()}); dtxWiki has stopped"
                        )
                    if server_task in done:
                        await server_task
                        if not server.bound.is_set() and not server.stopping.is_set():
                            raise RuntimeStartupError(
                                "dtxWiki HTTP server stopped before "
                                "accepting connections"
                            )
                        return
                    if bound_task in done:
                        pending.remove(bound_task)
                        if (
                            on_started is not None
                            and not server.should_exit
                            and (process is None or process.returncode is None)
                        ):
                            on_started()
            finally:
                application.state.worker_ready = False
                server.should_exit = True
                try:
                    for task in (ready_task, bound_task, stop_task):
                        await _cancel_task(task)
                    if server_task is not None and not server_task.done():
                        await server_task
                finally:
                    try:
                        if process is not None:
                            await _stop_worker(process)
                    finally:
                        if worker_tree is not None:
                            worker_tree.close()
                        for task in (exit_task, drain_task):
                            await _cancel_task(task)
    finally:
        if owner_lock is not None:
            owner_lock.release()
        lock = getattr(application.state, "runtime_lock", None)
        if lock is not None:
            lock.release()
