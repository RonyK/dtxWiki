"""Control-plane credential snapshots for workspace MCP management lists."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import MCPClient, WorkspaceMembership

PAT_BINDING_PREFIX = "api_token:"
OAUTH_BINDING_PREFIX = "oauth_grant:"


@dataclass(frozen=True)
class CredentialHealth:
    state: str
    expires_at: datetime | None
    oauth_client_id: str | None = None


def binding_key_for_client(client: MCPClient) -> str | None:
    """The exact credential a workspace-plane MCP client row names, if any."""

    if client.runtime_binding_key is not None:
        return client.runtime_binding_key
    metadata = client.token_metadata if isinstance(client.token_metadata, dict) else {}
    kind = metadata.get("token_kind")
    if metadata.get("token_id") and kind in {"api_token", "pat", "oauth_jwt"}:
        prefix = OAUTH_BINDING_PREFIX if kind == "oauth_jwt" else PAT_BINDING_PREFIX
        return f"{prefix}{metadata['token_id']}"
    return None


def _binding_ids(bindings: Collection[str], prefix: str) -> set[UUID]:
    ids = set()
    for binding in bindings:
        if binding.startswith(prefix):
            try:
                ids.add(UUID(binding[len(prefix) :]))
            except ValueError:
                continue
    return ids


def load_credential_health(
    session: Session,
    *,
    workspace_id: UUID,
    now: datetime,
    bindings: Collection[str],
) -> dict[tuple[UUID, str], CredentialHealth]:
    """Judge only the credentials named by ``bindings``.

    Never join workspace-plane rows into control-plane queries, and never scan
    a workspace's whole membership: the caller already read, on the workspace
    plane, which credentials its rows name, so the cost follows the number of
    connections rather than members x tokens x rotations.

    Rotation successors share grant_id. Separate authorizations never share
    liveness, even when their user, app and workspace are identical.
    """

    token_ids = _binding_ids(bindings, PAT_BINDING_PREFIX)
    grant_ids = _binding_ids(bindings, OAUTH_BINDING_PREFIX)
    if not token_ids and not grant_ids:
        return {}
    pats = (
        session.execute(
            select(
                ApiToken.id,
                ApiToken.user_id,
                ApiToken.expires_at,
                ApiToken.revoked_at,
                ApiTokenWorkspace.token_id.label("grant_token_id"),
                ApiTokenWorkspace.removed_at.label("grant_removed_at"),
            )
            .outerjoin(
                ApiTokenWorkspace,
                and_(
                    ApiTokenWorkspace.token_id == ApiToken.id,
                    ApiTokenWorkspace.workspace_id == workspace_id,
                ),
            )
            .where(ApiToken.id.in_(token_ids))
        ).all()
        if token_ids
        else []
    )
    refresh_tokens = (
        session.execute(
            select(
                OAuthRefreshToken.user_id,
                OAuthRefreshToken.client_id,
                OAuthRefreshToken.grant_id,
                OAuthRefreshToken.rotated_to_id,
                OAuthRefreshToken.revoked_at,
                OAuthRefreshToken.expires_at,
            ).where(OAuthRefreshToken.grant_id.in_(grant_ids))
        ).all()
        if grant_ids
        else []
    )
    user_ids = {row.user_id for row in pats} | {row.user_id for row in refresh_tokens}
    client_ids = {row.client_id for row in refresh_tokens}
    members = set(
        session.scalars(
            select(WorkspaceMembership.user_id)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id.in_(user_ids),
                User.status == "active",
            )
        )
    )

    result = {}
    for token in pats:
        state = "active"
        if (
            token.revoked_at
            or token.grant_token_id is None
            or token.grant_removed_at
            or token.user_id not in members
        ):
            state = "revoked"
        elif token.expires_at is not None and token.expires_at <= now:
            state = "expired"
        result[(token.user_id, f"{PAT_BINDING_PREFIX}{token.id}")] = CredentialHealth(
            state, token.expires_at
        )

    consents = {
        (c.user_id, c.client_id): c
        for c in session.execute(
            select(
                OAuthConsent.user_id, OAuthConsent.client_id, OAuthConsent.removed_at
            ).where(
                OAuthConsent.workspace_id == workspace_id,
                OAuthConsent.user_id.in_(user_ids),
                OAuthConsent.client_id.in_(client_ids),
            )
        )
    }
    client_revoked_at = dict(
        session.execute(
            select(OAuthClient.id, OAuthClient.revoked_at).where(
                OAuthClient.id.in_(client_ids)
            )
        ).all()
    )
    families: dict[tuple[UUID, str, UUID], list] = {}
    for token in refresh_tokens:
        family = (token.user_id, token.client_id, token.grant_id)
        families.setdefault(family, []).append(token)
    for (user_id, client_id, grant_id), tokens in families.items():
        consent = consents.get((user_id, client_id))
        leaves = [t for t in tokens if t.rotated_to_id is None]
        live = [t for t in leaves if t.revoked_at is None and t.expires_at > now]
        expiry = max((t.expires_at for t in live), default=None)
        if (
            consent is None
            or consent.removed_at
            or client_id not in client_revoked_at
            or client_revoked_at[client_id]
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
        result[(user_id, f"{OAUTH_BINDING_PREFIX}{grant_id}")] = CredentialHealth(
            state, expiry, client_id
        )
    return result
