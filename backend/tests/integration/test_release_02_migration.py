"""The v0.2 release migration: two traversals per dialect, four collected cases.

* Published 0.1.4 (eb86fdacb1bb) -> 0.2 -> 0.1.4 -> 0.2 on one populated
  database: backfills, schema contracts, retained 0.1.4 login/deletion
  evidence, the parent stamp/schema guard, late-DDL rollback in both
  directions and the startup upgrade from 0.1.4. Those fault inputs and one
  locator duplicate write share the traversal; none adds a database scenario.
* A fresh installation initialized directly at 0.2, then the same populated
  database started from 0.1.2 and 0.1.3. The first traversal starts after the
  0.1.3 input constraints and publication-time backfill, and only this one
  compares the frozen initialization DDL with the real release upgrades.

The first traversal also upgrades, downgrades and re-upgrades the consolidated
0.2.3 revision once, checking the Labs policy's defaults, values and NULL
constraint and both archived Inbox indexes without adding a collected case.
Fresh startup applies that revision after the frozen release initializer;
the frozen snapshots remain unchanged.

Scalar validation matrices belong to existing policy tests rather than this
release migration.
"""

import os
import shutil
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import DBAPIError

from backend.app.bootstrap import (
    current_alembic_head,
    ensure_app_version_synced,
    resolve_alembic_ini,
    resolve_migrations_dir,
)
from backend.app.db import make_session_factory
from backend.app.settings import Settings
from backend.models import Base
from backend.services.ops.alembic_migration_graph import initialization_script_directory
from backend.services.ops.input_maintenance import audit_inputs
from backend.services.page_service import PageService
from backend.tests.integration.test_release_schema_migration import (
    _assert_each_table_rebuilt_at_most_once,
    _captured_statements,
    _config,
    _schema_snapshot,
)
from backend.tests.utils.migration_paths import configure_test_repositories

# Published 0.1.4. The 0.2.1 package used a development-only parent instead.
PARENT = "eb86fdacb1bb"
REVISION = "5b7e5c149279"
RELEASE_023_REVISION = "00ba02774c4a"


