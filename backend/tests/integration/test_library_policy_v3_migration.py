from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models.workspace import (
    default_actor_id,
    default_agent_injection_policy,
    default_citation_policy,
    default_review_policy,
    default_write_policy,
)
from backend.services.library_formats import DEFAULT_FORMATS

epic1 = import_module("backend.migrations.versions.v0_1.efcbcf724889_core_schema")
workspace_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
)
mcp_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.identity_access_schema"
)
media_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.library_media_schema"
)
public_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.context_schema"
)
policy_migration = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.library_policy"
)


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


def _v3_custom_policy() -> dict[str, object]:
    return {
        "policy_schema_version": 3,
        "max_upload_bytes": 20_000_000,
        "manual_approval_required": False,
        "allow_store_without_converter": True,
        "unavailable_behavior": {
            "preview": "store_and_download",
            "semantic_conversion": "store_and_download",
        },
        "custom_formats": [
            {
                "format_id": " CUSTOM-CAD ",
                "display_name": " CAD   drawing ",
                "extensions": [" DWG ", ".DXF"],
            }
        ],
        "format_families": [
            {
                "family_id": "engineering",
                "name": "Engineering",
                "origin": "custom",
                "format_ids": ["custom-cad"],
                "max_upload_bytes": 450_000_000,
            }
        ],
        "format_overrides": {"custom-cad": 420_000_000},
    }


def test_v1_reset_keeps_only_explicit_current_preferences_and_flags():
    upgraded = policy_migration._upgrade_policy(
        {
            "policy_schema_version": 1,
            "max_upload_bytes": 2_000_000,
            "manual_approval_required": False,
            "allow_store_without_converter": False,
            "category_limits": {"document": 9_000_000},
            "enabled_formats": ["docx"],
            "format_families": [
                {
                    "family_id": "legacy",
                    "name": "Legacy",
                    "format_ids": ["docx"],
                    "max_upload_bytes": 8_000_000,
                }
            ],
            "format_overrides": {"docx": 7_000_000},
        }
    )

    assert upgraded == {
        "manual_approval_required": False,
        "policy_schema_version": 5,
        "max_upload_bytes": 2_000_000,
        "format_overrides": {},
        "builtin_format_overrides": {},
        "allow_store_without_converter": False,
        "format_families": [],
        "custom_formats": [],
        "unavailable_behavior": {
            "preview": "store_and_download",
            "semantic_conversion": "store_and_download",
        },
    }
    assert policy_migration._upgrade_policy(None)["max_upload_bytes"] == 20_000_000
    assert (
        policy_migration._upgrade_policy({"max_upload_bytes": "invalid"})[
            "max_upload_bytes"
        ]
        == 20_000_000
    )


def test_v2_development_preferences_are_reset_like_v1():
    upgraded = policy_migration._upgrade_policy(
        {
            "policy_schema_version": 2,
            "max_upload_bytes": 5_000_000,
            "format_overrides": {
                "docx": 42_000_000,
                "unknown": 4_000_000,
                "pdf": True,
            },
            "format_families": [
                {
                    "family_id": "documents",
                    "name": "Documents",
                    "origin": "custom",
                    "format_ids": ["docx", "xlsx"],
                    "max_upload_bytes": 45_000_000,
                },
                {
                    "family_id": "broken",
                    "name": "Broken",
                    "format_ids": ["unknown"],
                    "max_upload_bytes": 3_000_000,
                },
            ],
        }
    )

    assert upgraded["max_upload_bytes"] == 5_000_000
    assert upgraded["format_overrides"] == {}
    assert upgraded["format_families"] == []
    assert upgraded["custom_formats"] == []


def test_v3_transform_is_idempotent_and_preserves_valid_custom_references():
    once = policy_migration._upgrade_policy(_v3_custom_policy())
    twice = policy_migration._upgrade_policy(once)

    assert twice == once
    assert once["policy_schema_version"] == 5
    assert once["builtin_format_overrides"] == {}
    assert once["custom_formats"] == [
        {
            "format_id": "custom-cad",
            "display_name": "CAD drawing",
            "extensions": [".dwg", ".dxf"],
        }
    ]
    assert once["format_families"][0]["format_ids"] == ["custom-cad"]
    assert once["format_families"][0]["max_upload_bytes"] == 450_000_000
    assert once["format_overrides"] == {"custom-cad": 420_000_000}


