from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.context import ContextPage
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest
from backend.policy.auth import ActorContext
from backend.services.workspace_read import WorkspaceReadService
from backend.tests.conftest import authenticate_test_client


def _clients(page_service) -> tuple[TestClient, TestClient]:
    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="workspace-directory-secret",
        ),
        page_service=page_service,
    )
    owner = TestClient(app)
    requester = TestClient(app)
    authenticate_test_client(owner, email="directory-owner@example.com")
    authenticate_test_client(requester, email="directory-user@example.com")
    return owner, requester


def _actors(page_service):
    with page_service.session_factory() as session:
        owner = session.scalar(
            select(User).where(User.email == "directory-owner@example.com")
        )
        requester = session.scalar(
            select(User).where(User.email == "directory-user@example.com")
        )
        assert owner is not None and owner.personal_workspace_id is not None
        assert requester is not None and requester.personal_workspace_id is not None
        workspace = session.get(Workspace, owner.personal_workspace_id)
        assert workspace is not None
        return owner, requester, workspace


def _set_access(
    client: TestClient,
    workspace_id,
    *,
    visibility: str,
    join_policy: str,
):
    current = client.get(f"/api/v1/workspaces/{workspace_id}/access")
    assert current.status_code == 200
    return client.patch(
        f"/api/v1/workspaces/{workspace_id}/access",
        json={
            "visibility": visibility,
            "join_policy": join_policy,
            "expected_updated_at": current.json()["data"]["updated_at"],
            "confirm_access_change": True,
        },
    )


def test_directory_is_normalized_cursor_bound_and_membership_neutral(page_service):
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    with page_service.session_factory.begin() as session:
        persisted = session.get(Workspace, workspace.id)
        persisted.title = "Straße\tﬁ   Ａlpha Team"
        persisted.description = "  A directory-safe   short description.  "

    current_access = owner_client.get(
        f"/api/v1/workspaces/{workspace.id}/access"
    ).json()["data"]
    unconfirmed = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "team",
            "join_policy": "request",
            "expected_updated_at": current_access["updated_at"],
        },
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["error_code"] == (
        "workspace_access_confirmation_required"
    )
    changed = _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    )
    assert changed.status_code == 200

    stale = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "public",
            "expected_updated_at": current_access["updated_at"],
            "confirm_access_change": True,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_base_version"

    invalid_private_request = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "private",
            "join_policy": "request",
            "expected_updated_at": changed.json()["data"]["updated_at"],
            "confirm_access_change": True,
        },
    )
    assert invalid_private_request.status_code == 422

    legacy_visibility_write = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/settings/policies",
        json={"group": "workspace", "key": "visibility", "value": "public"},
    )
    assert legacy_visibility_write.status_code == 422

    token = owner_client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace.id)}],
            "name": "access-settings-denied",
            "scopes": ["mcp:write"],
        },
    ).json()["data"]["plain_token"]
    bearer = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "visibility": "public",
            "expected_updated_at": changed.json()["data"]["updated_at"],
            "confirm_access_change": True,
        },
    )
    assert bearer.status_code == 403
    assert bearer.json()["error_code"] == "scope_denied"

    response = requester_client.get(
        "/api/v1/workspace-directory",
        params={"q": "alpha", "limit": 1},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"] == [
        {
            "workspace_id": str(workspace.id),
            "title": "Straße\tﬁ   Ａlpha Team",
            "description": "A directory-safe short description.",
            "visibility": "team",
            "join_policy": "request",
            "current_request_status": None,
        }
    ]
    assert set(data["items"][0]) == {
        "workspace_id",
        "title",
        "description",
        "visibility",
        "join_policy",
        "current_request_status",
    }
    assert data["next_cursor"] is None

    exact_unicode = requester_client.get(
        "/api/v1/workspace-directory",
        params={"q": "STRASSE FI ALPHA"},
    )
    assert exact_unicode.status_code == 200
    assert [
        item["workspace_id"] for item in exact_unicode.json()["data"]["items"]
    ] == [str(workspace.id)]

    # A cursor is query-bound, and directory discovery never creates access.
    second_workspace = None
    with page_service.session_factory.begin() as session:
        second_workspace = Workspace(
            slug=f"alpha-{uuid4().hex[:8]}",
            title="Alpha Two",
            name="Alpha Two",
            visibility="team",
            join_policy="request",
            created_by_actor_id=owner.id,
            owner_actor_id=owner.id,
            owner_user_id=owner.id,
        )
        session.add(second_workspace)
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=owner.id,
                workspace_id=second_workspace.id,
                role="owner",
            )
        )
    first = requester_client.get(
        "/api/v1/workspace-directory",
        params={"q": "alpha", "limit": 1},
    ).json()["data"]
    assert first["next_cursor"] is not None
    mismatch = requester_client.get(
        "/api/v1/workspace-directory",
        params={"q": "beta", "cursor": first["next_cursor"]},
    )
    assert mismatch.status_code == 422

    exact = requester_client.get(f"/api/v1/workspaces/{workspace.id}")
    assert exact.status_code == 403
    with page_service.session_factory() as session:
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(requester.id),
            user_id=requester.id,
            token_kind="session",
            provenance="browser_session",
        )
        listed = WorkspaceReadService().list_workspaces(session, actor=actor)
        assert workspace.id not in {item.id for item in listed.workspaces}


