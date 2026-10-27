from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select

from backend.app.db import make_engine, make_session_factory
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.personal_workspace_service import create_personal_workspace
from backend.services.workspace_membership_service import (
    WorkspaceLastOwnerLockedError,
    WorkspaceMembershipService,
)


class _FailingAuditService:
    def emit(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")


def _create_user(session, email: str) -> User:
    user = User(
        email=email,
        password_hash="unused-test-hash",
        display_name=email.split("@", 1)[0],
        status="active",
    )
    session.add(user)
    session.flush()
    create_personal_workspace(session, user=user)
    return user


def _create_owned_workspace(session, owner: User, slug: str) -> Workspace:
    workspace = Workspace(
        slug=slug,
        title=slug.replace("-", " ").title(),
        name=slug.replace("-", " ").title(),
        created_by_actor_id=owner.id,
        owner_actor_id=owner.id,
        owner_user_id=owner.id,
    )
    session.add(workspace)
    session.flush()
    session.add(
        WorkspaceMembership(
            user_id=owner.id,
            workspace_id=workspace.id,
            role="owner",
        )
    )
    return workspace


def test_non_default_self_removal_preserves_and_returns_accessible_default(
    session_factory,
):
    with session_factory.begin() as session:
        owner = _create_user(session, "fallback-owner@example.com")
        target = _create_user(session, "fallback-target@example.com")
        starter_workspace_id = target.default_workspace_id
        assert starter_workspace_id is not None
        default_workspace = _create_owned_workspace(
            session, owner, "persisted-default"
        )
        removed_workspace = _create_owned_workspace(
            session, owner, "selected-for-removal"
        )
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=target.id,
                    workspace_id=default_workspace.id,
                    role="viewer",
                ),
                WorkspaceMembership(
                    user_id=target.id,
                    workspace_id=removed_workspace.id,
                    role="viewer",
                ),
            ]
        )
        target.default_workspace_id = default_workspace.id
        target_id = target.id
        default_workspace_id = default_workspace.id
        removed_workspace_id = removed_workspace.id

    result = WorkspaceMembershipService(session_factory).remove_member_by_id(
        actor_user_id=target_id,
        workspace_id=removed_workspace_id,
        target_user_id=target_id,
    )

    assert result.default_workspace_id == default_workspace_id
    assert result.fallback_workspace_id == default_workspace_id
    with session_factory() as session:
        persisted_target = session.get(User, target_id)
        assert persisted_target is not None
        assert persisted_target.default_workspace_id == default_workspace_id
        assert session.get(
            WorkspaceMembership, (target_id, starter_workspace_id)
        ) is not None
        assert session.get(
            WorkspaceMembership, (target_id, default_workspace_id)
        ) is not None
        assert session.get(
            WorkspaceMembership, (target_id, removed_workspace_id)
        ) is None


def test_removal_uses_another_membership_when_default_is_removed(session_factory):
    with session_factory.begin() as session:
        owner = _create_user(session, "fallback-guard-owner@example.com")
        target = _create_user(session, "fallback-guard-target@example.com")
        starter_workspace_id = target.default_workspace_id
        assert starter_workspace_id is not None
        fallback_workspace = _create_owned_workspace(
            session, owner, "fallback-guard-default"
        )
        removed_workspace = _create_owned_workspace(
            session, owner, "fallback-guard-removed"
        )
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=target.id,
                    workspace_id=fallback_workspace.id,
                    role="viewer",
                ),
                WorkspaceMembership(
                    user_id=target.id,
                    workspace_id=removed_workspace.id,
                    role="viewer",
                ),
            ]
        )
        target.default_workspace_id = removed_workspace.id
        starter_membership = session.get(
            WorkspaceMembership, (target.id, starter_workspace_id)
        )
        assert starter_membership is not None
        session.delete(starter_membership)
        target_id = target.id
        fallback_workspace_id = fallback_workspace.id
        removed_workspace_id = removed_workspace.id

    result = WorkspaceMembershipService(session_factory).remove_member_by_id(
        actor_user_id=target_id,
        workspace_id=removed_workspace_id,
        target_user_id=target_id,
    )

    assert result.default_workspace_id == fallback_workspace_id
    assert result.fallback_workspace_id == fallback_workspace_id
    with session_factory() as session:
        persisted_target = session.get(User, target_id)
        assert persisted_target is not None
        assert persisted_target.default_workspace_id == fallback_workspace_id
        assert session.get(
            WorkspaceMembership, (target_id, removed_workspace_id)
        ) is None


