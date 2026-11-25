from __future__ import annotations

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from backend.services.ops import read_only_database

POSTGRES_URL = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")

# These probes share a database with suites that reset its schema, so keep them
# on the same worker.  Both marks must live in one list: assigning pytestmark a
# second time silently discards the first mark.
pytestmark = [
    pytest.mark.xdist_group("postgres"),
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL not set",
    ),
]


def test_postgres_probe_verifies_read_only_transaction():
    snapshot = read_only_database.probe_read_only(POSTGRES_URL)
    assert snapshot.dialect == "postgresql"
    assert snapshot.read_only_verified is True
    assert snapshot.read_only_mechanism == (
        "postgresql_repeatable_read_transaction_read_only"
    )


def test_postgres_probe_rejects_injected_ddl(monkeypatch):
    table = f"dtxwiki_inventory_forbidden_{uuid4().hex}"

    def attempted_write(connection, **_kwargs):
        connection.exec_driver_sql(f"CREATE TABLE {table} (id integer)")
        return object()

    monkeypatch.setattr(read_only_database, "_inspect", attempted_write)
    cleanup = sa.create_engine(POSTGRES_URL, poolclass=NullPool, future=True)
    try:
        with pytest.raises(read_only_database.ProbeFailure) as raised:
            read_only_database.probe_read_only(POSTGRES_URL)
        assert raised.value.code == "database_probe_failed"
        with cleanup.connect() as connection:
            assert not sa.inspect(connection).has_table(table)
    finally:
        with cleanup.begin() as connection:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        cleanup.dispose()


def test_postgres_probe_pins_one_repeatable_read_snapshot(monkeypatch):
    table = f"dtxwiki_inventory_snapshot_{uuid4().hex}"
    writer = sa.create_engine(POSTGRES_URL, poolclass=NullPool, future=True)
    original = read_only_database._inspect
    observed: list[object] = []

    def inspect_across_commit(connection, **kwargs):
        observed.append(
            connection.execute(sa.text("SELECT to_regclass(:name)"), {"name": table})
            .scalar_one()
        )
        with writer.begin() as write_connection:
            write_connection.exec_driver_sql(f"CREATE TABLE {table} (id integer)")
        observed.append(
            connection.execute(sa.text("SELECT to_regclass(:name)"), {"name": table})
            .scalar_one()
        )
        return original(connection, **kwargs)

    monkeypatch.setattr(read_only_database, "_inspect", inspect_across_commit)
    try:
        snapshot = read_only_database.probe_read_only(POSTGRES_URL)
        assert snapshot.read_only_verified is True
        assert observed == [None, None]
    finally:
        with writer.begin() as connection:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        writer.dispose()
