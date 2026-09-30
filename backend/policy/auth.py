import logging
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from backend.app.settings import Settings
from backend.app.state import get_session_factory_from_app
from backend.models.oauth import OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rbac import get_workspace_access
from backend.policy.workspace_resolution import repair_default_workspace_to_personal
from backend.services.api_token_service import (
    ApiTokenService,
    InvalidApiTokenError,
)
from backend.services.credential_hashing import CredentialHasher
from backend.services.oauth_service import InvalidOAuthGrantError, OAuthService
from backend.services.session_service import (
    AuthenticatedUser,
    InvalidSessionError,
    SessionService,
    build_session_token_codec,
)
from backend.spi.types import (
    DEFAULT_REST_ROLE as DEFAULT_REST_ROLE,
)
from backend.spi.types import (
    ActorContext as ActorContext,
)

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("dtxwiki.mcp.auth")


def get_rest_actor(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ] = None,
) -> ActorContext:
    settings = request.app.state.settings
    session_factory = _session_factory(request)
    if credentials is not None:
        actor = _authenticate_api_token(
            token=credentials.credentials,
            session_factory=session_factory,
            settings=settings,
            public_base_url=public_base_url_from_request(request),
        )
        _bind_request_actor(request, actor)
        return actor
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token is not None:
        try:
            user = SessionService(
                session_factory=session_factory,
                credential_hasher=CredentialHasher(),
                token_codec=build_session_token_codec(settings),
            ).authenticate_session(session_token)
        except InvalidSessionError as exc:
            raise_unauthorized(str(exc))
        default_access = _default_workspace_access(
            session_factory=session_factory,
            user_id=user.user_id,
        )
        actor = _actor_from_user(
            user=user,
            workspace_id=(
                default_access.workspace_id if default_access is not None else None
            ),
            role=default_access.role if default_access is not None else None,
            token_kind="session",
        )
        _bind_request_actor(request, actor)
        return actor
    raise_unauthorized("Missing session cookie or bearer token")


def authenticate_bearer_token(request: Request, token: str) -> ActorContext:
    actor = _authenticate_api_token(
        token=token,
        session_factory=_session_factory(request),
        settings=request.app.state.settings,
        public_base_url=public_base_url_from_request(request),
    )
    _bind_request_actor(request, actor)
    return actor


def _authenticate_api_token(
    *,
    token: str,
    session_factory,
    settings: Settings,
    public_base_url: str,
) -> ActorContext:
    if not token.startswith("dtxw_"):
        return _authenticate_oauth_token(
            token=token,
            session_factory=session_factory,
            settings=settings,
            public_base_url=public_base_url,
        )
    token_service = ApiTokenService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
    )
    try:
        primary = token_service.authenticate_token(token)
    except InvalidApiTokenError as exc:
        raise_unauthorized(str(exc))
    # D-01v2: the token names a SET of allowed workspaces, not one bound
    # workspace, so no target is resolved (and no role confirmed) here --
    # trust_boundaries.actor_with_target_workspace_scope resolves both once a
    # request names its target workspace.
    return _actor_from_user(
        user=primary.user,
        workspace_id=None,
        role=None,
        token_kind="api_token",
        token_id=primary.token_id,
        scopes=primary.scopes,
        token_workspace_ids=primary.workspace_ids,
    )


def _authenticate_oauth_token(
    *,
    token: str,
    session_factory,
    settings: Settings,
    public_base_url: str,
) -> ActorContext:
    try:
        actor = OAuthService(
            # No database_provider: verify_access_token reads only
            # control-plane tables (OAuthClient, the jti denylist, User), never
            # workspace-plane MCPClient. The consent methods that do need the
            # router raise if they are ever called on this instance.
            session_factory=session_factory,
            settings=settings,
            public_base_url=public_base_url,
        ).verify_access_token(token)
    except InvalidOAuthGrantError as exc:
        raise_unauthorized(str(exc))
    # D-01v2: the credential's allowed set is read live from oauth_consents
    # (removed_at IS NULL) for this (user, client) pair -- not from a JWT
    # workspace claim; P10.3 T3 removed that claim from the token entirely
    # (verify_access_token/_issue_tokens no longer read or write it). No
    # workspace target or role is resolved here; trust_boundaries.
    # actor_with_target_workspace_scope resolves both once a request names
    # its target workspace.
    with session_factory() as session:
        token_workspace_ids = tuple(
            session.scalars(
                select(OAuthConsent.workspace_id)
                .where(
                    OAuthConsent.user_id == actor.user.user_id,
                    OAuthConsent.client_id == actor.client_id,
                    OAuthConsent.removed_at.is_(None),
                )
                .order_by(OAuthConsent.workspace_id)
            ).all()
        )
    return _actor_from_user(
        user=actor.user,
        workspace_id=None,
        role=None,
        token_kind="oauth_jwt",
        token_id=actor.grant_id,
        scopes=actor.scopes,
        client_id=actor.client_id,
        token_workspace_ids=token_workspace_ids,
    )


def _actor_from_user(
    *,
    user: AuthenticatedUser,
    workspace_id: UUID | None,
    role: str | None,
    token_kind: str,
    token_id: UUID | None = None,
    client_id: str | None = None,
    scopes: tuple[str, ...] = (),
    token_workspace_ids: tuple[UUID, ...] = (),
) -> ActorContext:
    workspace_role = role or "none"
    provenance = {
        "session": "browser_session",
        "oauth_jwt": "oauth_client",
        "api_token": "personal_access_token",
    }[token_kind]
    return ActorContext(
        actor_kind="human",
        actor_id=str(user.user_id),
        scopes=scopes,
        workspace_role=workspace_role,
        user_id=user.user_id,
        workspace_id=workspace_id,
        role=role,
        token_kind=token_kind,
        token_id=token_id,
        client_id=client_id,
        token_workspace_ids=token_workspace_ids,
        provenance=provenance,
    )


def _bind_request_actor(request: Request, actor: ActorContext) -> None:
    request.state.actor = actor
    if actor.workspace_id is not None:
        request.state.workspace_id = actor.workspace_id


def _session_factory(request: Request):
    return get_session_factory_from_app(request)


def _default_workspace_access(session_factory, user_id: UUID):
    with session_factory.begin() as session:
        user = session.get(User, user_id)
        if user is None:
            return None

        repair_default_workspace_to_personal(session=session, user=user)
        for workspace_id in (user.default_workspace_id, user.personal_workspace_id):
            if workspace_id is None or session.get(Workspace, workspace_id) is None:
                continue
            access = get_workspace_access(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            if access is None:
                continue
            return access

        membership_workspace_ids = list(
            session.scalars(
                select(WorkspaceMembership.workspace_id)
                .where(WorkspaceMembership.user_id == user_id)
                .order_by(WorkspaceMembership.workspace_id)
            ).all()
        )
        if len(membership_workspace_ids) != 1:
            return None
        return get_workspace_access(
            session,
            user_id=user_id,
            workspace_id=membership_workspace_ids[0],
        )


def raise_unauthorized(message: str) -> None:
    raise HTTPException(
        status_code=401,
        detail={
            "error_code": "unauthorized",
            "message": message,
        },
    )


def public_base_url_from_request(request: Request) -> str:
    configured = request.app.state.settings.public_base_url
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")
