from sqlalchemy import MetaData
from sqlalchemy.engine import Connection, Engine


def drop_reflected_postgres_metadata(engine: Engine) -> None:
    """Drop a dedicated test database's tables using real FK names.

    The ORM contains intentional bidirectional foreign-key cycles. PostgreSQL
    assigns names to constraints declared without one, but those generated
    names are absent from ``Base.metadata`` and make a direct ``drop_all``
    unsortable. Reflection recovers the database names so SQLAlchemy can drop
    the cyclic constraints before dropping their tables.
    """

    metadata = MetaData()
    metadata.reflect(bind=engine)
    metadata.drop_all(bind=engine)


def keep_sqlite_foreign_keys_inline(
    target: MetaData, connection: Connection, **_kwargs
) -> None:
    """Undo, for a SQLite ``create_all``, what a PostgreSQL one leaves behind.

    On PostgreSQL ``create_all`` adds each foreign key that closes one of those
    cycles by ALTER TABLE, and SQLAlchemy's ``AddConstraint`` marks the
    constraint object -- shared process-wide through ``Base.metadata`` -- never
    to render inside CREATE TABLE again. SQLite cannot add a constraint by
    ALTER, so every later SQLite ``create_all`` in that process silently built
    the cycle's tables without those foreign keys (47 of them, the
    workspace-scoped ownership keys among them): the serial lane, which runs
    the PostgreSQL suites and the SQLite ones in one process, lost their
    enforcement. Registered as a ``before_create`` listener, which runs after
    the tables are sorted and before any CREATE TABLE is compiled. No
    ForeignKeyConstraint carries any other create rule, and PostgreSQL routes
    the cycle through ALTER either way, so it only acts on SQLite.
    """

    if connection.dialect.name != "sqlite":
        return
    for table in target.tables.values():
        for constraint in table.foreign_key_constraints:
            constraint._create_rule = None
