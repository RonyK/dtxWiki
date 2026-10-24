import os
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

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import Base

PARENT = "ef53a0a7061c"
REVISION = "7e2b9c4d6a18"
EMAIL_REVISION = "c31a6d907e42"


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

    upgraded = False
    try:
        command.upgrade(config, PARENT)
        upgraded = True
        users = sa.Table("users", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                _user(existing_id, "preserved-local-hash", "existing@example.com"),
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
        command.upgrade(config, REVISION)
        users, identities, transactions = _tables()
        inspector = inspect(engine)
        assert next(
            column
            for column in inspector.get_columns("users")
            if column["name"] == "password_hash"
        )["nullable"]
        assert ("issuer", "subject") in {
            tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("user_identities")
        }
        assert ("user_id", "provider_slug") in {
            tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("user_identities")
        }
        assert "ix_user_identities_provider_slug" in {
            index["name"] for index in inspector.get_indexes("user_identities")
        }
        assert "ix_auth_login_transactions_expires_at" in {
            index["name"] for index in inspector.get_indexes("auth_login_transactions")
        }
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
            "return_to": "/settings",
            "created_at": now,
            "expires_at": now + timedelta(minutes=10),
        }
        with engine.begin() as connection:
            connection.execute(
                users.insert(), _user(external_id, None, "external@example.com")
            )
            connection.execute(identities.insert(), identity)
            connection.execute(transactions.insert(), transaction)
            assert (
                connection.scalar(sa.select(identities.c.last_email_verified)) is False
            )
            assert connection.scalar(sa.select(transactions.c.remember_me)) is False
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

        command.upgrade(config, EMAIL_REVISION)
        # Compare against independent ORM metadata on both dialects. Restrict
        # this follow-up's parity assertion to its tables: PostgreSQL's frozen
        # library/workspace schema has unrelated redundant unique constraints.
        auth_tables = {
            "auth_pending_signups",
            "email_reauthentication_challenges",
            "email_outbox",
        }

        def include_auth_object(obj, name, kind, reflected, compare_to):
            if kind == "table":
                return name in auth_tables
            return getattr(getattr(obj, "table", None), "name", None) in auth_tables

        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_auth_object}
            )
            assert compare_metadata(context, Base.metadata) == []
        metadata = sa.MetaData()
        pending = sa.Table("auth_pending_signups", metadata, autoload_with=engine)
        challenges = sa.Table(
            "email_reauthentication_challenges", metadata, autoload_with=engine
        )
        outbox = sa.Table("email_outbox", metadata, autoload_with=engine)
        assert "ix_auth_pending_signups_expires_at" in {
            index["name"] for index in inspect(engine).get_indexes(pending.name)
        }
        assert "ix_email_reauth_expires_at" in {
            index["name"] for index in inspect(engine).get_indexes(challenges.name)
        }
        signup = {
            "id": _id(),
            "binding_digest": "pending-browser",
            "provider_slug": "example",
            "assertion_data": {"issuer": "https://issuer.example", "subject": "new"},
            "return_to": "/oauth/authorize?client_id=preserved",
            "token_digest": "pending-token",
            "created_at": now,
            "expires_at": now + timedelta(minutes=30),
        }
        challenge = {
            "id": _id(),
            "user_id": existing_id,
            "session_generation": 0,
            "session_digest": "native-session",
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
            "recipient_user_id": None,
            "pending_signup_id": signup["id"],
            "recipient_email": "pending@example.com",
            "message_kind": "pending_signup",
            "status": "queued",
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        reauth_message = {
            **message,
            "id": _id(),
            "recipient_user_id": existing_id,
            "pending_signup_id": None,
            "reauthentication_challenge_id": challenge["id"],
            "message_kind": "email_reauthentication",
        }
        with engine.begin() as connection:
            preserved = (
                connection.execute(
                    sa.select(outbox).where(outbox.c.id == preserved_outbox_id)
                )
                .mappings()
                .one()
            )
            assert preserved["recipient_user_id"] == existing_id
            assert preserved["attempt_count"] == 2
            assert preserved["pending_signup_id"] is None
            assert preserved["reauthentication_challenge_id"] is None
            connection.execute(pending.insert(), signup)
            connection.execute(challenges.insert(), challenge)
            connection.execute(outbox.insert(), message)
            connection.execute(outbox.insert(), reauth_message)
            assert connection.scalar(sa.select(pending.c.remember_me)) is False
            for table, values in (
                (pending, {**signup, "id": _id(), "token_digest": "other"}),
                (pending, {**signup, "id": _id(), "binding_digest": "other"}),
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
                (outbox, {**message, "id": _id(), "pending_signup_id": None}),
                (outbox, {**message, "id": _id(), "recipient_user_id": existing_id}),
                (outbox, {**message, "id": _id(), "message_kind": "password_changed"}),
                (outbox, {**message, "id": _id(), "pending_signup_id": _id()}),
                (outbox, {**message, "id": _id(), "token_id": _id()}),
                (outbox, {**reauth_message, "id": _id(), "recipient_user_id": None}),
                (
                    outbox,
                    {
                        **reauth_message,
                        "id": _id(),
                        "reauthentication_challenge_id": _id(),
                    },
                ),
            ):
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(table.insert(), values)

        with pytest.raises(RuntimeError, match="would discard data"):
            command.downgrade(config, REVISION)
        assert {pending.name, challenges.name} <= set(inspect(engine).get_table_names())
        # CASCADE and the two old outbox FKs must survive SQLite's rebuild.
        with engine.begin() as connection:
            connection.execute(pending.delete())
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(outbox)
                    .where(outbox.c.message_kind == "pending_signup")
                )
                == 0
            )
            connection.execute(challenges.delete())
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 1
            )
        command.downgrade(config, REVISION)
        assert {pending.name, challenges.name}.isdisjoint(
            inspect(engine).get_table_names()
        )
        old_outbox = sa.Table("email_outbox", sa.MetaData(), autoload_with=engine)
        assert not old_outbox.c.recipient_user_id.nullable
        assert "pending_signup_id" not in old_outbox.c
        with engine.begin() as connection:
            assert connection.scalar(sa.select(old_outbox.c.attempt_count)) == 2
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(
                    old_outbox.insert(),
                    {
                        **{
                            key: value
                            for key, value in message.items()
                            if key != "pending_signup_id"
                        },
                        "id": _id(),
                        "recipient_user_id": existing_id,
                    },
                )
        with pytest.raises(RuntimeError, match="1 passwordless account"):
            command.downgrade(config, PARENT)
        assert {"user_identities", "auth_login_transactions"} <= set(
            inspect(engine).get_table_names()
        )
        with engine.begin() as connection:
            assert (
                connection.scalar(
                    sa.select(users.c.password_hash).where(users.c.id == existing_id)
                )
                == "preserved-local-hash"
            )
            # Deleting the native account removes identities and bound attempts.
            connection.execute(users.delete().where(users.c.id == external_id))
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(identities))
                == 0
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(transactions))
                == 0
            )
        command.downgrade(config, PARENT)
        assert {"user_identities", "auth_login_transactions"}.isdisjoint(
            inspect(engine).get_table_names()
        )
        assert not next(
            column
            for column in inspect(engine).get_columns("users")
            if column["name"] == "password_hash"
        )["nullable"]
        command.upgrade(config, EMAIL_REVISION)
        users, _, _ = _tables()
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.select(users.c.password_hash))
                == "preserved-local-hash"
            )
    finally:
        if upgraded:
            # A failed assertion must still leave the dedicated database empty.
            with engine.begin() as connection:
                present = set(inspect(connection).get_table_names())
                for table in (
                    "email_outbox",
                    "email_reauthentication_challenges",
                    "auth_pending_signups",
                ):
                    if table in present:
                        connection.execute(sa.text(f"DELETE FROM {table}"))
                connection.execute(
                    sa.text("DELETE FROM users WHERE password_hash IS NULL")
                )
            command.downgrade(config, "base")
        engine.dispose()
