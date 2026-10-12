"""Workspace and server administrators can inspect orphaned Context media.

Mirrors test_job_visibility_api.py's isolation pattern: an outsider must be
refused, and the refusal must come from the endpoint's own membership
dependency, not only from routing -- so the test also asserts the dependency
annotation is still present on the endpoint function (removing it would
still 404 at the router layer and this test would keep passing for the
wrong reason).
"""

import inspect
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.api import admin as admin_api
from backend.api import read as read_api
from backend.api.deps import WorkspaceAdminAccessDep
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.context_media.adapters.sqlalchemy import SqlAlchemyContextMediaRepository
from backend.context_media.application import RegisterMediaAsset
from backend.context_media.domain import ActorIdentity, RegisterContextMediaAsset
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.user import User
from backend.services import server_admin_service
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)

# Old uploads remain protected for as long as their own version is editing.
NOW_ISO = "2020-01-01T00:00:00+00:00"


def make_client(page_service, email: str, *, role: str = "owner") -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        workspace_slug="default",
        role=role,
    )
    return client


def make_server_admin_client(page_service, email: str) -> TestClient:
    client = make_client(page_service, email=email)
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        server_admin_service.grant_server_admin(session, user.id, granted_by="test")
    return client


def _workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _seed_page_bound_orphan(
    page_service, workspace_id, *, created_by_actor_id: str | None = None,
    title: str = "Orphan page", editing: bool = False
) -> None:
    """Register against a real editing row, then optionally submit it."""
    from datetime import datetime

    now = datetime.fromisoformat(NOW_ISO)
    created_by_actor_id = created_by_actor_id or str(uuid4())
    with page_service.session_factory.begin() as session:
        page = ContextPage(
            id=uuid4(),
            workspace_id=workspace_id,
            slug=f"orphan-api-{uuid4().hex[:8]}",
            title=title,
            current_version_id=None,
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="",
            content_hash="0" * 64,
            actor_type="human",
            actor_id=None,
            version_status="editing",
            created_by_actor_kind="human",
            created_by_actor_id=created_by_actor_id,
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        session.flush()

        if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
            session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=now,
                )
            )
            session.flush()
        blob_id = uuid4()
        session.add(
            StoredBlobRow(
                workspace_id=workspace_id, id=blob_id, sha256="d" * 64, next_epoch=2
            )
        )
        session.flush()
        session.add(
            BlobIncarnationRow(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=1,
                byte_size=68,
                storage_key=f"w/{workspace_id.hex}/a/{blob_id.hex}",
                state="available",
                publish_generation=1,
                security_generation=0,
                gc_generation=0,
                available_at=now,
            )
        )
        session.flush()
        inspection_id = uuid4()
        session.add(
            StoredBlobInspectionRow(
                workspace_id=workspace_id,
                id=inspection_id,
                blob_id=blob_id,
                blob_epoch=1,
                registry_version="registry-v1",
                detector_id="png-magic",
                detector_version="1",
                format_id="png",
                canonical_media_type="image/png",
                validation_schema_version="png-v1",
                metadata_json={},
                scanner_verdict="not_configured",
                scanner_provider=None,
                scanner_version=None,
                ordinary_available=True,
                created_at=now,
            )
        )
        session.flush()
        repository = SqlAlchemyContextMediaRepository(session)
        actor = ActorIdentity("human", created_by_actor_id)
        RegisterMediaAsset().execute(
            repository,
            RegisterContextMediaAsset(
                workspace_id=workspace_id,
                asset_id=uuid4(),
                page_id=page.id,
                created_for_version_id=version.id,
                blob=BlobReference(workspace_id, blob_id, BlobEpoch(1)),
                inspection_id=inspection_id,
                original_filename="orphan.png",
                canonical_media_type="image/png",
                logical_bytes=68,
                policy_revision="context-mvp-v1",
                actor=actor,
            ),
            now=now,
        )
        if not editing:
            version.version_status = "draft"
            page.latest_draft_version_id = version.id


def test_workspace_orphan_listing_requires_admin_role(page_service):
    from uuid import UUID

    editor = make_client(page_service, "orphans-editor@example.com", role="editor")
    workspace_id = _workspace_id(editor)
    _seed_page_bound_orphan(page_service, UUID(workspace_id))

    response = editor.get(f"/api/v1/workspaces/{workspace_id}/context/media/orphans")
    assert response.status_code == 403, response.text


