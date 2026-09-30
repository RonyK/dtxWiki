import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Boolean, MetaData, Table, create_engine, inspect, select, text

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

# Every test here reflects and drops the tables of whatever PostgreSQL database
# it is pointed at, and CI aims several of these files at one database, so they
# must not run beside each other under `-n`. The mark belongs in the module
# rather than in a conftest hook: xdist reads the group off the item while
# building its node id, before pytest_collection_modifyitems runs, so a mark
# added there arrives too late to reach the scheduler.
pytestmark = pytest.mark.xdist_group("postgres")


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_postgres_alembic_upgrade_head_and_downgrade_base():
    """Execute the real migration chain against PostgreSQL, not a compiler stub."""

    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    initial_tables = set(inspect(engine).get_table_names())
    assert not initial_tables, (
        "DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL must name an empty, "
        "dedicated database"
    )
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    upgraded = False
    try:
        command.upgrade(config, "0001_initial_release")
        upgraded = True
        metadata = MetaData()
        users = Table("users", metadata, autoload_with=engine)
        workspaces = Table("workspace", metadata, autoload_with=engine)
        now = datetime.now(UTC)
        user_id = uuid4()
        workspace_id = uuid4()
        actor_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(
                    id=user_id,
                    email="Straße@Example.com",
                    password_hash="migration-test-hash",
                    display_name="  Ａlice\tSmith ",
                    status="active",
                    session_generation=0,
                    default_workspace_id=None,
                    updated_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                workspaces.insert().values(
                    id=workspace_id,
                    slug="unicode-migration-test",
                    title="  Ｔeam\tStraße  ",
                    name="Unicode migration test",
                    description=None,
                    goal="Migration coverage",
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
                    owner_user_id=None,
                    updated_at=now,
                    created_at=now,
                )
            )
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert {
            "users",
            "workspace",
            "context_page",
            "email_outbox",
            "workspace_media_write_intent",
            "workspace_media_quota_usage",
            "stored_blob",
            "blob_incarnation",
            "stored_blob_active_incarnation",
            "stored_blob_inspection",
            "media_physical_charge",
            "media_deletion_receipt",
            "media_install_request",
            "media_outbox",
            "security_blocked_blob",
        } <= set(inspector.get_table_names())
        active_ownership = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys(
                "stored_blob_active_incarnation"
            )
            if foreign_key["name"] == "fk_stored_blob_active_incarnation"
        )
        assert active_ownership["constrained_columns"] == [
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ]
        assert active_ownership["referred_table"] == "blob_incarnation"
        assert active_ownership["referred_columns"] == [
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ]
        assert ("workspace_id", "sha256") in {
            tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("stored_blob")
        }
        assert "ix_media_outbox_claim" in {
            index["name"]
            for index in inspector.get_indexes("media_outbox")
        }
        is_public = next(
            column
            for column in inspector.get_columns("context_page")
            if column["name"] == "is_public"
        )
        assert isinstance(is_public["type"], Boolean)
        assert any(
            index["name"] == "uq_cpv_current_published_per_page" and index["unique"]
            for index in inspector.get_indexes("context_page_version")
        )
        workspace_key = next(
            column
            for column in inspector.get_columns("workspace")
            if column["name"] == "directory_title_key"
        )
        member_key = next(
            column
            for column in inspector.get_columns("users")
            if column["name"] == "member_search_key"
        )
        assert workspace_key["nullable"] is False
        assert member_key["nullable"] is False
        directory_index = next(
            index
            for index in inspector.get_indexes("workspace")
            if index["name"] == "ix_workspace_directory_title_key"
        )
        # pg8000/SQLAlchemy reflects this collated btree index as ordinary
        # column_names and omits the optional `expressions` field. Verify the
        # stable column order through reflection and the collation through
        # PostgreSQL's canonical index definition instead.
        assert directory_index["column_names"] == ["directory_title_key", "id"]
        with engine.connect() as connection:
            directory_index_definition = connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND indexname = :name"
                ),
                {"name": "ix_workspace_directory_title_key"},
            )
        assert directory_index_definition is not None
        assert 'directory_title_key COLLATE "C"' in directory_index_definition
        assert any(
            index["name"] == "ix_users_member_search_key"
            for index in inspector.get_indexes("users")
        )
        upgraded_metadata = MetaData()
        upgraded_users = Table("users", upgraded_metadata, autoload_with=engine)
        upgraded_workspaces = Table(
            "workspace", upgraded_metadata, autoload_with=engine
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(upgraded_workspaces.c.directory_title_key).where(
                        upgraded_workspaces.c.id == workspace_id
                    )
                )
                == "team strasse"
            )
            assert (
                connection.scalar(
                    select(upgraded_users.c.member_search_key).where(
                        upgraded_users.c.id == user_id
                    )
                )
                == "alice smith strasse@example.com"
            )
    finally:
        if upgraded:
            command.downgrade(config, "base")
        engine.dispose()


