from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

epic1 = import_module("backend.migrations.versions.0002_epic1")
workspace_phase = import_module(
    "backend.migrations.revision_0002.workspace_schema"
)
mcp_phase = import_module("backend.migrations.revision_0002.identity_access_schema")
public_phase = import_module(
    "backend.migrations.revision_0002.context_schema"
)


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


def _insert_workspace(connection, *, workspace_id: UUID, slug: str, visibility: str):
    now = datetime.now(UTC)
    actor_id = UUID("00000000-0000-0000-0000-000000000010")
    connection.execute(
        text(
            "INSERT INTO workspace ("
            "id, slug, title, name, goal, default_language, library_policy, "
            "write_policy, citation_policy, agent_injection_policy, review_policy, "
            "lint_policy, auto_fix_enabled, visibility, join_policy, "
            "created_by_actor_id, owner_actor_id, updated_at, created_at, "
            "directory_title_key"
            ") VALUES ("
            ":id, :slug, :title, :name, :goal, 'en', :policy, :policy, :policy, "
            ":policy, :policy, :policy, 0, :visibility, 'invite_only', :actor, "
            ":actor, :now, :now, :title_key)"
        ),
        {
            "id": workspace_id.hex,
            "slug": slug,
            "title": slug,
            "name": slug,
            "goal": "migration fixture",
            "policy": json.dumps({}),
            "visibility": visibility,
            "actor": actor_id.hex,
            "now": now,
            "title_key": slug.casefold(),
        },
    )


def _insert_legacy_public_page(
    connection,
    *,
    workspace_id: UUID,
    slug: str,
    lifecycle_status: str = "published",
    pointed_version_page_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    page_id = uuid4()
    version_page_id = pointed_version_page_id or page_id
    version_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO context_page ("
            "id, workspace_id, slug, title, page_type, tags, aliases, "
            "lifecycle_status, is_public, created_from_ask_turn_ids, "
            "updated_at, created_at"
            ") VALUES ("
            ":id, :workspace_id, :slug, :slug, 'overview', :empty, :empty, "
            ":lifecycle_status, 1, :empty, :now, :now)"
        ),
        {
            "id": page_id.hex,
            "workspace_id": workspace_id.hex,
            "slug": slug,
            "empty": json.dumps([]),
            "lifecycle_status": lifecycle_status,
            "now": now,
        },
    )
    if pointed_version_page_id is None:
        connection.execute(
            text(
                "INSERT INTO context_page_version ("
                "id, page_id, version_no, body_md, rendering_profile, content_hash, "
                "graph_status, actor_type, version_status, created_by_actor_kind, "
                "created_by_actor_id, created_at"
                ") VALUES ("
                ":id, :page_id, 1, :body, 'gfm_mermaid', :content_hash, 'linear', "
                "'human', 'published_snapshot', 'human', :actor_id, :now)"
            ),
            {
                "id": version_id.hex,
                "page_id": version_page_id.hex,
                "body": f"# {slug}\n",
                "content_hash": slug.ljust(64, "0")[:64],
                "actor_id": str(UUID(int=16)),
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE context_page SET current_version_id = :version_id, "
                "current_published_version_id = :version_id WHERE id = :page_id"
            ),
            {"version_id": version_id.hex, "page_id": page_id.hex},
        )
    return page_id, version_id


def test_public_exposure_phase_backfills_only_valid_workspace_grants(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'public-exposure.db'}"
    config = _config(database_url)
    command.upgrade(config, "0001_initial_release")
    engine = create_engine(database_url)
    try:
        _run_steps(
            engine,
            epic1._upgrade_base_schema,
            workspace_phase.upgrade,
            mcp_phase.upgrade,
        )
        public_workspace_id = uuid4()
        team_workspace_id = uuid4()
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspace_id=public_workspace_id,
                slug="public-workspace",
                visibility="public",
            )
            _insert_workspace(
                connection,
                workspace_id=team_workspace_id,
                slug="team-workspace",
                visibility="team",
            )
            valid_page_id, valid_version_id = _insert_legacy_public_page(
                connection,
                workspace_id=public_workspace_id,
                slug="valid-public",
            )
            team_page_id, _ = _insert_legacy_public_page(
                connection,
                workspace_id=team_workspace_id,
                slug="invalid-team",
            )
            archived_page_id, _ = _insert_legacy_public_page(
                connection,
                workspace_id=public_workspace_id,
                slug="invalid-archived",
                lifecycle_status="archived",
            )
            headless_page_id, _ = _insert_legacy_public_page(
                connection,
                workspace_id=public_workspace_id,
                slug="invalid-headless",
                pointed_version_page_id=uuid4(),
            )
            cross_page_id, _ = _insert_legacy_public_page(
                connection,
                workspace_id=public_workspace_id,
                slug="invalid-cross-page",
                pointed_version_page_id=valid_page_id,
            )
            connection.execute(
                text(
                    "UPDATE context_page SET current_version_id = :version_id, "
                    "current_published_version_id = :version_id WHERE id = :page_id"
                ),
                {
                    "version_id": valid_version_id.hex,
                    "page_id": cross_page_id.hex,
                },
            )

        _run_steps(engine, public_phase.upgrade)
        with engine.connect() as connection:
            rows = {
                row.id: row
                for row in connection.execute(
                    text(
                        "SELECT id, is_public, public_exposure_scope, "
                        "public_exposed_version_id FROM context_page"
                    )
                ).mappings()
            }
            assert rows[valid_page_id.hex]["is_public"] == 1
            assert rows[valid_page_id.hex]["public_exposure_scope"] == "indexed_web"
            assert (
                rows[valid_page_id.hex]["public_exposed_version_id"]
                == valid_version_id.hex
            )
            for page_id in (
                team_page_id,
                archived_page_id,
                headless_page_id,
                cross_page_id,
            ):
                assert rows[page_id.hex]["is_public"] == 0
                assert rows[page_id.hex]["public_exposure_scope"] is None
                assert rows[page_id.hex]["public_exposed_version_id"] is None

            inspector = inspect(connection)
            columns = {
                column["name"] for column in inspector.get_columns("context_page")
            }
            checks = {
                constraint["name"]
                for constraint in inspector.get_check_constraints("context_page")
            }
            foreign_keys = {
                constraint["name"]: constraint
                for constraint in inspector.get_foreign_keys("context_page")
            }
        assert {"public_exposure_scope", "public_exposed_version_id"} <= columns
        assert {
            "context_page_public_exposure_scope_check",
            "context_page_public_exposure_complete_check",
        } <= checks
        assert "fk_context_page_id_workspace_scoped" in foreign_keys
        exposure_fk = foreign_keys["fk_context_page_id_workspace_scoped"]
        assert exposure_fk["constrained_columns"] == [
            "workspace_id",
            "id",
            "public_exposed_version_id",
        ]
        assert exposure_fk["referred_columns"] == [
            "workspace_id",
            "page_id",
            "id",
        ]

        _run_steps(
            engine,
            public_phase.prepare_downgrade_data,
            public_phase.downgrade,
        )
        assert {
            "public_exposure_scope",
            "public_exposed_version_id",
        }.isdisjoint(
            {
                column["name"]
                for column in inspect(engine).get_columns("context_page")
            }
        )
    finally:
        engine.dispose()
