"""One populated call-log migration traversal per supported dialect."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.models.workspace import MCPClient
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

PARENT = "9d8695419436"
REVISION = "872d9c3e104b"
LIFECYCLE_COLUMNS = {
    "actor_kind",
    "actor_id",
    "idempotency_key",
    "retryable",
}
LIFECYCLE_INDEX = "ix_mcp_tool_call_actor_key_started"


def _parent_calls(connection):
    # SQLite reflects UUID storage as CHAR; restore UUID processors while
    # reflecting only the columns actually present at the parent revision.
    return sa.Table(
        "mcp_tool_call",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("client_id", sa.Uuid()),
        sa.Column("request_id", sa.Uuid()),
        autoload_with=connection,
    )


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_mcp_call_lifecycle_upgrade_downgrade_reupgrade(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'call-lifecycle-migration.sqlite'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not url or not url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(url)
    assert not (set(sa.inspect(engine).get_table_names()) - {"alembic_version"})
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["context_repositories_root"] = tmp_path / "repositories"
    now = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    receipt_id, historical_call_id, historical_request_id = uuid4(), uuid4(), uuid4()
    try:
        command.upgrade(config, PARENT)
        original_tables = set(sa.inspect(engine).get_table_names())
        original_indexes = {
            index["name"] for index in sa.inspect(engine).get_indexes("mcp_tool_call")
        }
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
            client = MCPClient(
                workspace_id=workspace_id,
                name="migration-client",
                client_type="system_managed",
                scopes=["*"],
                status="active",
            )
            session.add(client)
            session.flush()
            client_id = client.id
            session.add(
                MCPWriteIdempotency(
                    id=receipt_id,
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id="migration-actor",
                    tool_name="patch_context_edit",
                    idempotency_key="preserved",
                    request_fingerprint="fixture",
                    result_payload={"saved": True},
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                )
            )
            # Reflect the parent table: the current ORM includes columns that
            # do not exist yet, and the historical row must predate the upgrade.
            parent_calls = _parent_calls(session.connection())
            session.execute(
                parent_calls.insert().values(
                    id=historical_call_id,
                    workspace_id=workspace_id,
                    client_id=client_id,
                    request_id=historical_request_id,
                    schema_version="llmwiki.mcp.v1",
                    tool_name="patch_context_edit",
                    input_summary={"page_id": "historical-page"},
                    result_status="succeeded",
                    result_summary="Historical call preserved.",
                    result_detail={"page_id": "historical-page"},
                    warnings=[],
                    provider="unknown",
                    model_id="unknown",
                    params_summary={},
                    client_self_reported=False,
                    started_at=now,
                    completed_at=now + timedelta(seconds=1),
                    latency_ms=1000,
                )
            )
            session.commit()

        command.upgrade(config, REVISION)
        engine.dispose()
        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names()) == original_tables
        columns = {
            column["name"]: column for column in inspector.get_columns("mcp_tool_call")
        }
        assert LIFECYCLE_COLUMNS <= columns.keys()
        assert all(columns[name]["nullable"] for name in LIFECYCLE_COLUMNS)
        indexes = {
            index["name"]: index for index in inspector.get_indexes("mcp_tool_call")
        }
        assert set(indexes) == original_indexes | {LIFECYCLE_INDEX}
        assert not indexes[LIFECYCLE_INDEX]["unique"]
        assert indexes[LIFECYCLE_INDEX]["column_names"] == [
            "workspace_id",
            "actor_kind",
            "actor_id",
            "idempotency_key",
            "started_at",
            "id",
        ]
        # Partial: unkeyed calls, the bulk of the log, carry no index entry.
        where = indexes[LIFECYCLE_INDEX]["dialect_options"][f"{dialect}_where"]
        assert "idempotency_key IS NOT NULL" in str(where)
        with Session(engine) as session:
            historical = session.get(MCPToolCall, historical_call_id)
            assert historical.request_id == historical_request_id
            assert historical.input_summary == {"page_id": "historical-page"}
            assert historical.result_summary == "Historical call preserved."
            assert historical.result_detail == {"page_id": "historical-page"}
            assert historical.completed_at == now + timedelta(seconds=1)
            assert all(getattr(historical, name) is None for name in LIFECYCLE_COLUMNS)
            call = MCPToolCall(
                workspace_id=workspace_id,
                client_id=client_id,
                request_id=uuid4(),
                actor_kind="human",
                actor_id="migration-actor",
                tool_name="patch_context_edit",
                idempotency_key="retry-key",
                result_status="in_progress",
                started_at=now,
            )
            retry = MCPToolCall(
                workspace_id=workspace_id,
                client_id=client_id,
                request_id=uuid4(),
                actor_kind="human",
                actor_id="migration-actor",
                tool_name="patch_context_edit",
                idempotency_key="retry-key",
                result_status="in_progress",
                started_at=now + timedelta(seconds=1),
            )
            session.add_all([call, retry])
            session.commit()
            assert call.id != retry.id
            assert call.completed_at is call.error_code is call.retryable is None
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    MCPToolCall(
                        workspace_id=workspace_id,
                        client_id=client_id,
                        request_id=call.request_id,
                        tool_name="patch_context_edit",
                        result_status="in_progress",
                        started_at=now,
                    )
                )
                session.flush()
            call.completed_at = now + timedelta(seconds=2)
            call.result_status = "failed"
            call.error_code = "validation_failed"
            call.retryable = False
            session.commit()
            assert session.get(MCPToolCall, call.id).retryable is False
            lifecycle_call_id = call.id

        command.downgrade(config, PARENT)
        engine.dispose()
        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names()) == original_tables
        assert not LIFECYCLE_COLUMNS.intersection(
            column["name"] for column in inspector.get_columns("mcp_tool_call")
        )
        assert {
            index["name"] for index in inspector.get_indexes("mcp_tool_call")
        } == original_indexes
        with Session(engine) as session:
            calls = _parent_calls(session.connection())
            assert session.scalar(sa.select(sa.func.count()).select_from(calls)) == 3
            historical = (
                session.execute(
                    sa.select(calls).where(calls.c.id == historical_call_id)
                )
                .mappings()
                .one()
            )
            assert historical["request_id"] == historical_request_id
            assert historical["result_summary"] == "Historical call preserved."
            assert session.get(MCPWriteIdempotency, receipt_id).result_payload == {
                "saved": True
            }

        command.upgrade(config, REVISION)
        with Session(engine) as session:
            assert (
                session.scalar(sa.select(sa.func.count()).select_from(MCPToolCall)) == 3
            )
            historical = session.get(MCPToolCall, historical_call_id)
            assert historical.result_detail == {"page_id": "historical-page"}
            assert all(getattr(historical, name) is None for name in LIFECYCLE_COLUMNS)
            call = session.get(MCPToolCall, lifecycle_call_id)
            assert (
                call.result_status == "failed"
                and call.error_code == "validation_failed"
            )
            assert call.actor_id is call.idempotency_key is call.retryable is None
            assert session.get(MCPWriteIdempotency, receipt_id).result_payload == {
                "saved": True
            }
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
