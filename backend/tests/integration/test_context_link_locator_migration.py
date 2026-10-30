"""The locator migration's one traversal per supported database dialect."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.tests.integration.test_release_schema_migration import (
    _config,
)
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT_REVISION = "175b9664724e"
LOCATOR_REVISION = "af62c98d0731"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_context_link_locator_round_trip_preserves_wikilink_pins(
    dialect, tmp_path, tmp_path_factory
):
    # Migration execution and each dialect's real null/unique constraints cannot
    # be proved by a unit test. One traversal combines data backfill, nullable
    # slug, duplicate rejection, downgrade collision handling, and re-upgrade.
    if dialect == "sqlite":
        database_url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "link-locators.sqlite"
        )
    else:
        database_url = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
        if not database_url or not database_url.startswith("postgresql"):
            pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(database_url)
    config = _config(database_url)
    migrated = False

    def link_table():
        # SQLite reflection returns CHAR for UUIDs; keep only the bind/result
        # adapter explicit while reflecting the revision's real column set.
        table = sa.Table("page_links", sa.MetaData(), autoload_with=engine)
        for name in (
            "id",
            "workspace_id",
            "source_page_id",
            "source_version_id",
            "target_page_id",
        ):
            table.c[name].type = sa.Uuid()
        return table

    try:
        if dialect == "postgresql":
            assert not (
                set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
            ), "PostgreSQL migration tests require an empty, dedicated database"
        command.upgrade(config, PARENT_REVISION)
        migrated = True
        workspace_id = uuid4()
        with engine.begin() as connection:
            workspaces = sa.Table("workspace", sa.MetaData(), autoload_with=engine)
            for name in ("id", "created_by_actor_id", "owner_actor_id"):
                workspaces.c[name].type = sa.Uuid()
            actor_id = uuid4()
            connection.execute(
                workspaces.insert().values(
                    id=workspace_id,
                    slug="locator-migration",
                    title="Locator migration",
                    directory_title_key="locator migration",
                    name="Locator migration",
                    goal="Verify permanent pins",
                    default_language="en",
                    library_policy={},
                    write_policy={},
                    citation_policy={},
                    agent_injection_policy={},
                    review_policy={},
                    lint_policy={},
                    auto_fix_enabled=False,
                    visibility="private",
                    created_by_actor_id=actor_id,
                    owner_actor_id=actor_id,
                    updated_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            )
        with Session(engine) as session, session.begin():
            source = ContextPage(
                workspace_id=workspace_id, slug="source", title="Source"
            )
            target = ContextPage(
                workspace_id=workspace_id, slug="target", title="Target"
            )
            session.add_all([source, target])
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=source.id,
                version_no=1,
                body_md="# Source\n\n[[target]]",
                content_hash="a" * 64,
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="migration-test",
                version_status="published_snapshot",
            )
            session.add(version)
            session.flush()
            workspace_id, source_id, target_id, version_id = (
                workspace_id,
                source.id,
                target.id,
                version.id,
            )
        links = link_table()
        legacy_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                links.insert().values(
                    id=legacy_id,
                    workspace_id=workspace_id,
                    source_page_id=source_id,
                    source_version_id=version_id,
                    target_slug="target",
                    target_page_id=target_id,
                    occurrence_count=2,
                    first_snippet="Original snippet",
                    created_at=sa.func.current_timestamp(),
                )
            )

        command.upgrade(config, LOCATOR_REVISION)
        links = link_table()
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns("page_links")}
        assert columns["locator_key"]["nullable"] is False
        assert columns["target_slug"]["nullable"] is True
        assert any(
            constraint["column_names"]
            == ["source_page_id", "source_version_id", "locator_key"]
            for constraint in sa.inspect(engine).get_unique_constraints("page_links")
        )
        with engine.begin() as connection:
            row = connection.execute(sa.select(links)).mappings().one()
            assert row["locator_key"] == "wikilink:target"
            assert row["target_page_id"] == target_id
            assert row["id"] == legacy_id
            assert row["occurrence_count"] == 2
            assert row["first_snippet"] == "Original snippet"
            base = dict(
                workspace_id=workspace_id,
                source_page_id=source_id,
                source_version_id=version_id,
                target_page_id=target_id,
                occurrence_count=1,
                created_at=datetime.now(UTC),
            )
            connection.execute(
                links.insert(),
                [
                    dict(
                        base,
                        id=uuid4(),
                        target_slug="target",
                        locator_key="concept_path:target",
                    ),
                    dict(
                        base,
                        id=uuid4(),
                        target_slug=None,
                        locator_key=f"page_id:{target_id}",
                    ),
                ],
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    links.insert().values(
                        **base,
                        id=uuid4(),
                        target_slug=None,
                        locator_key="wikilink:target",
                    )
                )
            with pytest.raises(DBAPIError) as not_null_error, connection.begin_nested():
                connection.execute(
                    links.insert().values(
                        **base, id=uuid4(), target_slug=None, locator_key=None
                    )
                )
            if dialect == "postgresql":
                # pg8000 maps NOT NULL (23502) to ProgrammingError, unlike
                # UNIQUE (23505). Prove the actual constraint, not its class.
                assert not_null_error.value.orig.args[0]["C"] == "23502"
                assert not_null_error.value.orig.args[0]["c"] == "locator_key"
            else:
                assert isinstance(not_null_error.value, IntegrityError)
                assert "page_links.locator_key" in str(not_null_error.value)

        command.downgrade(config, PARENT_REVISION)
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns("page_links")}
        assert "locator_key" not in columns
        assert columns["target_slug"]["nullable"] is False
        links = link_table()
        with engine.connect() as connection:
            row = connection.execute(sa.select(links)).mappings().one()
            assert row["id"] == legacy_id
            assert row["target_page_id"] == target_id
        command.upgrade(config, LOCATOR_REVISION)
        links = link_table()
        with engine.connect() as connection:
            row = connection.execute(sa.select(links)).mappings().one()
            assert row["id"] == legacy_id
            assert row["locator_key"] == "wikilink:target"
            assert row["target_page_id"] == target_id

        # The locator-first branch already supports durable permanent URL
        # pins. Adding/removing Graph must preserve those pins independently
        # of a target's mutable slug, as well as classifying their syntax.
        permanent_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                links.insert().values(
                    id=permanent_id,
                    workspace_id=workspace_id,
                    source_page_id=source_id,
                    source_version_id=version_id,
                    target_slug=None,
                    target_page_id=target_id,
                    locator_key=f"page_id:{target_id}",
                    occurrence_count=1,
                    first_snippet="Permanent URL pin",
                    created_at=datetime.now(UTC),
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            merged = link_table()
            rows = {
                row["id"]: row
                for row in connection.execute(sa.select(merged)).mappings()
            }
            assert rows[legacy_id]["locator_key"] == "wikilink:target"
            assert rows[legacy_id]["link_syntax"] == "wikilink"
            assert rows[legacy_id]["target_page_id"] == target_id
            assert rows[permanent_id]["locator_key"] == f"page_id:{target_id}"
            assert rows[permanent_id]["link_syntax"] == "context_url"
            assert rows[permanent_id]["target_page_id"] == target_id
        # Graph is appended after the upstream locator revision, so its
        # downgrade must preserve all prior pins and locator constraints.
        command.downgrade(config, LOCATOR_REVISION)
        with engine.connect() as connection:
            upstream = link_table()
            rows = {
                row["id"]: row
                for row in connection.execute(sa.select(upstream)).mappings()
            }
            assert set(rows) == {legacy_id, permanent_id}
            assert rows[permanent_id]["locator_key"] == f"page_id:{target_id}"
            assert rows[permanent_id]["target_page_id"] == target_id
            assert rows[permanent_id]["first_snippet"] == "Permanent URL pin"
        command.upgrade(config, "head")
        with engine.connect() as connection:
            merged = link_table()
            rows = {
                row["id"]: row
                for row in connection.execute(sa.select(merged)).mappings()
            }
            assert rows[legacy_id]["locator_key"] == "wikilink:target"
            assert rows[legacy_id]["link_syntax"] == "wikilink"
            assert rows[legacy_id]["target_page_id"] == target_id
            assert rows[permanent_id]["locator_key"] == f"page_id:{target_id}"
            assert rows[permanent_id]["link_syntax"] == "context_url"
            assert rows[permanent_id]["target_page_id"] == target_id
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
        finally:
            engine.dispose()
