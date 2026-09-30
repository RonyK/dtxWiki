from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from backend.context_media.domain import (
    ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES,
    CONTEXT_IMAGE_FORMATS,
    CONTEXT_MEDIA_POLICY_REVISION,
    CONTEXT_MEDIA_UPLOAD_POLICY,
    MAX_CONTEXT_IMAGE_DIMENSION,
    MAX_CONTEXT_IMAGE_FRAMES,
    MAX_CONTEXT_IMAGE_PIXELS,
    MAX_CONTEXT_IMAGE_TOTAL_PIXELS,
    MAX_CONTEXT_IMAGE_UPLOAD_BYTES,
    ActorIdentity,
    AssetUsageSnapshot,
    AssociationState,
    ContextMediaAssetProjection,
    ContextMediaAssetRecord,
    InvalidLogicalReferenceError,
    MarkdownMediaPolicyError,
    UsageState,
    context_image_inspection_schema_revision,
    extract_logical_asset_references,
    logical_asset_reference,
    parse_logical_asset_reference,
    validate_markdown_media_policy,
)
from backend.media.domain import BlobEpoch, BlobReference


def _asset(*, revision: int = 1) -> ContextMediaAssetRecord:
    workspace_id = uuid4()
    return ContextMediaAssetRecord(
        workspace_id=workspace_id,
        asset_id=uuid4(),
        page_id=uuid4(),
        created_for_version_id=uuid4(),
        blob=BlobReference(workspace_id, uuid4(), BlobEpoch(1)),
        inspection_id=uuid4(),
        original_filename="diagram.png",
        canonical_media_type="image/png",
        logical_bytes=42,
        policy_revision=CONTEXT_MEDIA_POLICY_REVISION,
        created_by=ActorIdentity("human", str(uuid4())),
        association_state=AssociationState.ACTIVE,
        revision=revision,
    )


def test_logical_reference_parser_accepts_only_the_canonical_exact_uri() -> None:
    asset_id = uuid4()
    value = logical_asset_reference(asset_id)

    assert parse_logical_asset_reference(value) == asset_id
    assert extract_logical_asset_references(f"![one]({value})\n![two]({value})") == (
        asset_id,
        asset_id,
    )

    for invalid in (
        value.upper(),
        f"{value}?download=1",
        f"{value}/content",
        "dtx-media://asset/not-a-uuid",
        str(asset_id),
    ):
        with pytest.raises(InvalidLogicalReferenceError):
            parse_logical_asset_reference(invalid)


def test_logical_reference_extraction_reads_media_nodes_not_code_or_plain_text() -> (
    None
):
    first = uuid4()
    second = uuid4()
    first_ref = logical_asset_reference(first)
    second_ref = logical_asset_reference(second)
    markdown = f"""
plain {first_ref}
[ordinary link]({first_ref})
`![inline code]({first_ref})`
```markdown
![fenced code]({first_ref})
```
<pre><img src="{first_ref}"></pre>
![inline](<{first_ref}>)
![reference][diagram]

[diagram]: {second_ref} "title"
<img alt="raw" src="{first_ref}">
"""

    assert extract_logical_asset_references(markdown) == (first, second, first)


def test_logical_reference_extraction_rejects_noncanonical_media_destination() -> None:
    asset_id = uuid4()
    with pytest.raises(InvalidLogicalReferenceError):
        extract_logical_asset_references(
            f"![bad](dtx-media://asset/{asset_id}?download=1)"
        )


@pytest.mark.parametrize(
    ("body", "reason_code"),
    [
        ("![inline](data:image/png;base64,AAAA)", "data_destination"),
        ('<video src="blob:https://example.test/id"></video>', "blob_destination"),
        (
            '<img srcset="safe.png 1x, data:image/png;base64,AAAA 2x">',
            "data_destination",
        ),
        (
            "![temporary](/api/v1/media-sessions/session/assets/asset/content)",
            "temporary_media_destination",
        ),
        (
            "![Uploading diagram.png...](\u200b00000000-0000-4000-8000-000000000001)",
            "transient_upload_marker",
        ),
        (
            "<!-- upload cancelled 00000000-0000-4000-8000-000000000001: x.png -->",
            "operational_upload_comment",
        ),
        (
            "![bad](dtx-media://asset/not-a-uuid)",
            "logical_reference_invalid",
        ),
    ],
)
def test_markdown_media_policy_rejects_noncanonical_storage_state(
    body: str, reason_code: str
) -> None:
    with pytest.raises(MarkdownMediaPolicyError) as captured:
        validate_markdown_media_policy(body)

    assert captured.value.error_code == "media_markdown_policy_invalid"
    assert captured.value.reason_code == reason_code


def test_markdown_media_policy_ignores_examples_in_code_and_counts_duplicates() -> None:
    asset_id = uuid4()
    reference = logical_asset_reference(asset_id)
    body = f"""
`data:image/png;base64,AAAA`
```markdown
![example](blob:https://example.test/id)
<!-- upload failed marker: example.png -->
```
![one]({reference})
![two]({reference})
"""

    assert validate_markdown_media_policy(body) == (asset_id, asset_id)


def test_markdown_media_policy_does_not_reject_words_ending_in_data() -> None:
    assert (
        validate_markdown_media_policy(
            "Metadata: retained. Application data: documented."
        )
        == ()
    )


