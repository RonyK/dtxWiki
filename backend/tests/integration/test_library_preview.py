from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryAttemptPreviewDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalQuotaUsage,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.media import StoredBlobInspectionRow
from backend.models.workspace import Workspace
from backend.services.conversion_orchestrator import LIBRARY_CONVERSION_JOB_TYPE
from backend.services.preview_execution import (
    TrustedBuiltinPreviewExecutionBoundary,
)
from backend.services.preview_orchestrator import LIBRARY_PREVIEW_JOB_TYPE
from backend.services.preview_provider_registry import PreviewProviderRegistry
from backend.spi.preview import (
    PREVIEW_PROVIDER_SPI_VERSION,
    PreviewLimits,
    PreviewProbeResult,
    PreviewProviderDescriptor,
    PreviewProviderError,
    PreviewResult,
)
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.worker.runtime import OneShotWorker


@dataclass(frozen=True, slots=True)
class _PreviewApi:
    client: TestClient
    workspace_id: UUID
    session_factory: object


@pytest.fixture
def preview_api(page_service, tmp_path: Path) -> Iterator[_PreviewApi]:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="preview-test-session-secret",
        force_security_headers=True,
        library_blob_root=str(tmp_path / "library-blobs"),
        library_upload_hard_cap_bytes=10_000_000,
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        workspace_id = UUID(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="preview-owner@example.com",
                workspace_slug="default",
            )
        )
        yield _PreviewApi(client, workspace_id, page_service.session_factory)


def _library_url(api: _PreviewApi, suffix: str) -> str:
    return f"/api/v1/workspaces/{api.workspace_id}/library{suffix}"


def _upload(
    api: _PreviewApi,
    *,
    filename: str,
    content: bytes,
    media_type: str,
):
    return api.client.post(
        f"/api/v1/workspaces/{api.workspace_id}/library/items",
        files={"file": (filename, content, media_type)},
    )


def _valid_pdf() -> bytes:
    prefix = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    offset = str(len(prefix)).encode()
    return (
        prefix
        + b"xref\n0 1\n0000000000 65535 f \n"
        + b"trailer\n<< /Root 1 0 R >>\nstartxref\n"
        + offset
        + b"\n%%EOF"
    )


def _valid_image(pillow_format: str) -> bytes:
    image = Image.new("RGB", (24, 12), (20, 80, 160))
    output = BytesIO()
    try:
        image.save(output, format=pillow_format)
        return output.getvalue()
    finally:
        image.close()


def _valid_wav(*, sample_bytes: int = 160) -> bytes:
    assert sample_bytes > 0 and sample_bytes % 2 == 0
    samples = b"\0" * sample_bytes
    pcm_format = (
        (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8_000).to_bytes(4, "little")
        + (16_000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )
    body = b"fmt " + len(pcm_format).to_bytes(4, "little") + pcm_format
    body += b"data" + len(samples).to_bytes(4, "little") + samples
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _valid_mp4(codec: bytes = b"avc1") -> bytes:
    ftyp = _mp4_box(b"ftyp", b"isom" + b"\0" * 4 + b"isom")
    handler = _mp4_box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 4)
    sample = (16).to_bytes(4, "big") + codec + b"\0" * 8
    sample_table = _mp4_box(b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + sample)
    track = _mp4_box(
        b"trak",
        _mp4_box(
            b"mdia",
            handler + _mp4_box(b"minf", _mp4_box(b"stbl", sample_table)),
        ),
    )
    return ftyp + _mp4_box(b"moov", track) + _mp4_box(b"mdat", b"\0")


def _preview_url(api: _PreviewApi, _item_id: str, version_id: str) -> str:
    # P9.2: flat -- the item is no longer part of the URL.
    return _library_url(api, f"/versions/{version_id}/preview")


def _preview_source_url(api: _PreviewApi, item_id: str, version_id: str) -> str:
    return _preview_url(api, item_id, version_id) + "/source"


