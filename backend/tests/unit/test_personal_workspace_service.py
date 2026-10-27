import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.agent import AgentPage
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.personal_workspace_service import create_personal_workspace


def _user(session, *, email: str = "owner@example.com") -> User:
    user = User(
        email=email,
        password_hash="hash",
        display_name="Owner Name",
        status="active",
    )
    session.add(user)
    session.flush()
    return user


def test_personal_workspace_is_an_ordinary_private_owned_workspace(session_factory):
    with session_factory.begin() as session:
        user = _user(session)
        workspace = create_personal_workspace(session, user=user)
        workspace_id = workspace.id
        user_id = user.id

    with session_factory() as session:
        user = session.get(User, user_id)
        workspace = session.get(Workspace, workspace_id)
        membership = session.get(WorkspaceMembership, (user_id, workspace_id))
        roots = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id.is_(None),
            )
        ).all()

        assert user is not None
        assert workspace is not None
        assert user.default_workspace_id == workspace_id
        assert workspace.title == workspace.name == "Owner Name's Personal Workspace"
        assert workspace.slug == "personal-owner"
        assert workspace.visibility == "private"
        assert workspace.join_policy == "invite_only"
        assert workspace.owner_user_id == user_id
        assert membership is not None
        assert membership.role == "owner"
        assert session.get(WorkspaceMediaWriteIntent, workspace_id).state == "open"
        # The same seed as a workspace created from the switcher.
        assert {root.section for root in roots} == {
            "agent",
            "context",
            "library",
            "ask",
        }
        assert (
            session.scalar(
                select(AgentPage.id).where(AgentPage.workspace_id == workspace_id)
            )
            is not None
        )


def test_personal_workspace_slug_numbers_email_namesakes_and_falls_back(
    session_factory,
):
    with session_factory.begin() as session:
        namesake = _user(session, email="rony@one.example")
        assert create_personal_workspace(session, user=namesake).slug == "personal-rony"

        user = _user(session, email="rony@two.example")
        user.display_name = "김민아"
        personal = create_personal_workspace(session, user=user)

        assert personal.title == personal.name == "김민아's Personal Workspace"
        assert personal.slug == "personal-rony-2"

        no_ascii = _user(session, email="김민아@three.example")
        slug = create_personal_workspace(session, user=no_ascii).slug
        assert slug.startswith("personal-")
        assert len(slug) == len("personal-") + 8
        assert slug != create_personal_workspace(
            session, user=_user(session, email="이영희@four.example")
        ).slug


def test_private_workspace_rejects_request_join_policy_at_database(session_factory):
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(
                Workspace(
                    slug="invalid-private-request",
                    title="Invalid",
                    name="Invalid",
                    visibility="private",
                    join_policy="request",
                )
            )
            session.flush()


def test_membership_role_check_rejects_unknown_role(session_factory):
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            user = _user(session, email="invalid-role@example.com")
            workspace = session.scalar(
                select(Workspace).where(Workspace.slug == "default")
            )
            assert workspace is not None
            session.add(
                WorkspaceMembership(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    role="admin",
                )
            )
            session.flush()
