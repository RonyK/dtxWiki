"""Private IPC and lifecycle invariants for the single-command runtime."""

from __future__ import annotations

import asyncio
import json
import signal
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend.app import runtime
from backend.app.settings import Settings
from backend.services.ops import runtime_lock
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


class FakeWorker:
    pid = 987654321

    def __init__(self, ready=b'{"status":"ready"}\n'):
        self.stdout = asyncio.StreamReader()
        if ready is not None:
            self.stdout.feed_data(ready)
        self.returncode = None
        self.exited = asyncio.Event()
        self.writes = []
        self.stdin = SimpleNamespace(
            write=self.writes.append,
            drain=self.drain,
            close=self.close,
        )

    async def drain(self):
        pass

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def close(self, code=0):
        self.returncode = code
        self.exited.set()
        self.stdout.feed_eof()

    def kill(self):
        self.close(-signal.SIGKILL)


class FakeServer:
    def __init__(self, config):
        self.config = config
        self.bound = asyncio.Event()
        self.stopping = asyncio.Event()
        self.finished = asyncio.Event()
        self._should_exit = False

    @property
    def should_exit(self):
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value):
        self._should_exit = value
        if value:
            self.stopping.set()
            self.finished.set()

    def capture_signals(self):
        return nullcontext()

    async def serve(self):
        self.bound.set()
        await self.finished.wait()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    if runtime.os.name != "nt":
        monkeypatch.setattr(runtime.os, "killpg", lambda *_args: None)
    return Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'runtime.sqlite'}")


def test_private_ipc_uses_resolved_settings_and_same_source(environment, monkeypatch):
    settings = environment.model_copy(update={"smtp_password": "private-test-value"})
    captured = {}

    async def scenario():
        child = FakeWorker()

        async def spawn(*command, **kwargs):
            captured.update(command=command, options=kwargs)
            return child

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "attach_worker_tree", lambda _pid: null_guard())
        process, guard = await runtime._start_worker(settings, None)
        packet = json.loads(child.writes[0])
        assert packet["settings"]["database_url"] == settings.database_url
        assert packet["settings"]["smtp_password"] == "private-test-value"
        assert all("private-test-value" not in arg for arg in captured["command"])
        assert captured["command"][1:] == ("-P", "-m", "backend.worker.managed")
        assert captured["options"]["env"]["PYTHONSAFEPATH"] == "1"
        assert captured["options"]["env"]["PYTHONPATH"] == str(
            runtime.Path(runtime.__file__).resolve().parents[2]
        )
        await runtime._stop_worker(process)
        guard.close()

    asyncio.run(scenario())


def null_guard():
    return SimpleNamespace(close=lambda: None)


def test_startup_order_lock_bootstrap_worker_bind_browser(environment, monkeypatch):
    events = []
    application = SimpleNamespace(state=SimpleNamespace())
    servers = []

    def server(config):
        result = FakeServer(config)
        servers.append(result)
        return result

    def bootstrap():
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            runtime_lock.acquire_exclusive(runtime._instance_lock_dir(environment))
        events.append("bootstrap")

    async def start_worker(*_args):
        assert events == ["bootstrap", "worker-starting"]
        events.append("worker")
        return FakeWorker(), null_guard()

    def started():
        assert application.state.worker_ready is True
        assert servers[0].bound.is_set()
        assert servers[0].config.access_log is False
        events.append("browser")
        servers[0].should_exit = True

    monkeypatch.setattr(runtime, "_RuntimeServer", server)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    asyncio.run(
        runtime.serve_runtime(
            application,
            environment,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            bootstrap=bootstrap,
            on_started=started,
            on_progress=events.append,
        )
    )
    assert events == [
        "bootstrap",
        "worker-starting",
        "worker",
        "worker-ready",
        "browser",
    ]
    assert application.state.worker_ready is False
    runtime_lock.acquire_exclusive(runtime._instance_lock_dir(environment)).release()


