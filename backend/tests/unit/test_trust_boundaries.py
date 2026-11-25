from uuid import uuid4

import pytest

from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.policy.trust_boundaries import (
    actor_kind_for_check,
    actor_with_target_workspace_scope,
    check_actor_can_access,
    check_actor_scope,
    check_restricted_library_allowed,
)
from backend.schemas.common import ERROR_CODES

pytestmark = pytest.mark.unit


def _seed_user_workspace(session, *, role="owner"):
    user = User(
        email=f"{uuid4()}@example.com", password_hash="hash", display_name="User"
    )
    workspace = Workspace(
        slug=f"phase1-{uuid4().hex[:8]}", title="Phase1", name="Phase1", goal="phase1"
    )
    session.add_all([user, workspace])
    session.flush()
    grant_workspace_membership(
        session, user_id=user.id, workspace_id=workspace.id, role=role
    )
    return user, workspace


def _actor(user_id=None, **kwargs):
    return ActorContext(
        actor_kind=kwargs.pop("actor_kind", "human"),
        actor_id=str(user_id or uuid4()),
        user_id=user_id,
        token_kind=kwargs.pop("token_kind", "session"),
        scopes=kwargs.pop("scopes", ()),
        workspace_id=kwargs.pop("workspace_id", None),
        token_workspace_ids=kwargs.pop("token_workspace_ids", ()),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("token_kind", "scopes", "role", "action", "allowed"),
    [
        ("session", (), "viewer", "read", True),
        ("session", (), "viewer", "write", False),
        ("session", (), "editor", "write", True),
        ("session", (), "editor", "admin", False),
        ("session", (), "owner", "admin", True),
        ("api_token", ("mcp:read",), "viewer", "read", True),
        ("api_token", ("mcp:read",), "editor", "write", False),
        ("api_token", ("mcp:write",), "editor", "write", True),
        ("api_token", ("mcp:agent",), "editor", "write", True),
        ("api_token", ("mcp:write",), "owner", "admin", True),
        ("oauth_jwt", ("mcp:read",), "viewer", "read", True),
        ("oauth_jwt", ("mcp:read",), "editor", "write", False),
        ("oauth_jwt", ("mcp:write",), "editor", "write", True),
        ("oauth_jwt", ("mcp:agent",), "owner", "admin", False),
        ("oauth_jwt", ("mcp:write",), "owner", "admin", True),
    ],
)
def test_matrix_all_cells(session_factory, token_kind, scopes, role, action, allowed):
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role=role)
        actor = _actor(
            user.id,
            token_kind=token_kind,
            scopes=scopes,
            # D-01: a real api_token/oauth_jwt actor's workspace_id IS the
            # token's one bound workspace (see backend/policy/auth.py
            # _actor_from_user call sites) -- token_workspace_ids mirrors it
            # as a singleton. Only a session-cookie actor legitimately
            # carries no token workspace claim at all.
            workspace_id=(
                workspace.id if token_kind in {"oauth_jwt", "api_token"} else None
            ),
            token_workspace_ids=(
                (workspace.id,) if token_kind in {"oauth_jwt", "api_token"} else ()
            ),
        )
        if allowed:
            check_actor_can_access(actor, workspace.id, action, session=session)
        else:
            with pytest.raises(PermissionError) as exc:
                check_actor_can_access(actor, workspace.id, action, session=session)
            assert any(
                keyword in str(exc.value) for keyword in ("workspace", "scope", "role")
            )


