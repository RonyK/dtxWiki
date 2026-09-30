from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class EmailOutbox(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Transactional email delivery record.

    Recovery tokens are never stored in plaintext.  A retryable delivery keeps
    only an authenticated-encryption envelope whose key is derived from the
    server session secret; the worker decrypts it in memory immediately before
    rendering the canonical URL.

    ``updated_at`` is OVERLOADED: while ``status='sending'`` it is also the
    delivery claim's lease deadline. That works because the ONLY write a row
    receives while it is in that state is the one that takes it out again --
    the guarded claim in ``EmailDeliveryService._claim_for_send`` matches
    queued/failed only, and the exits are ``_settle_sent``,
    ``_record_attempt_failure`` and ``reclaim_stale_sending`` itself -- so the
    write that entered 'sending' IS the claim, and ``reclaim_stale_sending``
    reads its timestamp as the claim time. This is the weakest joint in the
    MAIL-01 design and is named here rather than hidden: a dedicated
    ``claimed_at`` column is the post-MIG-02 follow-up, deliberately deferred
    because MAIL-01 must not drag a schema change in with it.
    """

    __tablename__ = "email_outbox"
    __table_args__ = (
        CheckConstraint(
            "message_kind IN "
            "('password_reset', 'email_verification', 'password_changed', "
            "'job_failed', 'reconciliation_reminder')",
            name="email_outbox_message_kind_check",
        ),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed')",
            name="email_outbox_status_check",
        ),
        Index(
            "idx_email_outbox_claim",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )

    recipient_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("password_reset_tokens.id", ondelete="SET NULL"),
    )
    # Set only for message_kind="job_failed": the terminally-failed job's
    # job_type, denormalized as plain text at enqueue time (see
    # EmailDeliveryService.queue_job_failure_notifications) so _render can
    # name it without joining back to `job`. Deliberately not a job_id/FK:
    # this row lives on the identity plane (user-keyed PK, FKs to users and
    # password_reset_tokens only, no workspace_id column) while Job is
    # workspace-sharded (Epic-2 D-2/D-6/D-9) -- the two are not guaranteed
    # to be the same physical database, so a cross-plane FK could never be
    # enforced once that overlay ships. This is the transactional-outbox
    # pattern proper: everything a render needs lives on this row, so
    # delivery (EmailDeliveryService.sweep_due) never has to reach another
    # table, and can never render a degraded body just because the source
    # Job row was purged or unreachable.
    source_job_type: Mapped[str | None] = mapped_column(Text)
    recipient_email: Mapped[str] = mapped_column(Text, nullable=False)
    message_kind: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL means two different things, and which one depends on ``status``:
    #
    #   queued + NULL -> never scheduled; due immediately.
    #   failed + NULL -> TERMINAL. The retry budget is spent, the token was
    #                    revoked, or delivery was otherwise given up on. Both
    #                    ``EmailDeliveryService.sweep_due``'s due predicate
    #                    and ``_claim_for_send``'s guarded UPDATE refuse it,
    #                    which is what bounds retries at all.
    #   failed + time -> a real backoff; claimable again from that instant,
    #                    including the lease reclaim's explicit "now".
    #
    # Reading NULL as due-now on a failed row -- which the due scan did before
    # MAIL-01 was fixed -- turns the retry budget into a REMOVAL of the
    # backoff rather than a bound on the retries: the row is re-claimed on
    # every sweep, forever. Anything that writes NULL here must mean terminal.
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class PasswordRecoveryRateLimitSlot(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A portable, conflict-safe fixed-window rate-limit slot.

    Bucket identities are keyed digests, never raw IP or email values.  A
    unique slot number lets multiple web processes contend through the shared
    database without an in-memory limiter or a check-then-insert race.
    """

    __tablename__ = "password_recovery_rate_limit_slots"
    __table_args__ = (
        CheckConstraint(
            "bucket_kind IN ('ip', 'email', 'global')",
            name="password_recovery_rate_limit_bucket_kind_check",
        ),
        UniqueConstraint(
            "bucket_digest",
            "window_started_at",
            "slot",
            name="uq_password_recovery_rate_limit_slot",
        ),
        Index(
            "idx_password_recovery_rate_limit_expiry",
            "expires_at",
        ),
    )

    bucket_kind: Mapped[str] = mapped_column(Text, nullable=False)
    bucket_digest: Mapped[str] = mapped_column(Text, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