def test_removal_allows_zero_workspace_state_when_no_fallback_exists(session_factory):
    with session_factory.begin() as session:
        owner = _create_user(session, "zero-fallback-owner@example.com")
        target = User(
            email="zero-fallback-target@example.com",
            password_hash="unused-test-hash",
            display_name="zero-fallback-target",
            status="active",
        )
        session.add(target)
        session.flush()
        removed_workspace = _create_owned_workspace(
            session, owner, "zero-fallback-removed"
        )
        session.add(
            WorkspaceMembership(
                user_id=target.id,
                workspace_id=removed_workspace.id,
                role="viewer",
            )
        )
        target.default_workspace_id = removed_workspace.id
        target_id = target.id
        removed_workspace_id = removed_workspace.id

    result = WorkspaceMembershipService(session_factory).remove_member_by_id(
        actor_user_id=target_id,
        workspace_id=removed_workspace_id,
        target_user_id=target_id,
    )

    assert result.default_workspace_id is None
    assert result.fallback_workspace_id is None
    with session_factory() as session:
        persisted_target = session.get(User, target_id)
        assert persisted_target is not None
        assert persisted_target.default_workspace_id is None
        assert (
            session.get(WorkspaceMembership, (target_id, removed_workspace_id)) is None
        )


def test_membership_and_audit_roll_back_together(session_factory):
    with session_factory.begin() as session:
        owner = _create_user(session, "owner-rollback@example.com")
        target = _create_user(session, "target-rollback@example.com")
        workspace_id = owner.default_workspace_id
        assert workspace_id is not None
        owner_id = owner.id
        target_id = target.id
        audit_count_before = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
        )

    service = WorkspaceMembershipService(
        session_factory=session_factory,
        audit_service=_FailingAuditService(),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.add_member_with_metadata(
            actor_user_id=owner_id,
            workspace_id=workspace_id,
            email="target-rollback@example.com",
            confirm_first_share=True,
        )

    with session_factory() as session:
        assert session.get(WorkspaceMembership, (target_id, workspace_id)) is None
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
        ) == audit_count_before
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.owner_user_id == owner_id


def test_owner_projection_is_repaired_deterministically_on_idempotent_add(
    session_factory,
):
    with session_factory.begin() as session:
        first = _create_user(session, "projection-first@example.com")
        second = _create_user(session, "projection-second@example.com")
        shared = Workspace(
            slug="projection-repair",
            title="Projection repair",
            name="Projection repair",
            created_by_actor_id=first.id,
            owner_actor_id=first.id,
            owner_user_id=second.id,
        )
        session.add(shared)
        session.flush()
        first_membership = WorkspaceMembership(
            user_id=first.id,
            workspace_id=shared.id,
            role="owner",
        )
        second_membership = WorkspaceMembership(
            user_id=second.id,
            workspace_id=shared.id,
            role="owner",
        )
        session.add_all([first_membership, second_membership])
        session.flush()
        # Make the deterministic ordering explicit instead of relying on clock
        # resolution when both rows are created in one flush.
        second_membership.joined_at = first_membership.joined_at
        ordered_ids = sorted((first.id, second.id), key=str)
        expected_owner_id = ordered_ids[0]
        target = session.get(User, expected_owner_id)
        assert target is not None
        target_email = target.email
        workspace_id = shared.id

    result = WorkspaceMembershipService(session_factory).add_member_with_metadata(
        actor_user_id=first.id,
        workspace_id=workspace_id,
        email=target_email,
        role="owner",
    )

    assert result.created is False
    with session_factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.owner_user_id == expected_owner_id
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
        ) == 0


def test_member_list_tie_breaks_joined_time_by_user_id(session_factory):
    with session_factory.begin() as session:
        first = _create_user(session, "list-order-first@example.com")
        second = _create_user(session, "list-order-second@example.com")
        shared = Workspace(
            slug="member-list-order",
            title="Member list order",
            name="Member list order",
            created_by_actor_id=first.id,
            owner_actor_id=first.id,
            owner_user_id=first.id,
        )
        session.add(shared)
        session.flush()
        first_membership = WorkspaceMembership(
            user_id=first.id,
            workspace_id=shared.id,
            role="owner",
        )
        second_membership = WorkspaceMembership(
            user_id=second.id,
            workspace_id=shared.id,
            role="owner",
        )
        session.add_all([first_membership, second_membership])
        session.flush()
        second_membership.joined_at = first_membership.joined_at
        workspace_id = shared.id
        actor_user_id = first.id
        expected_ids = sorted((first.id, second.id), key=lambda value: value.hex)

    page = WorkspaceMembershipService(session_factory).list_members_page(
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
    )

    assert [member.user_id for member in page.items] == expected_ids
    assert page.limit == 25
    assert page.next_cursor is None


