from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.app.settings import Settings
from backend.domain.identity_policy import EmailPolicyError, normalize_email
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.mail import EmailOutbox
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.services.audit_service import AuditService
from backend.services.job_queue import (
    ALWAYS_HIDDEN_JOB_TYPES,
    DEFAULT_JOB_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

# TOMBSTONE (MAIL-02). Nothing enqueues this job type any more: EmailOutbox is
# identity-plane data (no workspace_id column) while Job is workspace-sharded,
# so the wakeup Job stranded on a plane no claim path visits -- and the handler
# it woke called deliver() with the WORKSPACE session. Delivery is sweep_due's
# alone now. The name stays because rows already exist in runtime databases and
# still get claimed: they must drain harmlessly rather than fail forever, so the
# type keeps its registration (worker/runtime.py) and its membership in both
# hidden sets (job_queue.py). Deleting the type outright needs a data migration
# for those rows and is a post-MIG-02 follow-up.
EMAIL_DELIVERY_JOB_TYPE = "email_delivery"

# The retry budget the deleted Job used to enforce, deliberately the SAME
# constant so the two can never drift: without it an undeliverable message
# retries on every sweep, forever, at the 1 hour backoff cap.
EMAIL_DELIVERY_MAX_ATTEMPTS = DEFAULT_JOB_MAX_ATTEMPTS

# sweep_due's per-pass batch size. Hoisted out of the signature default because
# it is a throughput/lock-hold tradeoff a reader has to be able to find.
EMAIL_OUTBOX_SWEEP_LIMIT = 50

# 'sending' lease bounds. A row is only ever in 'sending' while a single
# provider send is in flight, so 4x the SMTP timeout plus a 60 s floor cannot
# elapse while that send is still running -- see reclaim_stale_sending.
SENDING_LEASE_TIMEOUT_MULTIPLIER = 4.0
SENDING_LEASE_MIN_SECONDS = 60.0
MAIL_DELIVERY_LEASE_EXPIRED_ERROR = "mail_delivery_lease_expired"
STABLE_MAIL_DELIVERY_ERROR_CODES = {
    "mail_delivery_failed",
    "mail_delivery_unavailable",
    "mail_provider_failed",
}
STABLE_MAIL_DELIVERY_PATHS = {"worker", "cli_immediate"}

# SPA screens that redeem a one-time recovery token.
PASSWORD_RESET_LINK_PATH = "/reset-password"
EMAIL_VERIFICATION_LINK_PATH = "/verify-email"


def one_time_token_link(base_url: str, path: str, token: str) -> str:
    """Return the link that carries a one-time recovery token to the SPA.

    M-2: the token rides in the URL fragment, never the query string. A
    browser does not send the fragment to the server, so the token cannot
    reach an access log (uvicorn, Caddy, nginx) or a Referer header the way
    `?token=` did. The SPA reads `#token=`, strips it from the address bar
    with history.replaceState, and submits it only in POST JSON bodies (see
    frontend/src/features/auth/oneTimeLinkToken.ts). Every issuer -- the
    service result printed by the CLI or returned to a server admin, and the
    rendered email -- builds the link here so the shapes cannot drift.
    """

    return f"{base_url}{path}#{urlencode({'token': token})}"


def reconciliation_reminder_outbox_id(
    *, workspace_id: UUID, branch_version_id: UUID, recipient_user_id: UUID
) -> UUID:
    """Return the durable per-branch/per-owner delivery identity."""

    return uuid5(
        NAMESPACE_URL,
        "dtxwiki:context-reconciliation-reminder:"
        f"{workspace_id}:{branch_version_id}:{recipient_user_id}",
    )


def job_failure_outbox_id(*, job_id: UUID, recipient_user_id: UUID) -> UUID:
    """Return the durable per-job/per-owner delivery identity."""

    return uuid5(
        NAMESPACE_URL,
        f"dtxwiki:job-failure-notification:{job_id}:{recipient_user_id}",
    )


class MailDeliveryError(RuntimeError):
    error_code = "mail_delivery_failed"


class MailDeliveryConfigurationError(MailDeliveryError):
    error_code = "mail_delivery_unavailable"


class MailProviderError(MailDeliveryError):
    error_code = "mail_provider_failed"


def _validated_recipient(value: str) -> str:
    try:
        return normalize_email(value)
    except EmailPolicyError as exc:
        raise MailDeliveryConfigurationError("recipient email is invalid") from exc


@dataclass(frozen=True)
class OutboundMail:
    recipient: str
    sender: str
    subject: str
    text_body: str


class MailProvider(Protocol):
    def send(self, message: OutboundMail) -> None: ...


@dataclass(frozen=True)
class _ClaimOutcome:
    """What ``EmailDeliveryService._claim_for_send`` produced.

    A render failure is RETURNED rather than raised because the claim runs
    inside a transaction the caller commits: raising out of that block rolled
    the claim back, and with it every write recording the failure, so the row
    came back out of the sweep byte-identical to how it went in and was
    re-attempted forever. Both fields are None when the row was already sent
    and there is simply nothing to deliver.
    """

    message: OutboundMail | None = None
    render_error: MailDeliveryError | None = None


@dataclass(frozen=True)
class SMTPMailProvider:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    use_ssl: bool = False
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.starttls == self.use_ssl:
            raise MailDeliveryConfigurationError(
                "SMTP requires exactly one secure transport mode"
            )

    def send(self, message: OutboundMail) -> None:
        email = EmailMessage()
        email["To"] = message.recipient
        email["From"] = message.sender
        email["Subject"] = message.subject
        email.set_content(message.text_body)
        # ``smtplib`` falls back to ``ssl._create_stdlib_context()`` when no
        # context is supplied. On supported Python versions that context does
        # not verify certificates or hostnames. Recovery mail can carry a live
        # credential-reset token, so both implicit TLS and STARTTLS must use a
        # normal validating client context.
        tls_context = (
            ssl.create_default_context() if self.use_ssl or self.starttls else None
        )
        try:
            if self.use_ssl:
                client = smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                    context=tls_context,
                )
            else:
                client = smtplib.SMTP(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                )
            with client:
                client.ehlo()
                if self.starttls and not self.use_ssl:
                    client.starttls(context=tls_context)
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            # Do not relay a provider response into the HTTP/CLI layer: SMTP
            # text may echo message content or recipient data.
            raise MailProviderError("mail provider delivery failed") from exc


