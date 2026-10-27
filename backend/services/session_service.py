import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.settings import (
    SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS,
    SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS,
    SESSION_REMEMBERED_TTL_DEFAULT_SECONDS,
    SESSION_TTL_DEFAULT_SECONDS,
    Settings,
)
from backend.domain import identity_policy
from backend.domain.credential_input import (
    SESSION_TOKEN_MAX_BYTES,
    ascii_token_is_valid,
    require_ascii_token,
)
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.personal_workspace_service import create_personal_workspace
from backend.utils.database_errors import is_single_column_unique_violation

logger = logging.getLogger("dtxwiki.session")

SESSION_TOKEN_KIND = "session"
SESSION_TOKEN_VERSION = 1
# Single source of truth: these are the same literals Settings exposes to
# operators. Keeping a second copy here let the codec fall back to a lifetime
# that contradicted the documented Settings default whenever a construction
# site omitted the kwargs, with every test still green.
DEFAULT_SESSION_TTL_SECONDS = SESSION_TTL_DEFAULT_SECONDS
DEFAULT_REMEMBERED_SESSION_TTL_SECONDS = SESSION_REMEMBERED_TTL_DEFAULT_SECONDS
DEFAULT_REMEMBERED_SESSION_REFRESH_THRESHOLD_SECONDS = (
    SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS
)
DEFAULT_REMEMBERED_SESSION_ABSOLUTE_TTL_SECONDS = (
    SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS
)
RECENT_REAUTHENTICATION_TOKEN_KIND = "recent_reauthentication"
RECENT_REAUTHENTICATION_TOKEN_VERSION = 1
RECENT_REAUTHENTICATION_TTL_SECONDS = 5 * 60
MIN_PASSWORD_LENGTH = identity_policy.NEW_PASSWORD_MIN_LENGTH
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$u2au8gUQ3qKl1XimMaOpFQ"
    "$Kvoeh33YAsJ5V/VMP8lB4wS0V3JS85RQUdHFy8Ew0CA"
)
_AUTH_IP_BUCKET: dict[str, list[float]] = {}
_AUTH_EMAIL_BUCKET: dict[str, list[float]] = {}
_AUTH_BUCKET_LOCK = threading.Lock()
_LAST_AUTH_BUCKET_SWEEP = 0.0
AUTH_RATE_LIMIT_PER_IP = 120  # attempts per minute
AUTH_RATE_LIMIT_PER_EMAIL = 10  # attempts per minute


class SessionServiceError(RuntimeError):
    error_code = "auth_failed"


class AuthRateLimitError(SessionServiceError):
    """Login/signup rate-limit exceeded. The endpoint layer maps this to 429."""

    error_code = "rate_limited"


class DuplicateEmailError(SessionServiceError):
    error_code = "duplicate_email"


class InvalidCredentialsError(SessionServiceError):
    error_code = "invalid_credentials"


class InvalidSessionError(SessionServiceError):
    error_code = "invalid_session"


class RecentReauthenticationRequiredError(SessionServiceError):
    error_code = "recent_reauthentication_required"


class SuspendedUserError(SessionServiceError):
    error_code = "user_suspended"


class PasswordPolicyError(SessionServiceError):
    error_code = "password_policy_failed"


class EmailInputPolicyError(SessionServiceError):
    error_code = "email_policy_failed"


class DisplayNameInputPolicyError(SessionServiceError):
    error_code = "display_name_policy_failed"


class SignupDisabledError(SessionServiceError):
    error_code = "signup_disabled"


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str
    display_name: str
    status: str
    email_verified_at: datetime | None = None


@dataclass(frozen=True)
class VerifiedSession:
    user_id: UUID
    session_generation: int
    expires_at: datetime
    remember_me: bool
    authenticated_at: datetime | None


@dataclass(frozen=True)
class SessionResult:
    user: AuthenticatedUser
    session_token: str
    issued_at: datetime
    expires_at: datetime
    remember_me: bool


