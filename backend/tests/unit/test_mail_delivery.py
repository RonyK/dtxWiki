import re
import ssl
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.job import Job
from backend.models.mail import EmailOutbox, PasswordRecoveryRateLimitSlot
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.credential_hashing import CredentialHasher
from backend.services.job_queue import (
    DEFAULT_JOB_MAX_ATTEMPTS,
    JobQueueService,
)
from backend.services.mail_delivery import (
    EmailDeliveryService,
    MailDeliveryConfigurationError,
    MailDeliveryError,
    MailProviderError,
    OutboundMail,
    SMTPMailProvider,
    job_failure_outbox_id,
)
from backend.services.password_recovery_service import (
    InvalidPasswordResetTokenError,
    PasswordRecoveryRateLimiter,
    PasswordRecoveryRateLimitError,
    PasswordRecoveryService,
    UsedPasswordResetTokenError,
)


class RecordingProvider:
    def __init__(self) -> None:
        self.messages: list[OutboundMail] = []

    def send(self, message: OutboundMail) -> None:
        self.messages.append(message)


class FailingProvider:
    def send(self, message: OutboundMail) -> None:
        raise MailProviderError("mail provider delivery failed")


class CountingFailingProvider:
    """Always fails, and counts every provider contact -- the measurement the
    retry budget is supposed to bound."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: OutboundMail) -> None:
        self.calls += 1
        raise MailProviderError("mail provider delivery failed")


class BlockingProvider:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.send_count = 0
        self._lock = threading.Lock()

    def send(self, message: OutboundMail) -> None:
        with self._lock:
            self.send_count += 1
        self.entered.set()
        assert self.release.wait(timeout=5)


class RecordingSMTPClient:
    def __init__(self) -> None:
        self.ehlo_calls = 0
        self.starttls_context = None
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, *, context) -> None:
        self.starttls_context = context

    def login(self, _username: str, _password: str) -> None:
        return None

    def send_message(self, message) -> None:
        self.sent.append(message)


def mail_settings() -> Settings:
    return Settings(
        public_base_url="https://wiki.example.com",
        session_secret="test-mail-session-secret-with-enough-entropy",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )


def add_verified_user(session_factory, email: str = "verified@example.com") -> User:
    with session_factory.begin() as session:
        user = User(
            email=email,
            password_hash=CredentialHasher().hash_password("original-password"),
            display_name="Verified user",
            status="active",
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        user_id = user.id
    with session_factory() as session:
        return session.get(User, user_id)


EMAILED_LINK = re.compile(r"https://\S+")


def _emailed_link_token(text_body: str, *, path: str) -> str:
    """Return the token of the one link in a recovery email.

    M-2: the link must carry the token in the fragment only. A `?token=` link
    wrote a live credential into every access log the click passed through.
    """

    links = EMAILED_LINK.findall(text_body)
    assert len(links) == 1
    parts = urlsplit(links[0])
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "wiki.example.com",
        path,
    )
    assert parts.query == ""
    fragment = parse_qs(parts.fragment, strict_parsing=True)
    assert fragment.keys() == {"token"}
    return fragment["token"][0]


def test_smtp_starttls_uses_hostname_verifying_context(monkeypatch):
    client = RecordingSMTPClient()
    monkeypatch.setattr(
        "backend.services.mail_delivery.smtplib.SMTP",
        lambda *_args, **_kwargs: client,
    )

    SMTPMailProvider(host="smtp.example.com", port=587).send(
        OutboundMail(
            recipient="recipient@example.com",
            sender="wiki@example.com",
            subject="Recovery",
            text_body="body",
        )
    )

    assert client.starttls_context is not None
    assert client.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert client.starttls_context.check_hostname is True
    assert client.ehlo_calls == 2
    assert len(client.sent) == 1


@pytest.mark.parametrize(
    ("starttls", "use_ssl"),
    [(False, False), (True, True)],
)
def test_smtp_rejects_missing_or_ambiguous_tls_mode(starttls, use_ssl):
    with pytest.raises(ValueError, match="exactly one secure transport"):
        Settings(
            public_base_url="https://wiki.example.com",
            mail_provider="smtp",
            mail_from_address="wiki@example.com",
            smtp_host="smtp.example.com",
            smtp_starttls=starttls,
            smtp_use_ssl=use_ssl,
        )

    with pytest.raises(
        MailDeliveryConfigurationError,
        match="exactly one secure transport",
    ):
        SMTPMailProvider(
            host="smtp.example.com",
            port=587,
            starttls=starttls,
            use_ssl=use_ssl,
        )


def test_smtp_ssl_uses_hostname_verifying_context(monkeypatch):
    client = RecordingSMTPClient()
    captured = {}

    def make_ssl_client(*_args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(
        "backend.services.mail_delivery.smtplib.SMTP_SSL",
        make_ssl_client,
    )

    SMTPMailProvider(
        host="smtp.example.com",
        port=465,
        starttls=False,
        use_ssl=True,
    ).send(
        OutboundMail(
            recipient="recipient@example.com",
            sender="wiki@example.com",
            subject="Recovery",
            text_body="body",
        )
    )

    context = captured["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert client.starttls_context is None
    assert client.ehlo_calls == 1
    assert len(client.sent) == 1


def test_email_reset_uses_encrypted_outbox_and_no_worker_job(session_factory):
    """INVERTED by MAIL-02: this used to assert an email_delivery Job WAS
    enqueued alongside the outbox row. That Job was workspace-plane data
    written from a control-plane session, unclaimable under a split, so the
    assertion now runs the other way -- no Job is created at all, and the
    control-plane sweep is what delivers."""
    user = add_verified_user(session_factory)
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )

    result = recovery.issue_password_reset(
        email=user.email,
        delivery="email",
        requested_via="public",
        actor_kind="human",
        actor_id="public:password-recovery",
    )

    assert result.outbox_id is not None
    raw_token = result.reset_url.split("token=", 1)[1]
    with session_factory() as session:
        token = session.get(PasswordResetToken, result.token_id)
        outbox = session.get(EmailOutbox, result.outbox_id)
        assert token is not None and raw_token not in token.token_digest
        assert outbox is not None and raw_token not in (outbox.encrypted_token or "")
        assert session.scalar(select(func.count()).select_from(Job)) == 0

    delivery.deliver(session_factory=session_factory, outbox_id=result.outbox_id)

    assert len(provider.messages) == 1
    assert raw_token in provider.messages[0].text_body
    assert _emailed_link_token(
        provider.messages[0].text_body,
        path="/reset-password",
    ) == raw_token
    with session_factory() as session:
        outbox = session.get(EmailOutbox, result.outbox_id)
        assert outbox is not None and outbox.status == "sent"
        assert outbox.encrypted_token is None
        status = delivery.status(session)
        assert status.queued == 0
        assert status.failed == 0
        assert status.sent == 1
        assert status.latest_attempt_status == "sent"
        assert status.latest_attempt_at is not None


def test_email_verification_mail_carries_the_token_only_in_the_fragment(
    session_factory,
):
    user = add_verified_user(session_factory, "verify-link@example.com")
    with session_factory.begin() as session:
        session.get(User, user.id).email_verified_at = None
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )
    issued = recovery.issue_email_verification(
        user_id=user.id,
        actor_id=str(user.id),
    )

    delivery.deliver(session_factory=session_factory, outbox_id=issued.outbox_id)

    assert len(provider.messages) == 1
    token = _emailed_link_token(
        provider.messages[0].text_body,
        path="/verify-email",
    )
    assert token.startswith("dtxe_")
    # The emailed fragment carries the live credential.
    recovery.consume_email_verification(token=token)
    with session_factory() as session:
        assert session.get(User, user.id).email_verified_at is not None


def test_sqlite_delivery_claim_prevents_concurrent_provider_send(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'mail-claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    sessions = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    try:
        with sessions.begin() as session:
            user = User(
                email="claim@example.com",
                password_hash=CredentialHasher().hash_password("original-password"),
                display_name="Claim test",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
            outbox = EmailOutbox(
                recipient_user_id=user.id,
                recipient_email=user.email,
                message_kind="password_changed",
                status="queued",
            )
            session.add(outbox)
            session.flush()
            outbox_id = outbox.id

        provider = BlockingProvider()
        delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
        failures: list[BaseException] = []

        def deliver() -> None:
            try:
                delivery.deliver(session_factory=sessions, outbox_id=outbox_id)
            except BaseException as exc:  # asserted below
                failures.append(exc)

        first = threading.Thread(target=deliver, name="first-delivery")
        second = threading.Thread(target=deliver, name="second-delivery")
        first.start()
        try:
            assert provider.entered.wait(timeout=5)
            # The first claim is committed and its thread is parked in the
            # provider with no transaction open, so the second delivery runs
            # to completion now, against a row that still reads 'sending'.
            # Releasing the provider any earlier let the first settle 'sent'
            # ahead of the second's claim, which then returned quietly.
            second.start()
            second.join(timeout=5)
            assert not second.is_alive()
        finally:
            provider.release.set()
        first.join(timeout=5)

        assert not first.is_alive()
        # BEHAVIOUR CHANGE (MAIL-01): the loser used to block on the winner's
        # write lock until the row already read "sent" and then return it
        # quietly. The claim commits before the send now, so the loser sees
        # "sending" instead and says so. What this test actually guards is
        # unchanged and is the line below it: no interleaving can make the
        # provider send twice.
        assert len(failures) == 1
        assert isinstance(failures[0], MailDeliveryError)
        assert "already claimed" in str(failures[0])
        assert provider.send_count == 1
        with sessions() as session:
            persisted = session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "sent"
            assert persisted.attempt_count == 1
    finally:
        engine.dispose()


def test_sweep_due_delivers_outbox_row_with_no_job_anywhere(session_factory):
    """The property this whole change exists for: queueing an email creates an
    outbox row and NOTHING else, and the sweep alone finds and delivers it.

    Previously the enqueue had to be monkeypatched away to simulate the Job
    landing on a plane the worker's fan-out never visits once the database is
    sharded per workspace (D-6/D-9). MAIL-02 deleted the Job, so the
    simulation is now just the real behaviour."""
    user = add_verified_user(session_factory, "sweep-no-job@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)

    with session_factory.begin() as session:
        outbox = delivery.queue_password_changed_notification(
            session=session, user=user
        )
        outbox_id = outbox.id
        # No Job was created for this outbox at all -- not merely purged.
        assert session.scalar(select(func.count()).select_from(Job)) == 0

    delivered = delivery.sweep_due(session_factory=session_factory)
    assert delivered == 1

    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        assert outbox is not None
        assert outbox.status == "sent"
    assert len(provider.messages) == 1
    assert provider.messages[0].recipient == user.email


def test_sweep_due_respects_next_attempt_at_backoff(session_factory):
    user = add_verified_user(session_factory, "sweep-backoff@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)

    with session_factory.begin() as session:
        not_yet_due = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="password_changed",
            status="failed",
            next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(not_yet_due)
        session.flush()
        not_yet_due_id = not_yet_due.id

    delivered = delivery.sweep_due(session_factory=session_factory)

    assert delivered == 0
    assert provider.messages == []
    with session_factory() as session:
        outbox = session.get(EmailOutbox, not_yet_due_id)
        assert outbox.status == "failed"


def test_sweep_due_does_not_resend_a_sent_row(session_factory):
    user = add_verified_user(session_factory, "sweep-sent@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)

    with session_factory.begin() as session:
        already_sent = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="password_changed",
            status="sent",
            sent_at=datetime.now(UTC),
        )
        session.add(already_sent)
        session.flush()

    delivered = delivery.sweep_due(session_factory=session_factory)

    assert delivered == 0
    assert provider.messages == []


def test_sweep_and_a_direct_delivery_cannot_double_send(tmp_path):
    """Proves the sweep and a concurrent direct delivery share exactly ONE
    claim protocol -- _claim_for_send's guarded UPDATE -- not a second lock
    the sweep introduces. Same interleaving shape as
    test_sqlite_delivery_claim_prevents_concurrent_provider_send, but pits
    sweep_due() against a direct deliver() call (the shape the CLI's
    immediate delivery uses) instead of two direct deliver() calls."""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'mail-sweep-claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    sessions = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    try:
        with sessions.begin() as session:
            user = User(
                email="sweep-claim@example.com",
                password_hash=CredentialHasher().hash_password("original-password"),
                display_name="Sweep claim test",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
            outbox = EmailOutbox(
                recipient_user_id=user.id,
                recipient_email=user.email,
                message_kind="password_changed",
                status="queued",
            )
            session.add(outbox)
            session.flush()
            outbox_id = outbox.id

        provider = BlockingProvider()
        delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
        failures: list[BaseException] = []
        sweep_delivered: list[int] = []

        def run_sweep() -> None:
            try:
                sweep_delivered.append(
                    delivery.sweep_due(session_factory=sessions)
                )
            except BaseException as exc:  # asserted below
                failures.append(exc)

        def run_direct_deliver() -> None:
            try:
                delivery.deliver(session_factory=sessions, outbox_id=outbox_id)
            except BaseException as exc:  # asserted below
                failures.append(exc)

        sweep_thread = threading.Thread(target=run_sweep, name="sweep-delivery")
        direct_thread = threading.Thread(
            target=run_direct_deliver, name="direct-delivery"
        )
        sweep_thread.start()
        try:
            assert provider.entered.wait(timeout=5)
            # As in the two-deliver test: the direct call finishes while the
            # sweep is still parked in the provider, so it cannot meet a row
            # the sweep has already settled.
            direct_thread.start()
            direct_thread.join(timeout=5)
            assert not direct_thread.is_alive()
        finally:
            provider.release.set()
        sweep_thread.join(timeout=5)

        assert not sweep_thread.is_alive()
        # The sweep's claim is already committed by the time the direct call
        # runs its own guarded UPDATE, so that UPDATE matches nothing and the
        # direct call reports the live claim rather than quietly returning a
        # row someone else is mid-send on. The double-send guard is the
        # send_count line: no interleaving of two CONCURRENT claimants reaches
        # the provider twice. That is the whole of the guarantee -- it is not
        # "no path ever sends the same message twice", which is false: see
        # test_lease_recovery_of_an_accepted_send_is_at_least_once and the
        # note on reclaim_stale_sending.
        assert len(failures) == 1
        assert isinstance(failures[0], MailDeliveryError)
        assert sweep_delivered == [1]
        assert provider.send_count == 1
        with sessions() as session:
            persisted = session.get(EmailOutbox, outbox_id)
            assert persisted is not None
            assert persisted.status == "sent"
            assert persisted.attempt_count == 1
    finally:
        engine.dispose()


def test_render_job_failed_names_job_type_with_no_job_row_reachable(session_factory):
    """_render must name the failed job's type from the row itself
    (source_job_type), never by looking up a Job -- proven here by never
    creating one at all, not just letting it be purged: 0014's old
    source_job_id->job.id FK degraded to a generic body only once the row
    was ALREADY gone, but the new column never needs the lookup in the
    first place."""
    user = add_verified_user(session_factory, "job-failed-render@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)

    with session_factory.begin() as session:
        outbox = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="job_failed",
            source_job_type="library_conversion",
            status="queued",
        )
        session.add(outbox)
        session.flush()
        outbox_id = outbox.id
        assert session.scalar(select(func.count()).select_from(Job)) == 0

    delivery.deliver(session_factory=session_factory, outbox_id=outbox_id)

    assert len(provider.messages) == 1
    assert "library conversion" in provider.messages[0].text_body


def test_reconciliation_reminder_renders_without_token_or_source_job(session_factory):
    user = add_verified_user(session_factory, "reconcile-reminder@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)

    with session_factory.begin() as session:
        outbox = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="reconciliation_reminder",
            status="queued",
        )
        session.add(outbox)
        session.flush()
        outbox_id = outbox.id

    delivery.deliver(session_factory=session_factory, outbox_id=outbox_id)

    assert len(provider.messages) == 1
    assert provider.messages[0].recipient == user.email
    assert "reconciliation needs attention" in provider.messages[0].subject
    assert "has not been deleted or applied" in provider.messages[0].text_body


def test_reissue_erases_revoked_envelope(session_factory):
    """INVERTED by MAIL-02: this also asserted the matching email_delivery Job
    went terminal. terminalize_token_outboxes did that with a workspace-plane
    with_for_update() WRITE from a control-plane session; the Job is gone, and
    last_error="recovery_token_revoked" is now the whole fence -- it is what
    the sweep's claim predicate refuses."""
    user = add_verified_user(session_factory, "reissue@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )

    first = recovery.issue_password_reset(email=user.email, delivery="email")
    second = recovery.issue_password_reset(email=user.email, delivery="email")

    with session_factory() as session:
        first_token = session.get(PasswordResetToken, first.token_id)
        first_outbox = session.get(EmailOutbox, first.outbox_id)
        second_outbox = session.get(EmailOutbox, second.outbox_id)
        assert first_token is not None and first_token.revoked_at is not None
        assert first_outbox is not None
        assert first_outbox.encrypted_token is None
        assert first_outbox.status == "failed"
        assert first_outbox.last_error == "recovery_token_revoked"
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert second_outbox is not None
        assert second_outbox.status == "queued"
        assert second_outbox.encrypted_token is not None


def test_email_verification_reissue_erases_old_envelope(session_factory):
    user = add_verified_user(session_factory, "verify-reissue@example.com")
    with session_factory.begin() as session:
        model = session.get(User, user.id)
        assert model is not None
        model.email_verified_at = None
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )

    first = recovery.issue_email_verification(user_id=user.id, actor_id=str(user.id))
    second = recovery.issue_email_verification(user_id=user.id, actor_id=str(user.id))

    with session_factory() as session:
        first_token = session.get(PasswordResetToken, first.token_id)
        first_outbox = session.get(EmailOutbox, first.outbox_id)
        second_outbox = session.get(EmailOutbox, second.outbox_id)
        assert first_token is not None and first_token.revoked_at is not None
        assert first_outbox is not None
        assert first_outbox.encrypted_token is None
        assert first_outbox.status == "failed"
        assert second_outbox is not None
        assert second_outbox.encrypted_token is not None


