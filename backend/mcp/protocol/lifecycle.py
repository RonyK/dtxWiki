"""Shared MCP lifecycle guard for tools, prompts, and resources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    get_workspace_access,
    scope_actor_to_workspace_grant,
)
from backend.policy.trust_boundaries import (
    actor_kind_for_check,
    actor_with_target_workspace_scope,
    check_actor_can_access,
)
from backend.services.mcp_client_service import (
    MCPClientRuntimeDenied,
    evaluate_mcp_client_runtime,
)


@dataclass(frozen=True)
class ActorLifecycleResult:
    actor: ActorContext
    active_workspace_ids: tuple[UUID, ...]
    denial: MCPClientRuntimeDenied | None = None
    # Per-workspace effective scope for each of ``active_workspace_ids``, for
    # a pat/oauth_jwt actor only (``None`` otherwise, or when there is a
    # single/no active workspace and nothing to disaggregate). This used to
    # live on ``ActorContext.token_workspace_scopes``, which P10.3 T2 removed
    # -- no authorization check ever read it there either; it exists purely
    # for a future diagnostic/observability consumer.
    workspace_scopes: dict[UUID, tuple[str, ...]] | None = None


def enforce_mcp_surface_lifecycle(
    *,
    actor: ActorContext,
    session_factory,
    workspace_id: UUID,
) -> None:
    """Fail before an MCP surface reads or returns Workspace data.

    ``evaluate_mcp_client_runtime`` owns the stable-binding lookup, first-touch
    registration, tombstone semantics, and denial audit. This adapter only
    translates its domain denial to a typed MCP protocol error so every
    transport exposes the same lifecycle boundary.
    """

    if session_factory is None:
        return
    with session_factory.begin() as session:
        denial = evaluate_mcp_client_runtime(
            session,
            workspace_id=workspace_id,
            actor=actor,
        )
    if denial is not None:
        raise lifecycle_mcp_error(denial)


def enforce_actor_mcp_surface_lifecycle(
    *,
    actor: ActorContext,
    fallback_session_factory,
    db=None,
) -> ActorContext:
    """Guard actor-scoped discovery surfaces without a request target.

    Evaluate every live candidate with that Workspace's exact OAuth/PAT grant.
    A disabled client binding is local to its Workspace: discovery continues
    through the remaining active candidates and receives an actor narrowed to
    precisely those candidates. If every accessible candidate is disabled,
    preserve the ordinary typed lifecycle denial.
    """

    result = evaluate_actor_mcp_surface_lifecycle(
        actor=actor,
        fallback_session_factory=fallback_session_factory,
        db=db,
    )
    if not result.active_workspace_ids and result.denial is not None:
        raise lifecycle_mcp_error(result.denial)
    return result.actor


def evaluate_actor_mcp_surface_lifecycle(
    *,
    actor: ActorContext,
    fallback_session_factory,
    db=None,
) -> ActorLifecycleResult:
    """Return the active per-Workspace lifecycle projection for ``actor``.

    Unlike the protocol-facing enforcing adapter, this function returns an
    all-disabled denial to the tool runner in domain form, so it can preserve
    the ordinary structured ``mcp_client_*`` tool envelope.
    """

    if db is None and fallback_session_factory is None:
        return ActorLifecycleResult(actor=actor, active_workspace_ids=())

    check_kind = actor_kind_for_check(actor)
    control_factory = (
        db.control_session_factory() if db is not None else fallback_session_factory
    )
    candidates = actor.token_workspace_ids
    if not candidates:
        candidates = (actor.workspace_id,) if actor.workspace_id is not None else ()
    # F-19: a candidate set that was empty from the start (a workspace-unbound
    # PAT, or an oauth_jwt actor constructed with no workspace claim) is not
    # the same failure as a non-empty set where every candidate lost access.
    # The former is the normal state for a token that has never had a
    # Workspace attached; wiping its scopes here would close
    # list_my_workspaces, the one surface meant to work without a target
    # Workspace. Only the latter case (candidates existed and all were lost)
    # should scrub the actor below.
    had_candidates = bool(candidates)
    seen: set[UUID] = set()
    active: list[tuple[UUID, ActorContext]] = []
    first_denial: MCPClientRuntimeDenied | None = None
    for workspace_id in candidates:
        if workspace_id in seen:
            continue
        seen.add(workspace_id)
        # Token claims are not a substitute for live membership. In
        # particular, an unexpired OAuth JWT can outlive removal from one of
        # its claimed Workspaces. Discovery must neither recreate a lifecycle
        # row there nor let that inaccessible row suppress every remaining
        # Workspace exposed by the actor.
        session_factory = (
            db.session_for(workspace_id) if db is not None else fallback_session_factory
        )
        try:
            with control_factory() as session:
                check_actor_can_access(
                    actor,
                    workspace_id,
                    "read",
                    session=session,
                )
                scoped_actor = actor_with_target_workspace_scope(
                    actor,
                    workspace_id,
                    session=session,
                )
                access = (
                    get_workspace_access(
                        session,
                        user_id=scoped_actor.user_id,
                        workspace_id=workspace_id,
                    )
                    if scoped_actor.user_id is not None
                    else None
                )
                if access is not None:
                    scoped_actor = replace(
                        scoped_actor,
                        workspace_id=workspace_id,
                        workspace_role=access.role,
                        role=access.role,
                    )
            # A credential's workspace-plane replica is an authorization
            # prerequisite, not lifecycle data. Check and apply it before
            # evaluate_mcp_client_runtime can first-touch an MCPClient row.
            scoped_actor = scope_actor_to_workspace_grant(
                actor=scoped_actor,
                workspace_id=workspace_id,
                workspace_session_factory=session_factory,
            )
        except (
            PermissionError,
            GrantReplicaMissingError,
            WorkspacePlaneUnavailableError,
        ):
            continue
        if session_factory is None:
            active.append((workspace_id, scoped_actor))
            continue
        with session_factory.begin() as session:
            denial = evaluate_mcp_client_runtime(
                session,
                workspace_id=workspace_id,
                actor=scoped_actor,
            )
        if denial is not None:
            if first_denial is None:
                first_denial = denial
            continue
        active.append((workspace_id, scoped_actor))

    if not active:
        if first_denial is not None:
            return ActorLifecycleResult(
                actor=actor,
                active_workspace_ids=(),
                denial=first_denial,
            )
        if had_candidates and check_kind in {"oauth_jwt", "pat"}:
            # Every token candidate lost live consent/membership. Do not leave
            # the original aggregate scopes visible on discovery surfaces.
            return ActorLifecycleResult(
                actor=replace(
                    actor,
                    scopes=(),
                    workspace_id=None,
                    workspace_role="none",
                    role=None,
                    token_workspace_ids=(),
                ),
                active_workspace_ids=(),
                workspace_scopes={},
            )
        return ActorLifecycleResult(actor=actor, active_workspace_ids=())

    active_ids = tuple(workspace_id for workspace_id, _actor in active)
    if check_kind not in {"oauth_jwt", "pat"}:
        return ActorLifecycleResult(actor=actor, active_workspace_ids=active_ids)
    exact_scopes = {
        workspace_id: tuple(scoped_actor.scopes)
        for workspace_id, scoped_actor in active
    }
    aggregate_scopes = tuple(
        dict.fromkeys(
            scope
            for _workspace_id, scoped_actor in active
            for scope in scoped_actor.scopes
        )
    )
    selected = next(
        (
            scoped_actor
            for workspace_id, scoped_actor in active
            if workspace_id == actor.workspace_id
        ),
        active[0][1],
    )
    return ActorLifecycleResult(
        actor=replace(
            selected,
            scopes=aggregate_scopes,
            token_workspace_ids=active_ids,
        ),
        active_workspace_ids=active_ids,
        workspace_scopes=exact_scopes,
    )


def lifecycle_mcp_error(denial: MCPClientRuntimeDenied) -> McpError:
    return McpError(
        ErrorData(
            code=INVALID_PARAMS,
            message=str(denial),
            data={
                "error_code": denial.error_code,
                "client_id": str(denial.client_id),
            },
        )
    )


__all__ = [
    "ActorLifecycleResult",
    "enforce_actor_mcp_surface_lifecycle",
    "enforce_mcp_surface_lifecycle",
    "evaluate_actor_mcp_surface_lifecycle",
    "lifecycle_mcp_error",
]