def test_pdf_source_manifest_uses_dedicated_transport_and_explicit_fallback(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="source.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version = item["current_version"]

    response = preview_api.client.get(
        _preview_url(preview_api, item["id"], version["id"])
    )

    assert response.status_code == 200, response.text
    manifest = response.json()["data"]
    assert manifest["status"] == "ready"
    assert manifest["selected_preview_attempt_id"] is None
    assert manifest["fallbacks"] == ["metadata", "download"]
    assert manifest["download_url"] == version["original_url"]
    source_url = _preview_source_url(preview_api, item["id"], version["id"])
    assert manifest["variants"] == [
        {
            "id": None,
            "kind": "pdf",
            "media_type": "application/pdf",
            "delivery": "range",
            "content_url": source_url,
            "renderer_contract": "core.pdf.v1",
            "rank": 200,
            "byte_size": len(_valid_pdf()),
            "width": None,
            "height": None,
            "duration_ms": None,
            "page_count": None,
            "source": "original",
            "provider_id": None,
            "provider_version": None,
        }
    ]
    serialized = json.dumps(manifest, sort_keys=True)
    assert all(
        forbidden not in serialized
        for forbidden in ("storage_key", "blob_id", "sha256", "token=")
    )
    assert manifest["variants"][0]["content_url"] != manifest["download_url"]
    unchanged = preview_api.client.get(
        _preview_url(preview_api, item["id"], version["id"]),
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "private, no-store"


def test_preview_source_transport_supports_get_head_etag_and_bounded_range(
    preview_api: _PreviewApi,
) -> None:
    source = _valid_pdf()
    uploaded = _upload(
        preview_api,
        filename="report (final).pdf",
        content=source,
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version = item["current_version"]
    source_url = _preview_source_url(preview_api, item["id"], version["id"])

    full = preview_api.client.get(source_url)
    assert full.status_code == 200, full.text
    assert full.content == source
    assert full.headers["content-type"] == "application/pdf"
    assert full.headers["content-length"] == str(len(source))
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["x-content-type-options"] == "nosniff"
    assert full.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert full.headers["cross-origin-resource-policy"] == "same-origin"
    assert full.headers["content-disposition"].startswith(
        'inline; filename="report (final).pdf";'
    )
    etag = full.headers["etag"]
    assert etag.startswith('"lib-preview-source-v1-')
    assert hashlib.sha256(source).hexdigest() not in etag

    head = preview_api.client.head(source_url)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(source))
    assert head.headers["etag"] == etag

    partial = preview_api.client.get(
        source_url,
        headers={"Range": "bytes=1-7", "If-Range": etag},
    )
    assert partial.status_code == 206
    assert partial.content == source[1:8]
    assert partial.headers["content-range"] == f"bytes 1-7/{len(source)}"
    ranged_head = preview_api.client.head(
        source_url,
        headers={"Range": "bytes=1-7", "If-Range": etag},
    )
    assert ranged_head.status_code == 206
    assert ranged_head.content == b""
    assert ranged_head.headers["content-length"] == "7"
    assert ranged_head.headers["content-range"] == f"bytes 1-7/{len(source)}"

    unchanged = preview_api.client.get(
        source_url,
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "private, no-store"
    assert unchanged.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )
    assert unchanged.headers["cross-origin-resource-policy"] == "same-origin"

    stale_range = preview_api.client.get(
        source_url,
        headers={"Range": "bytes=1-7", "If-Range": '"different"'},
    )
    assert stale_range.status_code == 200
    assert stale_range.content == source
    invalid_range = preview_api.client.get(
        source_url,
        headers={"Range": "bytes=0-1,4-5"},
    )
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == f"bytes */{len(source)}"
    assert invalid_range.headers["cache-control"] == "private, no-store"
    assert invalid_range.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )
    assert invalid_range.headers["cross-origin-resource-policy"] == "same-origin"

    attachment = preview_api.client.get(version["original_url"])
    assert attachment.status_code == 200
    assert attachment.headers["content-disposition"].startswith("attachment;")
    assert attachment.headers["etag"] != etag


def test_pdf_range_reads_remain_audited_without_appearing_in_activity_history(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="range-audit.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version_id = UUID(item["current_version"]["id"])
    source_url = _preview_source_url(preview_api, item["id"], str(version_id))

    for byte_range in ("bytes=0-7", "bytes=8-15", "bytes=16-23"):
        response = preview_api.client.get(
            source_url,
            headers={"Range": byte_range},
        )
        assert response.status_code == 206, response.text

    with preview_api.session_factory() as session:
        preview_read_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.workspace_id == preview_api.workspace_id,
                AuditEvent.action == "library.preview.source.read",
                AuditEvent.target_id == version_id,
            )
        )
    assert preview_read_count == 3

    activity = preview_api.client.get(
        f"/api/v1/workspaces/{preview_api.workspace_id}/activity",
        params={"period": "all", "lane": "history", "limit": 100},
    )
    assert activity.status_code == 200, activity.text
    assert all(
        row["body"] != "library.preview.source.read"
        for row in activity.json()["data"]["items"]
    )


@pytest.mark.parametrize(
    ("filename", "media_type", "pillow_format"),
    (
        ("photo.jpg", "image/jpeg", "JPEG"),
        ("diagram.png", "image/png", "PNG"),
    ),
)
def test_admitted_source_image_manifest_has_exact_dimensions_and_inline_transport(
    preview_api: _PreviewApi,
    filename: str,
    media_type: str,
    pillow_format: str,
) -> None:
    source = _valid_image(pillow_format)
    uploaded = _upload(
        preview_api,
        filename=filename,
        content=source,
        media_type=media_type,
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version = item["current_version"]
    with preview_api.session_factory() as session:
        queued = session.scalar(
            select(Job).where(
                Job.job_type == LIBRARY_PREVIEW_JOB_TYPE,
            )
        )
        attempt = session.scalar(select(LibraryAttempt))
        assert queued is None
        assert attempt is None
    manifest_response = preview_api.client.get(
        _preview_url(preview_api, item["id"], version["id"])
    )
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()["data"]
    source_url = _preview_source_url(preview_api, item["id"], version["id"])
    assert manifest["status"] == "ready"
    assert manifest["selected_preview_attempt_id"] is None
    assert manifest["download_url"] == version["original_url"]
    assert manifest["variants"] == [
        {
            "id": None,
            "kind": "image",
            "media_type": media_type,
            "delivery": "inline",
            "content_url": source_url,
            "renderer_contract": "core.image.v1",
            "rank": 200,
            "byte_size": len(source),
            "width": 24,
            "height": 12,
            "duration_ms": None,
            "page_count": None,
            "source": "original",
            "provider_id": None,
            "provider_version": None,
        }
    ]
    content = preview_api.client.get(source_url)
    assert content.status_code == 200
    assert content.content == source
    assert content.headers["content-type"] == media_type
    assert content.headers["content-disposition"].startswith("inline;")


def test_preview_source_rejects_stale_mismatched_and_non_presented_sources(
    preview_api: _PreviewApi,
) -> None:
    first = _upload(
        preview_api,
        filename="first.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    ).json()["data"]
    second_source = _valid_pdf().replace(b"%PDF-1.4", b"%PDF-1.5", 1)
    second_response = _upload(
        preview_api,
        filename="second.pdf",
        content=second_source,
        media_type="application/pdf",
    )
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()["data"]
    # P9.2: the flat versions/{version_id}/preview/source family carries no
    # item in its URL, so a second item's own version is legitimately its
    # own source -- fetch it directly rather than via a stale "foreign"
    # item/version combination that no longer exists as a URL shape.
    own_second_source = preview_api.client.get(
        _preview_source_url(preview_api, second["id"], second["current_version"]["id"])
    )
    assert own_second_source.status_code == 200, own_second_source.text

    first_item_id = UUID(first["id"])
    with preview_api.session_factory.begin() as session:
        item = session.get(LibraryItem, first_item_id)
        assert item is not None
        item.current_version_id = None
        item.current_ready_version_id = None
        item.current_preview_version_id = None
    stale_url = _preview_source_url(
        preview_api,
        first["id"],
        first["current_version"]["id"],
    )
    assert preview_api.client.get(stale_url).status_code == 404

    second_version_id = UUID(second["current_version"]["id"])
    with preview_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, second_version_id)
        assert version is not None
        version.detected_media_type = "image/png"
    second_url = _preview_source_url(
        preview_api,
        second["id"],
        second["current_version"]["id"],
    )
    assert preview_api.client.get(second_url).status_code == 404

    with preview_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, second_version_id)
        assert version is not None
        version.detected_media_type = "application/pdf"
        inspection = session.scalar(
            select(StoredBlobInspectionRow).where(
                StoredBlobInspectionRow.workspace_id == preview_api.workspace_id,
                StoredBlobInspectionRow.blob_id == version.source_blob_id,
                StoredBlobInspectionRow.blob_epoch == version.source_blob_epoch,
            )
        )
        assert inspection is not None
        inspection.canonical_media_type = "image/png"
    assert preview_api.client.get(second_url).status_code == 404

    markdown = _upload(
        preview_api,
        filename="raw.md",
        content=b"# Not an inline source\n",
        media_type="text/markdown",
    ).json()["data"]
    markdown_source_url = _preview_source_url(
        preview_api,
        markdown["id"],
        markdown["current_version"]["id"],
    )
    assert preview_api.client.get(markdown_source_url).status_code == 404
    markdown_manifest = preview_api.client.get(
        _preview_url(
            preview_api,
            markdown["id"],
            markdown["current_version"]["id"],
        )
    ).json()["data"]
    assert all(
        variant["source"] != "original" for variant in markdown_manifest["variants"]
    )


