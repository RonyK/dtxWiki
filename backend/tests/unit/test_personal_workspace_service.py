import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid5

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.models.agent import AgentPage
from backend.models.base import Base
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.rbac import grant_workspace_membership
from backend.services.personal_workspace_service import (
    PERSONAL_WORKSPACE_NAMESPACE,
    _personal_workspace_slug,
    ensure_personal_workspace,
)


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


def test_personal_workspace_is_ordinary_private_owner_anchor(session_factory):
    with session_factory.begin() as session:
        user = _user(session)
        workspace = ensure_personal_workspace(session, user=user)
        workspace_id = workspace.id
        user_id = user.id

    with session_factory() as session:
        user = session.get(User, user_id)
        workspace = session.get(Workspace, workspace_id)
        membership = session.get(WorkspaceMembership, (user_id, workspace_id))
        media_write_intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        roots = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id
            )
        ).all()
        policies = session.scalars(
            select(TreePolicy).where(TreePolicy.workspace_id == workspace_id)
        ).all()

        assert user is not None
        assert workspace is not None
        assert user.personal_workspace_id == workspace_id
        assert user.default_workspace_id == workspace_id
        assert workspace.title == "Owner Name's Personal Workspace"
        assert workspace.name == "Owner Name's Personal Workspace"
        assert workspace.visibility == "private"
        assert workspace.join_policy == "invite_only"
        assert workspace.owner_user_id == user_id
        assert membership is not None
        assert membership.role == "owner"
        assert media_write_intent is not None
        assert media_write_intent.state == "open"
        assert {node.section for node in roots} == {"context", "library"}
        assert {node.slug for node in roots} == {"context", "library"}
        assert {policy.section for policy in policies} == {"context", "library"}
        assert (
            session.scalar(
                select(MCPClient.id).where(MCPClient.workspace_id == workspace_id)
            )
            is None
        )
        assert (
            session.scalar(
                select(AgentPage.id).where(AgentPage.workspace_id == workspace_id)
            )
            is None
        )
        assert workspace.slug == "personal-owner-name"
        assert "example" not in workspace.slug


def test_personal_workspace_ensure_is_idempotent(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="idempotent@example.com")
        first = ensure_personal_workspace(session, user=user)
        second = ensure_personal_workspace(session, user=user)
        assert second.id == first.id
        workspace_id = first.id

    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(WorkspaceMembership.user_id == user.id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceTreeNode)
                .where(WorkspaceTreeNode.workspace_id == workspace_id)
            )
            == 2
        )


def test_personal_workspace_reensure_preserves_owner_customization(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="customized@example.com")
        workspace = ensure_personal_workspace(session, user=user)
        workspace.title = "Research Commons"
        workspace.name = "Research Commons"
        original_slug = workspace.slug
        user.display_name = "Renamed Owner"
        workspace.visibility = "public"
        workspace.join_policy = "request"
        session.flush()

        ensured = ensure_personal_workspace(session, user=user)

        assert ensured.id == workspace.id
        assert ensured.title == "Research Commons"
        assert ensured.name == "Research Commons"
        assert ensured.slug == original_slug
        assert ensured.visibility == "public"
        assert ensured.join_policy == "request"
        assert ensured.owner_user_id == user.id