def _migration_engine(tmp_path, dialect, filename):
    database_url = (
        f"sqlite+pysqlite:///{tmp_path / filename}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not database_url or not database_url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = create_engine(database_url)
    if dialect == "sqlite":

        @event.listens_for(engine, "connect")
        def _foreign_keys(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    assert not (set(inspect(engine).get_table_names()) - {"alembic_version"}), (
        "migration test database must be empty"
    )
    if "alembic_version" in inspect(engine).get_table_names():
        with engine.connect() as connection:
            assert not connection.scalar(
                sa.text("SELECT count(*) FROM alembic_version")
            )
    return database_url, engine


def _stamp(engine):
    with engine.connect() as connection:
        return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_release_02_upgrade_downgrade_reupgrade(tmp_path, dialect):
    """One populated traversal per dialect from the published 0.1.4 boundary."""

    database_url, engine = _migration_engine(tmp_path, dialect, "release-02.db")
    config = _config(database_url)
    now = datetime.now(UTC)
    existing_id, external_id, workspace_id = uuid4(), uuid4(), uuid4()
    accounts = (existing_id, external_id)

    def retained_account_state():
        # 0.1.4 owns credentials, linked identities, login transactions and
        # deletion evidence. Neither direction of this revision may touch them.
        with engine.connect() as connection:
            users = _table(connection, "users")
            workspaces = _table(connection, "workspace")
            memberships = _table(connection, "workspace_memberships")
            identities = _table(connection, "user_identities")
            transactions = _table(connection, "auth_login_transactions")
            outbox = _table(connection, "email_outbox")
            return (
                connection.execute(
                    sa.select(
                        users.c.id,
                        users.c.email,
                        users.c.password_hash,
                        users.c.default_workspace_id,
                        users.c.status,
                        users.c.deletion_requested_at,
                        users.c.purge_after,
                        users.c.deletion_retention_days,
                        users.c.deletion_last_error,
                    )
                    .where(users.c.id.in_(accounts))
                    .order_by(users.c.id)
                ).all(),
                connection.execute(
                    sa.select(
                        workspaces.c.id,
                        workspaces.c.slug,
                        workspaces.c.owner_user_id,
                        workspaces.c.account_deletion_user_id,
                    ).order_by(workspaces.c.id)
                ).all(),
                connection.execute(sa.select(memberships)).all(),
                connection.execute(sa.select(identities)).all(),
                connection.execute(sa.select(transactions)).all(),
                connection.execute(
                    sa.select(
                        outbox.c.id,
                        outbox.c.recipient_user_id,
                        outbox.c.message_kind,
                        outbox.c.attempt_count,
                    )
                ).all(),
            )

    upgraded = False
    try:
        # The byte-identical published 0.1.4 migration builds the parent; a
        # current schema is never stamped with a historical identifier.
        command.upgrade(config, PARENT)
        upgraded = True
        with engine.begin() as connection:
            _insert(
                connection,
                "users",
                id=existing_id,
                email="existing@example.com",
                password_hash="preserved-local-hash",
                display_name="Existing User",
                member_search_key="existing user existing@example.com",
                status="active",
            )
            _insert(
                connection,
                "users",
                id=external_id,
                email=None,
                password_hash=None,
                display_name="External User",
                member_search_key="external user",
                status="deletion_pending",
                deletion_requested_at=now,
                purge_after=now + timedelta(days=14),
                deletion_retention_days=14,
                deletion_prior_status="active",
                deletion_failure_count=1,
                deletion_last_error="retained-cleanup-error",
            )
            _insert(
                connection,
                "workspace",
                id=workspace_id,
                slug="retained-workspace",
                title="Retained workspace",
                name="Retained workspace",
                directory_title_key="retained workspace",
                owner_user_id=existing_id,
                account_deletion_user_id=external_id,
                # Legacy authority state before the v0.2 co-access projection.
                authority_projection_version=0,
            )
            users = _table(connection, "users")
            connection.execute(
                users.update()
                .where(users.c.id == existing_id)
                .values(default_workspace_id=workspace_id)
            )
            _insert(
                connection,
                "workspace_memberships",
                user_id=existing_id,
                workspace_id=workspace_id,
                role="owner",
            )
            _insert(
                connection,
                "email_outbox",
                id=uuid4(),
                recipient_user_id=existing_id,
                recipient_email="existing@example.com",
                message_kind="password_changed",
                status="queued",
                attempt_count=2,
                created_at=now,
                updated_at=now,
            )
            _insert(
                connection,
                "user_identities",
                id=uuid4(),
                user_id=external_id,
                provider_slug="company",
                issuer="https://issuer.example",
                subject="retained-external-user",
                linked_via="jit",
            )
            _insert(
                connection,
                "auth_login_transactions",
                id=uuid4(),
                state_digest="retained-state",
                binding_digest="retained-binding",
                provider_slug="company",
                redirect_uri="https://wiki.example/callback",
                purpose="link",
                user_id=external_id,
                session_generation=0,
                session_digest="retained-browser-session",
                return_to="/settings",
                expires_at=now + timedelta(minutes=10),
            )
        retained = retained_account_state()
        parent_schema = _schema_snapshot(engine)
        assert audit_inputs(database_url) == []
        release_data = _seed_release_data(engine, workspace_id, existing_id, now)

        # A stamp alone is not 0.1.4 evidence: refuse before the first DDL.
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX ix_users_purge_after")
        invalid_schema = _schema_snapshot(engine)
        with pytest.raises(RuntimeError, match="published 0.1.4 stamp/schema conflict"):
            command.upgrade(config, REVISION)
        assert _schema_snapshot(engine) == invalid_schema
        assert _stamp(engine) == PARENT
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX ix_users_purge_after ON users (purge_after)"
            )
        assert _schema_snapshot(engine) == parent_schema

        with _captured_statements() as statements:
            command.upgrade(config, REVISION)
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        _assert_release_upgrade(engine, release_data, now, first=True)
        _assert_labs_policy_upgrade(engine, config, workspace_id)
        _assert_archived_inbox_indexes(engine)
        _assert_model_parity(engine)
        # Current runtime uses the current model. Advance to head before its
        # real publication, then traverse the historical downgrade as before.
        command.upgrade(config, "head")
        _seed_runtime_links(engine, release_data)
        command.downgrade(config, REVISION)
        assert "labs_policy" not in {
            column["name"] for column in inspect(engine).get_columns("workspace")
        }
        _assert_archived_inbox_indexes(engine, present=False)
        with engine.connect() as connection:
            workspaces = _table(connection, "workspace")
            assert connection.execute(
                sa.select(
                    workspaces.c.workspace_graph_enabled,
                    workspaces.c.usage_relation_signals_enabled,
                ).where(workspaces.c.id == workspace_id)
            ).one() == (True, False)
        assert retained_account_state() == retained
        assert audit_inputs(database_url) == []

        _assert_atomic_release_failures(config, engine, tmp_path, direction="downgrade")
        with _captured_statements() as statements:
            command.downgrade(config, PARENT)
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        # Exactly the published 0.1.4 schema returns, with its login and
        # deletion evidence: no 0.1.4 downgrade guard is crossed here.
        assert _schema_snapshot(engine) == parent_schema
        _assert_release_downgrade(engine, release_data)
        assert retained_account_state() == retained

        _assert_atomic_release_failures(config, engine, tmp_path, direction="upgrade")
        # Application startup upgrades a 0.1.4 database without stamp edits.
        with _captured_statements() as statements:
            ensure_app_version_synced(Settings(database_url=database_url))
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        assert _stamp(engine) == current_alembic_head()
        _assert_release_upgrade(engine, release_data, now, first=False)
        _assert_labs_policy_upgrade(
            engine, config, workspace_id, upgrade=False, raw_default=True
        )
        _assert_archived_inbox_indexes(engine, expected_revision=current_alembic_head())
        assert retained_account_state() == retained
        assert audit_inputs(database_url) == []
    finally:
        try:
            if upgraded:
                # The dedicated DB starts empty. The 0.1.4 downgrade refuses
                # retained login and deletion evidence, so remove it first.
                with engine.begin() as connection:
                    connection.execute(sa.text("DELETE FROM auth_login_transactions"))
                    connection.execute(sa.text("DELETE FROM user_identities"))
                    connection.execute(
                        sa.text(
                            "DELETE FROM users WHERE password_hash IS NULL "
                            "OR email IS NULL OR status = 'deletion_pending'"
                        )
                    )
                command.downgrade(config, "base")
                with engine.begin() as connection:
                    connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()


def _assert_labs_policy_upgrade(
    engine, config, workspace_id, *, upgrade=True, raw_default=False
):
    """Apply 0.2.3 once and inspect its Labs policy on the retained workspace."""

    revision = RELEASE_023_REVISION
    with engine.begin() as connection:
        workspaces = _table(connection, "workspace")
        connection.execute(
            workspaces.update()
            .where(workspaces.c.id == workspace_id)
            .values(workspace_graph_enabled=True, usage_relation_signals_enabled=False)
        )
        original = dict(
            connection.execute(
                sa.select(workspaces).where(workspaces.c.id == workspace_id)
            )
            .mappings()
            .one()
        )

    if upgrade:
        assert "labs_policy" not in original
        command.upgrade(config, revision)
    else:
        # Startup already performed the one re-upgrade in this traversal.
        assert original.pop("labs_policy") == {}
        revision = current_alembic_head()
        assert _stamp(engine) == revision
    columns = {
        column["name"]: column for column in inspect(engine).get_columns("workspace")
    }
    with engine.begin() as connection:
        workspaces = _table(connection, "workspace")
        current = dict(
            connection.execute(
                sa.select(workspaces).where(workspaces.c.id == workspace_id)
            )
            .mappings()
            .one()
        )
        assert {key: current[key] for key in original} == original
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == revision
        )
        assert not columns["labs_policy"]["nullable"]
        assert columns["labs_policy"]["default"] is not None
        assert current["labs_policy"] == {}
        connection.execute(
            workspaces.update()
            .where(workspaces.c.id == workspace_id)
            .values(labs_policy={"ask_enabled": True, "agent_enabled": True})
        )
        assert connection.scalar(
            sa.select(workspaces.c.labs_policy).where(workspaces.c.id == workspace_id)
        ) == {"ask_enabled": True, "agent_enabled": True}
        # pg8000 maps NOT NULL violations to ProgrammingError; verify the
        # database constraint code instead of the driver's exception subclass.
        with pytest.raises(DBAPIError) as rejected, connection.begin_nested():
            connection.execute(
                workspaces.update()
                .where(workspaces.c.id == workspace_id)
                .values(labs_policy=sa.null())
            )
        if engine.dialect.name == "postgresql":
            error = rejected.value.orig.args[0]
            assert error["C"] == "23502"
            assert error["c"] == "labs_policy"
        else:
            assert rejected.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"

    # A new raw SQL insert must also default off without consulting ORM defaults.
    if not raw_default:
        return
    with engine.begin() as connection:
        workspaces = _table(connection, "workspace")
        inserted_id = uuid4()
        connection.execute(
            workspaces.insert(),
            {**original, "id": inserted_id, "slug": "labs-default-workspace"},
        )
        assert (
            connection.scalar(
                sa.select(workspaces.c.labs_policy).where(
                    workspaces.c.id == inserted_id
                )
            )
            == {}
        )
        # Leave the retained-account snapshot unchanged after the raw-default probe.
        connection.execute(workspaces.delete().where(workspaces.c.id == inserted_id))


