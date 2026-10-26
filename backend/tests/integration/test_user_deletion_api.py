from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.container import BlobStoreProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models import (
    ApiToken,
    AppMetadata,
    ContextMediaAsset,
    ContextMediaUsage,
    ContextPage,
    ContextPageVersion,
    User,
    UserFavorite,
    Workspace,
    WorkspaceCredentialGrant,
    WorkspaceMembership,
    WorkspaceTreeNode,
)
from backend.services.credential_hashing import CredentialHasher
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
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
    with factory.begin() as session:
        grant_server_admin(session, admin.user_id)
        personal_id = session.get(User, target.user_id).personal_workspace_id
        shared = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        shared_id = shared.id
        session.add(
            WorkspaceMembership(
                user_id=target.user_id, workspace_id=shared_id, role="owner"
            )
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
    assert (
        admin_client.patch(
            "/api/v1/admin/user-deletion-policy", json={"retention_days": 1}
        ).status_code
        == 401
    )
    assert (
        admin_client.post(
            "/api/v1/auth/reauthenticate", json={"current_password": "admin-password"}
        ).status_code
        == 200
    )
    assert (
        admin_client.patch(
            "/api/v1/admin/user-deletion-policy", json={"retention_days": 1}
        ).json()["data"]["retention_days"]
        == 1
    )
    assert user_client.get("/api/v1/users/me/deletion-policy").json()["data"] == {
        "retention_days": 1
    }

    deletion_body = {
        "confirmation_email": target.email,
        "current_password": "user-password",
        "expected_retention_days": 1,
    }
    blocked = user_client.request("DELETE", "/api/v1/users/me", json=deletion_body)
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "user_deletion_last_owner"
    with factory.begin() as session:
        session.add(
            WorkspaceMembership(
                user_id=admin.user_id, workspace_id=shared_id, role="owner"
            )
        )
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
    scheduled = response.json()["data"]
    deadline = datetime.fromisoformat(scheduled["purge_after"])
    assert deadline - datetime.fromisoformat(scheduled["requested_at"]) == timedelta(
        days=1
    )
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
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"] == scheduled
    # The existing operator disable command cannot cancel scheduled cleanup.
    assert users.deactivate_user(email=target.email) is False
    with factory() as session:
        assert session.get(User, target.user_id).status == "deletion_pending"
    # A shared Personal scope may project an active co-owner during retention;
    # its durable personal_workspace_id remains the cleanup ownership anchor.
    with factory.begin() as session:
        session.add(
            WorkspaceMembership(
                user_id=admin.user_id, workspace_id=personal_id, role="owner"
            )
        )
        session.get(Workspace, personal_id).owner_user_id = admin.user_id

    store = FilesystemLibraryBlobStore(root=tmp_path)
    stored = store.put_bytes(
        b"retained personal content", workspace_id=personal_id, max_bytes=1024
    )
    provider = BlobStoreProvider(None, store=store)
    service = UserDeletionService(
        factory,
        context_media_repository_factory=(
            app.state.container.get_services().context_media_repository_factory
        ),
    )
    assert (
        service.purge_due(
            blob_store_provider=provider, now=deadline - timedelta(seconds=1)
        )
        == 0
    )
    with factory() as session:
        assert session.get(User, target.user_id) is not None
        assert session.get(Workspace, personal_id) is not None
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
        assert session.get(ContextPage, page_id) is not None
        retained_version = session.get(ContextPageVersion, version_id)
        assert retained_version.body_md == "Retained team documentation"
        assert retained_version.actor_display_name is None
        private_version = session.get(ContextPageVersion, private_version_id)
        assert private_version.version_status == "discarded"
        assert private_version.body_md == ""
        assert session.get(ContextPage, page_id).current_version_id == version_id
        assert session.get(ContextMediaAsset, (shared_id, private_asset_id)) is None
        private_page = session.get(ContextPage, private_page_id)
        assert private_page.lifecycle_status == "archived"
        private_node = session.get(WorkspaceTreeNode, private_node_id)
        assert private_node.title == private_page.title != "Private title"
        assert private_node.slug == private_page.slug != "private-title"
        assert session.get(Workspace, shared_id) is not None
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
