"""Real migration/recovery paths only; scalar CHECK cases live in unit tests.

Consolidate compatible checks into one traversal, preserving intermediate-state
assertions. See DEVELOPMENT.md for the per-migration test budget. The recovery
scenarios below cover distinct CLI, revocation and projection side effects.
"""

import os
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.app.runtime_resources import RuntimeResources
from backend.app.settings import Settings
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    REVISION,
    RULES,
    StoredInputCheck,
)
from backend.services.ops import runtime_lock
from backend.services.ops.input_maintenance import (
    InputMaintenanceError,
    audit_inputs,
    repair_input,
)

PRE_INPUT_REVISION = "efcbcf724889"


def _database(tmp_path, revision=PRE_INPUT_REVISION):
    url = f"sqlite:///{tmp_path / 'inputs.db'}"
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision)
    return url, config


def _user(connection, *, name="Alice", email="alice@example.test"):
    table = sa.Table("users", sa.MetaData(), autoload_with=connection)
    identity = uuid4()
    now = datetime.now(UTC)
    values = dict(
        id=identity.hex,
        email=email,
        password_hash="opaque-existing-hash",
        display_name=name,
        status="active",
        updated_at=now,
        created_at=now,
    )
    if "session_generation" in table.c:
        values["session_generation"] = 2
    if "member_search_key" in table.c:
        values["member_search_key"] = "old key"
    connection.execute(table.insert().values(**values))
    return identity


def _workspace(connection, identity, *, slug="nullable", title="Nullable"):
    # Reflect the released schema: the current ORM adds context_media_policy.
    table = sa.Table("workspace", sa.MetaData(), autoload_with=connection)
    now = datetime.now(UTC)
    connection.execute(
        table.insert().values(
            id=identity.hex,
            slug=slug,
            title=title,
            name=title,
            goal="Preserve pre-upgrade metadata",
            default_language="en",
            library_policy={},
            write_policy={},
            citation_policy={},
            agent_injection_policy={},
            review_policy={},
            lint_policy={},
            auto_fix_enabled=False,
            ask_retrieval_mode="auto",
            edge_projection_version=0,
            authority_projection_version=-1,
            visibility="private",
            join_policy="invite_only",
            directory_title_key=title.lower(),
            created_by_actor_id=uuid4().hex,
            owner_actor_id=uuid4().hex,
            created_at=now,
            updated_at=now,
        )
    )


def test_preupgrade_user_repairs_preserve_stamp_and_unlock_upgrade(
    tmp_path, monkeypatch
):
    # Separate from the successful policy round trip: dirty data must refuse
    # upgrade without advancing the stamp, then remain repairable. The oldest
    # supported schema lacks member_search_key and the recovery tables, and its
    # refused run rolls back the core revision; repairs on the parent
    # schema are covered by the email and CLI tests below.
    original_connect = sqlite3.dbapi2.connect

    def connect_with_foreign_keys_enabled(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    monkeypatch.setattr(sqlite3.dbapi2, "connect", connect_with_foreign_keys_enabled)
    url, config = _database(tmp_path, "bd62193d5cf6")
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        identity = _user(connection, name="x" * 33)
    report = audit_inputs(url)
    assert report[0]["count"] == 1
    assert "x" * 33 not in str(report)
    with pytest.raises(RuntimeError, match="preflight failed"):
        command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT version_num, display_name FROM alembic_version, users")
        ).one() == ("bd62193d5cf6", "x" * 33)
    repair_input(url, "user", identity, "display_name", "한글😀")
    repair_input(url, "user", identity, "email", " 새주소@EXAMPLE.TEST ")
    assert audit_inputs(url) == []
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT version_num, email FROM alembic_version, users")
        ).one() == ("bd62193d5cf6", "새주소@example.test")
    command.upgrade(config, PRE_INPUT_REVISION)
    tokens = sa.Table("api_tokens", sa.MetaData(), autoload_with=engine)
    token_id, orphan_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            tokens.insert(),
            [
                dict(
                    id=token.hex,
                    user_id=owner.hex,
                    name="Preserved token",
                    token_lookup_hash=token.hex,
                    hashed_token="existing hash",
                    scopes=["mcp:read"],
                    created_at=datetime.now(UTC),
                )
                for token, owner in ((token_id, identity), (orphan_id, uuid4()))
            ],
        )
    # This path cannot be proved by a CHECK-expression unit test: rebuilding
    # the parent table must keep its children, even when FK defaults are ON.
    # A pre-existing orphan is not the migration's doing and must neither be
    # deleted nor block this or any later (including no-op) Alembic run.
    # The policy round trip lives in test_input_migration_parent.py; here the
    # rebuild runs with FK defaults ON, then a no-op at the policy revision.
    command.upgrade(config, REVISION)
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == REVISION
        )
        assert (
            connection.execute(sa.text("SELECT password_hash FROM users")).scalar_one()
            == "opaque-existing-hash"
        )
        assert [
            (row[0], row[2])
            for row in connection.exec_driver_sql("PRAGMA foreign_key_check")
        ] == [("api_tokens", "users")]
        assert set(connection.scalars(sa.select(tokens.c.id))) == {
            token_id.hex,
            orphan_id.hex,
        }
    engine.dispose()