def _assert_archived_inbox_indexes(
    engine, *, present=True, expected_revision=RELEASE_023_REVISION
):
    """Inspect the indexes applied with Labs by the same 0.2.3 revision."""

    if present:
        assert _stamp(engine) == expected_revision
    inspector = inspect(engine)
    for table, owner in (
        ("workspace_inbox_entry", ["workspace_id", "user_id"]),
        ("account_inbox_entry", ["user_id"]),
    ):
        indexes = {index["name"]: index for index in inspector.get_indexes(table)}
        index = indexes.get(f"ix_{table}_archived")
        if not present:
            assert index is None
            continue
        assert index["column_names"] == [*owner, "updated_at", "kind", "item_key"]
        where = index["dialect_options"][f"{engine.dialect.name}_where"]
        assert "archived_revision IS NOT NULL" in str(where)


RELEASE_TABLES = {
    "account_inbox_entry",
    "workspace_inbox_entry",
    "workspace_inbox_recipient",
    "graph_observation",
    "graph_history_coverage",
    "co_access_pair",
    "context_document_reference",
    "context_published_citation",
    "context_citation_publication",
}
# ORM parity covers every table the release or its 0.1.4 parent shapes.
PARITY_TABLES = RELEASE_TABLES | {
    "users",
    "workspace",
    "user_identities",
    "auth_login_transactions",
    "email_outbox",
    "email_reauthentication_challenges",
    "auth_email_conflict_requests",
    "page_links",
    "edge_projection",
    "mcp_tool_call",
    "review_decision",
}


