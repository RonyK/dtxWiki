"""A bootstrapped, unseeded SQLite database that tests copy instead of rebuild."""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from backend.app.bootstrap import bootstrap
from backend.app.settings import Settings

_template: Path | None = None


def migrated_sqlite_template() -> Path:
    """An unseeded, fully migrated database, built once per pytest process.

    Most CLI and worker tests need an empty database at the current head, not
    the migration itself: a fresh Alembic upgrade costs about two seconds of
    CPU, and a byte copy costs a millisecond. The template is exactly what
    ``bootstrap`` leaves behind (head revision, version marker, WAL header), has
    no workspaces or users, and has its WAL checkpointed away so a copy carries
    no sidecar. Tests that are about the migration or the bootstrap path itself
    keep running ``bootstrap`` for real.
    """

    global _template
    if _template is not None and _template.is_file():
        return _template
    root = Path(tempfile.mkdtemp(prefix="dtxwiki-migrated-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    template = root / "template.sqlite"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DTXWIKI_DATA_DIR", str(root / "data-dir"))
        bootstrap(Settings(database_url=f"sqlite:///{template}"))
    connection = sqlite3.connect(template)
    try:
        busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0
    finally:
        connection.close()
    assert not Path(f"{template}-wal").exists()
    assert not Path(f"{template}-shm").exists()
    _template = template
    return template


def copy_migrated_sqlite(destination: Path) -> str:
    """Put a fresh copy of the migrated template at ``destination``; return its URL."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_sqlite_template(), destination)
    return f"sqlite:///{destination}"