def test_email_repair_atomically_revokes_recovery_and_pending_mail(tmp_path):
    url, _config = _database(tmp_path)
    engine = sa.create_engine(url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        identity = _user(connection)
        connection.execute(
            sa.text("UPDATE users SET email_verified_at=:now"), {"now": now}
        )
        tokens = sa.Table(
            "password_reset_tokens", sa.MetaData(), autoload_with=connection
        )
        outbox = sa.Table("email_outbox", sa.MetaData(), autoload_with=connection)
        for purpose in ("password_reset", "email_verification"):
            token_id = uuid4().hex
            connection.execute(
                tokens.insert().values(
                    id=token_id,
                    user_id=identity.hex,
                    token_digest=purpose,
                    purpose=purpose,
                    delivery="email",
                    requested_via="public",
                    revoke_api_tokens=False,
                    expires_at=now,
                    created_at=now,
                )
            )
            for status in ("queued", "sent"):
                connection.execute(
                    outbox.insert().values(
                        id=uuid4().hex,
                        recipient_user_id=identity.hex,
                        token_id=token_id,
                        recipient_email="alice@example.test",
                        message_kind=purpose,
                        encrypted_token="opaque",
                        status=status,
                        attempt_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
    repair_input(url, "user", identity, "email", "new@example.test")
    with engine.connect() as connection:
        user = connection.execute(
            sa.text("SELECT email_verified_at,session_generation FROM users")
        ).one()
        assert user == (None, 3)
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM password_reset_tokens "
                    "WHERE revoked_at IS NOT NULL"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM email_outbox WHERE status='failed' "
                    "AND encrypted_token IS NULL AND next_attempt_at IS NULL"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM email_outbox WHERE status='sent' "
                    "AND encrypted_token='opaque' "
                    "AND recipient_email='alice@example.test'"
                )
            ).scalar_one()
            == 2
        )
    with runtime_lock.acquire_shared():
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            repair_input(url, "user", identity, "display_name", "blocked")
    resources = RuntimeResources.for_settings(Settings(database_url=url))
    # Another data-dir participant shares only the SQLite-side activity path.
    with runtime_lock.acquire_path(resources.activity_paths[1], "shared"):
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            repair_input(url, "user", identity, "display_name", "blocked")
    # A failed multi-path acquisition must release any earlier path guards.
    repair_input(url, "user", identity, "display_name", "Unlocked")
    with engine.begin() as connection:
        _user(connection, email="occupied@example.test")
    with pytest.raises(DBAPIError):
        repair_input(url, "user", identity, "email", "occupied@example.test")
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT email, session_generation FROM users WHERE id=:id"),
            {"id": identity.hex},
        ).one() == ("new@example.test", 3)
    engine.dispose()