def test_directory_uses_binary_normalized_order_with_equal_key_cursors(page_service):
    _, requester_client = _clients(page_service)
    owner, _, _ = _actors(page_service)
    workspace_specs = [
        (UUID(int=100), "Straße\tﬁ"),
        (UUID(int=101), "STRASSE FI"),
        (UUID(int=102), "Zulu"),
        (UUID(int=103), "Ångstrom"),
    ]
    with page_service.session_factory.begin() as session:
        for workspace_id, title in workspace_specs:
            workspace = Workspace(
                id=workspace_id,
                slug=f"binary-{workspace_id.int}",
                title=title,
                name=title,
                visibility="public",
                join_policy="request",
                owner_user_id=owner.id,
            )
            session.add(workspace)
            session.add(
                WorkspaceMembership(
                    user_id=owner.id,
                    workspace_id=workspace_id,
                    role="owner",
                )
            )

    cursor = None
    observed: list[tuple[str, str]] = []
    while True:
        response = requester_client.get(
            "/api/v1/workspace-directory",
            params={
                "limit": 1,
                **({"cursor": cursor} if cursor is not None else {}),
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        observed.extend(
            (item["workspace_id"], item["title"]) for item in data["items"]
        )
        cursor = data["next_cursor"]
        if cursor is None:
            break

    assert observed == [
        (str(workspace_id), title) for workspace_id, title in workspace_specs
    ]


def test_join_attempts_are_idempotent_stale_safe_and_viewer_only(page_service):
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"

    first = requester_client.post(endpoint, json={"role": "owner"})
    duplicate = requester_client.post(endpoint)
    assert first.status_code == 201
    assert duplicate.status_code == 200
    first_data = first.json()["data"]
    assert first_data["request"]["attempt_no"] == 1
    assert duplicate.json()["data"]["request"]["request_id"] == (
        first_data["request"]["request_id"]
    )
    assert duplicate.json()["data"]["new_attempt"] is False

    owner_queue = owner_client.get(endpoint)
    assert owner_queue.status_code == 200
    assert owner_queue.json()["data"]["total"] == 1
    denied_queue = requester_client.get(endpoint)
    assert denied_queue.status_code == 403

    visible = requester_client.get("/api/v1/workspace-directory").json()["data"]
    directory_item = next(
        item for item in visible["items"] if item["workspace_id"] == str(workspace.id)
    )
    assert directory_item["current_request_status"] == "pending"
    pending_only = requester_client.get(
        "/api/v1/workspace-directory",
        params={"pending_only": True},
    )
    assert pending_only.status_code == 200
    assert [
        item["workspace_id"] for item in pending_only.json()["data"]["items"]
    ] == [str(workspace.id)]

    request_id = first_data["request"]["request_id"]
    stale = owner_client.patch(
        f"{endpoint}/{request_id}",
        json={"decision": "reject", "expected_attempt_no": 2},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "workspace_join_request_stale"
    rejected = owner_client.patch(
        f"{endpoint}/{request_id}",
        json={"decision": "reject", "expected_attempt_no": 1},
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["request"]["status"] == "rejected"

    second = requester_client.post(endpoint)
    assert second.status_code == 201
    assert second.json()["data"]["request"]["attempt_no"] == 2
    stale_old = owner_client.patch(
        f"{endpoint}/{request_id}",
        json={"decision": "approve", "expected_attempt_no": 1},
    )
    assert stale_old.status_code == 409
    approved = owner_client.patch(
        f"{endpoint}/{request_id}",
        json={"decision": "approve", "expected_attempt_no": 2},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["membership_created"] is True

    with page_service.session_factory() as session:
        membership = session.get(
            WorkspaceMembership,
            (requester.id, workspace.id),
        )
        persisted_requester = session.get(User, requester.id)
        assert membership is not None and membership.role == "viewer"
        assert persisted_requester.default_workspace_id == (
            requester.personal_workspace_id
        )
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace.id,
                    AuditEvent.target_kind == "workspace_join_request",
                )
            )
        )
        assert {event.action for event in events} >= {
            "workspace.join_request.requested",
            "workspace.join_request.rejected",
            "workspace.join_request.approved",
        }
        # UI-08: event_summary used to default to the raw action string
        # ("workspace.join_request.requested"), which the activity feed then
        # humanized into "Workspace.join request.requested" -- every row
        # showing the same nonsense title regardless of requester. Both the
        # prose summary and the requester's name (via payload["target_title"])
        # must be present on every join-request audit event.
        for event in events:
            assert event.event_summary != event.action
            assert event.event_summary is not None
            assert event.payload["target_title"] == requester.display_name


def test_join_request_activity_title_is_owner_only(page_service):
    # UI-08 part 2: _emit_audit now rides the requester's display_name in
    # payload["target_title"], which widens who can see it unless the
    # activity projection restricts it back down. Join requests are an
    # owner-only surface (WorkspaceOwnerAccessDep gates the queue itself), so
    # the activity feed's event_key/target_title for these rows must follow
    # the same boundary -- a non-owner member must not learn the requester's
    # name just because it started riding the audit payload.
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"

    created = requester_client.post(endpoint)
    assert created.status_code == 201
    request_id = created.json()["data"]["request"]["request_id"]
    approved = owner_client.patch(
        f"{endpoint}/{request_id}",
        json={"decision": "approve", "expected_attempt_no": 1},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["membership_created"] is True
    # The now-approved requester is a "viewer" member (see
    # test_join_attempts_are_idempotent_stale_safe_and_viewer_only), giving a
    # real non-owner actor to read the same feed with.

    owner_feed = owner_client.get(
        f"/api/v1/workspaces/{workspace.id}/activity",
        params={"period": "all", "kind": "workspace"},
    )
    assert owner_feed.status_code == 200
    owner_join_rows = [
        item
        for item in owner_feed.json()["data"]["items"]
        if item["body"] == "workspace.join_request.requested"
    ]
    assert owner_join_rows, "expected the join-request row in the owner's feed"
    assert owner_join_rows[0]["event_key"] == "workspace.join_request.requested"
    assert owner_join_rows[0]["target_title"] == requester.display_name

    viewer_feed = requester_client.get(
        f"/api/v1/workspaces/{workspace.id}/activity",
        params={"period": "all", "kind": "workspace"},
    )
    assert viewer_feed.status_code == 200
    viewer_join_rows = [
        item
        for item in viewer_feed.json()["data"]["items"]
        if item["body"] == "workspace.join_request.requested"
    ]
    assert viewer_join_rows, "expected the join-request row in the viewer's feed too"
    assert viewer_join_rows[0]["event_key"] is None
    assert viewer_join_rows[0]["target_title"] is None


def test_requester_can_cancel_and_terminal_rerequest_increments_attempt(page_service):
    owner_client, requester_client = _clients(page_service)
    _, _, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"
    first = requester_client.post(endpoint).json()["data"]["request"]
    cancelled = requester_client.delete(f"{endpoint}/me")
    duplicate_cancel = requester_client.delete(f"{endpoint}/me")
    assert cancelled.status_code == duplicate_cancel.status_code == 200
    assert cancelled.json()["data"]["request"]["status"] == "cancelled"
    assert duplicate_cancel.json()["data"]["request"]["attempt_no"] == 1
    retried = requester_client.post(endpoint)
    assert retried.status_code == 201
    assert retried.json()["data"]["request"]["request_id"] == first["request_id"]
    assert retried.json()["data"]["request"]["attempt_no"] == 2

    assert _set_access(
        owner_client,
        workspace.id,
        visibility="private",
        join_policy="invite_only",
    ).status_code == 200
    hidden_cancel = requester_client.delete(f"{endpoint}/me")
    missing_cancel = requester_client.delete(
        f"/api/v1/workspaces/{UUID(int=0)}/join-requests/me"
    )
    assert hidden_cancel.status_code == missing_cancel.status_code == 404
    assert hidden_cancel.json()["error_code"] == "not_found"


def test_approval_closes_pending_request_when_invitation_already_created_membership(
    page_service,
):
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"
    requested = requester_client.post(endpoint)
    assert requested.status_code == 201
    request = requested.json()["data"]["request"]

    # Model a direct invitation winning the race before the owner handles the
    # still-pending request. Approval must never overwrite the invited role.
    with page_service.session_factory.begin() as session:
        session.add(
            WorkspaceMembership(
                user_id=requester.id,
                workspace_id=workspace.id,
                role="editor",
            )
        )

    decided = owner_client.patch(
        f"{endpoint}/{request['request_id']}",
        json={
            "decision": "approve",
            "expected_attempt_no": request["attempt_no"],
        },
    )

    assert decided.status_code == 200
    data = decided.json()["data"]
    assert data["membership_created"] is False
    assert data["request"]["status"] == "closed"
    assert data["request"]["closed_reason"] == "membership_already_exists"
    with page_service.session_factory() as session:
        membership = session.get(
            WorkspaceMembership,
            (requester.id, workspace.id),
        )
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.target_id == UUID(request["request_id"]),
                AuditEvent.action == "workspace.join_request.closed",
            )
        )
        assert membership is not None and membership.role == "editor"
        assert event is not None
        assert event.payload["reason"] == "membership_already_exists"


def test_direct_add_closes_a_pending_join_request(page_service):
    # WS-06: the reverse direction of the test above. When an owner invites a
    # user who already has a pending join request (rather than approving that
    # request), the request must reach the same terminal state — otherwise it
    # is stuck at "pending" forever: it never reappears in the owner's queue
    # (add_member does not touch it), but `pending_join_request_count` and the
    # queue view would keep counting a request nothing can ever resolve.
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"
    requested = requester_client.post(endpoint)
    assert requested.status_code == 201
    request = requested.json()["data"]["request"]

    added = owner_client.post(
        f"/api/v1/workspaces/{workspace.id}/members",
        json={
            "email": "directory-user@example.com",
            "role": "editor",
            "confirm_first_share": True,
        },
    )
    assert added.status_code == 201
    assert added.json()["data"]["member"]["role"] == "editor"

    access = owner_client.get(f"/api/v1/workspaces/{workspace.id}/access")
    assert access.status_code == 200
    assert access.json()["data"]["pending_join_request_count"] == 0

    with page_service.session_factory() as session:
        closed_request = session.get(WorkspaceJoinRequest, UUID(request["request_id"]))
        assert closed_request is not None
        assert closed_request.status == "closed"
        assert closed_request.closed_reason == "membership_already_exists"
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.target_id == UUID(request["request_id"]),
                AuditEvent.action == "workspace.join_request.closed",
            )
        )
        assert event is not None
        assert event.payload["reason"] == "membership_already_exists"

    # The request no longer being pending must not permanently block a future
    # re-request (WS-01): if this member is later removed, a fresh request
    # must be able to start a new attempt rather than being stuck terminal.
    removed = owner_client.delete(
        f"/api/v1/workspaces/{workspace.id}/members/{requester.id}"
    )
    assert removed.status_code == 200
    retried = requester_client.post(endpoint)
    assert retried.status_code == 201
    assert retried.json()["data"]["request"]["attempt_no"] == 2


