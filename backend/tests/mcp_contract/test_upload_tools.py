from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

import anyio
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.container import DatabaseProvider, ToolCallContext, WorkspaceDirectory
from backend.context_media.domain.policy import MAX_CONTEXT_IMAGE_UPLOAD_BYTES
from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.router import dispatch, resolve_target_actor
from backend.mcp.upload_tools import upload_image
from backend.models.base import Base
from backend.models.context import ContextPage
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUploadRequest,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.mcp.upload_tools import UploadImageInput, decode_image_base64
from backend.services.context_image_ingest import (
    ContextImageIngestError,
    ContextImageIngestService,
)


def _image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=(24, 96, 160)).save(output, format=image_format)
    return output.getvalue()


PNG = _image_bytes("PNG")
JPEG = _image_bytes("JPEG")
GIF = _image_bytes("GIF")
WEBP = _image_bytes("WEBP")


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("draft-write",))


def _workspace_id(page_service) -> UUID:
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _open_editing_page(page_service, actor):
    """Create a fresh page-first page with its own open editing version.

    Every upload_image call now requires a real page_id and the caller's own
    open editing_version_id (P2/P3 page-first contract), so tests that need a
    real upload target open one through the same service-layer path
    (`create_page_first`) production page-first creation uses.
    """
    return page_service.create_page_first(
        workspace_slug="default",
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance="mcp_client",
    )


def _payload(
    workspace_id: UUID,
    content: bytes = PNG,
    *,
    page_id,
    editing_version_id,
    **overrides,
):
    payload = {
        "workspace_id": str(workspace_id),
        "filename": "diagram.png",
        "mime_type": "image/png",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "marker_uuid": str(uuid4()),
        "alt_text": "diagram [v1]",
        "page_id": str(page_id),
        "editing_version_id": str(editing_version_id),
    }
    payload.update(overrides)
    return payload


def _dispatch_upload(page_service, actor, payload):
    return dispatch(
        "upload_image",
        payload,
        actor=actor,
        page_service=page_service,
        conversation_token="upload-test-chat",
    )


@pytest.mark.parametrize(
    ("mime_type", "content"),
    (
        ("image/png", PNG),
        ("image/jpeg", JPEG),
        ("image/gif", GIF),
        ("image/webp", WEBP),
    ),
)
def test_upload_image_accepts_format_matrix(
    page_service, actor, tmp_path, monkeypatch, mime_type, content
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)

    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            workspace_id,
            content,
            mime_type=mime_type,
            page_id=editing_page.id,
            editing_version_id=editing_page.version_id,
        ),
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.mime_type == mime_type
    assert response.data.logical_ref == f"dtx-media://asset/{response.data.asset_id}"
    assert response.data.content_url == (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{editing_page.id}"
        f"/versions/{editing_page.version_id}/media/{response.data.asset_id}/content"
    )
    assert response.data.markdown == (
        f"![diagram \\[v1\\]]({response.data.logical_ref})"
    )
    dumped = response.model_dump(mode="json").get("data", {})
    assert "url" not in dumped
    assert "/api/v1/" not in response.data.markdown
    assert "resultType" not in dumped


@pytest.mark.parametrize(
    ("payload_patch", "expected_code"),
    (
        ({"filename": "vector.svg", "mime_type": "image/svg+xml"}, "validation_failed"),
        ({"mime_type": "image/jpeg"}, "media_content_mismatch"),
        ({"content_base64": ""}, "validation_failed"),
    ),
)
def test_upload_image_rejects_svg_mismatch_and_empty(
    page_service, actor, tmp_path, monkeypatch, payload_patch, expected_code
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    editing_page = _open_editing_page(page_service, actor)
    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            _workspace_id(page_service),
            page_id=editing_page.id,
            editing_version_id=editing_page.version_id,
            **payload_patch,
        ),
    )
    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == expected_code


def test_upload_image_decoded_size_boundary():
    workspace_id = uuid4()
    page_id = uuid4()
    editing_version_id = uuid4()
    at_limit = b"\x89PNG\r\n\x1a\n" + bytes(MAX_CONTEXT_IMAGE_UPLOAD_BYTES - 8)
    accepted = UploadImageInput.model_validate(
        {
            **_payload(
                workspace_id,
                at_limit,
                page_id=page_id,
                editing_version_id=editing_version_id,
            ),
            "conversation_token": "new",
        }
    )
    assert len(decode_image_base64(accepted.content_base64)) == (
        MAX_CONTEXT_IMAGE_UPLOAD_BYTES
    )

    over_limit = at_limit + b"x"
    encoded_over_limit = UploadImageInput.model_validate(
        {
            **_payload(
                workspace_id,
                over_limit,
                page_id=page_id,
                editing_version_id=editing_version_id,
            ),
            "conversation_token": "new",
        }
    )
    with pytest.raises(ValueError, match="decoded-byte limit"):
        decode_image_base64(encoded_over_limit.content_base64)