@dataclass(frozen=True)
class RecentReauthenticationResult:
    proof_token: str
    reauthenticated_until: datetime


@dataclass(frozen=True)
class VerifiedRecentReauthentication:
    user_id: UUID
    session_generation: int


@dataclass(frozen=True)
class RecentReauthenticationCodec:
    secret: str
    ttl_seconds: int = RECENT_REAUTHENTICATION_TTL_SECONDS
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("recent reauthentication secret must not be empty")
        if self.ttl_seconds <= 0:
            raise ValueError("recent reauthentication ttl must be positive")

    def issue(
        self,
        *,
        user_id: UUID,
        session_generation: int,
    ) -> RecentReauthenticationResult:
        issued_at = self.now().replace(microsecond=0)
        expires_at = issued_at + timedelta(seconds=self.ttl_seconds)
        payload = {
            "v": RECENT_REAUTHENTICATION_TOKEN_VERSION,
            "kind": RECENT_REAUTHENTICATION_TOKEN_KIND,
            "sub": str(user_id),
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": secrets.token_urlsafe(16),
            "sg": session_generation,
        }
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _base64url_encode(self._sign(encoded_payload.encode()))
        return RecentReauthenticationResult(
            proof_token=require_ascii_token(
                f"{encoded_payload}.{signature}",
                SESSION_TOKEN_MAX_BYTES,
                "recent reauthentication proof",
            ),
            reauthenticated_until=expires_at,
        )

    def verify(self, token: str) -> VerifiedRecentReauthentication:
        if not ascii_token_is_valid(token, SESSION_TOKEN_MAX_BYTES):
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof is invalid"
            )
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        except ValueError as exc:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof is malformed"
            ) from exc
        expected_signature = _base64url_encode(self._sign(encoded_payload.encode()))
        if not hmac.compare_digest(encoded_signature, expected_signature):
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof signature is invalid"
            )
        try:
            payload = json.loads(_base64url_decode(encoded_payload))
        except (ValueError, json.JSONDecodeError) as exc:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof payload is invalid"
            ) from exc
        if payload.get("v") != RECENT_REAUTHENTICATION_TOKEN_VERSION:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof version is unsupported"
            )
        if payload.get("kind") != RECENT_REAUTHENTICATION_TOKEN_KIND:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof kind is unsupported"
            )
        exp = payload.get("exp")
        if not isinstance(exp, int) or self.now().timestamp() >= exp:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof is expired"
            )
        session_generation = payload.get("sg")
        if not isinstance(session_generation, int) or session_generation < 0:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof generation is invalid"
            )
        try:
            return VerifiedRecentReauthentication(
                user_id=UUID(str(payload["sub"])),
                session_generation=session_generation,
            )
        except (KeyError, ValueError) as exc:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof subject is invalid"
            ) from exc

    def _sign(self, encoded_payload: bytes) -> bytes:
        signing_key = f"dtxwiki.recent_reauthentication.v1\0{self.secret}".encode()
        return hmac.new(
            signing_key,
            encoded_payload,
            digestmod=hashlib.sha256,
        ).digest()


