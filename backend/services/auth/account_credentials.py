"""Retire an account's live credentials and unfinished confirmations in one place."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken
from backend.models.auth_identity import AuthLoginTransaction
from backend.models.auth_verification import EmailReauthenticationChallenge
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
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
