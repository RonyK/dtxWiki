"""One round trip per dialect covers the consolidated 0.1.3 schema.

Budget: two collected cases (SQLite/PostgreSQL), with every stage asserted in
one traversal. Chain structure is already checked by test_migration_chain.py;
scalar expressions belong in unit tests. Do not add per-column/stage cases.
"""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import RULES
from backend.models.agent import AgentPage
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.favorite import UserFavorite
from backend.models.library import LibraryItem
from backend.services.context_media_policy import resolve_context_media_policy
from backend.services.ops.input_maintenance import audit_inputs, repair_input

pytestmark = pytest.mark.xdist_group("postgres")

CORE = "efcbcf724889"
INPUTS = "19db79d3e622"
CONTENT_TABLES = ("context_page", "agent_page", "library_item")


def _config(url):
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _postgres_url():
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                _postgres_url() is None,
                reason="requires a dedicated PostgreSQL migration test database",
            ),
        ),
    ]
)
def migration_database(request, tmp_path):
    url = (
        f"sqlite:///{tmp_path / 'migration-parent.sqlite'}"
        if request.param == "sqlite"
        else _postgres_url()
    )
    assert url is not None
    engine = sa.create_engine(url)
    # Never clean or migrate a populated external database. An empty Alembic
    # bookkeeping table left by another base downgrade contains no app data.
    initial_tables = set(sa.inspect(engine).get_table_names())
    assert initial_tables <= {"alembic_version"}, "requires an empty dedicated database"
    if "alembic_version" in initial_tables:
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM alembic_version")) == 0
            )
    config = _config(url)
    try:
        yield url, engine, config
    finally:
        try:
            command.downgrade(config, "base")
            remaining = set(sa.inspect(engine).get_table_names())
            assert remaining <= {"alembic_version"}
            if "alembic_version" in remaining:
                with engine.begin() as connection:
                    version = sa.Table(
                        "alembic_version", sa.MetaData(), autoload_with=connection
                    )
                    assert (
                        connection.scalar(
                            sa.select(sa.func.count()).select_from(version)
                        )
                        == 0
                    )
                    version.drop(connection)
        finally:
            engine.dispose()


def _workspace_table(engine):
    return sa.Table("workspace", sa.MetaData(), autoload_with=engine, resolve_fks=False)


def _insert_workspace(engine, slug, *, title="Migration workspace"):
    table = _workspace_table(engine)
    identity = uuid4()
    actor = uuid4()
    key = identity.hex if engine.dialect.name == "sqlite" else identity
    actor_key = actor.hex if engine.dialect.name == "sqlite" else actor
    now = datetime.now(UTC)
    with engine.begin() as connection:
        # Reflected columns have no ORM/Python defaults. In particular this
        # INSERT deliberately omits context_media_policy to exercise SQL's {}.
        connection.execute(
            table.insert().values(
                id=key,
                slug=slug,
                title=title,
                name=title,
                directory_title_key=slug,
                description="Preserve the complete workspace row",
                goal="Migration parent regression",
                default_language="ko",
                library_policy={},
                write_policy={},
                citation_policy={},
                agent_injection_policy={},
                review_policy={},
                lint_policy={},
                auto_fix_enabled=False,
                visibility="private",
                join_policy="invite_only",
                created_by_actor_id=actor_key,
                owner_actor_id=actor_key,
                owner_user_id=None,
                created_at=now,
                updated_at=now,
            )
        )
    return identity, key


def _row(engine, key):
    table = _workspace_table(engine)
    with engine.connect() as connection:
        return dict(
            connection.execute(sa.select(table).where(table.c.id == key))
            .mappings()
            .one()
        )


def _stamp(engine):
    with engine.connect() as connection:
        return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


