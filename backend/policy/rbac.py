from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db_errors import is_database_schema_mismatch
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.trust_boundaries import (
    actor_kind_for_check,
    check_actor_can_access,
    required_role_to_action,
)
from backend.services.read_errors import RejectedByPolicyError

# Authenticated bearer token kinds that represent a connected MCP client whose
# Settings > Access row can be toggled Disabled. Human sessions have no
# MCPClient row and therefore remain outside this lifecycle check.
_MCP_CONNECTION_TOKEN_KINDS = ("oauth_jwt", "api_token")

ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}
ROLE_ALIASES = {
    "admin": "owner",
    "member": "editor",
    "reviewer": "editor",
}


class WorkspacePlaneUnavailableError(RuntimeError):
    """D-01v3-e: the target workspace's OWN plane could not be reached or
    queried, distinct from every other outcome ``require_workspace_role``
    raises. Those are all policy decisions made WITH a working answer from
    both planes (denied, disabled, insufficient role); this one means no
    answer was available at all, so the caller must fail closed rather than
    optimistically allow on the control-plane check alone (Q1,
    .claude/plan/260909-p10-3-multi-workspace-credential.md section 2.2). Never a
    subclass of ``RejectedByPolicyError``/``ReadServiceError`` -- those
    families already have broad ``except`` clauses at every one of this
    error's call sites, and folding this in would route a transient
    infrastructure failure through response shapes (403/`rejected_by_policy`)
    that promise a considered, stable decision.
    """

    error_code = "workspace_plane_unavailable"

    def __init__(self, workspace_id: UUID | None = None) -> None:
        super().__init__("the target workspace's database is not reachable")
        self.workspace_id = workspace_id


class GrantReplicaMissingError(RuntimeError):
    """D-01v3-b: no live ``WorkspaceCredentialGrant`` replica for this
    (pat|oauth) credential on this workspace's OWN plane.

    Raised only for a ``pat``/``oauth_jwt`` actor (see
    ``_effective_scopes_from_workspace_grant``) -- system and session actors
    carry no workspace-plane credential grant at all, so they never reach this
    check. Two distinct real causes collapse to the same
    code: (a) the credential's allowed set just grew to include this
    workspace and ``credential_replica_service``'s fan-out (T4.5) has not
    landed a replica row yet (or landed it against a database that was
    itself unavailable, see ``WorkspacePlaneUnavailableError``), or (b) the
    replica was tombstoned (``revoked_at`` set) by a shrink's fan-out or the
    T4.6 reconciler. Case (b) is rare to observe here in practice: a shrink
    already denies at ``actor_with_target_workspace_scope``'s own
    ``token_workspace_ids`` check (the control-plane index row is gone
    immediately), well before this workspace-plane check ever runs -- so a
    caller reaching THIS error is overwhelmingly case (a), "not synced yet",
    not "was here, then removed".

    Deliberately modeled on ``WorkspacePlaneUnavailableError`` immediately
    above, not on ``RejectedByPolicyError``: this is not a considered,
    stable "no" the way ``workspace_membership_missing`` is (a human must
    act to fix that). Section 2.6's fan-out already tried once, synchronously,
    on the very write that would have created this row, and the T4.6
    reconciler keeps retrying it on its own -- exactly the same "this
    resolves itself shortly, tell the caller to retry, don't tell them to
    stop" shape ``workspace_plane_unavailable`` already carries. Folding it
    into ``RejectedByPolicyError``'s 403 family (as every one of that
    family's existing broad ``except`` clauses would silently do) would tell
    a retrying client to give up on a gap that is actively self-healing.
    """

    error_code = "grant_replica_missing"

    def __init__(self, workspace_id: UUID | None = None) -> None:
        super().__init__(
            "no live workspace-plane credential grant replica for this "
            "workspace yet"
        )
        self.workspace_id = workspace_id


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace_id: UUID
    role: str
    # D-01v3-b: ceiling(actor.scopes, already narrowed by
    # actor_with_target_workspace_scope) ∩ granted(the workspace-plane
    # replica's granted_scopes), for a pat/oauth_jwt actor only. ``None`` for
    # every other actor kind (system, mcp_client, user_session) -- they carry
    # no workspace-plane credential grant, so there is nothing to narrow;
    # ``None`` means "apply no additional narrowing", never "deny everything".
    granted_scopes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WorkspaceMembershipView:
    user_id: UUID
    email: str
    display_name: str
    workspace_id: UUID
    workspace_slug: str
    role: str


