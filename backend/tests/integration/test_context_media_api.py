import os
import sys
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from backend.app.container import DatabaseProvider, assemble
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.context_media.domain import CONTEXT_MEDIA_UPLOAD_POLICY
from backend.converters.external_protocol import ExternalProcessConverter
from backend.media.domain import (
    MediaStagingBytesExceededError,
    MediaStagingLimitExceededError,
)
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUsage,
)
from backend.models.library import (
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.services.context_media_access import resolve_public_context_media_scope
from backend.services.context_media_upload import ContextMediaUploadService
from backend.services.library_ingest import LibraryUploadAdmissionService
from backend.services.public_page_exposure import grant_public_page_exposure
from backend.tests.conftest import authenticate_test_client_in_workspace

# 1x1 transparent PNG accepted by both Library and Context validators.
# Route-shape parameters only need a well-formed id that no row carries: the
# assertion is that admission rejects before the id is ever resolved. A fresh
# uuid4() per collection made the node ids differ between processes, so
# `--last-failed`, `-k` by node id, and xdist's collection check could not
# agree on what these tests are called.
_ABSENT_ID = "00000000-0000-4000-8000-000000000000"

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c"
    "020000000b4944415478da6364f80f00010501012718e3660000000049454e44"
    "ae426082"
)
SECOND_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63f8cfc0f01f00050001ff89993d1d000000004945"
    "4e44ae426082"
)

_CONVERTER_HEALTH_READY: set = set()
# Mirrors the inherited-environment allowlist in ``_run_bounded``.
_CONVERTER_ENV_NAMES = ("LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")


def _is_bundled_host(command) -> bool:
    return (
        len(command) == 5
        and command[0] == sys.executable
        and command[1:4] == ("-m", "backend.worker.converter_host", "--converter")
    )


@pytest.fixture
def cached_converter_health(monkeypatch):
    """Run each bundled converter's health handshake once per module and worker.

    Every per-test container builds a fresh ``ConverterRegistry`` whose first
    readiness check spawns the converter subprocess (~0.26 s). Tests that only
    consume the readiness result opt in; handshake, mismatch and failure
    behavior stays covered, uncached, by ``test_converter_adapters.py``. Only
    the first-party bundled host (``<python> -m backend.worker.converter_host
    --converter <id>``) is cached, and only a ready result is remembered, keyed
    by command, descriptor, timeout and the environment values the subprocess
    inherits. Every other provider runs the real healthcheck unchanged.
    """

    real = ExternalProcessConverter.healthcheck

    def _healthcheck(self, *, timeout_seconds=5.0):
        if not _is_bundled_host(self._command):
            return real(self, timeout_seconds=timeout_seconds)
        key = (
            self._command,
            self._descriptor,
            timeout_seconds,
            tuple(os.environ.get(name) for name in _CONVERTER_ENV_NAMES),
        )
        if key in _CONVERTER_HEALTH_READY:
            return True, None
        result = real(self, timeout_seconds=timeout_seconds)
        if result == (True, None):
            _CONVERTER_HEALTH_READY.add(key)
        return result

    monkeypatch.setattr(ExternalProcessConverter, "healthcheck", _healthcheck)


def _context_image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    mode = "RGB" if image_format == "WEBP" else "RGBA"
    options = {"lossless": True} if image_format == "WEBP" else {}
    Image.new(mode, (2, 2), "teal").save(output, format=image_format, **options)
    return output.getvalue()


GIF_BYTES = _context_image_bytes("GIF")
WEBP_BYTES = _context_image_bytes("WEBP")


def test_public_media_scope_uses_pinned_grant_and_revokes_on_new_unlisted_publish(
    page_service,
) -> None:
    created = page_service.write_page(
        workspace_slug="default",
        slug="unlisted-media-grant",
        title="Unlisted Media Grant",
        body_md="# Unlisted Media Grant",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        workspace = session.get(Workspace, page.workspace_id)
        workspace.visibility = "team"
        grant_public_page_exposure(
            page,
            scope="unlisted_web",
            version_id=created.version_id,
        )
    db = DatabaseProvider(None, session_factory=page_service.session_factory)
    scope = resolve_public_context_media_scope(
        db,
        workspace_slug="default",
        page_slug="unlisted-media-grant",
    )
    assert scope is not None
    assert scope.page_version_id == created.version_id
    assert scope.exposure_scope == "unlisted_web"

    updated = page_service.update_page(
        page_id=created.id,
        title="Unlisted Media Grant",
        body_md="# Unlisted Media Grant\n\nNew snapshot.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )
    assert updated.version_id != created.version_id
    assert (
        resolve_public_context_media_scope(
            db,
            workspace_slug="default",
            page_slug="unlisted-media-grant",
        )
        is None
    )


def _make_client(session_factory) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="context-media-test-secret",
    )
    container = assemble(settings, session_factory=session_factory)
    client = TestClient(create_app(settings=settings, container=container))
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="context-media-owner@example.com",
        workspace_slug="default",
    )
    return client


def _make_signup_client(session_factory) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="context-media-signup-test-secret",
    )
    container = assemble(settings, session_factory=session_factory)
    return TestClient(create_app(settings=settings, container=container))


def _workspace_id(client: TestClient) -> UUID:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200, response.text
    return UUID(response.json()["data"]["current_workspace_id"])