def test_workspace_orphan_listing_hides_from_outsider(page_service):
    from uuid import UUID

    owner = make_client(page_service, "orphans-owner@example.com")
    owner_workspace = _workspace_id(owner)
    _seed_page_bound_orphan(page_service, UUID(owner_workspace))

    outsider = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(outsider, email="orphans-outsider@example.com")
    assert _workspace_id(outsider) != owner_workspace

    response = outsider.get(
        f"/api/v1/workspaces/{owner_workspace}/context/media/orphans"
    )
    assert response.status_code in {403, 404}, response.text
    assert "orphan.png" not in response.text

    # As in test_job_visibility_api.py: prove the refusal came from the
    # endpoint's own membership dependency, not only the router's 404 for an
    # unresolvable workspace slug, so this test cannot pass merely because
    # the inner guard was silently removed.
    signature = inspect.signature(read_api.list_workspace_context_media_orphans)
    assert any(
        parameter.annotation is WorkspaceAdminAccessDep
        for parameter in signature.parameters.values()
    ), "list_workspace_context_media_orphans must keep its own membership check"


def test_workspace_orphan_listing_returns_the_seeded_orphan(page_service):
    from uuid import UUID

    owner = make_client(page_service, "orphans-happy@example.com")
    workspace_id = _workspace_id(owner)
    _seed_page_bound_orphan(page_service, UUID(workspace_id))

    response = owner.get(f"/api/v1/workspaces/{workspace_id}/context/media/orphans")
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert "owner_kind" not in items[0]
    assert "session_id" not in items[0]
    assert items[0]["original_filename"] == "orphan.png"
    assert items[0]["workspace_id"] == workspace_id
    assert items[0]["page_id"] is not None
    assert items[0]["page_title"] == "Orphan page"
    # The seeded uploader is a random human actor id with no matching
    # `users` row: the display-name fallback must be explicit ("Human
    # editor"), not a raw uuid and not an omitted/blank field.
    assert items[0]["uploaded_by_display_name"] == "Human editor"


def test_workspace_orphan_listing_returns_page_title_and_uploader_display_name(
    page_service,
):
    """A page-bound orphan must carry a resolved page title (not just a
    page_id) and the real display name of the human who uploaded it (not
    just an actor id) -- both resolved server-side so the UI never has to
    render a raw internal identifier for either."""
    from uuid import UUID

    owner = make_client(page_service, "orphans-titled@example.com")
    workspace_id = _workspace_id(owner)
    with page_service.session_factory.begin() as session:
        uploader = session.scalar(
            select(User).where(User.email == "orphans-titled@example.com")
        )
        uploader_id = str(uploader.id)
        uploader_display_name = uploader.display_name

    _seed_page_bound_orphan(
        page_service,
        UUID(workspace_id),
        created_by_actor_id=uploader_id,
        title="Release Notes Draft",
    )

    response = owner.get(f"/api/v1/workspaces/{workspace_id}/context/media/orphans")
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert "owner_kind" not in items[0]
    assert "session_id" not in items[0]
    assert items[0]["page_title"] == "Release Notes Draft"
    assert items[0]["uploaded_by_display_name"] == uploader_display_name
    assert items[0]["uploaded_by_display_name"] != ""


def test_workspace_orphan_listing_excludes_a_live_in_flight_upload(page_service):
    """An old uninserted upload in an open editing row is never an orphan."""
    from uuid import UUID

    owner = make_client(page_service, "orphans-live@example.com")
    workspace_id = _workspace_id(owner)
    _seed_page_bound_orphan(
        page_service, UUID(workspace_id), editing=True
    )

    response = owner.get(f"/api/v1/workspaces/{workspace_id}/context/media/orphans")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"] == []


def test_admin_orphan_listing_requires_server_admin(page_service):
    from uuid import UUID

    member = make_client(page_service, "orphans-nonadmin@example.com")
    workspace_id = _workspace_id(member)
    _seed_page_bound_orphan(page_service, UUID(workspace_id))

    response = member.get(
        "/api/v1/admin/context-media/orphans", params={"workspace_id": workspace_id}
    )
    assert response.status_code in {401, 403}, response.text

    signature = inspect.signature(admin_api.admin_list_context_media_orphans)
    assert "require_server_admin" in repr(signature), (
        "admin_list_context_media_orphans must keep its require_server_admin gate"
    )


def test_admin_orphan_listing_returns_orphans_for_the_given_workspace(page_service):
    from uuid import UUID

    admin_client = make_server_admin_client(page_service, "orphans-admin@example.com")
    workspace_id = _workspace_id(admin_client)
    _seed_page_bound_orphan(page_service, UUID(workspace_id))

    response = admin_client.get(
        "/api/v1/admin/context-media/orphans", params={"workspace_id": workspace_id}
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert "owner_kind" not in items[0]
    assert "session_id" not in items[0]
