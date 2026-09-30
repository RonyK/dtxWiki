"""The PostgreSQL reset helper the ``create_all``-based suites start from.

The two refusal cases need no database: the helper must reject the engine
before it ever connects. The reset itself needs a real PostgreSQL database,
because the case it exists for -- a populated ``users``/``workspace`` foreign
key cycle that ``Base.metadata.drop_all`` cannot sort -- does not exist on
SQLite.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres_reset import reset_postgres_test_database


def _postgres_test_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


@pytest.mark.parametrize(
    "database", ["dtxwiki", "dtxwiki_migration", "dtxwiki_concurrency", "test"]
)
def test_reset_refuses_a_database_not_named_as_a_test_database(database):
    # Port 9 (discard) has nothing listening: a helper that connected before
    # checking the name would raise OperationalError here, not ValueError.
    engine = create_engine(f"postgresql+pg8000://user:pw@127.0.0.1:9/{database}")

    with pytest.raises(ValueError, match="named \\*_test or \\*-test"):
        reset_postgres_test_database(engine)


def test_reset_refuses_a_non_postgresql_engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'keep_test.sqlite'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE kept (id INTEGER PRIMARY KEY)"))

    with pytest.raises(ValueError, match="refusing to reset a sqlite database"):
        reset_postgres_test_database(engine)

    assert inspect(engine).get_table_names() == ["kept"]
    engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="DTXWIKI_POSTGRES_TEST_DATABASE_URL is not set to a PostgreSQL URL",
)
def test_reset_empties_a_schema_that_metadata_drop_all_cannot_sort():
    engine = create_engine(_postgres_test_url())
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        reset_postgres_test_database(engine)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(64) NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO alembic_version VALUES ('0002_epic1')")
            )
        # Populate the cycle in both directions: a user whose default
        # workspace is owned by that same user.
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            user = User(
                email="schema-reset@example.com",
                password_hash="not-a-real-hash",
                display_name="Schema Reset",
                default_workspace_id=workspace.id,
            )
            session.add(user)
            session.flush()
            workspace.owner_user_id = user.id
        with session_factory() as session:
            owned = session.scalar(
                select(Workspace).where(Workspace.owner_user_id.is_not(None))
            )
            assert owned is not None
            assert session.get(User, owned.owner_user_id).default_workspace_id == (
                owned.id
            )

        reset_postgres_test_database(engine)

        assert inspect(engine).get_table_names(schema="public") == []
        with engine.connect() as connection:
            relations = connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public'"
                )
            ).scalars()
            types = connection.execute(
                text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public'"
                )
            ).scalars()
            # Tables, the alembic_version stamp, sequences, indexes and the
            # row and enum types all live in the schema that was dropped.
            assert list(relations) == []
            assert list(types) == []

        # What every caller does next: build the schema and seed it again.
        Base.metadata.create_all(engine)
        with session_factory.begin() as session:
            seed_default_workspace(session)
    finally:
        reset_postgres_test_database(engine)
        engine.dispose()
