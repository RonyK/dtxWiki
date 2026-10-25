import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.settings import (
    AUTH_REAUTHENTICATION_MAX_AGE_DEFAULT_SECONDS,
    SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS,
    SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS,
    SESSION_REMEMBERED_TTL_DEFAULT_SECONDS,
    SESSION_TTL_DEFAULT_SECONDS,
    Settings,
)
from backend.domain import identity_policy
from backend.domain.auth_policy import provider_slug_valid
from backend.domain.credential_input import (
    SESSION_TOKEN_MAX_BYTES,
    ascii_token_is_valid,
    require_ascii_token,
)
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.auth.password_method import (
    DUMMY_PASSWORD_HASH as DUMMY_PASSWORD_HASH,
)
from backend.services.auth.password_method import verify_password
from backend.services.auth.session_issuer import (
    AuthenticatedUser as AuthenticatedUser,
)
from backend.services.auth.session_issuer import (
    SessionResult as SessionResult,
)
from backend.services.auth.session_issuer import (
    authenticated_user as authenticated_user,
)
from backend.services.auth.session_issuer import issue_native_session
from backend.services.credential_hashing import CredentialHasher
from backend.services.personal_workspace_service import ensure_personal_workspace
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
SCOPED_REAUTHENTICATION_TOKEN_VERSION = 2
RECENT_REAUTHENTICATION_TTL_SECONDS = AUTH_REAUTHENTICATION_MAX_AGE_DEFAULT_SECONDS
MIN_PASSWORD_LENGTH = identity_policy.NEW_PASSWORD_MIN_LENGTH
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
class VerifiedSession:
    user_id: UUID
    session_generation: int
    expires_at: datetime
    remember_me: bool
    authenticated_at: datetime | None
    amr: tuple[str, ...] | None = None
    idp: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class RecentReauthenticationResult:
    proof_token: str
    reauthenticated_until: datetime