def test_consuming_password_reset_terminalizes_own_envelope(session_factory):
    user = add_verified_user(session_factory, "consume-reset@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )
    issued = recovery.issue_password_reset(email=user.email, delivery="email")
    token = issued.reset_url.split("token=", 1)[1]

    consumer_without_provider = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    )
    consumer_without_provider.consume_password_reset(
        token=token,
        new_password="replacement-password",
    )
    with pytest.raises(UsedPasswordResetTokenError):
        consumer_without_provider.consume_password_reset(
            token=token,
            new_password="another-password",
        )

    with session_factory.begin() as session:
        outbox = session.get(EmailOutbox, issued.outbox_id)
        token_row = session.get(PasswordResetToken, issued.token_id)
        assert token_row is not None and token_row.used_at is not None
        assert outbox is not None and outbox.encrypted_token is None
        assert outbox.status == "failed"
        assert outbox.last_error == "recovery_token_revoked"
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert (
            delivery.revoke_token_outboxes(
                session=session,
                token_ids=[issued.token_id],
                revoked_at=datetime.now(UTC),
            )
            == 1
        )


def test_consuming_email_verification_terminalizes_own_envelope(
    session_factory,
):
    user = add_verified_user(session_factory, "consume-verify@example.com")
    with session_factory.begin() as session:
        session.get(User, user.id).email_verified_at = None
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )
    issued = recovery.issue_email_verification(
        user_id=user.id,
        actor_id=str(user.id),
    )
    token = issued.reset_url.split("token=", 1)[1]

    consumer_without_provider = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    )
    consumer_without_provider.consume_email_verification(token=token)
    with pytest.raises(InvalidPasswordResetTokenError):
        consumer_without_provider.consume_email_verification(token=token)

    with session_factory.begin() as session:
        outbox = session.get(EmailOutbox, issued.outbox_id)
        token_row = session.get(PasswordResetToken, issued.token_id)
        assert token_row is not None and token_row.used_at is not None
        assert outbox is not None and outbox.encrypted_token is None
        assert outbox.status == "failed"
        assert outbox.last_error == "recovery_token_revoked"
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert (
            delivery.revoke_token_outboxes(
                session=session,
                token_ids=[issued.token_id],
                revoked_at=datetime.now(UTC),
            )
            == 1
        )


