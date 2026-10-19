from datetime import UTC, datetime
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.context_media_policy import read_context_media_policy


def test_migration_keeps_existing_workspaces_in_inheritance_mode(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'retention-migration.db'}"
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "efcbcf724889")
    engine = create_engine(url)
    workspace_id = uuid4()
    with engine.begin() as connection:
        # Insert at the old schema without loading the new ORM column.
        table = Table("workspace", MetaData(), autoload_with=connection)
        # Supply the fields required by the old schema.
        row = {
            "id": workspace_id.hex,
            "slug": "retention-migration",
            "title": "Retention",
            "auto_fix_enabled": False,
            "ask_retrieval_mode": "auto",
            "edge_projection_version": 0,
            "authority_projection_version": -1,
            "visibility": "private",
            "join_policy": "invite_only",
            "directory_title_key": "retention",
            "created_at": datetime(2026, 10, 18, tzinfo=UTC),
            "updated_at": datetime(2026, 10, 18, tzinfo=UTC),
            "name": "Retention",
            "goal": "Keep defaults",
            "default_language": "en",
            "library_policy": {},
            "write_policy": {},
            "citation_policy": {},
            "agent_injection_policy": {},
            "review_policy": {},
            "lint_policy": {},
            "created_by_actor_id": uuid4().hex,
            "owner_actor_id": uuid4().hex,
        }
        connection.execute(table.insert().values(**row))
    command.upgrade(config, "head")
    factory = sessionmaker(engine)
    for default in (168, 72, 36, 12, 0):
        with factory() as session:
            policy = read_context_media_policy(
                session, UUID(str(workspace_id)), default
            )
            assert policy.override_hours is None
            assert policy.effective_hours == default
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT context_media_policy FROM workspace "
                    "WHERE slug='retention-migration'"
                )
            )
            == "{}"
        )
    engine.dispose()
