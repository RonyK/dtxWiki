"""Privileged, compare-and-swap recovery of an existing external identity."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.models.api_token import ApiToken
from backend.models.auth_identity import AuthLoginTransaction, UserIdentity
from backend.models.auth_verification import EmailReauthenticationChallenge
from backend.models.base import utc_now
from backend.models.password_reset import PasswordResetToken
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.db_write_intent import acquire_write_intent
from backend.services.mail_delivery import terminalize_token_outboxes
from backend.services.oauth_service import revoke_all_user_oauth_credentials


class IdentityRecoveryError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409):
        self.error_code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass
class IdentityRecoveryService:
    session_factory: object
    providers: ProviderRegistry

    def rebind(
        self,
        *,
        user_id: UUID,
        identity_id: UUID,
        expected_issuer: str,
        expected_subject: str,
        provider_slug: str,
        issuer: str,
        subject: str,
        reason: str,
        actor_id: str,
        actor_user_id: UUID | None = None,
        actor_session_generation: int | None = None,
    ) -> dict:
        """Caller verifies ownership out of band; a local CLI is privileged.

        HTTP callers must supply their live server-admin identity and generation.
        This does not prove control of the new IdP account or retire the old one.
        """
        provider = self.providers.providers.get(provider_slug)
        if provider is None:
            raise IdentityRecoveryError("sso_provider_unavailable", 422)
        accepted = provider.context.config.get("accepted_issuers") or (
            provider.context.config["issuer"],
        )
        if (
            issuer not in accepted
            or not 0 < len(subject.encode()) <= 512
            or any(not 0x20 <= ord(c) <= 0x7E for c in subject)
            or not 0 < len(reason.strip()) <= 1000
        ):
            raise IdentityRecoveryError("invalid_identity_recovery", 422)
        now = utc_now()
        try:
            with self.session_factory.begin() as session:
                acquire_write_intent(session)
                locked = {}
                for identifier in sorted({user_id, actor_user_id} - {None}, key=str):
                    locked[identifier] = session.scalar(
                        select(User).where(User.id == identifier).with_for_update()
                    )
                if actor_user_id is not None:
                    actor = locked[actor_user_id]
                    if (
                        actor is None
                        or actor.status != "active"
                        or actor.session_generation != actor_session_generation
                    ):
                        raise IdentityRecoveryError(
                            "recent_reauthentication_required", 401
                        )
                    if (
                        session.scalar(
                            select(ServerAdmin)
                            .where(ServerAdmin.user_id == actor_user_id)
                            .with_for_update()
                        )
                        is None
                    ):
                        raise IdentityRecoveryError("server_admin_required", 403)
                user = locked[user_id]
                if user is None or user.status != "active":
                    raise IdentityRecoveryError("user_not_found", 404)
                identity = session.scalar(
                    select(UserIdentity)
                    .where(
                        UserIdentity.id == identity_id,
                        UserIdentity.user_id == user_id,
                    )
                    .with_for_update()
                )
                if identity is None:
                    raise IdentityRecoveryError("identity_not_found", 404)
                if (identity.issuer, identity.subject) != (
                    expected_issuer,
                    expected_subject,
                ):
                    raise IdentityRecoveryError("identity_changed")
                if (identity.provider_slug, identity.issuer, identity.subject) == (
                    provider_slug,
                    issuer,
                    subject,
                ):
                    raise IdentityRecoveryError("identity_unchanged")
                collision = session.scalar(
                    select(UserIdentity.id).where(
                        UserIdentity.id != identity_id,
                        (
                            (UserIdentity.issuer == issuer)
                            & (UserIdentity.subject == subject)
                        )
                        | (
                            (UserIdentity.user_id == user_id)
                            & (UserIdentity.provider_slug == provider_slug)
                        ),
                    )
                )
                if collision is not None:
                    raise IdentityRecoveryError("identity_already_linked")
                identity.provider_slug, identity.issuer, identity.subject = (
                    provider_slug,
                    issuer,
                    subject,
                )
                identity.linked_via = "admin"
                identity.last_email = identity.email_at_link = (
                    identity.directory_tenant
                ) = None
                identity.last_email_verified = False
                identity.last_login_at = None
                user.session_generation += 1
                user.pending_email = None
                session.execute(
                    update(AuthLoginTransaction)
                    .where(
                        AuthLoginTransaction.user_id == user_id,
                        AuthLoginTransaction.consumed_at.is_(None),
                    )
                    .values(consumed_at=now)
                )
                challenge_ids = list(
                    session.scalars(
                        select(EmailReauthenticationChallenge.id).where(
                            EmailReauthenticationChallenge.user_id == user_id,
                            EmailReauthenticationChallenge.revoked_at.is_(None),
                            EmailReauthenticationChallenge.consumed_at.is_(None),
                        )
                    )
                )
                if challenge_ids:
                    session.execute(
                        update(EmailReauthenticationChallenge)
                        .where(
                            EmailReauthenticationChallenge.id.in_(challenge_ids),
                        )
                        .values(revoked_at=now)
                    )
                token_ids = list(
                    session.scalars(
                        select(PasswordResetToken.id).where(
                            PasswordResetToken.user_id == user_id,
                            PasswordResetToken.revoked_at.is_(None),
                            PasswordResetToken.used_at.is_(None),
                        )
                    )
                )
                if token_ids:
                    session.execute(
                        update(PasswordResetToken)
                        .where(PasswordResetToken.id.in_(token_ids))
                        .values(revoked_at=now)
                    )
                terminalize_token_outboxes(
                    session=session,
                    token_ids=token_ids,
                    reauthentication_challenge_ids=challenge_ids,
                    terminal_at=now,
                )
                session.execute(
                    update(ApiToken)
                    .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
                revoke_all_user_oauth_credentials(
                    session, user_id=user_id, revoked_at=now
                )
                AuditService().emit(
                    session=session,
                    workspace_id=None,
                    actor_kind="human" if actor_user_id else "system",
                    actor_id=actor_id,
                    action="auth.identity.rebound",
                    target_kind="user",
                    target_id=user_id,
                    payload={
                        "identity_id": str(identity_id),
                        "provider_slug": provider_slug,
                        "previous_issuer": expected_issuer,
                        "previous_subject": expected_subject,
                        "issuer": issuer,
                        "subject": subject,
                        "reason": reason.strip(),
                        "sessions_and_credentials_revoked": True,
                    },
                    retention_class="sensitive",
                )
                session.flush()
                return {
                    "user_id": user_id,
                    "identity_id": identity_id,
                    "provider_slug": provider_slug,
                    "issuer": issuer,
                    "subject": subject,
                }
        except IntegrityError as exc:
            raise IdentityRecoveryError("identity_already_linked") from exc
