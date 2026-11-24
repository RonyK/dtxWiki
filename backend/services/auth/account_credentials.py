"""Retire an account's live credentials and unfinished confirmations in one place."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken
from backend.models.auth_identity import AuthLoginTransaction
from backend.models.auth_verification import EmailReauthenticationChallenge
from backend.models.llm_connection import UserLLMConnection
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.mail_delivery import terminalize_token_outboxes
from backend.services.oauth_service import revoke_all_user_oauth_credentials


def _revoke_tokens(session: Session, user: User, now: datetime, purposes=None):
    statement = select(PasswordResetToken.id).where(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.revoked_at.is_(None),
        PasswordResetToken.used_at.is_(None),
    )
    if purposes is not None:
        statement = statement.where(PasswordResetToken.purpose.in_(purposes))
    token_ids = list(session.scalars(statement))
    if token_ids:
        session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id.in_(token_ids))
            .values(revoked_at=now)
        )
    return token_ids


def supersede_pending_email(session: Session, user: User, now: datetime) -> None:
    """Drop a native-email enrollment whose address another path replaced."""
    user.pending_email = None
    token_ids = _revoke_tokens(session, user, now, purposes=("email_verification",))
    terminalize_token_outboxes(session=session, token_ids=token_ids, terminal_at=now)


def revoke_user_credentials(session: Session, user: User, now: datetime) -> None:
    """Invalidate every session, pending confirmation and credential of a user."""
    user.session_generation += 1
    user.pending_email = None
    session.execute(
        update(AuthLoginTransaction)
        .where(
            AuthLoginTransaction.user_id == user.id,
            AuthLoginTransaction.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    challenge_ids = list(
        session.scalars(
            select(EmailReauthenticationChallenge.id).where(
                EmailReauthenticationChallenge.user_id == user.id,
                EmailReauthenticationChallenge.revoked_at.is_(None),
                EmailReauthenticationChallenge.consumed_at.is_(None),
            )
        )
    )
    if challenge_ids:
        session.execute(
            update(EmailReauthenticationChallenge)
            .where(EmailReauthenticationChallenge.id.in_(challenge_ids))
            .values(revoked_at=now)
        )
    terminalize_token_outboxes(
        session=session,
        token_ids=_revoke_tokens(session, user, now),
        reauthentication_challenge_ids=challenge_ids,
        terminal_at=now,
    )
    session.execute(
        update(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    revoke_all_user_oauth_credentials(session, user_id=user.id, revoked_at=now)


def retire_user_llm_connections(
    session: Session,
    *,
    user_id: UUID,
    now: datetime,
    actor_kind: str,
    actor_id: str,
) -> None:
    """Retire personal LLM credentials while the caller locks the account."""
    connection = UserLLMConnection
    retired = session.execute(
        update(connection)
        .where(
            connection.user_id == user_id,
            connection.status != "deleted",
            or_(
                connection.status != "disabled",
                connection.active_credential.is_not(None),
                connection.pending_credential.is_not(None),
            ),
        )
        .values(
            status="disabled",
            revision=connection.revision + 1,
            secret_revision=connection.secret_revision + 1,
            active_credential=None,
            pending_credential=None,
            verification_status="unverified",
            verified_at=None,
            updated_at=now,
        )
        .returning(connection.id, connection.revision)
    ).all()
    audit = AuditService()
    for connection_id, revision in retired:
        audit.emit(
            session=session,
            workspace_id=None,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="llm.connection.disabled",
            target_kind="user_llm_connection",
            target_id=connection_id,
            payload={"status": "disabled", "revision": revision},
            retention_class="sensitive",
        )
