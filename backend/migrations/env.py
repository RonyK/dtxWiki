from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from backend.models import Base
from backend.models.search_index import include_non_fts5_object
from backend.services.ops.db_revision_inventory import (
    classify_revision_consistency,
)
from backend.services.ops.migration_preflight import (
    evaluate_connection_preflight,
)
from backend.services.ops.read_only_database import inspect_revision_snapshot

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _script_revisions() -> frozenset[str]:
    """The revisions of the script directory this run migrates with."""

    return frozenset(revision.revision for revision in context.script.walk_revisions())


def _revision_destinations() -> tuple[str, ...]:
    try:
        destination = context.get_revision_argument()
    except (AttributeError, KeyError):
        return ()
    if destination is None:
        return ()
    raw_destinations = (
        (destination,) if isinstance(destination, str) else tuple(destination)
    )
    resolved: list[str] = []
    for revision in raw_destinations:
        if revision in {None, "base"}:
            continue
        if revision == "head":
            head = context.get_head_revision()
            if head is not None:
                resolved.append(head)
            continue
        if revision == "heads":
            resolved.extend(context.get_head_revisions())
            continue
        resolved.append(revision)
    return tuple(resolved)


def _reject_blind_nonbase_stamp(connection) -> None:
    """Allow non-base stamps only as no-op confirmation of verified state."""

    migration_fn = context.get_context().opts.get("fn")
    if getattr(migration_fn, "__name__", "") != "do_stamp":
        return
    destinations = _revision_destinations()
    if not destinations:
        # Unstamping to base is an explicit recovery operation.
        return
    snapshot = inspect_revision_snapshot(connection)
    consistency = classify_revision_consistency(
        snapshot, known_revisions=_script_revisions()
    )
    if (
        len(destinations) != 1
        or snapshot.alembic_revisions != destinations
        or consistency != "consistent"
    ):
        raise RuntimeError(
            "Alembic refuses a blind non-base stamp: DB stamp/schema evidence "
            "is stamp_marker_conflict; a stamp may only confirm an already "
            "consistent identical revision. Run the actual migration or "
            "perform an operator-led compatibility conversion."
        )


def _reject_unidentifiable_upgrade_start(connection) -> None:
    """Refuse an upgrade whose starting stamp/schema pair cannot be identified.

    Alembic itself only resolves the stamp against the migration graph, so a
    corrupt or retired identifier surfaces as ``CommandError`` after the run
    has begun, and an absent stamp over a complete schema replays 0001 onto
    existing tables. Both are refused here, before any DDL is attempted.
    """

    migration_fn = context.get_context().opts.get("fn")
    if getattr(migration_fn, "__name__", "") != "upgrade":
        return
    preflight = evaluate_connection_preflight(
        connection, known_revisions=_script_revisions()
    )
    if preflight.blocked:
        raise RuntimeError(preflight.refusal("Alembic refuses to upgrade"))


def _validate_resulting_revision_shape(connection) -> None:
    """Reject mutating Alembic commands that leave an unsafe stamp/schema pair."""

    if not _revision_destinations():
        # ``downgrade base`` and an explicit unstamp are recovery operations.
        return

    consistency = classify_revision_consistency(
        inspect_revision_snapshot(connection), known_revisions=_script_revisions()
    )
    if consistency != "consistent":
        raise RuntimeError(
            "Alembic refuses to finish: DB stamp/schema evidence is "
            f"{consistency}; the requested revision would not describe the "
            "resulting database. Manual operator intervention required."
        )


def _make_sqlite_ddl_transactional(engine) -> None:
    """Run every SQLite migration in one real transaction.

    Alembic treats SQLite DDL as non-transactional, and pysqlite only opens a
    transaction before DML: a revision's leading ``CREATE``/``ALTER`` ran in
    autocommit mode and survived a later failure, half-applying the revision
    under the old stamp. SQLite itself rolls DDL back fine, so transaction
    control moves from the driver to SQLAlchemy -- the driver never begins on
    its own, and SQLAlchemy's begin takes the write lock up front. With
    ``transactional_ddl`` the whole run (every revision, the stamp update and
    the resulting-shape check) commits or rolls back as one unit, as it already
    does on PostgreSQL.
    """

    @event.listens_for(engine, "connect")
    def _driver_does_not_begin(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_with_the_write_lock(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_non_fts5_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    sqlite = connectable.dialect.name == "sqlite"
    if sqlite:
        _make_sqlite_ddl_transactional(connectable)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_non_fts5_object,
            **({"transactional_ddl": True} if sqlite else {}),
        )

        _reject_blind_nonbase_stamp(connection)
        _reject_unidentifiable_upgrade_start(connection)

        with context.begin_transaction():
            context.run_migrations()
            _validate_resulting_revision_shape(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
