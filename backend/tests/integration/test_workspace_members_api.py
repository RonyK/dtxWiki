from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.tests.conftest import authenticate_test_client


def _client_pair(page_service) -> tuple[TestClient, TestClient]:
    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="workspace-members-secret",
        ),
        page_service=page_service,
    )
    owner = TestClient(app)
    member = TestClient(app)
    authenticate_test_client(owner, email="owner@example.com")
    authenticate_test_client(member, email="member@example.com")
    return owner, member


def _users_and_owner_workspace(page_service):
    with page_service.session_factory() as session:
        owner = session.scalar(select(User).where(User.email == "owner@example.com"))
        member = session.scalar(
            select(User).where(User.email == "member@example.com")
        )
        assert owner is not None and owner.personal_workspace_id is not None
        assert member is not None and member.personal_workspace_id is not None
        workspace = session.get(Workspace, owner.personal_workspace_id)
        assert workspace is not None
        return owner, member, workspace


def test_first_share_requires_confirmation_and_duplicate_add_is_idempotent(
    page_service,
):
    owner_client, _ = _client_pair(page_service)
    owner, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"

    refused = owner_client.post(endpoint, json={"email": member.email})
    assert refused.status_code == 409
    assert refused.json()["error_code"] == (
        "workspace_first_share_confirmation_required"
    )
    with page_service.session_factory() as session:
        assert session.get(WorkspaceMembership, (member.id, workspace.id)) is None
        assert session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.action == "workspace.member.added",
            )
        ) is None

    added = owner_client.post(
        endpoint,
        json={"email": member.email, "confirm_first_share": True},
    )
    assert added.status_code == 201
    assert added.json()["data"] == {
        "member": {
            "user_id": str(member.id),
            "email": member.email,
            "display_name": member.display_name,
            "status": "active",
            "role": "viewer",
            "joined_at": added.json()["data"]["member"]["joined_at"],
            "is_personal_anchor_owner": False,
            "is_current_user": False,
        },
        "created": True,
        "first_share": True,
        "first_share_warning_code": (
            "workspace_first_share_confirmation_required"
        ),
        "member_count": 2,
    }
    duplicate = owner_client.post(endpoint, json={"email": member.email})
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["created"] is False
    assert duplicate.json()["data"]["first_share"] is False
    conflicting_add = owner_client.post(
        endpoint,
        json={"email": member.email, "role": "editor"},
    )
    assert conflicting_add.status_code == 409
    assert conflicting_add.json()["error_code"] == (
        "workspace_membership_role_conflict"
    )

    with page_service.session_factory() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        assert persisted_member.default_workspace_id == member.personal_workspace_id
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace.id,
                    AuditEvent.action == "workspace.member.added",
                )
            )
        )
        assert len(events) == 1
        assert member.email not in str(events[0].payload)
        assert events[0].payload == {
            "workspace_id": str(workspace.id),
            "target_user_id": str(member.id),
            "old_role": None,
            "new_role": "viewer",
            "first_share": True,
        }
        persisted_workspace = session.get(Workspace, workspace.id)
        assert persisted_workspace is not None
        assert persisted_workspace.owner_user_id == owner.id
        assert persisted_workspace.visibility == "private"
        assert persisted_workspace.join_policy == "invite_only"
        assert persisted_workspace.title == "Personal Workspace"


def test_shared_personal_workspace_summary_carries_owner_display_name(page_service):
    # WS-09: every Personal workspace defaults to the exact same title
    # ("Personal Workspace"). Once shared, the member's own `GET
    # /workspaces` list contains two rows with identical titles (their own
    # Personal workspace and the owner's) unless something disambiguates
    # them — owner_display_name is that something.
    owner_client, member_client = _client_pair(page_service)
    owner, member, workspace = _users_and_owner_workspace(page_service)
    added = owner_client.post(
        f"/api/v1/workspaces/{workspace.id}/members",
        json={"email": member.email, "confirm_first_share": True},
    )
    assert added.status_code == 201

    listed = member_client.get("/api/v1/workspaces")
    assert listed.status_code == 200
    rows = {
        row["id"]: row for row in listed.json()["data"]["workspaces"]
    }
    shared_row = rows[str(workspace.id)]
    own_personal_row = rows[str(member.personal_workspace_id)]
    assert shared_row["title"] == own_personal_row["title"] == "Personal Workspace"
    assert shared_row["owner_display_name"] == owner.display_name
    # The member's own Personal workspace must not claim an owner other than
    # (or in addition to) the obvious — this pins the field is populated
    # from the live owner projection, not left stale/blank asymmetrically.
    assert own_personal_row["owner_display_name"] == member.display_name