@pytest.mark.parametrize(
    (
        "filename",
        "source",
        "media_type",
        "kind",
        "renderer_contract",
        "codecs",
        "duration_ms",
    ),
    [
        (
            "raw.wav",
            _valid_wav(),
            "audio/wav",
            "audio",
            "core.audio.v1",
            [],
            10,
        ),
        (
            "raw.mp4",
            _valid_mp4(),
            "video/mp4",
            "video",
            "core.video.v1",
            ["avc1"],
            None,
        ),
    ],
)
def test_validated_media_source_is_manifest_driven_and_range_streamed(
    preview_api: _PreviewApi,
    filename: str,
    source: bytes,
    media_type: str,
    kind: str,
    renderer_contract: str,
    codecs: list[str],
    duration_ms: int | None,
) -> None:
    uploaded = _upload(
        preview_api,
        filename=filename,
        content=source,
        media_type=media_type,
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version = item["current_version"]
    source_url = _preview_source_url(preview_api, item["id"], version["id"])

    manifest_response = preview_api.client.get(
        _preview_url(preview_api, item["id"], version["id"])
    )
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()["data"]
    variant = next(entry for entry in manifest["variants"] if entry["kind"] == kind)
    assert variant == {
        "id": None,
        "kind": kind,
        "media_type": media_type,
        "delivery": "range",
        "content_url": source_url,
        "renderer_contract": renderer_contract,
        "rank": 200,
        "byte_size": len(source),
        "width": None,
        "height": None,
        "duration_ms": duration_ms,
        "page_count": None,
        "source": "original",
        "provider_id": None,
        "provider_version": None,
        "codecs": codecs,
    }

    ranged = preview_api.client.get(source_url, headers={"Range": "bytes=0-"})
    assert ranged.status_code == 206
    assert ranged.content == source
    assert ranged.headers["content-range"] == f"bytes 0-{len(source) - 1}/{len(source)}"
    assert ranged.headers["accept-ranges"] == "bytes"
    assert ranged.headers["cache-control"] == "private, no-store"
    assert ranged.headers["cross-origin-resource-policy"] == "same-origin"


def test_media_open_ended_range_remains_seekable_above_document_chunk_limit(
    preview_api: _PreviewApi,
) -> None:
    source = _valid_wav(sample_bytes=8 * 1024 * 1024 + 4096)
    uploaded = _upload(
        preview_api,
        filename="large.wav",
        content=source,
        media_type="audio/wav",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    source_url = _preview_source_url(
        preview_api,
        item["id"],
        item["current_version"]["id"],
    )

    response = preview_api.client.head(
        source_url,
        headers={"Range": "bytes=0-"},
    )

    assert response.status_code == 206
    assert response.content == b""
    assert response.headers["content-length"] == str(len(source))
    assert response.headers["content-range"] == (
        f"bytes 0-{len(source) - 1}/{len(source)}"
    )


def test_source_media_requires_exact_persisted_admission_profile(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="profile.wav",
        content=_valid_wav(),
        media_type="audio/wav",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version_id = UUID(item["current_version"]["id"])
    source_url = _preview_source_url(preview_api, item["id"], str(version_id))

    with preview_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        version.detection_json = {
            **version.detection_json,
            "container": {"validator": "unknown", "valid": True},
        }

    assert preview_api.client.get(source_url).status_code == 404
    manifest = preview_api.client.get(
        _preview_url(preview_api, item["id"], str(version_id))
    ).json()["data"]
    assert all(variant["source"] != "original" for variant in manifest["variants"])


def test_source_image_requires_exact_bounded_admission_metadata(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="metadata-guard.png",
        content=_valid_image("PNG"),
        media_type="image/png",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version_id = UUID(item["current_version"]["id"])
    with preview_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        detection = dict(version.detection_json or {})
        container = dict(detection.get("container") or {})
        container["pixel_count"] = container["pixel_count"] + 1
        detection["container"] = container
        version.detection_json = detection

    manifest_response = preview_api.client.get(
        _preview_url(preview_api, item["id"], str(version_id))
    )
    assert manifest_response.status_code == 200
    assert all(
        variant["source"] != "original"
        for variant in manifest_response.json()["data"]["variants"]
    )
    source_url = _preview_source_url(preview_api, item["id"], str(version_id))
    assert preview_api.client.get(source_url).status_code == 404


def test_selected_canonical_markdown_is_exposed_as_semantic_preview(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="semantic.md",
        content=b"# Semantic Preview\n\nBody\n",
        media_type="text/markdown",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    version = item["current_version"]

    conversion = preview_api.client.app.state.container.services.library_conversion
    worker = OneShotWorker(job_queue=conversion.job_queue)
    worker.library_conversion_service = conversion
    with preview_api.session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=preview_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert result is not None and result.status == "completed"

    response = preview_api.client.get(
        _preview_url(preview_api, item["id"], version["id"])
    )

    assert response.status_code == 200, response.text
    manifest = response.json()["data"]
    markdown = next(
        variant
        for variant in manifest["variants"]
        if variant["renderer_contract"] == "core.markdown.v1"
    )
    assert markdown["source"] == "semantic"
    assert markdown["delivery"] == "text"
    assert markdown["content_url"].startswith(_library_url(preview_api, "/artifacts/"))
    content = preview_api.client.get(markdown["content_url"])
    assert content.status_code == 200, content.text
    assert b"Semantic Preview" in content.content
    partial = preview_api.client.get(
        markdown["content_url"],
        headers={"Range": "bytes=2-9", "If-Range": content.headers["etag"]},
    )
    assert partial.status_code == 206
    assert partial.content == content.content[2:10]
    assert partial.headers["content-security-policy"] == "default-src 'none'; sandbox"

    other_upload = _upload(
        preview_api,
        filename="foreign.md",
        content=b"# Foreign Preview\n",
        media_type="text/markdown",
    )
    assert other_upload.status_code == 201, other_upload.text
    other = other_upload.json()["data"]
    with preview_api.session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=preview_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert result is not None and result.status == "completed"
    other_manifest = preview_api.client.get(
        _preview_url(
            preview_api,
            other["id"],
            other["current_version"]["id"],
        )
    ).json()["data"]
    foreign_artifact = next(
        variant
        for variant in other_manifest["variants"]
        if variant["renderer_contract"] == "core.markdown.v1"
    )
    # P9.2: the flat artifacts/{artifact_id}/content family carries no
    # item/version in its URL, so a *different item's own* canonical
    # artifact is legitimately readable through its own content_url -- the
    # real workspace boundary is covered by
    # test_library_canonical_read_paths' cross-workspace assertions.
    assert foreign_artifact["content_url"] != markdown["content_url"]
    own_foreign_content = preview_api.client.get(foreign_artifact["content_url"])
    assert own_foreign_content.status_code == 200, own_foreign_content.text
    assert b"Foreign Preview" in own_foreign_content.content

    with preview_api.session_factory.begin() as session:
        stale_version = session.get(LibraryItemVersion, UUID(version["id"]))
        assert stale_version is not None
        stale_version.selected_conversion_attempt_id = None
    assert preview_api.client.get(markdown["content_url"]).status_code == 404


def test_table_semantic_variant_has_markdown_fallback(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="rows.csv",
        content=b"name,value\nalpha,1\n",
        media_type="text/csv",
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["data"]
    conversion = preview_api.client.app.state.container.services.library_conversion
    worker = OneShotWorker(job_queue=conversion.job_queue)
    worker.library_conversion_service = conversion
    with preview_api.session_factory.begin() as session:
        result = worker.dispatch_once(
            session,
            workspace_id=preview_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert result is not None and result.status == "completed"

    manifest = preview_api.client.get(
        _preview_url(
            preview_api,
            item["id"],
            item["current_version"]["id"],
        )
    ).json()["data"]
    semantic = [
        variant for variant in manifest["variants"] if variant["source"] == "semantic"
    ]
    assert [variant["renderer_contract"] for variant in semantic[:2]] == [
        "core.table.v1",
        "core.markdown.v1",
    ]
    assert semantic[0]["content_url"] == semantic[1]["content_url"]


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/x8AAusB9Wl2nXQAAAAASUVORK5CYII="
)


class _PdfImageProvider:
    def __init__(self) -> None:
        self.generate_calls = 0

    def descriptor(self) -> PreviewProviderDescriptor:
        return PreviewProviderDescriptor(
            provider_id="builtin.pdf-image",
            provider_version="1.0.0",
            spi_version=PREVIEW_PROVIDER_SPI_VERSION,
            formats=frozenset({"pdf"}),
            media_types=frozenset({"application/pdf"}),
            input_kinds=frozenset({"source"}),
            output_renderer_contracts=frozenset({"core.image.v1"}),
            output_media_types=frozenset({"image/png"}),
            priority=100,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="Apache-2.0",
            source_url="https://example.test/builtin-pdf-image",
            distribution_digest=hashlib.sha256(b"test-builtin-pdf-image").hexdigest(),
        )

    def probe(self, _request) -> PreviewProbeResult:
        return PreviewProbeResult(supported=True)

    def generate(self, request, sink) -> PreviewResult:
        self.generate_calls += 1
        assert not hasattr(request.source, "path")
        with request.source.open_binary() as source:
            assert source.read(4) == b"%PDF"
        receipt = sink.write_variant(
            source=BytesIO(_ONE_PIXEL_PNG),
            expected_sha256=hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
            expected_byte_size=len(_ONE_PIXEL_PNG),
            kind="image",
            media_type="image/png",
            renderer_contract="core.image.v1",
            delivery="inline",
            rank=10,
            filename="page-1.png",
            width=1,
            height=1,
        )
        return PreviewResult(variants=(receipt,))


class _ConfigurablePdfImageProvider(_PdfImageProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        priority: int,
        failure: str | None = None,
        session_factory=None,
    ) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.priority = priority
        self.failure = failure
        self.session_factory = session_factory

    def descriptor(self) -> PreviewProviderDescriptor:
        descriptor = super().descriptor()
        return PreviewProviderDescriptor(
            provider_id=self.provider_id,
            provider_version=descriptor.provider_version,
            spi_version=descriptor.spi_version,
            formats=descriptor.formats,
            media_types=descriptor.media_types,
            input_kinds=descriptor.input_kinds,
            output_renderer_contracts=descriptor.output_renderer_contracts,
            output_media_types=descriptor.output_media_types,
            priority=self.priority,
            execution_mode=descriptor.execution_mode,
            network_required=descriptor.network_required,
            deterministic=descriptor.deterministic,
            license_expression=descriptor.license_expression,
            source_url=descriptor.source_url,
            distribution_digest=descriptor.distribution_digest,
        )

    def generate(self, request, sink) -> PreviewResult:
        if self.failure == "timeout":
            raise PreviewProviderError(
                "preview_timeout",
                "The Preview provider exceeded its execution deadline.",
            )
        if self.failure == "crash":
            raise RuntimeError("unsafe provider detail must not escape")
        if self.session_factory is not None:
            # A second writer can commit while generate is running only when
            # the worker closed its lineage snapshot transaction first.
            with self.session_factory.begin() as session:
                session.add(
                    Job(
                        workspace_id=request.workspace_id,
                        job_type="preview_test_probe",
                        trigger_type="preview_test",
                        idempotency_key=f"probe:{request.trace_id}",
                        target_ref={"version_id": str(request.version_id)},
                        status="queued",
                    )
                )
        return super().generate(request, sink)


class _SemanticMarkdownProvider:
    def __init__(self) -> None:
        self.generate_calls = 0

    def descriptor(self) -> PreviewProviderDescriptor:
        return PreviewProviderDescriptor(
            provider_id="builtin.semantic-markdown",
            provider_version="1.0.0",
            spi_version=PREVIEW_PROVIDER_SPI_VERSION,
            formats=frozenset({"markdown"}),
            media_types=frozenset({"text/markdown"}),
            input_kinds=frozenset({"conversion_artifact"}),
            output_renderer_contracts=frozenset({"core.markdown.v1"}),
            output_media_types=frozenset({"text/markdown"}),
            priority=100,
            execution_mode="builtin",
            network_required=False,
            deterministic=True,
            license_expression="Apache-2.0",
            source_url="https://example.test/builtin-semantic-markdown",
            distribution_digest=hashlib.sha256(
                b"test-builtin-semantic-markdown"
            ).hexdigest(),
        )

    def probe(self, _request) -> PreviewProbeResult:
        return PreviewProbeResult(supported=True)

    def generate(self, request, sink) -> PreviewResult:
        self.generate_calls += 1
        with request.source.open_binary() as source:
            content = source.read()
        receipt = sink.write_variant(
            source=BytesIO(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            expected_byte_size=len(content),
            kind="markdown",
            media_type="text/markdown",
            renderer_contract="core.markdown.v1",
            delivery="text",
            rank=10,
            filename="preview.md",
        )
        return PreviewResult(variants=(receipt,))


def _configure_preview_providers(
    api: _PreviewApi,
    *providers,
):
    services = api.client.app.state.container.services
    services.__dict__["library_preview_provider_registry"] = PreviewProviderRegistry(
        tuple(providers)
    )
    services.__dict__["library_preview_execution_boundary"] = (
        TrustedBuiltinPreviewExecutionBoundary()
    )
    for key in (
        "library_preview_orchestrator",
        "library_conversion",
        "library_binary_registration",
    ):
        services.__dict__.pop(key, None)
    return services


def test_preview_execute_cross_workspace_is_opaque_not_found_without_mutation(
    preview_api: _PreviewApi,
) -> None:
    provider = _ConfigurablePdfImageProvider(
        "builtin.pdf-cross-workspace-direct",
        priority=100,
    )
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="cross-workspace-direct.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    orchestrator = services.library_preview_orchestrator

    with preview_api.session_factory.begin() as session:
        attempt = session.scalar(
            select(LibraryAttempt).where(
                LibraryAttempt.library_item_version_id == version_id,
                LibraryAttempt.stage == "preview_generation",
            )
        )
        assert attempt is not None
        run = session.get(LibraryRun, attempt.run_id)
        version = session.get(LibraryItemVersion, version_id)
        assert run is not None and version is not None
        item = session.get(LibraryItem, version.library_item_id)
        assert item is not None
        foreign_workspace = Workspace(
            slug="foreign-preview-direct",
            title="Foreign Preview Direct",
            name="Foreign Preview Direct",
        )
        session.add(foreign_workspace)
        session.flush()
        audit_ids_before = session.scalars(select(AuditEvent.id)).all()
        artifact_ids_before = session.scalars(select(LibraryArtifact.id)).all()

        with pytest.raises(ValueError) as missing_error:
            orchestrator.execute_attempt(
                session=session,
                workspace_id=foreign_workspace.id,
                attempt_id=uuid4(),
                execution_generation=1,
            )
        with pytest.raises(ValueError) as foreign_error:
            orchestrator.execute_attempt(
                session=session,
                workspace_id=foreign_workspace.id,
                attempt_id=attempt.id,
                execution_generation=1,
            )

        audit_ids_after = session.scalars(select(AuditEvent.id)).all()
        artifact_ids_after = session.scalars(select(LibraryArtifact.id)).all()

    assert str(foreign_error.value) == str(missing_error.value)
    assert str(preview_api.workspace_id) not in str(foreign_error.value)
    assert attempt.status == "queued"
    assert run.status == "queued"
    assert version.preview_status == "queued"
    assert item.current_preview_version_id is None
    assert audit_ids_after == audit_ids_before
    assert artifact_ids_after == artifact_ids_before
    assert provider.generate_calls == 0


def test_preview_worker_rejects_cross_workspace_job_without_lineage_mutation(
    preview_api: _PreviewApi,
) -> None:
    provider = _ConfigurablePdfImageProvider(
        "builtin.pdf-cross-workspace-worker",
        priority=100,
    )
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="cross-workspace-worker.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    orchestrator = services.library_preview_orchestrator

    with preview_api.session_factory.begin() as session:
        attempt = session.scalar(
            select(LibraryAttempt).where(
                LibraryAttempt.library_item_version_id == version_id,
                LibraryAttempt.stage == "preview_generation",
            )
        )
        assert attempt is not None
        run = session.get(LibraryRun, attempt.run_id)
        source_job = session.scalar(
            select(Job).where(
                Job.workspace_id == preview_api.workspace_id,
                Job.job_type == LIBRARY_PREVIEW_JOB_TYPE,
            )
        )
        assert run is not None and source_job is not None
        foreign_workspace = Workspace(
            slug="foreign-preview-worker",
            title="Foreign Preview Worker",
            name="Foreign Preview Worker",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_job = orchestrator.job_queue.enqueue(
            session=session,
            workspace_id=foreign_workspace.id,
            job_type=LIBRARY_PREVIEW_JOB_TYPE,
            trigger_type="cross_workspace_test",
            idempotency_key=(
                f"cross-workspace:{attempt.id}:{attempt.execution_generation}"
            ),
            target_ref={
                "library_preview_attempt_id": str(attempt.id),
                "library_preview_run_id": str(run.id),
                "library_item_version_id": str(version_id),
                "execution_generation": attempt.execution_generation,
            },
        )
        foreign_workspace_id = foreign_workspace.id
        source_job_id = source_job.id
        foreign_job_id = foreign_job.id
        attempt_id = attempt.id
        run_id = run.id
        audit_ids_before = session.scalars(select(AuditEvent.id)).all()
        artifact_ids_before = session.scalars(select(LibraryArtifact.id)).all()

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_preview_service = orchestrator
    dispatch = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=foreign_workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )

    assert dispatch is not None
    assert dispatch.job_id == foreign_job_id
    assert dispatch.status == "failed"
    assert dispatch.detail == {
        "error": "Preview attempt was not found",
        "terminal": True,
    }
    assert str(preview_api.workspace_id) not in dispatch.detail["error"]
    assert provider.generate_calls == 0

    with preview_api.session_factory() as session:
        persisted_source_job = session.get(Job, source_job_id)
        persisted_foreign_job = session.get(Job, foreign_job_id)
        persisted_attempt = session.get(LibraryAttempt, attempt_id)
        persisted_run = session.get(LibraryRun, run_id)
        persisted_version = session.get(LibraryItemVersion, version_id)
        audit_ids_after = session.scalars(select(AuditEvent.id)).all()
        artifact_ids_after = session.scalars(select(LibraryArtifact.id)).all()

    assert persisted_source_job is not None
    assert persisted_source_job.status == "queued"
    assert persisted_foreign_job is not None
    assert persisted_foreign_job.status == "failed"
    assert persisted_foreign_job.retry_count == 1
    assert persisted_attempt is not None and persisted_attempt.status == "queued"
    assert persisted_run is not None and persisted_run.status == "queued"
    assert persisted_version is not None
    assert persisted_version.preview_status == "queued"
    assert audit_ids_after == audit_ids_before
    assert artifact_ids_after == artifact_ids_before


def test_upload_enqueues_durable_preview_and_worker_promotes_without_db_lock(
    preview_api: _PreviewApi,
) -> None:
    provider = _ConfigurablePdfImageProvider(
        "builtin.pdf-durable",
        priority=100,
        session_factory=preview_api.session_factory,
    )
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="durable.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    orchestrator = services.library_preview_orchestrator

    with preview_api.session_factory() as session:
        queued = session.scalar(
            select(Job).where(
                Job.job_type == LIBRARY_PREVIEW_JOB_TYPE,
                Job.status == "queued",
            )
        )
        assert queued is not None
        assert queued.target_ref["library_item_version_id"] == str(version_id)

    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_preview_service = orchestrator
    result = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert result is not None
    assert result.status == "completed"
    assert result.detail["preview_status"] == "ready"
    assert provider.generate_calls == 1
    with preview_api.session_factory() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        assert version.preview_status == "ready"
        assert version.selected_preview_attempt_id is not None
        assert (
            session.scalar(select(Job).where(Job.job_type == "preview_test_probe"))
            is not None
        )


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_preview_worker_falls_back_after_provider_timeout_or_crash(
    preview_api: _PreviewApi,
    failure: str,
) -> None:
    failing = _ConfigurablePdfImageProvider(
        f"builtin.pdf-{failure}",
        priority=200,
        failure=failure,
    )
    fallback = _ConfigurablePdfImageProvider(
        f"builtin.pdf-{failure}-fallback",
        priority=100,
    )
    services = _configure_preview_providers(preview_api, failing, fallback)
    uploaded = _upload(
        preview_api,
        filename=f"{failure}.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    orchestrator = services.library_preview_orchestrator
    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_preview_service = orchestrator

    first = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert first is not None and first.status == "completed"
    assert first.detail["preview_status"] == "fallback_queued"
    with preview_api.session_factory() as session:
        failed_attempt = session.get(
            LibraryAttempt,
            UUID(first.detail["library_preview_attempt_id"]),
        )
        assert failed_attempt is not None
        assert failed_attempt.status == (
            "timed_out" if failure == "timeout" else "failed"
        )
    second = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert second is not None and second.status == "completed"
    assert second.detail["preview_status"] == "ready"
    assert fallback.generate_calls == 1


def test_preview_stale_job_reclaim_advances_generation_and_finishes(
    preview_api: _PreviewApi,
) -> None:
    provider = _ConfigurablePdfImageProvider(
        "builtin.pdf-reclaim",
        priority=100,
    )
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="reclaim.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    orchestrator = services.library_preview_orchestrator
    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_preview_service = orchestrator
    with preview_api.session_factory.begin() as session:
        job = orchestrator.job_queue.claim_next(
            session=session,
            workspace_id=preview_api.workspace_id,
            job_types={LIBRARY_PREVIEW_JOB_TYPE},
        )
        assert job is not None
        original_attempt_id = UUID(job.target_ref["library_preview_attempt_id"])
        job.started_at = utc_now() - timedelta(minutes=30)
    assert (
        worker.reclaim_stale_jobs_with_session_factory(
            preview_api.session_factory,
            lease_timeout_seconds=60,
        )
        == 1
    )
    with preview_api.session_factory() as session:
        attempt = session.get(LibraryAttempt, original_attempt_id)
        job = session.scalar(
            select(Job).where(Job.job_type == LIBRARY_PREVIEW_JOB_TYPE)
        )
        assert attempt is not None and attempt.execution_generation == 2
        assert job is not None and job.target_ref["execution_generation"] == 2
        assert job.status == "queued"
    completed = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert completed is not None and completed.status == "completed"
    assert completed.detail["preview_status"] == "ready"


def test_conversion_promotion_invalidates_semantic_preview_and_regenerates(
    preview_api: _PreviewApi,
) -> None:
    provider = _SemanticMarkdownProvider()
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="semantic-regeneration.md",
        content=b"# Semantic generation\n",
        media_type="text/markdown",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])

    conversion = services.library_conversion
    conversion_worker = OneShotWorker(job_queue=conversion.job_queue)
    conversion_worker.library_conversion_service = conversion
    converted = conversion_worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_CONVERSION_JOB_TYPE},
    )
    assert converted is not None and converted.status == "completed"
    orchestrator = services.library_preview_orchestrator
    with preview_api.session_factory.begin() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        old_conversion_id = version.selected_conversion_attempt_id
        assert old_conversion_id is not None
        old_artifact = session.scalar(
            select(LibraryArtifact).where(
                LibraryArtifact.library_item_version_id == version_id,
                LibraryArtifact.attempt_id == old_conversion_id,
                LibraryArtifact.stage == "conversion",
                LibraryArtifact.role == "markdown",
            )
        )
        assert old_artifact is not None
        scheduled = orchestrator.schedule(
            session=session,
            workspace_id=preview_api.workspace_id,
            version_id=version_id,
            input_artifact_id=old_artifact.id,
            actor_kind="system",
            actor_id="conversion-preview-test",
        )
        assert scheduled.job is not None

    preview_worker = OneShotWorker(job_queue=orchestrator.job_queue)
    preview_worker.library_preview_service = orchestrator
    first_preview = preview_worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert first_preview is not None
    assert first_preview.detail["preview_status"] == "ready"
    with preview_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        version = session.get(LibraryItemVersion, version_id)
        old_attempt = session.get(
            LibraryAttempt,
            version.selected_conversion_attempt_id if version else None,
        )
        old_run = (
            session.get(LibraryRun, old_attempt.run_id)
            if old_attempt is not None
            else None
        )
        old_artifact = (
            session.scalar(
                select(LibraryArtifact).where(
                    LibraryArtifact.attempt_id == old_attempt.id,
                    LibraryArtifact.stage == "conversion",
                    LibraryArtifact.role == "markdown",
                )
            )
            if old_attempt is not None
            else None
        )
        assert item is not None and version is not None
        assert old_attempt is not None and old_run is not None
        assert old_artifact is not None
        selected_preview_id = version.selected_preview_attempt_id
        assert selected_preview_id is not None

        # run_seq is one counter shared across both stages for this version
        # (library_run_version_seq_uq is not stage-scoped) -- old_run.run_seq
        # + 1 may already be taken by a preview run scheduled in between.
        next_run_seq = (
            session.scalar(
                select(func.max(LibraryRun.run_seq)).where(
                    LibraryRun.library_item_version_id == version.id
                )
            )
            or 0
        ) + 1
        new_run = LibraryRun(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=next_run_seq,
            reason="manual_reconvert",
            status="completed",
            requested_by_actor_type="system",
            requested_by_actor_id=None,
            provider_chain_json=list(old_run.provider_chain_json),
            policy_revision=old_run.policy_revision,
            output_schema_version=old_run.output_schema_version,
            idempotency_key=f"test-reconvert:{uuid4()}",
            finished_at=utc_now(),
        )
        new_run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json=dict(
                old_run.conversion_detail.error_fallback_matrix_json
            ),
            per_provider_budget_json=dict(
                old_run.conversion_detail.per_provider_budget_json
            ),
            total_budget_json=dict(old_run.conversion_detail.total_budget_json),
        )
        session.add(new_run)
        session.flush()
        new_attempt = LibraryAttempt(
            workspace_id=version.workspace_id,
            run_id=new_run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id=old_attempt.provider_id,
            provider_version=old_attempt.provider_version,
            provider_spi_version=old_attempt.provider_spi_version,
            config_hash=old_attempt.config_hash,
            input_sha256=old_attempt.input_sha256,
            output_schema_version=old_attempt.output_schema_version,
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
            parent_attempt_id=None,
            started_at=utc_now(),
            finished_at=utc_now(),
            warnings_json=[],
            provenance_json={},
            metrics_json={},
        )
        new_attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=(
                old_attempt.conversion_detail.manual_retry_disposition
            ),
            disposition_reason=old_attempt.conversion_detail.disposition_reason,
        )
        session.add(new_attempt)
        session.flush()
        new_artifact = LibraryArtifact(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            attempt_id=new_attempt.id,
            stage="conversion",
            blob_workspace_id=old_artifact.blob_workspace_id,
            blob_id=old_artifact.blob_id,
            blob_epoch=old_artifact.blob_epoch,
            role=old_artifact.role,
            kind=old_artifact.kind,
            media_type=old_artifact.media_type,
            content_sha256=old_artifact.content_sha256,
            byte_size=old_artifact.byte_size,
            ordinal=old_artifact.ordinal,
        )
        new_artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=old_artifact.conversion_detail.is_complete,
            sanitizer_version=old_artifact.conversion_detail.sanitizer_version,
            extraction_method=old_artifact.conversion_detail.extraction_method,
            index_text=old_artifact.conversion_detail.index_text,
            location_model=dict(old_artifact.conversion_detail.location_model),
            quality_score=old_artifact.conversion_detail.quality_score,
        )
        session.add(new_artifact)
        session.flush()
        assert conversion._promote_if_newer_run(
            session=session,
            workspace_id=preview_api.workspace_id,
            item=item,
            version=version,
            run=new_run,
            attempt=new_attempt,
            completeness="complete",
        )
        session.flush()
        assert version.selected_preview_attempt_id is None
        assert version.preview_status == "queued"
        assert item.current_preview_version_id is None
        replacement = session.scalar(
            select(LibraryAttempt)
            .join(
                LibraryAttemptPreviewDetail,
                LibraryAttemptPreviewDetail.attempt_id == LibraryAttempt.id,
            )
            .where(
                LibraryAttempt.stage == "preview_generation",
                LibraryAttemptPreviewDetail.input_attempt_id == new_attempt.id,
            )
        )
        assert replacement is not None
        assert replacement.preview_detail.input_artifact_id == new_artifact.id
        assert replacement.id != selected_preview_id
        replacement_id = replacement.id

    regenerated = preview_worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert regenerated is not None and regenerated.status == "completed"
    assert regenerated.detail["preview_status"] == "ready"
    with preview_api.session_factory() as session:
        version = session.get(LibraryItemVersion, version_id)
        assert version is not None
        assert version.selected_preview_attempt_id == replacement_id
        assert version.preview_status == "ready"
    assert provider.generate_calls == 2


def test_conversion_dependency_hook_preserves_source_only_preview(
    preview_api: _PreviewApi,
) -> None:
    provider = _ConfigurablePdfImageProvider(
        "builtin.pdf-source-stable",
        priority=100,
    )
    services = _configure_preview_providers(preview_api, provider)
    uploaded = _upload(
        preview_api,
        filename="source-stable.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])
    version_id = UUID(uploaded.json()["data"]["current_version"]["id"])
    orchestrator = services.library_preview_orchestrator
    worker = OneShotWorker(job_queue=orchestrator.job_queue)
    worker.library_preview_service = orchestrator
    completed = worker.dispatch_once_with_session_factory(
        preview_api.session_factory,
        workspace_id=preview_api.workspace_id,
        job_types={LIBRARY_PREVIEW_JOB_TYPE},
    )
    assert completed is not None and completed.detail["preview_status"] == "ready"
    with preview_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        version = session.get(LibraryItemVersion, version_id)
        assert item is not None and version is not None
        selected_id = version.selected_preview_attempt_id
        assert selected_id is not None
        assert (
            orchestrator.invalidate_for_conversion_promotion(
                session=session,
                item=item,
                version=version,
                conversion_attempt_id=uuid4(),
            )
            is None
        )
        assert version.selected_preview_attempt_id == selected_id
        assert item.current_preview_version_id == version.id


def test_generated_preview_is_selected_by_cas_reauthorized_and_released_on_delete(
    preview_api: _PreviewApi,
) -> None:
    uploaded = _upload(
        preview_api,
        filename="generated.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_payload = uploaded.json()["data"]
    item_id = UUID(item_payload["id"])
    version_id = UUID(item_payload["current_version"]["id"])

    provider = _PdfImageProvider()
    services = preview_api.client.app.state.container.services
    services.__dict__["library_preview_provider_registry"] = PreviewProviderRegistry(
        (provider,)
    )
    services.__dict__["library_preview_execution_boundary"] = (
        TrustedBuiltinPreviewExecutionBoundary()
    )
    services.__dict__.pop("library_preview_orchestrator", None)
    orchestrator = services.library_preview_orchestrator
    limits = PreviewLimits(max_input_bytes=1_000_000)
    with preview_api.session_factory.begin() as session:
        first = orchestrator.schedule(
            session=session,
            workspace_id=preview_api.workspace_id,
            version_id=version_id,
            limits=limits,
            actor_kind="user",
            actor_id="preview-owner",
            recipe={"scale": 1},
        )
        competing = orchestrator.schedule(
            session=session,
            workspace_id=preview_api.workspace_id,
            version_id=version_id,
            limits=limits,
            actor_kind="user",
            actor_id="preview-owner",
            recipe={"scale": 2},
        )
        assert first.attempt is not None and competing.attempt is not None
        first_attempt_id = first.attempt.id
        competing_attempt_id = competing.attempt.id

    with preview_api.session_factory.begin() as session:
        completed = orchestrator.execute_attempt(
            session=session,
            workspace_id=preview_api.workspace_id,
            attempt_id=first_attempt_id,
            execution_generation=1,
        )
        assert completed.status == "ready" and completed.selected is True
    with preview_api.session_factory.begin() as session:
        stale = orchestrator.execute_attempt(
            session=session,
            workspace_id=preview_api.workspace_id,
            attempt_id=competing_attempt_id,
            execution_generation=1,
        )
        assert stale.status == "stale" and stale.selected is False
    assert provider.generate_calls == 1

    manifest_response = preview_api.client.get(
        _preview_url(preview_api, str(item_id), str(version_id))
    )
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()["data"]
    generated = next(
        variant for variant in manifest["variants"] if variant["source"] == "generated"
    )
    assert manifest["selected_preview_attempt_id"] == str(first_attempt_id)
    assert generated["renderer_contract"] == "core.image.v1"
    assert generated["provider_id"] == "builtin.pdf-image"

    full = preview_api.client.get(generated["content_url"])
    assert full.status_code == 200, full.text
    assert full.content == _ONE_PIXEL_PNG
    assert full.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert full.headers["cross-origin-resource-policy"] == "same-origin"
    assert full.headers["x-content-type-options"] == "nosniff"
    etag = full.headers["etag"]
    partial = preview_api.client.get(
        generated["content_url"],
        headers={"Range": "bytes=1-4", "If-Range": etag},
    )
    assert partial.status_code == 206
    assert partial.content == _ONE_PIXEL_PNG[1:5]
    assert partial.headers["content-range"] == (f"bytes 1-4/{len(_ONE_PIXEL_PNG)}")

    with preview_api.session_factory() as session:
        item = session.get(LibraryItem, item_id)
        version = session.get(LibraryItemVersion, version_id)
        artifact_ids = set(
            session.scalars(
                select(LibraryArtifact.id).where(
                    LibraryArtifact.library_item_version_id == version_id
                )
            )
        )
        preview_run_ids = set(
            session.scalars(
                select(LibraryRun.id).where(
                    LibraryRun.library_item_version_id == version_id
                )
            )
        )
        preview_attempt_ids = set(
            session.scalars(
                select(LibraryAttempt.id).where(
                    LibraryAttempt.library_item_version_id == version_id
                )
            )
        )
        usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": preview_api.workspace_id, "quota_kind": "derived"},
        )
        assert item is not None and version is not None
        assert item.current_ready_version_id is None
        assert version.selected_preview_attempt_id == first_attempt_id
        assert artifact_ids and preview_run_ids and preview_attempt_ids
        assert usage is not None and usage.charged_bytes == len(_ONE_PIXEL_PNG)

    deleted = preview_api.client.delete(_library_url(preview_api, f"/items/{item_id}"))
    assert deleted.status_code == 200, deleted.text
    assert preview_api.client.get(generated["content_url"]).status_code == 404
    with preview_api.session_factory() as session:
        assert all(
            session.get(LibraryArtifact, value) is None for value in artifact_ids
        )
        assert all(
            session.get(LibraryRun, value) is None for value in preview_run_ids
        )
        assert all(
            session.get(LibraryAttempt, value) is None
            for value in preview_attempt_ids
        )
        usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": preview_api.workspace_id, "quota_kind": "derived"},
        )
        assert usage is not None and usage.charged_bytes == 0
