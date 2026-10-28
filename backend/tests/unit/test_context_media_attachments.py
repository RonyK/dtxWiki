"""The attachment listing's seam: cursor codec, wire projection, and the
guarantee that composing a new snapshot type did not disturb ``usage_token``.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.context_media.domain import (
    ActorIdentity,
    AssetUsageSnapshot,
    AssociationState,
    ContextMediaAssetRecord,
    ContextMediaAttachmentProjection,
    ContextMediaAttachmentSnapshot,
)
from backend.media.domain import BlobEpoch, BlobReference
from backend.services.context_media_attachments import (
    attachment_content_url,
    attachment_read_projection,
    decode_attachment_cursor,
    encode_attachment_cursor,
)

WORKSPACE_ID = uuid4()
PAGE_ID = uuid4()
VERSION_ID = uuid4()
ASSET_ID = uuid4()
BLOB_ID = uuid4()
UPLOADER = uuid4()
CREATED_AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _record(actor: ActorIdentity | None = None) -> ContextMediaAssetRecord:
    return ContextMediaAssetRecord(
        workspace_id=WORKSPACE_ID,
        asset_id=ASSET_ID,
        page_id=PAGE_ID,
        created_for_version_id=VERSION_ID,
        blob=BlobReference(WORKSPACE_ID, BLOB_ID, BlobEpoch(1)),
        inspection_id=uuid4(),
        original_filename="diagram.png",
        canonical_media_type="image/png",
        logical_bytes=1234,
        policy_revision="context-mvp-v1",
        created_by=actor or ActorIdentity("human", str(UPLOADER)),
        association_state=AssociationState.ACTIVE,
        revision=1,
    )


def _snapshot(**overrides) -> AssetUsageSnapshot:
    defaults = dict(
        asset=_record(),
        latest_draft_version_id=None,
        current_published_version_id=None,
        latest_draft_reference_count=0,
        current_published_reference_count=0,
        historical_reference_count=0,
        created_for_version_is_editing=False,
    )
    defaults.update(overrides)
    return AssetUsageSnapshot(**defaults)


def _attachment(**overrides) -> ContextMediaAttachmentProjection:
    snapshot = ContextMediaAttachmentSnapshot(
        usage=overrides.pop("usage", _snapshot()),
        page_title=overrides.pop("page_title", "Release notes"),
        kind=overrides.pop("kind", "image"),
        created_at=overrides.pop("created_at", CREATED_AT),
        is_orphan=overrides.pop("is_orphan", True),
        preview_version_id=overrides.pop("preview_version_id", VERSION_ID),
    )
    assert not overrides, overrides
    return ContextMediaAttachmentProjection.from_snapshot(snapshot)


def test_cursor_round_trips():
    cursor = encode_attachment_cursor(CREATED_AT, ASSET_ID)

    assert decode_attachment_cursor(cursor) == (CREATED_AT, ASSET_ID)


@pytest.mark.parametrize(
    "cursor",
    [None, "", "not-base64!!", "b3RoZXItcHJlZml4OjE=", "YXR0YWNoMTpub3QtYS1kYXRl"],
)
def test_an_unusable_cursor_restarts_rather_than_raising(cursor):
    # A stale or hand-edited cursor should cost the reader the first page
    # again, not a 500. Same policy as _decode_job_cursor.
    assert decode_attachment_cursor(cursor) is None


def test_composing_the_attachment_snapshot_leaves_usage_token_alone():
    """The CAS regression guard.

    ``usage_token`` hashes exactly the fields ``AssetUsageSnapshot`` declares.
    The attachment listing needed four more (page title, kind, created_at,
    orphan flag), and carrying them by composition rather than by adding
    fields is what keeps every previously-issued token valid. If someone
    "simplifies" this by folding those fields into AssetUsageSnapshot, delete
    starts answering 412 for everyone and this is the test that says why.
    """

    usage = _snapshot()
    attachment = ContextMediaAttachmentSnapshot(
        usage=usage,
        page_title="Release notes",
        kind="image",
        created_at=CREATED_AT,
        is_orphan=True,
    )

    assert attachment.usage.usage_token == usage.usage_token
    assert (
        ContextMediaAttachmentProjection.from_snapshot(attachment).asset.usage_token
        == usage.usage_token
    )


def test_projection_narrows_can_delete_by_the_callers_role():
    unused = _attachment()
    assert unused.asset.can_delete is True

    allowed = attachment_read_projection(
        unused,
        workspace_id=WORKSPACE_ID,
        uploaded_by_display_name="Ada",
        viewer_actor_kind="human",
        viewer_actor_id=str(UPLOADER),
        caller_may_delete=True,
    )
    refused = attachment_read_projection(
        unused,
        workspace_id=WORKSPACE_ID,
        uploaded_by_display_name="Ada",
        viewer_actor_kind="human",
        viewer_actor_id=str(UPLOADER),
        caller_may_delete=False,
    )

    assert allowed["can_delete"] is True
    # The domain still says the asset is unused; the role is what refuses.
    assert refused["can_delete"] is False


def test_projection_marks_only_the_callers_own_uploads():
    attachment = _attachment()

    mine = attachment_read_projection(
        attachment,
        workspace_id=WORKSPACE_ID,
        uploaded_by_display_name="Ada",
        viewer_actor_kind="human",
        viewer_actor_id=str(UPLOADER),
        caller_may_delete=True,
    )
    theirs = attachment_read_projection(
        attachment,
        workspace_id=WORKSPACE_ID,
        uploaded_by_display_name="Ada",
        viewer_actor_kind="human",
        viewer_actor_id=str(uuid4()),
        caller_may_delete=True,
    )

    assert mine["is_mine"] is True
    assert theirs["is_mine"] is False


def test_projection_requires_the_uploader_display_name():
    # Identity-plane data arrives from a separate session; omitting it should
    # fail loudly rather than quietly render a raw actor id at a person.
    with pytest.raises(TypeError):
        attachment_read_projection(
            _attachment(),
            workspace_id=WORKSPACE_ID,
            viewer_actor_kind="human",
            viewer_actor_id=str(UPLOADER),
            caller_may_delete=True,
        )


def test_content_url_points_at_the_existing_per_version_route():
    row = attachment_read_projection(
        _attachment(),
        workspace_id=WORKSPACE_ID,
        uploaded_by_display_name="Ada",
        viewer_actor_kind="human",
        viewer_actor_id=str(UPLOADER),
        caller_may_delete=True,
    )

    assert row["content_url"] == attachment_content_url(
        workspace_id=WORKSPACE_ID,
        page_id=PAGE_ID,
        version_id=VERSION_ID,
        asset_id=ASSET_ID,
    )
    assert row["content_url"].endswith(
        f"/context/pages/{PAGE_ID}/versions/{VERSION_ID}/media/{ASSET_ID}/content"
    )