def normalize_role(role: str) -> str:
    normalized = ROLE_ALIASES.get(role, role)
    if normalized not in ROLE_ORDER:
        raise ValueError("role must be one of: owner, editor, viewer")
    return normalized


def role_allows(actual_role: str, required_role: str) -> bool:
    actual = ROLE_ALIASES.get(actual_role, actual_role)
    if actual not in ROLE_ORDER:
        return False
    return ROLE_ORDER[actual] >= ROLE_ORDER[normalize_role(required_role)]


def get_workspace_access(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> WorkspaceAccess | None:
    membership = session.get(WorkspaceMembership, (user_id, workspace_id))
    if membership is None:
        return None
    return WorkspaceAccess(
        workspace_id=workspace_id,
        role=normalize_role(membership.role),
    )


def resolve_workspace_id_by_slug(session: Session, workspace_slug: str) -> UUID | None:
    workspace = session.scalar(
        select(Workspace).where(Workspace.slug == workspace_slug)
    )
    return workspace.id if workspace is not None else None


def resolve_workspace_id_by_context_page(
    session: Session,
    page_id: UUID,
) -> UUID | None:
    from backend.models.context import ContextPage

    page = session.get(ContextPage, page_id)
    return page.workspace_id if page is not None else None


def resolve_workspace_slug_by_id(session: Session, workspace_id: UUID) -> str | None:
    workspace = session.get(Workspace, workspace_id)
    return workspace.slug if workspace is not None else None


def _mcp_connection_is_disabled(
    session: Session,
    *,
    actor: Any,
    workspace_id: UUID,
) -> bool:
    """MCP-01: Settings > Access lets an owner disable one MCP connection
    without revoking its OAuth grant / PAT (that stays intact for a
    re-enable). Nothing on the token-verification path (JWT signature, jti
    denylist, ApiToken.revoked_at) reflects that toggle, so it must be
    enforced here instead -- the one seam both REST
    (rest_scopes.scope_actor_to_request_workspace) and MCP
    (mcp.router.resolve_target_actor) call once the target workspace for a
    bearer actor is known.

    ``session`` must already be scoped to ``workspace_id``'s own plane --
    ``MCPClient`` is workspace-plane data, not identity-plane like
    ``WorkspaceMembership``. ``require_workspace_role`` is the only caller;
    it opens this session from the required ``workspace_session_factory``
    kwarg rather than reusing its own (identity-plane) ``session`` argument,
    so a sharded overlay's disable toggle -- written via
    ``WorkspaceSessionFactoryDep`` in ``update_settings_mcp_client`` -- is
    read back from the same shard it was written to.

    Prefer the stable runtime binding introduced by MCP-2. In particular, an
    OAuth grant tombstone must block that grant across bearer rotation without
    blocking a newly authorized grant for the same public client. Null-binding
    rows retain the conservative legacy metadata fallback until migration or
    first-touch binding makes their identity exact.
    """

    token_kind = getattr(actor, "token_kind", None)
    if token_kind not in _MCP_CONNECTION_TOKEN_KINDS:
        return False
    user_id = getattr(actor, "user_id", None)
    if user_id is None:
        return False
    token_id = getattr(actor, "token_id", None)
    client_id = getattr(actor, "client_id", None)
    if token_kind == "oauth_jwt":
        if token_id is not None:
            runtime_binding_key = f"oauth_grant:{token_id}"
        else:
            return False
    else:
        if token_id is None:
            return False
        runtime_binding_key = f"api_token:{token_id}"

    exact = session.scalar(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.runtime_binding_key == runtime_binding_key,
        )
    )
    if exact is not None:
        return exact.status != "active"

    legacy_candidates = session.scalars(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.issued_to_user_id == user_id,
            MCPClient.runtime_binding_key.is_(None),
        )
    ).all()
    if token_kind == "oauth_jwt":
        if client_id is None:
            return False
        return any(
            candidate.status != "active"
            and (candidate.token_metadata or {}).get("oauth_client_id") == client_id
            for candidate in legacy_candidates
        )
    return any(
        candidate.status != "active"
        and (candidate.token_metadata or {}).get("token_id") == str(token_id)
        for candidate in legacy_candidates
    )