def test_sqlite_mutation_reservation_is_duplicate_safe_and_listing_is_nonlocking(
    tmp_path,
):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'membership-reservation.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        owner = _create_user(session, "reservation-owner@example.com")
        target = _create_user(session, "reservation-target@example.com")
        shared = _create_owned_workspace(session, owner, "reservation-shared")
        session.add(
            WorkspaceMembership(
                user_id=target.id,
                workspace_id=shared.id,
                role="viewer",
            )
        )
        owner_id = owner.id
        target_id = target.id
        workspace_id = shared.id

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        WorkspaceMembershipService(session_factory).change_member_role(
            actor_user_id=owner_id,
            workspace_id=workspace_id,
            target_user_id=target_id,
            role="editor",
        )
        assert statements.count("BEGIN IMMEDIATE") == 1

        statements.clear()
        page = WorkspaceMembershipService(session_factory).list_members_page(
            actor_user_id=owner_id,
            workspace_id=workspace_id,
        )
        assert len(page.items) == 2
        assert "BEGIN IMMEDIATE" not in statements
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)


@pytest.mark.parametrize("_iteration", range(20))
def test_concurrent_owner_leaves_keep_one_active_owner(tmp_path, _iteration):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'membership-race.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        first = _create_user(session, "first-owner@example.com")
        second = _create_user(session, "second-owner@example.com")
        shared = Workspace(
            slug="concurrent-owner-race",
            title="Concurrent owner race",
            name="Concurrent owner race",
            created_by_actor_id=first.id,
            owner_actor_id=first.id,
            owner_user_id=first.id,
        )
        session.add(shared)
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=first.id,
                    workspace_id=shared.id,
                    role="owner",
                ),
                WorkspaceMembership(
                    user_id=second.id,
                    workspace_id=shared.id,
                    role="owner",
                ),
            ]
        )
        session.flush()
        first_id = first.id
        second_id = second.id
        workspace_id = shared.id

    barrier = Barrier(2, timeout=30)

    def leave(user_id):
        barrier.wait(timeout=5)
        try:
            WorkspaceMembershipService(session_factory).remove_member_by_id(
                actor_user_id=user_id,
                workspace_id=workspace_id,
                target_user_id=user_id,
            )
        except WorkspaceLastOwnerLockedError:
            return "locked"
        return "removed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(leave, (first_id, second_id)))

    assert sorted(results) == ["locked", "removed"]
    with session_factory() as session:
        memberships = list(
            session.scalars(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id
                )
            )
        )
        assert len(memberships) == 1
        remaining = memberships[0]
        assert remaining.role == "owner"
        remaining_user = session.get(User, remaining.user_id)
        assert remaining_user is not None and remaining_user.status == "active"
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.owner_user_id == remaining.user_id
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "workspace.member.removed",
                )
            )
        )
        assert len(events) == 1


@pytest.mark.parametrize("_iteration", range(20))
def test_concurrent_owner_demotions_cannot_reach_zero_owners(tmp_path, _iteration):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'membership-demotion-race.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        first = _create_user(session, "first-demotion@example.com")
        second = _create_user(session, "second-demotion@example.com")
        shared = Workspace(
            slug="concurrent-demotion-race",
            title="Concurrent demotion race",
            name="Concurrent demotion race",
            created_by_actor_id=first.id,
            owner_actor_id=first.id,
            owner_user_id=first.id,
        )
        session.add(shared)
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=first.id,
                    workspace_id=shared.id,
                    role="owner",
                ),
                WorkspaceMembership(
                    user_id=second.id,
                    workspace_id=shared.id,
                    role="owner",
                ),
            ]
        )
        session.flush()
        user_ids = (first.id, second.id)
        workspace_id = shared.id

    barrier = Barrier(2, timeout=30)

    def demote(user_id):
        barrier.wait(timeout=5)
        try:
            WorkspaceMembershipService(session_factory).change_member_role(
                actor_user_id=user_id,
                workspace_id=workspace_id,
                target_user_id=user_id,
                role="viewer",
            )
        except WorkspaceLastOwnerLockedError:
            return "locked"
        return "demoted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(demote, user_ids))

    assert sorted(results) == ["demoted", "locked"]
    with session_factory() as session:
        roles = list(
            session.scalars(
                select(WorkspaceMembership.role)
                .where(WorkspaceMembership.workspace_id == workspace_id)
                .order_by(WorkspaceMembership.user_id)
            )
        )
        assert sorted(roles) == ["owner", "viewer"]
        assert session.scalar(
            select(func.count())
            .select_from(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == "owner",
            )
        ) == 1