# --- Sprint_5 Phase_2 fix-round-3 (Bot-W1) ------------------------------------
# The original ``test_matrix_all_cells`` covers 3 token_kind-based actor kinds
# (user_session/pat/oauth_jwt) × 5 (role, scope, action) cells. The Phase_1
# plan requires 5 actor_kinds × 3 actions of guaranteed coverage so the trust
# matrix is auditable without grepping for separate one-off tests.
#
# This block pins system's global access. Direct mcp_client credentials are
# covered separately as an unsupported runtime identity.
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor_kind", "same_workspace", "action", "allowed"),
    [
        # ``system`` — all 3 actions allowed in the own-workspace direction
        # (3 cells).
        ("system", True, "read", True),
        ("system", True, "write", True),
        ("system", True, "admin", True),
        # Sprint_5 Phase_2 fix-round-4 (TEST-FR3-2): ``system`` — all 3 actions
        # also allowed against a foreign workspace. The system actor has global
        # workspace access by design (worker entrypoints, migration scripts);
        # this row pins the "all-workspace allow" semantic that was previously
        # only asserted via the code's short-circuit. Together with the 3
        # own-workspace cells above this completes the system row of the 5×3
        # actor/action matrix (Phase_1 plan §315-328).
        ("system", False, "read", True),
        ("system", False, "write", True),
        ("system", False, "admin", True),
    ],
)
def test_matrix_actor_kind_dispatch_cells(
    session_factory, actor_kind, same_workspace, action, allowed
):
    """System actors retain deliberate all-workspace access."""
    with session_factory.begin() as session:
        workspace = Workspace(
            slug=f"phase1-w1-{uuid4().hex[:8]}",
            title="W1",
            name="W1",
            goal="phase1",
        )
        session.add(workspace)
        session.flush()
        ws_for_actor = workspace.id if same_workspace else uuid4()
        actor = _actor(None, actor_kind=actor_kind, workspace_id=ws_for_actor)
        if allowed:
            check_actor_can_access(actor, workspace.id, action, session=session)
        else:
            with pytest.raises(PermissionError) as exc:
                check_actor_can_access(actor, workspace.id, action, session=session)
            assert any(
                keyword in str(exc.value)
                for keyword in ("mcp_client", "workspace", "scope", "role")
            )


@pytest.mark.parametrize("action", ["read", "write", "admin"])
def test_direct_mcp_client_runtime_actor_is_rejected(session_factory, action):
    with session_factory.begin() as session:
        ws = Workspace(
            slug="phase1-mcp-denied", title="Denied", name="Denied", goal="phase1"
        )
        session.add(ws)
        session.flush()
        actor = _actor(None, actor_kind="mcp_client", workspace_id=ws.id)
        with pytest.raises(PermissionError, match="unsupported_actor_kind"):
            check_actor_can_access(actor, ws.id, action, session=session)


def test_restricted_library_denied():
    with pytest.raises(PermissionError, match="restricted"):
        check_restricted_library_allowed(
            _actor(uuid4(), workspace_role="viewer"), restricted=True
        )


def test_restricted_library_denied_carries_typed_error_code():
    from backend.policy.trust_boundaries import RestrictedLibraryDeniedError

    with pytest.raises(RestrictedLibraryDeniedError) as excinfo:
        check_restricted_library_allowed(
            _actor(uuid4(), workspace_role="viewer"), restricted=True
        )
    assert excinfo.value.error_code == "restricted_library_denied"
    assert excinfo.value.error_code in ERROR_CODES


def test_restricted_library_allowed_for_owner_role():
    # Unified predicate: REST and MCP now share the owner/admin/scope rule.
    check_restricted_library_allowed(
        _actor(uuid4(), workspace_role="owner"), restricted=True
    )


def test_restricted_library_allowed_for_restricted_scope():
    check_restricted_library_allowed(
        _actor(uuid4(), scopes=("library:restricted-read",)), restricted=True
    )


def test_restricted_library_allowed_for_system_actor():
    check_restricted_library_allowed(
        _actor(None, actor_kind="system"), restricted=True
    )


def test_restricted_library_unrestricted_target_always_allowed():
    check_restricted_library_allowed(_actor(uuid4()), restricted=False)


def test_restricted_library_role_alias_normalization():
    # L3: the restricted predicate resolves role aliases the same way the draft
    # predicate does (admin->owner; member/reviewer->editor). Restricted requires
    # owner-level, so editor-aliases are denied; only owner/admin/scope/system pass.
    from backend.policy.trust_boundaries import actor_can_read_restricted_library

    allowed = {"owner": True, "admin": True}
    denied = {"editor": False, "member": False, "reviewer": False,
              "viewer": False, "none": False}
    for role, expected in {**allowed, **denied}.items():
        assert (
            actor_can_read_restricted_library(_actor(uuid4(), workspace_role=role))
            is expected
        ), role


