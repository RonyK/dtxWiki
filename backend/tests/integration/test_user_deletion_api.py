from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.container import BlobStoreProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models import (
    AccountInboxEntry,
    ApiToken,
    AppMetadata,
    ChangeSet,
    ContextMediaUsage,
    ContextPage,
    ContextPageVersion,
    McpChatSession,
    MCPClient,
    User,
    UserFavorite,
    Workspace,
    WorkspaceCredentialGrant,
    WorkspaceInboxEntry,
    WorkspaceMembership,
    WorkspaceTreeNode,
)
from backend.models.base import utc_now
from backend.schemas.context_write import ReviewDecisionRequest, WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.credential_hashing import CredentialHasher
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
from backend.services.review_service import ReviewService
from backend.services.server_admin_service import grant_server_admin
from backend.services.user_admin_service import UserAdminService
from backend.services.user_deletion_service import UserDeletionService


def test_withdrawal_retains_until_deadline_and_retries_blob_reclamation(
    page_service, tmp_path, monkeypatch
):
    factory = page_service.session_factory
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="user-deletion-integration-secret",
        session_cookie_secure=False,
        library_blob_root=str(tmp_path),
    )
    app = create_app(settings=settings, page_service=page_service)
    admin_client = TestClient(app)
    user_client = TestClient(app)
    users = UserAdminService(factory, CredentialHasher())
    admin = users.add_user(email="operator@example.com", password="admin-password")
    target = users.add_user(email="withdraw@example.com", password="user-password")
    dormant = users.add_user(email="dormant@example.com", password="dormant-password")
    colleague = users.add_user(email="colleague@example.com", password="colleague-pw")
    teamlead = users.add_user(email="teamlead@example.com", password="teamlead-pw")
    outsider = users.add_user(email="outsider@example.com", password="outsider-pw")
    with factory.begin() as session:
        grant_server_admin(session, admin.user_id)
        personal_id = _starter_workspace_id(session, target.user_id)
        admin_personal_id = _starter_workspace_id(session, admin.user_id)
        teamlead_personal_id = _starter_workspace_id(session, teamlead.user_id)
        inbox_workspace_id = _starter_workspace_id(session, outsider.user_id)
        # An Inbox-only footprint must be cleaned even without membership or
        # authored content there; other recipients keep their personal state.
        session.add_all(
            [
                WorkspaceInboxEntry(
                    workspace_id=inbox_workspace_id,
                    user_id=target.user_id,
                    kind="test.workspace",
                    item_key="notice",
                    mode="mark",
                    read_revision="v1",
                ),
                WorkspaceInboxEntry(
                    workspace_id=inbox_workspace_id,
                    user_id=outsider.user_id,
                    kind="test.workspace",
                    item_key="notice",
                    mode="mark",
                    read_revision="v1",
                ),
                AccountInboxEntry(
                    user_id=target.user_id,
                    kind="test.account",
                    item_key="notice",
                    mode="stored",
                    revision="v1",
                    event_at=utc_now(),
                ),
                AccountInboxEntry(
                    user_id=outsider.user_id,
                    kind="test.account",
                    item_key="notice",
                    mode="stored",
                    revision="v1",
                    event_at=utc_now(),
                ),
            ]
        )
        personal_title = session.get(Workspace, personal_id).title
        shared = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        shared_id = shared.id
        shared_title = shared.title
        client_id = session.scalar(
            select(MCPClient.id).where(MCPClient.workspace_id == shared_id)
        )
        shared_chat = McpChatSession(
            workspace_id=shared_id,
            client_id=client_id,
            owner_user_id=target.user_id,
            mcp_session_id="retained-team-conversation",
            title="Shared conversation",
            summary="Shared work must survive account removal",
        )
        session.add(shared_chat)
        session.flush()
        shared_chat_id = shared_chat.id
        session.add(
            WorkspaceMembership(
                user_id=target.user_id, workspace_id=shared_id, role="owner"
            )
        )
        session.add(
            WorkspaceMembership(
                user_id=colleague.user_id, workspace_id=shared_id, role="editor"
            )
        )
        # Only active members count: a disabled member does not keep the
        # leaver's own workspace from being held and deleted.
        session.add(
            WorkspaceMembership(
                user_id=dormant.user_id, workspace_id=personal_id, role="editor"
            )
        )
        team = Workspace(
            slug="team-held",
            title="Team held",
            name="Team held",
            visibility="team",
            join_policy="request",
        )
        session.add(team)
        session.flush()
        team_id = team.id
        session.add_all(
            [
                WorkspaceMembership(
                    user_id=teamlead.user_id, workspace_id=team_id, role="owner"
                ),
                WorkspaceMembership(
                    user_id=colleague.user_id, workspace_id=team_id, role="editor"
                ),
            ]
        )
        page = ContextPage(
            workspace_id=shared_id,
            slug="shared-history",
            title="Shared history",
            created_by_actor_type="human",
            created_by_actor_id=target.user_id,
        )
        session.add(page)
        session.flush()
        page_id = page.id
        version = ContextPageVersion(
            workspace_id=shared_id,
            page_id=page_id,
            version_no=1,
            body_md="Retained team documentation",
            actor_type="human",
            actor_id=target.user_id,
            actor_display_name=target.display_name,
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id=str(target.user_id),
            content_hash="shared-content-hash",
        )
        session.add(version)
        session.flush()
        version_id = version.id
        private_version = ContextPageVersion(
            workspace_id=shared_id,
            page_id=page_id,
            version_no=2,
            body_md="Private unsent edit",
            actor_type="human",
            actor_id=target.user_id,
            actor_display_name=target.display_name,
            created_by_actor_kind="human",
            created_by_actor_id=str(target.user_id),
            content_hash="private-content-hash",
            version_status="editing",
        )
        session.add(private_version)
        session.flush()
        private_version_id = private_version.id
        page.current_published_version_id = version_id
        page.latest_draft_version_id = private_version_id
        page.current_version_id = private_version_id
        page.lifecycle_status = "published"
        private_page = ContextPage(
            workspace_id=shared_id,
            slug="private-title",
            title="Private title",
            created_by_actor_type="human",
            created_by_actor_id=target.user_id,
        )
        session.add(private_page)
        session.flush()
        private_page_id = private_page.id
        private_page_version = ContextPageVersion(
            workspace_id=shared_id,
            page_id=private_page_id,
            version_no=1,
            body_md="Unsubmitted private document",
            actor_type="human",
            actor_id=target.user_id,
            created_by_actor_kind="human",
            created_by_actor_id=str(target.user_id),
            content_hash="private-document-hash",
            version_status="editing",
        )
        session.add(private_page_version)
        session.flush()
        private_page.current_version_id = private_page_version.id
        private_page.latest_draft_version_id = private_page_version.id
        private_node = WorkspaceTreeNode(
            workspace_id=shared_id,
            section="context",
            node_kind="context_page",
            target_kind="context_page",
            target_id=private_page_id,
            title="Private title",
            slug="private-title",
            sort_key="a0",
        )
        session.add(private_node)
        session.flush()
        private_node_id = private_node.id
        session.add(
            UserFavorite(
                user_id=target.user_id,
                workspace_id=shared_id,
                target_kind="context_page",
                target_id=page_id,
            )
        )
        # Published history in a workspace the user never joined still carries
        # the author's name and must be part of the cleanup footprint.
        foreign_page = ContextPage(
            workspace_id=admin_personal_id,
            slug="guest-contribution",
            title="Guest contribution",
            created_by_actor_type="human",
            created_by_actor_id=target.user_id,
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=admin_personal_id,
            page_id=foreign_page.id,
            version_no=1,
            body_md="Contributed before leaving",
            actor_type="human",
            actor_id=target.user_id,
            actor_display_name=target.display_name,
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id=str(target.user_id),
            content_hash="foreign-content-hash",
        )
        session.add(foreign_version)
        session.flush()
        foreign_version_id = foreign_version.id
    # The leaver reviewed a change and routed the fix to themself.
    reviews = ReviewService()
    with factory.begin() as session:
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=shared_id,
            request=WriteDraftRequest(
                title="Needs revision",
                page_type="overview",
                body_markdown="# Needs revision\n\nBody.",
                reason="seed review",
            ),
            actor_kind="human",
            actor_id=str(admin.user_id),
            actor_role="owner",
        )
        review_id = draft.review_item_id
        shared_change_id = draft.change_set_id
        session.get(ChangeSet, shared_change_id).origin_chat_session_id = shared_chat_id
        reviews.apply_decision(
            session=session,
            workspace_id=shared_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Please revise.",
                audience_kind="requester",
            ),
            actor_kind="human",
            actor_id=str(target.user_id),
            actor_role="owner",
            actor_provenance="browser_session",
        )

    assert (
        admin_client.post(
            "/api/v1/auth/login",
            json={"email": admin.email, "password": "admin-password"},
        ).status_code
        == 200
    )
    assert (
        user_client.post(
            "/api/v1/auth/login",
            json={"email": target.email, "password": "user-password"},
        ).status_code
        == 200
    )
    colleague_client = TestClient(app)
    outsider_client = TestClient(app)
    for client, account, password in (
        (colleague_client, colleague, "colleague-pw"),
        (outsider_client, outsider, "outsider-pw"),
    ):
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": account.email, "password": password},
            ).status_code
            == 200
        )
    assert users.deactivate_user(email=dormant.email) is True
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c"
        "020000000b4944415478da6364f80f00010501012718e3660000000049454e44"
        "ae426082"
    )
    uploaded = user_client.post(
        f"/api/v1/workspaces/{shared_id}/context/pages/{page_id}/media",
        data={
            "marker_uuid": str(uuid4()),
            "editing_version_id": str(private_version_id),
        },
        files={"file": ("private.png", png, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    private_asset_id = UUID(uploaded.json()["data"]["asset_id"])
    with factory.begin() as session:
        session.add(
            ContextMediaUsage(
                workspace_id=shared_id,
                page_id=page_id,
                asset_id=private_asset_id,
                page_version_id=private_version_id,
                reference_count=1,
            )
        )
    # A fresh password login counts as recent confirmation for admin actions.
    assert (
        admin_client.patch(
            "/api/v1/admin/user-deletion-policy", json={"retention_days": 1}
        ).json()["data"]["retention_days"]
        == 1
    )
    with factory() as session:
        shared_others = session.scalar(
            select(func.count())
            .select_from(WorkspaceMembership)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == shared_id,
                WorkspaceMembership.user_id != target.user_id,
                User.status == "active",
            )
        )
    personal_target = {
        "workspace_id": str(personal_id),
        "title": personal_title,
        "other_member_count": 0,
    }
    assert user_client.get("/api/v1/users/me/deletion-preview").json()["data"] == {
        "retention_days": 1,
        "target_workspaces": [personal_target],
        "blocking_workspaces": [
            {
                "workspace_id": str(shared_id),
                "title": shared_title,
                "other_member_count": shared_others,
            }
        ],
    }
    deletion_body = {
        "confirmation_email": target.email,
        "current_password": "user-password",
        "expected_retention_days": 1,
        "approved_workspaces": [
            {"workspace_id": str(personal_id), "other_member_count": 0}
        ],
    }
    # Other active members keep the shared workspace from being decided by the
    # leaver alone: withdrawal waits until it is deleted or has another admin.
    blocked = user_client.request("DELETE", "/api/v1/users/me", json=deletion_body)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error_code"] == "user_deletion_last_owner"
    assert [item["workspace_id"] for item in blocked.json()["blocking_workspaces"]] == [
        str(shared_id)
    ]
    assert (
        user_client.patch(
            f"/api/v1/workspaces/{shared_id}/members/{colleague.user_id}",
            json={"role": "owner"},
        ).status_code
        == 200
    )
    assert (
        user_client.get("/api/v1/users/me/deletion-preview").json()["data"][
            "blocking_workspaces"
        ]
        == []
    )
    stale = user_client.request(
        "DELETE",
        "/api/v1/users/me",
        json={**deletion_body, "approved_workspaces": []},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error_code"] == "user_deletion_targets_changed"

    issued = user_client.post(
        "/api/v1/auth/tokens",
        json={
            "name": "withdrawal key",
            "scopes": ["mcp:read"],
            "workspace_grants": [{"workspace_id": str(shared_id)}],
        },
    )
    assert issued.status_code == 201, issued.text

    response = user_client.request("DELETE", "/api/v1/users/me", json=deletion_body)
    assert response.status_code == 200, response.text
    with factory() as session:
        assert session.get(Workspace, personal_id).account_deletion_user_id == (
            target.user_id
        )
        assert session.get(Workspace, shared_id).account_deletion_user_id is None
    # The workspace owner can work on the routed change after withdrawal.
    with factory.begin() as session:
        claimed = reviews.claim_review(
            session=session,
            workspace_id=shared_id,
            review_item_id=review_id,
            actor_kind="human",
            actor_id=str(colleague.user_id),
            actor_role="owner",
        )
        assert claimed.claimed_by == colleague.user_id
        assert claimed.current_audience_kind == "workspace_owners"
    assert user_client.get("/api/v1/auth/me").status_code == 401
    assert (
        user_client.post(
            "/api/v1/auth/login",
            json={"email": target.email, "password": "user-password"},
        ).status_code
        == 403
    )
    with factory() as session:
        token = session.scalar(
            select(ApiToken).where(ApiToken.user_id == target.user_id)
        )
        assert token.revoked_at is not None
        replica = session.scalar(
            select(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.user_id == target.user_id
            )
        )
        assert replica.revoked_at is not None

    # Restoring returns the prior status; the user signs in again and may
    # request deletion anew.
    restored = admin_client.post(
        f"/api/v1/admin/users/{target.user_id}/deletion-restore"
    )
    assert restored.json()["data"] == {
        "user_id": str(target.user_id),
        "status": "active",
    }
    with factory() as session:
        assert session.get(Workspace, personal_id).account_deletion_user_id is None
    assert (
        user_client.post(
            "/api/v1/auth/login",
            json={"email": target.email, "password": "user-password"},
        ).status_code
        == 200
    )
    response = user_client.request("DELETE", "/api/v1/users/me", json=deletion_body)
    assert response.status_code == 200, response.text
    scheduled = response.json()["data"]
    deadline = datetime.fromisoformat(scheduled["purge_after"])
    assert deadline - datetime.fromisoformat(scheduled["requested_at"]) == timedelta(
        days=1
    )
    # Delete-then-restore cannot reactivate a disabled account.
    dormant_targets = admin_client.get(
        f"/api/v1/admin/users/{dormant.user_id}/deletion-preview"
    ).json()["data"]["target_workspaces"]
    assert (
        admin_client.request(
            "DELETE",
            f"/api/v1/admin/users/{dormant.user_id}",
            json={
                "confirmation_email": dormant.email,
                "expected_retention_days": 1,
                "approved_workspaces": [
                    {
                        "workspace_id": item["workspace_id"],
                        "other_member_count": item["other_member_count"],
                    }
                    for item in dormant_targets
                ],
            },
        ).status_code
        == 200
    )
    assert (
        admin_client.post(
            f"/api/v1/admin/users/{dormant.user_id}/deletion-restore"
        ).json()["data"]["status"]
        == "disabled"
    )

    # An admin repeat after a policy change keeps the already confirmed deadline.
    assert (
        admin_client.patch(
            "/api/v1/admin/user-deletion-policy", json={"retention_days": 0}
        ).status_code
        == 200
    )
    repeated = admin_client.request(
        "DELETE",
        f"/api/v1/admin/users/{target.user_id}",
        json={
            "confirmation_email": target.email,
            "expected_retention_days": 0,
            "approved_workspaces": [],
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"] == scheduled
    # The existing operator disable command cannot cancel scheduled cleanup.
    assert users.deactivate_user(email=target.email) is False
    with factory() as session:
        assert session.get(User, target.user_id).status == "deletion_pending"

    # A server administrator approves every workspace left without an admin,
    # members or not. Held workspaces close to their members and to joins
    # until an active admin is appointed.
    teamlead_preview = admin_client.get(
        f"/api/v1/admin/users/{teamlead.user_id}/deletion-preview"
    ).json()["data"]
    assert teamlead_preview["blocking_workspaces"] == []
    assert {
        (item["workspace_id"], item["other_member_count"])
        for item in teamlead_preview["target_workspaces"]
    } == {(str(team_id), 1), (str(teamlead_personal_id), 0)}
    assert (
        admin_client.request(
            "DELETE",
            f"/api/v1/admin/users/{teamlead.user_id}",
            json={
                "confirmation_email": teamlead.email,
                "expected_retention_days": 0,
                "approved_workspaces": [
                    {
                        "workspace_id": item["workspace_id"],
                        "other_member_count": item["other_member_count"],
                    }
                    for item in teamlead_preview["target_workspaces"]
                ],
            },
        ).status_code
        == 200
    )
    closed = colleague_client.get(f"/api/v1/workspaces/{team_id}/tree")
    assert closed.status_code == 403, closed.text
    assert closed.json()["error_code"] == "workspace_deletion_pending"
    join = outsider_client.post(f"/api/v1/workspaces/{team_id}/join-requests")
    assert join.status_code == 409, join.text
    assert join.json()["error_code"] == "workspace_deletion_pending"
    assert (
        admin_client.get(f"/api/v1/admin/workspaces/{shared_id}/members").json()[
            "error_code"
        ]
        == "workspace_not_deletion_pending"
    )
    appointed = admin_client.patch(
        f"/api/v1/admin/workspaces/{team_id}/members/{colleague.user_id}",
        json={"role": "owner"},
    )
    assert appointed.status_code == 200, appointed.text
    assert colleague_client.get(f"/api/v1/workspaces/{team_id}/tree").status_code == (
        200
    )

    store = FilesystemLibraryBlobStore(root=tmp_path)
    stored = store.put_bytes(
        b"retained personal content", workspace_id=personal_id, max_bytes=1024
    )
    provider = BlobStoreProvider(None, store=store)
    service = UserDeletionService(factory)
    assert (
        service.purge_due(
            blob_store_provider=provider, now=deadline - timedelta(seconds=1)
        )
        == 1
    )
    with factory() as session:
        assert session.get(User, teamlead.user_id) is None
        assert session.get(Workspace, teamlead_personal_id) is None
        assert session.get(Workspace, team_id).account_deletion_user_id is None
        assert session.get(User, target.user_id) is not None
        assert session.get(Workspace, personal_id) is not None
        assert (
            session.get(
                WorkspaceInboxEntry,
                (inbox_workspace_id, target.user_id, "test.workspace", "notice"),
            ).read_revision
            == "v1"
        )
        assert (
            session.get(
                AccountInboxEntry, (target.user_id, "test.account", "notice")
            ).revision
            == "v1"
        )
    assert store.path_for_key(stored.storage_key).exists()

    original_purge = store.purge_workspace

    def fail_purge(_workspace_id):
        raise OSError("temporary store outage")

    monkeypatch.setattr(store, "purge_workspace", fail_purge)
    assert service.purge_due(blob_store_provider=provider, now=deadline) == 0
    with factory() as session:
        assert session.get(User, target.user_id) is not None
        assert session.get(Workspace, personal_id) is None
        assert (
            session.scalar(
                select(AppMetadata).where(
                    AppMetadata.key == f"user_deletion_blob_pending:{target.user_id}"
                )
            )
            is not None
        )
    assert store.path_for_key(stored.storage_key).exists()

    monkeypatch.setattr(store, "purge_workspace", original_purge)
    assert service.purge_due(blob_store_provider=provider, now=deadline) == 1
    with factory() as session:
        assert session.get(User, target.user_id) is None
        assert session.get(User, admin.user_id) is not None
        assert (
            session.get(
                WorkspaceInboxEntry,
                (inbox_workspace_id, target.user_id, "test.workspace", "notice"),
            )
            is None
        )
        assert (
            session.get(AccountInboxEntry, (target.user_id, "test.account", "notice"))
            is None
        )
        assert (
            session.get(
                WorkspaceInboxEntry,
                (inbox_workspace_id, outsider.user_id, "test.workspace", "notice"),
            ).read_revision
            == "v1"
        )
        assert (
            session.get(
                AccountInboxEntry, (outsider.user_id, "test.account", "notice")
            ).revision
            == "v1"
        )
        assert session.get(ContextPage, page_id) is not None
        retained_version = session.get(ContextPageVersion, version_id)
        assert retained_version.body_md == "Retained team documentation"
        assert retained_version.actor_display_name is None
        foreign = session.get(ContextPageVersion, foreign_version_id)
        assert foreign.body_md == "Contributed before leaving"
        assert foreign.actor_display_name is None
        # Unsubmitted private edits nobody else could open are deleted.
        private_version = session.get(ContextPageVersion, private_version_id)
        assert private_version.version_status == "discarded"
        assert private_version.body_md == ""
        assert session.get(ContextPage, page_id).current_version_id == version_id
        assert (
            session.get(
                ContextMediaUsage,
                (shared_id, page_id, private_asset_id, private_version_id),
            )
            is None
        )
        private_page = session.get(ContextPage, private_page_id)
        assert private_page.lifecycle_status == "archived"
        private_node = session.get(WorkspaceTreeNode, private_node_id)
        assert private_node.title == private_page.title != "Private title"
        assert private_node.slug == private_page.slug != "private-title"
        assert session.get(Workspace, shared_id) is not None
        shared_chat = session.get(McpChatSession, shared_chat_id)
        assert shared_chat.owner_user_id is None
        assert shared_chat.title == "Shared conversation"
        assert shared_chat.summary == "Shared work must survive account removal"
        assert session.get(ChangeSet, shared_change_id).origin_chat_session_id == (
            shared_chat_id
        )
        assert (
            session.scalar(
                select(UserFavorite).where(UserFavorite.user_id == target.user_id)
            )
            is None
        )
        assert (
            session.scalar(select(ApiToken).where(ApiToken.user_id == target.user_id))
            is None
        )
        assert (
            session.scalar(
                select(WorkspaceCredentialGrant).where(
                    WorkspaceCredentialGrant.user_id == target.user_id
                )
            )
            is None
        )
    assert not store.path_for_key(stored.storage_key).exists()
    recreated = users.add_user(email=target.email, password="replacement-password")
    assert isinstance(recreated.user_id, UUID) and recreated.user_id != target.user_id


def test_accounts_without_password_or_email_confirm_deletion(page_service, tmp_path):
    factory = page_service.session_factory
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="user-deletion-integration-secret",
        session_cookie_secure=False,
        library_blob_root=str(tmp_path),
    )
    app = create_app(settings=settings, page_service=page_service)
    admin_client = TestClient(app)
    user_client = TestClient(app)
    users = UserAdminService(factory, CredentialHasher())
    admin = users.add_user(email="operator@example.com", password="admin-password")
    external = users.add_user(email="external@example.com", password="external-pw")
    other = users.add_user(email="other@example.com", password="other-password")
    with factory.begin() as session:
        grant_server_admin(session, admin.user_id)
    for client, account, password in (
        (admin_client, admin, "admin-password"),
        (user_client, external, "external-pw"),
    ):
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": account.email, "password": password},
            ).status_code
            == 200
        )
    # As if signed up through an external provider: no password, no email.
    with factory.begin() as session:
        for user_id in (external.user_id, other.user_id):
            account = session.get(User, user_id)
            account.password_hash = None
            account.email = None

    def approval(client, preview_path):
        preview = client.get(preview_path).json()["data"]
        return {
            "expected_retention_days": preview["retention_days"],
            "approved_workspaces": [
                {
                    "workspace_id": item["workspace_id"],
                    "other_member_count": item["other_member_count"],
                }
                for item in preview["target_workspaces"]
            ],
        }

    body = approval(user_client, "/api/v1/users/me/deletion-preview")
    # Without a password, withdrawal needs a recent account verification, which
    # an hour-old login no longer is.
    sessions = app.state.session_service
    primary = sessions.token_codec.verify_session(
        user_client.cookies.get("dtxwiki_session")
    )
    later = primary.authenticated_at + timedelta(seconds=3600)
    app.state.session_service = replace(
        sessions, token_codec=replace(sessions.token_codec, now=lambda: later)
    )
    aged = user_client.request("DELETE", "/api/v1/users/me", json=body)
    assert aged.status_code == 401, aged.text
    assert aged.json()["error_code"] == "recent_reauthentication_required"
    app.state.session_service = sessions
    withdrawn = user_client.request("DELETE", "/api/v1/users/me", json=body)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["data"]["status"] == "deletion_pending"

    # An administrator deletes an account without an email by its id alone.
    deleted = admin_client.request(
        "DELETE",
        f"/api/v1/admin/users/{other.user_id}",
        json=approval(
            admin_client, f"/api/v1/admin/users/{other.user_id}/deletion-preview"
        ),
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deletion_pending"


def _starter_workspace_id(session, user_id):
    # A new account owns exactly the one workspace it starts with.
    return session.scalar(
        select(WorkspaceMembership.workspace_id).where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.role == "owner",
        )
    )
