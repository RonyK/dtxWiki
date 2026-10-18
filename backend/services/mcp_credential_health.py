"""Control-plane credential snapshots for workspace MCP management lists."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership


@dataclass(frozen=True)
class CredentialHealth:
    state: str
    expires_at: datetime | None
    oauth_client_id: str | None = None


def load_credential_health(
    session: Session, *, workspace_id: UUID, now: datetime
) -> dict[tuple[UUID, str], CredentialHealth]:
    """Never join workspace-plane rows into control-plane queries.

    Rotation successors share grant_id. Separate authorizations never share
    liveness, even when their user, app and workspace are identical.
    """
    members = set(
        session.scalars(
            select(WorkspaceMembership.user_id)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                User.status == "active",
            )
        )
    )
    result = {}
    for token, grant in session.execute(
        select(ApiToken, ApiTokenWorkspace)
        .outerjoin(
            ApiTokenWorkspace,
            and_(
                ApiTokenWorkspace.token_id == ApiToken.id,
                ApiTokenWorkspace.workspace_id == workspace_id,
            ),
        )
        .where(
            or_(
                ApiTokenWorkspace.workspace_id == workspace_id,
                ApiToken.user_id.in_(members),
            )
        )
    ):
        state = "active"
        if (
            token.revoked_at
            or grant is None
            or grant.removed_at
            or token.user_id not in members
        ):
            state = "revoked"
        elif token.expires_at is not None and token.expires_at <= now:
            state = "expired"
        result[(token.user_id, f"api_token:{token.id}")] = CredentialHealth(
            state, token.expires_at
        )

    consents = {
        (c.user_id, c.client_id): c
        for c in session.scalars(
            select(OAuthConsent).where(OAuthConsent.workspace_id == workspace_id)
        )
    }
    candidate_users = members | {key[0] for key in consents}
    tokens = list(
        session.scalars(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.user_id.in_(candidate_users)
            )
        )
    )
    clients = {
        c.id: c
        for c in session.scalars(
            select(OAuthClient).where(
                OAuthClient.id.in_({token.client_id for token in tokens})
            )
        )
    }
    families: dict[tuple[UUID, str, UUID], list[OAuthRefreshToken]] = {}
    for token in tokens:
        if token.grant_id is not None:
            families.setdefault(
                (token.user_id, token.client_id, token.grant_id), []
            ).append(token)
    for (user_id, client_id, grant_id), tokens in families.items():
        consent = consents.get((user_id, client_id))
        leaves = [t for t in tokens if t.rotated_to_id is None]
        live = [t for t in leaves if t.revoked_at is None and t.expires_at > now]
        expiry = max((t.expires_at for t in live), default=None)
        if (
            consent is None
            or consent.removed_at
            or client_id not in clients
            or clients[client_id].revoked_at
            or user_id not in members
        ):
            state = "revoked"
        elif live:
            state = "active"
        elif leaves and all(t.revoked_at is not None for t in leaves):
            state = "revoked"
        else:
            state = "expired"
        if expiry is None:
            expiry = max((t.expires_at for t in leaves), default=None)
        result[(user_id, f"oauth_grant:{grant_id}")] = CredentialHealth(
            state, expiry, client_id
        )
    return result
