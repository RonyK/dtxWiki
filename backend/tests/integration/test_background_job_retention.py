"""One retention traversal, usable against SQLite or the dedicated PG test DB."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine
from backend.models.base import Base
from backend.models.job import Job
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.workspace import Workspace
from backend.services.background_job_retention import prune_background_history

pytestmark = pytest.mark.xdist_group("postgres")


def test_retention_preserves_active_period_and_domain_history_while_draining_backlog():
    # These rows form one mixed-history traversal: the cursor must pass an
    # entire batch of preserved unknown keys before reaching expired work.
    # The 30-day key remains protected even while a newer 1-day key exists,
    # which models interval settings changing 30d -> 1d -> 30d.
    database_url = os.getenv("DTXWIKI_POSTGRES_TEST_DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql"):
        database_url = "sqlite+pysqlite:///:memory:"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    # An interior point of a 30-day epoch bucket, with an old completion
    # inside the same bucket. The host clock never participates.
    interval_seconds = 720 * 3600
    bucket = int(datetime(2026, 10, 27, tzinfo=UTC).timestamp()) // interval_seconds
    now = datetime.fromtimestamp(bucket * interval_seconds, UTC) + timedelta(days=20)
    old = now - timedelta(days=10)
    old_minute = old.replace(second=0, microsecond=0).isoformat()
    day_bucket = int(now.timestamp()) // 86400
    prefix = uuid4().int & ~((1 << 32) - 1)

    def job_id(offset):
        return UUID(int=prefix + offset)

    def completed_job(offset, job_type, key, **changes):
        values = dict(
            id=job_id(offset),
            workspace_id=workspace_id,
            job_type=job_type,
            trigger_type="scheduled",
            status="completed",
            idempotency_key=key,
            completed_at=old,
            created_at=old,
        )
        values.update(changes)
        return Job(**values)

    def receipt(key, expires_at, **changes):
        values = dict(
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="retention-actor",
            tool_name="update_context_edit",
            idempotency_key=key,
            request_fingerprint="fixture",
            result_payload={"receipt": key},
            expires_at=expires_at,
        )
        values.update(changes)
        return MCPWriteIdempotency(**values)

    try:
        with session_factory.begin() as session:
            session.add_all(
                [
                    Workspace(
                        id=workspace_id,
                        slug=f"retention-{workspace_id}",
                        title="Retention",
                        name="Retention",
                    ),
                    Workspace(
                        id=other_workspace_id,
                        slug=f"retention-{other_workspace_id}",
                        title="Other",
                        name="Other",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                completed_job(offset, "lint_tick", f"unknown:{offset}")
                for offset in range(1, 101)
            )
            session.add_all(
                [
                    completed_job(101, "lint_tick", f"lint_tick:24h:{day_bucket - 12}"),
                    completed_job(102, "lint_tick", f"lint_tick:720h:{bucket}"),
                    completed_job(
                        103,
                        "bundle_expiration_sweep",
                        f"{workspace_id}:{old_minute}",
                        trigger_type="schedule",
                    ),
                    completed_job(
                        104,
                        "lint_tick",
                        f"lint_tick:24h:{day_bucket - 1}",
                        completed_at=now - timedelta(days=1),
                    ),
                    completed_job(
                        105,
                        "lint_tick",
                        f"lint_tick:24h:{day_bucket - 13}",
                        trigger_type="manual",
                    ),
                    completed_job(
                        106,
                        "media_blob_gc",
                        f"media_blob_gc:1h:{int(old.timestamp()) // 3600}",
                        status="failed",
                    ),
                    completed_job(107, "library_conversion", "conversion:old"),
                    completed_job(108, "embed", "embed:old"),
                    completed_job(109, "lint_tick", None),
                    completed_job(
                        110,
                        "trail_tick",
                        f"trail_tick:24h:{day_bucket - 12}",
                        workspace_id=other_workspace_id,
                    ),
                ]
            )
            expired = receipt("expired", now - timedelta(seconds=1))
            valid = receipt("valid", now + timedelta(hours=1))
            boundary = receipt("boundary", now)
            other = receipt("other-expired", old, workspace_id=other_workspace_id)
            session.add_all([expired, valid, boundary, other])

        first = prune_background_history(
            session_factory, workspace_id, now=now, batch_size=100
        )
        assert first.jobs_deleted == 0
        assert first.next_job_cursor == job_id(100)
        assert first.receipts_deleted == 1

        second = prune_background_history(
            session_factory,
            workspace_id,
            now=now,
            batch_size=100,
            job_cursor=first.next_job_cursor,
        )
        assert second.jobs_deleted == 2
        assert second.receipts_deleted == 0
        assert second.next_job_cursor is None
        with session_factory() as session:
            remaining = set(
                session.scalars(select(Job.id).where(Job.workspace_id == workspace_id))
            )
            assert remaining == {job_id(offset) for offset in range(1, 111)} - {
                job_id(101),
                job_id(103),
                job_id(110),
            }
            assert session.get(Job, job_id(110)) is not None
            assert session.get(MCPWriteIdempotency, expired.id) is None
            assert all(
                session.get(MCPWriteIdempotency, row.id) is not None
                for row in (valid, boundary, other)
            )

        # The remaining first batch contains only preserved unknown keys.
        # It still advances the cursor but must not take SQLite write intent.
        statements = []

        def record_statement(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            preserved = prune_background_history(
                session_factory, workspace_id, now=now, batch_size=100
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        assert preserved.next_job_cursor == job_id(100)
        assert preserved.jobs_deleted == preserved.receipts_deleted == 0
        assert statements and all(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
    finally:
        with session_factory.begin() as session:
            workspace_ids = (workspace_id, other_workspace_id)
            session.execute(
                delete(MCPWriteIdempotency).where(
                    MCPWriteIdempotency.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(delete(Job).where(Job.workspace_id.in_(workspace_ids)))
            session.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        engine.dispose()
