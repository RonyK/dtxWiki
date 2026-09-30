"""Workspace deletion must drain control rows without losing retained data."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import (
    ContextMediaLogicalQuotaUsage,
    LibraryLogicalQuotaUsage,
    StoredBlobRow,
    User,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.mcp_chat import McpChatSession
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.rbac import get_workspace_access, grant_workspace_membership
from backend.schemas.read import WorkspaceCreateRequest
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.workspace_write import (
    WorkspaceDeleteRejectedError,
    WorkspaceWriteService,
)


def _create_deletable_workspace(session):
    default_workspace = session.scalar(
        select(Workspace).where(Workspace.slug == "default")
    )
    assert default_workspace is not None
    user = User(
        email="workspace-delete@example.com",
        password_hash="not-used",
        display_name="Workspace Delete",
        default_workspace_id=default_workspace.id,
    )
    session.add(user)
    session.flush()
    grant_workspace_membership(
        session,
        user_id=user.id,
        workspace_id=default_workspace.id,
        role="owner",
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user.id),
        user_id=user.id,
        token_kind="session",
        provenance="browser_session",
        scopes=("*",),
        workspace_role="owner",
        role="owner",
    )
    created = WorkspaceWriteService().create_workspace(
        session=session,
        request=WorkspaceCreateRequest(
            title="Deletable Workspace",
            slug="deletable-workspace",
        ),
        actor=actor,
    )
    return actor, created.id


def _configure_empty_media_scope(session, workspace_id, *, generation=0):
    intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
    assert intent is not None
    intent.state = "open"
    intent.delete_generation = generation
    session.add(
        WorkspaceMediaQuotaUsage(
            workspace_id=workspace_id,
            limit_bytes=1_000,
            charged_bytes=0,
            revision=0,
        )
    )
    for quota_kind in ("source", "derived"):
        session.add(
            LibraryLogicalQuotaUsage(
                workspace_id=workspace_id,
                quota_kind=quota_kind,
                limit_bytes=1_000,
                charged_bytes=0,
                revision=0,
            )
        )
    session.add(
        ContextMediaLogicalQuotaUsage(
            workspace_id=workspace_id,
            limit_bytes=1_000,
            charged_bytes=0,
            revision=0,
        )
    )
    session.flush()


def test_dependency_counts_includes_mcp_chats(session_factory):
    # A workspace with MCP chat sessions is NOT empty: it must be counted so the
    # delete guard blocks instead of force-deleting into an FK error.
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        session.add(
            McpChatSession(
                workspace_id=workspace.id,
                client_id=client.id,
                mcp_session_id="conv-del-test",
            )
        )
        session.flush()

        counts = WorkspaceWriteService._dependency_counts(session, workspace.id)

    assert counts.get("mcp_chats", 0) >= 1


def test_media_delete_fence_advances_once_and_is_idempotent(session_factory):
    with session_factory.begin() as session:
        _actor, workspace_id = _create_deletable_workspace(session)
        _configure_empty_media_scope(session, workspace_id, generation=4)

        first = WorkspaceWriteService._begin_media_delete(
            session=session,
            workspace_id=workspace_id,
        )
        second = WorkspaceWriteService._begin_media_delete(
            session=session,
            workspace_id=workspace_id,
        )

        assert first is second
        assert second.state == "deleting"
        assert second.delete_generation == 5


def test_delete_fails_fast_when_required_media_intent_is_missing(session_factory):
    with session_factory.begin() as session:
        actor, workspace_id = _create_deletable_workspace(session)
        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        session.delete(intent)

    with pytest.raises(
        RuntimeError,
        match="^workspace_media_intent_not_configured$",
    ):
        with session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
            )

    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None


def test_delete_workspace_drains_empty_media_control_rows(session_factory):
    with session_factory.begin() as session:
        actor, workspace_id = _create_deletable_workspace(session)
        _configure_empty_media_scope(session, workspace_id)

    with session_factory.begin() as session:
        deleted = WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )

    assert deleted == workspace_id
    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is None
        assert session.get(WorkspaceMediaWriteIntent, workspace_id) is None
        assert session.get(WorkspaceMediaQuotaUsage, workspace_id) is None
        assert session.get(ContextMediaLogicalQuotaUsage, workspace_id) is None
        assert (
            session.scalars(
                select(LibraryLogicalQuotaUsage).where(
                    LibraryLogicalQuotaUsage.workspace_id == workspace_id
                )
            ).all()
            == []
        )


def test_delete_workspace_explicitly_clears_user_workspace_pointers(session_factory):
    """Pointer cleanup must not depend on database FK cascade enforcement."""

    with session_factory.begin() as session:
        actor, workspace_id = _create_deletable_workspace(session)
        user = session.scalar(
            select(User).where(User.email == "workspace-delete@example.com")
        )
        assert user is not None
        user.default_workspace_id = workspace_id
        user.personal_workspace_id = workspace_id
        user_id = user.id

    with session_factory.begin() as session:
        deleted = WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )

    assert deleted == workspace_id
    with session_factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        assert user.default_workspace_id is None
        assert user.personal_workspace_id is None
        assert session.get(Workspace, workspace_id) is None


def test_delete_workspace_reports_retained_media_and_rolls_back_fence(
    session_factory,
):
    with session_factory.begin() as session:
        actor, workspace_id = _create_deletable_workspace(session)
        _configure_empty_media_scope(session, workspace_id)
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id,
                id=uuid4(),
                sha256="a" * 64,
                next_epoch=1,
            )
        )

    with pytest.raises(WorkspaceDeleteRejectedError) as exc_info:
        with session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
            )

    assert exc_info.value.blockers["media"] == 1
    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None
        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        assert intent.state == "open"
        assert intent.delete_generation == 0


def test_direct_bearer_actor_cannot_delete_workspace(session_factory):
    with session_factory.begin() as session:
        owner, workspace_id = _create_deletable_workspace(session)
    bearer = replace(
        owner,
        token_kind="api_token",
        provenance="personal_access_token",
    )

    with pytest.raises(RejectedByPolicyError) as exc_info:
        with session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=bearer,
            )

    assert exc_info.value.error_code == "human_session_required"
    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None


def test_direct_non_owner_cannot_delete_workspace(session_factory):
    with session_factory.begin() as session:
        _owner, workspace_id = _create_deletable_workspace(session)
        viewer = User(
            email="workspace-delete-viewer@example.com",
            password_hash="not-used",
            display_name="Workspace Delete Viewer",
        )
        session.add(viewer)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=viewer.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        viewer_actor = ActorContext(
            actor_kind="human",
            actor_id=str(viewer.id),
            user_id=viewer.id,
            token_kind="session",
            provenance="browser_session",
            workspace_role="viewer",
            role="viewer",
        )

    with pytest.raises(RejectedByPolicyError) as exc_info:
        with session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=viewer_actor,
            )

    assert exc_info.value.error_code == "workspace_role_insufficient"
    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None


def test_delete_rechecks_owner_after_an_earlier_gate(session_factory):
    """A stale route-level owner result cannot authorize later destruction."""

    with session_factory.begin() as session:
        owner, workspace_id = _create_deletable_workspace(session)

    with session_factory() as gate_session:
        access = get_workspace_access(
            gate_session,
            user_id=owner.user_id,
            workspace_id=workspace_id,
        )
        assert access is not None
        assert access.role == "owner"

    with session_factory.begin() as membership_session:
        membership = membership_session.get(
            WorkspaceMembership,
            (owner.user_id, workspace_id),
        )
        assert membership is not None
        membership.role = "viewer"

    with pytest.raises(RejectedByPolicyError) as exc_info:
        with session_factory.begin() as delete_session:
            WorkspaceWriteService().delete_workspace(
                session=delete_session,
                workspace_id=workspace_id,
                actor=owner,
            )

    assert exc_info.value.error_code == "workspace_role_insufficient"
    with session_factory() as session:
        assert session.get(Workspace, workspace_id) is not None


def test_repeated_delete_returns_not_found_instead_of_fk_error(session_factory):
    with session_factory.begin() as session:
        actor, workspace_id = _create_deletable_workspace(session)

    with session_factory.begin() as session:
        WorkspaceWriteService().delete_workspace(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )

    with pytest.raises(ReadNotFoundError):
        with session_factory.begin() as session:
            WorkspaceWriteService().delete_workspace(
                session=session,
                workspace_id=workspace_id,
                actor=actor,
            )