def _table(connection, name):
    table = sa.Table(name, sa.MetaData(), autoload_with=connection)
    # Reflection retains each real revision's columns, while these bind adapters
    # let the same fixture express UUIDs and UTC timestamps on both dialects.
    for column in table.c:
        model = Base.metadata.tables[name].c.get(column.name)
        if model is not None:
            column.type = model.type
    return table


def _insert(connection, table_name, **values):
    table = _table(connection, table_name)
    for column in Base.metadata.tables[table_name].c:
        if column.name in table.c and column.name not in values and column.default:
            default = column.default
            values[column.name] = (
                default.arg(None) if default.is_callable else default.arg
            )
    connection.execute(table.insert(), values)
    return values


def _seed_release_data(engine, workspace_id, existing_id, now):
    """Only 0.1.4 columns: the persisted sources the release must transform."""
    data = {key: uuid4() for key in ("source", "target", "version", "link", "purged")}
    data["workspace_id"] = workspace_id
    data["existing_id"] = existing_id
    data["viewed"] = set()
    data["referenced"] = set()
    with engine.begin() as connection:
        _insert(
            connection,
            "change_set",
            workspace_id=workspace_id,
            trigger_type="manual",
            actor_type="human",
            actor_id=data["purged"],
        )
        _insert(
            connection,
            "context_page",
            id=data["source"],
            workspace_id=workspace_id,
            slug="release-source",
            title="Source",
        )
        _insert(
            connection,
            "context_page",
            id=data["target"],
            workspace_id=workspace_id,
            slug="release-target",
            title="Target",
        )
        _insert(
            connection,
            "context_page_version",
            id=data["version"],
            workspace_id=workspace_id,
            page_id=data["source"],
            body_md="# Source\n\n[[release-target]]",
            content_hash="original-source",
            actor_type="human",
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id="release-migration",
        )
        pages = _table(connection, "context_page")
        connection.execute(
            pages.update()
            .where(pages.c.id == data["source"])
            .values(
                current_version_id=data["version"],
                current_published_version_id=data["version"],
                lifecycle_status="published",
            )
        )
        _insert(
            connection,
            "page_links",
            id=data["link"],
            workspace_id=workspace_id,
            source_page_id=data["source"],
            source_version_id=data["version"],
            target_slug="release-target",
            target_page_id=data["target"],
            occurrence_count=2,
            first_snippet="Original snippet",
        )
        # Two conversations support one pair; an outline touch is retained as a
        # reference and must not acquire body-view or co-access semantics.
        for offset in range(2):
            client, chat = uuid4(), uuid4()
            at = now - timedelta(minutes=10 + offset * 40)
            _insert(
                connection,
                "mcp_client",
                id=client,
                workspace_id=workspace_id,
                name=f"release-{offset}",
                client_type="generic",
                scopes=[],
                status="active",
            )
            _insert(
                connection,
                "mcp_chat_session",
                id=chat,
                workspace_id=workspace_id,
                client_id=client,
                mcp_session_id=str(chat),
                started_at=at,
                last_activity_at=at,
            )
            for position, resource in enumerate((data["source"], data["target"])):
                call, occurrence = uuid4(), uuid4()
                happened = at + timedelta(minutes=position)
                _insert(
                    connection,
                    "mcp_tool_call",
                    id=call,
                    workspace_id=workspace_id,
                    client_id=client,
                    mcp_session_id=chat,
                    request_id=uuid4(),
                    tool_name="read",
                    result_status="succeeded",
                    started_at=happened,
                    completed_at=happened,
                )
                _insert(
                    connection,
                    "mcp_chat_tool_call_artifact",
                    id=occurrence,
                    workspace_id=workspace_id,
                    chat_session_id=chat,
                    mcp_tool_call_id=call,
                    resource_kind="context_page",
                    resource_id=resource,
                    verb="referenced",
                    occurred_at=happened,
                )
                _insert(
                    connection,
                    "mcp_chat_artifact_link",
                    workspace_id=workspace_id,
                    chat_session_id=chat,
                    resource_kind="context_page",
                    resource_id=resource,
                    verb="referenced",
                    first_occurred_at=happened,
                    last_occurred_at=happened,
                    occurrence_count=1,
                    last_mcp_tool_call_id=call,
                )
                data["viewed"].add(occurrence)
            call, occurrence = uuid4(), uuid4()
            _insert(
                connection,
                "mcp_tool_call",
                id=call,
                workspace_id=workspace_id,
                client_id=client,
                mcp_session_id=chat,
                request_id=uuid4(),
                tool_name="read_structure_map",
                result_status="succeeded",
                started_at=at,
                completed_at=at,
            )
            _insert(
                connection,
                "mcp_chat_tool_call_artifact",
                id=occurrence,
                workspace_id=workspace_id,
                chat_session_id=chat,
                mcp_tool_call_id=call,
                resource_kind="context_page",
                resource_id=data["source"],
                verb="referenced",
                occurred_at=at,
            )
            data["referenced"].add(occurrence)
        data["calls"] = connection.execute(
            sa.text(
                "SELECT id, tool_name, result_status FROM mcp_tool_call ORDER BY id"
            )
        ).all()
    return data


