import os
import threading
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Update

from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.password_reset import PasswordResetToken
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import server_admin_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService, OutboundMail
from backend.services.password_recovery_service import (
    PasswordRecoveryAuthorizationError,
    PasswordRecoveryService,
)
from backend.services.personal_workspace_service import create_personal_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata


class _NoopMailProvider:
    def send(self, _message: OutboundMail) -> None:
        return None


def _raw_token(url: str) -> str:
    # M-2: recovery links carry the token in the fragment, never the query.
    parts = urlsplit(url)
    assert parts.query == ""
    return parse_qs(parts.fragment, strict_parsing=True)["token"][0]


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


def _updates_password_reset_tokens(statement: object) -> bool:
    return isinstance(statement, Update) and statement.table.is_derived_from(
        PasswordResetToken.__table__
    )


def test_password_reset_update_hook_matches_orm_annotated_table_only():
    assert _updates_password_reset_tokens(update(PasswordResetToken))
    assert not _updates_password_reset_tokens(update(User))


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
def test_postgres_admin_demotion_serializes_before_reset_issue():
    database_url = _postgres_url()
    assert database_url is not None
    grant_lookup_started = threading.Event()

    class TrackingSession(Session):
        def scalar(self, statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", ())
            if (
                threading.current_thread().name == "reset-issue"
                and descriptions
                and descriptions[0].get("entity") is ServerAdmin
            ):
                grant_lookup_started.set()
            return super().scalar(statement, *args, **kwargs)

    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        expire_on_commit=False,
    )
    grant_deleted = threading.Event()
    release_demotion_commit = threading.Event()
    errors: list[BaseException] = []
    try:
        with sessions.begin() as session:
            seed_default_workspace(session)
            admin = User(
                email="postgres-admin-race@example.com",
                password_hash=CredentialHasher().hash_password("correct-password"),
                display_name="Admin race",
                status="active",
            )
            target = User(
                email="postgres-reset-target@example.com",
                password_hash=CredentialHasher().hash_password("target-password"),
                display_name="Reset target",
                status="active",
            )
            session.add_all((admin, target))
            session.flush()
            create_personal_workspace(session, user=admin)
            create_personal_workspace(session, user=target)
            server_admin_service.grant_server_admin(
                session,
                admin.id,
                granted_by="test",
            )
            admin_id = admin.id
            admin_session_generation = admin.session_generation
            target_id = target.id

        recovery = PasswordRecoveryService(
            session_factory=sessions,
            credential_hasher=CredentialHasher(),
            public_base_url="https://wiki.example.com",
        )

        def demote() -> None:
            with sessions.begin() as session:
                assert server_admin_service.revoke_server_admin(session, admin_id)
                grant_deleted.set()
                assert release_demotion_commit.wait(timeout=10)

        def issue() -> None:
            try:
                recovery.issue_password_reset_for_user(
                    target_user_id=target_id,
                    delivery="link",
                    requested_by_user_id=admin_id,
                    requested_by_session_generation=admin_session_generation,
                    actor_kind="human",
                    actor_id=str(admin_id),
                )
            except BaseException as exc:  # asserted below
                errors.append(exc)

        demotion_thread = threading.Thread(target=demote, name="admin-demotion")
        issue_thread = threading.Thread(target=issue, name="reset-issue")
        demotion_thread.start()
        assert grant_deleted.wait(timeout=10)
        issue_thread.start()
        assert grant_lookup_started.wait(timeout=10)
        release_demotion_commit.set()
        demotion_thread.join(timeout=10)
        issue_thread.join(timeout=10)

        assert not demotion_thread.is_alive()
        assert not issue_thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], PasswordRecoveryAuthorizationError)
        with sessions() as session:
            assert (
                session.scalar(select(func.count()).select_from(PasswordResetToken))
                == 0
            )
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
@pytest.mark.parametrize("purpose", ["password_reset", "email_verification"])
def test_postgres_consume_claim_holds_user_before_reissue_token_lock(purpose):
    database_url = _postgres_url()
    assert database_url is not None
    consume_token_claimed = threading.Event()
    reissue_user_lookup_started = threading.Event()
    reissue_token_lookup_started = threading.Event()
    release_consume = threading.Event()

    class TrackingSession(Session):
        def scalar(self, statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", ())
            is_user_lookup = descriptions and descriptions[0].get("entity") is User
            is_token_lookup = (
                descriptions and descriptions[0].get("entity") is PasswordResetToken
            )
            thread_name = threading.current_thread().name
            if thread_name == "token-reissue" and is_user_lookup:
                reissue_user_lookup_started.set()
            if thread_name == "token-reissue" and is_token_lookup:
                reissue_token_lookup_started.set()
            return super().scalar(statement, *args, **kwargs)

        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if (
                threading.current_thread().name == "token-consume"
                and _updates_password_reset_tokens(statement)
            ):
                consume_token_claimed.set()
                assert release_consume.wait(timeout=10)
            return result

    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        expire_on_commit=False,
    )
    errors: list[BaseException] = []
    consume_results = []
    reissue_results = []
    try:
        with sessions.begin() as session:
            seed_default_workspace(session)
            user = User(
                email=f"postgres-{purpose}@example.com",
                password_hash=CredentialHasher().hash_password("original-password"),
                display_name="Token race",
                status="active",
                email_verified_at=(
                    datetime.now(UTC) if purpose == "password_reset" else None
                ),
            )
            session.add(user)
            session.flush()
            user_id = user.id

        mail_delivery = EmailDeliveryService(
            settings=Settings(
                public_base_url="https://wiki.example.com",
                session_secret="postgres-recovery-race-secret-with-enough-entropy",
                mail_provider="smtp",
                mail_from_address="wiki@example.com",
                smtp_host="smtp.example.com",
            ),
            provider=_NoopMailProvider(),
        )
        recovery = PasswordRecoveryService(
            session_factory=sessions,
            credential_hasher=CredentialHasher(),
            public_base_url="https://wiki.example.com",
            mail_delivery_service=mail_delivery,
        )
        if purpose == "password_reset":
            initial = recovery.issue_password_reset(
                email=f"postgres-{purpose}@example.com",
                delivery="link",
            )
        else:
            initial = recovery.issue_email_verification(
                user_id=user_id,
                actor_id=str(user_id),
            )
        raw_token = _raw_token(initial.reset_url)

        def consume() -> None:
            try:
                if purpose == "password_reset":
                    consume_results.append(
                        recovery.consume_password_reset(
                            token=raw_token,
                            new_password="replacement-password",
                        )
                    )
                else:
                    consume_results.append(
                        recovery.consume_email_verification(token=raw_token)
                    )
            except BaseException as exc:  # asserted below
                errors.append(exc)

        def reissue() -> None:
            try:
                if purpose == "password_reset":
                    reissue_results.append(
                        recovery.issue_password_reset(
                            email=f"postgres-{purpose}@example.com",
                            delivery="link",
                        )
                    )
                else:
                    reissue_results.append(
                        recovery.issue_email_verification(
                            user_id=user_id,
                            actor_id=str(user_id),
                        )
                    )
            except BaseException as exc:  # asserted below
                errors.append(exc)

        consume_thread = threading.Thread(target=consume, name="token-consume")
        reissue_thread = threading.Thread(target=reissue, name="token-reissue")
        consume_thread.start()
        assert consume_token_claimed.wait(timeout=10)
        reissue_thread.start()
        assert reissue_user_lookup_started.wait(timeout=10)
        # With the corrected User -> Token order, reissue blocks acquiring User
        # and cannot even attempt its token lookup until consume commits.  The
        # former Token -> User consume order instead lets reissue lock User and
        # reach this token lookup while consume still owns Token, constructing
        # the exact ABBA wait that PostgreSQL resolves as a deadlock.
        reached_token_before_consume_release = reissue_token_lookup_started.wait(
            timeout=1
        )
        release_consume.set()
        consume_thread.join(timeout=10)
        reissue_thread.join(timeout=10)

        assert not consume_thread.is_alive()
        assert not reissue_thread.is_alive()
        assert not reached_token_before_consume_release
        assert errors == []
        assert len(consume_results) == 1
        assert len(reissue_results) == 1
        with sessions() as session:
            user = session.get(User, user_id)
            assert user is not None
            initial_token = session.get(PasswordResetToken, initial.token_id)
            assert initial_token is not None and initial_token.used_at is not None
            active_count = session.scalar(
                select(func.count())
                .select_from(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.purpose == purpose,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.revoked_at.is_(None),
                )
            )
            if purpose == "password_reset":
                assert active_count == 1
                assert CredentialHasher().verify_password(
                    "replacement-password", user.password_hash
                )
            else:
                assert active_count == 0
                assert user.email_verified_at is not None
    finally:
        release_consume.set()
        drop_reflected_postgres_metadata(engine)
        engine.dispose()