def _real_session_factory() -> sessionmaker:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_upload_image_uses_workspace_plane_not_control_plane(
    actor, tmp_path, monkeypatch
):
    """Context media assets and blobs are workspace-owned data.

    ContextMediaAsset carries a FK to ContextPage that cannot span databases,
    so ``upload_image``'s
    ingest write must land in ``request.workspace_id``'s plane, not the
    control plane. ``upload_image`` requires the 'editor' role, which makes
    ``backend.mcp.router`` bind ``ctx.workspace_id`` to that same value -- the
    ``ingest`` call site in upload_tools.py routes via
    ``ctx.db.session_for(request.workspace_id)``.

    D-1 already routes ``assert_page_not_archived``'s authorization read
    through ``session_for(workspace_id)`` -- ``ContextPage`` is a
    workspace-plane domain table, resolved through the directory on the
    control session but read for real on the workspace session. The
    control-plane page below is poisoned (``lifecycle_status="archived"``)
    while the workspace-plane page carries the real, published status: if
    ``assert_page_not_archived`` ever reverted to reading lifecycle_status
    off the single control session (the directory-then-route pattern
    undone), it would see the poisoned archived status and the upload would
    fail instead of succeeding.
    """

    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    control_factory = _real_session_factory()
    workspace_factory = _real_session_factory()

    workspace_id = uuid4()
    page_id = uuid4()
    editing_version_id = uuid4()
    with control_factory.begin() as session:
        session.add(
            ContextPage(
                id=page_id,
                workspace_id=workspace_id,
                slug="control-plane-page",
                title="Control Plane Page",
                page_type="overview",
                lifecycle_status="archived",
            )
        )
    with workspace_factory.begin() as session:
        session.add(
            ContextPage(
                id=page_id,
                workspace_id=workspace_id,
                slug="control-plane-page",
                title="Control Plane Page",
                page_type="overview",
                lifecycle_status="published",
            )
        )

    class _FakeDb:
        def control_session_factory(self):
            return control_factory

        def session_for(self, requested_workspace_id):
            assert requested_workspace_id == workspace_id
            return workspace_factory

    captured: dict[str, object] = {}

    class _FakeIngestService:
        def ingest(self, *, session_factory, command):
            captured["session_factory"] = session_factory
            asset_id = uuid4()
            version_id = uuid4()
            return SimpleNamespace(
                asset_id=asset_id,
                logical_ref=f"dtx-media://asset/{asset_id}",
                content_url=(
                    f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
                    f"/versions/{version_id}/media/{asset_id}/content"
                ),
                canonical_media_type="image/png",
                byte_size=len(PNG),
                sha256=hashlib.sha256(PNG).hexdigest(),
                reused=False,
            )

    ctx = ToolCallContext(
        actor=actor,
        db=_FakeDb(),
        page_service=None,
        conversation_token="new",
        services=SimpleNamespace(
            context_image_ingest=_FakeIngestService(),
            workspace_directory=WorkspaceDirectory(),
        ),
        workspace_id=workspace_id,
    )

    output = upload_image(
        _payload(
            workspace_id,
            PNG,
            page_id=page_id,
            editing_version_id=editing_version_id,
        ),
        ctx,
    )

    assert output.envelope.result_status == "succeeded", output.envelope
    assert captured["session_factory"] is workspace_factory
    assert captured["session_factory"] is not control_factory


