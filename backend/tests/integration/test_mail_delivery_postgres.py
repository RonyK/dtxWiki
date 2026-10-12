import os
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.user import User
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import (
    EmailDeliveryService,
    MailDeliveryError,
    OutboundMail,
)
from backend.services.password_recovery_service import PasswordRecoveryService
from backend.services.personal_workspace_service import ensure_personal_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

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


class BlockingProvider:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.send_count = 0

    def send(self, _message: OutboundMail) -> None:
        with self._lock:
            self.send_count += 1
        self.entered.set()
        assert self.release.wait(timeout=10)


@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
def test_postgres_outbox_lock_prevents_cli_worker_double_send():
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        database_url=database_url,
        public_base_url="https://wiki.example.com",
        session_secret="postgres-mail-lock-test-secret",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )
    provider = BlockingProvider()
    delivery = EmailDeliveryService(settings=settings, provider=provider)
    errors: list[BaseException] = []
    try:
        with session_factory.begin() as session:
            seed_default_workspace(session)
            user = User(
                email="postgres-mail-lock@example.com",
                password_hash=CredentialHasher().hash_password("correct-password"),
                display_name="Postgres mail lock",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
            ensure_personal_workspace(session, user=user)
        recovery = PasswordRecoveryService(
            session_factory=session_factory,
            credential_hasher=CredentialHasher(),
            public_base_url=settings.public_base_url,
            mail_delivery_service=delivery,
        )
        issued = recovery.issue_password_reset(
            email="postgres-mail-lock@example.com",
            delivery="email",
        )
        assert issued.outbox_id is not None

        def deliver_once() -> None:
            try:
                delivery.deliver(
                    session_factory=session_factory, outbox_id=issued.outbox_id
                )
            except BaseException as exc:  # asserted below
                errors.append(exc)

        cli_thread = threading.Thread(target=deliver_once)
        worker_thread = threading.Thread(target=deliver_once)
        cli_thread.start()
        try:
            assert provider.entered.wait(timeout=10)
            # The worker runs to completion while the CLI delivery is parked
            # in the provider with no transaction open. Released any earlier,
            # the CLI delivery could settle 'sent' before the worker's claim,
            # and the worker would then return quietly instead of raising.
            worker_thread.start()
            worker_thread.join(timeout=10)
            assert not worker_thread.is_alive()
        finally:
            provider.release.set()
        cli_thread.join(timeout=10)

        assert not cli_thread.is_alive()
        # MAIL-01 commits the claim before the send, so the loser of the race
        # now sees "sending" and says so instead of blocking on the winner's
        # write lock until the row reads "sent". Exactly one thread must lose:
        # the second deliver() only starts after the first has entered the
        # provider, so the row is already committed as 'sending' and the
        # loser's guarded UPDATE cannot match. "<= 1" also passed when NEITHER
        # thread raised, which would mean both had claimed the row.
        assert len(errors) == 1
        assert all(isinstance(error, MailDeliveryError) for error in errors)
        assert provider.send_count == 1
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()