@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires a dedicated PostgreSQL migration test database",
)
def test_downgrade_refuses_when_oauth_sessions_exist():
    """A populated OAuth table must stop the downgrade, not crash it.

    0002 drops oauth_refresh_tokens.workspace_id and oauth_codes.workspace_id
    without preserving them, so the downgrade's NOT NULL re-add has nothing to
    fill. Before the refusal guard this aborted mid-downgrade with a raw
    "contains null values" from PostgreSQL. The round-trip test above never
    caught it because it seeds only users and one workspace, leaving both
    OAuth tables empty -- so this test exists to make an OAuth row part of
    what the migration suite actually exercises.
    """

    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    # `alembic_version` survives a `downgrade base`, so a sibling test having
    # already run in this database is fine; a domain table is not.
    initial_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert not initial_tables, (
        "DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL must name an empty, "
        f"dedicated database (found {sorted(initial_tables)})"
    )
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    upgraded = False
    try:
        command.upgrade(config, "0001_initial_release")
        upgraded = True
        metadata = MetaData()
        users = Table("users", metadata, autoload_with=engine)
        workspaces = Table("workspace", metadata, autoload_with=engine)
        clients = Table("oauth_clients", metadata, autoload_with=engine)
        refresh_tokens = Table(
            "oauth_refresh_tokens", metadata, autoload_with=engine
        )
        now = datetime.now(UTC)
        user_id = uuid4()
        workspace_id = uuid4()
        actor_id = uuid4()
        client_id = "oauth-downgrade-refusal-client"
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(
                    id=user_id,
                    email="oauth-downgrade@example.com",
                    password_hash="migration-test-hash",
                    display_name="OAuth Downgrade",
                    status="active",
                    session_generation=0,
                    default_workspace_id=None,
                    updated_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                workspaces.insert().values(
                    id=workspace_id,
                    slug="oauth-downgrade-refusal",
                    title="OAuth downgrade refusal",
                    name="OAuth downgrade refusal",
                    description=None,
                    goal="Migration coverage",
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
                    owner_user_id=None,
                    updated_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                clients.insert().values(
                    id=client_id,
                    client_name="Downgrade refusal client",
                    redirect_uris=["https://example.test/callback"],
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    application_type="native",
                    scope="mcp:read",
                    created_via="dynamic_registration",
                    last_used_at=None,
                    revoked_at=None,
                    created_at=now,
                )
            )
            connection.execute(
                refresh_tokens.insert().values(
                    token_lookup_hash="downgrade-refusal-lookup",
                    hashed_token="downgrade-refusal-hash",
                    user_id=user_id,
                    client_id=client_id,
                    workspace_id=workspace_id,
                    scope="mcp:read",
                    expires_at=now,
                    rotated_to_id=None,
                    revoked_at=None,
                    id=uuid4(),
                    created_at=now,
                )
            )
        command.upgrade(config, "head")

        with pytest.raises(RuntimeError, match="oauth_refresh_tokens still holds"):
            command.downgrade(config, "0001_initial_release")

        # The refusal must leave head intact, not a half-reverted schema.
        assert "grant_id" in {
            column["name"]
            for column in inspect(engine).get_columns("oauth_refresh_tokens")
        }
    finally:
        if upgraded:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM oauth_refresh_tokens"))
            command.downgrade(config, "base")
        engine.dispose()
