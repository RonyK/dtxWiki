import importlib

import pytest
from fastapi import FastAPI
from fastapi.responses import Response

import backend.api.context_media as context_media
import backend.services.context_image_ingest as context_image_ingest
from backend.api.context_media import create_context_media_router
from backend.context_media.domain import CONTEXT_MEDIA_UPLOAD_POLICY
from backend.schemas.context_media import (
    ContextMediaAssetPayload,
    ContextMediaCapabilitiesPayload,
    ContextMediaUploadPayload,
)


def test_context_image_ingest_status_code_contract_reports_both_directions() -> None:
    original_error_codes = context_image_ingest.CONTEXT_IMAGE_INGEST_ERROR_CODES
    mismatched_error_codes = frozenset(
        (set(original_error_codes) - {"media_state_changed"})
        | {"only_in_error_codes"}
    )
    try:
        context_image_ingest.CONTEXT_IMAGE_INGEST_ERROR_CODES = mismatched_error_codes
        with pytest.raises(RuntimeError) as exc_info:
            importlib.reload(context_media)

        message = str(exc_info.value)
        assert "media_state_changed" in message
        assert "only_in_error_codes" in message
    finally:
        context_image_ingest.CONTEXT_IMAGE_INGEST_ERROR_CODES = original_error_codes
        importlib.reload(context_media)


def test_context_media_router_builds_openapi_without_concrete_adapters() -> None:
    app = FastAPI()
    app.include_router(
        create_context_media_router(
            lambda: None,
            lambda: None,
            content_responder=lambda capability, request: Response(),
        ),
        prefix="/api/v1",
    )

    paths = app.openapi()["paths"]
    assert set(paths) == {
        "/api/v1/workspaces/{workspace_id}/context/media/capabilities",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/media",
        "/api/v1/workspaces/{workspace_id}/context/media/assets/{asset_id}",
        (
            "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
            "/versions/{page_version_id}/media/{asset_id}/content"
        ),
    }
    upload_responses = paths[
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/media"
    ]["post"]["responses"]
    assert set(upload_responses) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "410",
        "413",
        "415",
        "422",
        "429",
        "503",
    }
    assert upload_responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RESTErrorBody"
    }
    assert upload_responses["429"]["headers"]["Retry-After"]["schema"] == {
        "type": "string"
    }

    for path in (
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{page_version_id}/media/{asset_id}/content",
    ):
        responses = paths[path]["get"]["responses"]
        assert {"200", "206", "304", "401", "404", "416", "422"} <= set(responses)
        assert responses["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/RESTErrorBody"
        }


def test_context_media_capability_additions_remain_legacy_parse_compatible() -> None:
    legacy = ContextMediaCapabilitiesPayload.model_validate(
        {
            "policy_revision": "context-mvp-v1",
            "uploadable_media_types": ["image/png", "image/jpeg"],
            "max_upload_bytes": 10 * 1024 * 1024,
        }
    )

    assert legacy.max_files_per_batch is None
    assert legacy.max_batch_bytes is None
    assert legacy.max_parallel_uploads is None


def test_context_media_rest_payloads_accept_the_canonical_format_matrix() -> None:
    capabilities = ContextMediaCapabilitiesPayload.model_validate(
        {
            "policy_revision": CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision,
            "uploadable_media_types": (
                CONTEXT_MEDIA_UPLOAD_POLICY.uploadable_media_types
            ),
            "max_upload_bytes": CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes,
        }
    )
    assert capabilities.uploadable_media_types == (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    )

    for media_type in capabilities.uploadable_media_types:
        upload = ContextMediaUploadPayload.model_validate(
            {
                "asset_id": "00000000-0000-4000-8000-000000000001",
                "logical_ref": (
                    "dtx-media://asset/00000000-0000-4000-8000-000000000001"
                ),
                "content_url": "/api/v1/media/content",
                "mime": media_type,
                "bytes": 1,
                "sha256": "0" * 64,
            }
        )
        asset = ContextMediaAssetPayload.model_validate(
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "logical_ref": upload.logical_ref,
                "original_filename": "image",
                "media_type": media_type,
                "bytes": 1,
                "association_state": "active",
                "usage_state": "unused",
                "latest_draft_reference_count": 0,
                "current_published_reference_count": 0,
                "historical_reference_count": 0,
                "can_delete": True,
                "usage_token": "token",
                "content_url": upload.content_url,
            }
        )
        assert upload.mime == asset.media_type == media_type