def _assert_release_upgrade(engine, data, now, *, first):
    inspector = inspect(engine)
    assert RELEASE_TABLES <= set(inspector.get_table_names())
    indexes = {i["name"]: i for i in inspector.get_indexes("mcp_tool_call")}
    assert indexes["ix_mcp_tool_call_workspace_session_started"]["column_names"] == [
        "workspace_id",
        "mcp_session_id",
        "started_at",
        "id",
    ]
    assert not indexes["ix_mcp_tool_call_workspace_session_started"]["unique"]
    locator = {c["name"]: c for c in inspector.get_columns("page_links")}
    assert not locator["locator_key"]["nullable"] and locator["target_slug"]["nullable"]
    assert inspector.get_foreign_keys("workspace_inbox_recipient") == []
    assert inspector.get_pk_constraint("workspace_inbox_recipient")[
        "constrained_columns"
    ] == ["workspace_id", "user_id"]
    with engine.begin() as connection:
        links = _table(connection, "page_links")
        legacy = (
            connection.execute(sa.select(links).where(links.c.id == data["link"]))
            .mappings()
            .one()
        )
        assert legacy["locator_key"] == "wikilink:release-target"
        assert (
            legacy["occurrence_count"] == 2
            and legacy["first_snippet"] == "Original snippet"
        )
        assert legacy["target_page_id"] == data["target"]
        versions = _table(connection, "context_page_version")
        assert (
            connection.scalar(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == data["version"]
                )
            )
            == "original-source"
        )
        recipients = _table(connection, "workspace_inbox_recipient")
        recipient = (
            connection.execute(
                sa.select(recipients).where(recipients.c.user_id == data["purged"])
            )
            .mappings()
            .one()
        )
        assert recipient["deleted_at"] is not None
        pairs = _table(connection, "co_access_pair")
        pair = connection.execute(sa.select(pairs)).mappings().one()
        assert pair["support_session_count"] == 2
        assert {pair["source_id"], pair["target_id"]} == {
            data["source"],
            data["target"],
        }
        occurrences = _table(connection, "mcp_chat_tool_call_artifact")
        assert set(
            connection.scalars(
                sa.select(occurrences.c.verb).where(
                    occurrences.c.id.in_(data["viewed"])
                )
            )
        ) == {"viewed"}
        assert set(
            connection.scalars(
                sa.select(occurrences.c.verb).where(
                    occurrences.c.id.in_(data["referenced"])
                )
            )
        ) == {"referenced"}
        assert all(
            segment is not None and position in (0, 1)
            for segment, position in connection.execute(
                sa.select(
                    occurrences.c.co_access_segment, occurrences.c.co_access_position
                ).where(occurrences.c.id.in_(data["viewed"]))
            )
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT id, tool_name, result_status FROM mcp_tool_call ORDER BY id"
                )
            ).all()
            == data["calls"]
        )
        for table_name in (
            "context_document_reference",
            "context_published_citation",
            "context_citation_publication",
        ):
            table = _table(connection, table_name)
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        if not first:
            return
        # The locator UNIQUE constraint requires real database enforcement.
        # One duplicate write per dialect exercises its rebuilt table; scalar
        # invalid-input matrices are covered by the existing policy tests.
        duplicate = dict(legacy)
        duplicate["id"] = uuid4()
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(links.insert(), duplicate)
        duplicate.update(locator_key=f"page_id:{data['target']}", target_slug=None)
        connection.execute(links.insert(), duplicate)
        _insert(
            connection,
            "context_document_reference",
            workspace_id=data["workspace_id"],
            owner_version_id=data["version"],
            target_kind="context_page",
            context_page_id=data["target"],
        )
        _insert(
            connection,
            "context_citation_publication",
            workspace_id=data["workspace_id"],
            version_id=data["version"],
            fingerprint="frozen",
        )
        _insert(
            connection,
            "context_published_citation",
            workspace_id=data["workspace_id"],
            source_version_id=data["version"],
            target_kind="context_page",
            context_page_id=data["target"],
        )
        # Inbox marks have a concrete mode/nullability contract on both tables.
        for name in ("workspace_inbox_entry", "account_inbox_entry"):
            values = dict(
                user_id=data["existing_id"],
                kind="test",
                item_key="release",
                mode="mark",
                updated_at=now,
            )
            if name == "workspace_inbox_entry":
                values["workspace_id"] = data["workspace_id"]
            _insert(connection, name, **values)


