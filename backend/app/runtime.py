"""Serve one dtxWiki app while supervising initialization and its worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from backend.app.data_dir import data_dir
from backend.app.process_tree import prepare_worker_tree
from backend.app.runtime_resources import RuntimeResourceError, RuntimeResources
from backend.app.settings import Settings
from backend.services.ops import runtime_lock

logger = logging.getLogger("dtxwiki.runtime")
INIT_START_TIMEOUT_SECONDS = 60.0
WORKER_START_TIMEOUT_SECONDS = 60.0
WORKER_STOP_TIMEOUT_SECONDS = 10.0
CHILD_KILL_TIMEOUT_SECONDS = 5.0
HTTP_STOP_TIMEOUT_SECONDS = 12.0


class RuntimeStartupError(RuntimeError):
    """Initialization, a required child, or HTTP could not start or stop."""


class _RuntimeStopping(Exception):
    """A normal stop interrupted an initialization phase."""


@dataclass
class _Child:
    process: asyncio.subprocess.Process
    role: str
    tree: object | None = None
    drain_task: asyncio.Task | None = None


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
            raise RuntimeStartupError("dtxWiki HTTP server could not start") from exc

    def handle_exit(self, sig, frame) -> None:
        super().handle_exit(sig, frame)
        self.stopping.set()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Cover initialization and worker launch; reap children before allowing
        # the entry point to return. No database operation runs in this parent.
        if (
            self._signals_installed
            or threading.current_thread() is not threading.main_thread()
        ):
            yield
            return
        self._signals_installed = True
        previous = {
            sig: signal.signal(sig, self.handle_exit)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
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


async def _phase[T](
    operation: Awaitable[T], server: _RuntimeServer, seconds: float, label: str
) -> T:
    """One deadline covers spawn, configure, registration and completion."""
    task = asyncio.create_task(operation)
    stopping = asyncio.create_task(server.stopping.wait())
    try:
        done, _ = await asyncio.wait(
            (task, stopping), timeout=seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if server.should_exit or stopping in done:
            raise _RuntimeStopping()
        if task not in done:
            raise RuntimeStartupError(
                f"dtxWiki {label} did not become ready before its deadline"
            )
        return await task
    finally:
        await _cancel_task(task)
        await _cancel_task(stopping)


async def _stop_worker(process: asyncio.subprocess.Process) -> None:
    """Preserve the ten-second grace period, then kill and confirm reaping."""
    try:
        async with asyncio.timeout(WORKER_STOP_TIMEOUT_SECONDS):
            if process.returncode is None and process.stdin is not None:
                try:
                    process.stdin.write(b"shutdown\n")
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                process.stdin.close()
            await process.wait()
    except TimeoutError:
        logger.error("runtime child %s did not stop; terminating it", process.pid)
        with suppress(ProcessLookupError):
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), CHILD_KILL_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise RuntimeStartupError(
                f"dtxWiki could not confirm child {process.pid} exit"
            ) from exc
    finally:
        if os.name != "nt":
            # Bundled detached hosts hold their own activity guards and parent
            # watchers. This group cleanup does not claim third-party coverage.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


async def _stop_child(child: _Child) -> None:
    try:
        await _stop_worker(child.process)
    finally:
        if child.tree is not None:
            child.tree.close()
        await _cancel_task(child.drain_task)


async def _drain_output(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


async def _read_message(child: _Child) -> dict:
    assert child.process.stdout is not None
    try:
        raw = await child.process.stdout.readline()
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeStartupError(
            f"dtxWiki {child.role} could not start; see the preceding error"
        ) from exc
    if not isinstance(message, dict):
        raise RuntimeStartupError(
            f"dtxWiki {child.role} sent an invalid startup message"
        )
    return message


async def _start_child(
    settings: Settings, command: Sequence[str] | None, role: str, children: list[_Child]
) -> _Child:
    # A venv launcher can create the real Windows interpreter before its PID
    # is returned. Pre-create a named job so ChildControl joins that same job
    # before it reads configuration or imports database/application code.
    tree = prepare_worker_tree()
    try:
        environment = {
            **os.environ,
            "DTXWIKI_DATA_DIR": str(data_dir()),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONSAFEPATH": "1",
            **RuntimeResources.for_settings(
                settings, managed=settings.worker_mode == "managed"
            ).export_environment(),
            **tree.environment(),
        }
        module = (
            "backend.app.runtime_init" if role == "init" else "backend.worker.managed"
        )
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *(command or (sys.executable, "-P", "-m", module)),
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
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Recover a process created before cancellation reached its
            # transport. Its job/EOF guard remains active during cleanup.
            process = await asyncio.shield(spawning)
            children.append(_Child(process, role, tree))
            raise
        child = _Child(process, role, tree)
        children.append(child)
        tree.attach(process.pid)
        packet = {
            "protocol": 2,
            "role": role,
            "settings": settings.model_dump(mode="json"),
        }
        raw = (json.dumps(packet) + "\n").encode()
        from backend.app.runtime_protocol import PACKET_LIMIT

        if len(raw) > PACKET_LIMIT:
            raise RuntimeStartupError(
                "dtxWiki runtime configuration exceeds the private protocol limit"
            )
        assert process.stdin is not None
        process.stdin.write(raw)
        await process.stdin.drain()
        registered = await _read_message(child)
        if (
            registered != {"status": "registered", "role": role}
            or process.returncode is not None
        ):
            raise RuntimeStartupError(
                f"dtxWiki {role} did not register its activity guards"
            )
        process.stdin.write(b'{"command":"start"}\n')
        await process.stdin.drain()
        return child
    except BaseException:
        tree.close()
        raise


async def _initialize(
    settings: Settings, command: Sequence[str] | None, children: list[_Child]
) -> Settings:
    child = await _start_child(settings, command, "init", children)
    result = await _read_message(child)
    config = result.get("config")
    if (
        set(result) != {"status", "app_version_synced", "config"}
        or result["status"] != "result"
        or result["app_version_synced"] is not True
        or not isinstance(config, dict)
        or set(config) != {"oauth_jwt_private_key_path"}
        or not (
            config["oauth_jwt_private_key_path"] is None
            or isinstance(config["oauth_jwt_private_key_path"], str)
        )
    ):
        raise RuntimeStartupError("dtxWiki init sent an invalid verification result")
    assert child.process.stdout is not None
    child.drain_task = asyncio.create_task(_drain_output(child.process.stdout))
    code = await child.process.wait()
    if code != 0:
        raise RuntimeStartupError(
            f"dtxWiki init failed after its result (exit code {code})"
        )
    # Reap before application creation, not merely after a success message.
    await _stop_child(child)
    children.remove(child)
    return settings.model_copy(update=config)


async def _start_worker(
    settings: Settings, command: Sequence[str] | None, children: list[_Child]
) -> _Child:
    child = await _start_child(settings, command, "worker", children)
    if (
        await _read_message(child) != {"status": "ready"}
        or child.process.returncode is not None
    ):
        raise RuntimeStartupError(
            "dtxWiki background worker could not start; see the preceding error"
        )
    assert child.process.stdout is not None
    child.drain_task = asyncio.create_task(_drain_output(child.process.stdout))
    return child


async def _finish_http(server: _RuntimeServer, task: asyncio.Task | None) -> None:
    server.should_exit = True
    if task is None:
        return
    done, _ = await asyncio.wait((task,), timeout=HTTP_STOP_TIMEOUT_SECONDS)
    if not done:
        task.cancel()
        done, _ = await asyncio.wait((task,), timeout=2.0)
        if not done:
            raise RuntimeStartupError("dtxWiki HTTP teardown did not complete")
    with suppress(asyncio.CancelledError):
        await task


async def serve_runtime(
    application,
    settings: Settings,
    *,
    host: str,
    port: int,
    log_level: str,
    application_factory: Callable[[Settings], object] | None = None,
    init_command: Sequence[str] | None = None,
    proxy_headers: bool = False,
    forwarded_allow_ips: str | None = None,
    on_started: Callable[[], None] | None = None,
    on_server: Callable[[uvicorn.Server], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    worker_command: Sequence[str] | None = None,
) -> None:
    """Run supervised initialization, then serve API/SPA with one worker.

    Entry points supply a lazy factory. An explicitly supplied application is
    for already-prepared embedding hosts and does not request initialization.
    The API parent remains in this process; only blocking init and the worker
    are supervised children. Arbitrary parent-native hangs remain an OS/outer
    process management concern.
    """
    if application is None and application_factory is None:
        raise ValueError("an application or application_factory is required")
    mode = settings.worker_mode
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
    children: list[_Child] = []
    guard = None
    previous_resource_environment = {}
    worker = None
    server_task = bound_task = exit_task = stop_task = None
    try:
        with server.capture_signals():
            if on_server is not None:
                on_server(server)
            try:
                resources = RuntimeResources.for_settings(
                    settings, managed=mode == "managed"
                )
                guard = resources.acquire_parent(managed=mode == "managed")
                for key, value in resources.export_environment().items():
                    previous_resource_environment[key] = os.environ.get(key)
                    os.environ[key] = value
                if server.should_exit:
                    return
                if application_factory is not None:
                    settings = await _phase(
                        _initialize(settings, init_command, children),
                        server,
                        INIT_START_TIMEOUT_SECONDS,
                        "initialization",
                    )
                    if server.should_exit:
                        return
                    application = application_factory(settings)
                    application.state.app_version_synced = True
                    server.config.app = application
                application.state.worker_mode = mode
                application.state.worker_ready = False
                if mode == "managed":
                    if on_progress is not None:
                        on_progress("worker-starting")
                    worker = await _phase(
                        _start_worker(settings, worker_command, children),
                        server,
                        WORKER_START_TIMEOUT_SECONDS,
                        "background worker",
                    )
                    if server.should_exit:
                        return
                    application.state.worker_ready = True
                    if on_progress is not None:
                        on_progress("worker-ready")
                    exit_task = asyncio.create_task(worker.process.wait())
                server_task = asyncio.create_task(server.serve())
                bound_task = asyncio.create_task(server.bound.wait())
                stop_task = asyncio.create_task(server.stopping.wait())
                pending = {server_task, bound_task, stop_task}
                if exit_task is not None:
                    pending.add(exit_task)
                while pending:
                    done, _ = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    if exit_task is not None and exit_task in done:
                        application.state.worker_ready = False
                        server.should_exit = True
                        raise RuntimeStartupError(
                            "dtxWiki background worker stopped unexpectedly "
                            f"(exit code {exit_task.result()}); dtxWiki has stopped"
                        )
                    if stop_task in done:
                        return
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
                            and (worker is None or worker.process.returncode is None)
                        ):
                            on_started()
            except _RuntimeStopping:
                return
            except runtime_lock.RuntimeLockHeldError as exc:
                raise RuntimeStartupError(
                    "dtxWiki is already running or storage is still active; "
                    "stop the existing processes first"
                ) from exc
            except RuntimeResourceError as exc:
                raise RuntimeStartupError(str(exc)) from exc
            except OSError as exc:
                raise RuntimeStartupError(
                    "dtxWiki runtime child or storage guard could not start"
                ) from exc
            finally:
                if application is not None:
                    application.state.worker_ready = False
                try:
                    await _finish_http(server, server_task)
                finally:
                    try:
                        for child in reversed(children):
                            await _stop_child(child)
                    finally:
                        await _cancel_task(bound_task)
                        await _cancel_task(exit_task)
                        await _cancel_task(stop_task)
    finally:
        for key, previous in previous_resource_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        if guard is not None:
            guard.release()
        lock = getattr(getattr(application, "state", None), "runtime_lock", None)
        if lock is not None:
            lock.release()