def test_actor_can_read_drafts():
    from backend.policy.trust_boundaries import actor_can_read_drafts

    assert actor_can_read_drafts(_actor(uuid4(), workspace_role="editor")) is True
    assert actor_can_read_drafts(_actor(uuid4(), workspace_role="owner")) is True
    assert actor_can_read_drafts(_actor(uuid4(), workspace_role="admin")) is True
    assert actor_can_read_drafts(_actor(uuid4(), workspace_role="viewer")) is False
    assert actor_can_read_drafts(_actor(uuid4(), workspace_role="none")) is False
    assert actor_can_read_drafts(_actor(None, actor_kind="system")) is True
    assert actor_can_read_drafts(None) is False


def test_admin_only_for_admin_role(session_factory):
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="editor")
        actor = _actor(
            user.id,
            token_kind="api_token",
            scopes=("mcp:write",),
            workspace_id=workspace.id,
            token_workspace_ids=(workspace.id,),
        )
        with pytest.raises(PermissionError, match="role"):
            check_actor_can_access(actor, workspace.id, "admin", session=session)


def test_oauth_token_claim_binding(session_factory):
    """D-01: an oauth_jwt actor's workspace_id IS its one bound workspace --
    a token bound to a DIFFERENT workspace than the one being accessed is
    denied outright, no matter what token_workspace_ids also says."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="owner")
        other_workspace_id = uuid4()
        actor = _actor(
            user.id,
            token_kind="oauth_jwt",
            scopes=("mcp:read",),
            workspace_id=other_workspace_id,
            token_workspace_ids=(other_workspace_id,),
        )
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(actor, workspace.id, "read", session=session)


def test_oauth_token_empty_claim_denied_not_fallthrough(session_factory):
    """D-01: an oauth_jwt actor with no bound workspace (workspace_id=None --
    unreachable in practice since verify_access_token hard-rejects a token
    whose workspace claim does not parse, but a defensive fail-closed check
    all the same) must deny outright, not fall through to a membership check
    -- otherwise the token's owning user's real WorkspaceMembership row would
    silently grant access the token itself never claimed."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="owner")
        actor = _actor(
            user.id,
            token_kind="oauth_jwt",
            scopes=("mcp:read",),
            workspace_id=None,
            token_workspace_ids=(),
        )
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(actor, workspace.id, "read", session=session)


def test_pat_empty_claim_denied_not_fallthrough(session_factory):
    """D-01: a PAT with no bound workspace (workspace_id=None -- unreachable
    in practice since ApiTokenActor.workspace_id is a required field, but a
    defensive fail-closed check all the same) must deny outright rather than
    falling through to membership."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="owner")
        actor = _actor(
            user.id,
            token_kind="api_token",
            scopes=("mcp:read",),
            workspace_id=None,
            token_workspace_ids=(),
        )
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(actor, workspace.id, "read", session=session)


def test_session_actor_empty_claim_falls_through_to_membership(session_factory):
    """Session-cookie actors carry no token workspace claim at all -- an
    empty tuple here means "no token-level restriction", not "zero grant",
    and must keep falling through to the real-time membership check."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="viewer")
        actor = _actor(user.id, token_kind="session", token_workspace_ids=())
        check_actor_can_access(actor, workspace.id, "read", session=session)
        with pytest.raises(PermissionError, match="role"):
            check_actor_can_access(actor, workspace.id, "write", session=session)


