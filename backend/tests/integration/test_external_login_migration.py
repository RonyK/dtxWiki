# Budget exception: one complete upgrade/downgrade/re-upgrade traversal per DB
# covers the single external-login revision. Per dialect: 17 invalid storage
# writes, 3 incompatible credential states, 4 isolated evidence guards and
# 1 restored legacy mail-kind check (25 rejection inputs; 50 across both DBs).
# Each constraint/table can break independently; SQLite CHECK/FK rebuilds and
# PostgreSQL transactional DDL require real DBs, within the same traversal.
import os
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

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import Base
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.ops.input_maintenance import audit_inputs

PARENT = "5c3395a75eb0"
REVISION = "da94025713de"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_external_login_upgrade_downgrade_reupgrade(tmp_path, dialect):
    """One traversal per dialect covers data, defaults, constraints and refusal."""

    database_url = (
        f"sqlite+pysqlite:///{tmp_path / 'external-login.db'}"
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
                sa.select(
                    sa.Table("workspace", sa.MetaData(), autoload_with=connection)
                )
            ).all(),
            connection.execute(sa.select(WorkspaceMembership.__table__)).all(),
        )

    upgraded = False
    try:
        command.upgrade(config, PARENT)
        upgraded = True
        users = sa.Table("users", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                Workspace.__table__.insert(),
                {
                    "id": workspace_id,
                    "slug": "retained-workspace",
                    "name": "Retained workspace",
                    "title": "Retained workspace",
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
            connection.execute(
                Workspace.__table__.update().values(
                    owner_user_id=UUID(str(existing_id))
                )
            )
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
            retained = retained_account_state(connection)
        command.upgrade(config, REVISION)
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
            # Check each independent storage boundary without another migration.
            for table, values in (
                (identities, {**identity, "id": _id(), "user_id": existing_id}),
                (identities, {**identity, "id": _id(), "subject": "another-subject"}),
                (
                    identities,
                    {**identity, "id": _id(), "user_id": _id(), "subject": "orphan"},
                ),
                (
                    identities,
                    {
                        **identity,
                        "id": _id(),
                        "provider_slug": "other",
                        "subject": "other",
                        "linked_via": "untrusted",
                    },
                ),
                (transactions, {**transaction, "id": _id()}),
                (
                    transactions,
                    {
                        **transaction,
                        "id": _id(),
                        "state_digest": "other",
                        "purpose": "unsupported",
                    },
                ),
            ):
                with pytest.raises(DBAPIError) as rejected, connection.begin_nested():
                    connection.execute(table.insert(), values)
                if dialect == "postgresql":
                    # pg8000 maps FK/CHECK violations to ProgrammingError;
                    # assert SQLSTATE rather than a driver-specific subclass.
                    assert rejected.value.orig.args[0]["C"] in {
                        "23505",
                        "23503",
                        "23514",
                    }

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
            for table, values in (
                (challenges, {**challenge, "id": _id()}),
                (
                    challenges,
                    {
                        **challenge,
                        "id": _id(),
                        "token_digest": "scope",
                        "scope": "login",
                    },
                ),
                (
                    challenges,
                    {
                        **challenge,
                        "id": _id(),
                        "token_digest": "orphan",
                        "user_id": _id(),
                    },
                ),
                (outbox, {**message, "id": _id(), "recipient_user_id": None}),
                (
                    outbox,
                    {**message, "id": _id(), "reauthentication_challenge_id": None},
                ),
                (
                    outbox,
                    {**message, "id": _id(), "reauthentication_challenge_id": _id()},
                ),
                (outbox, {**message, "id": _id(), "message_kind": "pending_signup"}),
                (outbox, {**message, "id": _id(), "message_kind": "password_changed"}),
                (outbox, {**message, "id": _id(), "token_id": _id()}),
                (conflicts, {**conflict, "id": _id(), "status": "merged"}),
                (conflicts, {**conflict, "id": _id(), "target_user_id": _id()}),
            ):
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(table.insert(), values)

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

        # Each nullable credential/pending-email state has a distinct refusal.
        for credentials in (
            {
                "password_hash": None,
                "email": "external@example.com",
                "pending_email": None,
            },
            {
                "password_hash": "external-local-hash",
                "email": None,
                "pending_email": None,
            },
            {
                "password_hash": "external-local-hash",
                "email": "external@example.com",
                "pending_email": "pending@example.com",
            },
        ):
            with engine.begin() as connection:
                connection.execute(
                    users.update()
                    .where(users.c.id == external_id)
                    .values(**credentials)
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
        for table, values in (
            (conflicts, conflict),
            (identities, identity),
            (transactions, transaction),
        ):
            with engine.begin() as connection:
                connection.execute(table.insert(), values)
            assert_downgrade_refused(table.name)
            with engine.begin() as connection:
                connection.execute(table.delete())

        command.downgrade(config, PARENT)
        assert (auth_tables - {"users", "email_outbox"}).isdisjoint(
            inspect(engine).get_table_names()
        )
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("users")
        }
        assert not columns["email"]["nullable"]
        assert not columns["password_hash"]["nullable"]
        assert "pending_email" not in columns
        assert "personal_workspace_id" not in columns
        old_outbox = sa.Table("email_outbox", sa.MetaData(), autoload_with=engine)
        assert "reauthentication_challenge_id" not in old_outbox.c
        with engine.begin() as connection:
            assert retained_account_state(connection) == retained
            assert connection.scalar(sa.select(old_outbox.c.attempt_count)) == 2
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    old_outbox.insert(),
                    {
                        key: value
                        for key, value in message.items()
                        if key != "reauthentication_challenge_id"
                    },
                )
        command.upgrade(config, REVISION)
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
