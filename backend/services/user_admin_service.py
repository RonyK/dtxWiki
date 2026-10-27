from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.oauth_service import revoke_live_token_grant_families
from backend.services.personal_workspace_service import create_personal_workspace
from backend.services.session_service import (
    AuthenticatedUser,
    DuplicateEmailError,
    InvalidCredentialsError,
    authenticated_user,
    normalize_display_name,
    normalize_email,
    validate_password,
)
from backend.utils.database_errors import is_single_column_unique_violation


@dataclass(frozen=True)
class UserAdminService:
    session_factory: object
    credential_hasher: CredentialHasher
    audit_service: AuditService = field(default_factory=AuditService)

    def add_user(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> AuthenticatedUser:
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
            # Account creation owns only its newly created workspace.
            # Membership in an existing workspace is an explicit, separate
            # administration action and never comes from a process-wide slug.
            create_personal_workspace(session, user=user)
            return authenticated_user(user)

    def reset_password(
        self,
        *,
        email: str,
        password: str,
        actor_id: str = "cli:unknown",
    ) -> AuthenticatedUser:
        normalized_email = normalize_email(email)
        validate_password(password)
        with self.session_factory.begin() as session:
            user = session.scalar(select(User).where(User.email == normalized_email))
            if user is None:
                raise InvalidCredentialsError("user not found")
            user.password_hash = self.credential_hasher.hash_password(password)
            user.session_generation += 1
            session.flush()
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="system",
                actor_id=actor_id,
                action="auth.password.admin_reset",
                target_kind="user",
                target_id=user.id,
                payload={
                    "requested_via": "cli",
                    "execution_path": "dtxwiki users reset-password",
                    "sessions_revoked": True,
                    "api_tokens_revoked": False,
                },
                event_summary="A server operator reset a user password",
                retention_class="sensitive",
            )
            return authenticated_user(user)

    def deactivate_user(self, *, email: str) -> bool:
        """Deactivate a user and immediately revoke all OAuth grant families."""

        normalized_email = normalize_email(email)
        with self.session_factory.begin() as session:
            user = session.scalar(select(User).where(User.email == normalized_email))
            if user is None:
                raise InvalidCredentialsError("user not found")
            changed = user.status == "active"
            user.status = "disabled"
            revoke_live_token_grant_families(
                session,
                user_id=user.id,
            )
            session.flush()
            return changed
