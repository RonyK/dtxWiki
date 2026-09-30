from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.models.mail import PasswordRecoveryRateLimitSlot
from backend.models.password_reset import PasswordResetToken
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import (
    EMAIL_VERIFICATION_LINK_PATH,
    PASSWORD_RESET_LINK_PATH,
    EmailDeliveryService,
    MailDeliveryError,
    one_time_token_link,
    terminalize_token_outboxes,
)
from backend.services.oauth_service import revoke_all_user_oauth_credentials
from backend.services.session_service import (
    AuthenticatedUser,
    authenticated_user,
    normalize_email,
    validate_password,
)

PASSWORD_RESET_TOKEN_PREFIX = "dtxr_"
EMAIL_VERIFICATION_TOKEN_PREFIX = "dtxe_"
PASSWORD_RESET_TOKEN_NAMESPACE = "dtxwiki.password_reset.v1"
EMAIL_VERIFICATION_TOKEN_NAMESPACE = "dtxwiki.email_verification.v1"
DEFAULT_PASSWORD_RESET_TTL_SECONDS = 30 * 60
MIN_PASSWORD_RESET_TTL_SECONDS = 5 * 60
MAX_PASSWORD_RESET_TTL_SECONDS = 24 * 60 * 60
DEFAULT_EMAIL_VERIFICATION_TTL_SECONDS = 24 * 60 * 60
PASSWORD_RECOVERY_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
PASSWORD_RECOVERY_RATE_LIMIT_PER_IP = 8
PASSWORD_RECOVERY_RATE_LIMIT_PER_EMAIL = 4
PASSWORD_RECOVERY_RATE_LIMIT_GLOBAL = 200
# AUTH-02: a tighter cap layered on top of the shared ip/email throttle above,
# specific to /auth/email-verification/request. That shared throttle mirrors
# the password-reset lane's numbers (4 per 15 min per email — effectively up
# to ~16/hour sustained), which is right for guarding against credential
# guessing but loose for this endpoint's actual threat: an authenticated
# caller resending verification mail to ONE already-known account
# (self-inflicted or third-party mail-bombing via an open-signup deployment).
# This is account-scoped resend spam, not network-level guessing, so it is
# keyed on email only — no IP dimension.
EMAIL_VERIFICATION_RESEND_RATE_LIMIT_WINDOW_SECONDS = 60 * 60
EMAIL_VERIFICATION_RESEND_RATE_LIMIT_PER_EMAIL = 3
_TOKEN_URLSAFE_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_PASSWORD_RESET_ISSUE_LOCK = Lock()
_EMAIL_VERIFICATION_ISSUE_LOCK = Lock()


class PasswordRecoveryError(RuntimeError):
    error_code = "password_recovery_failed"


class PasswordRecoveryConfigurationError(PasswordRecoveryError):
    error_code = "password_recovery_unavailable"


class PasswordRecoveryUserNotFoundError(PasswordRecoveryError):
    error_code = "password_recovery_user_not_found"


class InvalidPasswordResetTokenError(PasswordRecoveryError):
    error_code = "invalid_password_reset_token"


class ExpiredPasswordResetTokenError(InvalidPasswordResetTokenError):
    error_code = "password_reset_token_expired"


class UsedPasswordResetTokenError(InvalidPasswordResetTokenError):
    error_code = "password_reset_token_used"


class InvalidPasswordResetRequestError(PasswordRecoveryError):
    error_code = "invalid_password_reset_request"


class PasswordRecoveryRateLimitError(PasswordRecoveryError):
    error_code = "password_recovery_rate_limited"


class PasswordRecoveryAuthorizationError(PasswordRecoveryError):
    error_code = "server_admin_required"


class PasswordRecoveryRecentReauthenticationRequiredError(PasswordRecoveryError):
    error_code = "recent_reauthentication_required"


@dataclass(frozen=True)
class PasswordResetIssueResult:
    token_id: UUID
    user_id: UUID
    reset_url: str
    expires_at: datetime
    delivery: str
    outbox_id: UUID | None = None