def test_v4_aliases_upgrade_to_v5_with_an_immutable_preset_baseline():
    upgraded = policy_migration._upgrade_policy(
        {
            "policy_schema_version": 4,
            "builtin_format_overrides": {"docx": {"extensions": [" WORD "]}},
            "format_families": [
                {
                    "family_id": "documents",
                    "name": "Documents",
                    "origin": "preset",
                    "preset_id": "document",
                    "preset_revision": 1,
                    "format_ids": ["docx", "xlsx"],
                    "max_upload_bytes": None,
                }
            ],
        }
    )

    assert upgraded["policy_schema_version"] == 5
    assert upgraded["builtin_format_overrides"] == {"docx": {"extensions": [".word"]}}
    assert upgraded["format_families"][0]["preset_baseline"] == {
        "preset_id": "document",
        "preset_revision": 1,
        "format_ids": ["docx", "xlsx"],
        "format_extensions": {
            "docx": [".docx"],
            "xlsx": [".xlsx"],
        },
    }
    assert policy_migration._upgrade_policy(upgraded) == upgraded


def test_consolidated_migration_catalog_matches_runtime_package_aliases():
    assert policy_migration._BUILT_IN_DEFAULT_EXTENSIONS == {
        descriptor.format_id: list(descriptor.extensions)
        for descriptor in DEFAULT_FORMATS
    }


def test_active_source_builtins_survive_policy_upgrade_without_custom_aliases():
    upgraded = policy_migration._upgrade_policy(
        {
            "policy_schema_version": 4,
            "custom_formats": [
                {
                    "format_id": "custom-active",
                    "display_name": "Active source",
                    "extensions": [".html"],
                }
            ],
            "format_families": [
                {
                    "family_id": "source",
                    "name": "Source",
                    "origin": "custom",
                    "format_ids": ["html", "javascript"],
                    "max_upload_bytes": None,
                }
            ],
            "format_overrides": {
                "html": 3_000_000,
                "javascript": 4_000_000,
            },
        }
    )

    assert upgraded["custom_formats"] == []
    assert upgraded["format_families"][0]["format_ids"] == [
        "html",
        "javascript",
    ]
    assert upgraded["format_overrides"] == {
        "html": 3_000_000,
        "javascript": 4_000_000,
    }


def test_alembic_upgrade_sanitizes_an_existing_v3_workspace_row(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'policy-v3.db'}"
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "bd62193d5cf6")

    engine = create_engine(database_url)
    try:
        _run_steps(
            engine,
            epic1._upgrade_base_schema,
            workspace_phase.upgrade,
            mcp_phase.upgrade,
            public_phase.upgrade,
            media_phase.upgrade,
        )
        workspace_id = uuid4()
        workspaces = sa.Table("workspace", sa.MetaData(), autoload_with=engine)
        assert "workspace_graph_enabled" not in workspaces.c
        now = datetime.now(UTC)
        with engine.begin() as connection:
            # Seed only this frozen phase boundary's columns. Current Workspace
            # inserts also include Graph View fields introduced by a later revision.
            connection.execute(
                workspaces.insert().values(
                    id=workspace_id.hex,
                    slug="policy-migration",
                    title="Policy migration",
                    directory_title_key="policy migration",
                    name="Policy migration",
                    goal="Migration coverage",
                    default_language="en",
                    library_policy=_v3_custom_policy(),
                    write_policy=default_write_policy(),
                    citation_policy=default_citation_policy(),
                    agent_injection_policy=default_agent_injection_policy(),
                    review_policy=default_review_policy(),
                    lint_policy={},
                    auto_fix_enabled=False,
                    visibility="private",
                    created_by_actor_id=default_actor_id().hex,
                    owner_actor_id=default_actor_id().hex,
                    updated_at=now,
                    created_at=now,
                )
            )
        _run_steps(engine, policy_migration.upgrade)
        with engine.connect() as connection:
            persisted = connection.scalar(
                sa.select(workspaces.c.library_policy).where(
                    workspaces.c.id == workspace_id.hex
                )
            )
            assert persisted == policy_migration._upgrade_policy(_v3_custom_policy())
    finally:
        engine.dispose()