def test_inactive_requester_and_policy_fail_with_stable_codes(page_service):
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    private = requester_client.post(
        f"/api/v1/workspaces/{workspace.id}/join-requests"
    )
    assert private.status_code == 404
    assert private.json()["error_code"] == "not_found"
    private_exact = requester_client.get(f"/api/v1/workspaces/{workspace.id}")
    assert private_exact.status_code == 404
    assert private_exact.json()["error_code"] == "not_found"

    assert _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="request",
    ).status_code == 200
    endpoint = f"/api/v1/workspaces/{workspace.id}/join-requests"
    requested = requester_client.post(endpoint)
    request = requested.json()["data"]["request"]
    with page_service.session_factory.begin() as session:
        owner_membership = session.get(
            WorkspaceMembership,
            (owner.id, workspace.id),
        )
        owner_membership.role = "editor"
    no_live_owner = owner_client.patch(
        f"{endpoint}/{request['request_id']}",
        json={
            "decision": "approve",
            "expected_attempt_no": request["attempt_no"],
        },
    )
    assert no_live_owner.status_code == 403
    assert no_live_owner.json()["error_code"] == "workspace_role_insufficient"
    with page_service.session_factory.begin() as session:
        session.get(
            WorkspaceMembership,
            (owner.id, workspace.id),
        ).role = "owner"
        session.get(User, requester.id).status = "suspended"
    inactive = owner_client.patch(
        f"{endpoint}/{request['request_id']}",
        json={
            "decision": "approve",
            "expected_attempt_no": request["attempt_no"],
        },
    )
    assert inactive.status_code == 409
    assert inactive.json()["error_code"] == (
        "workspace_join_request_requester_inactive"
    )