def test_concurrent_personal_ensure_converges_on_one_anchor(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'personal-concurrent.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = _user(session, email="concurrent@example.com")
        user_id = user.id

    barrier = Barrier(2, timeout=30)

    def provision() -> object:
        with factory.begin() as session:
            user = session.get(User, user_id)
            assert user is not None
            barrier.wait()
            return ensure_personal_workspace(session, user=user).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        workspace_ids = list(executor.map(lambda _: provision(), range(2)))

    with factory() as session:
        persisted = session.get(User, user_id)
        assert persisted is not None
        assert workspace_ids == [
            persisted.personal_workspace_id,
            persisted.personal_workspace_id,
        ]
        assert (
            session.scalar(
                select(func.count())
                .select_from(Workspace)
                .where(Workspace.owner_user_id == user_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(WorkspaceMembership.user_id == user_id)
            )
            == 1
        )
    engine.dispose()


def test_personal_workspace_slug_collision_uses_number_without_changing_id(
    session_factory,
):
    with session_factory.begin() as session:
        namesake = _user(session, email="namesake@example.com")
        namesake.display_name = "김민아"
        occupied = ensure_personal_workspace(session, user=namesake)
        assert occupied.slug == "personal-user"

        user = _user(session, email="collision@example.com")
        user.display_name = "김민아"
        first_candidate_id = uuid5(PERSONAL_WORKSPACE_NAMESPACE, str(user.id))

        personal = ensure_personal_workspace(session, user=user)

        assert personal.id == first_candidate_id
        assert personal.title == "김민아's Personal Workspace"
        assert personal.name == personal.title
        assert personal.slug == "personal-user-2"
        assert "collision" not in personal.slug

        # Freeing the base must not create a second workspace when recovering
        # an existing numbered candidate whose anchor pointer was lost.
        user.personal_workspace_id = None
        occupied.slug = "renamed-workspace"
        session.flush()

        recovered = ensure_personal_workspace(session, user=user)

        assert recovered.id == first_candidate_id
        assert recovered.slug == "personal-user-2"


def test_personal_workspace_slug_bounds_unicode_lowercase_expansion():
    slug = _personal_workspace_slug("İ" * 32)

    assert len(slug) <= 80
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
    assert slug.startswith("personal-i-i-")
    assert len(slug) <= len("personal-") + 32


def test_personal_workspace_recovers_legacy_candidate_without_renaming(session_factory):
    with session_factory.begin() as session:
        user = _user(session)
        workspace_id = uuid5(PERSONAL_WORKSPACE_NAMESPACE, str(user.id))
        legacy = Workspace(
            id=workspace_id,
            slug=f"personal-{workspace_id.hex}",
            title="Personal Workspace",
            name="Personal Workspace",
            visibility="public",
            owner_user_id=user.id,
        )
        session.add(legacy)
        session.flush()

        personal = ensure_personal_workspace(session, user=user)

        assert personal.id == workspace_id
        assert user.personal_workspace_id == workspace_id
        assert personal.slug == f"personal-{workspace_id.hex}"
        assert personal.title == personal.name == "Personal Workspace"
        assert personal.visibility == "public"


def test_personal_workspace_preserves_accessible_explicit_default(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="explicit-default@example.com")
        existing = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert existing is not None
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=existing.id,
            role="viewer",
        )
        user.default_workspace_id = existing.id

        personal = ensure_personal_workspace(session, user=user)

        assert user.default_workspace_id == existing.id
        assert user.personal_workspace_id == personal.id


def test_personal_workspace_repairs_inaccessible_default(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="repair-default@example.com")
        inaccessible = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert inaccessible is not None
        user.default_workspace_id = inaccessible.id

        personal = ensure_personal_workspace(session, user=user)

        assert user.default_workspace_id == personal.id


def test_deactivation_preserves_anchor_and_reactivation_reuses_it(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="reactivate@example.com")
        personal = ensure_personal_workspace(session, user=user)
        personal_id = personal.id
        user.status = "suspended"
        assert ensure_personal_workspace(session, user=user).id == personal_id
        user.status = "active"
        assert ensure_personal_workspace(session, user=user).id == personal_id

    with session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "reactivate@example.com")
        )
        assert user is not None
        assert user.personal_workspace_id == personal_id
        assert user.default_workspace_id == personal_id


def test_suspended_user_without_anchor_receives_recovery_invariants(session_factory):
    with session_factory.begin() as session:
        user = _user(session, email="suspended-anchor@example.com")
        user.status = "suspended"

        personal = ensure_personal_workspace(session, user=user)
        user_id = user.id
        personal_id = personal.id

        assert user.status == "suspended"
        assert user.personal_workspace_id == personal_id
        assert user.default_workspace_id == personal_id

    with session_factory() as session:
        user = session.get(User, user_id)
        membership = session.get(WorkspaceMembership, (user_id, personal_id))
        roots = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == personal_id
            )
        ).all()

        assert user is not None
        assert user.status == "suspended"
        assert membership is not None
        assert membership.role == "owner"
        assert {root.section for root in roots} == {"context", "library"}


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