def test_consumed_password_reset_leaves_a_sent_outbox_alone_idempotently(
    session_factory,
):
    user = add_verified_user(session_factory, "sent-reset@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    issuer = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )
    issued = issuer.issue_password_reset(email=user.email, delivery="email")
    token = issued.reset_url.split("token=", 1)[1]
    sent = delivery.deliver(session_factory=session_factory, outbox_id=issued.outbox_id)
    assert sent.status == "sent"
    assert sent.encrypted_token is None
    assert sent.sent_at is not None
    sent_at = sent.sent_at

    PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    ).consume_password_reset(
        token=token,
        new_password="replacement-password",
    )

    with session_factory.begin() as session:
        for _ in range(2):
            assert (
                delivery.revoke_token_outboxes(
                    session=session,
                    token_ids=[issued.token_id],
                    revoked_at=datetime.now(UTC),
                )
                == 1
            )
        outbox = session.get(EmailOutbox, issued.outbox_id)
        assert outbox is not None and outbox.status == "sent"
        assert outbox.sent_at == sent_at
        assert outbox.encrypted_token is None
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_consumed_verification_leaves_a_sent_outbox_alone_idempotently(
    session_factory,
):
    user = add_verified_user(session_factory, "sent-verification@example.com")
    with session_factory.begin() as session:
        model = session.get(User, user.id)
        assert model is not None
        model.email_verified_at = None
    delivery = EmailDeliveryService(
        settings=mail_settings(),
        provider=RecordingProvider(),
    )
    issuer = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
        mail_delivery_service=delivery,
    )
    issued = issuer.issue_email_verification(
        user_id=user.id,
        actor_id=str(user.id),
    )
    token = issued.reset_url.split("token=", 1)[1]
    sent = delivery.deliver(session_factory=session_factory, outbox_id=issued.outbox_id)
    assert sent.status == "sent"
    assert sent.encrypted_token is None
    assert sent.sent_at is not None
    sent_at = sent.sent_at

    PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    ).consume_email_verification(token=token)

    with session_factory.begin() as session:
        for _ in range(2):
            assert (
                delivery.revoke_token_outboxes(
                    session=session,
                    token_ids=[issued.token_id],
                    revoked_at=datetime.now(UTC),
                )
                == 1
            )
        outbox = session.get(EmailOutbox, issued.outbox_id)
        assert outbox is not None and outbox.status == "sent"
        assert outbox.sent_at == sent_at
        assert outbox.encrypted_token is None
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_concurrent_email_verification_reissue_keeps_one_retryable_envelope(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'verification-reissue.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        with sessions.begin() as session:
            seed_default_workspace(session)
            user = User(
                email="concurrent-verification@example.com",
                password_hash=CredentialHasher().hash_password("original-password"),
                display_name="Concurrent verification",
                status="active",
                email_verified_at=None,
            )
            session.add(user)
            session.flush()
            user_id = user.id
        delivery = EmailDeliveryService(
            settings=mail_settings(),
            provider=RecordingProvider(),
        )
        barrier = threading.Barrier(2, timeout=30)
        results = []
        errors: list[BaseException] = []

        def issue() -> None:
            try:
                barrier.wait()
                results.append(
                    PasswordRecoveryService(
                        session_factory=sessions,
                        credential_hasher=CredentialHasher(),
                        public_base_url="https://wiki.example.com",
                        mail_delivery_service=delivery,
                    ).issue_email_verification(
                        user_id=user_id,
                        actor_id=str(user_id),
                    )
                )
            except BaseException as exc:  # asserted below
                errors.append(exc)

        threads = [threading.Thread(target=issue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        with sessions() as session:
            tokens = list(
                session.scalars(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id,
                        PasswordResetToken.purpose == "email_verification",
                    )
                ).all()
            )
            outboxes = list(
                session.scalars(
                    select(EmailOutbox).where(
                        EmailOutbox.token_id.in_([token.id for token in tokens])
                    )
                ).all()
            )
            active = [
                token
                for token in tokens
                if token.used_at is None and token.revoked_at is None
            ]
            retryable = [
                outbox
                for outbox in outboxes
                if outbox.status == "queued" and outbox.encrypted_token is not None
            ]
            assert len(tokens) == 2
            assert len(active) == 1
            assert len(retryable) == 1
            assert retryable[0].token_id == active[0].id
            retired = next(outbox for outbox in outboxes if outbox not in retryable)
            assert retired.status == "failed"
            assert retired.encrypted_token is None
    finally:
        engine.dispose()


def test_provider_failure_persists_only_stable_error_category(session_factory):
    user = add_verified_user(session_factory, "failure@example.com")
    settings = mail_settings()
    delivery = EmailDeliveryService(settings=settings, provider=RecordingProvider())
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
    )
    result = recovery.issue_password_reset(
        email=user.email,
        delivery="email",
        requested_via="cli",
    )
    failing = EmailDeliveryService(settings=settings, provider=FailingProvider())

    with pytest.raises(MailProviderError):
        failing.deliver(session_factory=session_factory, outbox_id=result.outbox_id)

    with session_factory() as session:
        outbox = session.get(EmailOutbox, result.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.last_error == "mail_provider_failed"
        assert result.reset_url not in (outbox.last_error or "")


def test_successful_reset_queues_password_changed_response_guidance(
    session_factory,
):
    user = add_verified_user(session_factory, "changed-notice@example.com")
    provider = RecordingProvider()
    settings = mail_settings()
    delivery = EmailDeliveryService(settings=settings, provider=provider)
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
    )
    issued = recovery.issue_password_reset(
        email=user.email,
        delivery="link",
        requested_via="cli",
    )

    recovery.consume_password_reset(
        token=issued.reset_url.split("token=", 1)[1],
        new_password="replacement-password",
    )

    with session_factory.begin() as session:
        outbox = session.scalar(
            select(EmailOutbox).where(EmailOutbox.message_kind == "password_changed")
        )
        assert outbox is not None and outbox.status == "queued"
        assert outbox.encrypted_token is None
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        outbox_id = outbox.id

    delivery.deliver(session_factory=session_factory, outbox_id=outbox_id)

    assert len(provider.messages) == 1
    message = provider.messages[0]
    assert message.recipient == user.email
    assert "If this was not you" in message.text_body
    assert "contact the server operator" in message.text_body
    assert "PAT/MCP credentials" in message.text_body


def _add_member(
    session_factory,
    workspace_id,
    *,
    email: str,
    role: str,
    verified: bool = True,
    status: str = "active",
) -> User:
    with session_factory.begin() as session:
        user = User(
            email=email,
            password_hash=CredentialHasher().hash_password("member-password"),
            display_name=email,
            status=status,
            email_verified_at=datetime.now(UTC) if verified else None,
        )
        session.add(user)
        session.flush()
        session.add(
            WorkspaceMembership(user_id=user.id, workspace_id=workspace_id, role=role)
        )
        user_id = user.id
    with session_factory() as session:
        return session.get(User, user_id)


def test_queue_job_failure_notifications_noop_without_mail_configured(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    _add_member(session_factory, workspace_id, email="owner@example.com", role="owner")
    # No mail_provider/smtp_host set -- mail_configured is False.
    settings = Settings(
        public_base_url="https://wiki.example.com",
        session_secret="test-mail-session-secret-with-enough-entropy",
    )
    delivery = EmailDeliveryService(settings=settings)

    with session_factory.begin() as session:
        queued = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=uuid4(),
            job_type="library_discovery",
            error="boom",
        )
        assert queued == []

    with session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 0


def test_queue_job_failure_notifications_noop_for_email_delivery_job_type(
    session_factory,
):
    """email_delivery is ALWAYS excluded (unlike embed, see the test below):
    it is this notification's own transport, so notifying about its own
    failure here would be a duplicate-effort loop."""
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    _add_member(session_factory, workspace_id, email="owner@example.com", role="owner")
    delivery = EmailDeliveryService(settings=mail_settings())

    with session_factory.begin() as session:
        queued = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=uuid4(),
            job_type="email_delivery",
            error="boom",
        )
        assert queued == []