def test_stale_pending_count_blocks_access_save_around_a_late_arriving_request(
    page_service,
):
    # WS-07: Workspace.updated_at only guards the Workspace row. A join
    # request inserted after the confirmation dialog opened (and read the
    # pending count it displays) does not touch Workspace.updated_at at all,
    # so without the pending-count guard the save below would silently close
    # it despite the dialog having promised "0 requests closed".
    owner_client, requester_client = _clients(page_service)
    _, _, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    ).status_code == 200

    # The dialog's snapshot: read once, matching what confirmAccessChange
    # sends as expected_updated_at / expected_pending_join_request_count.
    snapshot = owner_client.get(f"/api/v1/workspaces/{workspace.id}/access").json()[
        "data"
    ]
    assert snapshot["pending_join_request_count"] == 0

    # A join request arrives in the confirmation window, after the snapshot
    # but before the owner clicks confirm. It does not change
    # Workspace.updated_at.
    requested = requester_client.post(
        f"/api/v1/workspaces/{workspace.id}/join-requests"
    )
    assert requested.status_code == 201
    request_id = requested.json()["data"]["request"]["request_id"]

    saved_with_stale_snapshot = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "public",
            "join_policy": "request",
            "expected_updated_at": snapshot["updated_at"],
            "expected_pending_join_request_count": snapshot[
                "pending_join_request_count"
            ],
            "confirm_access_change": True,
        },
    )
    assert saved_with_stale_snapshot.status_code == 409
    assert (
        saved_with_stale_snapshot.json()["error_code"] == "stale_base_version"
    )

    with page_service.session_factory() as session:
        still_pending = session.get(WorkspaceJoinRequest, UUID(request_id))
        assert still_pending is not None
        assert still_pending.status == "pending"
    unchanged = owner_client.get(f"/api/v1/workspaces/{workspace.id}/access").json()[
        "data"
    ]
    assert unchanged["visibility"] == "team"
    assert unchanged["pending_join_request_count"] == 1

    # A fresh snapshot (matching the true current count) must still succeed —
    # the new guard must not block ordinary, non-stale saves.
    retried = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "public",
            "join_policy": "request",
            "expected_updated_at": unchanged["updated_at"],
            "expected_pending_join_request_count": unchanged[
                "pending_join_request_count"
            ],
            "confirm_access_change": True,
        },
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["visibility"] == "public"