def _assert_release_downgrade(engine, data):
    with engine.connect() as connection:
        links = _table(connection, "page_links")
        # Only the durable wikilink survives; permanent URL projections can be
        # regenerated after re-upgrade and never rewrite published source bytes.
        assert set(connection.scalars(sa.select(links.c.id))) == {
            data["link"],
            data["runtime_link"],
        }
        runtime = (
            connection.execute(
                sa.select(links).where(links.c.id == data["runtime_link"])
            )
            .mappings()
            .one()
        )
        assert runtime["occurrence_count"] == 2
        assert runtime["target_page_id"] == data["runtime_target"]
        occurrences = _table(connection, "mcp_chat_tool_call_artifact")
        assert set(connection.scalars(sa.select(occurrences.c.verb))) == {"referenced"}
        assert (
            connection.execute(
                sa.text(
                    "SELECT id, tool_name, result_status FROM mcp_tool_call ORDER BY id"
                )
            ).all()
            == data["calls"]
        )


def _assert_model_parity(engine):
    def include(obj, name, kind, reflected, compare_to):
        if kind == "table":
            return name in PARITY_TABLES
        return getattr(getattr(obj, "table", None), "name", None) in PARITY_TABLES

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": include}
        )
        assert compare_metadata(context, Base.metadata) == []


