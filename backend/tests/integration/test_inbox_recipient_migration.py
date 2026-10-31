"""One upgrade/downgrade/re-upgrade traversal per supported dialect.

This migration scenario exercises preexisting purged-author data and real
schema defaults/constraints. It is separate from the ordinary functional test
budget; additional migration cases are unnecessary for these shared states.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.change import ChangeSet
from backend.models.inbox import WorkspaceInboxEntry, WorkspaceInboxRecipient
from backend.models.user import User
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT_REVISION = "af62c98d0731"
RECIPIENT_REVISION = "6f4a9f50c046"
RECIPIENT_TABLE = "workspace_inbox_recipient"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_recipient_fences_round_trip_preserves_history_and_live_inbox(
    dialect, tmp_path, tmp_path_factory
):
    if dialect == "sqlite":
        database_url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "inbox-recipient.sqlite"
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
        assert RECIPIENT_TABLE not in inspect(engine).get_table_names()

        author_id, inbox_only_id, system_id = uuid4(), uuid4(), uuid4()
        workspace_id = uuid4()
        # The shared parent predates Graph columns on the current ORM model.
        workspace = Table("workspace", MetaData(), autoload_with=engine)
        with Session(engine) as session, session.begin():
            active = User(
                email="live@example.test",
                display_name="Live recipient",
                password_hash="migration-test-hash",
            )
            session.add(active)
            session.flush()
            active_id = active.id
            session.execute(
                workspace.insert().values(
                    id=workspace_id.hex if dialect == "sqlite" else workspace_id,
                    slug="recipient-migration",
                    title="Retained",
                    directory_title_key="retained",
                    name="Retained",
                    goal="Retained migration history",
                    default_language="en",
                    library_policy={},
                    write_policy={},
                    citation_policy={},
                    agent_injection_policy={},
                    review_policy={},
                    lint_policy={},
                    auto_fix_enabled=False,
                    visibility="private",
                    created_by_actor_id=(
                        active_id.hex if dialect == "sqlite" else active_id
                    ),
                    owner_actor_id=(
                        active_id.hex if dialect == "sqlite" else active_id
                    ),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            session.add_all(
                [
                    ChangeSet(
                        workspace_id=workspace_id,
                        trigger_type="manual",
                        actor_type="human",
                        actor_id=author_id,
                        reason="Retained history after final account purge",
                    ),
                    # Repeated authors and a matching orphan entry must produce
                    # one fence for their shared recipient key.
                    ChangeSet(
                        workspace_id=workspace_id,
                        trigger_type="manual",
                        actor_type="human",
                        actor_id=author_id,
                    ),
                    ChangeSet(
                        workspace_id=workspace_id,
                        trigger_type="manual",
                        actor_type="human",
                        actor_id=active_id,
                    ),
                    ChangeSet(
                        workspace_id=workspace_id,
                        trigger_type="manual",
                        actor_type="system",
                        actor_id=system_id,
                    ),
                    ChangeSet(
                        workspace_id=workspace_id,
                        trigger_type="manual",
                        actor_type="human",
                        actor_id=None,
                    ),
                    WorkspaceInboxEntry(
                        workspace_id=workspace_id,
                        user_id=author_id,
                        kind="test.result",
                        item_key="late-result",
                        mode="mark",
                        read_revision="r1",
                    ),
                    WorkspaceInboxEntry(
                        workspace_id=workspace_id,
                        user_id=inbox_only_id,
                        kind="test.result",
                        item_key="orphan-without-history",
                        mode="mark",
                        read_revision="r2",
                    ),
                    WorkspaceInboxEntry(
                        workspace_id=workspace_id,
                        user_id=active_id,
                        kind="test.result",
                        item_key="live-personal-state",
                        mode="mark",
                        read_revision="r3",
                    ),
                ]
            )

        history = select(ChangeSet.__table__).order_by(ChangeSet.id)
        active_user = select(User.__table__).where(User.id == active_id)
        active_entries = select(WorkspaceInboxEntry.__table__).where(
            WorkspaceInboxEntry.user_id == active_id
        )

        def retained_rows(connection):
            return (
                connection.execute(history).all(),
                connection.execute(active_user).all(),
                connection.execute(active_entries).all(),
            )

        with engine.connect() as connection:
            original = retained_rows(connection)
        assert len(original[0]) == 5 and len(original[1]) == len(original[2]) == 1

        command.upgrade(config, RECIPIENT_REVISION)
        inspector = inspect(engine)
        assert inspector.get_pk_constraint(RECIPIENT_TABLE)["constrained_columns"] == [
            "workspace_id",
            "user_id",
        ]
        assert inspector.get_foreign_keys(RECIPIENT_TABLE) == []
        columns = {
            column["name"]: column for column in inspector.get_columns(RECIPIENT_TABLE)
        }
        assert not columns["workspace_id"]["nullable"]
        assert not columns["user_id"]["nullable"]
        assert columns["deleted_at"]["nullable"]
        assert columns["deleted_at"]["default"] is None
        with Session(engine) as session:
            recipients = session.scalars(select(WorkspaceInboxRecipient)).all()
            assert {(row.workspace_id, row.user_id) for row in recipients} == {
                (workspace_id, author_id),
                (workspace_id, inbox_only_id),
            }
            assert all(row.deleted_at is not None for row in recipients)
            assert list(session.scalars(select(WorkspaceInboxEntry.user_id))) == [
                active_id
            ]
        with engine.begin() as connection:
            connection.execute(
                WorkspaceInboxRecipient.__table__.insert().values(
                    workspace_id=workspace_id, user_id=active_id
                )
            )
        with Session(engine) as session:
            assert (
                session.get(
                    WorkspaceInboxRecipient, (workspace_id, active_id)
                ).deleted_at
                is None
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                WorkspaceInboxRecipient.__table__.insert().values(
                    workspace_id=workspace_id, user_id=active_id
                )
            )

        command.downgrade(config, PARENT_REVISION)
        assert RECIPIENT_TABLE not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert retained_rows(connection) == original
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == PARENT_REVISION
            )

        command.upgrade(config, RECIPIENT_REVISION)
        with Session(engine) as session:
            recipients = session.scalars(select(WorkspaceInboxRecipient)).all()
            # The history rebuilds its fence; Inbox-only orphans were already
            # reclaimed, and live recipients remain lazy after re-upgrade.
            assert [(row.workspace_id, row.user_id) for row in recipients] == [
                (workspace_id, author_id)
            ]
            assert recipients[0].deleted_at is not None
            assert list(session.scalars(select(WorkspaceInboxEntry.user_id))) == [
                active_id
            ]
        with engine.connect() as connection:
            assert retained_rows(connection) == original
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == RECIPIENT_REVISION
            )
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
                with engine.begin() as connection:
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()
