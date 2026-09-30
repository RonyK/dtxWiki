import pytest
from sqlalchemy import select

from backend.app import bootstrap as bootstrap_module
from backend.app.bootstrap import bootstrap
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.models.workspace import Workspace


def test_bootstrap_creates_sqlite_db_without_a_global_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings()

    bootstrap(settings)

    assert (tmp_path / "dtxwiki" / "data" / "dtxwiki.sqlite").is_file()
    factory = make_session_factory(make_engine(settings.database_url))
    with factory.begin() as session:
        workspaces = session.scalars(select(Workspace)).all()
    assert workspaces == []


def test_bootstrap_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings()

    bootstrap(settings)
    bootstrap(settings)

    factory = make_session_factory(make_engine(settings.database_url))
    with factory.begin() as session:
        workspaces = session.scalars(select(Workspace)).all()
    assert workspaces == []


def test_bootstrap_disabled_verifies_instead_of_creating_anything(
    tmp_path, monkeypatch
):
    """auto_bootstrap=false never migrates: an absent database is refused."""
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings(auto_bootstrap=False)

    with pytest.raises(
        bootstrap_module.DatabaseNotCurrentError, match="'dtxwiki bootstrap'"
    ):
        bootstrap(settings)

    assert not (tmp_path / "dtxwiki").exists()


def test_bootstrap_disabled_accepts_a_database_the_operator_bootstrapped(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    bootstrap(Settings())
    calls = []
    monkeypatch.setattr(
        bootstrap_module, "run_alembic_upgrade", lambda url: calls.append(url)
    )

    bootstrap(Settings(auto_bootstrap=False))

    assert calls == []


def test_bootstrap_disposes_every_engine_it_creates(tmp_path, monkeypatch):
    # ensure_app_version_synced / _read_db_version must not
    # rely on GC for connection cleanup: every engine they build gets an
    # explicit dispose() (same pattern as current_db_alembic_revision).
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings()
    created = []
    disposed = []
    real_make_engine = bootstrap_module.make_engine

    def tracking_make_engine(url):
        engine = real_make_engine(url)
        original_dispose = engine.dispose

        def dispose(*args, **kwargs):
            disposed.append(engine)
            return original_dispose(*args, **kwargs)

        engine.dispose = dispose
        created.append(engine)
        return engine

    monkeypatch.setattr(bootstrap_module, "make_engine", tracking_make_engine)

    bootstrap(settings)

    assert created
    assert disposed == created


def test_bootstrap_refuses_default_session_secret_outside_solo_local(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings(app_mode="team")

    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        bootstrap(settings)

    assert not (tmp_path / "dtxwiki").exists()


def test_bootstrap_accepts_real_session_secret_for_team_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "dtxwiki"))
    settings = Settings(
        app_mode="team",
        session_secret="a-real-secret-that-is-not-default",
    )

    bootstrap(settings)

    assert (tmp_path / "dtxwiki" / "data" / "dtxwiki.sqlite").is_file()


def test_a_served_app_holds_the_runtime_lock_until_its_lifespan_ends(
    tmp_path, monkeypatch
):
    """C10: `create_app(bootstrap_on_startup=True)` takes the shared lock."""
    from fastapi.testclient import TestClient

    from backend.app.main import create_app
    from backend.services.ops import runtime_lock

    data_dir = tmp_path / "dtxwiki"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(data_dir))

    app = create_app(settings=Settings(), bootstrap_on_startup=True)

    with pytest.raises(runtime_lock.RuntimeLockHeldError, match="stop dtxWiki"):
        runtime_lock.acquire_exclusive(data_dir)
    with TestClient(app):
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            runtime_lock.acquire_exclusive(data_dir)
    runtime_lock.acquire_exclusive(data_dir).release()


def test_a_served_app_refuses_to_start_while_maintenance_holds_the_lock(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI

    from backend.services.ops import runtime_lock

    data_dir = tmp_path / "dtxwiki"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(data_dir))
    maintenance = runtime_lock.acquire_exclusive(data_dir)
    try:
        with pytest.raises(
            runtime_lock.RuntimeLockHeldError, match="update, rollback or migrate"
        ):
            bootstrap(Settings(), FastAPI())
    finally:
        maintenance.release()

    assert not (data_dir / "data" / "dtxwiki.sqlite").exists()


def test_a_refused_bootstrap_gives_the_runtime_lock_back(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from backend.services.ops import runtime_lock

    data_dir = tmp_path / "dtxwiki"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(data_dir))
    app = FastAPI()

    with pytest.raises(bootstrap_module.DatabaseNotCurrentError):
        bootstrap(Settings(auto_bootstrap=False), app)

    runtime_lock.acquire_exclusive(data_dir).release()


def test_bootstrap_without_an_app_takes_no_runtime_lock(tmp_path, monkeypatch):
    """`dtxwiki update --finalize-bootstrap` migrates under update's own lock."""
    from backend.services.ops import runtime_lock

    data_dir = tmp_path / "dtxwiki"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(data_dir))
    with runtime_lock.acquire_exclusive(data_dir):
        bootstrap(Settings())

    assert (data_dir / "data" / "dtxwiki.sqlite").is_file()
