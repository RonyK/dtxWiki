import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.repositories.workspace_repo import insert_workspace
from backend.services.ask_session_service import ensure_ask_root
from backend.tests.utils.migration_paths import configure_test_repositories
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

_PARENT = "dafc5339b3b1"
_REVISION = "916a6162b789"
_NEW_COLUMNS = {
    "status",
    "error_code",
    "input_evidence",
    "generation",
    "started_at",
    "request_id",
    "selection",
    "steering_messages",
}
_PENDING_INDEX = "uq_ask_turn_session_pending"
_REQUEST_INDEX = "uq_ask_turn_request"


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_ask_turn_status_migration_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'ask-turn-status.sqlite'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not url or not url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(url)
    assert not sa.inspect(engine).get_table_names()
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    configure_test_repositories(config)

    def ask_turn_columns():
        return {column["name"] for column in sa.inspect(engine).get_columns("ask_turn")}

    def ask_turn_indexes():
        return {index["name"] for index in sa.inspect(engine).get_indexes("ask_turn")}

    def stored_turn():
        with engine.connect() as connection:
            return connection.execute(
                sa.text(
                    "SELECT user_message, assistant_message FROM ask_turn "
                    "WHERE id = :id"
                ).bindparams(sa.bindparam("id", old_turn_id, type_=sa.Uuid()))
            ).one()

    def stored_nulls():
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT input_evidence IS NULL, generation IS NULL FROM ask_turn "
                    "WHERE id = :id"
                ).bindparams(sa.bindparam("id", old_turn_id, type_=sa.Uuid()))
            ).one()
        return tuple(bool(value) for value in row)

    try:
        command.upgrade(config, _PARENT)
        parent_columns = ask_turn_columns()
        with Session(engine) as session, session.begin():
            workspace = insert_workspace(session, slug="existing", title="Existing")
            root = ensure_ask_root(session, workspace.id, actor_id=str(uuid4()))
            ask_session = AskSession(
                workspace_id=workspace.id,
                title="Existing session",
                actor_kind="human",
                placement_tree_node_id=root.id,
            )
            session.add(ask_session)
            session.flush()
            workspace_id, ask_session_id = workspace.id, ask_session.id
        # The parent schema has no status columns, so the turn is raw SQL.
        old_turn_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO ask_turn (id, created_at, workspace_id, "
                    "ask_session_id, turn_no, user_message, assistant_message, "
                    "topic_tags, promotion_state) VALUES (:id, CURRENT_TIMESTAMP, "
                    ":workspace_id, :ask_session_id, 1, 'Old question', "
                    "'Old answer', '[]', 'not_promoted')"
                ).bindparams(
                    sa.bindparam("id", old_turn_id, type_=sa.Uuid()),
                    sa.bindparam("workspace_id", workspace_id, type_=sa.Uuid()),
                    sa.bindparam("ask_session_id", ask_session_id, type_=sa.Uuid()),
                )
            )
        with Session(engine) as session, session.begin():
            session.add(
                AskTurnCitation(
                    workspace_id=workspace_id,
                    ask_turn_id=old_turn_id,
                    target_kind="context_page",
                    target_id=uuid4(),
                    rank=0,
                )
            )

        command.upgrade(config, _REVISION)
        assert ask_turn_columns() == parent_columns | _NEW_COLUMNS
        assert {_PENDING_INDEX, _REQUEST_INDEX} <= ask_turn_indexes()
        with Session(engine) as session, session.begin():
            old = session.get(AskTurn, old_turn_id)
            # An existing turn is a complete answer with no evidence record:
            # NULL, not JSON null, so it reads as an older turn.
            assert (old.status, old.error_code) == ("completed", None)
            assert stored_nulls() == (True, True)
            assert stored_turn() == ("Old question", "Old answer")
            assert old.started_at is None
            assert old.steering_messages == []

            def add_turn(turn_no, status, *, started_at=None, request_id=None):
                turn = AskTurn(
                    workspace_id=workspace_id,
                    ask_session_id=ask_session_id,
                    turn_no=turn_no,
                    user_message=f"Question {turn_no}",
                    status=status,
                    started_at=started_at,
                    request_id=request_id,
                )
                session.add(turn)
                session.flush()
                return turn.id

            # Queued turns wait without the slot; one started turn holds it.
            request_id = uuid4()
            add_turn(2, "pending", started_at=datetime.now(UTC))
            add_turn(3, "pending", request_id=request_id)
            add_turn(4, "pending")
            # SQLite names the column, PostgreSQL the index.
            with (
                pytest.raises(
                    sa.exc.DBAPIError,
                    match=rf"{_PENDING_INDEX}|ask_turn\.ask_session_id",
                ),
                session.begin_nested(),
            ):
                add_turn(5, "pending", started_at=datetime.now(UTC))
            with (
                pytest.raises(
                    sa.exc.DBAPIError, match=rf"{_REQUEST_INDEX}|ask_turn\.request_id"
                ),
                session.begin_nested(),
            ):
                add_turn(5, "pending", request_id=request_id)
            with (
                pytest.raises(sa.exc.DBAPIError, match="ck_ask_turn_status"),
                session.begin_nested(),
            ):
                add_turn(5, "pendng")

        command.downgrade(config, _PARENT)
        assert ask_turn_columns() == parent_columns
        assert not {_PENDING_INDEX, _REQUEST_INDEX} & ask_turn_indexes()
        assert stored_turn() == ("Old question", "Old answer")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM ask_turn_citation"))
                == 1
            )
        command.upgrade(config, _REVISION)
        with Session(engine) as session:
            assert session.get(AskTurn, old_turn_id).status == "completed"
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