def _assert_policy_column_and_server_default(engine, stage):
    column = next(
        column
        for column in sa.inspect(engine).get_columns("workspace")
        if column["name"] == "context_media_policy"
    )
    assert column["nullable"] is False
    assert isinstance(column["type"], sa.JSON)
    assert column["default"] is not None and "'{}'" in column["default"]
    assert _workspace_table(engine).c.context_media_policy.default is None
    _, key = _insert_workspace(engine, f"server-default-{stage}")
    assert _row(engine, key)["context_media_policy"] == {}
    table = _workspace_table(engine)
    with pytest.raises(sa.exc.DBAPIError) as rejected, engine.begin() as connection:
        connection.execute(
            table.update()
            .where(table.c.id == key)
            .values(context_media_policy=sa.null())
        )
    if engine.dialect.name == "postgresql":
        assert rejected.value.orig.args[0]["C"] == "23502"
    else:
        assert rejected.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"
    assert _row(engine, key)["context_media_policy"] == {}
    return key


def _seed_existing_content(engine, workspace_id):
    """Insert existing rows without the four columns added by 0.1.3."""
    context_id, version_id, agent_id, library_id = (uuid4() for _ in range(4))
    with engine.begin() as connection:
        connection.execute(
            ContextPage.__table__.insert().values(
                id=context_id,
                workspace_id=workspace_id,
                title="Preserved context",
                slug="preserved-context",
            )
        )
        connection.execute(
            ContextPageVersion.__table__.insert().values(
                id=version_id,
                workspace_id=workspace_id,
                page_id=context_id,
                body_md="# Preserved context\n\nExisting draft body.",
                content_hash="preserved",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="migration-test",
            )
        )
        connection.execute(
            ContextPage.__table__.update()
            .where(ContextPage.id == context_id)
            .values(latest_draft_version_id=version_id)
        )
        connection.execute(
            AgentPage.__table__.insert().values(
                id=agent_id,
                workspace_id=workspace_id,
                title="Preserved agent",
                slug="preserved-agent",
                category="skill",
            )
        )
        connection.execute(
            LibraryItem.__table__.insert().values(
                id=library_id,
                workspace_id=workspace_id,
                connector_type="web",
                uri="https://example.com/preserved",
                title="Preserved library item",
            )
        )
    return context_id


def _existing_rows(engine):
    rows = {}
    with engine.connect() as connection:
        for name in (
            "context_page",
            "context_page_version",
            "agent_page",
            "library_item",
        ):
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            rows[name] = [
                dict(row) for row in connection.execute(sa.select(table)).mappings()
            ]
    return rows


def _schema_snapshot(engine):
    inspector = sa.inspect(engine)
    schema = {}
    tables = {rule.table for rule in RULES} | {"context_page_version"}
    if inspector.has_table("user_favorite"):
        tables.add("user_favorite")
    for name in sorted(tables):
        # Catalog order is not meaningful; retain all reflected properties,
        # including partial-index predicates and reciprocal version FKs.
        schema[name] = {
            "columns": sorted(
                inspector.get_columns(name), key=lambda item: item["name"]
            ),
            "primary_key": inspector.get_pk_constraint(name),
            **{
                kind: sorted(
                    read(name),
                    key=lambda item: json.dumps(item, sort_keys=True, default=str),
                )
                for kind, read in (
                    ("indexes", inspector.get_indexes),
                    ("foreign_keys", inspector.get_foreign_keys),
                    ("unique_constraints", inspector.get_unique_constraints),
                    ("checks", inspector.get_check_constraints),
                )
            },
        }
    return json.dumps(schema, sort_keys=True, default=str)


