"""One populated upgrade/downgrade/re-upgrade traversal per dialect.

Historical reclassification, aggregate evidence and the new schema constraints
share this traversal; no extra database scenarios are needed for those states.
"""

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from backend.models.context import EdgeProjection
from backend.models.graph import CoAccessPair
from backend.models.mcp_chat import (
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT = "1d8f1c08ca36"
REVISION = "7c2bde1490af"
VIEW_INDEX = "ix_mcp_chat_tool_call_artifact_viewed_session"
PAIR_INDEX = "ix_mcp_chat_tool_call_artifact_viewed_pair"


@contextmanager
def _check_violation(dialect, name):
    # pg8000 classifies SQLSTATE 23514 as ProgrammingError; SQLite uses
    # IntegrityError. Check the actual named CHECK and PG SQLSTATE rather
    # than broadening the expectation to any failed statement.
    with pytest.raises(DBAPIError, match=name) as violation:
        yield
    if dialect == "postgresql":
        assert violation.value.orig.args[0]["C"] == "23514"
    else:
        assert isinstance(violation.value, IntegrityError)


def _legacy_trace(
    session, workspace_id, *, at, documents, tool="read", status="succeeded"
):
    client = MCPClient(
        workspace_id=workspace_id,
        name=str(uuid4()),
        client_type="generic",
        scopes=[],
        status="active",
    )
    session.add(client)
    session.flush()
    # This data belongs to the pre-cursor schema. Explicit old columns keep
    # the traversal independent of newer nullable ORM columns.
    chat = SimpleNamespace(id=uuid4())
    session.execute(
        McpChatSession.__table__.insert().values(
            id=chat.id,
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(uuid4()),
            started_at=at,
            last_activity_at=at,
        )
    )
    per_call = []
    for offset, document in enumerate(documents):
        occurred = at + timedelta(minutes=offset)
        call = MCPToolCall(
            workspace_id=workspace_id,
            client_id=client.id,
            request_id=uuid4(),
            tool_name=tool,
            result_status=status,
            started_at=occurred,
            completed_at=occurred,
        )
        session.add(call)
        session.flush()
        row = SimpleNamespace(id=uuid4(), chat_session_id=chat.id)
        session.execute(
            McpChatToolCallArtifact.__table__.insert().values(
                id=row.id,
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=call.id,
                resource_kind="context_page",
                resource_id=document,
                verb="referenced",
                occurred_at=occurred,
            )
        )
        session.add(
            McpChatArtifactLink(
                workspace_id=workspace_id,
                chat_session_id=chat.id,
                resource_kind="context_page",
                resource_id=document,
                verb="referenced",
                first_occurred_at=occurred,
                last_occurred_at=occurred,
                occurrence_count=1,
                last_mcp_tool_call_id=call.id,
            )
        )
        per_call.append(row)
    session.flush()
    return per_call


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_lifetime_co_access_round_trip_preserves_trace_and_rebuilds_evidence(
    dialect, tmp_path, tmp_path_factory
):
    if dialect == "sqlite":
        url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "lifetime-co-access.sqlite"
        )
    else:
        url = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
        if not url or not url.startswith("postgresql"):
            pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = create_engine(url)
    config = _config(url)
    migrated = False
    try:
        if dialect == "postgresql":
            assert not (set(inspect(engine).get_table_names()) - {"alembic_version"})
        command.upgrade(config, PARENT)
        migrated = True
        assert "co_access_pair" not in inspect(engine).get_table_names()
        left, right, single = UUID(int=101), UUID(int=102), UUID(int=103)
        now = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
        old = now - timedelta(days=90)
        with Session(engine) as session, session.begin():
            workspace = seed_default_workspace(session)
            workspace_id = workspace.id
            untouched = seed_default_workspace(session, slug="untouched")
            untouched_id = untouched.id
            viewed = _legacy_trace(
                session, workspace_id, at=old, documents=(left, right, single)
            )
            viewed += _legacy_trace(
                session, workspace_id, at=now, documents=(right, left)
            )
            recent_left, recent_right = UUID(int=104), UUID(int=105)
            viewed += _legacy_trace(
                session,
                workspace_id,
                at=now - timedelta(days=20),
                documents=(recent_left, recent_right),
            )
            viewed += _legacy_trace(
                session,
                workspace_id,
                at=now,
                documents=(recent_left, recent_right),
            )
            references = _legacy_trace(
                session,
                workspace_id,
                at=now,
                documents=(left, right),
                tool="read_structure_map",
            )
            failed = _legacy_trace(
                session, workspace_id, at=now, documents=(single,), status="failed"
            )
            # Mix a body view and edit-open reference in one existing rollup.
            source = viewed[0]
            source_session_id = source.chat_session_id
            opener = MCPToolCall(
                workspace_id=workspace_id,
                client_id=session.scalar(
                    select(McpChatSession.client_id).where(
                        McpChatSession.id == source_session_id
                    )
                ),
                request_id=uuid4(),
                tool_name="begin_context_edit",
                result_status="succeeded",
                started_at=old,
                completed_at=old + timedelta(seconds=30),
            )
            session.add(opener)
            session.flush()
            mixed = SimpleNamespace(id=uuid4())
            session.execute(
                McpChatToolCallArtifact.__table__.insert().values(
                    id=mixed.id,
                    workspace_id=workspace_id,
                    chat_session_id=source.chat_session_id,
                    mcp_tool_call_id=opener.id,
                    resource_kind="context_page",
                    resource_id=left,
                    verb="referenced",
                    occurred_at=opener.completed_at,
                )
            )
            mixed_rollup = session.scalar(
                select(McpChatArtifactLink).where(
                    McpChatArtifactLink.chat_session_id == source.chat_session_id,
                    McpChatArtifactLink.resource_id == left,
                )
            )
            mixed_rollup.occurrence_count = 2
            mixed_rollup.last_occurred_at = opener.completed_at
            mixed_rollup.last_mcp_tool_call_id = opener.id
            session.flush()
            viewed_ids = {row.id for row in viewed}
            reference_ids = {row.id for row in (*references, *failed, mixed)}
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=left,
                    target_kind="context_page",
                    target_id=right,
                    edge_kind="co_access",
                    provenance="usage",
                    # Equal counts/dates must still replace legacy provenance.
                    weight=2,
                    support_session_count=2,
                    first_observed_at=old + timedelta(minutes=1),
                    occurred_at=now + timedelta(minutes=1),
                    source_ref={"table": "mcp_chat_tool_call_artifact"},
                )
            )
        with Session(engine) as session:
            untouched_version = session.get(
                Workspace, untouched_id
            ).edge_projection_version
            original_calls = session.execute(
                select(
                    MCPToolCall.id, MCPToolCall.tool_name, MCPToolCall.result_status
                ).order_by(MCPToolCall.id)
            ).all()

        def verify_upgrade():
            inspector = inspect(engine)
            indexes = {
                index["name"]: index
                for index in inspector.get_indexes("mcp_chat_tool_call_artifact")
            }
            assert indexes[VIEW_INDEX]["column_names"] == [
                "workspace_id",
                "chat_session_id",
                "occurred_at",
                "mcp_tool_call_id",
                "id",
            ]
            assert indexes[PAIR_INDEX]["column_names"] == [
                "workspace_id",
                "chat_session_id",
                "resource_kind",
                "resource_id",
                "co_access_segment",
                "co_access_position",
            ]
            predicate = indexes[VIEW_INDEX]["dialect_options"][f"{dialect}_where"]
            assert "viewed" in str(predicate)
            pair_columns = {
                column["name"] for column in inspector.get_columns("co_access_pair")
            }
            assert "chat_session_id" not in pair_columns
            assert "workspace_id" in pair_columns
            with Session(engine) as session:
                pairs = session.scalars(select(CoAccessPair)).all()
                by_target = {(row.source_id, row.target_id): row for row in pairs}
                assert set(by_target) == {
                    (left, right),
                    (left, single),
                    (right, single),
                    (recent_left, recent_right),
                }
                pair = by_target[(left, right)]
                assert pair.support_session_count == 2
                assert pair.first_observed_at == old + timedelta(minutes=1)
                assert pair.occurred_at == now + timedelta(minutes=1)
                assert by_target[(left, single)].support_session_count == 1
                edge = session.scalar(
                    select(EdgeProjection).where(
                        EdgeProjection.edge_kind == "co_access",
                        EdgeProjection.source_id == left,
                        EdgeProjection.target_id == right,
                    )
                )
                assert edge.weight == 2
                assert edge.provenance == "usage"
                assert edge.source_ref == {"table": "co_access_pair"}
                assert edge.support_session_count == 2
                assert edge.first_observed_at == pair.first_observed_at
                assert edge.occurred_at == pair.occurred_at
                assert (
                    session.get(Workspace, untouched_id).edge_projection_version
                    == untouched_version
                )
                cursor = session.get(McpChatSession, source_session_id).co_access_state
                assert cursor["version"] == 1 and len(cursor["runs"]) <= 12
                assert cursor["segment"] == 0
                positions = session.execute(
                    select(
                        McpChatToolCallArtifact.co_access_segment,
                        McpChatToolCallArtifact.co_access_position,
                    ).where(McpChatToolCallArtifact.id.in_(viewed_ids))
                ).all()
                assert all(
                    segment is not None and 0 <= position < 12
                    for segment, position in positions
                )
                assert all(
                    segment is None and position is None
                    for segment, position in session.execute(
                        select(
                            McpChatToolCallArtifact.co_access_segment,
                            McpChatToolCallArtifact.co_access_position,
                        ).where(McpChatToolCallArtifact.id.in_(reference_ids))
                    )
                )
                assert (
                    session.get(Workspace, workspace_id).authority_projection_version
                    == -1
                )
                assert dict(
                    session.execute(
                        select(McpChatToolCallArtifact.id, McpChatToolCallArtifact.verb)
                    ).all()
                ) == {
                    **dict.fromkeys(viewed_ids, "viewed"),
                    **dict.fromkeys(reference_ids, "referenced"),
                }
                assert (
                    session.execute(
                        select(
                            MCPToolCall.id,
                            MCPToolCall.tool_name,
                            MCPToolCall.result_status,
                        ).order_by(MCPToolCall.id)
                    ).all()
                    == original_calls
                )
                counts = session.execute(
                    select(
                        McpChatArtifactLink.verb, McpChatArtifactLink.occurrence_count
                    ).where(
                        McpChatArtifactLink.chat_session_id == source_session_id,
                        McpChatArtifactLink.resource_id == left,
                    )
                ).all()
                assert sorted(counts) == [("referenced", 1), ("viewed", 1)]
                values = dict(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=left,
                    target_kind="context_page",
                    target_id=right,
                    support_session_count=2,
                    first_observed_at=old,
                    occurred_at=now,
                )
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(CoAccessPair.__table__.insert().values(**values))
            with (
                _check_violation(dialect, "ck_co_access_pair_positive_support"),
                engine.begin() as connection,
            ):
                connection.execute(
                    CoAccessPair.__table__.insert().values(
                        **{
                            **values,
                            "target_id": UUID(int=104),
                            "support_session_count": 0,
                        }
                    )
                )
            with (
                _check_violation(dialect, "ck_co_access_pair_canonical_order"),
                engine.begin() as connection,
            ):
                connection.execute(
                    CoAccessPair.__table__.insert().values(
                        **{**values, "source_id": right, "target_id": left}
                    )
                )
            with (
                _check_violation(dialect, "ck_co_access_pair_observed_span"),
                engine.begin() as connection,
            ):
                connection.execute(
                    CoAccessPair.__table__.insert().values(
                        **{
                            **values,
                            "target_id": UUID(int=104),
                            "first_observed_at": now + timedelta(days=1),
                        }
                    )
                )

        command.upgrade(config, REVISION)
        verify_upgrade()

        before_downgrade = datetime.now(UTC)
        command.downgrade(config, PARENT)
        after_downgrade = datetime.now(UTC)
        assert "co_access_pair" not in inspect(engine).get_table_names()
        assert "co_access_state" not in {
            column["name"] for column in inspect(engine).get_columns("mcp_chat_session")
        }
        assert VIEW_INDEX not in {
            index["name"]
            for index in inspect(engine).get_indexes("mcp_chat_tool_call_artifact")
        }
        assert PAIR_INDEX not in {
            index["name"]
            for index in inspect(engine).get_indexes("mcp_chat_tool_call_artifact")
        }
        with Session(engine) as session:
            assert set(session.scalars(select(McpChatToolCallArtifact.verb))) == {
                "referenced"
            }
            assert (
                sum(session.scalars(select(McpChatArtifactLink.occurrence_count))) == 13
            )
            legacy_edges = session.scalars(
                select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
            ).all()
            # The 90-day contribution disappears; the surviving recent pair
            # carries the released decayed read sum, never lifetime count 2.
            legacy_by_pair = {
                (row.source_id, row.target_id): row for row in legacy_edges
            }
            # Released semantics also counted structural references: this pair
            # has one current body-read chat plus one structural-read chat.
            assert set(legacy_by_pair) == {(left, right), (recent_left, recent_right)}
            assert legacy_by_pair[(left, right)].first_observed_at == now + timedelta(
                minutes=1
            )
            legacy = legacy_by_pair[(recent_left, recent_right)]
            assert legacy.source_ref == {"table": "mcp_chat_tool_call_artifact"}

            def expected_legacy_weight(at):
                return sum(
                    0.5 ** (max(0, (at - paired_at).total_seconds()) / 86400 / 30)
                    for paired_at in (
                        now - timedelta(days=20) + timedelta(minutes=1),
                        now + timedelta(minutes=1),
                    )
                )

            assert (
                expected_legacy_weight(after_downgrade)
                <= legacy.weight
                <= expected_legacy_weight(before_downgrade)
            )
        command.upgrade(config, REVISION)
        verify_upgrade()
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION
            )
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
                with engine.begin() as connection:
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()
