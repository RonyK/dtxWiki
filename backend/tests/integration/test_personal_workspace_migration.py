from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{tmp_path / name}"


def test_personal_workspace_schema_constraints_round_trip(tmp_path):
    database_url = _database_url(tmp_path, "personal-schema.db")
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "personal_workspace_id" in {
            column["name"] for column in inspector.get_columns("users")
        }
        assert "join_policy" in {
            column["name"] for column in inspector.get_columns("workspace")
        }
        personal_fk = next(
            fk
            for fk in inspector.get_foreign_keys("users")
            if fk["constrained_columns"] == ["personal_workspace_id"]
        )
        assert personal_fk["referred_table"] == "workspace"
        assert personal_fk["options"].get("ondelete") == "SET NULL"
        assert ("personal_workspace_id",) in {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("users")
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("workspace")
        } >= {
            "workspace_join_policy_check",
            "workspace_private_join_policy_check",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("workspace_memberships")
        } >= {"workspace_membership_role_check"}

        engine.dispose()
        command.downgrade(config, "0001_initial_release")
        engine = create_engine(database_url)
        downgraded = inspect(engine)
        assert "personal_workspace_id" not in {
            column["name"] for column in downgraded.get_columns("users")
        }
        assert "join_policy" not in {
            column["name"] for column in downgraded.get_columns("workspace")
        }
    finally:
        engine.dispose()


def test_personal_workspace_backfill_includes_suspended_and_preserves_live_default(
    tmp_path,
):
    database_url = _database_url(tmp_path, "personal-backfill.db")
    config = _config(database_url)
    command.upgrade(config, "0001_initial_release")
    engine = create_engine(database_url)
    active_with_default_id = uuid4()
    active_with_stale_default_id = uuid4()
    inactive_id = uuid4()
    default_workspace_id = uuid4()
    now = datetime.now(UTC)
    metadata = sa.MetaData()
    with engine.begin() as connection:
        users = sa.Table("users", metadata, autoload_with=connection)
        workspaces = sa.Table("workspace", metadata, autoload_with=connection)
        memberships = sa.Table(
            "workspace_memberships",
            metadata,
            autoload_with=connection,
        )
        connection.execute(
            workspaces.insert().values(
                id=default_workspace_id.hex,
                slug="default",
                title="Default Workspace",
                name="Default Workspace",
                description=None,
                goal="Existing installation workspace",
                default_language="en",
                library_policy={},
                write_policy={},
                citation_policy={},
                agent_injection_policy={},
                review_policy={},
                lint_policy={},
                auto_fix_enabled=False,
                visibility="private",
                created_by_actor_id=active_with_default_id.hex,
                owner_actor_id=active_with_default_id.hex,
                owner_user_id=None,
                updated_at=now,
                created_at=now,
            )
        )
        connection.execute(
            users.insert(),
            [
                _baseline_user(
                    active_with_default_id,
                    "active-default@example.com",
                    "active",
                    default_workspace_id,
                    now,
                ),
                _baseline_user(
                    active_with_stale_default_id,
                    "active-stale@example.com",
                    "active",
                    default_workspace_id,
                    now,
                ),
                _baseline_user(
                    inactive_id,
                    "inactive@example.com",
                    "suspended",
                    None,
                    now,
                ),
            ],
        )
        connection.execute(
            memberships.insert().values(
                user_id=active_with_default_id.hex,
                workspace_id=default_workspace_id.hex,
                role="viewer",
                joined_at=now,
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        migration = import_module(
            "backend.migrations.revision_0002.workspace_schema"
        )
        with engine.begin() as connection:
            migration._backfill_personal_workspaces(connection)
            migration._backfill_personal_workspaces(connection)
        metadata = sa.MetaData()
        with engine.connect() as connection:
            users = sa.Table("users", metadata, autoload_with=connection)
            workspaces = sa.Table("workspace", metadata, autoload_with=connection)
            memberships = sa.Table(
                "workspace_memberships",
                metadata,
                autoload_with=connection,
            )
            tree_nodes = sa.Table(
                "workspace_tree_node",
                metadata,
                autoload_with=connection,
            )
            tree_policies = sa.Table(
                "tree_policy",
                metadata,
                autoload_with=connection,
            )
            mcp_clients = sa.Table("mcp_client", metadata, autoload_with=connection)
            rows = {
                UUID(str(row.id)): row
                for row in connection.execute(select(users)).mappings().all()
            }

            first = rows[active_with_default_id]
            second = rows[active_with_stale_default_id]
            inactive = rows[inactive_id]
            assert first["personal_workspace_id"] is not None
            assert second["personal_workspace_id"] is not None
            assert inactive["personal_workspace_id"] is not None
            assert (
                len(
                    {
                        str(first["personal_workspace_id"]),
                        str(second["personal_workspace_id"]),
                        str(inactive["personal_workspace_id"]),
                    }
                )
                == 3
            )
            assert inactive["status"] == "suspended"
            assert UUID(str(first["default_workspace_id"])) == default_workspace_id
            assert UUID(str(second["default_workspace_id"])) == UUID(
                str(second["personal_workspace_id"])
            )
            assert UUID(str(inactive["default_workspace_id"])) == UUID(
                str(inactive["personal_workspace_id"])
            )

            for user_id, personal_id in (
                (active_with_default_id, first["personal_workspace_id"]),
                (active_with_stale_default_id, second["personal_workspace_id"]),
                (inactive_id, inactive["personal_workspace_id"]),
            ):
                personal = (
                    connection.execute(
                        select(workspaces).where(workspaces.c.id == personal_id)
                    )
                    .mappings()
                    .one()
                )
                assert personal["title"] == "Personal Workspace"
                assert personal["visibility"] == "private"
                assert personal["join_policy"] == "invite_only"
                assert UUID(str(personal["owner_user_id"])) == user_id
                assert "example" not in personal["slug"]
                assert (
                    connection.scalar(
                        select(sa.func.count())
                        .select_from(workspaces)
                        .where(
                            workspaces.c.owner_user_id == user_id.hex,
                            workspaces.c.slug.like("personal-%"),
                        )
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        select(memberships.c.role).where(
                            memberships.c.user_id == user_id.hex,
                            memberships.c.workspace_id == personal_id,
                        )
                    )
                    == "owner"
                )
                assert set(
                    connection.scalars(
                        select(tree_nodes.c.section).where(
                            tree_nodes.c.workspace_id == personal_id
                        )
                    )
                ) == {"context", "library"}
                assert set(
                    connection.scalars(
                        select(tree_policies.c.section).where(
                            tree_policies.c.workspace_id == personal_id
                        )
                    )
                ) == {"context", "library"}
                assert (
                    connection.scalar(
                        select(mcp_clients.c.id).where(
                            mcp_clients.c.workspace_id == personal_id
                        )
                    )
                    is None
                )

            assert (
                connection.scalar(
                    select(memberships.c.role).where(
                        memberships.c.user_id == active_with_default_id.hex,
                        memberships.c.workspace_id == default_workspace_id.hex,
                    )
                )
                == "viewer"
            )
    finally:
        engine.dispose()


def _baseline_user(
    user_id,
    email: str,
    status: str,
    default_workspace_id,
    now: datetime,
) -> dict:
    return {
        "id": user_id.hex,
        "email": email,
        "password_hash": "hash",
        "display_name": email.split("@", maxsplit=1)[0],
        "status": status,
        "session_generation": 0,
        "default_workspace_id": (
            default_workspace_id.hex if default_workspace_id is not None else None
        ),
        "updated_at": now,
        "created_at": now,
    }
