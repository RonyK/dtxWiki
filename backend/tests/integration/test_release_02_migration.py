"""One populated 0.1.4 -> 0.2 -> 0.1.4 -> 0.2 traversal per dialect.

There are two collected cases. Each uses the same data for backfills, schema
contracts, one nullable-native-account refusal, one auth-evidence refusal, one
deletion refusal, and upgrade/downgrade late-DDL rollback. Those five refusal
and fault inputs plus one locator duplicate write per dialect exercise distinct
data-loss/transaction/constraint paths;
none introduces another database traversal. Scalar validation matrices belong
to existing policy tests rather than this consolidated release migration.
"""

import os
import shutil
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import Base
from backend.models.workspace import WorkspaceMembership
from backend.services.ops.input_maintenance import audit_inputs
from backend.services.page_service import PageService
from backend.tests.integration.test_release_schema_migration import (
    _assert_each_table_rebuilt_at_most_once,
    _captured_statements,
    _schema_snapshot,
)

PARENT = "ef53a0a7061c"
REVISION = "5b7e5c149279"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_release_02_upgrade_downgrade_reupgrade(tmp_path, dialect):
    """One traversal per dialect covers data, defaults, constraints and refusal."""

    database_url = (
        f"sqlite+pysqlite:///{tmp_path / 'release-02.db'}"
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
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    now = datetime.now(UTC)

    def _id():
        value = uuid4()
        return value.hex if dialect == "sqlite" else value

    existing_id, external_id, preserved_outbox_id = _id(), _id(), _id()
    workspace_id = uuid4()

    def _user(user_id, password_hash, email):
        return {
            "id": user_id,
            "email": email,
            "password_hash": password_hash,
            "display_name": "Migration User",
            "member_search_key": f"migration user {email}",
            "status": "active",
            "session_generation": 0,
            "created_at": now,
            "updated_at": now,
        }

    def _tables():
        metadata = sa.MetaData()
        return tuple(
            sa.Table(name, metadata, autoload_with=engine)
            for name in ("users", "user_identities", "auth_login_transactions")
        )

    def retained_account_state(connection):
        return (
            connection.execute(
                sa.select(
                    users.c.id,
                    users.c.email,
                    users.c.password_hash,
                    users.c.default_workspace_id,
                ).where(users.c.id == existing_id)
            ).one(),
            # Reflected: the current model maps workspace columns added later.
            connection.execute(
                sa.text("SELECT id, slug, owner_user_id FROM workspace ORDER BY id")
            ).all(),
            connection.execute(sa.select(WorkspaceMembership.__table__)).all(),
        )

    upgraded = False
    try:
        command.upgrade(config, PARENT)
        upgraded = True
        users = sa.Table("users", sa.MetaData(), autoload_with=engine)
        workspaces = sa.Table("workspace", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                # Reflect the parent revision: ORM Python defaults include
                # Graph View columns which are added after this migration.
                workspaces.insert(),
                {
                    "id": workspace_id.hex if dialect == "sqlite" else workspace_id,
                    "slug": "retained-workspace",
                    "directory_title_key": "retained workspace",
                    "name": "Retained workspace",
                    "title": "Retained workspace",
                    "goal": "Preserve external-login migration data",
                    "default_language": "en",
                    "context_media_policy": {},
                    "library_policy": {},
                    "write_policy": {},
                    "citation_policy": {},
                    "agent_injection_policy": {},
                    "review_policy": {},
                    "lint_policy": {},
                    "auto_fix_enabled": False,
                    "visibility": "private",
                    "created_by_actor_id": _id(),
                    "owner_actor_id": _id(),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                users.insert(),
                {
                    **_user(
                        existing_id, "preserved-local-hash", "existing@example.com"
                    ),
                    "default_workspace_id": workspace_id.hex
                    if dialect == "sqlite"
                    else workspace_id,
                },
            )
            connection.execute(workspaces.update().values(owner_user_id=existing_id))
            connection.execute(
                WorkspaceMembership.__table__.insert(),
                {
                    "user_id": UUID(str(existing_id)),
                    "workspace_id": workspace_id,
                    "role": "owner",
                },
            )
            outbox = sa.Table("email_outbox", sa.MetaData(), autoload_with=connection)
            connection.execute(
                outbox.insert(),
                {
                    "id": preserved_outbox_id,
                    "recipient_user_id": existing_id,
                    "recipient_email": "existing@example.com",
                    "message_kind": "password_changed",
                    "status": "queued",
                    "attempt_count": 2,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                users.update().values(
                    personal_workspace_id=workspace_id.hex
                    if dialect == "sqlite"
                    else workspace_id
                )
            )
            retained = retained_account_state(connection)
        release_data = _seed_release_data(
            engine, workspace_id, UUID(str(existing_id)), now
        )
        with _captured_statements() as statements:
            command.upgrade(config, REVISION)
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        _assert_release_upgrade(engine, release_data, now, first=True)
        _seed_runtime_links(engine, release_data)
        users, identities, transactions = _tables()
        with engine.connect() as connection:
            assert retained_account_state(connection) == retained
        identity = {
            "id": _id(),
            "user_id": external_id,
            "provider_slug": "example",
            "issuer": "https://issuer.example",
            "subject": "external-user",
            "linked_via": "jit",
            "created_at": now,
        }
        transaction = {
            "id": _id(),
            "state_digest": "state-digest",
            "binding_digest": "binding-digest",
            "provider_slug": "example",
            "redirect_uri": "https://wiki.example/api/v1/auth/sso/example/callback",
            "purpose": "link",
            "user_id": external_id,
            "session_generation": 0,
            "session_digest": "stable-browser-session",
            "return_to": "/settings",
            "created_at": now,
            "expires_at": now + timedelta(minutes=10),
        }
        with engine.begin() as connection:
            connection.execute(users.insert(), _user(external_id, None, None))
            connection.execute(identities.insert(), identity)
            connection.execute(transactions.insert(), transaction)
            assert (
                connection.scalar(sa.select(identities.c.last_email_verified)) is False
            )
            assert connection.scalar(sa.select(transactions.c.remember_me)) is False
            assert connection.scalar(sa.select(identities.c.directory_tenant)) is None
            assert (
                connection.scalar(sa.select(transactions.c.session_digest))
                == "stable-browser-session"
            )

        assert audit_inputs(database_url) == []
        # Independent ORM parity covers every changed table on both dialects.
        # Frozen library/workspace tables have unrelated redundant constraints.
        auth_tables = {
            "users",
            "user_identities",
            "auth_login_transactions",
            "email_reauthentication_challenges",
            "email_outbox",
            "auth_email_conflict_requests",
        }

        def include_auth_object(obj, name, kind, reflected, compare_to):
            if kind == "table":
                return name in auth_tables
            return getattr(getattr(obj, "table", None), "name", None) in auth_tables

        assert "auth_pending_signups" not in inspect(engine).get_table_names()
        metadata = sa.MetaData()
        challenges = sa.Table(
            "email_reauthentication_challenges", metadata, autoload_with=engine
        )
        outbox = sa.Table("email_outbox", metadata, autoload_with=engine)
        conflicts = sa.Table(
            "auth_email_conflict_requests", metadata, autoload_with=engine
        )
        assert not outbox.c.recipient_user_id.nullable
        assert "pending_signup_id" not in outbox.c
        assert {"ix_email_reauth_expires_at", "ix_email_reauth_user_created_at"} <= {
            index["name"] for index in inspect(engine).get_indexes(challenges.name)
        }
        challenge = {
            "id": _id(),
            "user_id": existing_id,
            "session_generation": 0,
            "session_digest": "stable-native-session",
            "binding_digest": "email-browser",
            "recipient_digest": "email-digest",
            "token_digest": "email-token",
            "scope": "admin",
            "return_to": "/settings/admin",
            "created_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
        message = {
            "id": _id(),
            "recipient_user_id": existing_id,
            "recipient_email": "existing@example.com",
            "reauthentication_challenge_id": challenge["id"],
            "message_kind": "email_reauthentication",
            "status": "queued",
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        conflict = {
            "id": _id(),
            "target_user_id": existing_id,
            "email": "existing@example.com",
            "provider_slug": "company",
            "issuer": "https://idp.example.com",
            "subject": "claimant",
            "status": "pending",
            "created_at": now,
        }
        with engine.begin() as connection:
            connection.execute(conflicts.insert(), conflict)
            preserved = (
                connection.execute(
                    sa.select(outbox).where(outbox.c.id == preserved_outbox_id)
                )
                .mappings()
                .one()
            )
            assert preserved["recipient_user_id"] == existing_id
            assert preserved["attempt_count"] == 2
            assert preserved["reauthentication_challenge_id"] is None
            connection.execute(challenges.insert(), challenge)
            connection.execute(outbox.insert(), message)

        def assert_downgrade_refused(message):
            with pytest.raises(RuntimeError, match=message):
                command.downgrade(config, PARENT)
            # Every guard belongs to the same revision and must preserve the
            # complete schema, including tables created later in upgrade().
            inspector = inspect(engine)
            assert auth_tables <= set(inspector.get_table_names())
            assert "reauthentication_challenge_id" in {
                column["name"] for column in inspector.get_columns("email_outbox")
            }
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                    == REVISION
                )

        assert_downgrade_refused("incompatible account")

        with engine.begin() as connection:
            connection.execute(
                users.update()
                .where(users.c.id == external_id)
                .values(
                    password_hash="external-local-hash",
                    email="external@example.com",
                    pending_email=None,
                )
            )
            connection.execute(identities.delete())
            connection.execute(transactions.delete())
            connection.execute(conflicts.delete())
        # Isolate the evidence tables: a populated sibling must not hide a
        # missing guard. Consumed/expired records still cannot be discarded.
        assert_downgrade_refused("email_reauthentication_challenges")
        with engine.begin() as connection:
            connection.execute(challenges.delete())
            # The new cascade removes only the challenge's message; the
            # native recipient and original password-change outbox survive.
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 1
            )

        _assert_deletion_guard(config, engine, users, existing_id, now)
        _assert_atomic_release_failures(config, engine, tmp_path, direction="downgrade")
        with _captured_statements() as statements:
            command.downgrade(config, PARENT)
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        _assert_release_downgrade(engine, release_data)
        assert (auth_tables - {"users", "email_outbox"}).isdisjoint(
            inspect(engine).get_table_names()
        )
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("users")
        }
        assert not columns["email"]["nullable"]
        assert not columns["password_hash"]["nullable"]
        assert "pending_email" not in columns
        assert "personal_workspace_id" in columns
        old_outbox = sa.Table("email_outbox", sa.MetaData(), autoload_with=engine)
        assert "reauthentication_challenge_id" not in old_outbox.c
        with engine.begin() as connection:
            assert retained_account_state(connection) == retained
            assert connection.scalar(sa.select(old_outbox.c.attempt_count)) == 2
        _assert_atomic_release_failures(config, engine, tmp_path, direction="upgrade")
        with _captured_statements() as statements:
            command.upgrade(config, REVISION)
        if dialect == "sqlite":
            _assert_each_table_rebuilt_at_most_once(statements)
        _assert_release_upgrade(engine, release_data, now, first=False)
        assert audit_inputs(database_url) == []
        users, identities, transactions = _tables()
        with engine.begin() as connection:
            assert retained_account_state(connection) == retained
            connection.execute(
                users.update()
                .where(users.c.id == external_id)
                .values(password_hash=None, email=None)
            )
            connection.execute(identities.insert(), identity)
            connection.execute(transactions.insert(), transaction)
            assert (
                connection.scalar(
                    sa.select(users.c.password_hash).where(users.c.id == existing_id)
                )
                == "preserved-local-hash"
            )
            assert (
                connection.scalar(
                    sa.select(users.c.email).where(users.c.id == existing_id)
                )
                == "existing@example.com"
            )
            # Deleting an external account cascades both new user-owned tables.
            connection.execute(users.delete().where(users.c.id == external_id))
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(identities))
                == 0
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(transactions))
                == 0
            )
        if dialect == "postgresql":
            # SQLite model parity is already checked by the shared release
            # head test. PostgreSQL types/constraints need this real DB, at
            # head because later revisions add their own user columns.
            command.upgrade(config, "head")
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection, opts={"include_object": include_auth_object}
                )
                assert compare_metadata(context, Base.metadata) == []
    finally:
        try:
            if upgraded:
                # Dedicated DB starts empty. Cleanup includes refusal evidence.
                with engine.begin() as connection:
                    present = set(inspect(connection).get_table_names())
                    for table in (
                        "email_outbox",
                        "auth_email_conflict_requests",
                        "email_reauthentication_challenges",
                        "auth_login_transactions",
                        "user_identities",
                    ):
                        if table in present:
                            connection.execute(sa.text(f"DELETE FROM {table}"))
                    columns = {
                        column["name"]
                        for column in inspect(connection).get_columns("users")
                    }
                    predicate = "password_hash IS NULL OR email IS NULL"
                    if "pending_email" in columns:
                        predicate += " OR pending_email IS NOT NULL"
                    connection.execute(sa.text("DELETE FROM users WHERE " + predicate))
                command.downgrade(config, "base")
                with engine.begin() as connection:
                    connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            engine.dispose()


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
DELETION_COLUMNS = {
    "deletion_requested_at",
    "purge_after",
    "deletion_retention_days",
    "deletion_prior_status",
    "deletion_failure_count",
    "deletion_last_error",
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
            link_syntax="wikilink",
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
    columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert "personal_workspace_id" not in columns
    assert DELETION_COLUMNS <= columns.keys()
    assert all(
        columns[name]["nullable"] and columns[name]["default"] is None
        for name in DELETION_COLUMNS
    )
    indexes = {i["name"]: i for i in inspector.get_indexes("mcp_tool_call")}
    assert indexes["ix_mcp_tool_call_workspace_session_started"]["column_names"] == [
        "workspace_id",
        "mcp_session_id",
        "started_at",
        "id",
    ]
    assert not indexes["ix_mcp_tool_call_workspace_session_started"]["unique"]
    assert "ix_review_decision_review_created_id" in {
        i["name"] for i in inspector.get_indexes("review_decision")
    }
    assert "ix_users_purge_after" in {i["name"] for i in inspector.get_indexes("users")}
    assert "ix_workspace_account_deletion_user_id" in {
        i["name"] for i in inspector.get_indexes("workspace")
    }
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
    inspector = inspect(engine)
    assert RELEASE_TABLES.isdisjoint(inspector.get_table_names())
    assert DELETION_COLUMNS.isdisjoint(
        c["name"] for c in inspector.get_columns("users")
    )
    assert "locator_key" not in {c["name"] for c in inspector.get_columns("page_links")}
    assert "ix_mcp_tool_call_workspace_session_started" not in {
        i["name"] for i in inspector.get_indexes("mcp_tool_call")
    }
    with engine.connect() as connection:
        users = _table(connection, "users")
        assert (
            connection.scalar(
                sa.select(users.c.personal_workspace_id).where(
                    users.c.id == data["existing_id"]
                )
            )
            is None
        )
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


def _assert_deletion_guard(config, engine, users, user_id, now):
    with engine.begin() as connection:
        connection.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(
                status="deletion_pending",
                deletion_requested_at=now,
                purge_after=now + timedelta(days=30),
                deletion_retention_days=30,
            )
        )
    before = _schema_snapshot(engine)
    with pytest.raises(RuntimeError, match="deletion requests are pending"):
        command.downgrade(config, PARENT)
    assert _schema_snapshot(engine) == before
    with engine.begin() as connection:
        row = connection.execute(sa.select(users).where(users.c.id == user_id)).one()
        assert row.deletion_retention_days == 30
        assert row.purge_after - row.deletion_requested_at == timedelta(days=30)
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        connection.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(
                status="active",
                deletion_requested_at=None,
                purge_after=None,
                deletion_retention_days=None,
            )
        )


def _assert_atomic_release_failures(config, engine, tmp_path, *, direction):
    """Injected late DDL errors share the successful traversal's database.

    A successful migration cannot prove transaction rollback. This adds exactly
    one upgrade and one downgrade failure input per dialect, not extra database
    scenarios; the retry executes the real migration after each rollback.
    """
    scripts = tmp_path / f"failed-{direction}"
    shutil.copytree(
        resolve_migrations_dir(), scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    revision = scripts / "versions/v0_2/5b7e5c149279_release_0_2.py"
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
    before = _schema_snapshot(engine)
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        rows = {
            table: connection.execute(sa.text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        }
    with pytest.raises(RuntimeError, match="late release DDL failure"):
        if direction == "upgrade":
            command.upgrade(broken, REVISION)
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
    factory = sessionmaker(bind=engine, expire_on_commit=False)
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