def test_owner_list_is_stably_paginated_and_non_owner_is_forbidden(page_service):
    owner_client, member_client = _client_pair(page_service)
    owner, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"
    assert owner_client.post(
        endpoint,
        json={"email": member.email, "confirm_first_share": True},
    ).status_code == 201

    first = owner_client.get(endpoint, params={"limit": 1, "offset": 0})
    second = owner_client.get(endpoint, params={"limit": 1, "offset": 1})
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["total"] == 2
    assert first.json()["data"]["has_more"] is True
    assert second.json()["data"]["has_more"] is False
    ids = {
        first.json()["data"]["items"][0]["user_id"],
        second.json()["data"]["items"][0]["user_id"],
    }
    assert ids == {
        str(owner.id),
        str(member.id),
    }

    denied = member_client.get(endpoint)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "workspace_role_insufficient"


def test_member_list_cursor_filters_normalize_without_read_audit_noise(
    page_service,
):
    owner_client, _ = _client_pair(page_service)
    owner, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"
    with page_service.session_factory.begin() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        persisted_member.display_name = "Straße\u2003ﬁ   Member"
    assert owner_client.post(
        endpoint,
        json={"email": member.email, "confirm_first_share": True},
    ).status_code == 201
    # The list is ordered by (joined_at, user id) and joined_at is stamped from
    # the wall clock, so a backward clock step (or a tie, broken by the random
    # user id) can swap the pages asserted below. Pin the join order; the
    # sub-second parts keep the cursor's timestamp round trip exercised.
    with page_service.session_factory.begin() as session:
        for user_id, joined_at in (
            (owner.id, datetime(2026, 1, 1, 9, 0, 0, 250000, tzinfo=UTC)),
            (member.id, datetime(2026, 1, 1, 9, 0, 1, 250000, tzinfo=UTC)),
        ):
            membership = session.get(WorkspaceMembership, (user_id, workspace.id))
            assert membership is not None
            membership.joined_at = joined_at

    first_response = owner_client.get(
        endpoint,
        params={"query": "ＥＸＡＭＰＬＥ．ＣＯＭ", "limit": 1},
    )
    assert first_response.status_code == 200
    first = first_response.json()["data"]
    assert first["limit"] == 1
    assert first["offset"] == 0
    assert first["total"] == 2
    assert first["has_more"] is True
    assert first["next_cursor"] is not None
    assert first["items"][0]["user_id"] == str(owner.id)
    assert first["items"][0]["is_current_user"] is True

    second_response = owner_client.get(
        endpoint,
        params={
            "query": "example.com",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )
    assert second_response.status_code == 200
    second = second_response.json()["data"]
    assert second["total"] == 2
    assert second["items"][0]["user_id"] == str(member.id)
    assert second["items"][0]["is_current_user"] is False
    assert second["next_cursor"] is None
    assert second["has_more"] is False

    viewer_response = owner_client.get(
        endpoint,
        params={"query": "ＭＥＭＢＥＲ", "role": "viewer"},
    )
    assert viewer_response.status_code == 200
    viewer_page = viewer_response.json()["data"]
    assert viewer_page["total"] == 1
    assert [item["user_id"] for item in viewer_page["items"]] == [str(member.id)]

    exact_unicode = owner_client.get(
        endpoint,
        params={"query": "STRASSE FI MEMBER", "role": "viewer"},
    )
    assert exact_unicode.status_code == 200
    assert [
        item["user_id"] for item in exact_unicode.json()["data"]["items"]
    ] == [str(member.id)]

    query_mismatch = owner_client.get(
        endpoint,
        params={
            "query": "different",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )
    assert query_mismatch.status_code == 422
    assert query_mismatch.json()["error_code"] == "validation_failed"
    role_mismatch = owner_client.get(
        endpoint,
        params={
            "query": "example.com",
            "role": "viewer",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )
    assert role_mismatch.status_code == 422
    assert role_mismatch.json()["error_code"] == "validation_failed"

    invalid_role = owner_client.get(endpoint, params={"role": "admin"})
    assert invalid_role.status_code == 422
    assert invalid_role.json()["error_code"] == "workspace_role_invalid"

    with page_service.session_factory() as session:
        list_events = list(
            session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.workspace_id == workspace.id,
                    AuditEvent.action == "workspace.members.listed",
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        assert list_events == []


def test_role_change_owner_locks_and_self_remove_default_fallback(page_service):
    owner_client, member_client = _client_pair(page_service)
    owner, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"
    assert owner_client.post(
        endpoint,
        json={"email": member.email, "confirm_first_share": True},
    ).status_code == 201

    changed = owner_client.patch(
        f"{endpoint}/{member.id}",
        json={"role": "editor"},
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["member"]["role"] == "editor"
    assert changed.json()["data"]["member"]["is_current_user"] is False

    anchor_locked = owner_client.patch(
        f"{endpoint}/{owner.id}",
        json={"role": "viewer"},
    )
    assert anchor_locked.status_code == 409
    assert anchor_locked.json()["error_code"] == "personal_workspace_owner_locked"
    anchor_remove = owner_client.delete(f"{endpoint}/{owner.id}")
    assert anchor_remove.status_code == 409
    assert anchor_remove.json()["error_code"] == "personal_workspace_owner_locked"

    with page_service.session_factory.begin() as session:
        persisted_member = session.get(User, member.id)
        persisted_owner = session.get(User, owner.id)
        assert persisted_member is not None
        assert persisted_owner is not None
        persisted_member.default_workspace_id = workspace.id
        # A viewer/editor may leave even when the workspace's only owner is
        # inactive. The active-owner guard applies only to an owner departure.
        persisted_owner.status = "suspended"

    removed = member_client.delete(f"{endpoint}/{member.id}")
    assert removed.status_code == 200
    assert removed.json()["data"] == {
        "removed_user_id": str(member.id),
        "previous_role": "editor",
        "default_workspace_id": str(member.personal_workspace_id),
        "fallback_workspace_id": str(member.personal_workspace_id),
        "member_count": 1,
    }
    with page_service.session_factory() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        assert persisted_member.default_workspace_id == member.personal_workspace_id
        assert session.get(WorkspaceMembership, (member.id, workspace.id)) is None
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.action == "workspace.member.removed",
            )
        )
        assert event is not None
        assert event.actor_id == str(member.id)
        assert event.payload["self_remove"] is True
        assert member.email not in str(event.payload)


def test_self_remove_non_default_preserves_and_reports_accessible_default(
    page_service,
):
    owner_client, member_client = _client_pair(page_service)
    owner, member, removed_workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{removed_workspace.id}/members"
    assert (
        owner_client.post(
            endpoint,
            json={"email": member.email, "confirm_first_share": True},
        ).status_code
        == 201
    )

    with page_service.session_factory.begin() as session:
        default_workspace = Workspace(
            slug="member-persisted-default",
            title="Member Persisted Default",
            name="Member Persisted Default",
            created_by_actor_id=owner.id,
            owner_actor_id=owner.id,
            owner_user_id=owner.id,
        )
        session.add(default_workspace)
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=owner.id,
                    workspace_id=default_workspace.id,
                    role="owner",
                ),
                WorkspaceMembership(
                    user_id=member.id,
                    workspace_id=default_workspace.id,
                    role="viewer",
                ),
            ]
        )
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        persisted_member.default_workspace_id = default_workspace.id
        default_workspace_id = default_workspace.id
        personal_workspace_id = persisted_member.personal_workspace_id
        assert personal_workspace_id is not None

    removed = member_client.delete(f"{endpoint}/{member.id}")

    assert removed.status_code == 200
    assert removed.json()["data"] == {
        "removed_user_id": str(member.id),
        "previous_role": "viewer",
        "default_workspace_id": str(default_workspace_id),
        "fallback_workspace_id": str(default_workspace_id),
        "member_count": 1,
    }
    with page_service.session_factory() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        assert persisted_member.default_workspace_id == default_workspace_id
        assert session.get(
            WorkspaceMembership, (member.id, personal_workspace_id)
        ) is not None
        assert session.get(
            WorkspaceMembership, (member.id, default_workspace_id)
        ) is not None
        assert session.get(
            WorkspaceMembership, (member.id, removed_workspace.id)
        ) is None