def test_visibility_downgrade_closes_requests_and_revokes_every_public_page(
    page_service,
):
    owner_client, requester_client = _clients(page_service)
    owner, _, workspace = _actors(page_service)
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace.id).slug = "public-downgrade"
    workspace.slug = "public-downgrade"
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="request",
    ).status_code == 200
    requested = requester_client.post(
        f"/api/v1/workspaces/{workspace.id}/join-requests"
    )
    assert requested.status_code == 201

    page = page_service.write_page(
        workspace_slug="public-downgrade",
        slug="public-before-downgrade",
        title="Public Before Downgrade",
        body_md="# Public Before Downgrade",
        actor_kind="human",
        actor_id=str(owner.id),
        actor_provenance="browser_session",
    )
    published = owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={"is_public": True},
    )
    assert published.status_code == 200
    anonymous = TestClient(owner_client.app)
    public_path = (
        "/api/v1/public/w/public-downgrade/pages/public-before-downgrade"
    )
    assert anonymous.get(public_path).status_code == 200

    downgraded = _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="request",
    )
    assert downgraded.status_code == 200
    payload = downgraded.json()["data"]
    assert payload["closed_join_request_count"] == 1
    assert payload["cleared_public_page_count"] == 1
    assert anonymous.get(public_path).status_code == 404

    with page_service.session_factory() as session:
        persisted_page = session.get(ContextPage, page.id)
        request = session.scalar(
            select(WorkspaceJoinRequest).where(
                WorkspaceJoinRequest.workspace_id == workspace.id
            )
        )
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.action == "workspace.access.updated",
                AuditEvent.payload["visibility"].as_string() == "team",
            )
        )
        assert persisted_page.is_public is False
        assert request.status == "closed"
        assert request.closed_reason == "visibility_downgrade"
        assert audit is not None
        closed_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.action == "workspace.join_request.closed",
            )
        )
        assert closed_audit is not None
        assert closed_audit.payload["reason"] == "visibility_downgrade"

    made_private = _set_access(
        owner_client,
        workspace.id,
        visibility="private",
        join_policy="invite_only",
    )
    assert made_private.status_code == 200
    assert made_private.json()["data"]["join_policy"] == "invite_only"
    directory = requester_client.get("/api/v1/workspace-directory").json()["data"]
    assert str(workspace.id) not in {
        item["workspace_id"] for item in directory["items"]
    }