def test_queue_job_failure_notifications_reaches_owners_for_failed_embed_job(
    session_factory,
):
    """embed is hidden from Activity (routine per-edit volume), but NOT from
    this notification: a terminally failed embed job is rare and
    consequential (the workspace's semantic index silently regressed on one
    page), and neither of the two mechanisms an earlier version of this
    exclusion cited actually catches that case (see
    job_queue.NOISY_UNTIL_FAILED_JOB_TYPES). By the time this method runs the
    job IS already terminally failed (called from worker/runtime.py's
    _notify_terminal_failure only when status=="failed"), so embed must not
    be silently dropped here the way it is from Activity."""
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    owner = _add_member(
        session_factory, workspace_id, email="owner@example.com", role="owner"
    )
    delivery = EmailDeliveryService(settings=mail_settings())

    with session_factory.begin() as session:
        queued = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=uuid4(),
            job_type="embed",
            error="embedding provider unreachable",
        )
        assert len(queued) == 1
        assert queued[0].recipient_user_id == owner.id
        assert queued[0].message_kind == "job_failed"


def test_queue_job_failure_notifications_only_reaches_verified_owners(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    owner = _add_member(
        session_factory, workspace_id, email="owner@example.com", role="owner"
    )
    _add_member(
        session_factory,
        workspace_id,
        email="unverified-owner@example.com",
        role="owner",
        verified=False,
    )
    _add_member(
        session_factory, workspace_id, email="editor@example.com", role="editor"
    )
    delivery = EmailDeliveryService(settings=mail_settings())
    job_id = uuid4()

    with session_factory.begin() as session:
        queued = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=job_id,
            job_type="library_discovery",
            error="conversion exhausted retries",
        )
        assert len(queued) == 1
        outbox = queued[0]
        assert outbox.recipient_user_id == owner.id
        assert outbox.recipient_email == owner.email
        assert outbox.message_kind == "job_failed"
        assert outbox.source_job_type == "library_discovery"
        assert outbox.status == "queued"
        # INVERTED by MAIL-02: this used to assert a matching email_delivery
        # Job. The dedupe identity moved onto the outbox row itself, which is
        # the only plane this control-plane transaction can speak for.
        assert outbox.id == job_failure_outbox_id(
            job_id=job_id, recipient_user_id=owner.id
        )
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_queue_job_failure_notifications_is_idempotent_per_job_and_owner(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    _add_member(session_factory, workspace_id, email="owner@example.com", role="owner")
    delivery = EmailDeliveryService(settings=mail_settings())
    job_id = uuid4()

    with session_factory.begin() as session:
        first = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=job_id,
            job_type="library_discovery",
            error="boom",
        )
        assert len(first) == 1

    with session_factory.begin() as session:
        # Simulates the same terminal transition being reached twice (e.g. a
        # crash between marking the job failed and this call). Must not
        # create a second outbox row for the same (job, owner) pair.
        second = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=job_id,
            job_type="library_discovery",
            error="boom again",
        )
        assert second == []

    with session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 1