@dataclass(frozen=True)
class EmailVerificationConsumeResult:
    user: AuthenticatedUser
    verified_at: datetime


@dataclass(frozen=True)
class PasswordRecoveryRateLimiter:
    secret: str
    now: Callable[[], datetime] = utc_now
    window_seconds: int = PASSWORD_RECOVERY_RATE_LIMIT_WINDOW_SECONDS
    per_ip: int = PASSWORD_RECOVERY_RATE_LIMIT_PER_IP
    per_email: int = PASSWORD_RECOVERY_RATE_LIMIT_PER_EMAIL
    global_limit: int = PASSWORD_RECOVERY_RATE_LIMIT_GLOBAL

    def consume(
        self, *, session: Session, client_ip: str, email: str, purpose: str
    ) -> None:
        """Claim the per-IP and per-email buckets.

        The instance-wide backstop is deliberately NOT claimed here — see
        consume_global(), which callers claim only once a request is known to
        produce a delivery. Otherwise spray against addresses that have no
        account starves every real user's recovery from one constant key
        (AUTH-06).

        ``purpose`` keeps the anonymous password-reset lane and the
        authenticated email-verification lane on disjoint buckets: without it,
        a reset spray against a victim address drains that user's
        verification quota too (AUTH-02).
        """

        normalized_email = normalize_email(email)
        now = self.now()
        window_started_at, expires_at = self._window(now)
        session.execute(
            delete(PasswordRecoveryRateLimitSlot).where(
                PasswordRecoveryRateLimitSlot.expires_at <= now
            )
        )
        buckets = (
            ("ip", client_ip or "unknown", self.per_ip),
            ("email", normalized_email, self.per_email),
        )
        for bucket_kind, value, limit in buckets:
            digest = self._bucket_digest(bucket_kind, value, purpose=purpose)
            if not self._claim_slot(
                session=session,
                bucket_kind=bucket_kind,
                bucket_digest=digest,
                window_started_at=window_started_at,
                expires_at=expires_at,
                limit=limit,
            ):
                raise PasswordRecoveryRateLimitError(
                    "password recovery request was rate limited"
                )

    def consume_global(self, *, session: Session, purpose: str) -> None:
        """Claim the instance-wide backstop bucket.

        Callers must claim this only once a request is known to actually
        produce a delivery (e.g. after eligibility is confirmed) — otherwise
        an anonymous spray against unregistered addresses can exhaust the
        single global key and silently starve every real user's recovery for
        the rest of the window (AUTH-06).
        """

        now = self.now()
        window_started_at, expires_at = self._window(now)
        if not self._claim_slot(
            session=session,
            bucket_kind="global",
            bucket_digest=self._bucket_digest(
                "global", "password-reset-request", purpose=purpose
            ),
            window_started_at=window_started_at,
            expires_at=expires_at,
            limit=self.global_limit,
        ):
            raise PasswordRecoveryRateLimitError(
                "password recovery request was rate limited"
            )

    def consume_email_only(self, *, session: Session, email: str, purpose: str) -> None:
        """Claim only the per-email bucket.

        AUTH-02: a tighter cap layered on top of the shared consume() call
        for email-verification resends. This is one already-authenticated
        account being resent to repeatedly — resend spam, not network-level
        credential guessing — so there is no IP dimension to claim here; the
        shared consume() call above already covers that axis. Construct this
        limiter instance with a smaller ``per_email``/``window_seconds`` than
        the shared defaults (see EMAIL_VERIFICATION_RESEND_RATE_LIMIT_*).
        """

        normalized_email = normalize_email(email)
        now = self.now()
        window_started_at, expires_at = self._window(now)
        session.execute(
            delete(PasswordRecoveryRateLimitSlot).where(
                PasswordRecoveryRateLimitSlot.expires_at <= now
            )
        )
        if not self._claim_slot(
            session=session,
            bucket_kind="email",
            bucket_digest=self._bucket_digest(
                "email", normalized_email, purpose=purpose
            ),
            window_started_at=window_started_at,
            expires_at=expires_at,
            limit=self.per_email,
        ):
            raise PasswordRecoveryRateLimitError(
                "password recovery request was rate limited"
            )

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        window_epoch = int(now.timestamp()) // self.window_seconds * self.window_seconds
        window_started_at = datetime.fromtimestamp(window_epoch, tz=UTC)
        expires_at = window_started_at + timedelta(seconds=self.window_seconds * 2)
        return window_started_at, expires_at

    def _bucket_digest(self, bucket_kind: str, value: str, *, purpose: str) -> str:
        # Keep the anonymous password-reset lane and the authenticated
        # email-verification lane on disjoint buckets: without `purpose`,
        # reset spray against a victim address drains that user's
        # verification quota (and the shared instance-wide backstop).
        payload = (
            f"dtxwiki.password_recovery.rate.v1\0{purpose}\0{bucket_kind}\0{value}"
        ).encode()
        digest = hmac.new(self.secret.encode(), payload, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    @staticmethod
    def _claim_slot(
        *,
        session: Session,
        bucket_kind: str,
        bucket_digest: str,
        window_started_at: datetime,
        expires_at: datetime,
        limit: int,
    ) -> bool:
        # A saturated bucket must cost one SELECT, not `limit` failed INSERTs
        # (AUTH-06). Counting first only to pick the starting slot — and still
        # probing up to `limit` — is what keeps this correct under
        # concurrency: returning False here means every slot in
        # [used + 1, limit] is taken, i.e. the bucket really is full. A fixed
        # retry count would falsely reject a request that landed on an
        # already-occupied slot while capacity remained.
        used = int(
            session.scalar(
                select(func.count())
                .select_from(PasswordRecoveryRateLimitSlot)
                .where(
                    PasswordRecoveryRateLimitSlot.bucket_digest == bucket_digest,
                    PasswordRecoveryRateLimitSlot.window_started_at
                    == window_started_at,
                )
            )
            or 0
        )
        if used >= limit:
            return False
        for slot in range(used + 1, limit + 1):
            try:
                with session.begin_nested():
                    session.add(
                        PasswordRecoveryRateLimitSlot(
                            bucket_kind=bucket_kind,
                            bucket_digest=bucket_digest,
                            window_started_at=window_started_at,
                            slot=slot,
                            expires_at=expires_at,
                        )
                    )
                    session.flush()
                return True
            except IntegrityError:
                continue
        return False


@dataclass(frozen=True)
class PasswordResetConsumeResult:
    user: AuthenticatedUser
    revoked_credentials: dict[str, int]


@dataclass(frozen=True)
class PasswordRecoveryService:
    session_factory: object
    credential_hasher: CredentialHasher
    public_base_url: str | None
    audit_service: AuditService = field(default_factory=AuditService)
    mail_delivery_service: EmailDeliveryService | None = None
    now: Callable[[], datetime] = utc_now

    def __post_init__(self) -> None:
        if self.public_base_url is None:
            return
        normalized = self.public_base_url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise PasswordRecoveryConfigurationError(
                "password reset links require a canonical HTTPS public base URL"
            )
        object.__setattr__(self, "public_base_url", normalized)

    def issue_password_reset(
        self,
        *,
        email: str,
        delivery: str = "link",
        expires_in_seconds: int = DEFAULT_PASSWORD_RESET_TTL_SECONDS,
        requested_by_user_id: UUID | None = None,
        requested_via: str = "cli",
        actor_kind: str = "system",
        actor_id: str = "cli:unknown",
        revoke_api_tokens: bool = False,
    ) -> PasswordResetIssueResult:
        normalized_email = normalize_email(email)
        # Email-address and user-id issue lanes must share the same Tier-B
        # single-process lock. Different lock keys could otherwise let an
        # administrator reset race a public/CLI reissue for the same user on
        # SQLite, where SELECT ... FOR UPDATE is unavailable.
        with _PASSWORD_RESET_ISSUE_LOCK:
            return self._issue_password_reset_serialized(
                email=normalized_email,
                delivery=delivery,
                expires_in_seconds=expires_in_seconds,
                requested_by_user_id=requested_by_user_id,
                requested_via=requested_via,
                actor_kind=actor_kind,
                actor_id=actor_id,
                revoke_api_tokens=revoke_api_tokens,
            )

    def issue_password_reset_for_user(
        self,
        *,
        target_user_id: UUID,
        delivery: str = "link",
        expires_in_seconds: int = DEFAULT_PASSWORD_RESET_TTL_SECONDS,
        requested_by_user_id: UUID,
        requested_by_session_generation: int,
        actor_kind: str = "human",
        actor_id: str,
        revoke_api_tokens: bool = False,
    ) -> PasswordResetIssueResult:
        """Issue an administrator reset bound to the selected user identity."""

        with _PASSWORD_RESET_ISSUE_LOCK:
            return self._issue_password_reset_serialized(
                email=None,
                target_user_id=target_user_id,
                delivery=delivery,
                expires_in_seconds=expires_in_seconds,
                requested_by_user_id=requested_by_user_id,
                requested_by_session_generation=requested_by_session_generation,
                requested_via="server_admin",
                actor_kind=actor_kind,
                actor_id=actor_id,
                revoke_api_tokens=revoke_api_tokens,
            )

    def _issue_password_reset_serialized(
        self,
        *,
        email: str | None,
        target_user_id: UUID | None = None,
        delivery: str = "link",
        expires_in_seconds: int = DEFAULT_PASSWORD_RESET_TTL_SECONDS,
        requested_by_user_id: UUID | None = None,
        requested_by_session_generation: int | None = None,
        requested_via: str = "cli",
        actor_kind: str = "system",
        actor_id: str = "cli:unknown",
        revoke_api_tokens: bool = False,
    ) -> PasswordResetIssueResult:
        if delivery not in {"email", "link"}:
            raise InvalidPasswordResetRequestError("unsupported reset delivery")
        if requested_via not in {"public", "server_admin", "cli"}:
            raise InvalidPasswordResetRequestError("unsupported reset request source")
        if requested_via == "public":
            # AUTH-01: public self-service recovery is the "I may be
            # compromised" lane. Force credential revocation here, at the
            # service boundary, so it is enforced for every current and
            # future caller of this method that passes requested_via="public"
            # — not dependent on any one HTTP handler remembering to pass
            # the flag. server_admin/cli callers pass their own explicit
            # choice above and are unaffected (zero behavior change).
            revoke_api_tokens = True
        validate_password_reset_ttl(expires_in_seconds)
        if self.public_base_url is None:
            raise PasswordRecoveryConfigurationError(
                "password reset links require a canonical HTTPS public base URL"
            )
        normalized_email = normalize_email(email) if email is not None else None
        raw_token = generate_password_reset_token(
            revoke_api_tokens=revoke_api_tokens
        )
        token_digest = password_reset_token_digest(raw_token)
        issued_at = self.now()
        expires_at = issued_at + timedelta(seconds=expires_in_seconds)
        with self.session_factory.begin() as session:
            if requested_via == "server_admin":
                if (
                    requested_by_user_id is None
                    or requested_by_session_generation is None
                    or target_user_id is None
                ):
                    raise PasswordRecoveryAuthorizationError(
                        "server-admin grant is required"
                    )
                locked_users: dict[UUID, User | None] = {}
                for locked_user_id in sorted(
                    {requested_by_user_id, target_user_id}, key=lambda value: value.int
                ):
                    locked_users[locked_user_id] = session.scalar(
                        select(User)
                        .where(User.id == locked_user_id)
                        .with_for_update()
                    )
                requester = locked_users[requested_by_user_id]
                if (
                    requester is None
                    or requester.status != "active"
                    or requester.session_generation
                    != requested_by_session_generation
                ):
                    raise PasswordRecoveryRecentReauthenticationRequiredError(
                        "recent password reauthentication is required"
                    )
                live_grant = session.scalar(
                    select(ServerAdmin)
                    .where(ServerAdmin.user_id == requested_by_user_id)
                    .with_for_update()
                )
                if live_grant is None:
                    raise PasswordRecoveryAuthorizationError(
                        "server-admin grant is required"
                    )
                user = locked_users[target_user_id]
            else:
                user = session.scalar(
                    select(User)
                    .where(User.email == normalized_email)
                    .with_for_update()
                )
            if user is None or user.status != "active":
                raise PasswordRecoveryUserNotFoundError("active user not found")
            if delivery == "email" and user.email_verified_at is None:
                raise PasswordRecoveryConfigurationError(
                    "email delivery requires a verified user email"
                )
            if delivery == "email" and self.mail_delivery_service is None:
                raise PasswordRecoveryConfigurationError(
                    "email recovery is not configured"
                )
            self._revoke_active_tokens(
                session=session,
                user_id=user.id,
                purpose="password_reset",
                revoked_at=issued_at,
            )
            model = PasswordResetToken(
                user_id=user.id,
                token_digest=token_digest,
                purpose="password_reset",
                delivery=delivery,
                requested_by_user_id=requested_by_user_id,
                requested_via=requested_via,
                revoke_api_tokens=revoke_api_tokens,
                created_at=issued_at,
                expires_at=expires_at,
            )
            session.add(model)
            session.flush()
            outbox_id = None
            if delivery == "email":
                assert self.mail_delivery_service is not None
                try:
                    outbox = self.mail_delivery_service.queue_token_email(
                        session=session,
                        user=user,
                        token_model=model,
                        raw_token=raw_token,
                        message_kind="password_reset",
                    )
                    outbox_id = outbox.id
                except MailDeliveryError as exc:
                    raise PasswordRecoveryConfigurationError(str(exc)) from exc
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="auth.password.reset.issued",
                target_kind="user",
                target_id=user.id,
                payload={
                    "requested_via": requested_via,
                    "delivery": delivery,
                    "expires_at": expires_at.isoformat(),
                    "revoke_api_tokens": revoke_api_tokens,
                },
                event_summary="A one-time password reset was issued",
                retention_class="sensitive",
            )
            return PasswordResetIssueResult(
                token_id=model.id,
                user_id=user.id,
                reset_url=one_time_token_link(
                    self.public_base_url, PASSWORD_RESET_LINK_PATH, raw_token
                ),
                expires_at=expires_at,
                delivery=delivery,
                outbox_id=outbox_id,
            )

    @staticmethod
    def is_public_password_reset_eligible(
        *,
        session: Session,
        email: str,
    ) -> bool:
        """Return public-email eligibility behind the service boundary."""

        normalized_email = normalize_email(email)
        user = session.scalar(select(User).where(User.email == normalized_email))
        return bool(
            user is not None
            and user.status == "active"
            and user.email_verified_at is not None
        )

    def peek_password_reset_token(self, *, token: str) -> None:
        """Read-only validity check, raising the same error subclasses as
        ``consume_password_reset`` without consuming the token or taking its
        row locks.

        M-23 (260908 UI audit): the reset screen showed the new-password form
        for an expired/used/malformed link and only surfaced that after the
        visitor filled it out and submitted. The token is a CSPRNG secret
        (see ``generate_password_reset_token``), so a caller must already
        possess it -- this exposes no more than ``consume_password_reset``
        already does to anyone holding the same link, and unlike that
        endpoint it changes no state, so it carries no additional
        rate-limiting requirement.
        """
        digest = password_reset_token_digest(token)
        checked_at = self.now()
        with self.session_factory.begin() as session:
            user_id = session.scalar(
                select(PasswordResetToken.user_id).where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "password_reset",
                )
            )
            if user_id is None:
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            user = session.scalar(select(User).where(User.id == user_id))
            if user is None or user.status != "active":
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            model = session.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "password_reset",
                    PasswordResetToken.user_id == user.id,
                )
            )
            if model is None:
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            _raise_password_reset_token_state(model, at=checked_at)

    def consume_password_reset(
        self,
        *,
        token: str,
        new_password: str,
        revoke_api_tokens: bool = False,
    ) -> PasswordResetConsumeResult:
        validate_password(new_password)
        digest = password_reset_token_digest(token)
        consumed_at = self.now()
        with self.session_factory.begin() as session:
            user_id = session.scalar(
                select(PasswordResetToken.user_id).where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "password_reset",
                )
            )
            if user_id is None:
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.status != "active":
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            model = session.scalar(
                select(PasswordResetToken)
                .where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "password_reset",
                    PasswordResetToken.user_id == user.id,
                )
                .with_for_update()
            )
            if model is None:
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            _raise_password_reset_token_state(model, at=consumed_at)
            claimed = session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == model.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.revoked_at.is_(None),
                    PasswordResetToken.expires_at > consumed_at,
                )
                .values(used_at=consumed_at)
            )
            if claimed.rowcount != 1:
                session.refresh(model)
                _raise_password_reset_token_state(model, at=consumed_at)
                raise InvalidPasswordResetTokenError(
                    "password reset link is invalid or unavailable"
                )
            user.password_hash = self.credential_hasher.hash_password(new_password)
            user.session_generation += 1
            self._revoke_active_tokens(
                session=session,
                user_id=user.id,
                purpose="password_reset",
                revoked_at=consumed_at,
                exclude_token_id=model.id,
            )
            terminalize_token_outboxes(
                session=session,
                token_ids=[model.id],
                terminal_at=consumed_at,
            )
            revoked_credentials = {
                "api_tokens": 0,
                "oauth_refresh_tokens": 0,
                "oauth_access_tokens": 0,
                "oauth_authorization_codes": 0,
            }
            effective_revoke_api_tokens = (
                model.revoke_api_tokens or revoke_api_tokens
            )
            if effective_revoke_api_tokens:
                api_tokens = list(
                    session.scalars(
                        select(ApiToken).where(
                            ApiToken.user_id == user.id,
                            ApiToken.revoked_at.is_(None),
                        )
                    ).all()
                )
                for api_token in api_tokens:
                    api_token.revoked_at = consumed_at
                revoked_credentials["api_tokens"] = len(api_tokens)
                revoked_credentials.update(
                    revoke_all_user_oauth_credentials(
                        session,
                        user_id=user.id,
                        revoked_at=consumed_at,
                    )
                )
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(user.id),
                action="auth.password.reset.completed",
                target_kind="user",
                target_id=user.id,
                payload={
                    "requested_via": model.requested_via,
                    "delivery": model.delivery,
                    "sessions_revoked": True,
                    "revoke_api_tokens": effective_revoke_api_tokens,
                    "revoked_credentials": revoked_credentials,
                },
                event_summary="A user completed a one-time password reset",
                retention_class="sensitive",
            )
            if self.mail_delivery_service is not None:
                try:
                    self.mail_delivery_service.queue_password_changed_notification(
                        session=session,
                        user=user,
                    )
                except MailDeliveryError:
                    # Notification availability must not undo the credential
                    # change. Delivery configuration is surfaced separately in
                    # Deployment and the outbox worker.
                    pass
            session.flush()
            return PasswordResetConsumeResult(
                user=authenticated_user(user),
                revoked_credentials=revoked_credentials,
            )

    def issue_email_verification(
        self,
        *,
        user_id: UUID,
        actor_id: str,
        expires_in_seconds: int = DEFAULT_EMAIL_VERIFICATION_TTL_SECONDS,
    ) -> PasswordResetIssueResult:
        with _EMAIL_VERIFICATION_ISSUE_LOCK:
            return self._issue_email_verification_serialized(
                user_id=user_id,
                actor_id=actor_id,
                expires_in_seconds=expires_in_seconds,
            )

    def _issue_email_verification_serialized(
        self,
        *,
        user_id: UUID,
        actor_id: str,
        expires_in_seconds: int,
    ) -> PasswordResetIssueResult:
        if self.public_base_url is None or self.mail_delivery_service is None:
            raise PasswordRecoveryConfigurationError(
                "email verification is not configured"
            )
        validate_password_reset_ttl(expires_in_seconds)
        raw_token = generate_email_verification_token()
        digest = recovery_token_digest(raw_token, purpose="email_verification")
        issued_at = self.now()
        expires_at = issued_at + timedelta(seconds=expires_in_seconds)
        with self.session_factory.begin() as session:
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.status != "active":
                raise PasswordRecoveryUserNotFoundError("active user not found")
            if user.email_verified_at is not None:
                return PasswordResetIssueResult(
                    token_id=user.id,
                    user_id=user.id,
                    reset_url="",
                    expires_at=user.email_verified_at,
                    delivery="email",
                    outbox_id=None,
                )
            self._revoke_active_tokens(
                session=session,
                user_id=user.id,
                purpose="email_verification",
                revoked_at=issued_at,
            )
            model = PasswordResetToken(
                user_id=user.id,
                token_digest=digest,
                purpose="email_verification",
                delivery="email",
                requested_by_user_id=user.id,
                requested_via="public",
                revoke_api_tokens=False,
                created_at=issued_at,
                expires_at=expires_at,
            )
            session.add(model)
            session.flush()
            try:
                outbox = self.mail_delivery_service.queue_token_email(
                    session=session,
                    user=user,
                    token_model=model,
                    raw_token=raw_token,
                    message_kind="email_verification",
                )
            except MailDeliveryError as exc:
                raise PasswordRecoveryConfigurationError(str(exc)) from exc
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=actor_id,
                action="auth.email.verification.issued",
                target_kind="user",
                target_id=user.id,
                payload={"delivery": "email", "expires_at": expires_at.isoformat()},
                event_summary="A user requested email verification",
                retention_class="sensitive",
            )
            return PasswordResetIssueResult(
                token_id=model.id,
                user_id=user.id,
                reset_url=one_time_token_link(
                    self.public_base_url, EMAIL_VERIFICATION_LINK_PATH, raw_token
                ),
                expires_at=expires_at,
                delivery="email",
                outbox_id=outbox.id,
            )

    def consume_email_verification(
        self,
        *,
        token: str,
    ) -> EmailVerificationConsumeResult:
        digest = recovery_token_digest(token, purpose="email_verification")
        verified_at = self.now()
        with self.session_factory.begin() as session:
            user_id = session.scalar(
                select(PasswordResetToken.user_id).where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "email_verification",
                )
            )
            if user_id is None:
                raise InvalidPasswordResetTokenError(
                    "email verification link is invalid or unavailable"
                )
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.status != "active":
                raise InvalidPasswordResetTokenError(
                    "email verification link is invalid or unavailable"
                )
            model = session.scalar(
                select(PasswordResetToken)
                .where(
                    PasswordResetToken.token_digest == digest,
                    PasswordResetToken.purpose == "email_verification",
                    PasswordResetToken.user_id == user.id,
                )
                .with_for_update()
            )
            if model is None:
                raise InvalidPasswordResetTokenError(
                    "email verification link is invalid or unavailable"
                )
            claimed = session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == model.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.revoked_at.is_(None),
                    PasswordResetToken.expires_at > verified_at,
                )
                .values(used_at=verified_at)
            )
            if claimed.rowcount != 1:
                raise InvalidPasswordResetTokenError(
                    "email verification link is invalid or unavailable"
                )
            user.email_verified_at = verified_at
            self._revoke_active_tokens(
                session=session,
                user_id=user.id,
                purpose="email_verification",
                revoked_at=verified_at,
                exclude_token_id=model.id,
            )
            terminalize_token_outboxes(
                session=session,
                token_ids=[model.id],
                terminal_at=verified_at,
            )
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(user.id),
                action="auth.email.verified",
                target_kind="user",
                target_id=user.id,
                payload={},
                event_summary="A user verified their email address",
                retention_class="sensitive",
            )
            session.flush()
            return EmailVerificationConsumeResult(
                user=authenticated_user(user),
                verified_at=verified_at,
            )

    def _revoke_active_tokens(
        self,
        *,
        session: Session,
        user_id: UUID,
        purpose: str,
        revoked_at: datetime,
        exclude_token_id: UUID | None = None,
    ) -> int:
        statement = (
            select(PasswordResetToken.id)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.purpose == purpose,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if exclude_token_id is not None:
            statement = statement.where(PasswordResetToken.id != exclude_token_id)
        token_ids = list(session.scalars(statement).all())
        if not token_ids:
            return 0
        session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id.in_(token_ids))
            .values(revoked_at=revoked_at)
        )
        terminalize_token_outboxes(
            session=session,
            token_ids=token_ids,
            terminal_at=revoked_at,
        )
        return len(token_ids)