def _pages_url(client: TestClient) -> str:
    """P9.1: the canonical, workspace-scoped pages collection URL."""

    return f"/api/v1/workspaces/{_workspace_id(client)}/context/pages"


def _create_editing_page(
    client: TestClient,
    workspace_id: UUID,
    *,
    workspace_slug: str = "default",
) -> dict:
    """Page-first creation: the only live path that reaches "editing" status.

    No live path reopens a fresh "editing" session on an already-existing
    page (that was the deleted begin_editing bridge's job, and it had no
    real caller) -- every test here reaches a specific, predictable page
    identity by reading it back off this call's own response (e.g. `slug`)
    instead of asking for one by name.
    """
    response = client.post(f"/api/v1/workspaces/{workspace_id}/context/pages", json={})
    assert response.status_code == 201, response.text
    page = response.json()["data"]
    assert page["workspace_id"] == str(workspace_id)
    assert page["version_status"] == "editing"
    return page


def test_context_media_capabilities_publish_central_upload_policy(
    session_factory,
) -> None:
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/media/capabilities"
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "policy_revision": CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision,
        "uploadable_media_types": list(
            CONTEXT_MEDIA_UPLOAD_POLICY.uploadable_media_types
        ),
        "max_upload_bytes": CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes,
        "max_files_per_batch": CONTEXT_MEDIA_UPLOAD_POLICY.max_files_per_batch,
        "max_batch_bytes": CONTEXT_MEDIA_UPLOAD_POLICY.max_batch_bytes,
        "max_parallel_uploads": CONTEXT_MEDIA_UPLOAD_POLICY.max_parallel_uploads,
    }


@pytest.mark.usefixtures("cached_converter_health")
def test_new_signup_personal_workspace_accepts_first_context_upload(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_signup_client(session_factory)

    signup = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "first-context-upload@example.com",
            "password": "correct-password",
        },
    )
    assert signup.status_code == 201, signup.text
    workspace_id = _workspace_id(client)
    with session_factory() as session:
        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        assert intent.state == "open"
        assert intent.delete_generation == 0
        workspace_slug = session.get(Workspace, workspace_id).slug
    editing_page = _create_editing_page(
        client, workspace_id, workspace_slug=workspace_slug
    )

    uploaded = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=uuid4(),
    )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["data"]["mime"] == "image/png"


@pytest.mark.parametrize(
    "path",
    (
        pytest.param(
            f"/api/v1/workspaces/{_ABSENT_ID}/context/pages/{_ABSENT_ID}/media",
            id="page-media",
        ),
    ),
)
def test_context_media_upload_routes_reject_oversized_length_before_handler(
    session_factory,
    monkeypatch,
    path,
) -> None:
    client = _make_client(session_factory)
    register_called = False

    def unexpected_register(*args, **kwargs):
        del args, kwargs
        nonlocal register_called
        register_called = True
        raise AssertionError("upload handler ran before raw request admission")

    monkeypatch.setattr(ContextMediaUploadService, "register", unexpected_register)
    response = client.post(
        path,
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=not-read",
            "Content-Length": str(
                CONTEXT_MEDIA_UPLOAD_POLICY.max_request_body_bytes + 1
            ),
        },
    )

    assert response.status_code == 413, response.text
    assert response.json()["error_code"] == "media_too_large"
    assert register_called is False


def _upload_png(
    client: TestClient,
    *,
    page: dict,
    filename: str,
) -> dict:
    response = client.post(
        f"{_pages_url(client)}/{page['id']}/media",
        data={"marker_uuid": str(uuid4()), "editing_version_id": page["version_id"]},
        files={"file": (filename, PNG_BYTES, "image/png")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _upload_editing_png(
    client: TestClient,
    *,
    page: dict,
    marker_uuid: UUID,
    content: bytes = PNG_BYTES,
    media_type: str = "image/png",
) -> object:
    return client.post(
        f"{_pages_url(client)}/{page['id']}/media",
        data={
            "marker_uuid": str(marker_uuid),
            "editing_version_id": page["version_id"],
        },
        files={"file": ("canonical.png", content, media_type)},
    )


@pytest.mark.parametrize(
    ("error_type", "status_code", "retry_after"),
    (
        (MediaStagingLimitExceededError, 429, "60"),
        (MediaStagingBytesExceededError, 413, None),
    ),
)
def test_staging_failures_share_one_contract_across_canonical_upload_surfaces(
    session_factory,
    tmp_path,
    monkeypatch,
    error_type,
    status_code,
    retry_after,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    editing_page = _create_editing_page(client, workspace_id)

    def reject_context_upload(*args, **kwargs):
        del args, kwargs
        raise error_type

    async def reject_library_upload(*args, **kwargs):
        del args, kwargs
        raise error_type

    monkeypatch.setattr(
        ContextMediaUploadService,
        "register",
        reject_context_upload,
    )
    monkeypatch.setattr(
        LibraryUploadAdmissionService,
        "admit",
        reject_library_upload,
    )

    canonical = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=uuid4(),
    )
    library = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("library.png", PNG_BYTES, "image/png")},
    )

    for response in (canonical, library):
        assert response.status_code == status_code, response.text
        assert response.json()["error_code"] == error_type.error_code
        assert response.headers.get("Retry-After") == retry_after


