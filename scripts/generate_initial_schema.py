"""Freeze the final release DDL from an empty, actually migrated database.

Run separately for SQLite and PostgreSQL against disposable empty databases.
The generated initializer never imports live ORM metadata or replays history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import AddConstraint, CreateTable

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

RELEASE_REVISION = "5b7e5c149279"
RELEASE_014_REVISION = "eb86fdacb1bb"
FTS_NAMES = ("search_doc_fts", "page_name_trgm")


def _fts_shadow(name: str) -> bool:
    return any(name.startswith(f"{prefix}_") for prefix in FTS_NAMES)


def sqlite_ddl(connection: sa.Connection) -> list[str]:
    """Retain virtual-table declarations, never their generated shadow DDL."""
    rows = connection.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'view' THEN 2 ELSE 3 END, name"
    )
    return [sql for _kind, name, sql in rows if not _fts_shadow(name)]


def postgresql_ddl(connection: sa.Connection, table_names: list[str]) -> list[str]:
    """Keep PostgreSQL cycles, expression indexes and guard functions intact."""
    metadata = sa.MetaData()
    metadata.reflect(connection, only=table_names)
    schema = connection.scalar(sa.text("SELECT current_schema()"))
    qualifier = connection.dialect.identifier_preparer.quote(schema) + "."

    def unqualify(statement: str, prefix: str) -> str:
        # Catalog renderers qualify the owning table/function, although the
        # original migrations use the connection's current schema. Remove
        # only that structural token, never SQL literals or function bodies.
        return statement.replace(prefix + qualifier, prefix, 1)

    # Named placeholders keep literal percent characters unchanged. These are
    # standalone DDL strings, subsequently executed without bound parameters.
    dialect = postgresql.dialect(paramstyle="named")
    statements = [
        str(
            CreateTable(
                metadata.tables[name], include_foreign_key_constraints=[]
            ).compile(dialect=dialect)
        ).strip()
        for name in table_names
    ]
    # All tables (and their durable UNIQUE constraints) must precede reciprocal
    # FKs. In particular Context/Agent/Library have workspace-scoped cycles.
    statements.extend(
        str(AddConstraint(constraint).compile(dialect=dialect)).strip()
        for name in table_names
        for constraint in sorted(
            metadata.tables[name].foreign_key_constraints,
            key=lambda item: item.name or "",
        )
    )
    statements.extend(
        unqualify(row[0], " ON ")
        for row in connection.execute(
            sa.text(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index i "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = current_schema() AND t.relname != 'alembic_version' "
                "AND NOT EXISTS (SELECT 1 FROM pg_constraint c "
                "WHERE c.conindid = i.indexrelid) ORDER BY i.indexrelid::regclass::text"
            )
        )
    )
    statements.extend(
        unqualify(
            row[0].replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION", 1),
            "CREATE FUNCTION ",
        )
        for row in connection.execute(
            sa.text(
                "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() AND p.prokind = 'f' "
                "ORDER BY p.proname, p.oid::regprocedure::text"
            )
        )
    )
    statements.extend(
        unqualify(row[0], " ON ")
        for row in connection.execute(
            sa.text(
                "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND NOT t.tgisinternal "
                "ORDER BY c.relname, t.tgname"
            )
        )
    )
    return statements


def generate(database_url: str, destination: Path) -> None:
    engine = sa.create_engine(database_url)
    try:
        if engine.dialect.name not in {"sqlite", "postgresql"}:
            raise ValueError("initial schema supports SQLite and PostgreSQL")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            if inspector.get_table_names() or inspector.get_view_names():
                raise ValueError(
                    "schema generation requires a disposable empty database"
                )
        config = Config(str(resolve_alembic_ini()))
        migrations = resolve_migrations_dir()
        config.set_main_option("script_location", str(migrations))
        config.set_main_option("sqlalchemy.url", database_url)
        # The explicit published 0.1.4 target bypasses the fresh-only route, so
        # the reference schema is the result of the real release migrations.
        command.upgrade(config, RELEASE_014_REVISION)
        command.upgrade(config, RELEASE_REVISION)
        with engine.connect() as connection:
            tables = sorted(
                name
                for name in sa.inspect(connection).get_table_names()
                if name != "alembic_version" and not _fts_shadow(name)
            )
            for name in tables:
                table = sa.table(name)
                if connection.scalar(sa.select(sa.func.count()).select_from(table)):
                    raise ValueError(f"empty migration unexpectedly seeded {name}")
            ddl = (
                sqlite_ddl(connection)
                if engine.dialect.name == "sqlite"
                else postgresql_ddl(connection, tables)
            )
        script = ScriptDirectory.from_config(config)
        chain = list(script.walk_revisions(base="base", head=RELEASE_REVISION))
        source_hashes = {
            Path(revision.path).relative_to(migrations).as_posix(): hashlib.sha256(
                Path(revision.path).read_bytes()
            ).hexdigest()
            for revision in reversed(chain)
        }
        payload = {
            "format_version": 1,
            "revision": RELEASE_REVISION,
            "dialect": engine.dialect.name,
            "source": "Published Alembic release chain on an empty database",
            "source_revision_sha256": source_hashes,
            "tables": tables,
            "statements": ddl,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"{engine.dialect.name}: {len(tables)} tables, {len(ddl)} statements")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    generate(os.environ[arguments.database_url_env], arguments.output)


if __name__ == "__main__":
    main()
