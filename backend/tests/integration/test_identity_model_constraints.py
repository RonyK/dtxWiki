import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.models import ApiToken, Base, User


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
