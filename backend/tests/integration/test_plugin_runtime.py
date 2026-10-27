"""Plugins compose under core namespaces and retain independent schema stamps."""

# Budget exception: two existing traversals cover application/queue lifecycle
# and an independent plugin Alembic chain. Real queue outcomes count separately;
# direct callback checks that duplicated queue success/failure paths were removed.

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import typer
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import ForeignKey, Index, Integer, Uuid, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from typer.testing import CliRunner

from backend.app import plugins
from backend.app.bootstrap import (
    bootstrap_core,
    current_alembic_head,
    ensure_app_version_synced,
    ensure_data_dirs,
)
from backend.app.settings import Settings
from backend.scripts.seed_dev import seed_default_workspace
from backend.spi.plugins import (
    DtxwikiPlugin,
    JobHandler,
    JobInput,
    JobResult,
    PermanentJobError,
    PluginMigrations,
    PluginMount,
    RetryableJobError,
    WorkerContext,
)
from backend.worker.runtime import OneShotWorker


def test_plugin_routes_lifespan_cli_and_worker_share_enabled_instance(
    monkeypatch, session_factory
):
    events = []
    configured = []
    router = APIRouter()

    @router.get("/probe")
    def probe():
        return {"plugin": "example"}

    @asynccontextmanager
    async def lifespan(_app):
        events.append("start")
        yield
        events.append("stop")

    cli = typer.Typer()

    @cli.command()
    def probe_cli():
        typer.echo("example command")

    def business(context, job):
        assert isinstance(context, WorkerContext)
        assert isinstance(job, JobInput)
        with pytest.raises(TypeError):
            job.payload["nested"]["secret"] = "mutated"
        if job.payload.get("failure") == "permanent":
            raise PermanentJobError("example_permanent")
        if job.payload.get("failure") == "retryable":
            raise RetryableJobError("example_retryable")
        if job.payload.get("failure") == "unexpected":
            raise RuntimeError("secret-from-plugin")
        return JobResult({"plugin": "example", "nested": ["json"]})

    failures = []
    handler = JobHandler(
        business=business,
        on_failure=lambda context, job, error: failures.append((context, job, error)),
    )
    plugin = DtxwikiPlugin(
        name="example",
        version="1.0",
        requires_core=">=0.1,<1",
        requires_spi=">=1.1,<2",
        configure=configured.append,
        mount=lambda: PluginMount(
            api_router=router,
            ui_app=Starlette(routes=[Route("/", lambda _: PlainTextResponse("UI"))]),
        ),
        lifespan=lifespan,
        worker_handlers=lambda: {"example_job": handler},
        cli=cli,
    )
    monkeypatch.setattr(
        plugins.metadata,
        "entry_points",
        lambda **_: [SimpleNamespace(name="example", load=lambda: plugin)],
    )
    settings = Settings(plugins="example", app_version="0.1.3", _env_file=None)
    registry = plugins.load_plugins(settings)
    app = FastAPI(lifespan=registry.lifespan)
    registry.mount(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/ext/example/probe").json() == {"plugin": "example"}
        assert client.get("/ext/example/").text == "UI"
        assert client.get("/probe").status_code == 404
        assert events == ["start"]
    assert events == ["start", "stop"]
    root = typer.Typer()
    registry.install_cli(root)
    command = CliRunner().invoke(root, ["example", "probe-cli"])
    assert command.exit_code == 0, command.output
    assert command.output.strip() == "example command"
    with pytest.raises(plugins.PluginConfigurationError, match="already registered"):
        registry.install_cli(root)
    worker = OneShotWorker(settings=settings)
    assert worker._handler_for(SimpleNamespace(job_type="example_job")) is handler
    assert worker._handler_for(SimpleNamespace(job_type="extraction")) is not None
    # Execute each distinct outcome through the real core-owned queue. The
    # callback also verifies immutable nested input on these same invocations.
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    for failure, expected, error in (
        (None, "completed", None),
        ("permanent", "failed", "example_permanent"),
        ("retryable", "queued", "example_retryable"),
        ("unexpected", "queued", "plugin_job_failed"),
    ):
        with session_factory.begin() as session:
            queued = worker.job_queue.enqueue(
                session=session,
                workspace_id=workspace_id,
                job_type="example_job",
                trigger_type="manual",
                target_ref={
                    "nested": {"secret": "original"},
                    "failure": failure,
                },
            )
            queued_id = queued.id
        dispatched = worker.dispatch_once_with_session_factory(
            session_factory,
            workspace_id=workspace_id,
            job_types={"example_job"},
        )
        assert dispatched.job_id == queued_id
        assert dispatched.status == expected
        if error is None:
            assert dispatched.detail == {"plugin": "example", "nested": ["json"]}
        else:
            assert dispatched.detail["error"] == error
    assert len(failures) == 1
    assert failures[0][2].code == "example_permanent"
    assert failures[0][2].retryable is False
    collision = replace(plugin, worker_handlers=lambda: {"extraction": handler})
    with pytest.raises(plugins.PluginConfigurationError, match="worker handler"):
        plugins.PluginRegistry((collision,)).worker_handlers()

    # Exercise the real CLI entry path as well as registry composition. Each
    # invocation owns one instance, even when root and bootstrap both need it.
    from backend.cli import main as cli_main

    monkeypatch.setenv("DTXWIKI_PLUGINS", "example")
    configured.clear()
    command = CliRunner().invoke(cli_main.app, ["example", "probe-cli"])
    assert command.exit_code == 0, command.output
    assert command.output.strip() == "example command"
    assert configured == [None]
    bootstrapped = []

    def bootstrap(settings):
        bootstrapped.append(settings)
        plugins.load_plugins(settings)

    monkeypatch.setattr(cli_main, "bootstrap_app", bootstrap)
    command = CliRunner().invoke(
        cli_main.app, ["bootstrap", "--db", "sqlite:///override.db"]
    )
    assert command.exit_code == 0, command.output
    assert bootstrapped[0].database_url == "sqlite:///override.db"
    assert configured == [None, None]


def test_plugin_migration_chain_preserves_core_stamp_and_validates_model_ownership(
    tmp_path, monkeypatch
):
    class PluginBase(DeclarativeBase):
        pass

    class PluginRecord(PluginBase):
        __tablename__ = "example_records"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)

    directory = tmp_path / "plugin"
    versions = directory / "versions"
    versions.mkdir(parents=True)
    (versions / "initial.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = 'example_initial'\n"
        "down_revision = None\n"
        "def upgrade():\n"
        "    op.create_table('example_records', sa.Column('id', sa.Integer, "
        "primary_key=True))\n"
        "def downgrade():\n"
        "    op.drop_table('example_records')\n"
    )
    plugin = DtxwikiPlugin(
        name="example",
        version="1.0",
        requires_core=">=0.1,<1",
        requires_spi=">=1.1,<2",
        migrations=PluginMigrations(directory, PluginBase, "control"),
    )
    monkeypatch.setattr(
        plugins.metadata,
        "entry_points",
        lambda **_: [SimpleNamespace(name="example", load=lambda: plugin)],
    )
    settings = Settings(
        plugins="example",
        app_version="0.1.3",
        database_url=f"sqlite:///{tmp_path / 'plugins.db'}",
        session_secret="s" * 64,
        _env_file=None,
    )
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    ensure_data_dirs()
    ensure_app_version_synced(settings)
    engine = create_engine(settings.database_url)
    registry = plugins.load_plugins(settings)
    with pytest.raises(plugins.PluginConfigurationError, match="not current"):
        registry.migrate(settings, allow_upgrade=False)
    bootstrap_core(settings)
    # A second real core preflight must accept the plugin-owned extra tables.
    bootstrap_core(settings)
    registry.migrate(settings, allow_upgrade=False)
    with engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == current_alembic_head()
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version_example"))
            == "example_initial"
        )
        connection.execute(text("INSERT INTO example_records (id) VALUES (1)"))
    # Plugin chains use a different migration environment from core. A late
    # DDL failure must roll back before retrying; core-chain tests cannot
    # exercise this independent engine/transaction failure path.
    next_revision = versions / "next.py"
    next_revision.write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = 'example_next'\n"
        "down_revision = 'example_initial'\n"
        "def upgrade():\n"
        "    op.create_table('example_temporary', sa.Column('id', sa.Integer, "
        "primary_key=True))\n"
        "    raise RuntimeError('late plugin migration failure')\n"
        "def downgrade():\n"
        "    op.drop_table('example_temporary')\n"
    )
    with pytest.raises(RuntimeError, match="late plugin migration"):
        registry.migrate(settings, allow_upgrade=True)
    assert "example_temporary" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version_example"))
            == "example_initial"
        )
    next_revision.write_text(
        next_revision.read_text().replace(
            "    raise RuntimeError('late plugin migration failure')\n", ""
        )
    )
    registry.migrate(settings, allow_upgrade=True)
    registry.migrate(settings, allow_upgrade=False)
    assert "example_temporary" in inspect(engine).get_table_names()
    from backend.models.base import Base

    class InvalidBase(DeclarativeBase):
        pass

    class ExternalReference(InvalidBase):
        __tablename__ = "example_external_reference"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        user_id: Mapped[object] = mapped_column(ForeignKey("users.id"))

    class WorkspacePluginBase(DeclarativeBase):
        pass

    class WorkspacePluginRecord(WorkspacePluginBase):
        __tablename__ = "example_workspace_records"
        __table_args__ = (Index("ix_example_workspace", "workspace_id", "id"),)
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        workspace_id: Mapped[object] = mapped_column(Uuid)

    # Even a formerly valid workspace model must fail before migration;
    # core's workspace purge cannot reach independent plugin metadata.
    for plane in ("workspace", "control"):
        with pytest.raises(
            plugins.PluginConfigurationError, match="workspace deletion lifecycle"
        ):
            plugins._validate_migrations(
                replace(
                    plugin,
                    migrations=PluginMigrations(directory, WorkspacePluginBase, plane),
                )
            )

    for migration in (
        PluginMigrations(directory, Base, "control"),
        PluginMigrations(directory, PluginBase, "workspace"),
        PluginMigrations(directory, InvalidBase, "control"),
    ):
        with pytest.raises(plugins.PluginConfigurationError):
            plugins._validate_migrations(replace(plugin, migrations=migration))
    other = replace(plugin, name="second")
    monkeypatch.setattr(
        plugins.metadata,
        "entry_points",
        lambda **_: [
            SimpleNamespace(name="example", load=lambda: plugin),
            SimpleNamespace(name="second", load=lambda: other),
        ],
    )
    with pytest.raises(plugins.PluginConfigurationError, match="collides with another"):
        plugins.load_plugins(settings.model_copy(update={"plugins": "example,second"}))
    engine.dispose()
