"""Resource equivalence and lifetime admission use real OS locks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.runtime_resources import RuntimeResourceError, RuntimeResources
from backend.services.ops import runtime_lock


def resources(monkeypatch, root, database, suffix=""):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(root))
    settings = SimpleNamespace(database_url=f"sqlite:///{database}{suffix}")
    return RuntimeResources.for_settings(settings, managed=True)


def test_driver_query_and_symlink_aliases_share_guards(tmp_path, monkeypatch):
    db = tmp_path / "store.sqlite"
    db.touch()
    root = tmp_path / "root"
    primary = resources(monkeypatch, root, db)
    alias = RuntimeResources.for_settings(
        SimpleNamespace(database_url=f"sqlite+pysqlite:///{db}?timeout=600"),
        managed=True,
    )
    assert primary == alias
    with primary.acquire_parent(managed=True):
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            alias.acquire_parent(managed=True)
    if hasattr(Path, "symlink_to"):
        link = tmp_path / "alias.sqlite"
        try:
            link.symlink_to(db)
        except OSError:
            return  # Native Windows may lack permission to create symlinks.
        assert resources(monkeypatch, root, link) == primary


def test_root_and_database_are_independent_conflicting_resources(tmp_path, monkeypatch):
    a = resources(monkeypatch, tmp_path / "root-a", tmp_path / "a.sqlite")
    same_db = resources(monkeypatch, tmp_path / "root-b", tmp_path / "a.sqlite")
    same_root = resources(monkeypatch, tmp_path / "root-a", tmp_path / "b.sqlite")
    independent = resources(monkeypatch, tmp_path / "root-b", tmp_path / "b.sqlite")
    with a.acquire_parent(managed=True):
        for conflicting in (same_db, same_root):
            with pytest.raises(runtime_lock.RuntimeLockHeldError):
                conflicting.acquire_parent(managed=True)
        with independent.acquire_parent(managed=True):
            pass


def test_database_tilde_is_literal_like_the_sqlite_driver(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    monkeypatch.chdir(tmp_path)
    r = resources(monkeypatch, tmp_path / "root", "~/literal.sqlite")
    engine = create_engine("sqlite:///~/literal.sqlite")
    try:
        args, _kwargs = engine.dialect.create_connect_args(engine.url)
        assert r.database == Path(args[0]) == tmp_path / "~" / "literal.sqlite"
    finally:
        engine.dispose()


def test_surviving_child_activity_blocks_replacement_and_maintenance(
    tmp_path, monkeypatch
):
    r = resources(monkeypatch, tmp_path / "root", tmp_path / "a.sqlite")
    parent = r.acquire_parent(managed=True)
    child = r.acquire_activity()
    parent.release()
    try:
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            r.acquire_parent(managed=True)
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            r.acquire_activity("exclusive")
    finally:
        child.release()
    with r.acquire_parent(managed=True):
        pass


def test_external_api_coexists_with_one_consumer_but_not_managed(tmp_path, monkeypatch):
    r = resources(monkeypatch, tmp_path / "root", tmp_path / "a.sqlite")
    with r.acquire_activity(), r.acquire_consumer():
        with r.acquire_parent(managed=False):
            with pytest.raises(runtime_lock.RuntimeLockHeldError):
                r.acquire_consumer()
            with pytest.raises(runtime_lock.RuntimeLockHeldError):
                r.acquire_parent(managed=True)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file:data?uri=true",
        "sqlite:///normal.sqlite?uri=yes&mode=ro",
        "sqlite:///normal.sqlite?uri=on",
        "sqlite:///normal.sqlite?uri=t",
        "postgresql://a/db",
    ],
)
def test_managed_refuses_unproven_storage_identity(tmp_path, monkeypatch, url):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeResourceError):
        RuntimeResources.for_settings(SimpleNamespace(database_url=url), managed=True)


def test_hardlink_is_refused_before_opening_database(tmp_path, monkeypatch):
    db = tmp_path / "a.sqlite"
    db.touch()
    try:
        (tmp_path / "alias.sqlite").hardlink_to(db)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    with pytest.raises(RuntimeResourceError):
        resources(monkeypatch, tmp_path / "root", db)


def test_strict_lock_errors_propagate_and_release_partial_admission(
    tmp_path, monkeypatch
):
    r = resources(monkeypatch, tmp_path / "root", tmp_path / "a.sqlite")
    original = runtime_lock.acquire_path

    def fail_activity(path, mode, *, strict=True):
        if path in r.activity_paths:
            raise PermissionError("cannot lock activity")
        return original(path, mode, strict=strict)

    monkeypatch.setattr(runtime_lock, "acquire_path", fail_activity)
    with pytest.raises(PermissionError):
        r.acquire_parent(managed=True)
    monkeypatch.setattr(runtime_lock, "acquire_path", original)
    with r.acquire_owner():
        pass


def test_windows_primitive_failure_is_not_reported_as_a_strict_lock(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def missing_msvcrt(name, *args, **kwargs):
        if name == "msvcrt":
            raise ImportError("platform primitive unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_msvcrt)
    # Legacy optional callers retain their documented best-effort behavior.
    assert runtime_lock._lock_windows(-1, "shared") is True
    with pytest.raises(OSError, match="could not be acquired"):
        runtime_lock._lock_windows(-1, "shared", strict=True)
