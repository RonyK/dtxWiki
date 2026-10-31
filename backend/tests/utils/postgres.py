from contextlib import nullcontext

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.mock import MockConnection
from sqlalchemy.schema import AddConstraint
from sqlalchemy.sql.ddl import sort_tables_and_constraints

from backend.models.base import Base


def create_isolated_postgres_metadata(engine: Engine | MockConnection) -> None:
    """Create a PostgreSQL test schema without changing mapped metadata.

    PostgreSQL emits the ORM's cyclic foreign keys through ``AddConstraint``.
    SQLAlchemy marks each such constraint as excluded from later inline table
    creation, mutating its metadata object. Reusing ``Base.metadata`` on SQLite
    in the same process would silently omit those foreign keys, because SQLite
    cannot add them after creating its tables. Fresh copies retain the complete
    schema and leave the mapped tables and SQLite's FTS5 DDL hooks untouched.

    The cyclic ALTER constraints also need a deterministic order. A pointer's
    SET NULL action must precede its ownership NO ACTION check. PostgreSQL
    orders the internal triggers by name, however, and their unpadded OID
    suffixes can reverse creation order at a decimal boundary. Give the test
    schema's SET NULL action triggers an explicit priority without changing
    any constraint or mapped metadata.
    """

    if engine.dialect.name != "postgresql":
        raise ValueError("isolated PostgreSQL test metadata requires PostgreSQL")
    metadata = MetaData(
        schema=Base.metadata.schema,
        naming_convention=Base.metadata.naming_convention,
    )
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    ordered = sort_tables_and_constraints(
        sorted(metadata.tables.values(), key=lambda table: table.fullname)
    )
    deferred = [
        constraint
        for table, constraints in ordered
        if table is None
        for constraint in constraints
    ]
    for constraint in deferred:
        # Change only the disposable copy. Table.create retains its ordinary
        # indexes/defaults/table hooks while leaving cyclic FKs for the ordered
        # ALTER phase below. The repository's metadata-wide hooks are SQLite
        # FTS5 hooks and deliberately do not run on this PostgreSQL-only path.
        constraint.use_alter = True

    is_mock = isinstance(engine, MockConnection)
    context = nullcontext(engine) if is_mock else engine.begin()
    with context as connection:
        inspector = None if is_mock else inspect(connection)
        created = set()
        for table, _constraints in ordered:
            if table is None:
                continue
            if inspector is not None and inspector.has_table(
                table.name, schema=table.schema
            ):
                continue
            table.create(connection, checkfirst=False)
            created.add(table)
        for constraint in sorted(deferred, key=_postgres_constraint_order):
            if constraint.table in created:
                connection.execute(AddConstraint(constraint))
        if not isinstance(connection, MockConnection):
            _prioritize_postgres_set_null_actions(connection, metadata)


def _prioritize_postgres_set_null_actions(
    connection: Connection, metadata: MetaData
) -> None:
    actions = connection.execute(
        text(
            "SELECT trigger.oid, trigger.tgname, target.relname AS target_table, "
            "target_schema.nspname AS target_schema "
            "FROM pg_trigger AS trigger "
            "JOIN pg_constraint AS fk ON fk.oid = trigger.tgconstraint "
            "JOIN pg_proc AS action ON action.oid = trigger.tgfoid "
            "JOIN pg_class AS source ON source.oid = fk.conrelid "
            "JOIN pg_namespace AS source_schema "
            "ON source_schema.oid = source.relnamespace "
            "JOIN pg_class AS target ON target.oid = trigger.tgrelid "
            "JOIN pg_namespace AS target_schema "
            "ON target_schema.oid = target.relnamespace "
            "WHERE trigger.tgisinternal AND fk.contype = 'f' "
            "AND action.proname IN ('RI_FKey_setnull_del', 'RI_FKey_setnull_upd') "
            "AND source.relname = ANY(CAST(:source_tables AS text[])) "
            "AND source_schema.nspname = "
            "COALESCE(CAST(:source_schema AS text), current_schema())"
        ),
        {
            "source_tables": [table.name for table in metadata.tables.values()],
            "source_schema": metadata.schema,
        },
    ).mappings()
    quote = connection.dialect.identifier_preparer.quote
    for action in actions:
        name = f"000_dtxwiki_setnull_{action['oid']}"
        if action["tgname"] == name:
            continue
        target = f"{quote(action['target_schema'])}.{quote(action['target_table'])}"
        connection.exec_driver_sql(
            f"ALTER TRIGGER {quote(action['tgname'])} ON {target} "
            f"RENAME TO {quote(name)}"
        )


def _postgres_constraint_order(constraint):
    return (
        constraint.ondelete != "SET NULL",
        constraint.table.fullname,
        tuple(column.name for column in constraint.columns),
        constraint.referred_table.fullname,
        tuple(element.column.name for element in constraint.elements),
        str(constraint.name or ""),
    )


def drop_reflected_postgres_metadata(engine: Engine) -> None:
    """Drop a dedicated test database's tables using real FK names.

    The ORM contains intentional bidirectional foreign-key cycles. PostgreSQL
    assigns names to constraints declared without one, but those generated
    names are absent from ``Base.metadata`` and make a direct ``drop_all``
    unsortable. Reflection recovers the database names so SQLAlchemy can drop
    the cyclic constraints before dropping their tables. Table cleanup removes
    their triggers, but PostgreSQL retains the trigger functions; remove only
    the known migration guards so a subsequent fresh upgrade can recreate them.
    """

    with engine.begin() as connection:
        metadata = MetaData()
        metadata.reflect(bind=connection)
        metadata.drop_all(bind=connection)
        for function in (
            "dtxwiki_guard_media_evidence()",
            "dtxwiki_reject_graph_observation_mutation()",
        ):
            connection.execute(text(f"DROP FUNCTION IF EXISTS {function}"))


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
