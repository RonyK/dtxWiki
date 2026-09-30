from uuid import uuid4

import pytest

from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.policy.workspace_resolution import (
    WorkspaceAmbiguityError,
    resolve_and_check,
    resolve_workspace_for_actor,
)

pytestmark = pytest.mark.unit


def _user(session, email="workspace-resolution@example.com"):
    user = User(email=email, password_hash="hash", display_name="Test User")
    session.add(user)
    session.flush()
    return user


def _workspace(session, slug: str):
    workspace = Workspace(slug=slug, title=slug, name=slug, goal="phase1")
    session.add(workspace)
    session.flush()
    return workspace


def _actor(user_id, **kwargs):
    return ActorContext(
        actor_kind=kwargs.pop("actor_kind", "human"),
        actor_id=str(user_id or kwargs.get("actor_id", "actor")),
        user_id=user_id,
        token_kind=kwargs.pop("token_kind", "session"),
        **kwargs,
    )


def test_priority_order(session_factory):
    with session_factory.begin() as session:
        user = _user(session)
        member_ws = _workspace(session, "phase1-member")
        explicit_ws = _workspace(session, "phase1-explicit")
        token_ws = _workspace(session, "phase1-token")
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=member_ws.id, role="viewer"
        )

        assert (
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(user.id, token_workspace_ids=(token_ws.id,)),
                requested_workspace_id=None,
            ).source
            == "token_claim"
        )
        assert (
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(user.id),
                requested_workspace_id=explicit_ws.id,
            ).source
            == "explicit"
        )
        assert (
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(user.id),
                requested_workspace_id=None,
            ).source
            == "membership_single"
        )


def test_ambiguity_raises(session_factory):
    with session_factory.begin() as session:
        user = _user(session, "ambiguous@example.com")
        ws_a = _workspace(session, "phase1-amb-a")
        ws_b = _workspace(session, "phase1-amb-b")
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=ws_a.id, role="viewer"
        )
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=ws_b.id, role="viewer"
        )
        # Sprint_5 Phase_0~2 fix-round (TEST-3): pin the specific failure mode
        # ('ambiguous'), not the bare 'workspace' substring which would also
        # match a 'workspace not found' regression accidentally routed to
        # this branch.
        with pytest.raises(WorkspaceAmbiguityError, match=r"picker"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(user.id),
                requested_workspace_id=None,
            )


def test_user_default_then_personal_precede_membership_picker(session_factory):
    with session_factory.begin() as session:
        user = _user(session, "pointer-priority@example.com")
        default_ws = _workspace(session, "pointer-default")
        personal_ws = _workspace(session, "pointer-personal")
        other_ws = _workspace(session, "pointer-other")
        for workspace in (default_ws, personal_ws, other_ws):
            grant_workspace_membership(
                session,
                user_id=user.id,
                workspace_id=workspace.id,
                role="viewer",
            )
        user.default_workspace_id = default_ws.id
        user.personal_workspace_id = personal_ws.id

        resolved = resolve_workspace_for_actor(
            session=session,
            actor=_actor(user.id),
            requested_workspace_id=None,
        )
        assert resolved.workspace_id == default_ws.id
        assert resolved.source == "user_default"

        default_membership = session.get(
            WorkspaceMembership,
            (user.id, default_ws.id),
        )
        assert default_membership is not None
        session.delete(default_membership)
        session.flush()

        resolved = resolve_workspace_for_actor(
            session=session,
            actor=_actor(user.id),
            requested_workspace_id=None,
        )
        assert resolved.workspace_id == personal_ws.id
        assert resolved.source == "personal"
        assert user.default_workspace_id == personal_ws.id

        user_id = user.id
        personal_workspace_id = personal_ws.id

    with session_factory() as session:
        persisted_user = session.get(User, user_id)
        assert persisted_user is not None
        assert persisted_user.default_workspace_id == personal_workspace_id


def test_stale_personal_repair_does_not_overwrite_explicit_default_selection(
    session_factory,
):
    with session_factory.begin() as session:
        user = _user(session, "repair-race@example.com")
        personal_ws = _workspace(session, "repair-race-personal")
        explicit_ws = _workspace(session, "repair-race-explicit")
        for workspace in (personal_ws, explicit_ws):
            grant_workspace_membership(
                session,
                user_id=user.id,
                workspace_id=workspace.id,
                role="viewer",
            )
        user.personal_workspace_id = personal_ws.id
        user.default_workspace_id = None
        user_id = user.id
        explicit_workspace_id = explicit_ws.id

    # Keep an attached, deliberately stale User identity across transactions:
    # this is the state a request that read the null pointer just before the
    # explicit-selection transaction committed would carry into its repair.
    stale_session = session_factory()
    try:
        stale_user = stale_session.get(User, user_id)
        assert stale_user is not None
        assert stale_user.default_workspace_id is None
        stale_session.commit()

        with session_factory.begin() as winner_session:
            winner = winner_session.get(User, user_id)
            assert winner is not None
            winner.default_workspace_id = explicit_workspace_id

        with stale_session.begin():
            resolved = resolve_workspace_for_actor(
                session=stale_session,
                actor=_actor(user_id),
                requested_workspace_id=None,
            )
            assert resolved.workspace_id == explicit_workspace_id
            assert resolved.source == "user_default"
    finally:
        stale_session.close()

    with session_factory() as session:
        persisted_user = session.get(User, user_id)
        assert persisted_user is not None
        assert persisted_user.default_workspace_id == explicit_workspace_id