def test_canonical_context_media_editing_upload_is_idempotent_and_format_complete(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    editing_page = _create_editing_page(client, workspace_id)
    marker = uuid4()

    first = _upload_editing_png(
        client, page=editing_page, marker_uuid=marker
    )
    assert first.status_code == 200, first.text
    payload = first.json()["data"]
    assert payload == {
        "asset_id": str(marker),
        "logical_ref": f"dtx-media://asset/{marker}",
        "content_url": (
            f"/api/v1/workspaces/{workspace_id}/context/pages/{editing_page['id']}"
            f"/versions/{editing_page['version_id']}/media/{marker}/content"
        ),
        "mime": "image/png",
        "bytes": len(PNG_BYTES),
        "sha256": __import__("hashlib").sha256(PNG_BYTES).hexdigest(),
    }
    retry = _upload_editing_png(
        client, page=editing_page, marker_uuid=marker
    )
    assert retry.status_code == 200
    assert retry.json()["data"] == payload

    for media_type, content, expected_format_id in (
        ("image/gif", GIF_BYTES, "gif"),
        ("image/webp", WEBP_BYTES, "webp"),
    ):
        image_marker = uuid4()
        uploaded = _upload_editing_png(
            client,
            page=editing_page,
            marker_uuid=image_marker,
            content=content,
            media_type=media_type,
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["data"]["mime"] == media_type
        with session_factory() as session:
            stored_asset = session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": image_marker},
            )
            assert stored_asset is not None
            inspection = session.get(
                StoredBlobInspectionRow,
                {
                    "workspace_id": workspace_id,
                    "id": stored_asset.inspection_id,
                },
            )
            assert inspection is not None
            assert inspection.format_id == expected_format_id
            assert inspection.registry_version == (
                CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision
            )
            assert inspection.validation_schema_version == (
                CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision
            )
            assert inspection.metadata_json["context_policy_revision"] == (
                CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision
            )
            assert inspection.metadata_json["context_image_frame_count"] == 1
            assert inspection.metadata_json["context_image_total_pixels"] == 4

    unsupported = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=uuid4(),
        content=b"II*\x00-not-enabled",
        media_type="image/tiff",
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["envelope"]["error_code"] == ("media_type_unsupported")


def test_context_logical_quota_rejection_keeps_installed_orphan_accounted(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    editing_page = _create_editing_page(client, workspace_id)
    first_marker = uuid4()

    first = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=first_marker,
    )
    assert first.status_code == 200, first.text
    with session_factory.begin() as session:
        usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        assert usage is not None and usage.charged_bytes == len(PNG_BYTES)
        usage.limit_bytes = usage.charged_bytes

    retry = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=first_marker,
    )
    assert retry.status_code == 200, retry.text

    with session_factory() as session:
        counts_before = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                StoredBlobRow,
                BlobIncarnationRow,
                StoredBlobInspectionRow,
                MediaPhysicalCharge,
                MediaStagingLease,
                MediaInstallRequest,
                MediaOutboxRow,
                ContextMediaAsset,
                ContextMediaLogicalCharge,
            )
        }
    blob_root = tmp_path / "library"
    files_before = {
        path.relative_to(blob_root).as_posix()
        for path in blob_root.rglob("*")
        if path.is_file()
    }

    rejected = _upload_editing_png(
        client,
        page=editing_page,
        marker_uuid=uuid4(),
        content=SECOND_PNG_BYTES,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error_code"] == "media_quota_exceeded"

    with session_factory() as session:
        usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        assert usage is not None and usage.charged_bytes == len(PNG_BYTES)
        physical_usage = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assert physical_usage is not None
        assert physical_usage.charged_bytes == len(PNG_BYTES) + len(SECOND_PNG_BYTES)
        counts_after = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                StoredBlobRow,
                BlobIncarnationRow,
                StoredBlobInspectionRow,
                MediaPhysicalCharge,
                MediaStagingLease,
                MediaInstallRequest,
                MediaOutboxRow,
                ContextMediaAsset,
                ContextMediaLogicalCharge,
            )
        }
    expected_deltas = {
        "stored_blob": 1,
        "blob_incarnation": 1,
        "stored_blob_inspection": 1,
        "media_physical_charge": 2,
        "media_staging_lease": 1,
        "media_install_request": 1,
        "media_outbox": 2,
        "context_media_asset": 0,
        "context_media_logical_charge": 0,
    }
    assert {
        table: counts_after[table] - counts_before[table] for table in counts_before
    } == expected_deltas
    files_after = {
        path.relative_to(blob_root).as_posix()
        for path in blob_root.rglob("*")
        if path.is_file()
    }
    # Domain rejection does not activate an association, but the already
    # published immutable blob remains durably journaled and physically
    # charged so a reconciler/GC can remove it with a deletion receipt.
    assert len(files_after - files_before) == 1