def _seed_oauth_client(session, *, client_id: str, scope: str = "mcp:read mcp:write"):
    from backend.models.oauth import OAuthClient

    session.add(
        OAuthClient(
            id=client_id,
            client_name="Step-up test client",
            redirect_uris=["http://127.0.0.1:53682/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope=scope,
        )
    )


def _seed_oauth_consent(session, *, user_id, client_id: str, workspace_id, scope: str):
    from backend.models.oauth import OAuthConsent

    session.add(
        OAuthConsent(
            user_id=user_id,
            client_id=client_id,
            workspace_id=workspace_id,
            scope=scope,
            desired_scopes=scope.split(),
        )
    )


def test_oauth_consent_for_a_different_workspace_no_longer_grants_step_up(
    session_factory,
):
    """D-01 removed the step-up-via-live-consent mechanism outright: an
    oauth_jwt actor's own token_workspace_id claim is the sole source of
    truth now. A live OAuthConsent row for a workspace OTHER than the one
    the token is bound to must NOT let the same token reach it -- reaching
    a second workspace requires a separate credential (a token from its own
    /oauth/authorize round), never a wider grant on this one."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="editor")
        client_id = f"client-{uuid4().hex[:8]}"
        _seed_oauth_client(session, client_id=client_id)
        _seed_oauth_consent(
            session,
            user_id=user.id,
            client_id=client_id,
            workspace_id=workspace.id,
            scope="mcp:read mcp:write",
        )
        session.flush()
        other_workspace_id = uuid4()
        actor = _actor(
            user.id,
            token_kind="oauth_jwt",
            client_id=client_id,
            scopes=("mcp:read",),
            workspace_id=other_workspace_id,
            token_workspace_ids=(other_workspace_id,),
        )
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            check_actor_can_access(actor, workspace.id, "read", session=session)


def test_oauth_live_consent_narrows_scope_for_its_own_bound_workspace(
    session_factory,
):
    """``actor_with_target_workspace_scope`` (not the step-up mechanism,
    which is gone) is what still consults live consent: a token's OWN bound
    workspace narrows to the LIVE OAuthConsent scope, which can be tighter
    than what the token itself claims if a later /oauth/authorize round
    reduced the grant since the token was issued."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="editor")
        client_id = f"client-{uuid4().hex[:8]}"
        _seed_oauth_client(session, client_id=client_id)
        _seed_oauth_consent(
            session,
            user_id=user.id,
            client_id=client_id,
            workspace_id=workspace.id,
            scope="mcp:read",
        )
        session.flush()
        actor = _actor(
            user.id,
            token_kind="oauth_jwt",
            client_id=client_id,
            scopes=("mcp:read", "mcp:write"),
            workspace_id=workspace.id,
            token_workspace_ids=(workspace.id,),
        )
        scoped = actor_with_target_workspace_scope(
            actor, workspace.id, session=session
        )
        assert scoped.scopes == ("mcp:read",)


def test_oauth_consent_for_a_different_client_does_not_grant_this_clients_token(
    session_factory,
):
    """Consent is scoped per (user, client, workspace) -- a grant recorded
    for one OAuth client must not narrow-and-allow a DIFFERENT client's
    token for the same user/workspace; it must deny outright."""
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="owner")
        granted_client_id = f"client-{uuid4().hex[:8]}"
        calling_client_id = f"client-{uuid4().hex[:8]}"
        _seed_oauth_client(session, client_id=granted_client_id)
        _seed_oauth_client(session, client_id=calling_client_id)
        _seed_oauth_consent(
            session,
            user_id=user.id,
            client_id=granted_client_id,
            workspace_id=workspace.id,
            scope="mcp:read mcp:write",
        )
        session.flush()
        actor = _actor(
            user.id,
            token_kind="oauth_jwt",
            client_id=calling_client_id,
            scopes=("mcp:read",),
            workspace_id=workspace.id,
            token_workspace_ids=(workspace.id,),
        )
        with pytest.raises(PermissionError, match="token_workspace_denied"):
            actor_with_target_workspace_scope(actor, workspace.id, session=session)


def test_pat_scope_is_independent_of_any_oauth_consent_row(session_factory):
    """A PAT never consults OAuthConsent -- its effective scope for its own
    bound workspace is its token ceiling intersected with its own live PAT
    workspace grant, whether or not an unrelated OAuthConsent row exists for
    the same user/workspace under some OAuth client."""
    with session_factory.begin() as session:
        from backend.models.api_token import ApiToken, ApiTokenWorkspace

        user, workspace = _seed_user_workspace(session, role="owner")
        client_id = f"client-{uuid4().hex[:8]}"
        _seed_oauth_client(session, client_id=client_id)
        _seed_oauth_consent(
            session,
            user_id=user.id,
            client_id=client_id,
            workspace_id=workspace.id,
            scope="mcp:read",
        )
        session.flush()
        token = ApiToken(
            user_id=user.id,
            name="trust-boundary-pat",
            token_lookup_hash=f"trust-boundary-{uuid4()}",
            hashed_token="unused",
            scopes=["mcp:read", "mcp:write"],
        )
        session.add(token)
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=token.id,
                workspace_id=workspace.id,
                desired_scopes=["mcp:read", "mcp:write"],
                revision=1,
                sync_state="synced",
            )
        )
        session.flush()
        actor = _actor(
            user.id,
            token_kind="api_token",
            token_id=token.id,
            client_id=client_id,
            scopes=("mcp:read", "mcp:write"),
            workspace_id=workspace.id,
            token_workspace_ids=(workspace.id,),
        )
        scoped = actor_with_target_workspace_scope(
            actor, workspace.id, session=session
        )
        assert scoped.scopes == ("mcp:read", "mcp:write")