def test_self_remove_returns_null_fallback_when_no_workspace_remains(page_service):
    owner_client, member_client = _client_pair(page_service)
    _owner, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"
    assert (
        owner_client.post(
            endpoint,
            json={"email": member.email, "confirm_first_share": True},
        ).status_code
        == 201
    )

    with page_service.session_factory.begin() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        personal_workspace_id = persisted_member.personal_workspace_id
        assert personal_workspace_id is not None
        personal_membership = session.get(
            WorkspaceMembership,
            (member.id, personal_workspace_id),
        )
        assert personal_membership is not None
        session.delete(personal_membership)
        persisted_member.personal_workspace_id = None
        persisted_member.default_workspace_id = workspace.id

    removed = member_client.delete(f"{endpoint}/{member.id}")

    assert removed.status_code == 200
    assert removed.json()["data"] == {
        "removed_user_id": str(member.id),
        "previous_role": "viewer",
        "default_workspace_id": None,
        "fallback_workspace_id": None,
        "member_count": 1,
    }
    listed = member_client.get("/api/v1/workspaces")
    assert listed.status_code == 200
    assert listed.json()["data"]["workspaces"] == []
    assert listed.json()["data"]["current_workspace_id"] is None


def test_add_rejects_inactive_user_and_bearer_token(page_service):
    owner_client, _ = _client_pair(page_service)
    _, member, workspace = _users_and_owner_workspace(page_service)
    endpoint = f"/api/v1/workspaces/{workspace.id}/members"
    with page_service.session_factory.begin() as session:
        persisted_member = session.get(User, member.id)
        assert persisted_member is not None
        persisted_member.status = "suspended"

    inactive = owner_client.post(
        endpoint,
        json={"email": member.email, "confirm_first_share": True},
    )
    assert inactive.status_code == 409
    assert inactive.json()["error_code"] == "workspace_member_inactive"

    token_response = owner_client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace.id)}],
            "name": "membership-denied",
            "scopes": ["mcp:write"],
        },
    )
    assert token_response.status_code == 201
    token = token_response.json()["data"]["plain_token"]
    bearer = owner_client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 403
    assert bearer.json()["error_code"] == "scope_denied"