def test_job_failed_notification_renders_and_names_the_job_type(session_factory):
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    owner = _add_member(
        session_factory, workspace_id, email="owner@example.com", role="owner"
    )
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    job_queue = JobQueueService()

    with session_factory.begin() as session:
        source_job = job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="library_discovery",
            trigger_type="manual",
            idempotency_key="notify-render-1",
        )
        source_job_id = source_job.id
        queued = delivery.queue_job_failure_notifications(
            session=session,
            workspace_id=workspace_id,
            job_id=source_job_id,
            job_type="library_discovery",
            error="boom",
        )
        outbox_id = queued[0].id

    delivery.deliver(session_factory=session_factory, outbox_id=outbox_id)

    assert len(provider.messages) == 1
    message = provider.messages[0]
    assert message.recipient == owner.email
    assert "library discovery" in message.text_body
    assert "재시도 횟수를 모두" in message.text_body


def test_terminal_failure_audit_is_private_and_idempotent(session_factory):
    user = add_verified_user(session_factory, "terminal@example.com")
    settings = mail_settings()
    delivery = EmailDeliveryService(settings=settings, provider=RecordingProvider())
    recovery = PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
    )
    issued = recovery.issue_password_reset(
        email=user.email,
        delivery="email",
        requested_via="cli",
    )
    failing = EmailDeliveryService(settings=settings, provider=FailingProvider())
    with pytest.raises(MailProviderError):
        failing.deliver(session_factory=session_factory, outbox_id=issued.outbox_id)
    with session_factory.begin() as session:
        first = failing.record_terminal_failure(
            session=session,
            outbox_id=issued.outbox_id,
            delivery_path="cli_immediate",
            error_code="mail_provider_failed",
        )
        second = failing.record_terminal_failure(
            session=session,
            outbox_id=issued.outbox_id,
            delivery_path="cli_immediate",
            error_code="provider said terminal@example.com could not receive token",
        )
        assert first is not None and second is not None

    with session_factory() as session:
        outbox = session.get(EmailOutbox, issued.outbox_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.email.delivery.failed"
                )
            ).all()
        )
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.attempt_count == 1
        assert outbox.last_error == "mail_provider_failed"
        assert outbox.next_attempt_at is None
        assert outbox.encrypted_token is None
        assert len(events) == 1
        assert events[0].target_id == user.id
        assert events[0].payload == {
            "message_kind": "password_reset",
            "delivery_path": "cli_immediate",
            "error_code": "mail_provider_failed",
        }
        serialized = f"{events[0].payload} {events[0].event_payload_ref}"
        assert user.email not in serialized
        assert issued.reset_url not in serialized