def _assert_atomic_release_failures(config, engine, tmp_path, *, direction):
    """Injected late DDL errors share the successful traversal's database.

    A successful migration cannot prove transaction rollback. This adds exactly
    one upgrade and one downgrade failure input per dialect, not extra database
    scenarios; the retry executes the real migration after each rollback. The
    upgrade fails after all 0.2.3 DDL, covering both release steps in the same
    transaction; the downgrade still checks the published 0.2.1 boundary.
    """
    scripts = tmp_path / f"failed-{direction}"
    shutil.copytree(
        resolve_migrations_dir(), scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    filename = (
        "00ba02774c4a_release_0_2_3.py"
        if direction == "upgrade"
        else "5b7e5c149279_release_0_2.py"
    )
    revision = scripts / "versions/v0_2" / filename
    revision.write_text(
        revision.read_text()
        + (
            f"\n_original_{direction} = {direction}\n"
            f"def {direction}():\n"
            f"    _original_{direction}()\n"
            '    raise RuntimeError("late release DDL failure")\n'
        )
    )
    broken = Config(str(resolve_alembic_ini()))
    broken.set_main_option("script_location", str(scripts))
    broken.set_main_option("sqlalchemy.url", config.get_main_option("sqlalchemy.url"))
    configure_test_repositories(broken)
    before = _schema_snapshot(engine)
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        rows = {
            table: connection.execute(sa.text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        }
    with pytest.raises(RuntimeError, match="late release DDL failure"):
        if direction == "upgrade":
            command.upgrade(broken, RELEASE_023_REVISION)
        else:
            command.downgrade(broken, PARENT)
    assert _schema_snapshot(engine) == before
    with engine.connect() as connection:
        assert {
            table: connection.execute(sa.text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        } == rows


def _seed_runtime_links(engine, data):
    # A real publication writes URL and wikilink projections together. Downgrade
    # must use the surviving wikilink, rather than keep a mixed URL edge weight.
    factory = make_session_factory(engine)
    from backend.scripts.seed_dev import seed_default_workspace

    with factory.begin() as session:
        seed_default_workspace(session, slug="retained-workspace")
    service = PageService(factory)

    def write(slug, body):
        return service.write_page(
            workspace_slug="retained-workspace",
            slug=slug,
            title=slug,
            body_md=body,
            actor_kind="human",
            actor_id="release-runtime-links",
            actor_provenance="browser_session",
        )

    target = write("runtime-target", "# Target")
    source = write(
        "runtime-mixed",
        f"# Mixed\n\n[[{target.slug}]] twice [[{target.slug}]]\n\n"
        f"[Target](/context?target={target.id})",
    )
    with engine.connect() as connection:
        links = _table(connection, "page_links")
        rows = (
            connection.execute(
                sa.select(links).where(links.c.source_page_id == source.id)
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2
        wiki = next(row for row in rows if row["link_syntax"] == "wikilink")
        assert wiki["occurrence_count"] == 2
        data["runtime_link"] = wiki["id"]
        data["runtime_target"] = target.id


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_older_releases_upgrade_directly_at_startup(tmp_path, dialect):
    """Fresh initialization, then startup from 0.1.2 and 0.1.3, per dialect.

    The 0.1.4 traversal starts after the 0.1.3 input constraints and
    publication-time backfill. Startup upgrades 0.1.2 and 0.1.3 directly
    through the published 0.1.4 boundary, without an intermediate install.
    The same database is first initialized directly at 0.2 and its schema is
    compared with those populated upgrades, which catches snapshot drift and
    accidental replay of historical migrations without another matrix.
    """
    database_url, engine = _migration_engine(tmp_path, dialect, "older-releases.db")
    config = _config(database_url)
    settings = Settings(database_url=database_url)
    native, workspace, source, target, version, link = (uuid4() for _ in range(6))
    publication_time = datetime.now(UTC) - timedelta(days=5)
    retained_fields = {
        "users": "id, email, password_hash, display_name, default_workspace_id",
        "workspace": "id, slug, title, name, owner_user_id",
        "context_page": "id, workspace_id, slug, title, current_published_version_id",
        "context_page_version": "id, page_id, workspace_id, body_md, content_hash",
        "page_links": "id, workspace_id, source_page_id, source_version_id, "
        "target_slug, target_page_id, occurrence_count, first_snippet",
        "audit_event": "id, workspace_id, action, target_id, payload, created_at",
    }

    def retained_state():
        with engine.connect() as connection:
            return {
                name: connection.execute(
                    sa.text(f"SELECT {fields} FROM {name} ORDER BY id")
                ).all()
                for name, fields in retained_fields.items()
            }

    migrated = False
    try:
        with _captured_statements() as statements:
            ensure_app_version_synced(settings)
        migrated = True
        # A fresh install records the frozen release baseline, then each
        # appended revision once. Historical replay would add earlier stamps
        # and rebuild SQLite tables along the way.
        stamp_writes = [
            statement
            for statement in statements
            if statement.lstrip().startswith(
                ("INSERT INTO alembic_version", "UPDATE alembic_version")
            )
        ]
        initialization = list(initialization_script_directory().walk_revisions())
        assert len(stamp_writes) == len(initialization)
        assert stamp_writes[0].lstrip().startswith("INSERT INTO alembic_version")
        assert REVISION in stamp_writes[0]
        assert all(
            statement.lstrip().startswith("UPDATE alembic_version")
            for statement in stamp_writes[1:]
        )
        assert current_alembic_head() in stamp_writes[-1]
        assert not any("_alembic_tmp_" in statement for statement in statements)
        initialized_schema = _schema_snapshot(engine)
        command.downgrade(config, "efcbcf724889")
        with engine.begin() as connection:
            _insert(
                connection,
                "users",
                id=native,
                email="older@example.com",
                password_hash="retained-older-hash",
                display_name="Older User",
                member_search_key="older user older@example.com",
            )
            _insert(
                connection,
                "workspace",
                id=workspace,
                slug="older-workspace",
                title="Older workspace",
                name="Older workspace",
                directory_title_key="older workspace",
                owner_user_id=native,
            )
            users = _table(connection, "users")
            connection.execute(
                users.update()
                .where(users.c.id == native)
                .values(
                    default_workspace_id=workspace,
                    personal_workspace_id=workspace.hex
                    if dialect == "sqlite"
                    else workspace,
                )
            )
            _insert(
                connection,
                "context_page",
                id=source,
                workspace_id=workspace,
                slug="older-source",
                title="Older source",
            )
            _insert(
                connection,
                "context_page",
                id=target,
                workspace_id=workspace,
                slug="older-target",
                title="Older target",
            )
            _insert(
                connection,
                "context_page_version",
                id=version,
                workspace_id=workspace,
                page_id=source,
                body_md="# Older content\n\n[[older-target]]",
                content_hash="retained-older-content",
                actor_type="human",
                version_status="published_snapshot",
                created_by_actor_kind="human",
                created_by_actor_id=str(native),
            )
            pages = _table(connection, "context_page")
            connection.execute(
                pages.update()
                .where(pages.c.id == source)
                .values(
                    current_version_id=version,
                    current_published_version_id=version,
                    lifecycle_status="published",
                )
            )
            _insert(
                connection,
                "page_links",
                id=link,
                workspace_id=workspace,
                source_page_id=source,
                source_version_id=version,
                target_slug="older-target",
                target_page_id=target,
                occurrence_count=2,
                first_snippet="Retained older snippet",
            )
            _insert(
                connection,
                "audit_event",
                id=uuid4(),
                workspace_id=workspace,
                actor_kind="human",
                actor_id=str(native),
                action="context_page.draft.publish",
                target_kind="context_page",
                target_id=source,
                payload={
                    "version_id": str(version),
                    "previous_published_version_id": None,
                },
                created_at=publication_time,
            )
        retained = retained_state()
        for old_version, revision in (
            ("0.1.2", "efcbcf724889"),
            ("0.1.3", "19db79d3e622"),
        ):
            if old_version == "0.1.3":
                command.downgrade(config, revision)
            with engine.begin() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                    == revision
                )
                connection.execute(
                    sa.text("DELETE FROM app_metadata WHERE key = 'dtxwiki_version'")
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO app_metadata (key, value) "
                        "VALUES ('dtxwiki_version', :version)"
                    ),
                    {"version": old_version},
                )
            # A direct startup upgrade from each actual old schema/stamp.
            ensure_app_version_synced(settings)
            assert retained_state() == retained
            assert _schema_snapshot(engine) == initialized_schema
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                    == current_alembic_head()
                )
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT value FROM app_metadata "
                            "WHERE key = 'dtxwiki_version'"
                        )
                    )
                    == settings.app_version
                )
                pages = _table(connection, "context_page")
                assert (
                    connection.scalar(
                        sa.select(pages.c.content_changed_at).where(
                            pages.c.id == source
                        )
                    )
                    == publication_time
                )
                assert connection.scalar(
                    sa.text("SELECT locator_key FROM page_links")
                ) == ("wikilink:older-target")
                if dialect == "sqlite":
                    assert (
                        connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                        == []
                    )
            assert audit_inputs(database_url) == []
    finally:
        try:
            if migrated:
                command.downgrade(config, "base")
                with engine.begin() as connection:
                    connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()
