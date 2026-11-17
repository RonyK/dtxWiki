"""The existing Context tools share the browser mutable-draft workflow."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace


@pytest.fixture
def actor(mcp_default_actor):
    # "read" is needed by the patch_context_edit tests below, which call
    # read_structure_map to discover a real block id/hash before patching --
    # matching test_patch_draft_block_operations.py's identical actor scope.
    return mcp_default_actor(
        actor_id="edit-tools-actor-a", scopes=("draft-write", "read")
    )


@pytest.fixture
def other_actor(mcp_default_actor):
    return mcp_default_actor(actor_id="edit-tools-actor-b", scopes=("draft-write",))


@pytest.fixture
def read_only_actor(mcp_default_actor):
    return mcp_default_actor(
        actor_id="edit-tools-read-only", scopes=("guide", "search", "read")
    )


def _workspace(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _stored(page_service, version_id) -> ContextPageVersion:
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None
        session.expunge(version)
        return version


def _create_page(page_service, actor, workspace_id, **overrides):
    payload = {
        "workspace_id": str(workspace_id),
        "title": "P8.6 Edit Tools Page",
        "body_md": "# Hello\n\nInitial body.",
        "idempotency_key": str(uuid4()),
    }
    payload.update(overrides)
    response = dispatch(
        "create_context_page", payload, actor=actor, page_service=page_service
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    return response.data


def test_create_context_page_creates_shared_draft(page_service, actor):
    workspace_id = _workspace(page_service)
    data = _create_page(page_service, actor, workspace_id)
    assert data.version_status == "draft"
    assert data.editing_revision == 1


def test_read_only_actor_cannot_create_context_page(page_service, read_only_actor):
    workspace_id = _workspace(page_service)
    response = dispatch(
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "Denied",
            "idempotency_key": "denied-create",
        },
        actor=read_only_actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "rejected_by_policy"
    assert response.envelope.error_code == "scope_denied"
    assert not hasattr(response, "data")


def test_begin_context_edit_idempotently_resumes_session_by_stable_url(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    response = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{workspace_id}/{created.id}",
            "idempotency_key": "begin-resume-1",
            "from": str(created.version_id),
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.version_status == "draft"


def test_update_then_submit_context_edit_happy_path(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    updated = dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "title": created.title,
            "body_md": "# Hello\n\nUpdated body.",
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "update-happy-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert updated.envelope.result_status == "succeeded", updated.envelope
    assert updated.data.version_id == created.version_id
    assert updated.data.version_status == "draft"
    assert updated.data.editing_revision == created.editing_revision + 1

    submitted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(updated.data.version_id),
            "expected_editing_revision": updated.data.editing_revision,
            "idempotency_key": "submit-happy-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert submitted.envelope.result_status == "succeeded", submitted.envelope
    assert submitted.data.version_status == "draft"
    assert submitted.data.review_item_id is not None


def test_submit_context_edit_cannot_publish_another_workspace(page_service, actor):
    # Independent security path: an A-only PAT can name B's raw UUIDs even
    # when the router has authorized A. Real SQL and Git storage are needed
    # to prove completion leaves B's draft, published ref, and reviews alone.
    workspace_id = _workspace(page_service)
    assert actor.token_workspace_ids == (workspace_id,)
    with page_service.session_factory.begin() as session:
        other = seed_default_workspace(session, slug="submit-other", title="Other")
        other_id = other.id
        other.review_policy = {
            **other.review_policy,
            "review_required": {
                **other.review_policy["review_required"],
                "always_for_ai_authored": False,
            },
        }
    draft = page_service.create_page_first(
        workspace_slug="submit-other",
        title="Other workspace draft",
        body_md="# Other workspace\n\nThis draft must stay unpublished.",
        actor_kind="human",
        actor_id="other-author",
        actor_provenance="browser_session",
    )

    def other_state():
        with page_service.session_factory() as session:
            page = session.get(ContextPage, draft.id)
            version = session.get(ContextPageVersion, draft.version_id)
            store = session.info["context_snapshot_store"]
            return (
                page.current_version_id,
                page.current_published_version_id,
                version.version_status,
                version.editing_revision,
                version.body_md,
                version.commit_oid,
                store.list_heads(other_id),
                tuple(
                    session.execute(
                        select(ReviewItem.id, ReviewItem.status)
                        .where(ReviewItem.workspace_id == other_id)
                        .order_by(ReviewItem.id)
                    )
                ),
            )

    before = other_state()
    response = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(draft.id),
            "version_id": str(draft.version_id),
            "expected_editing_revision": draft.editing_revision,
            "idempotency_key": "submit-foreign-workspace",
        },
        actor=actor,
        page_service=page_service,
    )

    after = other_state()
    assert response.envelope.error_code == "not_found", (
        response.envelope,
        before,
        after,
    )
    assert after == before
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(MCPWriteIdempotency.id).where(
                    MCPWriteIdempotency.workspace_id == workspace_id,
                    MCPWriteIdempotency.idempotency_key == "submit-foreign-workspace",
                )
            )
            is None
        )


def test_cite_changes_references_on_private_edit_and_replays(page_service, actor):
    workspace_id = _workspace(page_service)
    target = _create_page(page_service, actor, workspace_id, title="Cite target")
    created = _create_page(page_service, actor, workspace_id)
    payload = {
        "workspace_id": str(workspace_id),
        "page_id": str(created.id),
        "version_id": str(created.version_id),
        "expected_editing_revision": created.editing_revision,
        "idempotency_key": "mcp-cite-happy-1",
        "add": [{"target_kind": "context_page", "target_id": str(target.id)}],
    }

    cited = dispatch("cite", payload, actor=actor, page_service=page_service)
    replayed = dispatch("cite", payload, actor=actor, page_service=page_service)

    assert cited.envelope.result_status == "succeeded", cited.envelope
    assert cited.data.publication_state == "draft"
    assert cited.data.editing_revision == created.editing_revision + 1
    assert [ref.target_id for ref in cited.data.added] == [target.id]
    assert replayed.envelope.result_status == "succeeded", replayed.envelope
    assert replayed.data.replayed is True
    assert replayed.data.editing_revision == cited.data.editing_revision
    assert _stored(page_service, created.version_id).version_status == "draft"


def test_update_context_edit_rejects_stale_revision_as_conflict(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    request_body = {
        "workspace_id": str(workspace_id),
        "page_id": str(created.id),
        "title": created.title,
        "body_md": "# Hello\n\nFirst update.",
        "version_id": str(created.version_id),
        "expected_editing_revision": created.editing_revision,
    }
    first = dispatch(
        "update_context_edit",
        {**request_body, "idempotency_key": "update-conflict-a"},
        actor=actor,
        page_service=page_service,
    )
    assert first.envelope.result_status == "succeeded", first.envelope

    # Same (now stale) revision resent with a fresh key: must conflict,
    # not silently clobber the save above.
    stale = dispatch(
        "update_context_edit",
        {**request_body, "idempotency_key": "update-conflict-b"},
        actor=actor,
        page_service=page_service,
    )
    assert stale.envelope.result_status == "conflict"
    assert stale.envelope.error_code == "editing_revision_conflict"
    assert stale.data is None


def test_discard_context_edit_archives_page_by_stable_url(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    response = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{workspace_id}/{created.id}",
            "version_id": str(created.version_id),
            "idempotency_key": "discard-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.page_archived is False


def test_list_my_context_edits_isolates_actors_and_paginates(
    page_service, actor, other_actor
):
    workspace_id = _workspace(page_service)
    mine = [
        _create_page(page_service, actor, workspace_id, title=f"Mine {i}")
        for i in range(3)
    ]
    theirs = _create_page(page_service, other_actor, workspace_id, title="Theirs")

    first_page = dispatch(
        "list_my_context_edits",
        {"workspace_id": str(workspace_id), "limit": 2},
        actor=actor,
        page_service=page_service,
    )
    assert first_page.envelope.result_status == "succeeded", first_page.envelope
    assert len(first_page.data.items) == 2
    assert first_page.data.next_cursor is not None
    first_page_ids = {item.page_id for item in first_page.data.items}
    assert theirs.id not in first_page_ids

    second_page = dispatch(
        "list_my_context_edits",
        {
            "workspace_id": str(workspace_id),
            "limit": 2,
            "cursor": first_page.data.next_cursor,
        },
        actor=actor,
        page_service=page_service,
    )
    assert second_page.envelope.result_status == "succeeded", second_page.envelope
    assert len(second_page.data.items) == 1
    assert second_page.data.next_cursor is None
    second_page_ids = {item.page_id for item in second_page.data.items}
    assert theirs.id not in second_page_ids
    assert first_page_ids | second_page_ids == {page.id for page in mine}

    other_listing = dispatch(
        "list_my_context_edits",
        {"workspace_id": str(workspace_id)},
        actor=other_actor,
        page_service=page_service,
    )
    assert other_listing.envelope.result_status == "succeeded"
    assert [item.page_id for item in other_listing.data.items] == [theirs.id]
    other_item = other_listing.data.items[0]
    assert other_item.version_id == theirs.version_id
    assert other_item.editing_revision == theirs.editing_revision
    assert other_item.content_hash == theirs.content_hash


def test_list_my_context_edits_never_leaks_a_named_page_id_it_does_not_own(
    page_service, actor, other_actor
):
    """The same isolation property, phrased as an active-probe negative: even
    naming another actor's page_id in a filterless list call must not surface
    that actor's row."""

    workspace_id = _workspace(page_service)
    _create_page(page_service, other_actor, workspace_id, title="Not Yours")

    response = dispatch(
        "list_my_context_edits",
        {"workspace_id": str(workspace_id)},
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    assert response.data.items == []


def test_begin_context_edit_rejects_unknown_page(page_service, actor):
    workspace_id = _workspace(page_service)
    response = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(uuid4()),
            "idempotency_key": "begin-missing-page",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "not_found"


def _structure_map(workspace_id, page_id, actor, page_service):
    return dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "page_id": str(page_id)},
        actor=actor,
        page_service=page_service,
    )