def test_marker_idempotency_and_different_bytes_rejection(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)
    marker_uuid = uuid4()
    payload = _payload(
        workspace_id,
        marker_uuid=str(marker_uuid),
        page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
    )

    first = _dispatch_upload(page_service, actor, payload)
    replay = _dispatch_upload(page_service, actor, payload)
    conflict = _dispatch_upload(
        page_service,
        actor,
        _payload(
            workspace_id,
            JPEG,
            marker_uuid=str(marker_uuid),
            mime_type="image/jpeg",
            page_id=editing_page.id,
            editing_version_id=editing_page.version_id,
        ),
    )

    assert first.data.reused is False
    assert replay.data.reused is True
    assert replay.data.asset_id == first.data.asset_id
    assert conflict.envelope.error_code == "media_state_changed"
    assert conflict.envelope.result_status == "conflict"
    assert conflict.envelope.retryable is False
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count(ContextMediaUploadRequest.marker_uuid)).where(
                ContextMediaUploadRequest.workspace_id == workspace_id,
                ContextMediaUploadRequest.marker_uuid == marker_uuid,
            )
        ) == 1


def test_upload_image_rejects_cross_workspace_page(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        other = Workspace(slug="upload-other", title="Other", name="Other")
        session.add(other)
        session.flush()
        page = ContextPage(
            workspace_id=other.id,
            slug="other-page",
            title="Other Page",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        page_id = page.id

    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            workspace_id,
            page_id=page_id,
            editing_version_id=uuid4(),
        ),
    )
    assert response.envelope.error_code == "not_found"


def test_upload_image_rejects_missing_page_as_not_found(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            _workspace_id(page_service),
            page_id=uuid4(),
            editing_version_id=uuid4(),
        ),
    )

    assert response.envelope.error_code == "not_found"
    assert response.envelope.error_message == "page not found"


def test_upload_image_rejects_archived_page_as_not_found(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = ContextPage(
            workspace_id=workspace_id,
            slug="archived-upload-page",
            title="Archived Upload Page",
            page_type="overview",
            lifecycle_status="archived",
        )
        session.add(page)
        session.flush()
        page_id = page.id

    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            workspace_id,
            page_id=page_id,
            editing_version_id=uuid4(),
        ),
    )

    assert response.envelope.error_code == "not_found"
    assert response.envelope.error_message == "page not found"


def test_upload_image_rejects_visible_page_from_a_different_workspace(
    page_service, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editor_id = uuid4()
    with page_service.session_factory.begin() as session:
        other = Workspace(slug="upload-visible-other", title="Other", name="Other")
        session.add(other)
        session.flush()
        page = ContextPage(
            workspace_id=other.id,
            slug="visible-other-page",
            title="Visible Other Page",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(page)
        grant_workspace_membership(
            session,
            user_id=editor_id,
            workspace_id=workspace_id,
            role="editor",
        )
        grant_workspace_membership(
            session,
            user_id=editor_id,
            workspace_id=other.id,
            role="editor",
        )
        session.flush()
        page_id = page.id
    editor = ActorContext(
        actor_kind="human",
        actor_id=str(editor_id),
        user_id=editor_id,
        scopes=("draft-write",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
        token_kind="session_cookie",
    )

    response = _dispatch_upload(
        page_service,
        editor,
        _payload(
            workspace_id,
            page_id=page_id,
            editing_version_id=uuid4(),
        ),
    )

    assert response.envelope.error_code == "not_found"
    assert response.envelope.result_status == "failed"


def test_upload_image_page_target_binds_asset_to_the_open_editing_version(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)

    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            workspace_id,
            page_id=f"/p/{workspace_id}/{editing_page.id}",
            editing_version_id=editing_page.version_id,
        ),
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.content_url == (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{editing_page.id}"
        f"/versions/{editing_page.version_id}/media/{response.data.asset_id}/content"
    )
    assert response.data.markdown.endswith(f"({response.data.logical_ref})")
    assert "/api/v1/" not in response.data.markdown
    with page_service.session_factory() as session:
        asset = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": response.data.asset_id},
        )
        assert asset is not None
        assert asset.page_id == editing_page.id
        assert asset.created_for_version_id == editing_page.version_id


def test_upload_image_rejects_foreign_actor_editing_version_without_residue(
    page_service, actor, tmp_path, monkeypatch
):
    """The version's own author gets media_editing_not_open once it stops
    being "editing"; a DIFFERENT actor naming someone else's editing version
    must instead see the same opaque not_found a nonexistent version gets --
    a distinguishable answer would itself confirm this version id currently
    exists in someone's private editing session (P3.2 opacity rule, see
    backend/context_media/adapters/sqlalchemy.py's add_asset). This must also
    have zero side effects: no asset, no request row, no quota charge.
    """
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)
    foreign_actor = replace(actor, actor_id="foreign-session-editor")

    def snapshot():
        with page_service.session_factory() as session:
            quota = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
            return (
                session.scalar(
                    select(func.count(ContextMediaAsset.id)).where(
                        ContextMediaAsset.workspace_id == workspace_id
                    )
                ),
                session.scalar(
                    select(func.count(ContextMediaUploadRequest.marker_uuid)).where(
                        ContextMediaUploadRequest.workspace_id == workspace_id
                    )
                ),
                (quota.charged_bytes, quota.revision) if quota is not None else None,
            )

    before = snapshot()
    refused = _dispatch_upload(
        page_service,
        foreign_actor,
        _payload(
            workspace_id,
            JPEG,
            filename="foreign.jpg",
            mime_type="image/jpeg",
            page_id=editing_page.id,
            editing_version_id=editing_page.version_id,
        ),
    )

    assert refused.envelope.error_code == "not_found"
    assert refused.envelope.result_status == "failed"
    assert refused.envelope.retryable is False
    assert snapshot() == before


