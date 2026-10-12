"""Read-only migrated SQLite starting states, built once per test run.

A migration test that only needs a *starting* database (a head schema to
inspect or damage, a 0001 baseline to seed legacy rows onto) should not pay for
the Alembic chain that produced it. This module builds each starting state with
the real Alembic commands the first time a test asks for it and hands every
test a private byte copy.

The chain under test is never replaced: tests that exercise an upgrade or a
downgrade still run it on their copy. A template only stands in for the
database the test would otherwise have built before its own stimulus.

Templates are plain rollback-journal files (``journal_mode=delete``, no
``-wal``/``-shm`` sidecars) because Alembic uses a bare ``create_engine``; the
build refuses to publish anything else, so a copy is a faithful clone of what
the test used to construct. SQLite files embed no path, so the copy can live
anywhere.

Names:

* ``base_0001``: ``bd62193d5cf6``.
* ``rev_efcbcf724889``: pinned to ``efcbcf724889``. It does not follow ``head``.
* ``head``: whatever ``alembic upgrade head`` produces.
* ``boundary_pre_tenancy``: ``base_0001`` plus ``_upgrade_base_schema`` and the
  workspace, identity, context, library media, library policy and context-data
  phases, the schema right before the tenancy phase. A partial schema stamped
  ``bd62193d5cf6``: never expect it to be ``ready`` or a head.
* ``boundary_workspace_member``: ``base_0001`` plus ``_upgrade_base_schema``.
  Partial for the same reason.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
from collections.abc import Callable
from importlib import import_module
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_PRE_TENANCY_PHASES = (
    "workspace_schema",
    "identity_access_schema",
    "context_schema",
    "library_media_schema",
    "library_policy",
    "context_data",
)


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _run_steps(database_url: str, *steps: Callable[[], None]) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for step in steps:
                    step()
    finally:
        engine.dispose()


def _build_base_0001(database_url: str) -> None:
    command.upgrade(_config(database_url), "bd62193d5cf6")


def _build_rev_efcbcf724889(database_url: str) -> None:
    command.upgrade(_config(database_url), "efcbcf724889")


def _build_head(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")


def _build_boundary_workspace_member(database_url: str) -> None:
    epic1 = import_module("backend.migrations.versions.v0_1.efcbcf724889_core_schema")
    _build_base_0001(database_url)
    _run_steps(database_url, epic1._upgrade_base_schema)


def _build_boundary_pre_tenancy(database_url: str) -> None:
    epic1 = import_module("backend.migrations.versions.v0_1.efcbcf724889_core_schema")
    phases = [
        import_module(
            f"backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.{name}"
        ).upgrade
        for name in _PRE_TENANCY_PHASES
    ]
    _build_base_0001(database_url)
    _run_steps(database_url, epic1._upgrade_base_schema, *phases)


_BUILDERS: dict[str, Callable[[str], None]] = {
    "base_0001": _build_base_0001,
    "rev_efcbcf724889": _build_rev_efcbcf724889,
    "head": _build_head,
    "boundary_pre_tenancy": _build_boundary_pre_tenancy,
    "boundary_workspace_member": _build_boundary_workspace_member,
}


def _require_plain_rollback_journal(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode == "delete", f"template {database.name} is {mode}, not delete"
    leftovers = [
        database.name + suffix
        for suffix in _SIDECAR_SUFFIXES
        if (database.parent / (database.name + suffix)).exists()
    ]
    assert leftovers == [], f"template {database.name} has sidecars {leftovers}"


class MigratedSqliteTemplates:
    """Lazily built, flock-shared migrated templates under one directory."""

    def __init__(self, shared_dir: Path) -> None:
        self._dir = shared_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _template(self, name: str) -> Path:
        if name not in _BUILDERS:
            raise KeyError(f"unknown migrated template {name!r}")
        published = self._dir / f"{name}.sqlite"
        with open(self._dir / f"{name}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if published.exists():
                return published
            building = self._dir / f"{name}.{os.getpid()}.building"
            building.unlink(missing_ok=True)
            _BUILDERS[name](f"sqlite:///{building}")
            _require_plain_rollback_journal(building)
            building.chmod(0o444)
            os.replace(building, published)
        return published

    def clone(self, name: str, destination: Path) -> str:
        """Copy template ``name`` to a fresh ``destination``; return its URL.

        The destination must not exist: a clone is never swapped over a file a
        test (or an engine) already holds open.
        """
        assert not destination.exists(), f"{destination} already exists"
        shutil.copyfile(self._template(name), destination)
        return f"sqlite:///{destination}"


def shared_templates(tmp_path_factory) -> MigratedSqliteTemplates:
    """The run-wide templates, shared by every xdist worker of one run."""
    root = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        root = root.parent
    return MigratedSqliteTemplates(root / "migrated-sqlite-templates")