def test_every_workspace_visibility_change_revokes_unlisted_grants(page_service):
    owner_client, _ = _clients(page_service)
    owner, _, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="invite_only",
    ).status_code == 200

    def create_and_share(slug: str):
        page = page_service.write_page(
            workspace_slug=workspace.slug,
            slug=slug,
            title=slug,
            body_md=f"# {slug}",
            actor_kind="human",
            actor_id=str(owner.id),
            actor_provenance="browser_session",
        )
        shared = owner_client.post(
            f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
            json={
                "is_public": True,
                "acknowledge_public_exposure": True,
                "expected_published_version_id": str(page.version_id),
            },
        )
        assert shared.status_code == 200, shared.text
        with page_service.session_factory() as session:
            persisted = session.get(ContextPage, page.id)
            assert persisted.public_exposure_scope == "unlisted_web"
            assert persisted.public_exposed_version_id == page.version_id
        return page

    team_to_private = create_and_share("team-to-private")
    changed_private = _set_access(
        owner_client,
        workspace.id,
        visibility="private",
        join_policy="invite_only",
    )
    assert changed_private.status_code == 200
    assert changed_private.json()["data"]["cleared_public_page_count"] == 1

    assert _set_access(
        owner_client,
        workspace.id,
        visibility="team",
        join_policy="invite_only",
    ).status_code == 200
    team_to_public = create_and_share("team-to-public")
    changed_public = _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="invite_only",
    )
    assert changed_public.status_code == 200
    assert changed_public.json()["data"]["cleared_public_page_count"] == 1

    with page_service.session_factory() as session:
        for page_id in (team_to_private.id, team_to_public.id):
            persisted = session.get(ContextPage, page_id)
            assert persisted.is_public is False
            assert persisted.public_exposure_scope is None
            assert persisted.public_exposed_version_id is None

    anonymous = TestClient(owner_client.app)
    for slug in ("team-to-private", "team-to-public"):
        assert (
            anonymous.get(
                f"/api/v1/public/w/{workspace.slug}/pages/{slug}"
            ).status_code
            == 404
        )