def test_last_owner_cannot_leave_non_personal_workspace(page_service):
    owner_client, _ = _client_pair(page_service)
    owner, _, _ = _users_and_owner_workspace(page_service)
    with page_service.session_factory.begin() as session:
        workspace = Workspace(
            slug="sole-owner",
            title="Sole Owner",
            name="Sole Owner",
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
        workspace_id = workspace.id

    endpoint = f"/api/v1/workspaces/{workspace_id}/members/{owner.id}"
    refused = owner_client.delete(endpoint)
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "workspace_last_owner_required"
    demotion = owner_client.patch(endpoint, json={"role": "viewer"})
    assert demotion.status_code == 409
    assert demotion.json()["error_code"] == "workspace_last_owner_required"
    with page_service.session_factory() as session:
        assert session.get(WorkspaceMembership, (owner.id, workspace_id)) is not None


def test_member_not_found_and_invalid_mutation_role_use_stable_errors(page_service):
    owner_client, _ = _client_pair(page_service)
    owner, _, workspace = _users_and_owner_workspace(page_service)
    missing_user_id = UUID("00000000-0000-0000-0000-000000000999")
    endpoint = f"/api/v1/workspaces/{workspace.id}/members/{missing_user_id}"

    missing_patch = owner_client.patch(endpoint, json={"role": "viewer"})
    assert missing_patch.status_code == 404
    assert missing_patch.json()["error_code"] == "workspace_member_not_found"
    missing_delete = owner_client.delete(endpoint)
    assert missing_delete.status_code == 404
    assert missing_delete.json()["error_code"] == "workspace_member_not_found"

    invalid_patch = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/members/{owner.id}",
        json={"role": "admin"},
    )
    assert invalid_patch.status_code == 422
    assert invalid_patch.json()["error_code"] == "workspace_role_invalid"
    invalid_add = owner_client.post(
        f"/api/v1/workspaces/{workspace.id}/members",
        json={"email": "member@example.com", "role": "admin"},
    )
    assert invalid_add.status_code == 422
    assert invalid_add.json()["error_code"] == "workspace_role_invalid"
    direct_owner_add = owner_client.post(
        f"/api/v1/workspaces/{workspace.id}/members",
        json={"email": "member@example.com", "role": "owner"},
    )
    assert direct_owner_add.status_code == 422
    assert direct_owner_add.json()["error_code"] == "workspace_role_invalid"