def test_consolidated_release_schema_round_trip(migration_database):
    url, engine, config = migration_database
    command.upgrade(config, "bd62193d5cf6")
    assert audit_inputs(url) == []
    command.upgrade(config, CORE)
    assert "context_media_policy" not in _workspace_table(engine).c
    assert not sa.inspect(engine).has_table("user_favorite")
    for name in CONTENT_TABLES:
        assert "content_changed_at" not in {
            column["name"] for column in sa.inspect(engine).get_columns(name)
        }
    identity, key = _insert_workspace(engine, "retained-workspace", title="x" * 129)
    before_audit = _row(engine, key)
    findings = {finding["rule"]: finding for finding in audit_inputs(url)}
    for field in ("title", "name"):
        assert findings[f"ck_workspace_{field}_input_v1"]["count"] == 1
    assert _stamp(engine) == CORE
    assert _row(engine, key) == before_audit
    repair_input(url, "workspace", identity, "title", "Retained workspace")
    assert _stamp(engine) == CORE
    assert audit_inputs(url) == []
    expected = _row(engine, key)
    assert expected["title"] == expected["name"] == "Retained workspace"
    context_id = _seed_existing_content(engine, identity)
    existing_content = _existing_rows(engine)
    core_schema = _schema_snapshot(engine)
    defaults = []
    head_schema = None

    for stage in ("first", "again"):
        command.upgrade(config, INPUTS)
        assert _stamp(engine) == INPUTS
        assert _row(engine, key) == {**expected, "context_media_policy": {}}
        actual_content = _existing_rows(engine)
        for name in CONTENT_TABLES:
            column = next(
                column
                for column in sa.inspect(engine).get_columns(name)
                if column["name"] == "content_changed_at"
            )
            assert isinstance(column["type"], sa.DateTime)
            assert column["nullable"] is True
            assert column["default"] is None
            assert all(
                row["content_changed_at"] is None for row in actual_content[name]
            )
            for row in actual_content[name]:
                row.pop("content_changed_at")
        # No publication evidence was seeded, so timestamps remain unknown;
        # all original row fields, including version pointers, are unchanged.
        assert actual_content == existing_content
        assert audit_inputs(url) == []
        for name in {rule.table for rule in RULES}:
            checks = {
                constraint["name"]
                for constraint in sa.inspect(engine).get_check_constraints(name)
            }
            assert {rule.name for rule in RULES if rule.table == name} <= checks
        table = _workspace_table(engine)
        with pytest.raises(sa.exc.DBAPIError) as rejected, engine.begin() as connection:
            connection.execute(
                table.update().where(table.c.id == key).values(title="x" * 129)
            )
        if engine.dialect.name == "postgresql":
            assert rejected.value.orig.args[0]["C"] == "23514"
        else:
            assert rejected.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
        assert _row(engine, key)["title"] == expected["title"]
        # Existing workspaces keep server inheritance, including a zero-hour
        # default. This folds the former standalone retention migration case.
        factory = sessionmaker(engine)
        # The Workspace model can be ahead of this intermediate revision, so
        # resolve the stored policy from the row instead of loading the model.
        stored = SimpleNamespace(
            context_media_policy=_row(engine, key)["context_media_policy"]
        )
        for default in (168, 72, 36, 12, 0):
            policy = resolve_context_media_policy(stored, default)
            assert policy.override_hours is None
            assert policy.effective_hours == default
        defaults.append(_assert_policy_column_and_server_default(engine, stage))
        for default_key in defaults:
            assert _row(engine, default_key)["context_media_policy"] == {}

        favorites = sa.Table("user_favorite", sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(favorites))
                == 0
            )
        favorite = dict(
            workspace_id=identity,
            user_id=uuid4(),
            target_kind="context_page",
            target_id=context_id,
            created_at=datetime(2026, 10, 20, 12, 34, 56, tzinfo=UTC),
        )
        with engine.begin() as connection:
            connection.execute(UserFavorite.__table__.insert().values(**favorite))
        with factory() as session:
            saved = session.get(
                UserFavorite,
                tuple(
                    favorite[name]
                    for name in (
                        "user_id",
                        "workspace_id",
                        "target_kind",
                        "target_id",
                    )
                ),
            )
            assert saved is not None
            assert saved.created_at == favorite["created_at"]
        retention = {"orphan_retention_hours": 17, "preserved_label": "보존 😀"}
        with engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.id == key)
                .values(context_media_policy=retention)
            )
        assert _row(engine, key) == {**expected, "context_media_policy": retention}
        current_schema = _schema_snapshot(engine)
        if head_schema is None:
            head_schema = current_schema
        else:
            assert current_schema == head_schema
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                assert (
                    connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
                    == "ok"
                )
                assert (
                    connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                )

        if stage == "first":
            command.downgrade(config, CORE)
            assert _stamp(engine) == CORE
            assert _schema_snapshot(engine) == core_schema
            assert _row(engine, key) == expected
            assert _existing_rows(engine) == existing_content
            for default_key in defaults:
                assert _row(engine, default_key)["title"] == "Migration workspace"