def test_canonical_context_media_page_upload_authorizes_and_returns_version_preview(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    page = _create_editing_page(client, workspace_id)
    marker = uuid4()
    response = client.post(
        f"{_pages_url(client)}/{page['id']}/media",
        data={"marker_uuid": str(marker), "editing_version_id": page["version_id"]},
        files={"file": ("page.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["content_url"] == (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page['id']}"
        f"/versions/{page['version_id']}/media/{marker}/content"
    )
    assert "media_session_id" not in response.json()["data"]


def _publish_editing(client: TestClient, page: dict, *, body_md: str) -> dict:
    """Save, submit, (approve,) and publish a page-first "editing" page.

    P8.1: an "editing" row now stays "editing" through any number of
    autosaves (author-private, per P2.2/P2.6) -- only an explicit
    submit-for-review call flips it to "draft", so that step is required
    here before publish is reachable at all.
    """
    saved = _autosave(client, page, body_md=body_md)
    submitted_response = client.post(
        f"{_pages_url(client)}/{page['id']}/submit-for-review",
        json={
            "idempotency_key": f"submit-{uuid4()}",
            "version_id": saved["version_id"],
            "expected_editing_revision": saved["editing_revision"],
        },
    )
    assert submitted_response.status_code == 200, submitted_response.text
    saved = submitted_response.json()["data"]
    if saved.get("review_item_id") is not None:
        decision = client.post(
            f"/api/v1/workspaces/{_workspace_id(client)}/reviews/"
            f"{saved['review_item_id']}/decision",
            json={"decision_type": "approve"},
        )
        assert decision.status_code == 200, decision.text
    response = client.post(
        f"{_pages_url(client)}/{page['id']}/versions/{saved['version_id']}/publish",
        json={
            "reason": "media integration fixture",
            "publish_despite_findings": False,
            "expected_content_hash": saved["content_hash"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _submit_editing(client: TestClient, page: dict) -> dict:
    """Explicitly close ``page``'s own open "editing" row for review (P8.1)."""

    response = client.post(
        f"{_pages_url(client)}/{page['id']}/submit-for-review",
        json={
            "idempotency_key": f"submit-{uuid4()}",
            "version_id": page["version_id"],
            "expected_editing_revision": page["editing_revision"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _autosave(client: TestClient, page: dict, *, body_md: str) -> dict:
    response = client.post(
        f"{_pages_url(client)}/{page['id']}/autosave",
        json={
            "idempotency_key": f"autosave-{uuid4()}",
            "title": "Context Media",
            "body_md": body_md,
            "base_version_id": page["version_id"],
            "base_content_hash": page["content_hash"],
            "expected_editing_revision": page["editing_revision"],
            "session_version_id": page["version_id"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _version_content_url(client: TestClient, page: dict, asset: dict) -> str:
    return (
        f"{_pages_url(client)}/{page['id']}/versions/{page['version_id']}"
        f"/media/{asset['asset_id']}/content"
    )


@pytest.mark.usefixtures("cached_converter_health")
def test_context_media_http_vertical_editing_preview_public_and_delete(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    library_upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("library.png", PNG_BYTES, "image/png")},
    )
    assert library_upload.status_code == 201, library_upload.text
    library_version_id = UUID(library_upload.json()["data"]["current_version"]["id"])
    editing_page = _create_editing_page(client, workspace_id)
    used = _upload_png(
        client,
        page=editing_page,
        filename="used.png",
    )
    unused = _upload_png(
        client,
        page=editing_page,
        filename="unused.png",
    )

    with session_factory() as session:
        leases = session.scalars(select(MediaStagingLease)).all()
        installs = session.scalars(select(MediaInstallRequest)).all()
        charges = session.scalars(select(MediaPhysicalCharge)).all()
        incarnations = session.scalars(select(BlobIncarnationRow)).all()
        outbox_topics = session.scalars(
            select(MediaOutboxRow.topic).order_by(MediaOutboxRow.topic)
        ).all()
        quota = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        assets = session.scalars(
            select(ContextMediaAsset).order_by(ContextMediaAsset.id)
        ).all()
        library_version = session.get(LibraryItemVersion, library_version_id)
        context_logical_usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        context_logical_charges = session.scalars(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id
            )
        ).all()
        library_source_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": workspace_id, "quota_kind": "source"},
        )
        library_source_charges = session.scalars(
            select(LibraryLogicalCharge).where(
                LibraryLogicalCharge.workspace_id == workspace_id,
                LibraryLogicalCharge.quota_kind == "source",
            )
        ).all()

    # Every idempotency operation is durably bound before the digest fast path.
    # The Library install owns the physical staging charge; the two Context
    # operations reuse that digest and retain deleted, zero-byte operation
    # leases without duplicating the blob or its physical charge.
    assert len(leases) == 3
    assert {lease.state for lease in leases} == {"deleted"}
    assert len(installs) == 1 and installs[0].state == "available"
    assert len(incarnations) == 1 and incarnations[0].state == "available"
    assert sorted((charge.accounting_kind, charge.state) for charge in charges) == [
        ("available", "charged"),
        ("staging", "released"),
    ]
    assert quota is not None and quota.charged_bytes == len(PNG_BYTES)
    assert outbox_topics == ["BlobAvailable", "InstallRequested"]
    assert len(assets) == 2
    assert (
        context_logical_usage is not None
        and context_logical_usage.charged_bytes == 2 * len(PNG_BYTES)
    )
    assert len(context_logical_charges) == 2
    assert {charge.state for charge in context_logical_charges} == {"charged"}
    assert (
        library_source_usage is not None
        and library_source_usage.charged_bytes == len(PNG_BYTES)
    )
    assert len(library_source_charges) == 1
    assert library_source_charges[0].state == "charged"
    assert {(asset.blob_id, asset.blob_epoch) for asset in assets} == {
        (incarnations[0].blob_id, incarnations[0].blob_epoch)
    }
    assert library_version is not None
    assert (
        library_version.source_blob_id,
        library_version.source_blob_epoch,
    ) == (incarnations[0].blob_id, incarnations[0].blob_epoch)

    other_editor = TestClient(client.app)
    authenticate_test_client_in_workspace(
        other_editor,
        session_factory,
        email="context-media-other-editor@example.com",
        workspace_slug="default",
        role="editor",
    )

    editing_read = client.get(used["content_url"])
    assert editing_read.status_code == 200
    assert editing_read.content == PNG_BYTES
    assert editing_read.headers["cache-control"] == "no-store"
    assert used["sha256"] not in editing_read.headers["etag"]
    assert other_editor.get(used["content_url"]).status_code == 404

    page = _publish_editing(
        client,
        body_md=f"![diagram]({used['logical_ref']})",
        page=editing_page,
    )
    assert client.get(used["content_url"]).status_code == 200
    assert other_editor.get(unused["content_url"]).status_code == 404

    exact_url = _version_content_url(client, page, used)
    exact = client.get(exact_url)
    assert exact.status_code == 200
    assert exact.content == PNG_BYTES
    assert exact.headers["accept-ranges"] == "bytes"
    exact_etag = exact.headers["etag"]
    head = client.head(exact_url)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(PNG_BYTES))
    partial = client.get(exact_url, headers={"Range": "bytes=1-3"})
    assert partial.status_code == 206
    assert partial.content == PNG_BYTES[1:4]
    assert partial.headers["content-range"] == f"bytes 1-3/{len(PNG_BYTES)}"
    assert partial.headers["content-length"] == "3"
    partial_head = client.head(exact_url, headers={"Range": "bytes=-2"})
    assert partial_head.status_code == 206
    assert partial_head.content == b""
    assert partial_head.headers["content-range"] == (
        f"bytes {len(PNG_BYTES) - 2}-{len(PNG_BYTES) - 1}/{len(PNG_BYTES)}"
    )
    matching_if_range = client.get(
        exact_url,
        headers={"Range": "bytes=0-1", "If-Range": exact_etag},
    )
    assert matching_if_range.status_code == 206
    assert matching_if_range.content == PNG_BYTES[:2]
    stale_if_range = client.get(
        exact_url,
        headers={"Range": "bytes=0-1", "If-Range": '"stale"'},
    )
    assert stale_if_range.status_code == 200
    assert stale_if_range.content == PNG_BYTES
    unchanged = client.get(exact_url, headers={"If-None-Match": exact_etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    invalid_range = client.get(exact_url, headers={"Range": "bytes=4-2"})
    assert invalid_range.status_code == 416
    assert invalid_range.json()["error_code"] == "media_range_invalid"
    assert invalid_range.headers["content-range"] == f"bytes */{len(PNG_BYTES)}"

    viewer = TestClient(client.app)
    authenticate_test_client_in_workspace(
        viewer,
        session_factory,
        email="context-media-viewer@example.com",
        workspace_slug="default",
        role="viewer",
    )
    assert viewer.get(exact_url).status_code == 200

    draft = _autosave(
        client,
        page,
        body_md=f"![diagram]({used['logical_ref']})\n\nDraft only.",
    )
    draft_url = _version_content_url(client, draft, used)
    assert client.get(draft_url).status_code == 200
    assert viewer.get(draft_url).status_code == 404
    assert viewer.get(draft_url, headers={"Range": "bytes=4-2"}).status_code == 404

    unused_list = client.get(f"{_pages_url(client)}/{page['id']}/media?usage=unused")
    assert unused_list.status_code == 200, unused_list.text
    unused_assets = unused_list.json()["data"]
    assert [item["id"] for item in unused_assets] == [unused["asset_id"]]
    preview_url = unused_assets[0]["content_url"]
    assert client.get(preview_url).status_code == 200
    assert viewer.get(preview_url).status_code == 404

    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        context_page = session.get(ContextPage, UUID(page["id"]))
        assert workspace is not None and context_page is not None
        assert context_page.current_published_version_id is not None
        workspace.visibility = "public"
        grant_public_page_exposure(
            context_page,
            scope="indexed_web",
            version_id=context_page.current_published_version_id,
        )

    public_url = (
        f"/api/v1/public/w/default/pages/{page['slug']}/media/"
        f"{used['asset_id']}"
    )
    public_read = client.get(public_url)
    assert public_read.status_code == 200
    assert public_read.content == PNG_BYTES
    assert "X-Robots-Tag" not in public_read.headers
    public_etag = public_read.headers["etag"]
    public_partial = client.get(public_url, headers={"Range": "bytes=2-4"})
    assert public_partial.status_code == 206
    assert public_partial.content == PNG_BYTES[2:5]
    assert (
        client.get(
            public_url,
            headers={"If-None-Match": public_etag},
        ).status_code
        == 304
    )

    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        context_page = session.get(ContextPage, UUID(page["id"]))
        assert workspace is not None and context_page is not None
        assert context_page.current_published_version_id is not None
        workspace.visibility = "team"
        grant_public_page_exposure(
            context_page,
            scope="unlisted_web",
            version_id=context_page.current_published_version_id,
        )
    unlisted_read = client.get(public_url)
    assert unlisted_read.status_code == 200
    assert unlisted_read.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert unlisted_read.headers["Cache-Control"] == "no-store"

    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        context_page = session.get(ContextPage, UUID(page["id"]))
        assert workspace is not None and context_page is not None
        assert context_page.current_published_version_id is not None
        workspace.visibility = "public"
        grant_public_page_exposure(
            context_page,
            scope="indexed_web",
            version_id=context_page.current_published_version_id,
        )

    published = client.post(
        f"{_pages_url(client)}/{page['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "publish retained media",
            "publish_despite_findings": False,
            "expected_content_hash": draft["content_hash"],
        },
    )
    assert published.status_code == 200, published.text
    republished_read = client.get(public_url)
    assert republished_read.status_code == 200
    assert republished_read.content == PNG_BYTES
    assert republished_read.headers["etag"] != public_etag
    assert viewer.get(exact_url).status_code == 404
    assert client.get(exact_url).status_code == 200

    unused_public = public_url.replace(used["asset_id"], unused["asset_id"])
    unused_public_read = client.get(unused_public)
    assert unused_public_read.status_code == 404
    assert unused_public_read.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert unused_public_read.headers["Cache-Control"] == "no-store"

    refreshed_unused = client.get(
        f"{_pages_url(client)}/{page['id']}/media?usage=unused"
    ).json()["data"]
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/context/media/assets/{unused['asset_id']}",
        headers={"If-Match": refreshed_unused[0]["usage_token"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(preview_url).status_code == 404
    with session_factory() as session:
        context_logical_usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        context_logical_charges = session.scalars(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id
            )
        ).all()
    assert (
        context_logical_usage is not None
        and context_logical_usage.charged_bytes == len(PNG_BYTES)
    )
    assert {charge.state for charge in context_logical_charges} == {
        "charged",
        "released",
    }


def test_context_media_in_place_autosave_replaces_exact_version_usage(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    editing_page = _create_editing_page(client, workspace_id)
    asset_a = _upload_png(
        client,
        page=editing_page,
        filename="asset-a.png",
    )
    asset_b = _upload_png(
        client,
        page=editing_page,
        filename="asset-b.png",
    )
    created = _publish_editing(
        client,
        body_md=f"![a]({asset_a['logical_ref']})",
        page=editing_page,
    )

    first = _autosave(client, created, body_md=f"![b]({asset_b['logical_ref']})")
    second = _autosave(client, first, body_md=f"![a]({asset_a['logical_ref']})")
    assert second["version_id"] == first["version_id"]

    with session_factory() as session:
        usages = session.scalars(
            select(ContextMediaUsage).where(
                ContextMediaUsage.page_version_id == UUID(second["version_id"])
            )
        ).all()
    assert [(row.asset_id, row.reference_count) for row in usages] == [
        (UUID(asset_a["asset_id"]), 1)
    ]
    assert client.get(_version_content_url(client, second, asset_a)).status_code == 200
    assert client.get(_version_content_url(client, second, asset_b)).status_code == 404


def test_asset_uploaded_then_orphaned_by_discard_cannot_be_adopted_later(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    """P8.5: an upload registered against an "editing" row that is later
    DISCARDED (never published_editing_page's content, never given a body
    reference) must not be adoptable into some other, later version -- that
    would resurrect a session the author explicitly threw away. This is
    different from `test_context_media_in_place_autosave_replaces_exact_
    version_usage` above, where the creating row was PUBLISHED (a deliberate,
    successful close): reusing a dormant-but-published session's unused
    upload later is ordinary editorial reuse and must keep working."""

    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    editing_page = _create_editing_page(client, workspace_id)
    orphaned_asset = _upload_png(
        client,
        page=editing_page,
        filename="orphaned.png",
    )

    discard = client.post(
        f"{_pages_url(client)}/{editing_page['id']}/discard-edit",
        json={
            "idempotency_key": f"discard-{uuid4()}",
            "version_id": editing_page["version_id"],
        },
    )
    assert discard.status_code == 204, discard.text

    reopened = client.post(
        f"{_pages_url(client)}/{editing_page['id']}/begin-edit",
        json={"idempotency_key": f"begin-{uuid4()}"},
    )
    assert reopened.status_code == 200, reopened.text
    reopened_page = reopened.json()["data"]
    assert reopened_page["version_id"] != editing_page["version_id"]

    late_association = client.post(
        f"{_pages_url(client)}/{reopened_page['id']}/autosave",
        json={
            "idempotency_key": f"autosave-{uuid4()}",
            "title": "Late association",
            "body_md": f"![orphan]({orphaned_asset['logical_ref']})",
            "base_version_id": reopened_page["version_id"],
            "base_content_hash": reopened_page["content_hash"],
            "expected_editing_revision": reopened_page["editing_revision"],
            "session_version_id": reopened_page["version_id"],
        },
    )
    assert late_association.status_code == 409, late_association.text
    assert late_association.json()["error_code"] == "media_editing_not_open"


def test_security_blocker_hides_context_private_public_and_editing_content(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)

    page_editing = _create_editing_page(client, workspace_id)
    page_asset = _upload_png(
        client,
        page=page_editing,
        filename="page-blocked.png",
    )
    page = _publish_editing(
        client,
        body_md=f"![blocked]({page_asset['logical_ref']})",
        page=page_editing,
    )
    private_page = _create_editing_page(client, workspace_id)
    private_marker = uuid4()
    private_upload = _upload_editing_png(
        client,
        page=private_page,
        marker_uuid=private_marker,
        content=SECOND_PNG_BYTES,
    )
    assert private_upload.status_code == 200, private_upload.text
    private_asset = private_upload.json()["data"]

    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        context_page = session.get(ContextPage, UUID(page["id"]))
        assert workspace is not None and context_page is not None
        assert context_page.current_published_version_id is not None
        workspace.visibility = "public"
        grant_public_page_exposure(
            context_page,
            scope="indexed_web",
            version_id=context_page.current_published_version_id,
        )

    version_url = _version_content_url(client, page, page_asset)
    preview_url = page_asset["content_url"]
    public_url = (
        f"/api/v1/public/w/default/pages/{page['slug']}/media/"
        f"{page_asset['asset_id']}"
    )
    private_url = private_asset["content_url"]
    authorized_etags = {}
    for url in (version_url, preview_url, public_url, private_url):
        authorized = client.get(url)
        assert authorized.status_code == 200, url
        authorized_etags[url] = authorized.headers["etag"]

    with session_factory.begin() as session:
        for asset_id in (page_asset["asset_id"], private_asset["asset_id"]):
            asset = session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": UUID(asset_id)},
            )
            assert asset is not None
            session.add(
                SecurityBlockedBlobRow(
                    workspace_id=workspace_id,
                    blob_id=asset.blob_id,
                    blob_epoch=asset.blob_epoch,
                    generation=1,
                    state="quarantined",
                    reason_code="integration_test_block",
                )
            )

    for url, etag in authorized_etags.items():
        blocked_requests = (
            client.get(url),
            client.head(url),
            client.get(url, headers={"Range": "bytes=0-1"}),
            client.head(url, headers={"Range": "bytes=-2"}),
            client.get(url, headers={"Range": "bytes=4-2"}),
            client.get(url, headers={"If-None-Match": etag}),
            client.get(
                url,
                headers={"Range": "bytes=0-1", "If-Range": etag},
            ),
        )
        for blocked in blocked_requests:
            assert blocked.status_code == 404, (url, blocked.text)


def test_context_media_save_rolls_back_on_foreign_reference(
    session_factory, tmp_path, monkeypatch,
) -> None:
    # Claim no longer exists: a rejected save must preserve the existing
    # page, private editing row, upload ownership, and empty usage set.
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    page = _create_editing_page(client, workspace_id)
    uploaded = _upload_png(client, page=page, filename="rollback.png")
    rejected = client.post(
        f"{_pages_url(client)}/{page['id']}/autosave",
        json={"idempotency_key": "t-6",
            "title": "Context Media",
            "body_md": f"![foreign](dtx-media://asset/{uuid4()})",
            "base_version_id": page["version_id"],
            "base_content_hash": page["content_hash"],
            "expected_editing_revision": 1,
        },
    )
    assert rejected.status_code == 422, rejected.text
    with session_factory() as session:
        persisted_page = session.get(ContextPage, UUID(page["id"]))
        version = session.get(ContextPageVersion, UUID(page["version_id"]))
        asset = session.get(
            ContextMediaAsset, (workspace_id, UUID(uploaded["asset_id"]))
        )
        usages = session.scalars(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace_id
            )
        ).all()
    assert persisted_page is not None
    assert version is not None and version.version_status == "editing"
    assert version.body_md == page["body_md"]
    assert version.editing_revision == 1
    assert asset is not None and asset.page_id == UUID(page["id"])
    assert asset.created_for_version_id == version.id
    assert usages == []


def test_upload_rejects_closed_foreign_and_wrong_page_editing_versions(
    session_factory, tmp_path, monkeypatch,
) -> None:
    # Replaces session-claim conflicts with direct registration's version
    # ownership, scope, and open-state contract.
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    client = _make_client(session_factory)
    workspace_id = _workspace_id(client)
    page = _create_editing_page(client, workspace_id)
    other_page = _create_editing_page(client, workspace_id)
    other_editor = TestClient(client.app)
    authenticate_test_client_in_workspace(
        other_editor, session_factory, email="foreign-upload-editor@example.com",
        workspace_slug="default", role="editor",
    )
    foreign = _upload_editing_png(other_editor, page=page, marker_uuid=uuid4())
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error_code"] == "not_found"
    wrong_page = _upload_editing_png(
        client,
        page={**page, "version_id": other_page["version_id"]},
        marker_uuid=uuid4(),
    )
    assert wrong_page.status_code == 404, wrong_page.text
    assert wrong_page.json()["error_code"] == "not_found"
    # P8.1: an "editing" row only closes on an explicit submit -- an
    # ordinary autosave keeps it "editing" (author-private).
    saved = _autosave(client, page, body_md="# closed\n")
    submitted = client.post(
        f"{_pages_url(client)}/{page['id']}/submit-for-review",
        json={"idempotency_key": "t-7",
            "version_id": saved["version_id"],
            "expected_editing_revision": saved["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    closed = _upload_editing_png(client, page=page, marker_uuid=uuid4())
    assert closed.status_code == 409, closed.text
    assert closed.json()["error_code"] == "media_editing_not_open"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ContextMediaAsset)) == 0
        untouched = session.get(ContextPageVersion, UUID(other_page["version_id"]))
        assert untouched.version_status == "editing"


@pytest.mark.parametrize("close_version", [False, True])
def test_asset_delete_enforces_editing_author_before_usage_token(
    session_factory, tmp_path, monkeypatch, close_version,
) -> None:
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    owner = _make_client(session_factory)
    workspace_id = _workspace_id(owner)
    page = _create_editing_page(owner, workspace_id)
    asset = _upload_png(owner, page=page, filename="private.png")
    editor = TestClient(owner.app)
    authenticate_test_client_in_workspace(
        editor,
        session_factory,
        email="delete-other-editor@example.com",
        workspace_slug="default",
        role="editor",
    )
    url = (
        f"/api/v1/workspaces/{workspace_id}/context/media/assets/{asset['asset_id']}"
    )
    denied = editor.delete(url, headers={"If-Match": "stale"})
    assert denied.status_code == 403, denied.text
    assert denied.json()["error_code"] == "media_owner_mismatch"
    assert owner.get(asset["content_url"]).status_code == 200
    if close_version:
        # P8.1: an "editing" row only closes on an explicit submit -- an
        # ordinary autosave keeps it "editing" (author-private).
        saved = _autosave(owner, page, body_md="# closed\n")
        submitted = owner.post(
            f"{_pages_url(owner)}/{page['id']}/submit-for-review",
            json={"idempotency_key": "t-8",
                "version_id": saved["version_id"],
                "expected_editing_revision": saved["editing_revision"],
            },
        )
        assert submitted.status_code == 200, submitted.text
    listing = owner.get(f"{_pages_url(owner)}/{page['id']}/media?usage=unused")
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["data"]] == [asset["asset_id"]]
    token = listing.json()["data"][0]["usage_token"]
    deleting_actor = editor if close_version else owner
    deleted = deleting_actor.delete(url, headers={"If-Match": token})
    assert deleted.status_code == 200, deleted.text
    assert owner.get(asset["content_url"]).status_code == 404


def test_page_media_listing_hides_another_authors_private_editing_uploads(
    session_factory, tmp_path, monkeypatch,
) -> None:
    """P8.4: an "editing" row's uninserted uploads are private to its author.

    The page itself is legitimately readable by every editor here (it has a
    published body), so `require_page`'s role check passes and the listing is
    reached -- the asset row behind the owner's still-open, unsubmitted
    editing version is what must not appear. Its `original_filename` and the
    `created_for_version_id` embedded in `content_url` would otherwise hand a
    non-author both the private file's name and the exact editing version id
    that naming directly is required to 404 on.
    """

    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DTXWIKI_LIBRARY_BLOB_ROOT", str(tmp_path / "library"))
    owner = _make_client(session_factory)
    workspace_id = _workspace_id(owner)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner.post(
        pages_url,
        json={
            "title": "Shared Media Target",
            "body_md": "# Shared Media Target\n\nPublished body.",
        },
    )
    assert created.status_code == 201, created.text
    page_id = created.json()["data"]["id"]
    submitted = owner.post(
        f"{pages_url}/{page_id}/submit-for-review",
        json={
            "version_id": created.json()["data"]["version_id"],
            "expected_editing_revision": created.json()["data"]["editing_revision"],
            "idempotency_key": "t-9-submit",
        },
    )
    assert submitted.status_code == 200, submitted.text
    submitted_data = submitted.json()["data"]
    decided = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{submitted_data['review_item_id']}/decision",
        json={"decision_type": "approve"},
    )
    assert decided.status_code == 200, decided.text

    editing = owner.post(
        f"{pages_url}/{page_id}/begin-edit", json={"idempotency_key": "t-9"}
    )
    assert editing.status_code == 200, editing.text
    editing_page = editing.json()["data"]
    assert editing_page["version_status"] == "editing"
    media_url = f"{pages_url}/{page_id}/media"
    upload_response = owner.post(
        media_url,
        data={
            "marker_uuid": str(uuid4()),
            "editing_version_id": editing_page["version_id"],
        },
        files={"file": ("owner-private-secret.png", PNG_BYTES, "image/png")},
    )
    assert upload_response.status_code == 200, upload_response.text
    asset = upload_response.json()["data"]

    editor = TestClient(owner.app)
    authenticate_test_client_in_workspace(
        editor,
        session_factory,
        email="media-listing-other-editor@example.com",
        workspace_slug="default",
        role="editor",
    )

    owner_listing = owner.get(f"{media_url}?usage=unused")
    assert owner_listing.status_code == 200, owner_listing.text
    assert [item["id"] for item in owner_listing.json()["data"]] == [
        asset["asset_id"]
    ]

    foreign_listing = editor.get(f"{media_url}?usage=unused")
    assert foreign_listing.status_code == 200, foreign_listing.text
    foreign_items = foreign_listing.json()["data"]
    assert foreign_items == []
    serialized = foreign_listing.text
    assert "owner-private-secret.png" not in serialized
    assert editing_page["version_id"] not in serialized

    used_listing = editor.get(f"{media_url}?usage=used")
    assert used_listing.status_code == 200, used_listing.text
    assert used_listing.json()["data"] == []
    assert "owner-private-secret.png" not in used_listing.text

    # Bytes stay protected by the version-scoped gate, which is what makes the
    # listing (and the version id it discloses) the only leak here.
    assert editor.get(asset["content_url"]).status_code == 404
    assert owner.get(asset["content_url"]).status_code == 200