def test_markdown_media_policy_checks_destinations_without_rejecting_prose() -> None:
    body = (
        "The MIME token data:image/png and the example endpoint "
        "/api/v1/uploads/example are prose, not media destinations."
    )

    assert validate_markdown_media_policy(body) == ()


def test_srcset_parser_counts_logical_media_candidates() -> None:
    first = uuid4()
    second = uuid4()

    assert validate_markdown_media_policy(
        '<source srcset="'
        f"{logical_asset_reference(first)} 1x, "
        f'{logical_asset_reference(second)} 2x">'
    ) == (first, second)


def test_raw_html_media_parser_handles_angle_bracket_inside_quoted_alt() -> None:
    asset_id = uuid4()
    reference = logical_asset_reference(asset_id)

    assert validate_markdown_media_policy(f'<img alt=">" src="{reference}">') == (
        asset_id,
    )


def test_raw_video_counts_both_source_and_poster_usage() -> None:
    source_id = uuid4()
    poster_id = uuid4()
    source = logical_asset_reference(source_id)
    poster = logical_asset_reference(poster_id)

    assert extract_logical_asset_references(
        f'<video controls poster="{poster}" src="{source}"></video>'
    ) == (poster_id, source_id)


def test_asset_owner_and_context_image_format_policy_are_domain_invariants() -> None:
    asset = _asset()
    with pytest.raises(FrozenInstanceError):
        asset.revision = 2
    # P3.1: page_id/created_for_version_id are both always required now (no
    # more page/session XOR) -- construction with either missing is a
    # TypeError from the dataclass itself, not a domain ValueError.
    assert replace(asset, canonical_media_type="image/webp").canonical_media_type == (
        "image/webp"
    )
    with pytest.raises(ValueError, match="PNG, JPEG, GIF, and WebP"):
        replace(asset, canonical_media_type="image/tiff")

    assert ALLOWED_CONTEXT_IMAGE_MEDIA_TYPES == {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
    assert CONTEXT_IMAGE_FORMATS["image/webp"].format_id == "webp"
    assert CONTEXT_IMAGE_FORMATS["image/jpeg"].extensions == (".jpg", ".jpeg")
    assert CONTEXT_IMAGE_FORMATS["image/gif"].multi_frame_allowed is True
    assert CONTEXT_IMAGE_FORMATS["image/png"].multi_frame_allowed is False


def test_context_image_resource_policy_is_revisioned_and_format_derived() -> None:
    assert CONTEXT_MEDIA_POLICY_REVISION == "context-image-v2"
    assert CONTEXT_MEDIA_UPLOAD_POLICY.policy_revision == CONTEXT_MEDIA_POLICY_REVISION
    assert CONTEXT_MEDIA_UPLOAD_POLICY.uploadable_media_types == tuple(
        CONTEXT_IMAGE_FORMATS
    )
    assert MAX_CONTEXT_IMAGE_DIMENSION == 16_384
    assert MAX_CONTEXT_IMAGE_PIXELS == 40_000_000
    assert MAX_CONTEXT_IMAGE_FRAMES == 128
    assert MAX_CONTEXT_IMAGE_TOTAL_PIXELS == 40_000_000
    assert MAX_CONTEXT_IMAGE_UPLOAD_BYTES == 10 * 1024 * 1024
    assert (
        CONTEXT_MEDIA_UPLOAD_POLICY.max_request_body_bytes
        == MAX_CONTEXT_IMAGE_UPLOAD_BYTES + (256 * 1024)
    )
    assert context_image_inspection_schema_revision("context-mvp-v1") == (
        "context-media-v1"
    )
    assert context_image_inspection_schema_revision(CONTEXT_MEDIA_POLICY_REVISION) == (
        CONTEXT_MEDIA_POLICY_REVISION
    )


def test_usage_projection_classifies_retained_history_and_token_inputs() -> None:
    asset = _asset()
    draft_id = uuid4()
    published_id = uuid4()
    unused = AssetUsageSnapshot(asset, draft_id, published_id, 0, 0, 0, False)
    history = replace(unused, historical_reference_count=2)
    active = replace(history, latest_draft_reference_count=1)

    assert unused.state is UsageState.UNUSED
    assert history.state is UsageState.HISTORY_ONLY
    assert active.state is UsageState.ACTIVE
    assert len({unused.usage_token, history.usage_token, active.usage_token}) == 3
    assert unused.usage_token.startswith("cmu1_")
    assert ContextMediaAssetProjection.from_snapshot(unused).can_delete is True
    assert ContextMediaAssetProjection.from_snapshot(history).can_delete is False
    assert replace(unused, asset=replace(asset, revision=2)).usage_token != (
        unused.usage_token
    )


def test_usage_snapshot_requires_a_real_bool_for_created_for_version_is_editing() -> (
    None
):
    # P3.4: this flag (not reference counts) is what
    # DeleteUnusedMedia/orphan-sweep use to keep an unshared, uninserted
    # upload exempt from reclamation and author-restricted delete while its
    # creating editing session is still open.
    asset = _asset()
    with pytest.raises(ValueError, match="must be a bool"):
        AssetUsageSnapshot(asset, None, None, 0, 0, 0, created_for_version_is_editing=1)
    protected = AssetUsageSnapshot(
        asset, None, None, 0, 0, 0, created_for_version_is_editing=True
    )
    assert protected.created_for_version_is_editing is True
