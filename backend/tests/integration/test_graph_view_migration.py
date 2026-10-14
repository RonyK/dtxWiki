"""Graph View extends the shipped core without replaying or rewriting it."""

import os
import shutil
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.bootstrap import resolve_migrations_dir
from backend.tests.integration.test_release_schema_migration import (
    _config,
    _insert_initial_release_page_link_source,
    _insert_initial_release_workspace,
    _schema_snapshot,
)
from backend.tests.utils.migrated_sqlite_templates import shared_templates

CORE_REVISION = "efcbcf724889"
GRAPH_REVISION = "9aeab6944f25"
GRAPH_FILE = "9aeab6944f25_add_graph_view.py"


def test_populated_released_core_upgrades_and_downgrades_without_data_loss(
    tmp_path, tmp_path_factory
):
    templates = shared_templates(tmp_path_factory)
    database_url = templates.clone("base_0001", tmp_path / "released-core.db")
    config = _config(database_url)
    workspace_id, page_id, version_id, link_id, edge_id = (uuid4() for _ in range(5))
    now = datetime(2026, 10, 14, tzinfo=UTC)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_initial_release_workspace(
                connection, workspace_id=workspace_id, slug="workspace-graph-migration"
            )
            _insert_initial_release_page_link_source(
                connection,
                workspace_id=workspace_id,
                page_id=page_id,
                version_id=version_id,
            )
            connection.execute(
                text(
                    "INSERT INTO page_links "
                    "(workspace_id, source_page_id, source_version_id, target_slug, "
                    "occurrence_count, first_snippet, id, created_at) "
                    "VALUES (:workspace, :page, :version, 'existing-target', "
                    "2, 'released wikilink', :id, :now)"
                ),
                {
                    "workspace": workspace_id.hex,
                    "page": page_id.hex,
                    "version": version_id.hex,
                    "id": link_id.hex,
                    "now": now,
                },
            )
        command.upgrade(config, CORE_REVISION)
        # Reflect the actual released schema; current ORM defaults name graph
        # columns which deliberately do not exist at this release boundary.
        edges = Table("edge_projection", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                edges.insert().values(
                    workspace_id=workspace_id.hex,
                    source_kind="context_page",
                    source_id=page_id.hex,
                    target_kind="context_page",
                    target_id=page_id.hex,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=2.0,
                    source_ref={"released": True},
                    last_seen_at=now,
                    occurred_at=now,
                    id=edge_id.hex,
                    created_at=now,
                )
            )
            before_data = {
                table: connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")
                ).all()
                for table in ("workspace", "page_links", "edge_projection")
            }
        before_schema = _schema_snapshot(engine)
        assert "graph_observation" not in before_schema

        command.upgrade(config, GRAPH_REVISION)
        with engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == GRAPH_REVISION
            )
            assert connection.execute(
                text(
                    "SELECT graph_observation_sequence, workspace_graph_enabled, "
                    "usage_relation_signals_enabled FROM workspace WHERE id = :id"
                ),
                {"id": workspace_id.hex},
            ).one() == (0, 0, 1)
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM graph_observation")) == 0
            )
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM graph_history_coverage"))
                == 0
            )
            assert connection.execute(
                text(
                    "SELECT support_session_count, first_observed_at "
                    "FROM edge_projection"
                )
            ).one() == (None, None)
            links = Table("page_links", MetaData(), autoload_with=connection)
            old_link = (
                connection.execute(select(links).where(links.c.id == link_id.hex))
                .mappings()
                .one()
            )
            assert old_link["link_syntax"] == "wikilink"
            assert old_link["first_snippet"] == "released wikilink"
            # The two syntaxes can coexist for the same source/version/target.
            url_link = dict(old_link)
            url_link.update(
                id=uuid4().hex,
                link_syntax="context_url",
                first_snippet="new URL projection",
            )
            connection.execute(links.insert().values(**url_link))
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                duplicate = dict(old_link)
                duplicate["id"] = uuid4().hex
                connection.execute(links.insert().values(**duplicate))
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        command.downgrade(config, CORE_REVISION)
        assert _schema_snapshot(engine) == before_schema
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == CORE_REVISION
            )
            after_data = {
                table: connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")
                ).all()
                for table in before_data
            }
            assert after_data == before_data
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        # Reapplying Graph View after downgrade reconstructs the same schema.
        command.upgrade(config, GRAPH_REVISION)
        assert "graph_observation" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_graph_revision_failure_rolls_back_to_released_core(tmp_path, tmp_path_factory):
    templates = shared_templates(tmp_path_factory)
    database_url = templates.clone("rev_efcbcf724889", tmp_path / "failed-graph.db")
    scripts = tmp_path / "migrations"
    shutil.copytree(
        resolve_migrations_dir(), scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    graph = scripts / "versions" / "v0_1" / GRAPH_FILE
    graph.write_text(
        graph.read_text()
        + "\n_original_upgrade = upgrade\n"
        + "def upgrade():\n"
        + "    _original_upgrade()\n"
        + '    raise RuntimeError("late graph migration failure")\n'
    )
    config = _config(database_url)
    config.set_main_option("script_location", str(scripts))
    config.set_main_option("version_locations", str(scripts / "versions"))
    engine = create_engine(database_url)
    try:
        before = _schema_snapshot(engine)
        with pytest.raises(RuntimeError, match="late graph migration failure"):
            command.upgrade(config, GRAPH_REVISION)
        assert _schema_snapshot(engine) == before
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == CORE_REVISION
            )
    finally:
        engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    not os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL", "").startswith(
        "postgresql"
    ),
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_postgres_released_core_graph_upgrade_guards_and_downgrade():
    database_url = os.environ["DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL"]
    config = _config(database_url)
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    assert tables <= {"alembic_version"}, "requires an empty dedicated database"
    if "alembic_version" in tables:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM alembic_version")) == 0
    upgraded = False
    try:
        command.upgrade(config, CORE_REVISION)
        upgraded = True
        workspace_id, actor_id = uuid4(), uuid4()
        now = datetime(2026, 10, 14, tzinfo=UTC)
        workspaces = Table("workspace", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                workspaces.insert().values(
                    id=workspace_id,
                    slug="postgres-graph-migration",
                    title="Postgres Graph migration",
                    directory_title_key="postgres graph migration",
                    name="Postgres Graph migration",
                    goal="Verify released core upgrade",
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
                    updated_at=now,
                    created_at=now,
                )
            )
            before_workspace = connection.execute(select(workspaces)).one()
        before_schema = _schema_snapshot(engine)
        command.upgrade(config, GRAPH_REVISION)
        graph = Table("graph_observation", MetaData(), autoload_with=engine)
        observation_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                graph.insert().values(
                    id=observation_id,
                    workspace_id=workspace_id,
                    operation="node_created",
                    workspace_sequence=1,
                    source_kind="context_page",
                    source_id=uuid4(),
                    occurred_at=now,
                    recorded_at=now,
                    idempotency_key="migration-test",
                )
            )
        for statement in (
            graph.update().values(action_kind="edited"),
            graph.delete(),
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                with engine.begin() as connection:
                    connection.execute(statement)
        # The database trigger must admit only the parent workspace cascade.
        cascade_workspace_id = uuid4()
        with engine.begin() as connection:
            cascade_workspace = dict(before_workspace._mapping)
            cascade_workspace.update(
                id=cascade_workspace_id, slug="postgres-graph-cascade"
            )
            connection.execute(workspaces.insert().values(**cascade_workspace))
            connection.execute(
                graph.insert().values(
                    id=uuid4(),
                    workspace_id=cascade_workspace_id,
                    operation="node_created",
                    workspace_sequence=1,
                    source_kind="context_page",
                    source_id=uuid4(),
                    occurred_at=now,
                    recorded_at=now,
                    idempotency_key="cascade-test",
                )
            )
            connection.execute(
                workspaces.delete().where(workspaces.c.id == cascade_workspace_id)
            )
            assert (
                connection.scalar(
                    select(sa.func.count())
                    .select_from(graph)
                    .where(graph.c.workspace_id == cascade_workspace_id)
                )
                == 0
            )

        command.downgrade(config, CORE_REVISION)
        assert _schema_snapshot(engine) == before_schema
        with engine.connect() as connection:
            assert connection.execute(select(workspaces)).one() == before_workspace
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == CORE_REVISION
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM pg_proc WHERE proname = "
                        "'dtxwiki_reject_graph_observation_mutation'"
                    )
                )
                == 0
            )
    finally:
        if upgraded:
            command.downgrade(config, "base")
        engine.dispose()