def test_system_actor_allows_all(session_factory):
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="phase1-system", title="System", name="System", goal="phase1"
        )
        session.add(workspace)
        session.flush()
        actor = _actor(None, actor_kind="system")
        for action in ("read", "write", "admin"):
            check_actor_can_access(actor, workspace.id, action, session=session)


def test_pat_scope_required_for_write(session_factory):
    with session_factory.begin() as session:
        user, workspace = _seed_user_workspace(session, role="editor")
        actor = _actor(
            user.id,
            token_kind="api_token",
            scopes=("mcp:read",),
            workspace_id=workspace.id,
            token_workspace_ids=(workspace.id,),
        )
        with pytest.raises(PermissionError, match="scope"):
            check_actor_can_access(actor, workspace.id, "write", session=session)


def test_worker_actor_kind_denied_via_fallthrough():
    """Bot-B3 fix-round 3: actor_kind='worker' must NOT silently map to system.

    Worker runtime is expected to construct ``ActorContext(actor_kind='system',
    ...)`` itself; rewriting the worker label to system was a fail-open path.
    """
    with pytest.raises(PermissionError, match="unknown actor kind"):
        actor_kind_for_check(_actor(None, actor_kind="worker"))


def test_unknown_actor_kind_denied_via_fallthrough():
    """Bot-B3 fix-round 3: any unknown actor_kind must fail closed."""
    with pytest.raises(PermissionError, match="unknown actor kind") as excinfo:
        actor_kind_for_check(_actor(None, actor_kind="agent"))
    error_code = str(excinfo.value).split(":", 1)[0]
    assert error_code == "unknown_actor_kind"
    assert error_code in ERROR_CODES


def test_system_actor_kind_still_allows_admin(session_factory):
    """Bot-B3 fix-round 3 positive control (ADR A6 pair for the two negatives
    above): actor_kind='system' continues to allow all actions, so the
    fail-closed change is scoped to genuinely-unknown kinds only.
    """
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="phase1-b3-system",
            title="B3 System",
            name="B3 System",
            goal="phase1",
        )
        session.add(workspace)
        session.flush()
        actor = _actor(None, actor_kind="system")
        assert actor_kind_for_check(actor) == "system"
        for action in ("read", "write", "admin"):
            check_actor_can_access(actor, workspace.id, action, session=session)


def test_check_actor_scope_denies_a_workspace_the_token_is_not_bound_to():
    """D-01: the sensitive-body scope gate must key on the workspace being
    read -- a token's scope is only ever valid for the ONE workspace it is
    bound to, so an explicit ``resource_workspace_id`` naming a DIFFERENT
    workspace must deny outright even though the token's own scope would
    otherwise be sufficient there. An MCP call rebinds ``actor.workspace_id``
    to the resolved target workspace (``resolve_target_actor``), so that is
    the right implicit resource; a REST path workspace is passed explicitly
    because the REST actor is never rebound.
    """
    workspace_a, workspace_b = uuid4(), uuid4()
    actor = _actor(
        uuid4(),
        token_kind="api_token",
        scopes=("mcp:write",),
        workspace_id=workspace_a,
        token_workspace_ids=(workspace_a,),
    )

    check_actor_scope(actor, "admin")
    check_actor_scope(actor, "admin", resource_workspace_id=workspace_a)
    with pytest.raises(PermissionError, match="token_workspace_denied"):
        check_actor_scope(actor, "admin", resource_workspace_id=workspace_b)
