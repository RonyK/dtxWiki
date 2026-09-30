"""Reset a dedicated PostgreSQL test database to an empty ``public`` schema.

``Base.metadata.drop_all`` cannot do this. ``users`` and ``workspace`` point at
each other through foreign keys the ORM declares without names, so SQLAlchemy
cannot sort the two tables for DROP and raises ``CircularDependencyError``. The
Alembic migrations name every constraint, so a migrated database is unaffected;
only tests that build their schema with ``Base.metadata.create_all`` hit it.

Dropping the schema removes every table, sequence and type in it -- the
``alembic_version`` stamp included -- in one statement, whatever the ORM
metadata says, so the reset does not depend on the metadata being droppable.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# The same convention the PostgreSQL suites already skip on: a database whose
# name does not say it is disposable is never reset.
TEST_DATABASE_SUFFIXES = ("_test", "-test")


def reset_postgres_test_database(engine: Engine) -> None:
    """Drop and recreate ``public`` in a database named as a test database.

    Refuses, before connecting, an engine that is not PostgreSQL or a database
    whose name does not end in ``_test``/``-test``: some callers fall back to
    ``DTXWIKI_DATABASE_URL``, and this statement must never reach a database
    that holds real data.
    """

    if engine.dialect.name != "postgresql":
        raise ValueError(
            f"refusing to reset a {engine.dialect.name} database: this helper "
            "drops the PostgreSQL public schema"
        )
    database = engine.url.database or ""
    if not database.endswith(TEST_DATABASE_SUFFIXES):
        raise ValueError(
            f"refusing to drop the public schema of {database!r}: a database "
            "reset by tests must be named *_test or *-test"
        )
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
