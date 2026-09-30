"""Migrations after the 0.1.1 release: atomic on SQLite, and convergent (C11, d-2).

0.1.1 publishes ``0001_initial_release -> 0002_epic1`` and freezes those files.
Every later schema change is a new revision on top of ``0002_epic1``. Two
properties make that safe for databases 0.1.1 created:

* a revision that fails part-way leaves no partial DDL behind -- SQLite DDL
  used to run in autocommit mode under Alembic, so an aborted revision left
  its first tables in place under the old stamp;
* a database created by the released ``0002_epic1`` and upgraded later ends
  at exactly the schema a fresh install of the same code gets.

The synthetic revisions below live in a throwaway copy of the script
directory, so they exercise the shipped ``env.py`` without touching the real
chain.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.ops.migration_preflight import evaluate_migration_preflight

pytestmark = pytest.mark.integration

FAILS_AFTER_DDL = '''
import sqlalchemy as sa
from alembic import op

revision = "0003_fails_after_ddl"
down_revision = "0002_epic1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "future_feature", sa.Column("id", sa.Integer, primary_key=True)
    )
    with op.batch_alter_table("workspace") as batch:
        batch.add_column(sa.Column("future_flag", sa.Text, nullable=True))
    op.execute("UPDATE workspace SET future_flag = 'set'")
    raise RuntimeError("simulated failure after DDL")


def downgrade() -> None:
    raise NotImplementedError
'''

ADDS_A_FEATURE = '''
import sqlalchemy as sa
from alembic import op

revision = "0003_adds_a_feature"
down_revision = "0002_epic1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "future_feature",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.Text, nullable=False, server_default="none"),
    )
    op.create_index("ix_future_feature_label", "future_feature", ["label"])
    with op.batch_alter_table("workspace") as batch:
        batch.add_column(sa.Column("future_flag", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace") as batch:
        batch.drop_column("future_flag")
    op.drop_index("ix_future_feature_label", table_name="future_feature")
    op.drop_table("future_feature")
'''


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _config(database_url: str, script_location: Path | None = None) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option(
        "script_location", str(script_location or resolve_migrations_dir())
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _script_directory_with(tmp_path: Path, **revisions: str) -> Path:
    """The shipped env.py and released chain, plus synthetic later revisions."""

    source = resolve_migrations_dir()
    target = tmp_path / "migrations"
    (target / "versions").mkdir(parents=True)
    for name in ("env.py", "script.py.mako"):
        shutil.copy2(source / name, target / name)
    for name in ("0001_initial_release.py", "0002_epic1.py"):
        shutil.copy2(source / "versions" / name, target / "versions" / name)
    for revision, body in revisions.items():
        (target / "versions" / f"{revision}.py").write_text(body, encoding="utf-8")
    return target


def _tables(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def _columns(database: Path, table: str) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _stamp(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute("SELECT * FROM alembic_version")]
    finally:
        connection.close()


def test_a_failing_revision_leaves_no_partial_ddl(tmp_path: Path) -> None:
    database = tmp_path / "released.sqlite"
    command.upgrade(_config(_url(database)), "0002_epic1")
    scripts = _script_directory_with(
        tmp_path, **{"0003_fails_after_ddl": FAILS_AFTER_DDL}
    )

    with pytest.raises(RuntimeError, match="simulated failure after DDL"):
        command.upgrade(_config(_url(database), scripts), "head")

    assert "future_feature" not in _tables(database)
    assert "future_flag" not in _columns(database, "workspace")
    assert _stamp(database) == ["0002_epic1"]
    assert evaluate_migration_preflight(_url(database)).verdict == "ready"


def test_a_failing_first_install_leaves_an_empty_database(tmp_path: Path) -> None:
    """The whole run is one unit: the released revisions roll back too."""

    database = tmp_path / "fresh.sqlite"
    scripts = _script_directory_with(
        tmp_path, **{"0003_fails_after_ddl": FAILS_AFTER_DDL}
    )

    with pytest.raises(RuntimeError, match="simulated failure after DDL"):
        command.upgrade(_config(_url(database), scripts), "head")

    assert _tables(database) == set()
    assert evaluate_migration_preflight(_url(database)).verdict == "fresh"


def test_a_later_revision_applies_on_top_of_the_released_chain(
    tmp_path: Path,
) -> None:
    """C11: Alembic accepts a stamp its own script directory knows."""

    database = tmp_path / "released.sqlite"
    command.upgrade(_config(_url(database)), "0002_epic1")
    scripts = _script_directory_with(
        tmp_path, **{"0003_adds_a_feature": ADDS_A_FEATURE}
    )

    command.upgrade(_config(_url(database), scripts), "head")

    assert _stamp(database) == ["0003_adds_a_feature"]
    assert "future_feature" in _tables(database)
    assert "future_flag" in _columns(database, "workspace")


def _schema(database_url: str) -> dict[str, object]:
    """Everything a revision can change, independent of creation order."""

    engine = sa.create_engine(database_url, future=True)
    try:
        inspector = sa.inspect(engine)
        schema: dict[str, object] = {}
        for table in sorted(inspector.get_table_names()):
            schema[table] = {
                "columns": sorted(
                    (
                        column["name"],
                        str(column["type"]),
                        column["nullable"],
                        str(column.get("default")),
                    )
                    for column in inspector.get_columns(table)
                ),
                "primary_key": sorted(
                    inspector.get_pk_constraint(table)["constrained_columns"]
                ),
                "foreign_keys": sorted(
                    repr(
                        (
                            key.get("name"),
                            key["constrained_columns"],
                            key["referred_table"],
                            key["referred_columns"],
                            sorted(key.get("options", {}).items()),
                        )
                    )
                    for key in inspector.get_foreign_keys(table)
                ),
                "indexes": sorted(
                    repr((index["name"], index["column_names"], index["unique"]))
                    for index in inspector.get_indexes(table)
                ),
                "unique": sorted(
                    repr((constraint.get("name"), constraint["column_names"]))
                    for constraint in inspector.get_unique_constraints(table)
                ),
                "checks": sorted(
                    repr((check.get("name"), " ".join(check["sqltext"].split())))
                    for check in inspector.get_check_constraints(table)
                ),
            }
        with engine.connect() as connection:
            schema["__triggers_and_views__"] = sorted(
                (row.type, row.name, " ".join((row.sql or "").split()))
                for row in connection.execute(
                    sa.text(
                        "SELECT type, name, sql FROM sqlite_master "
                        "WHERE type IN ('trigger', 'view')"
                    )
                )
            )
        return schema
    finally:
        engine.dispose()


def _seed_released_database(database_url: str) -> None:
    """Rows a 0.1.1 user would have, so the later upgrade runs over data."""

    from backend.app.db import make_engine, make_session_factory
    from backend.scripts.seed_dev import seed_default_workspace

    engine = make_engine(database_url)
    try:
        with make_session_factory(engine).begin() as session:
            seed_default_workspace(session)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "later_revisions",
    [{}, {"0003_adds_a_feature": ADDS_A_FEATURE}],
    ids=["shipped-chain", "with-a-later-revision"],
)
def test_a_released_0002_database_upgrades_to_the_fresh_head_schema(
    tmp_path: Path, later_revisions: dict[str, str]
) -> None:
    scripts = (
        _script_directory_with(tmp_path, **later_revisions)
        if later_revisions
        else resolve_migrations_dir()
    )
    released = _url(tmp_path / "released.sqlite")
    fresh = _url(tmp_path / "fresh.sqlite")

    # What 0.1.1 installed, used, and what a later release then runs on it.
    command.upgrade(_config(released, scripts), "0002_epic1")
    _seed_released_database(released)
    command.upgrade(_config(released, scripts), "head")
    # A first install of that later release.
    command.upgrade(_config(fresh, scripts), "head")

    assert _schema(released) == _schema(fresh)
    engine = sa.create_engine(released, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM workspace")) == 1
    finally:
        engine.dispose()