@pytest.mark.parametrize("reply", [b"garbage\n", b'{"status":"failed"}\n'])
def test_handshake_failure_never_serves_or_opens_browser(
    environment, monkeypatch, reply
):
    application = SimpleNamespace(state=SimpleNamespace())
    children = []
    progress = []

    async def start_worker(*_args):
        child = FakeWorker(reply)
        children.append(child)
        return child, null_guard()

    def unexpected():
        pytest.fail("browser opened before worker readiness")

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    with pytest.raises(runtime.RuntimeStartupError, match="could not start"):
        asyncio.run(
            runtime.serve_runtime(
                application,
                environment,
                host="127.0.0.1",
                port=21080,
                log_level="error",
                on_started=unexpected,
                on_progress=progress.append,
            )
        )
    assert children[0].returncode == 0
    assert progress == ["worker-starting"]
    assert application.state.worker_ready is False
    runtime_lock.acquire_exclusive(runtime._instance_lock_dir(environment)).release()


def test_unexpected_worker_exit_stops_http_and_reports_code(environment, monkeypatch):
    application = SimpleNamespace(state=SimpleNamespace())
    children = []
    servers = []

    async def start_worker(*_args):
        child = FakeWorker()
        children.append(child)
        return child, null_guard()

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    with pytest.raises(runtime.RuntimeStartupError, match="exit code 7"):
        asyncio.run(
            runtime.serve_runtime(
                application,
                environment,
                host="127.0.0.1",
                port=21080,
                log_level="error",
                on_server=servers.append,
                on_started=lambda: children[0].close(7),
            )
        )
    assert servers[0].should_exit is True
    assert application.state.worker_ready is False


def test_external_worker_mode_never_spawns_child(environment, monkeypatch):
    application = SimpleNamespace(state=SimpleNamespace())
    servers = []

    async def unexpected(*_args):
        pytest.fail("external mode spawned a child")

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_start_worker", unexpected)
    asyncio.run(
        runtime.serve_runtime(
            application,
            environment.model_copy(update={"worker_mode": "external"}),
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_server=servers.append,
            on_started=lambda: setattr(servers[0], "should_exit", True),
            on_progress=lambda _phase: pytest.fail(
                "external mode reported worker startup"
            ),
        )
    )
    assert application.state.worker_mode == "external"


def test_duplicate_instance_is_rejected_before_bootstrap(environment, monkeypatch):
    application = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)

    def unexpected():
        pytest.fail("duplicate runtime bootstrapped the database")

    with runtime_lock.acquire_exclusive(runtime._instance_lock_dir(environment)):
        with pytest.raises(runtime.RuntimeStartupError, match="already running"):
            asyncio.run(
                runtime.serve_runtime(
                    application,
                    environment,
                    host="127.0.0.1",
                    port=21080,
                    log_level="error",
                    bootstrap=unexpected,
                )
            )


def test_sqlite_relative_and_absolute_paths_share_instance_identity(
    environment, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    absolute = environment.model_copy(
        update={
            "database_url": f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite'}",
        }
    )
    relative = environment.model_copy(
        update={
            "database_url": "sqlite+pysqlite:///identity.sqlite",
        }
    )
    assert runtime._instance_lock_dir(absolute) == runtime._instance_lock_dir(relative)


def test_shutdown_during_handshake_stops_child_without_starting_http(
    environment, monkeypatch
):
    application = SimpleNamespace(state=SimpleNamespace())
    servers = []
    children = []

    async def start_worker(*_args):
        child = FakeWorker(ready=None)
        children.append(child)
        # Exercise the actual server's should_exit setter, as used by the
        # desktop parent watcher, without needing a network listener.
        servers[0].should_exit = True
        return child, null_guard()

    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    asyncio.run(
        runtime.serve_runtime(
            application,
            environment,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_server=servers.append,
            on_started=lambda: pytest.fail("HTTP started during shutdown"),
        )
    )
    assert children[0].returncode == 0
    assert not servers[0].started


def test_lifespan_failure_is_reported_as_runtime_failure(environment):
    @asynccontextmanager
    async def fail_startup(_app):
        raise RuntimeError("test startup failure")
        yield

    application = FastAPI(lifespan=fail_startup)
    with pytest.raises(runtime.RuntimeStartupError, match="application startup failed"):
        asyncio.run(
            runtime.serve_runtime(
                application,
                environment.model_copy(update={"worker_mode": "external"}),
                host="127.0.0.1",
                port=0,
                log_level="error",
            )
        )