def test_maintenance_refuses_fresh_database_without_creating_it(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(InputMaintenanceError):
        repair_input(f"sqlite:///{path}", "user", uuid4(), "display_name", "Alice")
    assert not path.exists()


def test_metadata_refuses_utf16_sqlite_before_creating_tables(tmp_path):
    from backend.models import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'utf16.sqlite'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA encoding='UTF-16'")
        with pytest.raises(RuntimeError, match="UTF-8"):
            Base.metadata.create_all(connection)
        assert sa.inspect(connection).get_table_names() == []
    engine.dispose()


def test_optional_title_repairs_preserve_null_and_automatic_chat_semantics(tmp_path):
    from backend.models import (
        LibraryItem,
        McpChatSession,
        MCPClient,
        WorkspaceTreeNode,
    )

    url, _config = _database(tmp_path)
    engine = sa.create_engine(url)
    workspace, client, item, chat, node = (uuid4() for _ in range(5))
    with engine.begin() as connection:
        _workspace(connection, workspace)
        connection.execute(
            MCPClient.__table__.insert().values(
                id=client,
                workspace_id=workspace,
                name="Client",
            )
        )
        connection.execute(
            LibraryItem.__table__.insert().values(
                id=item,
                workspace_id=workspace,
                connector_type="manual",
                uri="manual:nullable",
                title="Explicit",
            )
        )
        connection.execute(
            McpChatSession.__table__.insert().values(
                id=chat,
                workspace_id=workspace,
                client_id=client,
                mcp_session_id="nullable",
                title="Explicit",
                title_user_edited=True,
            )
        )
        connection.execute(
            WorkspaceTreeNode.__table__.insert().values(
                id=node,
                workspace_id=workspace,
                section="library",
                node_kind="library_item_ref",
                target_kind="library_item",
                target_id=item,
                title="x" * 320,
                sort_key="0001",
                primary_placement=True,
                visibility_status="visible",
                is_system_node=False,
                can_have_children=False,
            )
        )
    repair_input(url, "tree-node", node, "title", "Independent library label")
    repair_input(url, "library-item", item, "title", None)
    repair_input(url, "mcp-chat", chat, "title", None)
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT title FROM library_item")).scalar_one()
            is None
        )
        assert connection.execute(
            sa.text("SELECT title,title_user_edited FROM mcp_chat_session")
        ).one() == (None, 0)
        assert (
            connection.scalar(
                sa.text("SELECT title FROM workspace_tree_node WHERE id=:id"),
                {"id": node.hex},
            )
            == "Independent library label"
        )
    engine.dispose()


def test_context_title_repair_keeps_staged_name_and_historical_body(tmp_path):
    import json

    from backend.models import (
        ChangeOperation,
        ChangeSet,
        ContextPage,
        ContextPageVersion,
        PageNameIndex,
    )

    # Seed the released schema directly: current services query 0.1.3 columns.
    url, _config = _database(tmp_path)
    engine = sa.create_engine(url)
    workspace, page, version_id = (uuid4() for _ in range(3))
    body = "# Old title\nOriginal body"
    with engine.begin() as connection:
        _workspace(connection, workspace, slug="default", title="Default")
        connection.execute(
            ContextPage.__table__.insert().values(
                id=page,
                workspace_id=workspace,
                slug="repair-title",
                title="Old title",
            )
        )
        connection.execute(
            ContextPageVersion.__table__.insert().values(
                id=version_id,
                workspace_id=workspace,
                page_id=page,
                body_md=body,
                content_hash="preserved-historical-hash",
                actor_type="human",
                version_status="draft",
                created_by_actor_kind="human",
                created_by_actor_id="00000000-0000-0000-0000-000000000010",
            )
        )
        connection.execute(
            ContextPage.__table__.update()
            .where(ContextPage.id == page)
            .values(current_version_id=version_id, latest_draft_version_id=version_id)
        )
        change = uuid4()
        operation = uuid4()
        patch = json.dumps({"title": "Draft alternate"})
        connection.execute(
            ChangeSet.__table__.insert().values(
                id=change,
                workspace_id=workspace,
                trigger_type="manual",
                actor_type="human",
            )
        )
        connection.execute(
            ChangeOperation.__table__.insert().values(
                id=operation,
                workspace_id=workspace,
                change_set_id=change,
                target_type="context_page",
                target_id=page,
                operation="metadata_patch",
                diff_summary=patch,
                diff_ref=f"context_page_version:{version_id}",
            )
        )
        connection.execute(
            PageNameIndex.__table__.insert(),
            [
                {
                    "workspace_id": workspace,
                    "page_id": page,
                    "source_version_id": version_id,
                    "visibility_scope": "draft",
                    "name": title,
                    "name_norm": title.lower(),
                    "name_kind": "title",
                }
                for title in ("Old title", "Draft alternate")
            ],
        )
    repair_input(url, "context-page", page, "title", "Repaired title")
    with engine.connect() as connection:
        names = (
            connection.execute(
                sa.text(
                    "SELECT name FROM page_name_index WHERE page_id=:page "
                    "AND name_kind='title'"
                ),
                {"page": page.hex},
            )
            .scalars()
            .all()
        )
        assert set(names) == {"Repaired title", "Draft alternate"}
        assert (
            connection.execute(
                sa.text("SELECT body_md FROM context_page_version WHERE id=:version"),
                {"version": version_id.hex},
            ).scalar_one()
            == body
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT diff_summary FROM change_operation WHERE id=:operation"
                ),
                {"operation": operation.hex},
            ).scalar_one()
            == patch
        )
        projected = (
            connection.execute(
                sa.text(
                    "SELECT name FROM page_name_trgm "
                    "WHERE replace(page_id,'-','')=:page "
                    "AND name_kind='title'"
                ),
                {"page": page.hex},
            )
            .scalars()
            .all()
        )
        assert set(projected) == {"Repaired title", "Draft alternate"}
    engine.dispose()


