import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import (
    LLMVerificationOperation,
    User,
    UserLLMConnection,
    Workspace,
    WorkspaceLLMConnection,
)
from backend.repositories.workspace_repo import insert_workspace
from backend.tests.utils.migration_paths import configure_test_repositories
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

_PARENT = "002398e0fcdc"
_REVISION = "dafc5339b3b1"
_PERSONAL = UserLLMConnection.__tablename__
_WORKSPACE = WorkspaceLLMConnection.__tablename__
_VERIFICATION = LLMVerificationOperation.__tablename__


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_llm_connection_migration_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'llm-connections.sqlite'}"
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

    try:
        command.upgrade(config, _PARENT)
        parent_tables = set(sa.inspect(engine).get_table_names())
        with Session(engine) as session, session.begin():
            user = User(display_name="Existing account")
            session.add(user)
            workspace = insert_workspace(
                session, slug="existing", title="Existing Wiki"
            )
            session.flush()
            user_id, workspace_id = user.id, workspace.id

        command.upgrade(config, _REVISION)
        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names()) == parent_tables | {
            _PERSONAL,
            _WORKSPACE,
            _VERIFICATION,
        }
        assert inspector.get_foreign_keys(_PERSONAL)[0]["referred_table"] == "users"
        assert inspector.get_foreign_keys(_WORKSPACE) == []
        assert {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(_WORKSPACE)
        } == {("workspace_id", "id")}

        personal_id, second_personal_id, workspace_connection_id = (
            uuid4() for _ in range(3)
        )
        with engine.begin() as connection:
            for table, scope, scope_id, identifier in (
                (_PERSONAL, "user_id", user_id, personal_id),
                (_PERSONAL, "user_id", user_id, second_personal_id),
                (_WORKSPACE, "workspace_id", workspace_id, workspace_connection_id),
            ):
                connection.execute(
                    sa.text(
                        f"INSERT INTO {table} (id, {scope}, name, adapter_id, "
                        "adapter_implementation_version, config_schema_version, "
                        "endpoint_profile, created_at, updated_at) VALUES "
                        "(:id, :owner, 'Example', 'builtin.openai', '1', 1, '{}', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ).bindparams(
                        sa.bindparam("id", identifier, type_=sa.Uuid()),
                        sa.bindparam("owner", scope_id, type_=sa.Uuid()),
                    )
                )

        with Session(engine) as session:
            common = session.get(WorkspaceLLMConnection, workspace_connection_id)
            assert (common.status, common.verification_status) == (
                "disabled",
                "unverified",
            )
            assert (common.revision, common.secret_revision) == (1, 0)
            assert (
                common.active_credential is None and common.pending_credential is None
            )
            assert (
                common.provider_options,
                common.model_profiles,
                common.allowed_roles,
            ) == ({}, [], [])
            personal = session.get(UserLLMConnection, personal_id)
            assert len(session.scalars(sa.select(UserLLMConnection)).all()) == 2
            personal.active_credential = {"ciphertext": "fixture"}
            personal.status = "deleted"
            with pytest.raises(sa.exc.DBAPIError, match="deleted_credentials"):
                session.flush()

        # One verification in progress per connection; finished ones stack.
        def verification(status, scope="user", workspace=None):
            return LLMVerificationOperation(
                scope=scope,
                user_id=user_id,
                workspace_id=workspace,
                connection_id=personal_id,
                target="active",
                config_hash="hash",
                status=status,
            )

        with Session(engine) as session, session.begin():
            session.add_all([verification("succeeded"), verification("failed")])
            session.add(verification("queued"))
        with Session(engine) as session:
            stored = session.scalars(sa.select(LLMVerificationOperation)).all()
            assert [item.results for item in stored] == [[], [], []]
            for duplicate, constraint in (
                (verification("running"), "UNIQUE|unique|duplicate"),
                (verification("succeeded", "user", workspace_id), "workspace"),
            ):
                session.add(duplicate)
                with pytest.raises(sa.exc.DBAPIError, match=constraint):
                    session.flush()
                session.rollback()

        command.downgrade(config, _PARENT)
        assert set(sa.inspect(engine).get_table_names()) == parent_tables
        command.upgrade(config, _REVISION)
        with Session(engine) as session:
            assert session.get(User, user_id).display_name == "Existing account"
            assert session.get(Workspace, workspace_id).title == "Existing Wiki"
            assert (
                session.scalar(
                    sa.select(sa.func.count()).select_from(UserLLMConnection)
                )
                == 0
            )
            for model in (WorkspaceLLMConnection, LLMVerificationOperation):
                assert (
                    session.scalar(sa.select(sa.func.count()).select_from(model)) == 0
                )
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