def _credential_grant_lookup_kind(check_kind: str) -> str | None:
    if check_kind == "pat":
        return "pat"
    if check_kind == "oauth_jwt":
        return "oauth"
    return None


def _effective_scopes_from_workspace_grant(
    workspace_session: Session, *, actor: Any, workspace_id: UUID
) -> tuple[str, ...] | None:
    """D-01v3-b: ceiling ∩ desired ∩ granted, the workspace-plane half.

    ``actor.scopes`` arriving here is already ceiling ∩ desired -- narrowed
    by ``actor_with_target_workspace_scope`` against the control-plane index
    row before ``require_workspace_role`` was ever called. This intersects
    that value one more time against ``WorkspaceCredentialGrant.
    granted_scopes``, the replica actually landed on THIS workspace's own
    plane. A plain set intersection is the entire safety property (plan
    section 2.6's own reasoning): even a corrupted or stale-wide replica
    (``granted_scopes`` broader than the control plane ever authorized)
    still cannot grant more than ``actor.scopes`` already allows, because
    intersection can only narrow, never widen, whichever side is wider.

    Returns ``None`` for every actor kind other than ``pat``/``oauth_jwt``
    (system and session actors) -- those never had a
    workspace-plane credential grant to begin with, so there is nothing to
    read or intersect; ``None`` here means "no such row exists for this
    actor kind, by design", not "look it up and find it missing".

    Raises ``GrantReplicaMissingError`` when the actor IS pat/oauth_jwt and
    no live (``revoked_at IS NULL``) replica row exists for
    ``(workspace_id, credential_kind, credential_id)``.
    ``actor.token_id`` is the credential identity: for a PAT actor it is
    ``ApiToken.id`` (``ApiTokenActor.token_id`` in
    ``api_token_service.authenticate_token``, threaded through
    ``backend.policy.auth._authenticate_api_token``'s ``token_id=primary.
    token_id``), the exact value ``credential_replica_service``'s PAT path
    writes ``WorkspaceCredentialGrant.credential_id`` as (its own docstring:
    "credential_id IS the PK's token_id"). For an oauth_jwt actor it is the
    refresh-token family's ``grant_id`` (``backend.policy.auth.
    _authenticate_oauth_token``'s ``token_id=actor.grant_id``, itself
    ``OAuthService.verify_access_token``'s JWT ``grant_id`` claim), the same
    identifier ``resolve_oauth_replica_credential_id`` resolves and the OAuth
    fan-out path writes ``WorkspaceCredentialGrant.credential_id`` as. Both
    are the same identifier space the fan-out wrote under by construction,
    not by coincidence -- see ``backend/tests/unit/test_rbac_credential_
    grant.py`` for an explicit round-trip test of this exact claim (write via
    ``credential_replica_service``, read back here).
    """

    check_kind = actor_kind_for_check(actor)
    credential_kind = _credential_grant_lookup_kind(check_kind)
    if credential_kind is None:
        return None
    credential_id = getattr(actor, "token_id", None)
    grant = (
        workspace_session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id,
                WorkspaceCredentialGrant.credential_kind == credential_kind,
                WorkspaceCredentialGrant.credential_id == credential_id,
            )
        )
        if credential_id is not None
        else None
    )
    if grant is None or grant.revoked_at is not None:
        raise GrantReplicaMissingError(workspace_id=workspace_id)
    granted = set(grant.granted_scopes)
    return tuple(scope for scope in (actor.scopes or ()) if scope in granted)