def test_late_public_grant_makes_visibility_confirmation_stale(page_service):
    owner_client, _ = _clients(page_service)
    owner, _, workspace = _actors(page_service)
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="invite_only",
    ).status_code == 200
    snapshot = owner_client.get(
        f"/api/v1/workspaces/{workspace.id}/access"
    ).json()["data"]
    assert snapshot["public_page_count"] == 0

    page = page_service.write_page(
        workspace_slug=workspace.slug,
        slug="late-public-grant",
        title="Late Public Grant",
        body_md="# Late Public Grant",
        actor_kind="human",
        actor_id=str(owner.id),
        actor_provenance="browser_session",
    )
    shared = owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={"is_public": True},
    )
    assert shared.status_code == 200

    stale = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/access",
        json={
            "visibility": "team",
            "join_policy": "invite_only",
            "expected_updated_at": snapshot["updated_at"],
            "expected_public_page_count": snapshot["public_page_count"],
            "confirm_access_change": True,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_base_version"

    with page_service.session_factory() as session:
        persisted_workspace = session.get(Workspace, workspace.id)
        persisted_page = session.get(ContextPage, page.id)
        assert persisted_workspace.visibility == "public"
        assert persisted_page.is_public is True
        assert persisted_page.public_exposure_scope == "indexed_web"
        assert persisted_page.public_exposed_version_id == page.version_id


def test_settings_policy_path_rejects_visibility_instead_of_bypassing_access_invariants(
    page_service,
):
    # UI-04: PATCH .../settings/policies with key="visibility" used to write
    # workspace.visibility directly (settings_service.py's generic field
    # editor), bypassing every invariant WorkspaceAccessService.update_access
    # enforces for the same field: no stale-write check, no join-policy
    # reconciliation, pending join requests left open, and -- the sharp edge
    # -- public pages NOT cleared when the workspace stops being public. A
    # later flip back to public through the correct path would then
    # instantly re-expose every page that used to be public. This proves the
    # settings path now refuses the field outright (422) rather than
    # applying a subset of what Access & sharing enforces.
    owner_client, _ = _clients(page_service)
    owner, _, workspace = _actors(page_service)
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace.id).slug = "settings-visibility-guard"
    workspace.slug = "settings-visibility-guard"
    assert _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="request",
    ).status_code == 200
    page = page_service.write_page(
        workspace_slug="settings-visibility-guard",
        slug="settings-guard-page",
        title="Settings Guard Page",
        body_md="# Settings Guard Page",
        actor_kind="human",
        actor_id=str(owner.id),
        actor_provenance="browser_session",
    )
    published = owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={"is_public": True},
    )
    assert published.status_code == 200

    rejected = owner_client.patch(
        f"/api/v1/workspaces/{workspace.id}/settings/policies",
        json={"group": "workspace", "key": "visibility", "value": "private"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "settings_policy_update_rejected"
    with page_service.session_factory() as session:
        persisted_workspace = session.get(Workspace, workspace.id)
        persisted_page = session.get(ContextPage, page.id)
        # Neither the workspace nor the page moved -- a partial/bypassing
        # write would have flipped visibility to "private" while leaving
        # is_public=True on the page (the exact "re-exposure trap" shape).
        assert persisted_workspace.visibility == "public"
        assert persisted_page.is_public is True

    # The real (Access & sharing) path for the same transition still works
    # and, unlike the rejected settings path, actually clears the page.
    downgraded = _set_access(
        owner_client,
        workspace.id,
        visibility="private",
        join_policy="invite_only",
    )
    assert downgraded.status_code == 200
    assert downgraded.json()["data"]["cleared_public_page_count"] == 1
    with page_service.session_factory() as session:
        assert session.get(ContextPage, page.id).is_public is False


def test_public_sharing_profile_scoped_index_and_mcp_matrix(page_service):
    owner_client, requester_client = _clients(page_service)
    owner, requester, workspace = _actors(page_service)
    with page_service.session_factory.begin() as session:
        persisted = session.get(Workspace, workspace.id)
        persisted.slug = "profile-public"
        persisted.title = "Public Profile"
    workspace.slug = "profile-public"
    page = page_service.write_page(
        workspace_slug="profile-public",
        slug="index-page",
        title="Index Page",
        body_md="# Index Page\n\nPublic index body.",
        actor_kind="human",
        actor_id=str(owner.id),
        actor_provenance="browser_session",
    )

    acknowledgement_required = owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={
            "is_public": True,
            "expected_published_version_id": str(page.version_id),
        },
    )
    assert acknowledgement_required.status_code == 422
    assert acknowledgement_required.json()["error_code"] == (
        "public_exposure_acknowledgement_required"
    )
    unlisted = owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={
            "is_public": True,
            "acknowledge_public_exposure": True,
            "expected_published_version_id": str(page.version_id),
        },
    )
    assert unlisted.status_code == 200

    anonymous = TestClient(owner_client.app)
    assert (
        anonymous.get(
            "/api/v1/public/w/profile-public/pages/index-page"
        ).status_code
        == 200
    )
    hidden_profile = anonymous.get("/api/v1/public/w/profile-public")
    hidden_scoped_index = anonymous.get(
        "/api/v1/public/w/profile-public/llms.txt"
    )
    for hidden_response in (hidden_profile, hidden_scoped_index):
        assert hidden_response.status_code == 404
        assert hidden_response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert hidden_response.headers["Cache-Control"] == "no-store"

    assert _set_access(
        owner_client,
        workspace.id,
        visibility="public",
        join_policy="invite_only",
    ).status_code == 200
    # The visibility change revokes rather than reinterpreting the unlisted
    # grant as indexed. A fresh explicit share is required.
    assert (
        anonymous.get(
            "/api/v1/public/w/profile-public/pages/index-page"
        ).status_code
        == 404
    )
    assert owner_client.post(
        f"/api/v1/workspaces/{page.workspace_id}/context/pages/{page.id}/publish",
        json={"is_public": True},
    ).status_code == 200

    profile = anonymous.get("/api/v1/public/w/profile-public")
    scoped = anonymous.get("/api/v1/public/w/profile-public/llms.txt")
    root = anonymous.get("/llms.txt")
    assert profile.status_code == scoped.status_code == root.status_code == 200
    assert "X-Robots-Tag" not in profile.headers
    assert "X-Robots-Tag" not in scoped.headers
    assert "X-Robots-Tag" not in root.headers
    assert profile.json()["data"]["workspace_id"] == str(workspace.id)
    assert "Index Page" in scoped.text

    with page_service.session_factory.begin() as session:
        second = Workspace(
            slug="another-public",
            title="Another Public",
            name="Another Public",
            visibility="public",
            join_policy="invite_only",
            created_by_actor_id=owner.id,
            owner_actor_id=owner.id,
            owner_user_id=owner.id,
        )
        session.add(second)
    assert anonymous.get("/llms.txt").status_code == 404
    assert anonymous.get("/api/v1/public/w/profile-public/llms.txt").status_code == 200
    assert anonymous.get("/api/v1/public/w/missing").status_code == 404

    # Public discovery never widens MCP/internal content access.
    with page_service.session_factory() as session:
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(requester.id),
            user_id=requester.id,
            token_kind="oauth_jwt",
            scopes=("mcp:read",),
            provenance="oauth_client",
        )
        listed = WorkspaceReadService().list_workspaces(session, actor=actor)
        assert workspace.id not in {item.id for item in listed.workspaces}

    token = requester_client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [
                {"workspace_id": str(requester.personal_workspace_id)}
            ],
            "name": "directory-denied",
            "scopes": ["mcp:read"],
        },
    ).json()["data"]["plain_token"]
    bearer = requester_client.get(
        "/api/v1/workspace-directory",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    assert bearer.json()["error_code"] == "scope_denied"


def test_workspace_visibility_database_check_is_fail_closed(session_factory):
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.add(
            Workspace(
                slug=f"invalid-{uuid4().hex[:8]}",
                title="Invalid",
                name="Invalid",
                visibility="listed",
            )
        )