@dataclass(frozen=True)
class VerifiedRecentReauthentication:
    user_id: UUID
    session_generation: int
    scope: str | None = None
    session_digest: str | None = None
    expires_at: datetime | None = None


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
        scope: str | None = None,
        session_digest: str | None = None,
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
        if scope is not None:
            if scope not in {"account", "admin", "primary"} or (
                not isinstance(session_digest, str)
                or len(session_digest) != 64
                or any(char not in "0123456789abcdef" for char in session_digest)
            ):
                raise ValueError("scoped reauthentication requires a session binding")
            payload["v"] = SCOPED_REAUTHENTICATION_TOKEN_VERSION
            payload["scope"] = scope
            payload["sid"] = session_digest
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _base64url_encode(
            self._sign(encoded_payload.encode(), version=payload["v"])
        )
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
        signed_version = RECENT_REAUTHENTICATION_TOKEN_VERSION
        expected_signature = _base64url_encode(self._sign(encoded_payload.encode()))
        if not hmac.compare_digest(encoded_signature, expected_signature):
            signed_version = SCOPED_REAUTHENTICATION_TOKEN_VERSION
            expected_signature = _base64url_encode(
                self._sign(encoded_payload.encode(), version=signed_version)
            )
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
        if not isinstance(payload, dict) or payload.get("v") != signed_version:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof version is unsupported"
            )
        scope, session_digest = payload.get("scope"), payload.get("sid")
        if signed_version == SCOPED_REAUTHENTICATION_TOKEN_VERSION:
            if scope not in {"account", "admin", "primary"} or (
                not isinstance(session_digest, str)
                or len(session_digest) != 64
                or any(char not in "0123456789abcdef" for char in session_digest)
            ):
                raise RecentReauthenticationRequiredError("scoped proof is invalid")
        elif scope is not None or session_digest is not None:
            raise RecentReauthenticationRequiredError("legacy proof has scoped claims")
        if payload.get("kind") != RECENT_REAUTHENTICATION_TOKEN_KIND:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof kind is unsupported"
            )
        exp, issued_at = payload.get("exp"), payload.get("iat")
        now = self.now().timestamp()
        if (
            type(exp) is not int
            or type(issued_at) is not int
            or not 0 <= issued_at <= now
            or issued_at >= exp
        ):
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof lifetime is invalid"
            )
        # A shorter operator policy applies immediately to already-issued
        # proofs. Raising the policy never extends their signed expiry.
        effective_expiry = min(exp, issued_at + self.ttl_seconds)
        if now >= effective_expiry:
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
                scope=payload.get("scope"),
                session_digest=payload.get("sid"),
                expires_at=datetime.fromtimestamp(effective_expiry, UTC),
            )
        except (KeyError, ValueError, OverflowError, OSError) as exc:
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof subject is invalid"
            ) from exc

    def _sign(self, encoded_payload: bytes, *, version: int = 1) -> bytes:
        signing_key = (
            f"dtxwiki.recent_reauthentication.v{version}\0{self.secret}".encode()
        )
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
        amr: tuple[str, ...] | None = None,
        idp: str | None = None,
        session_id: str | None = None,
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
            "sid": session_id or secrets.token_urlsafe(32),
        }
        if not self._valid_session_id(payload["sid"]):
            raise ValueError("session identity is invalid")
        if amr is not None:
            if not _valid_amr(amr):
                raise ValueError("session authentication methods are invalid")
            payload["amr"] = list(amr)
        if idp is not None:
            if not _valid_idp(idp):
                raise ValueError("session identity provider is invalid")
            payload["idp"] = idp
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
        if not isinstance(payload, dict) or payload.get("v") != SESSION_TOKEN_VERSION:
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
            isinstance(authenticated_at_claim, int)
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
                remember_me = remembered_claim is True
        try:
            amr_claim = payload.get("amr")
            idp_claim = payload.get("idp")
            if amr_claim is not None and (
                not isinstance(amr_claim, list) or not _valid_amr(amr_claim)
            ):
                raise InvalidSessionError("session authentication methods are invalid")
            if idp_claim is not None and not _valid_idp(idp_claim):
                raise InvalidSessionError("session identity provider is invalid")
            session_id = payload.get("sid")
            if session_id is None:
                # Published native tokens predate SID. Only a cryptographically
                # verified token can acquire this deterministic renewal identity.
                session_id = hmac.digest(
                    f"dtxwiki.session.identity.v1\0{self.secret}".encode(),
                    token.encode(),
                    "sha256",
                ).hex()
            elif not self._valid_session_id(session_id):
                raise InvalidSessionError("session identity is invalid")
            return VerifiedSession(
                user_id=UUID(str(payload["sub"])),
                session_generation=session_generation,
                expires_at=expires_at,
                remember_me=remember_me,
                authenticated_at=authenticated_at,
                amr=tuple(amr_claim) if amr_claim is not None else None,
                idp=idp_claim,
                session_id=session_id,
            )
        except (KeyError, ValueError) as exc:
            raise InvalidSessionError("session token subject is invalid") from exc

    @staticmethod
    def _valid_session_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 32 <= len(value) <= 128
            and ascii_token_is_valid(value, 128)
        )

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
    # Personal-only workspace grant performed after account creation.
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
    reauthentication_max_age_seconds: int = RECENT_REAUTHENTICATION_TTL_SECONDS

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
        # Reject invalid public input before opening a database transaction or
        # spending password-hashing work. Provisioning rechecks its own inputs
        # because trusted external-login callers use that seam directly too.
        normalized_email = normalize_email(email)
        validate_password(password)
        normalized_name = normalize_display_name(display_name, normalized_email)
        with self.session_factory.begin() as session:
            try:
                user = self.provision_user(
                    session,
                    email=normalized_email,
                    password=password,
                    display_name=normalized_name,
                )
            except IntegrityError as exc:
                if not is_single_column_unique_violation(
                    exc, table="users", column="email"
                ):
                    raise
                raise DuplicateEmailError("email already exists") from exc
            return self.issue_session(user, remember_me=remember_me, amr=("pwd",))

    def provision_user(
        self,
        session: Session,
        *,
        email: str | None,
        display_name: str | None = None,
        password: str | None = None,
        email_verified_at: datetime | None = None,
    ) -> User:
        """Provision a native account inside the caller's transaction.

        The caller decides whether registration is authorized. An external
        provider's JIT policy is independent of public password signup policy.
        Unique conflicts propagate so the broker can roll back and retry its
        complete identity binding transaction.
        """

        normalized_email = normalize_email(email) if email is not None else None
        if password is not None:
            if normalized_email is None:
                raise EmailInputPolicyError(
                    "password credentials require a native email"
                )
            validate_password(password)
        name = normalize_display_name(display_name, normalized_email)
        user = User(
            email=normalized_email,
            password_hash=(
                self.credential_hasher.hash_password(password)
                if password is not None
                else None
            ),
            display_name=name,
            email_verified_at=email_verified_at,
            status="active",
        )
        session.add(user)
        session.flush()
        # Every login method starts in an ordinary private Personal workspace
        # and receives no implicit global or installation workspace membership.
        ensure_personal_workspace(session, user=user)
        self._emit_session_audit(
            session,
            user_id=user.id,
            action="auth.user.signed_up",
            target_kind="user",
            target_id=user.id,
            payload={"display_name": user.display_name},
        )
        return user

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
            if not verify_password(
                self.credential_hasher,
                password,
                user.password_hash if user is not None else None,
            ):
                raise InvalidCredentialsError("email or password is invalid")
            assert user is not None and user.password_hash is not None
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
            return self.issue_session(user, remember_me=remember_me, amr=("pwd",))

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
                    amr=verified.amr,
                    idp=verified.idp,
                    session_id=verified.session_id,
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
            if not verify_password(
                self.credential_hasher,
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
                result = self.issue_session(user, remember_me=remember_me, amr=("pwd",))
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
        session_token: str | None = None,
    ) -> RecentReauthenticationResult:
        validate_password(current_password, new=False)
        result: RecentReauthenticationResult | None = None
        invalid_password = False
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                raise InvalidSessionError("session user no longer exists")
            self._assert_active(user)
            if not verify_password(
                self.credential_hasher,
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
                current = self.token_codec.verify_session(session_token or "")
                if (
                    current.user_id != user.id
                    or current.session_generation != user.session_generation
                ):
                    raise InvalidSessionError("session token has been revoked")
                result = self._reauthentication_codec().issue(
                    user_id=user.id,
                    session_generation=user.session_generation,
                    scope="primary",
                    session_digest=self.session_binding(session_token),
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

    def issue_external_reauthentication(
        self,
        session: Session,
        *,
        user_id: UUID,
        session_generation: int,
        scope: str = "primary",
        session_token: str | None = None,
    ) -> RecentReauthenticationResult:
        """Issue native step-up proof after a broker-verified external login.

        This is a trusted service seam, never an HTTP claim. The broker owns
        identity verification and binds its one-time transaction to the native
        browser session. Recheck its generation under the caller's transaction
        so revocation or suspension also invalidates an in-flight callback.
        """

        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if (
            user is None
            or user.status != "active"
            or user.session_generation != session_generation
        ):
            raise RecentReauthenticationRequiredError(
                "external reauthentication session has been revoked"
            )
        result = self._reauthentication_codec().issue(
            user_id=user.id,
            session_generation=user.session_generation,
            scope=scope,
            session_digest=self.session_binding(session_token or ""),
        )
        self._emit_session_audit(
            session,
            user_id=user.id,
            action="auth.reauthentication.succeeded",
            target_kind="user",
            target_id=user.id,
            payload={"reauthenticated_until": result.reauthenticated_until.isoformat()},
        )
        return result

    def verify_recent_reauthentication(
        self,
        *,
        proof_token: str,
        user_id: UUID,
        scope: str | None = None,
        session_token: str | None = None,
    ) -> VerifiedRecentReauthentication:
        verified = self._reauthentication_codec().verify(proof_token)
        if verified.scope is not None and (
            verified.scope not in {scope, "primary"}
            or session_token is None
            or not hmac.compare_digest(
                verified.session_digest or "",
                self.session_binding(session_token),
            )
        ):
            raise RecentReauthenticationRequiredError(
                "recent reauthentication proof has another purpose or session"
            )
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
        amr: tuple[str, ...] | None = None,
        idp: str | None = None,
        session_id: str | None = None,
    ) -> SessionResult:
        return self.issue_session(
            user,
            remember_me=remember_me,
            authenticated_at=authenticated_at,
            amr=amr,
            idp=idp,
            session_id=session_id,
        )

    def issue_session(
        self,
        user: User,
        *,
        remember_me: bool = False,
        authenticated_at: datetime | None = None,
        amr: tuple[str, ...] | None = None,
        idp: str | None = None,
        session_id: str | None = None,
    ) -> SessionResult:
        """Issue a native session inside the caller's account transaction.

        The caller owns primary authentication and the session-created audit.
        This method shares cookie credentials, TTL and revocation semantics
        between password login and external providers.
        """

        self._assert_active(user)
        return issue_native_session(
            self.token_codec,
            user,
            remember_me=remember_me,
            authenticated_at=authenticated_at,
            amr=amr,
            idp=idp,
            session_id=session_id,
        )

    def _reauthentication_codec(self) -> RecentReauthenticationCodec:
        if self.recent_reauthentication_codec is not None:
            return replace(
                self.recent_reauthentication_codec,
                ttl_seconds=self.reauthentication_max_age_seconds,
            )
        return RecentReauthenticationCodec(
            secret=self.token_codec.secret,
            now=self.token_codec.now,
            ttl_seconds=self.reauthentication_max_age_seconds,
        )

    def session_binding(self, session_token: str) -> str:
        current = self.token_codec.verify_session(session_token)
        if current.session_id is None:
            raise InvalidSessionError("session identity is missing")
        return hashlib.sha256(current.session_id.encode()).hexdigest()

    def _assert_active(self, user: User) -> None:
        if user.status != "active":
            raise SuspendedUserError("user is not active")


def consume_auth_slot(
    client_ip: str,
    email: str = "",
    *,
    user_id: UUID | None = None,
    namespace: str | None = None,
) -> None:
    """Rate-limit authentication attempts by IP and account identifier.

    Anonymous attempts use normalized native email. Authenticated operations
    use the user UUID, so email-less SSO accounts have independent quotas and
    profile email changes cannot evade them. External starts/callbacks use
    separate namespaces so they cannot exhaust native password quotas.
    The shared per-IP bucket within each namespace bounds
    credential spray across many accounts. Both axes use a 60-second tail on
    ``time.monotonic``. Buckets are in-process module globals — the single-process
    deployment invariant (ADR A12) makes them authoritative.

    Raises:
        AuthRateLimitError: when either bucket is full. The endpoint layer
            maps this to HTTP 429.
    """
    now = time.monotonic()
    ip_key = f"{namespace}:{client_ip}" if namespace else client_ip
    # FastAPI runs synchronous auth endpoints in a thread pool. Keep both
    # limit axes and the opportunistic sweep in one critical section so a
    # concurrent request cannot mutate a bucket while another iterates or
    # deletes it, and so one attempt consumes both axes atomically.
    with _AUTH_BUCKET_LOCK:
        ip_bucket = [
            stamp for stamp in _AUTH_IP_BUCKET.get(ip_key, []) if now - stamp < 60
        ]
        if len(ip_bucket) >= AUTH_RATE_LIMIT_PER_IP:
            raise AuthRateLimitError("auth rate limit exceeded for client IP")
        ip_bucket.append(now)
        _AUTH_IP_BUCKET[ip_key] = ip_bucket

        email_key = f"user:{user_id}" if user_id else email.strip().lower()
        if email_key and namespace:
            email_key = f"{namespace}:{email_key}"
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


def _valid_amr(value: tuple[str, ...] | list[str]) -> bool:
    return bool(
        isinstance(value, (tuple, list))
        and 1 <= len(value) <= 8
        and all(
            isinstance(method, str)
            and 1 <= len(method) <= 64
            and all("!" <= character <= "~" for character in method)
            for method in value
        )
    )


def _valid_idp(value: str) -> bool:
    return provider_slug_valid(value)


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
