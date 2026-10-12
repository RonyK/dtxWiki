"""Compatibility round-trip tests for ``backend.scripts.migrate_data``.

The purpose is to prove SQLite and PostgreSQL backends accept the same
dtxWiki data via metadata-driven copy. The SQLite round-trip runs in CI
unconditionally; the PostgreSQL round-trip skips when no PG URL is set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    func,
    select,
    text,
)
from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine
from backend.models import Base
from backend.models.workspace import Workspace
from backend.scripts.migrate_data import _resolve_order, migrate_data
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres_reset import reset_postgres_test_database

TEST_REVISION = "efcbcf724889"


def _create_schema(url: str) -> None:
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        _stamp_revision(engine, TEST_REVISION)
    finally:
        engine.dispose()


def _seed(url: str) -> None:
    engine = make_engine(url)
    try:
        Base.metadata.create_all(engine)
        _stamp_revision(engine, TEST_REVISION)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with session_factory.begin() as session:
            seed_default_workspace(session)
    finally:
        engine.dispose()


def _row_counts(url: str) -> dict[str, int]:
    engine = make_engine(url)
    counts: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for table in Base.metadata.tables.values():
                counts[table.name] = (
                    conn.execute(select(func.count()).select_from(table)).scalar() or 0
                )
    finally:
        engine.dispose()
    return counts


def _postgres_test_url() -> str | None:
    url = os.getenv("DTXWIKI_POSTGRES_TEST_DATABASE_URL") or os.getenv(
        "DTXWIKI_DATABASE_URL"
    )
    return url if url and url.startswith("postgresql") else None


def _reset_postgres(url: str) -> None:
    engine = make_engine(url)
    try:
        # Not Base.metadata.drop_all: the ORM's unnamed users<->workspace FK
        # cycle makes it unsortable. Dropping the schema also removes the
        # alembic_version stamp.
        reset_postgres_test_database(engine)
        Base.metadata.create_all(engine)
        _stamp_revision(engine, TEST_REVISION)
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


def _composite_ownership_cycle(
    *,
    pointer_nullable: bool,
    match: str | None = None,
) -> MetaData:
    metadata = MetaData()
    owner = Table(
        "owner",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("selected_child_id", Integer, nullable=pointer_nullable),
    )
    Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner_id", Integer, nullable=False),
        ForeignKeyConstraint(["owner_id"], ["owner.id"]),
    )
    owner.append_constraint(
        ForeignKeyConstraint(
            ["selected_child_id", "id"],
            ["child.id", "child.owner_id"],
            match=match,
        )
    )
    return metadata


def test_resolve_order_defers_only_nullable_part_of_composite_ownership_fk() -> None:
    metadata = _composite_ownership_cycle(pointer_nullable=True)

    ordered, deferred = _resolve_order(metadata)

    assert [table.name for table in ordered] == ["owner", "child"]
    assert deferred == {"owner": frozenset({"selected_child_id"})}


@pytest.mark.parametrize(
    ("pointer_nullable", "match"),
    [
        (False, None),
        (True, "FULL"),
    ],
    ids=("all-columns-required", "match-full-partial-null-is-not-optional"),
)
def test_resolve_order_still_rejects_required_composite_fk_cycles(
    pointer_nullable: bool,
    match: str | None,
) -> None:
    metadata = _composite_ownership_cycle(
        pointer_nullable=pointer_nullable,
        match=match,
    )

    with pytest.raises(RuntimeError, match="Cyclic NOT NULL foreign keys"):
        _resolve_order(metadata)


def test_sqlite_to_sqlite_roundtrip(tmp_path: Path) -> None:
    src = f"sqlite:///{tmp_path / 'src.sqlite'}"
    dst = f"sqlite:///{tmp_path / 'dst.sqlite'}"
    _seed(src)
    _create_schema(dst)

    report = migrate_data(src, dst)

    assert sum(report.values()) > 0
    assert report.inserted_rows == report.source_rows
    assert report.skipped_rows == 0
    assert _row_counts(src) == _row_counts(dst)

    rerun = migrate_data(src, dst, force=True)

    assert rerun.inserted_rows == 0
    assert rerun.skipped_rows == rerun.source_rows

    engine = make_engine(dst)
    try:
        with engine.connect() as conn:
            workspace_slug = conn.execute(
                select(Workspace.slug).where(Workspace.slug == "default")
            ).scalar()
        assert workspace_slug == "default"
    finally:
        engine.dispose()


def test_copy_reads_source_in_batches(tmp_path: Path, monkeypatch) -> None:
    # --batch-size must bound the READ side too: the source is consumed via
    # fetchmany(batch_size), never materialized whole into memory.
    from sqlalchemy.engine.result import MappingResult

    src = f"sqlite:///{tmp_path / 'src.sqlite'}"
    dst = f"sqlite:///{tmp_path / 'dst.sqlite'}"
    _seed(src)
    engine = make_engine(src)
    try:
        with engine.begin() as conn:
            conn.execute(
                Workspace.__table__.insert(),
                [
                    {"slug": f"ws-{i}", "title": f"WS {i}", "name": f"WS {i}"}
                    for i in range(25)
                ],
            )
    finally:
        engine.dispose()
    _create_schema(dst)

    sizes: list[int | None] = []
    original_fetchmany = MappingResult.fetchmany

    def spy(self, size=None):
        sizes.append(size)
        return original_fetchmany(self, size)

    monkeypatch.setattr(MappingResult, "fetchmany", spy)

    report = migrate_data(src, dst, batch_size=10)

    assert sizes and all(size == 10 for size in sizes)
    workspace_rows = report.tables["workspace"].source_rows
    assert workspace_rows >= 25
    # the workspace table spans multiple batches: at least ceil(rows/10) fetches
    assert len(sizes) > workspace_rows // 10
    assert _row_counts(src) == _row_counts(dst)


def test_dry_run_streams_and_counts_without_inserting(tmp_path: Path) -> None:
    src = f"sqlite:///{tmp_path / 'src.sqlite'}"
    dst = f"sqlite:///{tmp_path / 'dst.sqlite'}"
    _seed(src)
    _create_schema(dst)

    report = migrate_data(src, dst, dry_run=True, batch_size=2)

    assert report.dry_run is True
    assert report.source_rows > 0
    assert report.inserted_rows == 0
    assert report.skipped_rows == 0
    assert all(count == 0 for count in _row_counts(dst).values())


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason=(
        "requires DTXWIKI_POSTGRES_TEST_DATABASE_URL or PostgreSQL DTXWIKI_DATABASE_URL"
    ),
)
def test_sqlite_to_postgres_roundtrip(tmp_path: Path) -> None:
    src = f"sqlite:///{tmp_path / 'src.sqlite'}"
    dst = _postgres_test_url()
    assert dst is not None

    _reset_postgres(dst)
    _seed(src)

    report = migrate_data(src, dst)

    assert sum(report.values()) > 0
    assert _row_counts(src) == _row_counts(dst)

    engine = make_engine(dst)
    try:
        with engine.connect() as conn:
            workspace_slug = conn.execute(
                select(Workspace.slug).where(Workspace.slug == "default")
            ).scalar()
        assert workspace_slug == "default"
    finally:
        engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason=(
        "requires DTXWIKI_POSTGRES_TEST_DATABASE_URL or PostgreSQL DTXWIKI_DATABASE_URL"
    ),
)
def test_postgres_to_sqlite_roundtrip(tmp_path: Path) -> None:
    src = _postgres_test_url()
    dst = f"sqlite:///{tmp_path / 'dst.sqlite'}"
    assert src is not None

    _reset_postgres(src)
    _seed(src)
    _create_schema(dst)

    report = migrate_data(src, dst)

    assert sum(report.values()) > 0
    assert _row_counts(src) == _row_counts(dst)
