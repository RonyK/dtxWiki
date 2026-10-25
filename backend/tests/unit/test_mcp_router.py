from uuid import uuid4

from sqlalchemy.exc import OperationalError

from backend.app.container import DatabaseProvider
from backend.mcp.envelope import MCPErrorResponse
from backend.mcp.router import (
    PolicyRejection,
    dispatch,
    resolve_target_actor,
    resolve_workspace_id_for_tool,
)
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.page_service import PageService


# resolve_target_actor requires an explicit `db` (Epic-2 D-1: the
# MCP-connection-disabled check inside require_workspace_role must open its
# own session on the target workspace's plane, not reuse whatever session
# session_factory happens to back -- see backend/policy/rbac.py). These
# single-DB tests have no real routing provider, so wrap the same
# session_factory the way backend.mcp.router.dispatch's own bare-dispatch
# fallback does: session_for(workspace_id) resolves back to this factory.
def _test_db(session_factory) -> DatabaseProvider:
    return DatabaseProvider(None, session_factory=session_factory)


def test_resolve_target_actor_rescopes_workspace_role(session_factory, page_service):
    user_id = uuid4()
    token_id = uuid4()
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="mcp-target-role",
            title="MCP Target Role",
            name="MCP Target Role",
            description="MCP target actor re-scope regression",
            goal="Fix-5 MCP actor scoping",
        )
        session.add(workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=workspace.id,
            role="viewer",
        )
        workspace_id = workspace.id
        session.add(
            ApiToken(
                id=token_id,
                user_id=user_id,
                name="MCP target role fixture",
                token_lookup_hash=f"mcp-target-role-{token_id}",
                hashed_token="unused",
                scopes=["mcp:read"],
            )
        )
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=token_id,
                workspace_id=workspace_id,
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        # P10.3 GAP-2: require_workspace_role's grant-replica intersection
        # (D-01v3-b) needs a live workspace_credential_grant row for this
        # hand-built pat actor -- it never went through the real PAT
        # creation/fan-out path (T4.5) that would normally create one.
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="pat",
                credential_id=token_id,
                user_id=user_id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        # D-01: a real api_token actor's workspace_id always mirrors its one
        # token_workspace_ids claim. role/workspace_role="owner" here is the
        # stale value this actor was carrying BEFORE resolution -- the point
        # of this test is that resolve_target_actor re-derives it from the
        # target workspace's real membership ("viewer") rather than trusting
        # what the actor already claims.
        role="owner",
        workspace_role="owner",
        workspace_id=workspace_id,
        token_kind="api_token",
        token_id=token_id,
        token_workspace_ids=(workspace_id,),
        scopes=("mcp:read",),
    )

    scoped_actor = resolve_target_actor(
        tool_name="search",
        payload={"workspace_id": str(workspace_id), "query": "needle"},
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert scoped_actor is not None
    assert scoped_actor.workspace_id == workspace_id
    assert scoped_actor.workspace_role == "viewer"
    assert scoped_actor.role == "viewer"
    assert scoped_actor.token_workspace_ids == (workspace_id,)


def test_resolve_target_actor_preserves_policy_denial_code(
    session_factory,
    page_service,
):
    user_id = uuid4()
    allowed_workspace_id = uuid4()
    with session_factory.begin() as session:
        denied_workspace = Workspace(
            slug="mcp-denied-token-workspace",
            title="MCP Denied Token Workspace",
            name="MCP Denied Token Workspace",
            description="MCP denial code regression",
            goal="Fix-5 MCP error semantics",
        )
        session.add(denied_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=denied_workspace.id,
            role="viewer",
        )
        denied_workspace_id = denied_workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="owner",
        workspace_role="owner",
        workspace_id=allowed_workspace_id,
        token_kind="api_token",
        token_workspace_ids=(allowed_workspace_id,),
        scopes=("mcp:read",),
    )

    result = resolve_target_actor(
        tool_name="search",
        payload={"workspace_id": str(denied_workspace_id), "query": "needle"},
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert result.error_code == "token_workspace_denied"


def test_resolve_target_actor_preserves_workspace_membership_missing(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.2-E (M-F5-2): when the actor has a session-cookie token
    (empty token_workspace_ids) but lacks membership in the target workspace,
    the MCP envelope must surface `workspace_membership_missing` rather than
    the generic `rejected_by_policy`.
    """
    user_id = uuid4()
    with session_factory.begin() as session:
        target_workspace = Workspace(
            slug="mcp-membership-missing",
            title="MCP Membership Missing",
            name="MCP Membership Missing",
            description="Fix-6 Task 1.2-E membership preservation",
            goal="Fix-6 MCP error_code distinctness",
        )
        session.add(target_workspace)
        session.flush()
        target_workspace_id = target_workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="owner",
        workspace_role="owner",
        workspace_id=None,
        token_kind="session_cookie",
        token_workspace_ids=(),
        scopes=("mcp:read",),
    )

    result = resolve_target_actor(
        tool_name="search",
        payload={"workspace_id": str(target_workspace_id), "query": "needle"},
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "workspace_membership_missing"


def test_resolve_target_actor_preserves_workspace_role_insufficient(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.2-E (M-F5-2): when the actor has membership but the role
    is below the tool's required role, the MCP envelope must surface
    `workspace_role_insufficient` rather than the generic `rejected_by_policy`.
    """
    user_id = uuid4()
    with session_factory.begin() as session:
        target_workspace = Workspace(
            slug="mcp-role-insufficient",
            title="MCP Role Insufficient",
            name="MCP Role Insufficient",
            description="Fix-6 Task 1.2-E role preservation",
            goal="Fix-6 MCP error_code distinctness",
        )
        session.add(target_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=target_workspace.id,
            role="viewer",
        )
        target_workspace_id = target_workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="viewer",
        workspace_role="viewer",
        workspace_id=target_workspace_id,
        token_kind="session_cookie",
        token_workspace_ids=(),
        scopes=("mcp:write",),
    )

    # create_context_page requires editor; viewer must surface the distinct code.
    result = resolve_target_actor(
        tool_name="create_context_page",
        payload={"workspace_id": str(target_workspace_id), "query": "x"},
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "workspace_role_insufficient"


def _seed_workspace(session, slug: str, title: str) -> Workspace:
    workspace = seed_default_workspace(session, slug=slug, title=title)
    workspace.description = f"Fix-6 identifier matrix fixture for {slug}"
    workspace.goal = "Fix-6 B-F5-CODEX-1 regression"
    session.flush()
    return workspace


def _seed_page(page_service: PageService, workspace_slug: str, slug: str) -> dict:
    page = page_service.write_page(
        workspace_slug=workspace_slug,
        slug=slug,
        title=f"Page {slug}",
        body_md=f"# Page {slug}\n\nFix-6 identifier-matrix fixture.",
        actor_kind="system",
        actor_id="fix6-test",
        actor_provenance="browser_session",
    )
    return page


def test_resolve_workspace_id_for_tool_rejects_page_id_slug_mismatch(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1 (B-F5-CODEX-1): page_id + workspace_slug pointing to
    different workspaces must reject with `not_found`.

    Codex's reproduction: caller knows workspace B's page_id but provides
    workspace A's slug. Previously the slug branch decided the auth check
    workspace while the service read used page_id workspace -> cross-workspace
    read bypass. Now both identifiers are compared before any role check.
    """
    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "fix6-ws-a", "Fix-6 Workspace A")
        workspace_b = _seed_workspace(session, "fix6-ws-b", "Fix-6 Workspace B")
        _ = workspace_a.id, workspace_b.id

    _seed_page(page_service, "fix6-ws-b", "page-on-b")
    page_b = page_service.read_page(workspace_slug="fix6-ws-b", slug="page-on-b")

    result = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={
            "page_id": str(page_b.id),
            "workspace_slug": "fix6-ws-a",
        },
        session_factory=session_factory,
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "not_found"


def test_resolve_workspace_id_for_tool_allows_page_id_slug_match(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1: page_id + workspace_slug agreeing on workspace proceed."""
    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "fix6-match-a", "Fix-6 Match A")
        workspace_id = workspace_a.id

    _seed_page(page_service, "fix6-match-a", "page-on-a")
    page_a = page_service.read_page(workspace_slug="fix6-match-a", slug="page-on-a")

    result = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={
            "page_id": str(page_a.id),
            "workspace_slug": "fix6-match-a",
        },
        session_factory=session_factory,
    )

    assert result == workspace_id


def test_resolve_workspace_id_for_tool_uses_page_id_alone(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1: page_id alone resolves to page's owning workspace."""
    with session_factory.begin() as session:
        workspace = _seed_workspace(session, "fix6-page-only", "Fix-6 Page Only")
        workspace_id = workspace.id

    _seed_page(page_service, "fix6-page-only", "lonely-page")
    page = page_service.read_page(workspace_slug="fix6-page-only", slug="lonely-page")

    result = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={"page_id": str(page.id)},
        session_factory=session_factory,
    )

    assert result == workspace_id


def test_resolve_workspace_id_for_tool_uses_slug_alone(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1: workspace_slug alone resolves to slug's workspace."""
    with session_factory.begin() as session:
        workspace = _seed_workspace(session, "fix6-slug-only", "Fix-6 Slug Only")
        workspace_id = workspace.id

    result = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={"workspace_slug": "fix6-slug-only", "slug": "any-page"},
        session_factory=session_factory,
    )

    assert result == workspace_id


def test_resolve_workspace_id_for_tool_rejects_workspace_id_page_id_mismatch(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1: explicit workspace_id + page_id mismatch also rejects."""
    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "fix6-explicit-a", "Fix-6 Explicit A")
        workspace_b = _seed_workspace(session, "fix6-explicit-b", "Fix-6 Explicit B")
        workspace_a_id = workspace_a.id
        _ = workspace_b.id

    _seed_page(page_service, "fix6-explicit-b", "page-on-explicit-b")
    page_b = page_service.read_page(
        workspace_slug="fix6-explicit-b", slug="page-on-explicit-b"
    )

    result = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={
            "workspace_id": str(workspace_a_id),
            "page_id": str(page_b.id),
        },
        session_factory=session_factory,
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "not_found"
    # A page ownership mismatch must not be conflated with two explicit
    # workspace locators disagreeing. Reuse the same two-workspace fixture;
    # syntax-only parser tests cannot observe this router error boundary.
    workspace_conflict = resolve_workspace_id_for_tool(
        tool_name="upload_image",
        payload={
            "workspace_id": str(workspace_a_id),
            "workspace_slug": "fix6-explicit-b",
            "page_id": str(page_b.id),
        },
        session_factory=session_factory,
    )
    assert workspace_conflict.error_code == "inconsistent_identifier"


def test_resolve_workspace_id_for_tool_rejects_page_id_mismatch_for_begin_context_edit(
    session_factory,
    page_service,
):
    """P8.6: the new page_id-bearing Context-editing tools get the same
    workspace_id/page_id consistency guard as upload_image (router.py's
    ``_PAGE_ID_CONSISTENCY_TOOLS``) -- a mismatch must reject before dispatch
    ever reaches the handler, not rely on the handler to notice."""

    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "p86-router-a", "P8.6 Router A")
        workspace_b = _seed_workspace(session, "p86-router-b", "P8.6 Router B")
        workspace_a_id = workspace_a.id
        _ = workspace_b.id

    _seed_page(page_service, "p86-router-b", "page-on-p86-b")
    page_b = page_service.read_page(workspace_slug="p86-router-b", slug="page-on-p86-b")

    result = resolve_workspace_id_for_tool(
        tool_name="begin_context_edit",
        payload={
            "workspace_id": str(workspace_a_id),
            "page_id": str(page_b.id),
        },
        session_factory=session_factory,
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "not_found"


def test_resolve_workspace_id_for_tool_uses_page_id_alone_for_discard_context_edit(
    session_factory,
    page_service,
):
    """P8.6: page_id alone (no explicit workspace_id) also resolves for the
    new tools, matching upload_image's existing contract."""

    with session_factory.begin() as session:
        workspace = _seed_workspace(session, "p86-page-only", "P8.6 Page Only")
        workspace_id = workspace.id

    _seed_page(page_service, "p86-page-only", "lonely-p86-page")
    page = page_service.read_page(
        workspace_slug="p86-page-only", slug="lonely-p86-page"
    )

    result = resolve_workspace_id_for_tool(
        tool_name="discard_context_edit",
        payload={"page_id": str(page.id)},
        session_factory=session_factory,
    )

    assert result == workspace_id


def test_resolve_target_actor_propagates_inconsistent_identifier(
    session_factory,
    page_service,
):
    """Fix-6 Task 1.1: dispatcher must surface `not_found`
    rather than silently treating the call as actor-only."""
    user_id = uuid4()
    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "fix6-dispatch-a", "Fix-6 Dispatch A")
        workspace_b = _seed_workspace(session, "fix6-dispatch-b", "Fix-6 Dispatch B")
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=workspace_a.id,
            role="owner",
        )
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=workspace_b.id,
            role="viewer",
        )
        workspace_a_id = workspace_a.id
        _ = workspace_b.id

    _seed_page(page_service, "fix6-dispatch-b", "secret-on-b")
    page_b = page_service.read_page(
        workspace_slug="fix6-dispatch-b", slug="secret-on-b"
    )

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="owner",
        workspace_role="owner",
        workspace_id=workspace_a_id,
        token_kind="oauth_jwt",
        token_workspace_ids=(workspace_a_id,),
        scopes=("mcp:read",),
    )

    result = resolve_target_actor(
        tool_name="upload_image",
        payload={
            "page_id": str(page_b.id),
            "workspace_slug": "fix6-dispatch-a",
        },
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "not_found"


def test_upload_image_rejects_workspace_page_mismatch_before_handler(
    session_factory,
    page_service,
):
    user_id = uuid4()
    with session_factory.begin() as session:
        workspace_a = _seed_workspace(session, "upload-router-a", "Upload Router A")
        workspace_b = _seed_workspace(session, "upload-router-b", "Upload Router B")
        for workspace in (workspace_a, workspace_b):
            grant_workspace_membership(
                session,
                user_id=user_id,
                workspace_id=workspace.id,
                role="editor",
            )
        workspace_a_id = workspace_a.id
        workspace_b_id = workspace_b.id

    _seed_page(page_service, "upload-router-b", "upload-page-on-b")
    page_b = page_service.read_page(
        workspace_slug="upload-router-b", slug="upload-page-on-b"
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="editor",
        workspace_role="editor",
        workspace_id=workspace_a_id,
        token_kind="oauth_jwt",
        token_workspace_ids=(workspace_a_id, workspace_b_id),
        scopes=("draft-write",),
    )

    result = resolve_target_actor(
        tool_name="upload_image",
        payload={
            "workspace_id": str(workspace_a_id),
            "page_id": str(page_b.id),
        },
        actor=actor,
        session_factory=session_factory,
        db=_test_db(session_factory),
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "not_found"


def test_dispatch_unknown_tool_returns_unknown_tool_envelope(page_service):
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        user_id=uuid4(),
        role="editor",
        workspace_role="editor",
        workspace_id=uuid4(),
        token_kind="session",
        scopes=("read", "draft-write"),
    )

    response = dispatch(
        "does_not_exist",
        {},
        actor=actor,
        page_service=page_service,
    )

    assert isinstance(response, MCPErrorResponse)
    assert response.envelope.result_status == "failed"
    assert response.envelope.workspace_id == actor.workspace_id
    assert response.envelope.actor.kind == actor.actor_kind
    assert response.envelope.actor.id == actor.actor_id
    assert response.envelope.result_summary == "Tool call failed: unknown_tool."
    assert response.envelope.error_code == "unknown_tool"
    assert set(response.model_dump().keys()) == {"envelope"}


class _FailingWorkspacePlaneProvider(DatabaseProvider):
    """T0 (D-01v3-e) test double: the identity/control plane (session_for
    with any OTHER id, and the control session_factory itself) stays healthy,
    but the one target workspace's own plane cannot be opened -- the exact
    split a physical-split overlay's session_for can produce and this
    single-DB test fixture otherwise cannot."""

    def __init__(self, session_factory, *, unavailable_workspace_id):
        super().__init__(None, session_factory=session_factory)
        self._unavailable_workspace_id = unavailable_workspace_id

    def session_for(self, workspace_id):
        if workspace_id == self._unavailable_workspace_id:
            return _raise_workspace_plane_unreachable
        return super().session_for(workspace_id)


def _raise_workspace_plane_unreachable():
    raise OperationalError(
        "SELECT 1", {}, Exception("could not connect to workspace database")
    )


def test_resolve_target_actor_maps_workspace_plane_failure_to_policy_rejection(
    session_factory,
    page_service,
):
    """T0: resolve_target_actor's own except clause only ever caught
    (PermissionError, ReadServiceError) -- a workspace-plane connectivity
    failure inside require_workspace_role's disable-check would have
    propagated uncaught as an unclassified crash instead of the retryable
    workspace_plane_unavailable PolicyRejection dispatch() maps to a proper
    MCP error envelope."""

    user_id = uuid4()
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="mcp-plane-unavailable-router",
            title="MCP Plane Unavailable Router",
            name="MCP Plane Unavailable Router",
            description="T0 workspace plane failure regression",
            goal="T0 D-01v3-e",
        )
        session.add(workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user_id,
            workspace_id=workspace.id,
            role="viewer",
        )
        workspace_id = workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        role="owner",
        workspace_role="owner",
        workspace_id=None,
        token_kind="session_cookie",
        token_workspace_ids=(),
        scopes=("mcp:read",),
    )

    result = resolve_target_actor(
        tool_name="search",
        payload={"workspace_id": str(workspace_id), "query": "needle"},
        actor=actor,
        session_factory=session_factory,
        db=_FailingWorkspacePlaneProvider(
            session_factory, unavailable_workspace_id=workspace_id
        ),
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "workspace_plane_unavailable"