def rebind_actor_to_workspace_access(actor: Any, access: WorkspaceAccess) -> Any:
    """Apply a ``require_workspace_role`` result back onto ``actor``.

    Every call site historically did this by hand with its own
    ``dataclasses.replace(actor, workspace_id=access.workspace_id,
    workspace_role=access.role, role=access.role)`` -- a shared helper means
    D-01v3-b's ``access.granted_scopes`` (non-``None`` only for a pat/
    oauth_jwt actor, see ``_effective_scopes_from_workspace_grant``) lands on
    ``actor.scopes`` the same way ``workspace_id``/``role``/``workspace_role``
    already do everywhere, instead of a hand-rolled ``replace(...)`` silently
    keeping the actor's pre-intersection scopes.
    """

    updates: dict[str, Any] = {
        "workspace_id": access.workspace_id,
        "workspace_role": access.role,
        "role": access.role,
    }
    if access.granted_scopes is not None:
        updates["scopes"] = access.granted_scopes
    return replace(actor, **updates)


def scope_actor_to_workspace_grant(
    *,
    actor: Any,
    workspace_id: UUID,
    workspace_session_factory: sessionmaker[Session],
) -> Any:
    """Intersect a credential actor with its workspace-plane grant replica.

    This is the side-effect-free preflight used before MCP lifecycle
    first-touch registration.  It deliberately does not inspect or create an
    ``MCPClient`` row: lifecycle status and denial auditing remain owned by
    ``evaluate_mcp_client_runtime``.  PAT/OAuth actors must have a live grant
    replica; actor kinds that do not use credential replicas are returned
    unchanged.
    """

    try:
        with workspace_session_factory() as workspace_session:
            granted_scopes = _effective_scopes_from_workspace_grant(
                workspace_session,
                actor=actor,
                workspace_id=workspace_id,
            )
    except SQLAlchemyError as exc:
        if is_database_schema_mismatch(exc):
            raise
        raise WorkspacePlaneUnavailableError(workspace_id=workspace_id) from exc
    if granted_scopes is None:
        return actor
    return replace(actor, scopes=granted_scopes)


