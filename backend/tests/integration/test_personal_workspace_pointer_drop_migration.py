import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT_REVISION = "ef53a0a7061c"
DROP_REVISION = "5c3395a75eb0"
POINTER = "personal_workspace_id"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_personal_pointer_round_trip_keeps_workspaces_and_defaults(
    dialect, tmp_path, tmp_path_factory
):
    if dialect == "sqlite":
        database_url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "personal-pointer.sqlite"
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

        with Session(engine) as session, session.begin():
            workspace = Workspace(
                slug="personal-pointer", title="Pointer", name="Pointer"
            )
            session.add(workspace)
            session.flush()
            # Use the historical table shape: the current User model includes
            # authentication columns introduced after this revision.
            users = sa.Table("users", sa.MetaData(), autoload_with=session.connection())
            user_id = uuid4()
            stored_id = user_id.hex if dialect == "sqlite" else user_id
            stored_workspace_id = (
                workspace.id.hex if dialect == "sqlite" else workspace.id
            )
            now = datetime.now(UTC)
            session.execute(
                users.insert().values(
                    id=stored_id,
                    email="pointer@example.com",
                    password_hash="hash",
                    display_name="Pointer",
                    member_search_key="pointer pointer@example.com",
                    status="active",
                    session_generation=0,
                    default_workspace_id=stored_workspace_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            grant_workspace_membership(
                session, user_id=user_id, workspace_id=workspace.id, role="owner"
            )
            # The current model no longer maps the pointer, so set it in SQL.
            session.execute(text(f"UPDATE users SET {POINTER} = default_workspace_id"))

        metadata = sa.MetaData()
        with engine.connect() as connection:
            workspaces = sa.Table("workspace", metadata, autoload_with=connection)
            users = sa.Table("users", metadata, autoload_with=connection)
            memberships = sa.Table(
                "workspace_memberships", metadata, autoload_with=connection
            )
            assert connection.scalar(text(f"SELECT {POINTER} FROM users")) is not None

        def kept_rows(connection):
            return (
                connection.execute(
                    select(users.c.id, users.c.default_workspace_id)
                ).all(),
                connection.execute(select(workspaces.c.id, workspaces.c.slug)).all(),
                connection.execute(
                    select(memberships.c.user_id, memberships.c.role)
                ).all(),
            )

        with engine.connect() as connection:
            original = kept_rows(connection)
        assert len(original[0]) == len(original[1]) == len(original[2]) == 1

        for revision, pointer_expected in (
            (DROP_REVISION, False),
            (PARENT_REVISION, True),
            (DROP_REVISION, False),
        ):
            if pointer_expected:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
            inspector = inspect(engine)
            columns = {column["name"] for column in inspector.get_columns("users")}
            assert (POINTER in columns) is pointer_expected
            if pointer_expected:
                pointer_fk = next(
                    fk
                    for fk in inspector.get_foreign_keys("users")
                    if fk["constrained_columns"] == [POINTER]
                )
                assert pointer_fk["referred_table"] == "workspace"
                assert pointer_fk["options"].get("ondelete") == "SET NULL"
                assert (POINTER,) in {
                    tuple(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints("users")
                }
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == revision
                )
                # Workspaces, memberships and landing defaults survive both
                # directions; a restored pointer is never guessed back.
                assert kept_rows(connection) == original
                if pointer_expected:
                    pointer = connection.scalar(text(f"SELECT {POINTER} FROM users"))
                    assert pointer is None
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
                # test_postgres_migration, which runs next in this database,
                # requires no tables at all, including Alembic's bookkeeping.
                with engine.begin() as connection:
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()