@dataclass(frozen=True)
class EmailOutboxCipher:
    secret: str

    @property
    def key(self) -> bytes:
        material = f"dtxwiki.email_outbox.v1\0{self.secret}".encode()
        return hashlib.sha256(material).digest()

    def encrypt(self, *, outbox_id: UUID, message_kind: str, token: str) -> str:
        nonce = secrets.token_bytes(12)
        aad = f"{outbox_id}:{message_kind}".encode()
        ciphertext = AESGCM(self.key).encrypt(nonce, token.encode(), aad)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip("=")
        return f"v1.{payload}"

    def decrypt(
        self,
        *,
        outbox_id: UUID,
        message_kind: str,
        envelope: str,
    ) -> str:
        if not envelope.startswith("v1."):
            raise MailDeliveryError("unsupported outbox encryption envelope")
        encoded = envelope[3:]
        encoded += "=" * (-len(encoded) % 4)
        try:
            payload = base64.urlsafe_b64decode(encoded)
            nonce, ciphertext = payload[:12], payload[12:]
            aad = f"{outbox_id}:{message_kind}".encode()
            return AESGCM(self.key).decrypt(nonce, ciphertext, aad).decode()
        except Exception as exc:  # normalize secret-envelope errors
            raise MailDeliveryError("invalid outbox encryption envelope") from exc


@dataclass(frozen=True)
class EmailOutboxStatus:
    queued: int
    failed: int
    sent: int
    latest_attempt_status: str | None
    latest_attempt_at: datetime | None