@pytest.mark.parametrize(
    ("error_code", "result_status", "retryable"),
    (
        ("validation_failed", "failed", False),
        ("media_type_unsupported", "failed", False),
        ("media_content_mismatch", "failed", False),
        ("media_too_large", "failed", False),
        ("not_found", "failed", False),
        ("media_policy_changed", "conflict", False),
        ("media_owner_mismatch", "rejected_by_policy", False),
        ("media_editing_not_open", "conflict", False),
        ("media_unavailable", "failed", True),
        ("media_quota_exceeded", "rejected_by_policy", False),
        ("media_staging_limit_exceeded", "failed", True),
        ("media_staging_bytes_exceeded", "failed", True),
        ("media_install_in_progress", "conflict", True),
        ("media_install_unavailable", "failed", True),
        ("media_state_changed", "conflict", False),
    ),
)
def test_upload_image_preserves_canonical_ingest_error_contract(
    page_service,
    actor,
    monkeypatch,
    error_code,
    result_status,
    retryable,
):
    editing_page = _open_editing_page(page_service, actor)

    def refuse(_service, *, session_factory, command):
        del session_factory, command
        raise ContextImageIngestError(error_code, f"simulated {error_code}")

    monkeypatch.setattr(ContextImageIngestService, "ingest", refuse)
    response = _dispatch_upload(
        page_service,
        actor,
        _payload(
            _workspace_id(page_service),
            page_id=editing_page.id,
            editing_version_id=editing_page.version_id,
        ),
    )

    assert response.envelope.error_code == error_code
    assert response.envelope.result_status == result_status
    assert response.envelope.retryable is retryable