def test_database_rate_limit_uses_digest_slots_shared_by_session(session_factory):
    limiter = PasswordRecoveryRateLimiter(
        secret="rate-limit-test-secret",
        per_ip=10,
        per_email=2,
        global_limit=20,
    )
    with session_factory.begin() as session:
        limiter.consume(
            session=session,
            client_ip="203.0.113.7",
            email=" Rate@Example.com ",
            purpose="password_reset",
        )
        limiter.consume(
            session=session,
            client_ip="203.0.113.7",
            email="rate@example.com",
            purpose="password_reset",
        )
        with pytest.raises(PasswordRecoveryRateLimitError):
            limiter.consume(
                session=session,
                client_ip="203.0.113.7",
                email="rate@example.com",
                purpose="password_reset",
            )

    with session_factory() as session:
        rows = list(session.scalars(select(PasswordRecoveryRateLimitSlot)).all())
        # consume() only claims the ip/email buckets now — the instance-wide
        # global backstop is claimed separately via consume_global() (AUTH-06)
        # and is never touched here, so the row floor drops from 6 to 4.
        assert (
            session.scalar(
                select(func.count()).select_from(PasswordRecoveryRateLimitSlot)
            )
            >= 4
        )
        serialized = " ".join(row.bucket_digest for row in rows)
        assert "203.0.113.7" not in serialized
        assert "rate@example.com" not in serialized


def test_claim_slot_probes_to_limit_not_a_fixed_retry_count(session_factory):
    # AUTH-06: the count-first optimization must still probe every slot up to
    # `limit`, not stop after a fixed number of retries. A naive "count then
    # retry twice" rewrite falsely rejects a request that lands on an
    # already-occupied slot number while real capacity remains.
    window_started_at = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at = window_started_at + timedelta(minutes=30)
    digest = "hmac-sha256:test-claim-slot-probe-digest"
    limit = 5

    with session_factory.begin() as session:
        for slot in range(1, limit):  # pre-fill slots 1..limit-1, one free
            session.add(
                PasswordRecoveryRateLimitSlot(
                    bucket_kind="ip",
                    bucket_digest=digest,
                    window_started_at=window_started_at,
                    slot=slot,
                    expires_at=expires_at,
                )
            )

    with session_factory.begin() as session:
        claimed = PasswordRecoveryRateLimiter._claim_slot(
            session=session,
            bucket_kind="ip",
            bucket_digest=digest,
            window_started_at=window_started_at,
            expires_at=expires_at,
            limit=limit,
        )
    assert claimed is True

    with session_factory.begin() as session:
        full = PasswordRecoveryRateLimiter._claim_slot(
            session=session,
            bucket_kind="ip",
            bucket_digest=digest,
            window_started_at=window_started_at,
            expires_at=expires_at,
            limit=limit,
        )
    assert full is False


