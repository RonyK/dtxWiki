"""A PostgreSQL ``create_all`` must not change what a later SQLite one builds.

Needs no database: a mock PostgreSQL engine runs the same DDL generation,
including the ALTER TABLE step that marks the shared constraint objects.
"""

from sqlalchemy import create_engine, create_mock_engine
from sqlalchemy.schema import AddConstraint

from backend.models.base import Base
from backend.tests.utils.postgres import keep_sqlite_foreign_keys_inline


def test_a_postgresql_create_all_leaves_sqlite_its_cyclic_foreign_keys():
    statements = []
    postgres = create_mock_engine(
        "postgresql+pg8000://",
        lambda statement, *_multiparams, **_params: statements.append(statement),
    )
    Base.metadata.create_all(postgres, checkfirst=False)
    closed_by_alter = [
        statement.element
        for statement in statements
        if isinstance(statement, AddConstraint)
    ]
    # Without the cycle step there is nothing to leak, and nothing to prove.
    assert closed_by_alter

    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            for constraint in closed_by_alter:
                rows = connection.exec_driver_sql(
                    f'PRAGMA foreign_key_list("{constraint.table.name}")'
                ).fetchall()
                built: dict[int, set[str]] = {}
                for key_id, _seq, _parent, column, *_rest in rows:
                    built.setdefault(key_id, set()).add(column)
                expected = {column.name for column in constraint.columns}
                assert expected in built.values(), constraint.name
    finally:
        # This test marks the shared constraints itself; if the listener ever
        # stops undoing that, fail here alone rather than in every later
        # SQLite schema this worker builds.
        with engine.connect() as connection:
            keep_sqlite_foreign_keys_inline(Base.metadata, connection)
        engine.dispose()