def test_upload_image_scope_role_and_token_refusals(
    page_service, mcp_default_actor, session_factory
):
    workspace_id = _workspace_id(page_service)
    scope_denied = _dispatch_upload(
        page_service,
        mcp_default_actor(scopes=("mcp:read",)),
        _payload(workspace_id, page_id=uuid4(), editing_version_id=uuid4()),
    )
    assert scope_denied.envelope.error_code == "scope_denied"

    viewer_id = uuid4()
    with session_factory.begin() as session:
        grant_workspace_membership(
            session,
            user_id=viewer_id,
            workspace_id=workspace_id,
            role="viewer",
        )
    viewer = ActorContext(
        actor_kind="human",
        actor_id=str(viewer_id),
        user_id=viewer_id,
        scopes=("draft-write",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
        token_kind="session_cookie",
    )
    role_denied = resolve_target_actor(
        tool_name="upload_image",
        payload={"workspace_id": str(workspace_id)},
        actor=viewer,
        session_factory=session_factory,
        db=DatabaseProvider(None, session_factory=session_factory),
    )
    assert role_denied.error_code == "workspace_role_insufficient"

    async def run_without_token():
        return await call_tool_async(
            "upload_image",
            _payload(workspace_id, page_id=uuid4(), editing_version_id=uuid4()),
            actor=mcp_default_actor(scopes=("draft-write",)),
            page_service=page_service,
        )

    token_denied = anyio.run(run_without_token)
    assert token_denied.structuredContent["envelope"]["error_code"] == (
        "conversation_token_required"
    )


def test_failing_upload_redacts_trace_error_chat_params_and_audit(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)
    marker_uuid = uuid4()
    first_payload = _payload(
        workspace_id,
        marker_uuid=str(marker_uuid),
        page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
    )

    async def run(payload):
        return await call_tool_async(
            "upload_image",
            {**payload, "conversation_token": "new"},
            actor=actor,
            page_service=page_service,
        )

    succeeded = anyio.run(run, first_payload)
    token = succeeded.structuredContent["envelope"]["conversation_token"]
    failing_payload = _payload(
        workspace_id,
        JPEG,
        marker_uuid=str(marker_uuid),
        mime_type="image/jpeg",
        page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
    )
    secret = failing_payload["content_base64"]

    async def fail():
        return await call_tool_async(
            "upload_image",
            {
                **failing_payload,
                "conversation_token": token,
                # Raw JSON-RPC extras are visible to trace redaction before the
                # strict input model rejects them. They must never override the
                # fingerprint derived from content_base64.
                "bytes": 0,
                "sha256": "0" * 64,
            },
            actor=actor,
            page_service=page_service,
        )

    failed = anyio.run(fail)
    assert failed.isError is True
    assert secret not in json.dumps(failed.structuredContent)
    with page_service.session_factory() as session:
        traces = session.scalars(
            select(MCPToolCall).where(MCPToolCall.tool_name == "upload_image")
        ).all()
    assert traces
    assert all(secret not in json.dumps(trace.input_summary) for trace in traces)
    assert all(secret not in json.dumps(trace.params_summary) for trace in traces)
    assert all(secret not in (trace.result_summary or "") for trace in traces)

    # MCP-07: absence of the secret is necessary but not sufficient -- the
    # rejected upload's forensic fingerprint (size + digest) must survive
    # redaction's 2nd/3rd re-entrant pass too. A failed upload has no
    # `payload["data"]` to restore "bytes"/"sha256" from (that only exists on
    # success), so the fields must come from the 1st pass's own summary and
    # not be silently dropped by later passes. This trace has no other row
    # (no AuditEvent for the failure) to recover the fingerprint from.
    failure_trace = next(
        trace for trace in traces if trace.input_summary.get("marker_uuid")
        == str(marker_uuid)
        and trace.input_summary.get("mime_type") == "image/jpeg"
    )
    expected_sha256 = hashlib.sha256(JPEG).hexdigest()
    assert failure_trace.input_summary.get("bytes") == len(JPEG)
    assert failure_trace.input_summary.get("sha256") == expected_sha256
    assert "content_base64" not in failure_trace.input_summary


def test_upload_redacts_base64_duplicated_into_filename(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)
    secret = base64.b64encode(GIF).decode("ascii")
    payload = _payload(
        workspace_id,
        GIF,
        filename=f"prefix-{secret}-suffix",
        mime_type="image/gif",
        page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
    )

    async def run():
        return await call_tool_async(
            "upload_image",
            {**payload, "conversation_token": "new"},
            actor=actor,
            page_service=page_service,
        )

    result = anyio.run(run)
    assert result.isError is False
    with page_service.session_factory() as session:
        trace = session.scalar(
            select(MCPToolCall)
            .where(MCPToolCall.tool_name == "upload_image")
            .order_by(MCPToolCall.started_at.desc())
        )
    assert trace is not None
    assert secret not in json.dumps(trace.input_summary)
    assert "[redacted image bytes]" in trace.input_summary["filename"]


def test_failing_upload_scrubs_base64_even_if_an_exception_mentions_it(
    page_service, actor, tmp_path, monkeypatch
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    workspace_id = _workspace_id(page_service)
    editing_page = _open_editing_page(page_service, actor)
    payload = _payload(
        workspace_id,
        page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
    )
    secret = payload["content_base64"]

    def explode(_service, *, session_factory, command):
        del session_factory
        leaked = base64.b64encode(command.content).decode("ascii")
        raise ValueError(f"simulated failure for {leaked}")

    monkeypatch.setattr(ContextImageIngestService, "ingest", explode)

    async def fail():
        return await call_tool_async(
            "upload_image",
            {**payload, "conversation_token": "new"},
            actor=actor,
            page_service=page_service,
        )

    failed = anyio.run(fail)
    assert failed.isError is True
    assert secret not in json.dumps(failed.structuredContent)
    with page_service.session_factory() as session:
        trace = session.scalar(
            select(MCPToolCall)
            .where(MCPToolCall.tool_name == "upload_image")
            .order_by(MCPToolCall.started_at.desc())
        )
    assert trace is not None
    assert secret not in json.dumps(trace.input_summary)
    assert secret not in json.dumps(trace.params_summary)
    assert secret not in (trace.result_summary or "")
