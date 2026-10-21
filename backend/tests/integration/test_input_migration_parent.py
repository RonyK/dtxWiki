"""One round trip per dialect covers parent-policy preservation and defaults.

Budget: two collected cases (SQLite/PostgreSQL), with every stage asserted in
one traversal. Chain structure is already checked by test_migration_chain.py;
scalar expressions belong in unit tests. Do not add per-column/stage cases.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.ops.input_maintenance import audit_inputs, repair_input

pytestmark = pytest.mark.xdist_group("postgres")

CORE = "efcbcf724889"
RETENTION = "d148b9746ff5"
INPUTS = "19db79d3e622"


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


def test_retention_policy_survives_input_restrictions_round_trip(migration_database):
    url, engine, config = migration_database
    command.upgrade(config, "bd62193d5cf6")
    assert audit_inputs(url) == []
    command.upgrade(config, CORE)
    assert "context_media_policy" not in _workspace_table(engine).c
    identity, key = _insert_workspace(engine, "retained-workspace", title="x" * 129)

    command.upgrade(config, RETENTION)
    assert _stamp(engine) == RETENTION
    assert _row(engine, key)["context_media_policy"] == {}
    policy = {"retention_days": 37, "preserved_label": "보존 😀"}
    table = _workspace_table(engine)
    with engine.begin() as connection:
        connection.execute(
            table.update().where(table.c.id == key).values(context_media_policy=policy)
        )
    before_audit = _row(engine, key)
    findings = {finding["rule"]: finding for finding in audit_inputs(url)}
    for field in ("title", "name"):
        assert findings[f"ck_workspace_{field}_input_v1"]["count"] == 1
    assert _stamp(engine) == RETENTION
    assert _row(engine, key) == before_audit

    repair_input(url, "workspace", identity, "title", "Retained workspace")
    assert _stamp(engine) == RETENTION
    assert audit_inputs(url) == []
    expected = _row(engine, key)
    assert expected["title"] == expected["name"] == "Retained workspace"
    assert expected["context_media_policy"] == policy
    defaults = [_assert_policy_column_and_server_default(engine, "retention")]

    for destination, direction, stage in (
        ("head", command.upgrade, "inputs"),
        (RETENTION, command.downgrade, "parent-again"),
        ("head", command.upgrade, "inputs-again"),
    ):
        direction(config, destination)
        assert _stamp(engine) == (RETENTION if destination == RETENTION else INPUTS)
        assert _row(engine, key) == expected
        for default_key in defaults:
            assert _row(engine, default_key)["context_media_policy"] == {}
        defaults.append(_assert_policy_column_and_server_default(engine, stage))
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                assert (
                    connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                )

    command.downgrade(config, CORE)
    assert _stamp(engine) == CORE
    assert "context_media_policy" not in _workspace_table(engine).c
    expected.pop("context_media_policy")
    assert _row(engine, key) == expected
    assert len(defaults) == 4
    for default_key in defaults:
        assert _row(engine, default_key)["title"] == "Migration workspace"