def test_cli_repairs_do_not_bootstrap_and_reject_unknown_fields(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from backend.cli.commands import admin
    from backend.cli.main import app

    url, _config = _database(tmp_path)
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        identity = _user(connection, name="x" * 33)

    def forbidden_bootstrap(*_args, **_kwargs):
        pytest.fail("maintenance must not bootstrap")

    monkeypatch.setattr(admin, "bootstrap_app", forbidden_bootstrap)
    runner = CliRunner()
    invalid = runner.invoke(
        app,
        [
            "users",
            "set-display-name",
            str(identity),
            "--display-name",
            "x" * 33,
            "--db",
            url,
        ],
    )
    assert invalid.exit_code == 2
    assert "x" * 33 not in invalid.output
    valid = runner.invoke(
        app,
        [
            "users",
            "set-display-name",
            str(identity),
            "--display-name",
            "Alice",
            "--db",
            url,
        ],
    )
    assert valid.exit_code == 0, valid.output
    invalid_field = runner.invoke(
        app,
        [
            "inputs",
            "repair-metadata",
            "api-token",
            str(identity),
            "--field",
            "hashed_token",
            "--value",
            "ignored",
            "--db",
            url,
        ],
    )
    assert invalid_field.exit_code == 2
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == PRE_INPUT_REVISION
        )
    engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    not os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL migration database",
)
def test_frozen_scalar_checks_execute_on_postgresql():
    engine = sa.create_engine(
        os.environ["DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL"]
    )
    try:
        for rule in RULES:
            metadata = sa.MetaData()
            table = sa.Table(
                "input_policy_probe",
                metadata,
                sa.Column(rule.field, sa.Text),
                sa.Column("node_kind", sa.Text),
                sa.CheckConstraint(StoredInputCheck(rule), name=rule.name),
            )
            metadata.create_all(engine)
            try:
                valid = "😀" * rule.maximum if rule.kind != "slug" else "x" * 80
                if rule.kind == "email":
                    valid = "é" * 32 + "@" + "b" * 189
                with engine.begin() as connection:
                    connection.execute(
                        table.insert().values(
                            {
                                rule.field: valid,
                                "node_kind": "saved_view",
                            }
                        )
                    )
                invalid = valid + "x"
                with pytest.raises(DBAPIError) as error, engine.begin() as connection:
                    connection.execute(
                        table.insert().values(
                            {
                                rule.field: invalid,
                                "node_kind": "saved_view",
                            }
                        )
                    )
                assert error.value.orig.args[0]["C"] == "23514"
            finally:
                metadata.drop_all(engine)
    finally:
        engine.dispose()
