"""Dedicated PostgreSQL fixtures must be reusable after a real migration."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.app.db import make_engine
from backend.models.base import Base
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.tests.integration.test_library_ready_version_ownership import (
    _assert_cross_item_pointer_is_rejected,
    _seed_owned_ready_version,
)
from backend.tests.utils.postgres import (
    create_isolated_postgres_metadata,
    drop_reflected_postgres_metadata,
)

POSTGRES_URL = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
GUARD_FUNCTIONS = (
    "dtxwiki_guard_media_evidence()",
    "dtxwiki_reject_graph_observation_mutation()",
)

pytestmark = [
    pytest.mark.xdist_group("postgres"),
    pytest.mark.skipif(
        not POSTGRES_URL or not POSTGRES_URL.startswith("postgresql"),
        reason="requires a dedicated PostgreSQL migration test database",
    ),
]


def test_cleanup_removes_migration_guards_and_allows_a_second_upgrade():
    engine = create_engine(POSTGRES_URL)
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    # An unrelated function proves cleanup targets only known migration guards,
    # rather than sweeping every routine or resetting the entire public schema.
    unrelated_function = f"test_cleanup_unrelated_{uuid4().hex}"
    drop_reflected_postgres_metadata(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION {unrelated_function}() RETURNS integer "
                    "LANGUAGE SQL AS 'SELECT 7'"
                )
            )
        for _ in range(2):
            command.upgrade(config, "head")
            with engine.connect() as connection:
                for function in GUARD_FUNCTIONS:
                    assert (
                        connection.execute(
                            text("SELECT to_regprocedure(:name)"), {"name": function}
                        ).scalar_one()
                        is not None
                    )

            drop_reflected_postgres_metadata(engine)
            assert inspect(engine).get_table_names() == []
            with engine.connect() as connection:
                for function in GUARD_FUNCTIONS:
                    assert (
                        connection.execute(
                            text("SELECT to_regprocedure(:name)"), {"name": function}
                        ).scalar_one()
                        is None
                    )
                assert (
                    connection.execute(
                        text(f"SELECT {unrelated_function}()")
                    ).scalar_one()
                    == 7
                )
    finally:
        drop_reflected_postgres_metadata(engine)
        with engine.begin() as connection:
            connection.execute(text(f"DROP FUNCTION IF EXISTS {unrelated_function}()"))
        engine.dispose()


@pytest.mark.parametrize(
    "pointer_name",
    ["current_ready_version_id", "current_preview_version_id", "current_version_id"],
)
def test_postgres_schema_creation_preserves_later_sqlite_ownership_and_fts(
    tmp_path, pointer_name
):
    """Exercise both real dialects in one interpreter, not separate workers."""

    postgres = create_engine(POSTGRES_URL)
    sqlite = make_engine(f"sqlite+pysqlite:///{tmp_path / 'after-postgres.db'}")
    ownership_name = f"fk_library_item_{pointer_name}_workspace_scoped"
    drop_reflected_postgres_metadata(postgres)
    try:
        create_isolated_postgres_metadata(postgres)
        # RI trigger names normally contain unpadded OIDs. Crossing a decimal
        # boundary can put the later ownership check first alphabetically.
        # Force the same order without allocating thousands of database OIDs;
        # the reused fixture must restore SET NULL priority before raw DELETE.
        with postgres.begin() as connection:
            actions = connection.execute(
                text(
                    "SELECT trigger.oid, trigger.tgname FROM pg_trigger AS trigger "
                    "JOIN pg_constraint AS fk ON fk.oid = trigger.tgconstraint "
                    "JOIN pg_proc AS action ON action.oid = trigger.tgfoid "
                    "WHERE trigger.tgrelid = 'library_item_version'::regclass "
                    "AND fk.conrelid = 'library_item'::regclass "
                    "AND action.proname = 'RI_FKey_setnull_del'"
                )
            ).mappings()
            quote = postgres.dialect.identifier_preparer.quote
            for action in actions:
                probe_name = f"zz_probe_setnull_{action['oid']}"
                connection.exec_driver_sql(
                    f"ALTER TRIGGER {quote(action['tgname'])} ON library_item_version "
                    f"RENAME TO {quote(probe_name)}"
                )
        # Match create_all's checkfirst lifecycle: a reused schema must not
        # receive duplicate deferred constraints or change their trigger order.
        create_isolated_postgres_metadata(postgres)
        # Ensure the new helper really creates every declared FK on PostgreSQL,
        # rather than making the SQLite control pass by omitting the PG schema.
        inspector = inspect(postgres)
        assert set(inspector.get_table_names()) == set(Base.metadata.tables)
        for table in Base.metadata.tables.values():
            reflected = inspector.get_foreign_keys(table.name)
            assert len(reflected) == len(table.foreign_key_constraints), table.name
            # ``conv`` names over PostgreSQL's identifier limit receive the
            # dialect's deterministic hash suffix, not their full model name.
            declared_names = {
                postgres.dialect.identifier_preparer.format_constraint(
                    constraint, _alembic_quote=False
                )
                for constraint in table.foreign_key_constraints
                if constraint.name is not None
            }
            assert declared_names <= {fk["name"] for fk in reflected}, table.name
            actual = {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                    fk["options"].get("ondelete"),
                    fk["options"].get("onupdate"),
                )
                for fk in reflected
            }
            expected = {
                (
                    tuple(column.name for column in constraint.columns),
                    constraint.elements[0].column.table.name,
                    tuple(fk.column.name for fk in constraint.elements),
                    constraint.ondelete,
                    constraint.onupdate,
                )
                for constraint in table.foreign_key_constraints
            }
            assert actual == expected, table.name

        # Leave SQLite's real metadata hooks intact, including FTS5 projections.
        Base.metadata.create_all(sqlite)
        with sqlite.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert {"search_doc_fts", "page_name_trgm"} <= set(
            inspect(sqlite).get_table_names()
        )
        for engine in (postgres, sqlite):
            foreign_keys = {
                fk["name"]: fk
                for fk in inspect(engine).get_foreign_keys("library_item")
            }
            assert foreign_keys[ownership_name]["constrained_columns"] == [
                "workspace_id",
                pointer_name,
                "id",
            ]
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            item_id, owned_version_id, foreign_version_id = _seed_owned_ready_version(
                sessions
            )
            with sessions.begin() as session:
                item = session.get(LibraryItem, item_id)
                assert item is not None
                setattr(item, pointer_name, owned_version_id)
                session.flush()
            if engine.dialect.name == "postgresql":
                # pg8000 classifies server 23503 as ProgrammingError, unlike
                # SQLite's IntegrityError. Pin the server code AND constraint
                # instead of accepting any generic DBAPI failure as rejection.
                with pytest.raises(DBAPIError) as rejected:
                    with sessions.begin() as session:
                        item = session.get(LibraryItem, item_id)
                        assert item is not None
                        setattr(item, pointer_name, foreign_version_id)
                        session.flush()
                error = rejected.value.orig.args[0]
                assert error["C"] == "23503"
                assert error["n"] == ownership_name
                with sessions() as session:
                    item = session.get(LibraryItem, item_id)
                    assert item is not None
                    assert getattr(item, pointer_name) == owned_version_id
            elif pointer_name == "current_ready_version_id":
                _assert_cross_item_pointer_is_rejected(
                    sessions,
                    first_item_id=item_id,
                    first_version_id=owned_version_id,
                    second_version_id=foreign_version_id,
                )
            else:
                with pytest.raises(IntegrityError):
                    with sessions.begin() as session:
                        item = session.get(LibraryItem, item_id)
                        assert item is not None
                        setattr(item, pointer_name, foreign_version_id)
                        session.flush()
                with sessions() as session:
                    item = session.get(LibraryItem, item_id)
                    assert item is not None
                    assert getattr(item, pointer_name) == owned_version_id

            # Both the raw DELETE and its FK SET NULL action are transactional.
            # A rollback must restore the owned version and original pointers.
            with sessions() as session:
                session.execute(
                    delete(LibraryItemVersion).where(
                        LibraryItemVersion.id == owned_version_id
                    )
                )
                session.expire_all()
                item = session.get(LibraryItem, item_id)
                assert item is not None
                assert getattr(item, pointer_name) is None
                assert item.current_ready_version_id is None
                session.rollback()
            with sessions() as session:
                item = session.get(LibraryItem, item_id)
                assert item is not None
                assert getattr(item, pointer_name) == owned_version_id
                assert item.current_ready_version_id == owned_version_id
                assert session.get(LibraryItemVersion, owned_version_id) is not None
            with sessions.begin() as session:
                session.execute(
                    delete(LibraryItemVersion).where(
                        LibraryItemVersion.id == owned_version_id
                    )
                )
            with sessions() as session:
                item = session.get(LibraryItem, item_id)
                assert item is not None
                assert getattr(item, pointer_name) is None
                assert item.current_ready_version_id is None
                assert session.get(LibraryItemVersion, owned_version_id) is None
                assert session.get(LibraryItemVersion, foreign_version_id) is not None
    finally:
        sqlite.dispose()
        drop_reflected_postgres_metadata(postgres)
        postgres.dispose()