@dataclass(frozen=True)
class SessionTokenCodec:
    secret: str
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    remembered_ttl_seconds: int = DEFAULT_REMEMBERED_SESSION_TTL_SECONDS
    remembered_refresh_threshold_seconds: int = (
        DEFAULT_REMEMBERED_SESSION_REFRESH_THRESHOLD_SECONDS
    )
    remembered_absolute_ttl_seconds: int = (
        DEFAULT_REMEMBERED_SESSION_ABSOLUTE_TTL_SECONDS
    )
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("session secret must not be empty")
        if self.ttl_seconds <= 0:
            raise ValueError("session ttl must be positive")
        if self.remembered_ttl_seconds <= 0:
            raise ValueError("remembered session ttl must be positive")
        if self.remembered_refresh_threshold_seconds <= 0:
            raise ValueError("remembered session refresh threshold must be positive")
        if self.remembered_absolute_ttl_seconds <= 0:
            raise ValueError("remembered session absolute ttl must be positive")
        if self.remembered_refresh_threshold_seconds >= self.remembered_ttl_seconds:
            raise ValueError(
                "remembered session refresh threshold must be less than its ttl"
            )
        if self.remembered_ttl_seconds > self.remembered_absolute_ttl_seconds:
            raise ValueError("remembered session ttl must not exceed its absolute ttl")

    def issue(
        self,
        user: User,
        *,
        remember_me: bool = False,
        authenticated_at: datetime | None = None,
        issued_at: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued_at = (issued_at or self.now()).astimezone(UTC).replace(microsecond=0)
        authenticated_at = (
            (authenticated_at or issued_at).astimezone(UTC).replace(microsecond=0)
        )
        if authenticated_at > issued_at:
            raise ValueError("session authentication time must not be in the future")
        ttl_seconds = self.remembered_ttl_seconds if remember_me else self.ttl_seconds
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        if remember_me:
            expires_at = min(
                expires_at,
                authenticated_at
                + timedelta(seconds=self.remembered_absolute_ttl_seconds),
            )
        payload = {
            "v": SESSION_TOKEN_VERSION,
            "kind": SESSION_TOKEN_KIND,
            "sub": str(user.id),
            "email": user.email,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": secrets.token_urlsafe(16),
            "sg": user.session_generation,
            "remembered": remember_me,
            "auth_time": int(authenticated_at.timestamp()),
        }
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _base64url_encode(self._sign(encoded_payload.encode()))
        token = require_ascii_token(
            f"{encoded_payload}.{signature}", SESSION_TOKEN_MAX_BYTES, "session token"
        )
        return token, expires_at

    def verify(self, token: str) -> UUID:
        return self.verify_session(token).user_id

    def verify_session(self, token: str) -> VerifiedSession:
        if not ascii_token_is_valid(token, SESSION_TOKEN_MAX_BYTES):
            raise InvalidSessionError("session token is invalid")
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        except ValueError as exc:
            raise InvalidSessionError("session token is malformed") from exc
        expected_signature = _base64url_encode(self._sign(encoded_payload.encode()))
        if not hmac.compare_digest(encoded_signature, expected_signature):
            raise InvalidSessionError("session token signature is invalid")
        try:
            payload = json.loads(_base64url_decode(encoded_payload))
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidSessionError("session token payload is invalid") from exc
        if payload.get("v") != SESSION_TOKEN_VERSION:
            raise InvalidSessionError("session token version is unsupported")
        if payload.get("kind") != SESSION_TOKEN_KIND:
            raise InvalidSessionError("session token kind is unsupported")
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise InvalidSessionError("session token expiry is invalid")
        if self.now().timestamp() >= exp:
            raise InvalidSessionError("session token is expired")
        # AUTH-08: fail closed like the RecentReauthenticationCodec twin
        # above — a token missing `sg` must not silently pass as
        # generation 0 (which matches every never-revoked user). `issue`
        # always sets `sg`, so the only real path here is a token from a
        # build that predates this claim.
        session_generation = payload.get("sg")
        if not isinstance(session_generation, int) or session_generation < 0:
            raise InvalidSessionError("session token generation is invalid")
        try:
            expires_at = datetime.fromtimestamp(exp, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise InvalidSessionError("session token expiry is invalid") from exc

        # Added to v1 tokens without changing the token version so sessions
        # issued by older deployments remain usable until their original exp.
        # Missing or malformed renewal claims fail closed to a non-remembered,
        # non-refreshable session rather than forcing a deployment-wide logout.
        remember_me = False
        authenticated_at = None
        remembered_claim = payload.get("remembered")
        authenticated_at_claim = payload.get("auth_time")
        issued_at_claim = payload.get("iat")
        if (
            remembered_claim is True
            and isinstance(authenticated_at_claim, int)
            and isinstance(issued_at_claim, int)
            and 0 <= authenticated_at_claim <= issued_at_claim <= exp
        ):
            try:
                authenticated_at = datetime.fromtimestamp(
                    authenticated_at_claim,
                    tz=UTC,
                )
            except (OverflowError, OSError, ValueError):
                authenticated_at = None
            else:
                remember_me = True
        try:
            return VerifiedSession(
                user_id=UUID(str(payload["sub"])),
                session_generation=session_generation,
                expires_at=expires_at,
                remember_me=remember_me,
                authenticated_at=authenticated_at,
            )
        except (KeyError, ValueError) as exc:
            raise InvalidSessionError("session token subject is invalid") from exc

    def remembered_refresh_due(self, verified: VerifiedSession) -> bool:
        if not verified.remember_me or verified.authenticated_at is None:
            return False
        now = self.now().astimezone(UTC).replace(microsecond=0)
        remaining_seconds = (verified.expires_at - now).total_seconds()
        if remaining_seconds <= 0:
            return False
        if remaining_seconds > self.remembered_refresh_threshold_seconds:
            return False
        candidate_expiry = min(
            now + timedelta(seconds=self.remembered_ttl_seconds),
            verified.authenticated_at
            + timedelta(seconds=self.remembered_absolute_ttl_seconds),
        )
        return candidate_expiry > verified.expires_at

    def _sign(self, encoded_payload: bytes) -> bytes:
        return hmac.new(
            self.secret.encode(),
            encoded_payload,
            digestmod=hashlib.sha256,
        ).digest()


def build_session_token_codec(settings: Settings) -> SessionTokenCodec:
    """Wire operator Settings into the codec.

    The construction is duplicated at every call site otherwise (the REST
    dependency in backend.api.auth and the cookie path in
    backend.policy.auth), and each site has to be edited in lockstep whenever
    a lifetime field is added. A site that misses one silently falls back to
    the module defaults instead of the operator's configuration.
    """

    return SessionTokenCodec(
        secret=settings.effective_session_secret,
        ttl_seconds=settings.session_ttl_seconds,
        remembered_ttl_seconds=settings.session_remembered_ttl_seconds,
        remembered_refresh_threshold_seconds=(
            settings.session_remembered_refresh_threshold_seconds
        ),
        remembered_absolute_ttl_seconds=(
            settings.session_remembered_absolute_ttl_seconds
        ),
    )


@dataclass(frozen=True)
class SessionService:
    session_factory: Callable
    credential_hasher: CredentialHasher
    token_codec: SessionTokenCodec
    # Deployment mode still controls whether signup is exposed, but not the
    # starter workspace every new account receives.
    app_mode: str = "solo_local"
    # AUTH-09: fail closed. HEAD's only production factory
    # (get_session_service in backend/api/auth.py) always passes
    # settings.effective_signup_enabled explicitly, so this default is
    # unreachable today — but a permissive default here is exactly the
    # pattern the project bans for security parameters, and it's a trap for
    # the next call site that omits the argument.
    signup_enabled: bool = False
    audit_service: AuditService | None = None
    recent_reauthentication_codec: RecentReauthenticationCodec | None = None

    def assert_signup_enabled(self) -> None:
        if not self.signup_enabled:
            raise SignupDisabledError("self-service signup is disabled")

    def signup(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None = None,
        remember_me: bool = False,
    ) -> SessionResult:
        # Recheck at the domain write boundary even though the HTTP endpoint
        # rejects disabled requests before consuming a rate-limit slot.
        self.assert_signup_enabled()
        normalized_email = normalize_email(email)
        validate_password(password)
        name = normalize_display_name(display_name, normalized_email)
        with self.session_factory.begin() as session:
            user = User(
                email=normalized_email,
                password_hash=self.credential_hasher.hash_password(password),
                display_name=name,
                status="active",
            )
            session.add(user)
            try:
                session.flush()
            except IntegrityError as exc:
                if not is_single_column_unique_violation(
                    exc, table="users", column="email"
                ):
                    raise
                raise DuplicateEmailError("email already exists") from exc
            # Onboarding is decoupled from the installation's legacy/default
            # content workspace.  Every signup mode starts in its own ordinary
            # private workspace and receives no implicit global membership.
            create_personal_workspace(session, user=user)
            self._emit_session_audit(
                session,
                user_id=user.id,
                action="auth.user.signed_up",
                target_kind="user",
                target_id=user.id,
                # AUTH-10: no email — target_id already carries a queryable
                # user id, and the email's presence is what makes this row a
                # PII-bearing row that outlives the account.
                payload={"display_name": user.display_name},
            )
            return self._issue_session(user, remember_me=remember_me)

    def login(
        self,
        *,
        email: str,
        password: str,
        remember_me: bool = False,
    ) -> SessionResult:
        validate_password(password, new=False)
        normalized_email = normalize_email(email)
        with self.session_factory.begin() as session:
            user = session.scalar(select(User).where(User.email == normalized_email))
            if user is None:
                self.credential_hasher.verify_password(password, DUMMY_PASSWORD_HASH)
                raise InvalidCredentialsError("email or password is invalid")
            if not self.credential_hasher.verify_password(password, user.password_hash):
                raise InvalidCredentialsError("email or password is invalid")
            self._assert_active(user)
            if self.credential_hasher.password_needs_rehash(user.password_hash):
                user.password_hash = self.credential_hasher.hash_password(password)
                session.flush()
            self._emit_session_audit(
                session,
                user_id=user.id,
                action="auth.session.created",
                target_kind="user",
                target_id=user.id,
                payload={},
            )
            return self._issue_session(user, remember_me=remember_me)

    def authenticate_session(self, token: str) -> AuthenticatedUser:
        user, _, _ = self._authenticate_session(token, refresh_remembered=False)
        return user

    def authenticate_session_context(
        self,
        token: str,
    ) -> tuple[AuthenticatedUser, VerifiedSession]:
        user, verified, _ = self._authenticate_session(
            token,
            refresh_remembered=False,
        )
        return user, verified

    def authenticate_session_with_refresh(
        self,
        token: str,
    ) -> tuple[AuthenticatedUser, SessionResult | None]:
        """Authenticate and renew an eligible remembered session.

        This is intentionally used only by GET /auth/me. The token is a
        stateless signed cookie, so issuing its replacement cannot revoke the
        prior token. Limiting renewal to the final refresh-threshold window
        bounds that unavoidable overlap to the configured threshold.
        """

        user, _, refreshed = self._authenticate_session(
            token,
            refresh_remembered=True,
        )
        return user, refreshed

    def _authenticate_session(
        self,
        token: str,
        *,
        refresh_remembered: bool,
    ) -> tuple[AuthenticatedUser, VerifiedSession, SessionResult | None]:
        verified = self.token_codec.verify_session(token)
        with self.session_factory() as session:
            user = session.get(User, verified.user_id)
            if user is None:
                raise InvalidSessionError("session user no longer exists")
            self._assert_active(user)
            if user.session_generation != verified.session_generation:
                raise InvalidSessionError("session token has been revoked")
            authenticated = authenticated_user(user)
            refreshed = None
            # `remembered_refresh_due` already requires a non-null
            # authenticated_at, but re-check it explicitly rather than with a
            # bare `assert`: assertions are compiled out under `python -O`, and
            # falling through with None would make `issue` restart the absolute
            # authentication window from now and defeat the 90-day cap.
            if (
                refresh_remembered
                and verified.authenticated_at is not None
                and self.token_codec.remembered_refresh_due(verified)
            ):
                refreshed = self._issue_session(
                    user,
                    remember_me=True,
                    authenticated_at=verified.authenticated_at,
                )
        if refreshed is not None and not self._audit_session_renewal(
            verified=verified,
            refreshed=refreshed,
        ):
            # Never hand out a rotation that could not be recorded. The
            # presented cookie is still inside its validity window (that is
            # what made it eligible), so dropping the rotation costs the
            # visitor nothing and the next /auth/me retries it.
            refreshed = None
        return authenticated, verified, refreshed

    def _audit_session_renewal(
        self,
        *,
        verified: VerifiedSession,
        refreshed: SessionResult,
    ) -> bool:
        """Record a remembered-session rotation. Returns whether it is durable.

        Rotation silently extends a browser credential by up to
        `remembered_ttl_seconds`, so it needs the same trail as
        auth.session.created/destroyed. Without it the only session events an
        operator can reconstruct are the original login and the eventual
        logout, and a stolen remembered cookie can be kept alive all the way to
        the absolute cap without producing a single Activity row.

        Emitted in its own transaction: the caller holds a read-only session
        from `session_factory()`, so a row added there would never commit. A
        write failure must not 500 an otherwise valid /auth/me, so it is
        reported instead of raised and the caller drops the rotation.
        """

        if self.audit_service is None:
            return True
        try:
            with self.session_factory.begin() as session:
                self._emit_session_audit(
                    session,
                    user_id=verified.user_id,
                    action="auth.session.renewed",
                    target_kind="user",
                    target_id=verified.user_id,
                    payload={
                        "previous_expires_at": verified.expires_at.isoformat(),
                        "expires_at": refreshed.expires_at.isoformat(),
                        "authenticated_at": (
                            verified.authenticated_at.isoformat()
                            if verified.authenticated_at is not None
                            else None
                        ),
                    },
                )
        except SQLAlchemyError:
            logger.warning(
                "remembered session renewal audit failed; keeping the presented cookie",
                exc_info=True,
            )
            return False
        return True

    def logout(self, token: str | None) -> None:
        if not token:
            return
        try:
            verified = self.token_codec.verify_session(token)
        except InvalidSessionError:
            return
        with self.session_factory.begin() as session:
            user = session.get(User, verified.user_id)
            if user is None:
                return
            if user.session_generation != verified.session_generation:
                return
            user.session_generation += 1
            session.flush()
            self._emit_session_audit(
                session,
                user_id=user.id,
                action="auth.session.destroyed",
                target_kind="user",
                target_id=user.id,
                payload={},
            )

    def change_password(
        self,
        *,
        user_id: UUID,
        current_password: str,
        new_password: str,
        remember_me: bool = False,
    ) -> SessionResult:
        validate_password(current_password, new=False)
        validate_password(new_password)
        result: SessionResult | None = None
        invalid_password = False
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                raise InvalidSessionError("session user no longer exists")
            self._assert_active(user)
            if not self.credential_hasher.verify_password(
                current_password,
                user.password_hash,
            ):
                # AUTH-04: an incorrect current-password guess used to leave
                # no audit trail at all — mirror reauthenticate's "flag, audit
                # inside the transaction, raise after" pattern so a wrong
                # guess against a stolen session cookie is reconstructable.
                invalid_password = True
                self._emit_session_audit(
                    session,
                    user_id=user.id,
                    action="auth.password.change.failed",
                    target_kind="user",
                    target_id=user.id,
                    payload={"reason": "invalid_credentials"},
                )
            else:
                user.password_hash = self.credential_hasher.hash_password(new_password)
                user.session_generation += 1
                session.flush()
                self._emit_session_audit(
                    session,
                    user_id=user.id,
                    action="auth.password.changed",
                    target_kind="user",
                    target_id=user.id,
                    payload={},
                )
                # Successful current-password verification is a fresh primary
                # authentication event. Preserve whether the browser was
                # remembered, but restart its absolute authentication window.
                result = self._issue_session(user, remember_me=remember_me)
        if invalid_password:
            raise InvalidCredentialsError("current password is invalid")
        assert result is not None
        return result

    def audit_password_change_failure(self, *, user_id: UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            self._emit_session_audit(
                session,
                user_id=user.id,
                action="auth.password.change.failed",
                target_kind="user",
                target_id=user.id,
                payload={"reason": reason},
            )

    def reauthenticate(
        self,
        *,
        user_id: UUID,
        current_password: str,
    ) -> RecentReauthenticationResult:
        validate_password(current_password, new=False)
        result: RecentReauthenticationResult | None = None
        invalid_password = False
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                raise InvalidSessionError("session user no longer exists")
            self._assert_active(user)
            if not self.credential_hasher.verify_password(
                current_password,
                user.password_hash,
            ):
                invalid_password = True
                self._emit_session_audit(
                    session,
                    user_id=user.id,
                    action="auth.reauthentication.failed",
                    target_kind="user",
                    target_id=user.id,
                    payload={"reason": "invalid_credentials"},
                )
            else:
                result = self._reauthentication_codec().issue(
                    user_id=user.id,
                    session_generation=user.session_generation,
                )
                self._emit_session_audit(
                    session,
                    user_id=user.id,
                    action="auth.reauthentication.succeeded",
                    target_kind="user",
                    target_id=user.id,
                    payload={
                        "reauthenticated_until": (
                            result.reauthenticated_until.isoformat()
                        )
                    },
                )
        if invalid_password:
            raise InvalidCredentialsError("current password is invalid")
        assert result is not None
        return result

    def audit_reauthentication_failure(self, *, user_id: UUID, reason: str) -> None:
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            self._emit_session_audit(
                session,
                user_id=user.id,
                action="auth.reauthentication.failed",
                target_kind="user",
                target_id=user.id,
                payload={"reason": reason},
            )

    def verify_recent_reauthentication(
        self,
        *,
        proof_token: str,
        user_id: UUID,
    ) -> VerifiedRecentReauthentication:
        verified = self._reauthentication_codec().verify(proof_token)
        if verified.user_id != user_id:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof belongs to another user"
            )
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if (
                user is None
                or user.status != "active"
                or user.session_generation != verified.session_generation
            ):
                raise RecentReauthenticationRequiredError(
                    "recent reauthentication proof has been revoked"
                )
        return verified

    def _emit_session_audit(
        self,
        session: Session,
        *,
        user_id: UUID,
        action: str,
        target_kind: str,
        target_id: UUID,
        payload: dict,
    ) -> None:
        """Emit a session lifecycle audit event at system scope.

        Session lifecycle is not workspace-scoped activity: scoping it to the
        user's "oldest" workspace membership (AUTH-05) leaked co-members'
        sign-in/sign-out/password timelines to every viewer of that shared
        workspace's kind=system activity, and silently dropped the event
        entirely for users with no membership. PasswordRecoveryService
        already emits its auth.* rows the same way, with workspace_id=None.
        """

        if self.audit_service is None:
            return
        self.audit_service.emit(
            session=session,
            workspace_id=None,
            actor_kind="human",
            actor_id=str(user_id),
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            payload=payload,
            # AUTH-10: session lifecycle rows are auth.* like
            # PasswordRecoveryService's, which already mark themselves
            # sensitive rather than falling back to the "standard" default.
            retention_class="sensitive",
        )

    def _issue_session(
        self,
        user: User,
        *,
        remember_me: bool = False,
        authenticated_at: datetime | None = None,
    ) -> SessionResult:
        issued_at = self.token_codec.now().astimezone(UTC).replace(microsecond=0)
        token, expires_at = self.token_codec.issue(
            user,
            remember_me=remember_me,
            authenticated_at=authenticated_at,
            issued_at=issued_at,
        )
        return SessionResult(
            user=authenticated_user(user),
            session_token=token,
            issued_at=issued_at,
            expires_at=expires_at,
            remember_me=remember_me,
        )

    def _reauthentication_codec(self) -> RecentReauthenticationCodec:
        if self.recent_reauthentication_codec is not None:
            return self.recent_reauthentication_codec
        return RecentReauthenticationCodec(
            secret=self.token_codec.secret,
            now=self.token_codec.now,
        )

    def _assert_active(self, user: User) -> None:
        if user.status != "active":
            raise SuspendedUserError("user is not active")


def consume_auth_slot(client_ip: str, email: str) -> None:
    """Rate-limit ``/auth/login`` and ``/auth/signup`` attempts.

    Same sliding-window counter as ``oauth_service.consume_token_slot``:
    two-axis buckets (per-IP and per-email) over a 60-second tail keyed on
    ``time.monotonic``. The per-email bucket throttles online brute force
    against a single account (email is case/whitespace-folded so variants
    share one bucket); the per-IP bucket bounds credential spray across many
    accounts. Buckets are in-process module globals — the single-process
    deployment invariant (ADR A12) makes them authoritative.

    Raises:
        AuthRateLimitError: when either bucket is full. The endpoint layer
            maps this to HTTP 429.
    """
    now = time.monotonic()
    # FastAPI runs synchronous auth endpoints in a thread pool. Keep both
    # limit axes and the opportunistic sweep in one critical section so a
    # concurrent request cannot mutate a bucket while another iterates or
    # deletes it, and so one attempt consumes both axes atomically.
    with _AUTH_BUCKET_LOCK:
        ip_bucket = [
            stamp for stamp in _AUTH_IP_BUCKET.get(client_ip, []) if now - stamp < 60
        ]
        if len(ip_bucket) >= AUTH_RATE_LIMIT_PER_IP:
            raise AuthRateLimitError("auth rate limit exceeded for client IP")
        ip_bucket.append(now)
        _AUTH_IP_BUCKET[client_ip] = ip_bucket

        email_key = email.strip().lower()
        if email_key:
            email_bucket = [
                stamp
                for stamp in _AUTH_EMAIL_BUCKET.get(email_key, [])
                if now - stamp < 60
            ]
            if len(email_bucket) >= AUTH_RATE_LIMIT_PER_EMAIL:
                raise AuthRateLimitError("auth rate limit exceeded for email")
            email_bucket.append(now)
            _AUTH_EMAIL_BUCKET[email_key] = email_bucket

        _sweep_auth_buckets(now)


def _sweep_auth_buckets(now: float) -> None:
    """Drop keys whose 60s window has fully drained.

    The per-key timestamp lists are already trimmed on use above; without
    this, the key COUNT still grows without bound (one entry per distinct IP
    and per distinct submitted email string) for the process lifetime — an
    unauthenticated caller sending a different email/IP on every /auth/login
    or /auth/signup attempt accumulates dict entries forever (AUTH-07).
    Opportunistic: runs at most once per 60s, piggybacking on real traffic
    rather than a background thread.
    """

    global _LAST_AUTH_BUCKET_SWEEP
    if now - _LAST_AUTH_BUCKET_SWEEP < 60:
        return
    _LAST_AUTH_BUCKET_SWEEP = now
    for bucket in (_AUTH_IP_BUCKET, _AUTH_EMAIL_BUCKET):
        stale = [
            key
            for key, stamps in bucket.items()
            if not stamps or now - stamps[-1] >= 60
        ]
        for key in stale:
            del bucket[key]


def authenticated_user(user: User) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        email_verified_at=user.email_verified_at,
    )


def normalize_email(email: str) -> str:
    try:
        return identity_policy.normalize_email(email)
    except identity_policy.EmailPolicyError as exc:
        raise EmailInputPolicyError(str(exc)) from exc


def normalize_display_name(display_name: str | None, email: str) -> str:
    try:
        return identity_policy.normalize_display_name(display_name, email)
    except identity_policy.DisplayNamePolicyError as exc:
        raise DisplayNameInputPolicyError(str(exc)) from exc


def validate_password(password: str, *, new: bool = True) -> None:
    try:
        identity_policy.validate_password(password, new=new)
    except identity_policy.PasswordInputPolicyError as exc:
        raise PasswordPolicyError(str(exc)) from exc


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode()