def test_user_without_membership_requires_workspace_discovery(session_factory):
    with session_factory.begin() as session:
        user = _user(session, "recovery@example.com")

        with pytest.raises(WorkspaceAmbiguityError, match=r"discovery"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(user.id),
                requested_workspace_id=None,
            )


def test_token_claim_overrides_path(session_factory):
    with session_factory.begin() as session:
        ws_token = _workspace(session, "phase1-token-only")
        ws_path = _workspace(session, "phase1-token-denied")
        # TEST-3: pin 'token' rather than the bare 'workspace' so that an
        # unrelated workspace-related error keyword cannot silently pass.
        with pytest.raises(WorkspaceAmbiguityError, match=r"token"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(
                    uuid4(), token_workspace_ids=(ws_token.id,), token_kind="oauth_jwt"
                ),
                requested_workspace_id=ws_path.id,
            )


def test_token_claim_subset_with_explicit(session_factory):
    with session_factory.begin() as session:
        ws_a = _workspace(session, "phase1-claim-a")
        ws_b = _workspace(session, "phase1-claim-b")
        resolved = resolve_workspace_for_actor(
            session=session,
            actor=_actor(
                uuid4(), token_workspace_ids=(ws_a.id, ws_b.id), token_kind="oauth_jwt"
            ),
            requested_workspace_id=ws_b.id,
        )
        assert resolved.workspace_id == ws_b.id
        assert resolved.source == "explicit"


def test_direct_mcp_client_actor_is_rejected_even_with_explicit_workspace(
    session_factory,
):
    with session_factory.begin() as session:
        own_ws = _workspace(session, "phase1-mcp-own")
        other_ws = _workspace(session, "phase1-mcp-other")
        with pytest.raises(WorkspaceAmbiguityError, match="not supported"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(None, actor_kind="mcp_client", workspace_id=own_ws.id),
                requested_workspace_id=other_ws.id,
            )


def test_system_actor_without_explicit_workspace_fails_closed(session_factory):
    with session_factory.begin() as session:
        with pytest.raises(WorkspaceAmbiguityError, match=r"explicit"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(None, actor_kind="system"),
                requested_workspace_id=None,
            )


def test_non_user_actor_without_explicit_workspace_fails_closed(session_factory):
    with session_factory.begin() as session:
        with pytest.raises(WorkspaceAmbiguityError, match=r"explicit"):
            resolve_workspace_for_actor(
                session=session,
                actor=_actor(None),
                requested_workspace_id=None,
            )


def test_system_actor_accepts_explicit_workspace(session_factory):
    with session_factory.begin() as session:
        workspace = _workspace(session, "system-explicit")
        resolved = resolve_workspace_for_actor(
            session=session,
            actor=_actor(None, actor_kind="system"),
            requested_workspace_id=workspace.id,
        )

        assert resolved.workspace_id == workspace.id
        assert resolved.source == "explicit"


# ---------------------------------------------------------------------------
# Sprint_5 Phase_1 §11.1 forward helper — ``resolve_and_check``
# ---------------------------------------------------------------------------


def test_resolve_and_check_rejects_direct_mcp_client_actor(session_factory):
    """The combined policy boundary also rejects retired direct actors."""
    with session_factory.begin() as session:
        own_ws = _workspace(session, "phase1-rc-own")
        actor = _actor(None, actor_kind="mcp_client", workspace_id=own_ws.id)
        with pytest.raises(WorkspaceAmbiguityError, match="not supported"):
            resolve_and_check(
                session=session,
                actor=actor,
                action="read",
            )


def test_resolve_and_check_honors_explicit_requested_workspace(session_factory):
    """MCP tool arguments can disambiguate a multi-workspace user."""
    with session_factory.begin() as session:
        user = _user(session, "rc-explicit@example.com")
        ws_a = _workspace(session, "phase1-rc-explicit-a")
        ws_b = _workspace(session, "phase1-rc-explicit-b")
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=ws_a.id, role="viewer"
        )
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=ws_b.id, role="viewer"
        )

        result = resolve_and_check(
            session=session,
            actor=_actor(user.id),
            action="read",
            requested_workspace_id=ws_b.id,
        )

        assert result.workspace_id == ws_b.id
        assert result.source == "explicit"


def test_resolve_and_check_raises_permission_error_on_workspace_mismatch(
    session_factory,
):
    """A workspace-less retired direct actor fails closed consistently."""
    with session_factory.begin() as session:
        # Force ambiguity by leaving workspace_id None on the mcp_client actor.
        actor = _actor(None, actor_kind="mcp_client", workspace_id=None)
        with pytest.raises(WorkspaceAmbiguityError, match=r"not supported"):
            resolve_and_check(
                session=session,
                actor=actor,
                action="read",
            )


def test_resolve_and_check_raises_permission_error_when_role_insufficient(
    session_factory,
):
    """Phase_2 §3.10.4: helper surfaces ``PermissionError`` when the actor's
    role is below the action requirement.

    Pins keyword ``workspace_role_insufficient`` per A6.
    """
    with session_factory.begin() as session:
        user = _user(session, "rc-insufficient@example.com")
        ws = _workspace(session, "phase1-rc-viewer")
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=ws.id, role="viewer"
        )
        actor = _actor(user.id, scopes=("mcp:write",))
        # action="write" requires editor; viewer role rejects.
        with pytest.raises(PermissionError, match=r"workspace_role_insufficient"):
            resolve_and_check(
                session=session,
                actor=actor,
                action="write",
            )