@dataclass(frozen=True)
class EmailDeliveryService:
    settings: Settings
    provider: MailProvider | None = None
    audit_service: AuditService = field(default_factory=AuditService)

    @property
    def cipher(self) -> EmailOutboxCipher:
        return EmailOutboxCipher(self.settings.effective_session_secret)

    @property
    def resolved_provider(self) -> MailProvider:
        if self.provider is not None:
            return self.provider
        if not self.settings.mail_configured:
            raise MailDeliveryConfigurationError("email recovery is not configured")
        assert self.settings.smtp_host is not None
        return SMTPMailProvider(
            host=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_username,
            password=self.settings.smtp_password,
            starttls=self.settings.smtp_starttls,
            use_ssl=self.settings.smtp_use_ssl,
            timeout_seconds=self.settings.smtp_timeout_seconds,
        )

    def queue_token_email(
        self,
        *,
        session: Session,
        user: User,
        token_model: PasswordResetToken,
        raw_token: str,
        message_kind: str,
    ) -> EmailOutbox:
        if message_kind not in {"password_reset", "email_verification"}:
            raise ValueError("unsupported token email kind")
        self._assert_configured()
        outbox_id = uuid4()
        outbox = EmailOutbox(
            id=outbox_id,
            recipient_user_id=user.id,
            token_id=token_model.id,
            recipient_email=_validated_recipient(user.email),
            message_kind=message_kind,
            encrypted_token=self.cipher.encrypt(
                outbox_id=outbox_id,
                message_kind=message_kind,
                token=raw_token,
            ),
            status="queued",
        )
        session.add(outbox)
        session.flush()
        return outbox

    def queue_password_changed_notification(
        self,
        *,
        session: Session,
        user: User,
    ) -> EmailOutbox | None:
        # A mail-less installation must never make a successful password reset
        # roll back just because notifications are unavailable.
        if not self.settings.mail_configured or user.email_verified_at is None:
            return None
        outbox = EmailOutbox(
            recipient_user_id=user.id,
            recipient_email=_validated_recipient(user.email),
            message_kind="password_changed",
            status="queued",
        )
        session.add(outbox)
        session.flush()
        return outbox

    def queue_job_failure_notifications(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        job_id: UUID,
        job_type: str,
        error: str,
    ) -> list[EmailOutbox]:
        """Secondary channel for a job that just exhausted its retry budget.

        The PRIMARY, always-on signal is durable in-app state: for most job
        types that is the workspace Activity feed, which reads Job.status and
        the redacted job_failure_disclosure (ActivityService._jobs). The raw
        last_error text reaches no API surface at all, including GET
        /admin/jobs -- it is a protected server-log/DB-diagnostic value only,
        and certainly no email body. embed is the one exception --
        Activity hides it unconditionally (HIDDEN_JOB_TYPES, noise from the
        per-edit volume), so for embed this email and GET
        /workspaces/{id}/jobs (which -- unlike Activity -- surfaces a FAILED
        embed job; see NOISY_UNTIL_FAILED_JOB_TYPES) are the only in-app
        signals. That is exactly why embed is NOT excluded here.

        Email is only a SECONDARY channel regardless: it only fires
        when mail is configured -- per mail_configured's own contract (see
        its docstring / D-6), a mail-less install is a supported deployment
        and must never depend on this to learn about a failure.

        Recipients are workspace OWNERS (workspace_memberships.role='owner';
        this schema has no separate workspace-level 'admin' role) with a
        verified email, mirroring queue_password_changed_notification's own
        verified-email gate. ALWAYS_HIDDEN_JOB_TYPES (email_delivery only) is
        excluded because it is this notification's own transport --
        notifying about its failure here would be a duplicate-effort loop.

        Replay safety is the outbox row's own identity, exactly as in
        queue_reconciliation_reminder_notifications: ``EmailOutbox.id`` is
        derived from (job, recipient), so the dedupe lives on the same row,
        the same table and the same plane as the thing it dedupes. The
        earlier check-then-insert asked the WORKSPACE-plane job table whether
        a wakeup Job already existed -- a question this control-plane
        transaction cannot answer once the two are separate databases.
        """
        if job_type in ALWAYS_HIDDEN_JOB_TYPES or not self.settings.mail_configured:
            return []
        owners = session.scalars(
            select(User)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == "owner",
                User.status == "active",
                User.email_verified_at.is_not(None),
            )
        ).all()
        queued: list[EmailOutbox] = []
        for owner in owners:
            outbox_id = job_failure_outbox_id(job_id=job_id, recipient_user_id=owner.id)
            existing = session.get(EmailOutbox, outbox_id)
            if existing is not None:
                if (
                    existing.recipient_user_id != owner.id
                    or existing.message_kind != "job_failed"
                ):
                    raise RuntimeError("job failure outbox identity collision")
                continue
            outbox = EmailOutbox(
                id=outbox_id,
                recipient_user_id=owner.id,
                recipient_email=_validated_recipient(owner.email),
                message_kind="job_failed",
                source_job_type=job_type,
                status="queued",
            )
            session.add(outbox)
            session.flush()
            queued.append(outbox)
        return queued

    def queue_reconciliation_reminder_notifications(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        branch_version_id: UUID,
    ) -> list[EmailOutbox]:
        """Queue one reminder per currently eligible workspace owner.

        ``EmailOutbox.id`` is derived from the exact workspace, branch, and
        recipient. It is the cross-retry idempotency fence: the reminder Job's
        workspace-plane Activity transaction may commit before its later
        control-plane mail transaction or status update, but replay can never
        create a second outbox for the same owner. As with every other outbox
        path, ``sweep_due`` is the one and only delivery route (MAIL-02).
        """

        if not self.settings.mail_configured:
            return []
        owners = session.scalars(
            select(User)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == "owner",
                User.status == "active",
                User.email_verified_at.is_not(None),
            )
        ).all()
        queued: list[EmailOutbox] = []
        for owner in owners:
            outbox_id = reconciliation_reminder_outbox_id(
                workspace_id=workspace_id,
                branch_version_id=branch_version_id,
                recipient_user_id=owner.id,
            )
            existing = session.get(EmailOutbox, outbox_id)
            if existing is not None:
                if (
                    existing.recipient_user_id != owner.id
                    or existing.message_kind != "reconciliation_reminder"
                ):
                    raise RuntimeError(
                        "reconciliation reminder outbox identity collision"
                    )
                continue
            outbox = EmailOutbox(
                id=outbox_id,
                recipient_user_id=owner.id,
                recipient_email=_validated_recipient(owner.email),
                message_kind="reconciliation_reminder",
                status="queued",
            )
            session.add(outbox)
            session.flush()
            queued.append(outbox)
        return queued

    @property
    def sending_lease_seconds(self) -> float:
        """How long a claimed row may sit in 'sending' before it is reclaimed.

        Bounded by the provider, not guessed: one send is one connection with
        a ``smtp_timeout_seconds`` ceiling, so 4x that plus a 60 s floor
        cannot elapse while a send is still in flight.
        """
        return max(
            SENDING_LEASE_MIN_SECONDS,
            SENDING_LEASE_TIMEOUT_MULTIPLIER * self.settings.smtp_timeout_seconds,
        )

    def deliver(
        self,
        *,
        session_factory: sessionmaker,
        outbox_id: UUID,
        delivery_path: str = "worker",
        max_attempts: int = EMAIL_DELIVERY_MAX_ATTEMPTS,
    ) -> EmailOutbox:
        """Deliver one outbox row as claim -> send -> settle, three commits.

        The provider send MUST NOT run inside a database transaction.
        ``SMTPMailProvider.send`` opens a fresh TCP+TLS connection per
        message with a 10 s default timeout, and on SQLite the surrounding
        transaction holds the writer lock for all of it -- a batch of
        ``EMAIL_OUTBOX_SWEEP_LIMIT`` rows blocked every other writer in the
        process for as long as the slowest provider took. Worse, the
        single-transaction shape only ever flushed: an abort anywhere in the
        batch discarded the ``status='sent'`` of rows whose mail had already
        left, and the next sweep sent those a second time.

        Phase 1 claims the row (queued/failed -> sending) AND renders the
        message, which has to happen there because rendering reads
        PasswordResetToken through the session. Phase 2 sends with no
        transaction open. Phase 3 records the outcome in its own transaction.

        Committing each phase is what makes an already-sent row's status
        durable -- and it is exactly why ``reclaim_stale_sending`` is
        mandatory rather than a nicety: a process that dies during phase 2
        now leaves a committed 'sending' row that nothing else would ever
        look at again. That recovery is at-least-once, not exactly-once: see
        the delivery-duplication note on ``reclaim_stale_sending``.

        ``max_attempts`` is the retry budget that replaces the one the
        deleted email_delivery Job used to enforce. The sweep spends the full
        budget across successive sweeps; ``dtxwiki users
        issue-password-reset --delivery email`` passes 1, because that path
        reports the failure to the operator synchronously and has no later
        attempt to make.

        The returned row is loaded on a session this method has already
        closed, so reading its attributes afterwards depends on
        ``expire_on_commit=False`` -- which every factory reaching here is
        built with (``backend/app/db.py``'s ``make_session_factory``, and the
        test fixtures that mirror it). A factory with SQLAlchemy's default
        ``expire_on_commit=True`` would hand callers a detached instance whose
        every attribute raises ``DetachedInstanceError``; that is a real
        constraint on this API, not an accident of the call sites.
        """
        with session_factory.begin() as claim_session:
            outcome = self._claim_for_send(
                session=claim_session,
                outbox_id=outbox_id,
            )
        # The claim -- its status='sending' AND its attempt_count increment --
        # is committed by now, whether or not rendering succeeded. A render
        # failure is therefore settled the same way a provider failure is: in
        # its own transaction, on top of a claim that is already durable. It
        # must NOT be recorded inside the claim transaction, because that
        # transaction is the one whose rollback would discard it.
        if outcome.render_error is not None:
            self._settle_failure(
                session_factory=session_factory,
                outbox_id=outbox_id,
                error_code=outcome.render_error.error_code,
                delivery_path=delivery_path,
                max_attempts=max_attempts,
            )
            raise sanitized_mail_delivery_error(outcome.render_error) from None
        if outcome.message is None:
            return self._reload(session_factory=session_factory, outbox_id=outbox_id)
        try:
            self.resolved_provider.send(outcome.message)
        except MailDeliveryError as exc:
            self._settle_failure(
                session_factory=session_factory,
                outbox_id=outbox_id,
                error_code=exc.error_code,
                delivery_path=delivery_path,
                max_attempts=max_attempts,
            )
            raise sanitized_mail_delivery_error(exc) from None
        except Exception:  # normalize provider failures
            self._settle_failure(
                session_factory=session_factory,
                outbox_id=outbox_id,
                error_code=MailProviderError.error_code,
                delivery_path=delivery_path,
                max_attempts=max_attempts,
            )
            raise MailProviderError("mail provider delivery failed") from None
        return self._settle_sent(session_factory=session_factory, outbox_id=outbox_id)

    def _claim_for_send(
        self,
        *,
        session: Session,
        outbox_id: UUID,
    ) -> _ClaimOutcome:
        """Phase 1: take the row and render its message.

        Returns an empty outcome -- rather than raising -- when the row is
        already sent, either by a concurrent claimant or by an earlier attempt
        of its own: there is simply nothing left to deliver.

        A RENDER failure is returned in ``_ClaimOutcome.render_error``, never
        raised: this runs inside the caller's claim transaction, so raising
        would roll the claim back, and recording the failure here would roll
        that recording back with it. ``deliver`` settles it afterwards, on the
        committed claim. Only a failure to take the claim at all raises, and
        that one *should* roll back -- the guarded UPDATE matched nothing, so
        there is nothing to keep.
        """
        outbox = session.get(EmailOutbox, outbox_id)
        if outbox is None:
            raise ValueError("email outbox row not found")
        if outbox.status == "sent":
            return _ClaimOutcome()
        # Atomically claim provider ownership.  A row lock alone is a no-op on
        # SQLite, while this guarded UPDATE is re-evaluated after a conflicting
        # writer commits on both SQLite and PostgreSQL. The CLI and the sweep
        # can therefore never both advance queued/failed -> sending.
        #
        # The next_attempt_at arm is the claim-side half of the due predicate
        # in sweep_due: a NULL next_attempt_at means "never scheduled" on a
        # queued row and "terminal, never again" on a failed one, so a row
        # whose retry budget is spent is unclaimable by ANY caller, not merely
        # unselected by the sweep. Deliberately clock-free, so a caller with a
        # different idea of "now" than the sweep's cannot resurrect it.
        claimed = session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.id == outbox_id,
                EmailOutbox.status.in_(("queued", "failed")),
                or_(
                    EmailOutbox.status == "queued",
                    EmailOutbox.next_attempt_at.is_not(None),
                ),
                or_(
                    EmailOutbox.last_error.is_(None),
                    EmailOutbox.last_error != "recovery_token_revoked",
                ),
            )
            .values(
                status="sending",
                attempt_count=EmailOutbox.attempt_count + 1,
                last_error=None,
                next_attempt_at=None,
                # updated_at is NOT set here: EmailOutbox.updated_at carries
                # onupdate=utc_now, which SQLAlchemy applies to a Core
                # update() exactly as it does to an ORM flush, so passing the
                # same value explicitly could never be shown to matter. What
                # IS load-bearing is that the column gets stamped at all --
                # from here until settle it is this claim's lease deadline
                # (see backend/models/mail.py) -- and that is pinned by
                # test_claim_stamps_updated_at_as_the_lease_deadline.
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.expire_all()
            outbox = session.get(EmailOutbox, outbox_id)
            if outbox is not None and outbox.status == "sent":
                return _ClaimOutcome()
            raise MailDeliveryError("email delivery is already claimed")
        session.refresh(outbox)
        try:
            return _ClaimOutcome(message=self._render(session, outbox))
        except MailDeliveryError as exc:
            return _ClaimOutcome(render_error=exc)
        except Exception:  # normalize rendering failures
            return _ClaimOutcome(
                render_error=MailProviderError("mail provider delivery failed")
            )

    def _settle_sent(
        self, *, session_factory: sessionmaker, outbox_id: UUID
    ) -> EmailOutbox:
        """Phase 3, success. Its own transaction, because the message has
        already left: this write must not be able to roll back alongside
        anything else."""
        with session_factory.begin() as session:
            outbox = session.get(EmailOutbox, outbox_id)
            if outbox is None:
                raise ValueError("email outbox row not found")
            outbox.status = "sent"
            outbox.sent_at = utc_now()
            outbox.next_attempt_at = None
            # Retain the encrypted envelope only while asynchronous delivery
            # may need to retry. Successful delivery must not leave
            # recoverable reset material in the database alongside the
            # long-lived digest record.
            outbox.encrypted_token = None
            session.flush()
        return outbox

    def _settle_failure(
        self,
        *,
        session_factory: sessionmaker,
        outbox_id: UUID,
        error_code: str,
        delivery_path: str,
        max_attempts: int,
    ) -> None:
        """Phase 3, failure. Swallows its own errors on purpose: the provider
        error the caller is about to raise is the more useful one, and a row
        left in 'sending' because this could not commit is precisely what
        ``reclaim_stale_sending`` exists to pick up."""
        try:
            with session_factory.begin() as session:
                outbox = session.get(EmailOutbox, outbox_id)
                if outbox is None:
                    return
                self._record_attempt_failure(
                    session=session,
                    outbox=outbox,
                    error_code=error_code,
                    delivery_path=delivery_path,
                    max_attempts=max_attempts,
                )
        except Exception:
            logger.exception(
                "failed to record the delivery failure of email outbox %s",
                outbox_id,
            )

    def _record_attempt_failure(
        self,
        *,
        session: Session,
        outbox: EmailOutbox,
        error_code: str,
        delivery_path: str,
        max_attempts: int,
    ) -> None:
        outbox.status = "failed"
        outbox.last_error = error_code
        if outbox.attempt_count >= max_attempts:
            # Retry budget spent. Set status="failed" BEFORE calling
            # record_terminal_failure: its "not failed yet" branch would
            # otherwise count this one attempt a second time.
            #
            # next_attempt_at=None on a FAILED row is the terminal marker:
            # sweep_due's due predicate and _claim_for_send's guard both
            # refuse it, which is what actually bounds the retries. It is not
            # "no backoff needed" -- the non-terminal branch below writes a
            # real timestamp precisely so this NULL can mean only one thing.
            outbox.next_attempt_at = None
            session.flush()
            self.record_terminal_failure(
                session=session,
                outbox_id=outbox.id,
                delivery_path=delivery_path,
                error_code=error_code,
            )
            return
        outbox.next_attempt_at = utc_now() + timedelta(
            seconds=min(3600, 30 * (2 ** max(0, outbox.attempt_count - 1)))
        )
        session.flush()

    def _reload(self, *, session_factory: sessionmaker, outbox_id: UUID) -> EmailOutbox:
        with session_factory() as session:
            outbox = session.get(EmailOutbox, outbox_id)
        if outbox is None:
            raise ValueError("email outbox row not found")
        return outbox

    def reclaim_stale_sending(
        self,
        *,
        session_factory: sessionmaker,
        now: datetime | None = None,
    ) -> int:
        """Return rows stranded in 'sending' to 'failed' so they are claimable
        again.

        Nothing anywhere else reads a row in 'sending'. Before MAIL-01 nothing
        ever committed one either, so the gap was invisible; committing the
        claim exposes it, and unlike Job -- which has
        reclaim_stale_jobs_with_session_factory -- this table had no recovery
        path at all, so a worker killed mid-send stranded that mail forever.

        The deadline is ``EmailOutbox.updated_at``, which doubles as the claim
        timestamp (see the overloading note on the model). Rows go back to
        'failed', NOT 'queued': that keeps the existing claim predicate, the
        ``recovery_token_revoked`` guard and the retry budget working
        untouched, and leaves the interruption visible in status rather than
        silently rewinding it.

        DELIVERY IS AT-LEAST-ONCE, NOT EXACTLY-ONCE, and this method is where
        that shows. Phase 2 hands the message to the provider outside any
        transaction; a process that dies after the provider ACCEPTED it but
        before ``_settle_sent`` commits leaves a committed 'sending' row that
        is indistinguishable from one whose send never happened. This reclaim
        returns it to the sweep and the recipient gets the message a second
        time -- measured: two deliveries, same recipient. Nothing on this side
        can close that window; it needs a provider-side idempotency key, which
        SMTP does not offer. What the design DOES guarantee is that no message
        is silently lost, and that no row is delivered twice through any path
        that completes normally (the guarded claim is what rules that out).
        """
        now = now or utc_now()
        deadline = now - timedelta(seconds=self.sending_lease_seconds)
        stale = (
            EmailOutbox.status == "sending",
            EmailOutbox.updated_at <= deadline,
        )
        # Probe read-only before opening a write transaction. This runs on
        # every sweep -- about once a second in the worker loop -- and an
        # UPDATE that matches nothing still takes SQLite's writer lock and
        # commits, which would make an idle worker a periodic writer. The
        # probe rides the same idx_email_outbox_claim status prefix as the due
        # scan below, so idle stays read-only.
        with session_factory() as probe_session:
            stranded = probe_session.scalar(
                select(EmailOutbox.id).where(*stale).limit(1)
            )
        if stranded is None:
            return 0
        with session_factory.begin() as session:
            reclaimed = session.execute(
                update(EmailOutbox)
                .where(*stale)
                .values(
                    status="failed",
                    last_error=MAIL_DELIVERY_LEASE_EXPIRED_ERROR,
                    # Explicitly due NOW, not NULL. This is the one place that
                    # wants a failed row back in the sweep immediately, and it
                    # has to say so: on a failed row NULL is the terminal
                    # marker, so inheriting it here would strand exactly the
                    # mail this method exists to rescue.
                    next_attempt_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
        if reclaimed:
            logger.warning(
                "reclaimed %d email outbox row(s) stranded in 'sending' past "
                "their lease",
                reclaimed,
            )
        return reclaimed

    def sweep_due(
        self,
        *,
        session_factory: sessionmaker,
        now: datetime | None = None,
        limit: int = EMAIL_OUTBOX_SWEEP_LIMIT,
    ) -> int:
        """Claim and deliver every due outbox row. THE delivery route.

        There is no wakeup Job any more (MAIL-02): email_outbox has no
        workspace_id column -- it is identity-plane data, FKs to users and
        password_reset_tokens only -- while Job is workspace-sharded (Epic-2
        D-2/D-6/D-9), so the Job this sweep used to merely back up could
        never be claimed, and the handler it woke would have called deliver()
        with the wrong plane's session. Latency now comes from cadence
        instead: worker/entry.py runs this once per loop iteration.

        Selection uses idx_email_outbox_claim (status, next_attempt_at,
        created_at) -- the same claim shape claim_next uses for `job` -- so it
        does not degrade into a full table scan as the table grows, and it
        returns nothing at all when the outbox is idle.

        Three phases, each with its own transaction and NONE of them wrapping
        a provider send: reclaim expired 'sending' leases, read the due ids,
        then deliver each row through deliver()'s own claim -> send -> settle.

        One row's outcome must not abort the batch: deliver() already
        recorded the failure/backoff (or lost a race to another claimant,
        which is equally not this sweep's problem) before raising, so every
        exception from a single row is logged and swallowed here, mirroring
        reclaim_stale_jobs_with_session_factory's "one broken thing can't
        take down the rest of the sweep" contract.

        Returns the number of rows this call itself delivered.
        """
        now = now or utc_now()
        try:
            self.reclaim_stale_sending(session_factory=session_factory, now=now)
        except Exception:
            logger.exception("email outbox lease reclaim failed; continuing this sweep")
        with session_factory() as read_session:
            due_ids = list(
                read_session.scalars(
                    select(EmailOutbox.id)
                    .where(
                        EmailOutbox.status.in_(("queued", "failed")),
                        # NULL next_attempt_at means "never scheduled" on a
                        # QUEUED row and "terminal, never again" on a FAILED
                        # one. Reading NULL as due-now regardless of status
                        # made every terminal marker -- budget spent, token
                        # revoked -- permanently due: the retry budget then
                        # removed the backoff instead of bounding the
                        # retries, and an undeliverable row was re-claimed on
                        # every sweep (about once a second) forever.
                        #
                        # Both arms stay index-covered: measured with EXPLAIN
                        # QUERY PLAN, SQLite resolves this as a MULTI-INDEX OR
                        # over idx_email_outbox_claim, using (status,
                        # next_attempt_at) on each -- one column MORE of the
                        # index than the NULL-or-<=now form it replaces, which
                        # could only search on (status).
                        or_(
                            and_(
                                EmailOutbox.status == "queued",
                                EmailOutbox.next_attempt_at.is_(None),
                            ),
                            EmailOutbox.next_attempt_at <= now,
                        ),
                        or_(
                            EmailOutbox.last_error.is_(None),
                            EmailOutbox.last_error != "recovery_token_revoked",
                        ),
                    )
                    .order_by(EmailOutbox.created_at.asc())
                    .limit(limit)
                )
            )
        delivered = 0
        for outbox_id in due_ids:
            try:
                self.deliver(session_factory=session_factory, outbox_id=outbox_id)
                delivered += 1
            except Exception:
                logger.exception(
                    "email outbox sweep failed to deliver %s; will retry "
                    "on the next sweep",
                    outbox_id,
                )
        return delivered

    def revoke_token_outboxes(
        self,
        *,
        session: Session,
        token_ids: list[UUID],
        revoked_at: datetime,
    ) -> int:
        """Erase retryable token envelopes and mark their delivery terminal.

        The caller revokes the digest rows in the same transaction.  Locking
        the matching outboxes orders reissue against an in-flight delivery:
        either delivery finishes first, or revocation removes the envelope
        before the provider can render it.
        """

        return terminalize_token_outboxes(
            session=session,
            token_ids=token_ids,
            terminal_at=revoked_at,
        )

    def status(self, session: Session) -> EmailOutboxStatus:
        counts = dict(
            session.execute(
                select(EmailOutbox.status, func.count()).group_by(EmailOutbox.status)
            ).all()
        )
        latest_attempt = session.execute(
            select(EmailOutbox.status, EmailOutbox.updated_at)
            .where(EmailOutbox.attempt_count > 0)
            .order_by(EmailOutbox.updated_at.desc(), EmailOutbox.id.desc())
            .limit(1)
        ).first()
        return EmailOutboxStatus(
            queued=int(counts.get("queued", 0)),
            failed=int(counts.get("failed", 0)),
            sent=int(counts.get("sent", 0)),
            latest_attempt_status=(
                str(latest_attempt.status) if latest_attempt is not None else None
            ),
            latest_attempt_at=(
                latest_attempt.updated_at if latest_attempt is not None else None
            ),
        )

    def record_terminal_failure(
        self,
        *,
        session: Session,
        outbox_id: UUID,
        delivery_path: str,
        error_code: str,
    ) -> EmailOutbox | None:
        """Persist one privacy-safe terminal marker and audit event per outbox."""

        outbox = session.get(EmailOutbox, outbox_id)
        if outbox is None or outbox.status == "sent":
            return outbox
        stable_error = (
            error_code
            if error_code in STABLE_MAIL_DELIVERY_ERROR_CODES
            else "mail_delivery_failed"
        )
        stable_path = (
            delivery_path if delivery_path in STABLE_MAIL_DELIVERY_PATHS else "worker"
        )
        event_payload_ref = f"email_outbox:{outbox.id}:terminal_failure"
        already_audited = session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "auth.email.delivery.failed",
                AuditEvent.event_payload_ref == event_payload_ref,
            )
        )
        if (
            already_audited is not None
            and outbox.last_error in STABLE_MAIL_DELIVERY_ERROR_CODES
        ):
            stable_error = outbox.last_error
        if already_audited is None and outbox.status != "failed":
            outbox.attempt_count += 1
        outbox.status = "failed"
        outbox.last_error = stable_error
        outbox.next_attempt_at = None
        outbox.encrypted_token = None

        if already_audited is None:
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="system",
                actor_id=(
                    "cli:password_reset"
                    if stable_path == "cli_immediate"
                    else "worker:email_delivery"
                ),
                action="auth.email.delivery.failed",
                target_kind="user",
                target_id=outbox.recipient_user_id,
                payload={
                    "message_kind": outbox.message_kind,
                    "delivery_path": stable_path,
                    "error_code": stable_error,
                },
                event_summary="Email delivery reached a terminal failure",
                event_payload_ref=event_payload_ref,
                retention_class="sensitive",
            )
        session.flush()
        return outbox

    def _render(self, session: Session, outbox: EmailOutbox) -> OutboundMail:
        sender = self.settings.mail_from_address
        if not sender:
            raise MailDeliveryConfigurationError("mail sender is not configured")
        if outbox.message_kind == "password_changed":
            return OutboundMail(
                recipient=outbox.recipient_email,
                sender=sender,
                subject="dtxWiki password changed",
                text_body=(
                    "Your dtxWiki password was changed. If this was not you, "
                    "contact the server operator immediately and revoke your "
                    "PAT/MCP credentials.\n\n"
                    "dtxWiki 비밀번호가 변경되었습니다. 본인이 변경하지 "
                    "않았다면 즉시 서버 운영자에게 문의하고 PAT/MCP "
                    "자격 증명을 폐기하세요."
                ),
            )
        if outbox.message_kind == "job_failed":
            # source_job_type is denormalized onto the row at enqueue time
            # (queue_job_failure_notifications) -- no join back to `job`,
            # and none possible in general once Job and EmailOutbox can live
            # on different physical databases (Epic-2 D-2/D-6/D-9). A
            # generic body only if the field was never set at all.
            job_kind = (
                outbox.source_job_type.replace("_", " ")
                if outbox.source_job_type
                else None
            )
            return OutboundMail(
                recipient=outbox.recipient_email,
                sender=sender,
                subject="dtxWiki: a background job failed",
                text_body=(
                    (
                        f"A {job_kind} job in your dtxWiki workspace failed after "
                        "exhausting its retry attempts and needs attention.\n\n"
                        if job_kind
                        else "A background job in your dtxWiki workspace failed "
                        "after exhausting its retry attempts and needs "
                        "attention.\n\n"
                    )
                    + "The workspace's Activity feed and Jobs view show that it "
                    "failed and how many attempts it took; the diagnostic detail "
                    "is server-admin only, so ask a server admin to review it "
                    "from Settings > Jobs.\n\n"
                    "dtxWiki 워크스페이스의 백그라운드 작업이 재시도 횟수를 모두 "
                    "소진하고 실패했습니다. Activity 와 Jobs 에서 실패 사실과 "
                    "시도 횟수를 확인할 수 있으며, 자세한 진단 내용은 서버 "
                    "관리자만 볼 수 있으므로 Settings > Jobs 확인을 요청하세요."
                ),
            )
        if outbox.message_kind == "reconciliation_reminder":
            return OutboundMail(
                recipient=outbox.recipient_email,
                sender=sender,
                subject="dtxWiki: reconciliation needs attention",
                text_body=(
                    "A reconciliation branch in your dtxWiki workspace has "
                    "remained unresolved beyond the workspace reminder interval.\n\n"
                    "Open the workspace Activity or Review lane to reconcile or "
                    "explicitly discard the branch. The branch has not been "
                    "deleted or applied to the page head.\n\n"
                    "dtxWiki 워크스페이스의 화해 대기 가지가 설정된 알림 시간보다 "
                    "오래 해결되지 않았습니다. Activity 또는 리뷰 레인에서 가지를 "
                    "화해하거나 명시적으로 폐기하세요. 가지는 삭제되지 않았고 페이지 "
                    "HEAD에도 적용되지 않았습니다."
                ),
            )
        token_model = (
            session.get(PasswordResetToken, outbox.token_id)
            if outbox.token_id is not None
            else None
        )
        now = utc_now()
        if (
            token_model is None
            or token_model.used_at is not None
            or token_model.revoked_at is not None
            or token_model.expires_at <= now
            or not outbox.encrypted_token
        ):
            raise MailDeliveryError("recovery token is no longer deliverable")
        token = self.cipher.decrypt(
            outbox_id=outbox.id,
            message_kind=outbox.message_kind,
            envelope=outbox.encrypted_token,
        )
        base_url = (self.settings.public_base_url or "").strip().rstrip("/")
        if outbox.message_kind == "password_reset":
            path = PASSWORD_RESET_LINK_PATH
            subject = "Reset your dtxWiki password"
            action = "reset your password / 비밀번호를 재설정"
        else:
            path = EMAIL_VERIFICATION_LINK_PATH
            subject = "Verify your dtxWiki email"
            action = "verify your email / 이메일 주소를 확인"
        url = one_time_token_link(base_url, path, token)
        return OutboundMail(
            recipient=outbox.recipient_email,
            sender=sender,
            subject=subject,
            text_body=(
                f"Use this one-time link to {action}:\n\n{url}\n\n"
                "If you did not request this, you can ignore this message.\n"
                "본인이 요청하지 않았다면 이 메일을 무시하세요."
            ),
        )

    def _assert_configured(self) -> None:
        if not self.settings.mail_configured:
            raise MailDeliveryConfigurationError("email recovery is not configured")