def generate_password_reset_token(*, revoke_api_tokens: bool = False) -> str:
    revocation_flag = "1" if revoke_api_tokens else "0"
    # token_urlsafe(32) contributes 256 bits of CSPRNG entropy.  The clear flag
    # lets the reset UI describe the issue-time credential scope, while the
    # digest over the complete token makes changing that flag invalidate it.
    return (
        f"{PASSWORD_RESET_TOKEN_PREFIX}{revocation_flag}_"
        f"{secrets.token_urlsafe(32)}"
    )


def generate_email_verification_token() -> str:
    return f"{EMAIL_VERIFICATION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def password_reset_token_digest(token: str) -> str:
    return recovery_token_digest(token, purpose="password_reset")


def _raise_password_reset_token_state(
    token: PasswordResetToken,
    *,
    at: datetime,
) -> None:
    if token.used_at is not None:
        raise UsedPasswordResetTokenError(
            "password reset link has already been used"
        )
    if token.revoked_at is not None:
        raise InvalidPasswordResetTokenError(
            "password reset link is invalid or unavailable"
        )
    if token.expires_at <= at:
        raise ExpiredPasswordResetTokenError("password reset link has expired")


def recovery_token_digest(token: str, *, purpose: str) -> str:
    if purpose == "password_reset":
        prefix = PASSWORD_RESET_TOKEN_PREFIX
        namespace = PASSWORD_RESET_TOKEN_NAMESPACE
        invalid_message = "password reset link is invalid or unavailable"
    elif purpose == "email_verification":
        prefix = EMAIL_VERIFICATION_TOKEN_PREFIX
        namespace = EMAIL_VERIFICATION_TOKEN_NAMESPACE
        invalid_message = "email verification link is invalid or unavailable"
    else:
        raise InvalidPasswordResetTokenError("unsupported recovery token purpose")
    if not token or not token.startswith(prefix):
        raise InvalidPasswordResetTokenError(invalid_message)
    if purpose == "password_reset":
        encoded = token[len(PASSWORD_RESET_TOKEN_PREFIX) :]
        try:
            revocation_flag, random_payload = encoded.split("_", maxsplit=1)
        except ValueError as exc:
            raise InvalidPasswordResetTokenError(invalid_message) from exc
        if (
            revocation_flag not in {"0", "1"}
            or len(random_payload) < 43
            or any(
                character not in _TOKEN_URLSAFE_ALPHABET
                for character in random_payload
            )
        ):
            raise InvalidPasswordResetTokenError(invalid_message)
    payload = f"{namespace}\0{purpose}\0{token}".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_password_reset_ttl(expires_in_seconds: int) -> None:
    if not (
        MIN_PASSWORD_RESET_TTL_SECONDS
        <= expires_in_seconds
        <= MAX_PASSWORD_RESET_TTL_SECONDS
    ):
        raise InvalidPasswordResetRequestError(
            "password reset expiry must be between 5 minutes and 24 hours"
        )
