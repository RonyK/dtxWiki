import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.app.db import make_engine
from backend.models import Base
from backend.models.workspace import Workspace
from backend.scripts.migrate_data import (
    MigrationPrecheckError,
    migrate_data,
    verify_alembic_revision,
    verify_target_empty,
)
from backend.services.ops import runtime_lock

REVISION = "0002_epic1"


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _create_schema(url: str, *, revision: str | None = REVISION) -> None:
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        if revision is not None:
            _stamp_revision(engine, revision)
    finally:
        engine.dispose()


def _stamp_revision(engine, revision: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(64) NOT NULL)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )


def _create_schema_without_wal(url: str, *, revision: str = REVISION) -> None:
    """Build a source database that the application engine has never opened."""

    engine = create_engine(url, future=True)
    try:
        Base.metadata.create_all(engine)
        _stamp_revision(engine, revision)
    finally:
        engine.dispose()


def _drop_final_marker_table(url: str) -> None:
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE workspace_job_hint"))
    finally:
        engine.dispose()


def _file_fingerprint(database: Path):
    return (
        hashlib.sha256(database.read_bytes()).hexdigest(),
        Path(f"{database}-wal").exists(),
        Path(f"{database}-shm").exists(),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_refuses_while_a_dtxwiki_runtime_holds_the_lock(tmp_path: Path) -> None:
    """C10: an idle server holds no SQLite lock, but it holds this one."""

    source_url = _url(tmp_path / "source.sqlite")
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema(source_url)
    _create_schema(target_url)
    data_dir = tmp_path / "data-dir"
    server = runtime_lock.acquire_shared(data_dir)
    try:
        with pytest.raises(
            MigrationPrecheckError, match=r"stop dtxWiki \(server and worker\) first"
        ):
            migrate_data(source_url, target_url, data_dir=data_dir)
    finally:
        assert server is not None
        server.release()

    report = migrate_data(source_url, target_url, data_dir=data_dir)

    assert report.source_rows == 0
    runtime_lock.acquire_exclusive(data_dir).release()


def test_rejects_same_source_and_target_url(tmp_path: Path) -> None:
    url = _url(tmp_path / "same.sqlite")
    _create_schema(url)

    with pytest.raises(MigrationPrecheckError, match="must differ"):
        migrate_data(url, url)


def test_rejects_missing_target_alembic_revision(tmp_path: Path) -> None:
    source_url = _url(tmp_path / "source.sqlite")
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema(source_url)
    _create_schema(target_url, revision=None)

    with pytest.raises(MigrationPrecheckError, match="target database has no"):
        verify_alembic_revision(source_url, target_url)


def test_rejects_revision_mismatch(tmp_path: Path) -> None:
    source_url = _url(tmp_path / "source.sqlite")
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema(source_url, revision="a")
    _create_schema(target_url, revision="b")

    with pytest.raises(MigrationPrecheckError, match="revisions differ"):
        verify_alembic_revision(source_url, target_url)


def test_rejects_non_empty_target_without_force(tmp_path: Path) -> None:
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema(target_url)
    engine = make_engine(target_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                Workspace.__table__.insert(),
                {
                    "slug": "occupied",
                    "title": "Occupied",
                    "name": "Occupied",
                },
            )

        with pytest.raises(MigrationPrecheckError, match="already contains"):
            verify_target_empty(engine, force=False)

        verify_target_empty(engine, force=True)
    finally:
        engine.dispose()


def test_rejects_source_whose_stamp_does_not_describe_its_schema(
    tmp_path: Path,
) -> None:
    """A laundered stamp must not be copied into a freshly built backend.

    The revision-equality precheck alone accepts it: both ends read
    ``0002_epic1`` and the copy only fails later on a missing source table.
    """
    source_url = _url(tmp_path / "source.sqlite")
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema(source_url)
    _create_schema(target_url)
    _drop_final_marker_table(source_url)

    with pytest.raises(MigrationPrecheckError, match="stamp_marker_conflict"):
        migrate_data(source_url, target_url)


def test_precheck_refusal_leaves_the_source_file_unconverted(
    tmp_path: Path,
) -> None:
    """Prechecks must not open the read-only side with the WAL write engine."""
    source_path = tmp_path / "source.sqlite"
    source_url = _url(source_path)
    target_url = _url(tmp_path / "target.sqlite")
    _create_schema_without_wal(source_url)
    _create_schema(target_url, revision=None)
    before = _file_fingerprint(source_path)

    with pytest.raises(MigrationPrecheckError, match="no alembic_version table"):
        migrate_data(source_url, target_url)

    assert _file_fingerprint(source_path) == before