def build_email_delivery_service(
    settings: Settings,
    *,
    provider: MailProvider | None = None,
) -> EmailDeliveryService:
    return EmailDeliveryService(settings=settings, provider=provider)


def sanitized_mail_delivery_error(exc: MailDeliveryError) -> MailDeliveryError:
    if isinstance(exc, MailProviderError):
        return MailProviderError("mail provider delivery failed")
    if isinstance(exc, MailDeliveryConfigurationError):
        return MailDeliveryConfigurationError("email delivery is not configured")
    return MailDeliveryError("mail delivery failed")


def terminalize_token_outboxes(
    *,
    session: Session,
    token_ids: list[UUID],
    terminal_at: datetime,
) -> int:
    """Erase every matching token envelope and mark its delivery terminal.

    ``last_error="recovery_token_revoked"`` is what keeps the sweep's claim
    predicate (and deliver()'s own guarded UPDATE) from ever picking these
    rows up again. ``terminal_at`` no longer has anything to stamp -- the
    workspace-plane Job row it used to complete is gone (MAIL-02) -- and is
    kept only so this function's callers keep their signature; dropping the
    parameter is a follow-up in password_recovery_service.py's lane.
    """

    if not token_ids:
        return 0
    outboxes = list(
        session.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.token_id.in_(token_ids))
            .with_for_update()
        ).all()
    )
    for outbox in outboxes:
        outbox.encrypted_token = None
        if outbox.status != "sent":
            outbox.status = "failed"
            outbox.last_error = "recovery_token_revoked"
            outbox.next_attempt_at = None
    session.flush()
    return len(outboxes)
