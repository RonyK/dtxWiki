"""One populated operation-attempt migration traversal per supported dialect."""

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
from backend.models.mcp_trace import MCPOperationAttempt, MCPWriteIdempotency
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

PARENT = "74e5d4f912a6"
REVISION = "872d9c3e104b"


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_mcp_operation_attempt_upgrade_downgrade_reupgrade(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'operation-migration.sqlite'}"
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
    receipt_id = uuid4()
    try:
        command.upgrade(config, PARENT)
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
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
            session.commit()
        command.upgrade(config, REVISION)
        # Alembic uses a separate engine. Reopen the connection that inspected
        # the empty database before reflecting SQLite's new index schema.
        engine.dispose()
        inspector = sa.inspect(engine)
        assert {
            index["name"] for index in inspector.get_indexes("mcp_operation_attempt")
            if not index.get("duplicates_constraint")
        } == {
            "ix_mcp_operation_attempt_actor_key",
            "ix_mcp_operation_attempt_expiry",
        }
        assert {
            item["name"]
            for item in inspector.get_unique_constraints("mcp_operation_attempt")
        } == {"uq_mcp_operation_attempt_scope"}
        with Session(engine) as session:
            attempt = MCPOperationAttempt(
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="migration-actor",
                tool_name="patch_context_edit",
                idempotency_key="key",
                started_at=now,
                expires_at=now + timedelta(hours=24),
            )
            session.add(attempt)
            session.commit()
            assert attempt.attempts == 1
            assert attempt.started_at == now
            assert (
                attempt.completed_at
                is attempt.result_payload
                is attempt.error_code
                is None
            )
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    MCPOperationAttempt(
                        workspace_id=workspace_id,
                        actor_kind="human",
                        actor_id="migration-actor",
                        tool_name="patch_context_edit",
                        idempotency_key="key",
                        started_at=now,
                        expires_at=now + timedelta(hours=24),
                    )
                )
                session.flush()
            attempt.completed_at = now + timedelta(seconds=1)
            attempt.result_payload = {"saved": True}
            session.commit()
            assert session.get(MCPOperationAttempt, attempt.id).result_payload == {
                "saved": True
            }
        command.downgrade(config, PARENT)
        assert "mcp_operation_attempt" not in sa.inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.get(MCPWriteIdempotency, receipt_id).result_payload == {
                "saved": True
            }
        command.upgrade(config, REVISION)
        with Session(engine) as session:
            assert (
                session.scalar(
                    sa.select(sa.func.count()).select_from(MCPOperationAttempt)
                )
                == 0
            )
            assert session.get(MCPWriteIdempotency, receipt_id).result_payload == {
                "saved": True
            }
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
