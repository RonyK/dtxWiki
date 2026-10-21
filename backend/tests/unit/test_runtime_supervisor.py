"""Private IPC, supervised init and actual child-exit lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import signal
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend.app import runtime
from backend.app.runtime_resources import RuntimeResources
from backend.app.settings import Settings
from backend.services.ops import runtime_lock
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


class FakeWorker:
    pid = 987654321

    def __init__(self, *, role="worker", ready=None):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.exited = asyncio.Event()
        self.writes = []
        self.role = role
        self.ready = ready if ready is not None else {"status": "ready"}
        self.stdin = SimpleNamespace(
            write=self.write, drain=self.drain, close=self.close
        )

    def write(self, raw):
        self.writes.append(raw)
        if raw == b"shutdown\n":
            return
        packet = json.loads(raw)
        if "settings" in packet:
            self.stdout.feed_data(
                (
                    json.dumps({"status": "registered", "role": self.role}) + "\n"
                ).encode()
            )
        elif packet == {"command": "start"}:
            self.stdout.feed_data((json.dumps(self.ready) + "\n").encode())

    async def drain(self):
        pass

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def close(self, code=0):
        if self.returncode is None:
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


def null_guard():
    return SimpleNamespace(
        close=lambda: None, environment=lambda: {}, attach=lambda _pid: None
    )


def test_private_ipc_config_registered_start_and_same_source(environment, monkeypatch):
    settings = environment.model_copy(update={"smtp_password": "private-test-value"})
    captured = {}

    async def scenario():
        process = FakeWorker()

        async def spawn(*command, **kwargs):
            captured.update(command=command, options=kwargs)
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        children = []
        child = await runtime._start_worker(settings, None, children)
        packet = json.loads(process.writes[0])
        assert packet["protocol"] == 2
        assert packet["role"] == "worker"
        assert packet["settings"]["database_url"] == settings.database_url
        assert packet["settings"]["smtp_password"] == "private-test-value"
        assert process.writes[1] == b'{"command":"start"}\n'
        assert all("private-test-value" not in arg for arg in captured["command"])
        assert captured["command"][1:] == ("-P", "-m", "backend.worker.managed")
        assert captured["options"]["env"]["PYTHONSAFEPATH"] == "1"
        assert captured["options"]["env"]["PYTHONPATH"] == str(
            runtime.Path(runtime.__file__).resolve().parents[2]
        )
        assert children == [child]
        await runtime._stop_child(child)
        assert process.returncode == 0

    asyncio.run(scenario())


def test_init_requires_exit_zero_after_result(environment, monkeypatch):
    async def scenario():
        process = FakeWorker(
            role="init",
            ready={
                "status": "result",
                "app_version_synced": True,
                "config": {"oauth_jwt_private_key_path": "/effective.pem"},
            },
        )
        waiting = asyncio.Event()
        original_wait = process.wait

        async def wait():
            waiting.set()
            return await original_wait()

        process.wait = wait
        children = []

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        task = asyncio.create_task(runtime._initialize(environment, None, children))
        await waiting.wait()
        assert not task.done(), "a result alone must not permit app creation"
        process.close(7)
        with pytest.raises(runtime.RuntimeStartupError, match="exit code 7"):
            await task
        await runtime._stop_child(children[0])

    asyncio.run(scenario())


def test_cancel_during_spawn_retains_process_for_reaping(environment, monkeypatch):
    async def scenario():
        process = FakeWorker()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def spawn(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        children = []
        task = asyncio.create_task(
            runtime._start_child(environment, None, "worker", children)
        )
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [child.process for child in children] == [process]
        assert process.writes == [], "a cancelled spawn must never receive start"
        await runtime._stop_child(children[0])
        assert process.returncode == 0

    asyncio.run(scenario())


def test_wrong_registration_role_never_gets_start_authority(environment, monkeypatch):
    async def scenario():
        process = FakeWorker(role="init")

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        children = []
        with pytest.raises(runtime.RuntimeStartupError, match="did not register"):
            await runtime._start_child(environment, None, "worker", children)
        assert len(process.writes) == 1
        assert "settings" in json.loads(process.writes[0])
        await runtime._stop_child(children[0])

    asyncio.run(scenario())


def test_init_deadline_reaps_before_releasing_owner(environment, monkeypatch):
    children_seen = []

    async def blocked_init(_settings, _command, children):
        process = FakeWorker(role="init")
        child = runtime._Child(process, "init")
        children.append(child)
        children_seen.append(child)
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_initialize", blocked_init)
    monkeypatch.setattr(runtime, "INIT_START_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(runtime.RuntimeStartupError, match="initialization.*deadline"):
        asyncio.run(
            runtime.serve_runtime(
                None,
                environment,
                application_factory=lambda _settings: pytest.fail("init timed out"),
                host="127.0.0.1",
                port=21080,
                log_level="error",
            )
        )
    assert children_seen[0].process.returncode == 0
    RuntimeResources.for_settings(environment).acquire_parent(managed=True).release()


def test_init_success_settings_applied_only_after_reap(environment, monkeypatch):
    async def scenario():
        process = FakeWorker(
            role="init",
            ready={
                "status": "result",
                "app_version_synced": True,
                "config": {"oauth_jwt_private_key_path": "/effective.pem"},
            },
        )
        original_write = process.write

        def write(raw):
            original_write(raw)
            if raw == b'{"command":"start"}\n':
                process.close(0)

        process.stdin.write = write

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        children = []
        settings = await runtime._initialize(environment, None, children)
        assert settings.oauth_jwt_private_key_path == "/effective.pem"
        assert process.returncode == 0
        assert children == []

    asyncio.run(scenario())


def test_startup_order_init_factory_worker_bind_browser(environment, monkeypatch):
    events = []
    application = SimpleNamespace(state=SimpleNamespace())
    servers = []

    async def initialize(settings, _command, _children):
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            RuntimeResources.for_settings(settings).acquire_owner()
        events.append("init-reaped")
        return settings

    def factory(settings):
        assert events == ["init-reaped"]
        events.append("factory")
        return application

    async def start_worker(_settings, _command, children):
        assert events == ["init-reaped", "factory", "worker-starting"]
        events.append("worker")
        child = runtime._Child(FakeWorker(), "worker")
        children.append(child)
        return child

    def started():
        assert application.state.worker_ready is True
        assert application.state.app_version_synced is True
        assert servers[0].bound.is_set()
        assert servers[0].config.access_log is False
        events.append("browser")
        servers[0].should_exit = True

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_initialize", initialize)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    asyncio.run(
        runtime.serve_runtime(
            None,
            environment,
            application_factory=factory,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_started=started,
            on_progress=events.append,
            on_server=servers.append,
        )
    )
    assert events == [
        "init-reaped",
        "factory",
        "worker-starting",
        "worker",
        "worker-ready",
        "browser",
    ]
    assert application.state.worker_ready is False
    RuntimeResources.for_settings(environment).acquire_parent(managed=True).release()


@pytest.mark.parametrize(
    "reply", [{"status": "failed"}, {"status": "ready", "extra": True}]
)
def test_handshake_failure_never_serves(environment, monkeypatch, reply):
    async def scenario():
        process = FakeWorker(ready=reply)

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(runtime.RuntimeStartupError, match="could not start"):
            await runtime.serve_runtime(
                app,
                environment,
                host="127.0.0.1",
                port=21080,
                log_level="error",
                on_started=lambda: pytest.fail("served before ready"),
            )
        assert process.returncode == 0
        assert app.state.worker_ready is False

    asyncio.run(scenario())


def test_shutdown_during_config_drain_reaps_child(environment, monkeypatch):
    async def scenario():
        process = FakeWorker()
        servers = []

        async def blocked_drain():
            servers[0].should_exit = True
            await asyncio.Event().wait()

        process.stdin.drain = blocked_drain

        async def spawn(*_args, **_kwargs):
            return process

        # Cleanup must also bound a pipe that never drains. The normal stop
        # budget is overridden only to avoid spending ten seconds in this unit.
        monkeypatch.setattr(runtime, "WORKER_STOP_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(runtime, "prepare_worker_tree", null_guard)
        monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
        await runtime.serve_runtime(
            SimpleNamespace(state=SimpleNamespace()),
            environment,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_server=servers.append,
            on_started=lambda: pytest.fail("served after stop"),
        )
        assert process.returncode == -signal.SIGKILL
        assert not servers[0].bound.is_set()

    asyncio.run(scenario())


def test_unexpected_worker_exit_stops_http_and_reports_code(environment, monkeypatch):
    children = []
    servers = []

    async def start_worker(_settings, _command, tracked):
        child = runtime._Child(FakeWorker(), "worker")
        tracked.append(child)
        children.append(child)
        return child

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    with pytest.raises(runtime.RuntimeStartupError, match="exit code 7"):
        asyncio.run(
            runtime.serve_runtime(
                SimpleNamespace(state=SimpleNamespace()),
                environment,
                host="127.0.0.1",
                port=21080,
                log_level="error",
                on_server=servers.append,
                on_started=lambda: children[0].process.close(7),
            )
        )
    assert servers[0].should_exit is True


def test_normal_stop_bounds_http_teardown_before_worker_reaping(
    environment, monkeypatch
):
    children = []
    servers = []

    class StuckTeardownServer(FakeServer):
        async def serve(self):
            self.bound.set()
            await self.finished.wait()
            # Emulate an asynchronous lifespan that never completes. Parent
            # native/event-loop freezes are outside this coroutine's contract.
            await asyncio.Event().wait()

    async def start_worker(_settings, _command, tracked):
        child = runtime._Child(FakeWorker(), "worker")
        tracked.append(child)
        children.append(child)
        return child

    monkeypatch.setattr(runtime, "_RuntimeServer", StuckTeardownServer)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    monkeypatch.setattr(runtime, "HTTP_STOP_TIMEOUT_SECONDS", 0.01)
    asyncio.run(
        runtime.serve_runtime(
            SimpleNamespace(state=SimpleNamespace()),
            environment,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_server=servers.append,
            on_started=lambda: setattr(servers[0], "should_exit", True),
        )
    )
    assert children[0].process.returncode == 0


def test_stop_at_worker_readiness_does_not_start_http(environment, monkeypatch):
    servers = []
    children = []

    async def start_worker(_settings, _command, tracked):
        child = runtime._Child(FakeWorker(), "worker")
        tracked.append(child)
        children.append(child)
        asyncio.get_running_loop().call_soon(setattr, servers[0], "should_exit", True)
        return child

    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)
    monkeypatch.setattr(runtime, "_start_worker", start_worker)
    asyncio.run(
        runtime.serve_runtime(
            SimpleNamespace(state=SimpleNamespace()),
            environment,
            host="127.0.0.1",
            port=21080,
            log_level="error",
            on_server=servers.append,
            on_started=lambda: pytest.fail("HTTP started after stop request"),
        )
    )
    assert not servers[0].bound.is_set()
    assert children[0].process.returncode == 0


def test_external_worker_mode_never_spawns_worker(environment, monkeypatch):
    application = SimpleNamespace(state=SimpleNamespace())
    servers = []

    async def unexpected(*_args):
        pytest.fail("external mode spawned a worker")

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
        )
    )
    assert application.state.worker_mode == "external"


def test_duplicate_instance_is_rejected_before_init(environment, monkeypatch):
    monkeypatch.setattr(runtime, "_RuntimeServer", FakeServer)

    def unexpected(_settings):
        pytest.fail("duplicate runtime created application")

    with RuntimeResources.for_settings(environment).acquire_owner():
        with pytest.raises(runtime.RuntimeStartupError, match="already running"):
            asyncio.run(
                runtime.serve_runtime(
                    None,
                    environment,
                    application_factory=unexpected,
                    host="127.0.0.1",
                    port=21080,
                    log_level="error",
                )
            )


def test_lifespan_failure_is_reported_as_runtime_failure(environment):
    @asynccontextmanager
    async def fail_startup(_app):
        raise RuntimeError("test startup failure")
        yield

    with pytest.raises(runtime.RuntimeStartupError, match="application startup failed"):
        asyncio.run(
            runtime.serve_runtime(
                FastAPI(lifespan=fail_startup),
                environment.model_copy(update={"worker_mode": "external"}),
                host="127.0.0.1",
                port=0,
                log_level="error",
            )
        )
