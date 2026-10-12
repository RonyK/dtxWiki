from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.schemas.page import PageMetadataPatch, PageUpdateRequest
from backend.services.page_service import MAX_ACTIVE_ALTERNATIVE_DRAFTS


def test_rest_and_mcp_share_metadata_patch_validation():
    invalid_patches = [
        {"page_type": "outside-taxonomy"},
        {"tags": "not-a-list"},
        {"placement_parent_node_id": "not-a-uuid"},
    ]
    for patch in invalid_patches:
        with pytest.raises(ValidationError):
            PageMetadataPatch.model_validate(patch)
        with pytest.raises(ValidationError):
            UpdateDraftRequest.model_validate(
                {
                    "page_id": uuid4(),
                    "base_version_id": uuid4(),
                    "base_content_hash": "a" * 64,
                    "update_mode": "metadata_update",
                    "metadata_patch": patch,
                    "reason": "invalid patch",
                }
            )

    with pytest.raises(ValidationError):
        PageUpdateRequest.model_validate(
            {
                "body_md": "body",
                "base_version_id": uuid4(),
                "base_content_hash": "a" * 64,
                "page_type": "outside-taxonomy",
            }
        )


def test_nonempty_source_context_is_explicitly_rejected():
    with pytest.raises(ValidationError, match="source_context is not supported"):
        WriteDraftRequest.model_validate(
            {
                "title": "Unsupported provenance",
                "page_type": "overview",
                "body_markdown": "# Unsupported provenance",
                "reason": "pin no-op rejection",
                "source_context": [uuid4()],
            }
        )


def test_alternative_draft_accumulation_is_bounded():
    assert MAX_ACTIVE_ALTERNATIVE_DRAFTS == 20