def test_patch_context_edit_happy_path_single_block_replace(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    before_map = _structure_map(workspace_id, created.id, actor, page_service)
    assert before_map.envelope.result_status == "succeeded", before_map.envelope
    target_block = next(b for b in before_map.blocks if b.preview == "Initial body.")

    response = dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "operations": [
                {
                    "op": "replace_block",
                    "block": target_block.id,
                    "expected": target_block.hash,
                    "content": "Patched via block address.",
                }
            ],
            "idempotency_key": "patch-happy-1",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.version_status == "draft"
    assert response.data.editing_revision == created.editing_revision + 1
    body = _stored(page_service, created.version_id).body_md
    assert "Patched via block address." in body
    assert "Initial body." not in body


def test_patch_context_edit_multi_operation_patch(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(
        page_service,
        actor,
        workspace_id,
        body_md="# Hello\n\nFirst paragraph.\n\nSecond paragraph.",
    )

    before_map = _structure_map(workspace_id, created.id, actor, page_service)
    assert before_map.envelope.result_status == "succeeded", before_map.envelope
    first_block = next(b for b in before_map.blocks if b.preview == "First paragraph.")
    second_block = next(
        b for b in before_map.blocks if b.preview == "Second paragraph."
    )

    response = dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "operations": [
                {
                    "op": "replace_block",
                    "block": first_block.id,
                    "expected": first_block.hash,
                    "content": "First, edited.",
                },
                {
                    "op": "replace_block",
                    "block": second_block.id,
                    "expected": second_block.hash,
                    "content": "Second, edited.",
                },
            ],
            "idempotency_key": "patch-multi-1",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.editing_revision == created.editing_revision + 1
    body = _stored(page_service, created.version_id).body_md
    assert "First, edited." in body
    assert "Second, edited." in body
    assert "First paragraph." not in body
    assert "Second paragraph." not in body


def test_patch_context_edit_rejects_stale_revision_as_masked_conflict(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    operations = [
        {
            "op": "replace_text",
            "old_text": "Initial body.",
            "new_text": "First edit.",
        }
    ]
    first = dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "operations": operations,
            "idempotency_key": "patch-conflict-a",
        },
        actor=actor,
        page_service=page_service,
    )
    assert first.envelope.result_status == "succeeded", first.envelope

    # Same (now stale) expected_editing_revision resent with a fresh key and a
    # DIFFERENT operation set: must conflict, not silently apply on top.
    stale = dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "First edit.",
                    "new_text": "Second edit.",
                }
            ],
            "idempotency_key": "patch-conflict-b",
        },
        actor=actor,
        page_service=page_service,
    )
    assert stale.envelope.result_status == "conflict"
    assert stale.envelope.error_code == "editing_revision_conflict"
    assert stale.data is None
    # The conflict names the draft's current version so the caller can re-read.
    assert f"server_version_id={first.data.version_id}" in (
        stale.envelope.error_message or ""
    )
    assert f"server_content_hash={first.data.content_hash}" in (
        stale.envelope.error_message or ""
    )


def test_patch_context_edit_replay_is_idempotent(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    request_body = {
        "workspace_id": str(workspace_id),
        "page_id": str(created.id),
        "version_id": str(created.version_id),
        "expected_editing_revision": created.editing_revision,
        "operations": [
            {
                "op": "replace_text",
                "old_text": "Initial body.",
                "new_text": "Replayed edit.",
            }
        ],
        "idempotency_key": "patch-replay-1",
    }

    first = dispatch(
        "patch_context_edit", request_body, actor=actor, page_service=page_service
    )
    assert first.envelope.result_status == "succeeded", first.envelope
    assert first.data.editing_revision == created.editing_revision + 1

    replay = dispatch(
        "patch_context_edit", request_body, actor=actor, page_service=page_service
    )
    assert replay.envelope.result_status == "succeeded", replay.envelope
    assert replay.data.version_id == first.data.version_id
    assert replay.data.editing_revision == first.data.editing_revision
    assert replay.data.content_hash == first.data.content_hash
    assert replay.data.edit_receipt.model_dump() == first.data.edit_receipt.model_dump()
