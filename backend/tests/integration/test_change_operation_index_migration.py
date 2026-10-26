import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.workspace import Workspace
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT_REVISION = "19db79d3e622"
INDEX_REVISION = "ef53a0a7061c"
INDEX_NAME = "ix_change_operation_change_set_id"
DELETION_REVISION = "eb86fdacb1bb"
DELETION_COLUMNS = {
    "deletion_requested_at",
    "purge_after",
    "deletion_retention_days",
}


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_change_operation_index_round_trip_preserves_existing_rows(
    dialect, tmp_path, tmp_path_factory
):
    if dialect == "sqlite":
        database_url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "change-operation-index.sqlite"
        )
    else:
        database_url = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
        if not database_url or not database_url.startswith("postgresql"):
            pytest.skip("requires a dedicated PostgreSQL migration test database")

    engine = create_engine(database_url)
    config = _config(database_url)
    migrated = False
    try:
        if dialect == "postgresql":
            assert not (set(inspect(engine).get_table_names()) - {"alembic_version"}), (
                "PostgreSQL migration tests require an empty, dedicated database"
            )
        command.upgrade(config, PARENT_REVISION)
        migrated = True
        assert INDEX_NAME not in {
            index["name"] for index in inspect(engine).get_indexes("change_operation")
        }
        with Session(engine) as session, session.begin():
            workspace = Workspace(
                slug="index-migration", title="Index migration", name="Index migration"
            )
            session.add(workspace)
            session.flush()
            change_set = ChangeSet(
                workspace_id=workspace.id, trigger_type="manual", actor_type="human"
            )
            session.add(change_set)
            session.flush()
            change_set_id = change_set.id
            # Two operations for one parent exercise the real nonunique-index
            # requirement: a mistakenly unique index must fail on this data.
            session.add_all(
                ChangeOperation(
                    workspace_id=workspace.id,
                    change_set_id=change_set_id,
                    target_type="context_page",
                    target_id=uuid4(),
                    operation="update",
                    diff_summary=f"Existing operation {number}",
                )
                for number in range(2)
            )

        rows_query = select(ChangeOperation.__table__).order_by(ChangeOperation.id)
        with engine.connect() as connection:
            original_rows = connection.execute(rows_query).all()
        assert len(original_rows) == 2

        for revision, index_expected in (
            (INDEX_REVISION, True),
            (PARENT_REVISION, False),
            (INDEX_REVISION, True),
        ):
            if index_expected:
                command.upgrade(config, revision)
            else:
                command.downgrade(config, revision)
            indexes = {
                index["name"]: index
                for index in inspect(engine).get_indexes("change_operation")
            }
            assert (INDEX_NAME in indexes) is index_expected
            if index_expected:
                assert indexes[INDEX_NAME]["column_names"] == ["change_set_id"]
                assert not indexes[INDEX_NAME]["unique"]
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == revision
                )
                assert connection.execute(rows_query).all() == original_rows
                if dialect == "sqlite" and index_expected:
                    plan = connection.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT * FROM change_operation "
                            "WHERE change_set_id = :change_set_id"
                        ),
                        {"change_set_id": change_set_id.hex},
                    ).all()
                    assert INDEX_NAME in str(plan), plan

        # Extend this released-boundary traversal for the next revision rather
        # than creating another SQLite/PostgreSQL database scenario. Existing
        # identity data must survive the nullable retention-column migration.
        users = Table("users", MetaData(), autoload_with=engine)
        now = datetime.now(UTC)
        user_id = uuid4().hex if dialect == "sqlite" else uuid4()
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(
                    id=user_id,
                    email="retention-migration@example.com",
                    password_hash="migration-test-hash",
                    display_name="Retained user",
                    member_search_key="retained user retention-migration@example.com",
                    status="active",
                    session_generation=0,
                    updated_at=now,
                    created_at=now,
                )
            )
        identity_query = select(users).where(users.c.id == user_id)
        with engine.connect() as connection:
            original_identity = connection.execute(identity_query).one()

        command.upgrade(config, DELETION_REVISION)
        migrated_users = Table("users", MetaData(), autoload_with=engine)
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("users")
        }
        assert DELETION_COLUMNS <= columns.keys()
        assert all(columns[name]["nullable"] for name in DELETION_COLUMNS)
        assert all(columns[name]["default"] is None for name in DELETION_COLUMNS)
        assert any(
            index["name"] == "ix_users_purge_after"
            and index["column_names"] == ["purge_after"]
            for index in inspect(engine).get_indexes("users")
        )
        with engine.begin() as connection:
            assert connection.execute(
                select(
                    migrated_users.c.deletion_requested_at,
                    migrated_users.c.purge_after,
                    migrated_users.c.deletion_retention_days,
                ).where(migrated_users.c.id == user_id)
            ).one() == (None, None, None)
            connection.execute(
                migrated_users.update()
                .where(migrated_users.c.id == user_id)
                .values(
                    status="deletion_pending",
                    deletion_requested_at=now,
                    purge_after=now + timedelta(days=30),
                    deletion_retention_days=30,
                )
            )
        with pytest.raises(RuntimeError, match="deletion requests are pending"):
            command.downgrade(config, INDEX_REVISION)
        with engine.begin() as connection:
            pending = connection.execute(
                select(migrated_users).where(migrated_users.c.id == user_id)
            ).one()
            assert pending.deletion_retention_days == 30
            assert pending.purge_after - pending.deletion_requested_at == timedelta(
                days=30
            )
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == (DELETION_REVISION)
            connection.execute(
                migrated_users.update()
                .where(migrated_users.c.id == user_id)
                .values(status="active")
            )
        command.downgrade(config, INDEX_REVISION)
        assert DELETION_COLUMNS.isdisjoint(
            column["name"] for column in inspect(engine).get_columns("users")
        )
        with engine.connect() as connection:
            assert connection.execute(identity_query).one() == original_identity
        command.upgrade(config, DELETION_REVISION)
        with engine.connect() as connection:
            assert connection.execute(identity_query).one() == original_identity
            assert connection.execute(rows_query).all() == original_rows
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == (DELETION_REVISION)
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
        finally:
            engine.dispose()
