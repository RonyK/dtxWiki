from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.models import ApiToken, Base, User


def test_initial_release_adds_user_and_api_token_tables(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'identity.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    token_columns = {
        column["name"] for column in inspector.get_columns("api_tokens")
    }
    user_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("users")
    }
    token_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("api_tokens")
    }

    assert {"users", "api_tokens"}.issubset(tables)
    assert {
        "id",
        "email",
        "password_hash",
        "display_name",
        "status",
        "created_at",
        "updated_at",
    }.issubset(user_columns)
    assert {
        "id",
        "user_id",
        "name",
        "token_lookup_hash",
        "hashed_token",
        "scopes",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
    }.issubset(token_columns)
    assert ("email",) in user_uniques
    assert ("token_lookup_hash",) in token_uniques


def test_user_and_api_token_models_roundtrip():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    with session_factory.begin() as session:
        user = User(
            email="alice@example.com",
            password_hash="argon2id:placeholder",
            display_name="Alice",
        )
        session.add(user)
        session.flush()
        session.add(
            ApiToken(
                user_id=user.id,
                name="ci",
                token_lookup_hash="sha256:lookup",
                hashed_token="argon2id:token",
                scopes=["mcp:read", "mcp:write"],
                expires_at=expires_at,
            )
        )

    with session_factory() as session:
        persisted_user = session.scalar(
            select(User).where(User.email == "alice@example.com")
        )
        assert persisted_user is not None
        assert persisted_user.status == "active"
        assert persisted_user.api_tokens[0].name == "ci"
        assert persisted_user.api_tokens[0].scopes == ["mcp:read", "mcp:write"]
        assert persisted_user.api_tokens[0].expires_at is not None


def test_user_email_and_token_lookup_hash_are_unique():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            first = User(
                email="alice@example.com",
                password_hash="argon2id:first",
                display_name="Alice",
            )
            second = User(
                email="bob@example.com",
                password_hash="argon2id:second",
                display_name="Bob",
            )
            session.add_all([first, second])
            session.flush()
            session.add_all(
                [
                    ApiToken(
                        user_id=first.id,
                        name="first",
                        token_lookup_hash="sha256:same",
                        hashed_token="argon2id:first-token",
                        scopes=["mcp:read"],
                    ),
                    ApiToken(
                        user_id=second.id,
                        name="second",
                        token_lookup_hash="sha256:same",
                        hashed_token="argon2id:second-token",
                        scopes=["mcp:read"],
                    ),
                ]
            )
            session.flush()

    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add_all(
                [
                    User(
                        email="duplicate@example.com",
                        password_hash="argon2id:first",
                        display_name="First",
                    ),
                    User(
                        email="duplicate@example.com",
                        password_hash="argon2id:second",
                        display_name="Second",
                    ),
                ]
            )
