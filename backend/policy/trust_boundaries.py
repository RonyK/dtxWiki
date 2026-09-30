"""Trust boundary checks — Sprint_5 Phase_1.

Closes R-F6-7 by centralising actor/action access decisions.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, TypeVar
from uuid import UUID

from backend.models.workspace import WorkspaceMembership

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from backend.policy.auth import ActorContext

ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}
ROLE_ALIASES = {"admin": "owner", "member": "editor", "reviewer": "editor"}


def oauth_consented_workspace_scope(
    session: Session, *, user_id: UUID, client_id: str, workspace_id: UUID
) -> tuple[str, ...] | None:
    """The scope (user, client) currently holds for this workspace, or None.

    D-01: read fresh from the control DB on every call. There is no positive
    scope cache any more -- an OAuth access token now carries the one
    workspace it was issued for, so this is a single-row lookup per request,
    and caching it only reintroduced the revoke-then-stale-read race a
    process-local cache used to have (F-15). Reuse within one request is left
    to the caller's own session/transaction, not to a second cache here.
    """

    from backend.models.oauth import OAuthConsent

    consent = session.get(OAuthConsent, (user_id, client_id, workspace_id))
    if consent is None or consent.removed_at is not None:
        return None
    return tuple(consent.desired_scopes)


def actor_with_target_workspace_scope(
    actor: ActorContext,
    workspace_id: UUID,
    *,
    session: Session,
) -> ActorContext:
    """Return the actor authorized against exactly ``workspace_id``.

    D-01v2: a PAT/OAuth credential now carries a SET of allowed workspaces
    (``actor.token_workspace_ids``), read live from ``api_token_workspaces``/
    ``oauth_consents`` at authentication time. This is the sole choke point
    every caller (MCP ``resolve_target_actor``, ``prompts_resources.py``,
    ``runner.py``, ``lifecycle.py``, REST ``rest_scopes.py``) uses to resolve
    a request's target workspace against that set: the target must be a
    member of the set, and live ``WorkspaceMembership`` must exist for it, or
    the call is denied outright. Unlike D-01, this also backfills ``role``/
    ``workspace_role`` for the resolved workspace -- a PAT/OAuth actor's role
    is otherwise unresolved (``None``) until this runs, since token
    verification no longer confirms a role for any one workspace up front.

    D-01v3's workspace-plane grant-replica intersection (ceiling ∩ desired ∩
    granted) is deliberately NOT implemented here -- this checks only the
    control-plane allowed set (``token_workspace_ids``), matching D-01v2's
    core candidacy behavior. That final ∩ granted narrowing happens one step
    later, in ``backend.policy.rbac.require_workspace_role`` (P10.3 GAP-2):
    this function only ever holds a single control-plane ``session``, and
    the grant replica lives on the target workspace's OWN plane, which
    ``require_workspace_role`` already opens (via its required
    ``workspace_session_factory`` kwarg) for the MCPClient disable check --
    reusing that same open session there, rather than threading a second
    ``DatabaseProvider``/session into every one of THIS function's callers,
    is what keeps the blast radius of that check to one function.
    """

    check_kind = actor_kind_for_check(actor)
    if check_kind not in {"pat", "oauth_jwt"}:
        return actor
    if workspace_id not in actor.token_workspace_ids:
        raise PermissionError(
            "token_workspace_denied: workspace is not in the token's allowed set"
        )
    if check_kind == "pat":
        from backend.models.api_token import ApiTokenWorkspace

        token_id = getattr(actor, "token_id", None)
        grant = (
            session.get(ApiTokenWorkspace, (token_id, workspace_id))
            if token_id is not None
            else None
        )
        if grant is None or grant.removed_at is not None:
            raise PermissionError(
                "token_workspace_denied: live PAT workspace grant is required"
            )
        desired = set(grant.desired_scopes)
        effective_scopes = tuple(
            scope for scope in (actor.scopes or ()) if scope in desired
        )
    else:
        user_id = getattr(actor, "user_id", None)
        client_id = getattr(actor, "client_id", None)
        consented_scopes = (
            oauth_consented_workspace_scope(
                session,
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
            )
            if user_id is not None and client_id
            else None
        )
        if consented_scopes is None:
            raise PermissionError(
                "token_workspace_denied: live OAuth consent is required"
            )
        # The token's own scope claim is the upper bound; live consent can
        # only narrow it further, never widen it past what was issued. An
        # ordinary re-authorize round for this same workspace only ever
        # merges/grows the stored consent scope (oauth_service.py's
        # `merge_scopes` at grant time) -- it is an explicit, separate
        # revoke-then-narrower-grant or account-level scope reduction that
        # can actually shrink live consent below the token's own claim, and
        # this intersection is what makes that reduction take effect on the
        # next call without waiting for the token to expire.
        granted = set(consented_scopes)
        effective_scopes = tuple(
            scope for scope in (actor.scopes or ()) if scope in granted
        )

    user_id = getattr(actor, "user_id", None)
    membership = (
        session.get(WorkspaceMembership, (user_id, workspace_id))
        if user_id is not None
        else None
    )
    if membership is None:
        raise PermissionError(
            "workspace_membership_missing: workspace membership is required"
        )
    return replace(
        actor,
        workspace_id=workspace_id,
        scopes=effective_scopes,
        role=membership.role,
        workspace_role=membership.role,
    )


def _normalize_role(role: str) -> str:
    normalized = ROLE_ALIASES.get(role, role)
    if normalized not in ROLE_ORDER:
        raise ValueError("role must be one of: owner, editor, viewer")
    return normalized


def _role_allows(actual_role: str, required_role: str) -> bool:
    return (
        ROLE_ORDER[_normalize_role(actual_role)]
        >= ROLE_ORDER[_normalize_role(required_role)]
    )


Action = Literal["read", "write", "admin"]
AccessActorKind = Literal["user_session", "pat", "oauth_jwt", "system"]
ACTION_REQUIRED_ROLE: dict[Action, str] = {
    "read": "viewer",
    "write": "editor",
    "admin": "owner",
}
def actor_kind_for_check(actor: ActorContext) -> AccessActorKind:
    """Return the access-check kind for ``actor``.

    Interactive access is either a human browser session or a human backed by
    a PAT/OAuth bearer. Background workers construct ``system`` actors. The
    retired direct ``mcp_client`` identity and every unknown actor kind fail
    closed; ``mcp_client`` remains only as persisted trace/audit vocabulary.
    """

    actor_kind = getattr(actor, "actor_kind", None)
    token_kind = getattr(actor, "token_kind", None)
    if actor_kind == "system":
        return "system"
    if actor_kind == "mcp_client":
        # ``mcp_client`` remains valid persisted provenance vocabulary, but
        # the runtime identity belonged to the removed local stdio transport.
        # Interactive MCP access must arrive as an authenticated human actor
        # backed by a PAT or OAuth grant.
        raise PermissionError(
            "unsupported_actor_kind: direct MCP client credentials are not supported"
        )
    if actor_kind == "human":
        if token_kind in {"api_token", "pat"}:
            return "pat"
        if token_kind == "oauth_jwt":
            return "oauth_jwt"
        return "user_session"
    raise PermissionError(f"unknown_actor_kind: unknown actor kind {actor_kind!r}")


def action_to_required_role(action: Action) -> str:
    return ACTION_REQUIRED_ROLE[action]


def required_role_to_action(required_role: str) -> Action:
    normalized = _normalize_role(required_role)
    if normalized == "owner":
        return "admin"
    if normalized == "editor":
        return "write"
    return "read"


def check_actor_can_access(
    actor: ActorContext,
    resource_workspace_id: UUID,
    action: Action,
    *,
    session: Session | None = None,
) -> None:
    """Raises PermissionError on deny, returns None on allow.

    Single source of truth for actor_kind × action, restricted-library-local
    scope, OAuth token workspace claim binding, and membership role checks.
    """

    check_kind = actor_kind_for_check(actor)
    if check_kind == "system":
        return
    # D-01v2: a PAT or OAuth credential carries a SET of allowed workspaces
    # (``token_workspace_ids``). That set is authoritative, not a filter
    # layered on top of membership -- a workspace outside the set is denied
    # outright, with no step-up via live consent and no fallback to
    # membership alone. ``_check_scope`` enforces set membership (for
    # check_kind in {"pat", "oauth_jwt"}) before it checks scope sufficiency.
    _check_scope(
        check_kind, actor, action, resource_workspace_id=resource_workspace_id
    )
    user_id = getattr(actor, "user_id", None)
    if session is None or user_id is None:
        raise PermissionError(
            "workspace_membership_missing: workspace membership is required"
        )
    membership = session.get(WorkspaceMembership, (user_id, resource_workspace_id))
    if membership is None:
        raise PermissionError(
            "workspace_membership_missing: workspace membership is required"
        )
    required_role = action_to_required_role(action)
    if not _role_allows(membership.role, required_role):
        raise PermissionError(
            f"workspace_role_insufficient: workspace role {required_role} is required"
        )


def _check_scope(
    check_kind: AccessActorKind,
    actor: ActorContext,
    action: Action,
    *,
    resource_workspace_id: UUID | None = None,
) -> None:
    if check_kind == "user_session":
        return
    if (
        resource_workspace_id is not None
        and check_kind in {"pat", "oauth_jwt"}
        and resource_workspace_id not in actor.token_workspace_ids
    ):
        # D-01v2: a PAT/OAuth token carries a SET of allowed workspaces
        # (``token_workspace_ids``), read live at authentication time. A
        # workspace this particular call is targeting (see
        # backend.api.read._require_sensitive_agent_page_access) that is
        # outside that set is denied outright -- a broad scope string on the
        # token never lets it reach a workspace the set does not name.
        raise PermissionError(
            "token_workspace_denied: workspace is not in the token's allowed set"
        )
    # D-01v2: a token-bearing actor's ``scopes`` is the exact grant for the
    # target workspace once ``actor_with_target_workspace_scope`` has
    # narrowed it (ceiling ∩ live consent for OAuth); before that it is the
    # token's ceiling. There is no per-workspace scope map to index by
    # ``resource_workspace_id`` here.
    scopes = set(getattr(actor, "scopes", ()) or ())
    if action == "read":
        allowed = (
            "mcp:read" in scopes
            or "mcp:write" in scopes
            or "mcp:agent" in scopes
        )
    elif action == "write":
        allowed = "mcp:write" in scopes or "mcp:agent" in scopes
    else:
        allowed = "mcp:write" in scopes
    if not allowed:
        raise PermissionError(
            "workspace_role_insufficient: scope insufficient for action"
        )


def check_actor_scope(
    actor: ActorContext,
    action: Action,
    *,
    resource_workspace_id: UUID | None = None,
) -> None:
    """Require the token scope for an action without repeating role checks.

    Sensitive-body gates already hold a resolved workspace role, but PAT and
    OAuth actors must still satisfy the scope dimension of the access matrix.
    System actors retain the same scope-free behavior as
    :func:`check_actor_can_access`.

    ``resource_workspace_id`` names the workspace being guarded. D-01v2: a
    pat/oauth_jwt actor is bound to a SET of allowed workspaces, so this is a
    set-membership check, not a single-claim mismatch check -- see
    ``_check_scope``. An MCP call has already rebound
    ``actor.workspace_id`` to the resolved target workspace
    (``backend.mcp.router.resolve_target_actor``), which is why it is the
    implicit default; a REST actor is never rebound, so its path workspace
    must be passed explicitly.
    """

    check_kind = actor_kind_for_check(actor)
    if check_kind == "system":
        return
    if resource_workspace_id is None:
        resource_workspace_id = getattr(actor, "workspace_id", None)
    _check_scope(
        check_kind, actor, action, resource_workspace_id=resource_workspace_id
    )


WorkspaceRow = TypeVar("WorkspaceRow")


def actor_workspace_filter(  # noqa: UP047
    actor: ActorContext,
    candidates: list[tuple[WorkspaceRow, str | None]],
) -> list[tuple[WorkspaceRow, str | None]]:
    """Filter workspace rows to the set a PAT/OAuth token is allowed to reach.

    D-01v2: a PAT or OAuth credential carries a SET of allowed workspaces
    (``actor.token_workspace_ids``) rather than a single claim -- a candidate
    row is kept iff its id is a member of that set. An empty set (a PAT/OAuth
    actor with no allowed workspaces) filters every candidate out. Session
    and system actors carry no token-level workspace restriction at this
    layer, so they see every candidate unfiltered.
    """

    check_kind = actor_kind_for_check(actor)
    if check_kind not in {"pat", "oauth_jwt"}:
        return candidates
    allowed = set(actor.token_workspace_ids)
    if not allowed:
        return []
    return [
        (workspace, role)
        for workspace, role in candidates
        if getattr(workspace, "id", None) in allowed
    ]


RESTRICTED_LIBRARY_SCOPES = {"library:restricted-read"}


class RestrictedLibraryDeniedError(PermissionError):
    """Typed denial for restricted Library item/artifact exposure.

    Subclasses ``PermissionError`` so existing ``except PermissionError`` sites
    keep catching it, while carrying a stable ``error_code`` so callers stop
    parsing the message string (single source for the REST/MCP wire code).
    """

    error_code = "restricted_library_denied"

    def __init__(
        self, message: str = "restricted Library item access denied"
    ) -> None:
        super().__init__(message)


def actor_can_read_restricted_library(actor: ActorContext | None) -> bool:
    """Single predicate for restricted Library visibility (REST, MCP, retrieval).

    Plan-settled winner (260606_0129 §6 쟁점 6): the permissive
    owner/admin/scope predicate wins over the former MCP system-only deny, so
    REST and MCP now answer identically. ``system`` actors (background jobs,
    indexing) retain full visibility.
    """

    if actor is None:
        return False
    if getattr(actor, "actor_kind", None) == "system":
        return True
    # Normalize the role through the same alias table the draft predicate uses
    # (admin->owner, member/reviewer->editor) so the two canonical predicates
    # resolve roles identically. Restricted Library requires owner-level.
    role = getattr(actor, "workspace_role", None)
    if role:
        try:
            if _role_allows(role, "owner"):
                return True
        except ValueError:
            pass
    return any(
        scope in RESTRICTED_LIBRARY_SCOPES
        for scope in getattr(actor, "scopes", ())
    )


def check_restricted_library_allowed(actor: ActorContext, *, restricted: bool) -> None:
    """Single choke point for restricted LibraryItem/LibraryArtifact exposure."""

    if restricted and not actor_can_read_restricted_library(actor):
        raise RestrictedLibraryDeniedError()


def actor_can_read_drafts(actor: ActorContext | None) -> bool:
    """Single predicate for draft (unpublished) content visibility.

    Drafts are pre-review content. The RAG retrieval tiers physically hold both
    published and draft chunks (Epic-1 §7.4), so this per-actor predicate is the
    sole defense against surfacing unreviewed content. Only actors with write
    capability (role ``editor`` or above — owners and write-context MCP agents
    whose membership resolves to editor+) may see drafts in catalogs (T1) and
    search results (T2); viewers and anonymous see published content only.
    ``system`` actors (indexing/background) retain full visibility.
    """

    if actor is None:
        return False
    if getattr(actor, "actor_kind", None) == "system":
        return True
    role = getattr(actor, "workspace_role", None)
    if not role:
        return False
    try:
        return _role_allows(role, "editor")
    except ValueError:
        return False


def actor_can_read_sensitive_agent_pages(actor: ActorContext | None) -> bool:
    """Return whether AgentPage categories reserved for owners may be named.

    Sensitive AgentPage titles are themselves private metadata. A background
    system actor is trusted; an interactive PAT/OAuth actor needs an
    owner-level role and an administrative MCP scope. Role resolution uses
    ``workspace_role`` to match ``actor_can_read_drafts`` exactly.
    """

    if actor is None:
        return False
    if getattr(actor, "actor_kind", None) == "system":
        return True
    if getattr(actor, "actor_kind", None) == "mcp_client":
        return False
    role = getattr(actor, "workspace_role", None)
    if not role:
        return False
    try:
        if not _role_allows(role, "owner"):
            return False
        check_actor_scope(actor, "admin")
        return True
    except (PermissionError, ValueError):
        return False