def test_claim_slot_saturated_bucket_costs_no_inserts(session_factory):
    # AUTH-06: on a saturated bucket, the pre-fix implementation issued
    # `limit` failed INSERT+ROLLBACK round-trips per call — a free
    # amplification multiplier for whoever floods the endpoint. A saturated
    # bucket must cost one SELECT and zero INSERTs.
    window_started_at = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at = window_started_at + timedelta(minutes=30)
    digest = "hmac-sha256:test-claim-slot-saturated-digest"
    limit = 5

    with session_factory.begin() as session:
        for slot in range(1, limit + 1):
            session.add(
                PasswordRecoveryRateLimitSlot(
                    bucket_kind="ip",
                    bucket_digest=digest,
                    window_started_at=window_started_at,
                    slot=slot,
                    expires_at=expires_at,
                )
            )

    engine = session_factory.kw["bind"]
    insert_statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into password_recovery_rate_limit_slots"):
            insert_statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with session_factory.begin() as session:
            claimed = PasswordRecoveryRateLimiter._claim_slot(
                session=session,
                bucket_kind="ip",
                bucket_digest=digest,
                window_started_at=window_started_at,
                expires_at=expires_at,
                limit=limit,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert claimed is False
    assert insert_statements == []


class ShutdownSignal(BaseException):
    """Stands in for SIGTERM/KeyboardInterrupt reaching the worker mid-batch.

    It must be a BaseException, not an Exception: a provider error is caught
    per row by deliver() and swallowed per row by sweep_due(), so it does NOT
    abort anything and would prove nothing about transaction boundaries. Only
    something that escapes both handlers reaches the caller's `with
    session_factory.begin()` and rolls the batch back -- which is exactly the
    shape of a worker being stopped between two sends.
    """


class InterruptedProvider:
    def __init__(self, *, interrupt_after: int) -> None:
        self.messages: list[OutboundMail] = []
        self._interrupt_after = interrupt_after

    def send(self, message: OutboundMail) -> None:
        if len(self.messages) >= self._interrupt_after:
            raise ShutdownSignal("worker stopped mid-batch")
        self.messages.append(message)


def _queued_notification(
    session, user: User, *, created_at: datetime, **overrides
) -> EmailOutbox:
    overrides.setdefault("status", "queued")
    outbox = EmailOutbox(
        recipient_user_id=user.id,
        recipient_email=user.email,
        message_kind="password_changed",
        created_at=created_at,
        **overrides,
    )
    session.add(outbox)
    session.flush()
    return outbox


def test_sweep_due_keeps_an_already_sent_row_sent_when_the_batch_aborts(
    session_factory,
):
    """MAIL-01: a row this sweep already handed to the provider must stay
    'sent' even if the batch never finishes.

    The pre-fix sweep ran every row inside ONE caller-owned transaction and
    deliver() only flushed, so an abort between two rows discarded the first
    row's status='sent' while the mail itself was already gone -- the next
    sweep then sends it a second time.
    """
    user = add_verified_user(session_factory, "sweep-abort@example.com")
    provider = InterruptedProvider(interrupt_after=1)
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        first_id = _queued_notification(session, user, created_at=base).id
        second_id = _queued_notification(
            session, user, created_at=base + timedelta(seconds=1)
        ).id

    with pytest.raises(ShutdownSignal):
        delivery.sweep_due(session_factory=session_factory)

    assert len(provider.messages) == 1
    with session_factory() as session:
        first = session.get(EmailOutbox, first_id)
        second = session.get(EmailOutbox, second_id)
        assert first is not None and first.status == "sent", (
            "the first row's send is already irreversible; its 'sent' status "
            "must be committed before the next row is attempted"
        )
        assert first.sent_at is not None
        assert first.encrypted_token is None
        # The interrupted row keeps its claim, which is what the 'sending'
        # lease below exists to recover.
        assert second is not None and second.status == "sending"


def test_sweep_due_reclaims_and_redelivers_a_row_stranded_in_sending(
    session_factory,
):
    """MAIL-01: nothing anywhere read a row in status='sending', so a process
    that died between claim and settle stranded that mail forever. The lease
    -- EmailOutbox.updated_at older than max(60, 4 * smtp_timeout_seconds) --
    is the only thing that ever gets it moving again."""
    user = add_verified_user(session_factory, "sweep-lease@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    stranded_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory.begin() as session:
        stranded_id = _queued_notification(
            session,
            user,
            created_at=stranded_at,
            status="sending",
            attempt_count=1,
            updated_at=stranded_at,
        ).id

    delivered = delivery.sweep_due(session_factory=session_factory)

    assert delivered == 1
    assert len(provider.messages) == 1
    with session_factory() as session:
        stranded = session.get(EmailOutbox, stranded_id)
        assert stranded is not None
        assert stranded.status == "sent"


def test_sweep_due_leaves_a_freshly_claimed_sending_row_alone(session_factory):
    """The lease must not be a blanket 'sending -> failed' reset: a send that
    is still in flight (updated_at inside the window) would then be delivered
    twice. The floor is 4x the SMTP timeout, so an in-flight send is always
    inside it."""
    user = add_verified_user(session_factory, "sweep-inflight@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    claimed_at = datetime.now(UTC)
    with session_factory.begin() as session:
        in_flight_id = _queued_notification(
            session,
            user,
            created_at=claimed_at,
            status="sending",
            attempt_count=1,
            updated_at=claimed_at,
        ).id

    delivered = delivery.sweep_due(session_factory=session_factory)

    assert delivered == 0
    assert provider.messages == []
    with session_factory() as session:
        in_flight = session.get(EmailOutbox, in_flight_id)
        assert in_flight is not None and in_flight.status == "sending"


def _expired_reset_outbox(session_factory, user: User) -> EmailOutbox:
    """A password_reset row whose token has already expired.

    ``_render`` raises ``MailDeliveryError`` for it before the provider is ever
    reached, which is the render-failure branch F1 covers. An expired token is
    the ordinary way to get there in production -- a reset issued just before a
    worker outage, a revoked-but-unfenced token, an unset mail_from_address.
    """
    with session_factory.begin() as session:
        token = PasswordResetToken(
            user_id=user.id,
            token_digest=f"expired-render-failure-digest-{uuid4()}",
            purpose="password_reset",
            delivery="email",
            requested_via="public",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(token)
        session.flush()
        outbox = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=user.email,
            message_kind="password_reset",
            status="queued",
            token_id=token.id,
            encrypted_token="not-a-real-envelope",
        )
        session.add(outbox)
        session.flush()
        return outbox


def test_render_failure_persists_its_attempt_and_backoff(session_factory):
    """MAIL-01 regression (F1): the render-failure branch must actually reach
    the database.

    ``deliver`` took the claim inside ``with session_factory.begin()``. When
    ``_claim_for_send`` raised out of that block on a render failure the
    context manager ROLLED BACK -- discarding the claim and everything
    ``_record_attempt_failure`` had just written. The row came back out of the
    sweep byte-identical to how it went in (queued/0/None/None), so it was
    re-attempted on every sweep -- about once a second in the worker loop --
    for ever, never spending its budget and never visible to an operator.
    """
    user = add_verified_user(session_factory, "render-failure@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    outbox_id = _expired_reset_outbox(session_factory, user).id

    assert delivery.sweep_due(session_factory=session_factory) == 0

    # The provider is never reached: rendering failed first.
    assert provider.messages == []
    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        assert outbox is not None
        assert outbox.status == "failed", (
            "a render failure must leave a committed 'failed' row, not roll "
            "the claim back to 'queued'"
        )
        assert outbox.attempt_count == 1, (
            "the attempt the claim took must be committed, or the retry "
            "budget can never be spent"
        )
        assert outbox.last_error == "mail_delivery_failed"
        assert outbox.next_attempt_at is not None, (
            "a non-terminal failure must arm a REAL backoff timestamp; NULL "
            "would make the row permanently due again"
        )
        assert outbox.next_attempt_at > datetime.now(UTC)


def test_render_failure_is_not_reattempted_every_sweep(session_factory):
    """MAIL-01 regression (F1 + F2 composed): the same unrenderable row swept
    repeatedly on a frozen clock must stop, not churn.

    Pre-fix it was claimed and rolled back on every single sweep -- two UPDATEs
    per sweep on an otherwise idle worker, which is precisely what
    ``reclaim_stale_sending``'s own comment says must never happen.
    """
    user = add_verified_user(session_factory, "render-churn@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(), provider=RecordingProvider()
    )
    outbox_id = _expired_reset_outbox(session_factory, user).id
    frozen = datetime.now(UTC)

    for _ in range(5):
        delivery.sweep_due(session_factory=session_factory, now=frozen)

    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        assert outbox is not None
        assert outbox.attempt_count == 1, (
            "the backoff armed by the first failure must hold off every "
            "later sweep at the same instant"
        )
        assert outbox.status == "failed"


def test_render_failure_spends_a_bounded_budget_then_stops(session_factory):
    """F1 + F2: an unrenderable row must reach a terminal state and stay
    there, with one audit event, exactly like a provider failure does."""
    user = add_verified_user(session_factory, "render-budget@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(), provider=RecordingProvider()
    )
    outbox_id = _expired_reset_outbox(session_factory, user).id
    now = datetime.now(UTC)

    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        now += timedelta(hours=2)  # past the 3600 s backoff cap
        delivery.sweep_due(session_factory=session_factory, now=now)
    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        assert outbox is not None
        assert outbox.attempt_count == DEFAULT_JOB_MAX_ATTEMPTS
        assert outbox.status == "failed"
        assert outbox.next_attempt_at is None
        assert outbox.encrypted_token is None, (
            "a terminal render failure must clear the recoverable reset "
            "envelope"
        )

    for _ in range(10):
        now += timedelta(hours=2)
        delivery.sweep_due(session_factory=session_factory, now=now)
    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.email.delivery.failed"
                )
            ).all()
        )
        assert outbox.attempt_count == DEFAULT_JOB_MAX_ATTEMPTS, (
            "a terminally-failed row must never be claimed again, however "
            "much later the sweep runs"
        )
        assert len(events) == 1


def test_spent_budget_stops_contacting_the_provider(session_factory):
    """MAIL-01 regression (F2): ``max_attempts`` must BOUND the retries, not
    remove the backoff.

    The terminal branches wrote ``next_attempt_at=None`` while the due scan
    read NULL as "due now" and the claim guard matched status IN
    ('queued','failed') with no ceiling, so a row that had spent its budget was
    permanently due: measured at budget 3, the provider was contacted 10 times
    over 10 sweeps. HEAD retried unboundedly too, but at the 3600 s backoff cap
    -- this removed the throttle entirely.
    """
    user = add_verified_user(session_factory, "budget-bound@example.com")
    provider = CountingFailingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    with session_factory.begin() as session:
        outbox_id = _queued_notification(
            session, user, created_at=datetime.now(UTC)
        ).id

    now = datetime.now(UTC)
    for _ in range(10):
        now += timedelta(hours=2)  # always past the 3600 s backoff cap
        delivery.sweep_due(session_factory=session_factory, now=now)

    assert provider.calls == DEFAULT_JOB_MAX_ATTEMPTS, (
        f"provider contacted {provider.calls} times for a budget of "
        f"{DEFAULT_JOB_MAX_ATTEMPTS}"
    )
    with session_factory() as session:
        outbox = session.get(EmailOutbox, outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.attempt_count == DEFAULT_JOB_MAX_ATTEMPTS
        assert outbox.next_attempt_at is None


def test_terminally_failed_row_leaves_an_idle_worker_read_only(session_factory):
    """F2's compounding cost. ``reclaim_stale_sending`` goes out of its way to
    probe read-only first so "an idle worker [does not become] a periodic
    writer"; a permanently-due terminal row defeated that from the other end,
    re-claiming and re-failing on every sweep."""
    user = add_verified_user(session_factory, "idle-readonly@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(), provider=CountingFailingProvider()
    )
    with session_factory.begin() as session:
        _queued_notification(session, user, created_at=datetime.now(UTC))

    now = datetime.now(UTC)
    for _ in range(DEFAULT_JOB_MAX_ATTEMPTS):
        now += timedelta(hours=2)
        delivery.sweep_due(session_factory=session_factory, now=now)

    writes: list[str] = []

    @event.listens_for(session_factory.kw["bind"], "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            writes.append(statement)

    try:
        for _ in range(5):
            now += timedelta(hours=2)
            delivery.sweep_due(session_factory=session_factory, now=now)
    finally:
        event.remove(session_factory.kw["bind"], "before_cursor_execute", _record)

    assert writes == [], (
        "a sweep with nothing deliverable must issue no writes at all; "
        f"got {len(writes)} over 5 sweeps"
    )


def test_reclaimed_lease_writes_an_explicit_due_timestamp(session_factory):
    """The lease reclaim WANTS its row back in the sweep immediately, and must
    now say so with a timestamp instead of sharing the NULL sentinel that
    means "terminal, never due" everywhere else."""
    user = add_verified_user(session_factory, "lease-explicit@example.com")
    delivery = EmailDeliveryService(
        settings=mail_settings(), provider=CountingFailingProvider()
    )
    stranded_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory.begin() as session:
        stranded_id = _queued_notification(
            session,
            user,
            created_at=stranded_at,
            status="sending",
            attempt_count=1,
            updated_at=stranded_at,
        ).id

    now = datetime.now(UTC)
    assert delivery.reclaim_stale_sending(session_factory=session_factory, now=now) == 1

    with session_factory() as session:
        stranded = session.get(EmailOutbox, stranded_id)
        assert stranded is not None
        assert stranded.status == "failed"
        assert stranded.next_attempt_at == now, (
            "the reclaimed row must carry an explicit 'due now' timestamp; "
            "NULL on a failed row now means the opposite"
        )


def test_claim_stamps_updated_at_as_the_lease_deadline(session_factory):
    """``EmailOutbox.updated_at`` doubles as the 'sending' lease deadline, and
    the claim is a Core UPDATE. This pins that the column's
    ``onupdate=utc_now`` really does fire for it -- the claim no longer passes
    ``updated_at`` explicitly, because that value was indistinguishable from
    the one onupdate writes and so could never be shown to matter."""
    user = add_verified_user(session_factory, "claim-stamp@example.com")
    provider = BlockingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    stale = datetime.now(UTC) - timedelta(hours=3)
    with session_factory.begin() as session:
        outbox_id = _queued_notification(
            session, user, created_at=stale, updated_at=stale
        ).id

    thread = threading.Thread(
        target=lambda: delivery.deliver(
            session_factory=session_factory, outbox_id=outbox_id
        )
    )
    thread.start()
    try:
        assert provider.entered.wait(timeout=5)
        with session_factory() as session:
            claimed = session.get(EmailOutbox, outbox_id)
            assert claimed is not None and claimed.status == "sending"
            assert claimed.updated_at > stale, (
                "an unstamped updated_at would make the lease deadline read "
                "as three hours ago and reclaim a send still in flight"
            )
    finally:
        provider.release.set()
        thread.join(timeout=5)


def test_deliver_refuses_a_terminally_failed_row(session_factory):
    """The terminal marker has to hold against a DIRECT ``deliver`` call, not
    only against the sweep's due scan: the claim guard is what a CLI retry, a
    stale id or a future caller runs into."""
    user = add_verified_user(session_factory, "terminal-claim@example.com")
    provider = CountingFailingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    with session_factory.begin() as session:
        terminal_id = _queued_notification(
            session,
            user,
            created_at=datetime.now(UTC),
            status="failed",
            attempt_count=DEFAULT_JOB_MAX_ATTEMPTS,
            last_error="mail_provider_failed",
            next_attempt_at=None,
        ).id

    with pytest.raises(MailDeliveryError):
        delivery.deliver(session_factory=session_factory, outbox_id=terminal_id)

    assert provider.calls == 0
    with session_factory() as session:
        terminal = session.get(EmailOutbox, terminal_id)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.attempt_count == DEFAULT_JOB_MAX_ATTEMPTS, (
            "a refused claim must not spend another attempt"
        )


class SettleCrash(BaseException):
    """The process dying between the provider's ACK and the settle commit.

    A BaseException, not an Exception: ``deliver``'s ``except Exception``
    would otherwise catch it and settle the failure, which is a handled
    outcome and proves nothing about a crash.
    """


def test_lease_recovery_of_an_accepted_send_is_at_least_once(session_factory):
    """PINS a documented limitation, not a bug to be fixed here: delivery is
    at-least-once.

    Phase 2 hands the message to the provider outside any transaction, so a
    process that dies after the provider ACCEPTED it but before
    ``_settle_sent`` commits leaves a committed 'sending' row that is
    indistinguishable from one whose send never happened. ``reclaim_stale_sending``
    returns it to the sweep and the recipient gets the message twice. Nothing
    on this side can close that window -- it needs a provider-side idempotency
    key, which SMTP does not offer -- so it is written down (in
    ``reclaim_stale_sending``'s docstring) and measured here rather than
    claimed away. If this test ever starts failing because a duplicate no
    longer happens, that docstring is what needs updating.
    """
    user = add_verified_user(session_factory, "at-least-once@example.com")
    provider = RecordingProvider()
    delivery = EmailDeliveryService(settings=mail_settings(), provider=provider)
    with session_factory.begin() as session:
        outbox_id = _queued_notification(
            session, user, created_at=datetime.now(UTC)
        ).id

    def _crash(self, **_kwargs):
        raise SettleCrash("process died after the provider accepted the message")

    original_settle = EmailDeliveryService._settle_sent
    EmailDeliveryService._settle_sent = _crash
    try:
        with pytest.raises(SettleCrash):
            delivery.deliver(session_factory=session_factory, outbox_id=outbox_id)
    finally:
        EmailDeliveryService._settle_sent = original_settle

    assert len(provider.messages) == 1
    with session_factory() as session:
        crashed = session.get(EmailOutbox, outbox_id)
        assert crashed is not None and crashed.status == "sending", (
            "the claim is committed, so the crash leaves a row nothing can "
            "tell apart from one whose send never left"
        )

    later = datetime.now(UTC) + timedelta(
        seconds=delivery.sending_lease_seconds + 30
    )
    assert delivery.sweep_due(session_factory=session_factory, now=later) == 1

    assert len(provider.messages) == 2, (
        "recovery re-sends: this is the at-least-once cost of committing the "
        "claim before the send"
    )
    assert {message.recipient for message in provider.messages} == {user.email}
