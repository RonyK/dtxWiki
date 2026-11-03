import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from backend.models.change import ChangeOperation, ChangeSet
from backend.services.ops.alembic_migration_graph import schema_release_head
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT_REVISION = "19db79d3e622"
INDEX_REVISION = "ef53a0a7061c"
INDEX_NAME = "ix_change_operation_change_set_id"
DELETION_REVISION = "eb86fdacb1bb"
# The deletion revision's parent; the branch's one revision follows dev's head.
DELETION_PARENT = "333a67e22cd7"
DELETION_COLUMNS = {
    "deletion_requested_at",
    "purge_after",
    "deletion_retention_days",
    "deletion_prior_status",
    "deletion_failure_count",
    "deletion_last_error",
}
HOLD_COLUMN = "account_deletion_user_id"
HOLD_INDEX = "ix_workspace_account_deletion_user_id"
TRACE_INDEX_PARENT = "1d8f1c08ca36"
TRACE_INDEX_REVISION = "b1d98889a4ad"
TRACE_INDEX_NAME = "ix_mcp_tool_call_workspace_session_started"
TRACE_INDEX_COLUMNS = ["workspace_id", "mcp_session_id", "started_at", "id"]


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
            # Seed the real parent schema without later ORM-only Graph columns.
            workspace_id = uuid4()
            workspaces = Table("workspace", MetaData(), autoload_with=engine)
            sqlite = engine.dialect.name == "sqlite"
            session.execute(
                workspaces.insert().values(
                    id=workspace_id.hex if sqlite else workspace_id,
                    slug="index-migration",
                    title="Index migration",
                    name="Index migration",
                    directory_title_key="index migration",
                    goal="Index migration regression",
                    default_language="en",
                    library_policy={},
                    write_policy={},
                    citation_policy={},
                    agent_injection_policy={},
                    review_policy={},
                    lint_policy={},
                    auto_fix_enabled=False,
                    visibility="private",
                    created_by_actor_id=(uuid4().hex if sqlite else uuid4()),
                    owner_actor_id=(uuid4().hex if sqlite else uuid4()),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            change_set = ChangeSet(
                workspace_id=workspace_id, trigger_type="manual", actor_type="human"
            )
            session.add(change_set)
            session.flush()
            change_set_id = change_set.id
            # Two operations for one parent exercise the real nonunique-index
            # requirement: a mistakenly unique index must fail on this data.
            session.add_all(
                ChangeOperation(
                    workspace_id=workspace_id,
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
        command.upgrade(config, DELETION_PARENT)
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
        workspace_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("workspace")
        }
        assert workspace_columns[HOLD_COLUMN]["nullable"]
        assert any(
            index["name"] == HOLD_INDEX and index["column_names"] == [HOLD_COLUMN]
            for index in inspect(engine).get_indexes("workspace")
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
            command.downgrade(config, DELETION_PARENT)
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
        command.downgrade(config, DELETION_PARENT)
        assert DELETION_COLUMNS.isdisjoint(
            column["name"] for column in inspect(engine).get_columns("users")
        )
        assert HOLD_COLUMN not in {
            column["name"] for column in inspect(engine).get_columns("workspace")
        }
        with engine.connect() as connection:
            assert connection.execute(identity_query).one() == original_identity
        command.upgrade(config, DELETION_REVISION)
        with engine.connect() as connection:
            assert connection.execute(identity_query).one() == original_identity
            assert connection.execute(rows_query).all() == original_rows
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == (DELETION_REVISION)

        # Reuse this dialect's traversal for the Chats lookup index. Calls
        # from two conversations exercise the page-wide IN predicate; calls
        # in one conversation also share a timestamp to retain the ID tie-break.
        command.upgrade(config, TRACE_INDEX_PARENT)
        assert TRACE_INDEX_NAME not in {
            index["name"] for index in inspect(engine).get_indexes("mcp_tool_call")
        }
        clients = Table("mcp_client", MetaData(), autoload_with=engine)
        calls = Table("mcp_tool_call", MetaData(), autoload_with=engine)
        client_id = uuid4()
        conversation_ids = [uuid4(), uuid4()]
        database_workspace_id = workspace_id.hex if sqlite else workspace_id
        with engine.begin() as connection:
            connection.execute(
                clients.insert().values(
                    id=client_id.hex if sqlite else client_id,
                    workspace_id=database_workspace_id,
                    name="Chats index migration",
                    client_type="generic",
                    scopes=["read"],
                    status="active",
                    token_metadata={},
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                calls.insert(),
                [
                    {
                        "id": call_id.hex if sqlite else call_id,
                        "workspace_id": database_workspace_id,
                        "client_id": client_id.hex if sqlite else client_id,
                        "mcp_session_id": (
                            conversation_id.hex if sqlite else conversation_id
                        ),
                        "request_id": request_id.hex if sqlite else request_id,
                        "schema_version": "llmwiki.mcp.v1",
                        "tool_name": "read",
                        "input_summary": {"target_type": "context_page"},
                        "result_status": "succeeded",
                        "warnings": [],
                        "provider": "unknown",
                        "model_id": "unknown",
                        "params_summary": {},
                        "client_self_reported": False,
                        "started_at": now,
                    }
                    for conversation_id, call_id, request_id in (
                        (conversation_ids[0], uuid4(), uuid4()),
                        (conversation_ids[0], uuid4(), uuid4()),
                        (conversation_ids[1], uuid4(), uuid4()),
                    )
                ],
            )
        trace_rows_query = select(calls).order_by(calls.c.id)
        with engine.connect() as connection:
            original_trace_rows = connection.execute(trace_rows_query).all()
        assert len(original_trace_rows) == 3

        for revision, index_expected in (
            (TRACE_INDEX_REVISION, True),
            (TRACE_INDEX_PARENT, False),
            (TRACE_INDEX_REVISION, True),
        ):
            if index_expected:
                command.upgrade(config, revision)
            else:
                command.downgrade(config, revision)
            indexes = {
                index["name"]: index
                for index in inspect(engine).get_indexes("mcp_tool_call")
            }
            assert (TRACE_INDEX_NAME in indexes) is index_expected
            if index_expected:
                assert indexes[TRACE_INDEX_NAME]["column_names"] == TRACE_INDEX_COLUMNS
                assert not indexes[TRACE_INDEX_NAME]["unique"]
            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == revision
                assert connection.execute(trace_rows_query).all() == original_trace_rows
                if sqlite and index_expected:
                    plan = connection.execute(
                        text(
                            "EXPLAIN QUERY PLAN SELECT id, mcp_session_id, started_at "
                            "FROM mcp_tool_call WHERE workspace_id = :workspace_id "
                            "AND mcp_session_id IN (:first_session, :second_session) "
                            "ORDER BY started_at, id"
                        ),
                        {
                            "workspace_id": database_workspace_id,
                            "first_session": conversation_ids[0].hex,
                            "second_session": conversation_ids[1].hex,
                        },
                    ).all()
                    details = " ".join(str(row[-1]) for row in plan)
                    assert TRACE_INDEX_NAME in details, plan
                    assert "workspace_id=? AND mcp_session_id=?" in details, plan

        # This branch and the independently appended document-reference chain
        # must converge without reparenting either revision or losing traces.
        # Reuse the same database rather than adding a second parent traversal.
        command.upgrade(config, "head")
        assert {
            "co_access_pair",
            "context_document_reference",
            "context_published_citation",
        } <= set(inspect(engine).get_table_names())
        assert TRACE_INDEX_NAME in {
            index["name"] for index in inspect(engine).get_indexes("mcp_tool_call")
        }
        with engine.connect() as connection:
            assert connection.execute(trace_rows_query).all() == original_trace_rows
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == schema_release_head("0.2.0")
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
        finally:
            engine.dispose()