def require_workspace_role(
    session: Session,
    actor: Any,
    workspace_id: UUID,
    required_role: str,
    *,
    workspace_session_factory: sessionmaker[Session],
) -> WorkspaceAccess:
    """Authorize ``actor`` for ``required_role`` on ``workspace_id``.

    Two planes, two sessions, deliberately not merged: ``session`` is the identity
    plane -- ``check_actor_can_access``/``get_workspace_access`` read
    ``WorkspaceMembership`` there. ``workspace_session_factory`` is a
    required keyword-only argument (no default -- a caller that forgets it
    gets a ``TypeError`` at the call site, not a check that silently no-ops)
    that opens a session on ``workspace_id``'s OWN plane, because
    ``MCPClient`` -- the row Settings > Access toggles -- is workspace-plane
    data. Once a caller shards by workspace, a control-only session cannot
    see a disable written to the workspace's own database; every production
    caller passes ``db.session_for(workspace_id)`` (the same call already
    used to open the actual domain read/write) so both checks land on the
    plane that owns their data. On an unsharded deployment
    ``session_for(workspace_id)`` returns the control factory anyway, so
    this costs one extra same-DB session, never a different answer.

    D-01v3-b (P10.3 T2 follow-up): this is also where the workspace-plane
    grant-replica intersection (ceiling ∩ desired ∩ granted) actually runs
    now -- the SAME ``with workspace_session_factory() as workspace_session:``
    block the MCPClient disable check already opens, since both read
    workspace-plane data for the same resolved target. See
    ``_effective_scopes_from_workspace_grant``.
    """

    required = normalize_role(required_role)
    try:
        check_actor_can_access(
            actor,
            workspace_id,
            required_role_to_action(required),
            session=session,
        )
    except PermissionError as exc:
        error_code = str(exc).split(":", 1)[0] or "workspace_membership_missing"
        raise RejectedByPolicyError(
            str(exc),
            error_code=error_code,
        ) from exc

    try:
        with workspace_session_factory() as workspace_session:
            connection_disabled = _mcp_connection_is_disabled(
                workspace_session, actor=actor, workspace_id=workspace_id
            )
            # Short-circuit before the grant-replica read when the
            # connection is already disabled: that is a separate, more
            # specific stored fact (an owner's explicit Settings > Access
            # toggle) that should win over -- and not pay the extra query
            # for -- a sync-state question that will be moot once we raise
            # mcp_connection_disabled just below anyway.
            granted_scopes = (
                None
                if connection_disabled
                else _effective_scopes_from_workspace_grant(
                    workspace_session, actor=actor, workspace_id=workspace_id
                )
            )
    except SQLAlchemyError as exc:
        # D-01v3-e (T0): a physical-split overlay's session_for(workspace_id)
        # can fail to open or query the target workspace's OWN database while
        # the identity/control plane above stayed reachable -- the two
        # PermissionError/RejectedByPolicyError branches in this function
        # cannot represent that, and neither could any caller distinguish it
        # from a genuine policy denial if it leaked out as one. Schema
        # mismatch is a DIFFERENT, already-handled diagnostic
        # (backend.app.main.unhandled_exception_handler) and must keep
        # surfacing as that, not get relabeled as plane unavailability.
        if is_database_schema_mismatch(exc):
            raise
        raise WorkspacePlaneUnavailableError(workspace_id=workspace_id) from exc
    if connection_disabled:
        raise RejectedByPolicyError(
            "this connection has been disabled for this workspace",
            error_code="mcp_connection_disabled",
        )

    # Some late-target callers need only the role decision and do not carry
    # the returned WorkspaceAccess into a rebound ActorContext. Enforce the
    # requested action again after the workspace-plane replica intersection,
    # so those callers cannot retain a wider control-plane scope by omission.
    if granted_scopes is not None:
        effective_actor = replace(actor, scopes=granted_scopes)
        try:
            check_actor_can_access(
                effective_actor,
                workspace_id,
                required_role_to_action(required),
                session=session,
            )
        except PermissionError as exc:
            error_code = str(exc).split(":", 1)[0] or "workspace_membership_missing"
            raise RejectedByPolicyError(
                str(exc),
                error_code=error_code,
            ) from exc

    if getattr(actor, "user_id", None) is None:
        return WorkspaceAccess(
            workspace_id=workspace_id, role="owner", granted_scopes=granted_scopes
        )
    access = get_workspace_access(
        session,
        user_id=actor.user_id,
        workspace_id=workspace_id,
    )
    if access is None:
        raise RejectedByPolicyError(
            "workspace membership is required",
            error_code="workspace_membership_missing",
        )
    return WorkspaceAccess(
        workspace_id=workspace_id,
        role=normalize_role(access.role),
        granted_scopes=granted_scopes,
    )


def list_user_workspaces(
    session: Session,
    user_id: UUID,
) -> list[tuple[Workspace, str]]:
    return list(
        session.execute(
            select(Workspace, WorkspaceMembership.role)
            .join(
                WorkspaceMembership,
                WorkspaceMembership.workspace_id == Workspace.id,
            )
            .where(WorkspaceMembership.user_id == user_id)
            .order_by(Workspace.created_at, Workspace.slug)
        ).all()
    )


def grant_workspace_membership(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    role: str,
) -> WorkspaceMembership:
    normalized_role = normalize_role(role)
    membership = session.get(WorkspaceMembership, (user_id, workspace_id))
    if membership is None:
        membership = WorkspaceMembership(
            user_id=user_id,
            workspace_id=workspace_id,
            role=normalized_role,
        )
        session.add(membership)
    else:
        membership.role = normalized_role

    workspace = session.get(Workspace, workspace_id)
    if workspace is not None and normalized_role == "owner":
        workspace.owner_user_id = user_id
    session.flush()
    return membership


def remove_workspace_membership(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> bool:
    membership = session.get(WorkspaceMembership, (user_id, workspace_id))
    if membership is None:
        return False
    session.delete(membership)
    workspace = session.get(Workspace, workspace_id)
    if workspace is not None and workspace.owner_user_id == user_id:
        workspace.owner_user_id = None
    session.flush()
    return True
