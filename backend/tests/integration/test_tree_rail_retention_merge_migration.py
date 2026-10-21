"""Upgrade either independently installed feature schema to the shared head."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.ops.input_maintenance import audit_inputs, repair_input
from backend.tests.utils.migrated_sqlite_templates import shared_templates

TREE_RAIL = "c7a4e91b2d60"
RETENTION = "d148b9746ff5"
MERGED = "49d9c0776c5a"
INPUTS = "19db79d3e622"
CREATED_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
RETENTION_SENTINEL = {"orphan_retention_hours": 17}


def _config(url):
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _seed_workspace(connection, workspace_id, *, retention):
    # Reflection uses the actual parent schema, without borrowing missing
    # columns or defaults from the current ORM model.
    workspace = Table("workspace", MetaData(), autoload_with=connection)
    row = {
        "id": workspace_id.hex,
        "slug": "merge-preservation",
        "title": "Preserved workspace",
        "name": "Preserved workspace",
        "goal": "Keep both independently installed features",
        "default_language": "en",
        "library_policy": {},
        "write_policy": {},
        "citation_policy": {},
        "agent_injection_policy": {},
        "review_policy": {},
        "lint_policy": {},
        "auto_fix_enabled": False,
        "ask_retrieval_mode": "auto",
        "edge_projection_version": 0,
        "authority_projection_version": -1,
        "visibility": "private",
        "join_policy": "invite_only",
        "directory_title_key": "preserved workspace",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "created_by_actor_id": uuid4().hex,
        "owner_actor_id": uuid4().hex,
    }
    if retention:
        row["context_media_policy"] = RETENTION_SENTINEL
    connection.execute(workspace.insert().values(**row))


@pytest.mark.parametrize(
    "parent", [TREE_RAIL, RETENTION], ids=["tree-rail", "retention"]
)
def test_upgrade_from_either_parent_preserves_existing_feature_data(
    tmp_path, tmp_path_factory, parent
):
    url = shared_templates(tmp_path_factory).clone(
        "rev_efcbcf724889", tmp_path / "merge.sqlite"
    )
    config = _config(url)
    # Apply the original parent for real. Stamping a complete current schema
    # with an old ID would fail to exercise either missing sibling migration.
    command.upgrade(config, parent)
    workspace_id = uuid4()
    user_id = uuid4()
    target_id = uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == [parent]
            inspector = inspect(connection)
            has_retention = "context_media_policy" in {
                column["name"] for column in inspector.get_columns("workspace")
            }
            assert has_retention is (parent == RETENTION)
            assert inspector.has_table("user_favorite") is (parent == TREE_RAIL)
            _seed_workspace(connection, workspace_id, retention=has_retention)
            if parent == TREE_RAIL:
                favorites = Table("user_favorite", MetaData(), autoload_with=connection)
                connection.execute(
                    favorites.insert().values(
                        user_id=user_id.hex,
                        workspace_id=workspace_id.hex,
                        target_kind="context_page",
                        target_id=target_id.hex,
                        created_at=CREATED_AT,
                    )
                )

        assert audit_inputs(url) == []
        repair_input(url, "workspace", workspace_id, "title", "Preserved workspace")
        command.upgrade(config, MERGED)
        assert audit_inputs(url) == []
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == [INPUTS]
            inspector = inspect(connection)
            for table in ("context_page", "agent_page", "library_item"):
                assert "content_changed_at" in {
                    column["name"] for column in inspector.get_columns(table)
                }
            workspace = Table("workspace", MetaData(), autoload_with=connection)
            saved = (
                connection.execute(
                    select(workspace).where(workspace.c.id == workspace_id.hex)
                )
                .mappings()
                .one()
            )
            assert saved["title"] == "Preserved workspace"
            assert saved["goal"] == "Keep both independently installed features"
            assert saved["context_media_policy"] == (
                RETENTION_SENTINEL if parent == RETENTION else {}
            )
            favorites = Table("user_favorite", MetaData(), autoload_with=connection)
            saved_favorites = connection.execute(select(favorites)).mappings().all()
            if parent == TREE_RAIL:
                assert len(saved_favorites) == 1
                assert dict(saved_favorites[0]) == {
                    "user_id": user_id.hex,
                    "workspace_id": workspace_id.hex,
                    "target_kind": "context_page",
                    "target_id": target_id.hex,
                    "created_at": CREATED_AT.replace(tzinfo=None),
                }
            else:
                assert saved_favorites == []
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
