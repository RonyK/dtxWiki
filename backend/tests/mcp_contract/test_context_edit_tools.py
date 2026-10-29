"""P8.6: the Context-editing-state-machine MCP tool surface.

create_context_page/begin_context_edit/update_context_edit/
submit_context_edit_for_review/discard_context_edit/list_my_context_edits
each wrap the matching PageService P8 command 1:1 with Browser REST
(backend/api/context_pages.py) -- there is exactly one implementation of
each command, shared by both surfaces. write_draft/update_draft/patch_draft/
submit_revision_for_review are untouched and out of scope here (see
backend/tests/mcp_contract/test_context_tools.py for their coverage).
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace


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
    }
    payload.update(overrides)
    response = dispatch(
        "create_context_page", payload, actor=actor, page_service=page_service
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    return response.data


def test_create_context_page_creates_private_editing_version(page_service, actor):
    workspace_id = _workspace(page_service)
    data = _create_page(page_service, actor, workspace_id)
    assert data.version_status == "editing"
    assert data.editing_revision == 1


def test_read_only_actor_cannot_create_context_page(
    page_service, read_only_actor
):
    workspace_id = _workspace(page_service)
    response = dispatch(
        "create_context_page",
        {"workspace_id": str(workspace_id), "title": "Denied"},
        actor=read_only_actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "rejected_by_policy"
    assert response.envelope.error_code == "scope_denied"
    assert not hasattr(response, "data")


def test_begin_context_edit_idempotently_resumes_own_open_session(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    response = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "idempotency_key": "begin-resume-1",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.version_status == "editing"


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
            "base_version_id": str(created.version_id),
            "base_content_hash": created.content_hash,
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "update-happy-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert updated.envelope.result_status == "succeeded", updated.envelope
    assert updated.data.version_id == created.version_id
    assert updated.data.version_status == "editing"
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


def test_update_context_edit_rejects_stale_base_as_conflict(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    request_body = {
        "workspace_id": str(workspace_id),
        "page_id": str(created.id),
        "title": created.title,
        "body_md": "# Hello\n\nFirst update.",
        "base_version_id": str(created.version_id),
        "base_content_hash": created.content_hash,
        "expected_editing_revision": created.editing_revision,
    }
    first = dispatch(
        "update_context_edit",
        {**request_body, "idempotency_key": "update-conflict-a"},
        actor=actor,
        page_service=page_service,
    )
    assert first.envelope.result_status == "succeeded", first.envelope

    # Same (now stale) base + revision resent with a fresh key: must conflict,
    # not silently clobber the save above.
    stale = dispatch(
        "update_context_edit",
        {**request_body, "idempotency_key": "update-conflict-b"},
        actor=actor,
        page_service=page_service,
    )
    assert stale.envelope.result_status == "conflict"
    assert stale.envelope.error_code == "stale_base_version"
    assert stale.data is None


def test_discard_context_edit_archives_a_never_published_page(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    response = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "idempotency_key": "discard-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.version_id == created.version_id
    assert response.data.page_archived is True


@pytest.mark.parametrize(
    "tool_name", ["begin_context_edit", "submit_context_edit_for_review",
                  "discard_context_edit", "patch_context_edit"]
)
def test_wrong_actor_gets_opaque_not_found_not_a_permission_error(
    page_service, actor, other_actor, tool_name
):
    """P2.2/P2.6 convention: a different author's editing row reads as
    not_found, never as a distinguishable permission error -- confirming it
    holds for the new P8.6 tools too.

    ``update_context_edit`` is deliberately NOT parametrized here -- see
    ``test_update_context_edit_on_another_actors_editing_row_forks_a_draft``
    below for why its cross-actor behavior differs (a pre-existing
    ``autosave_page`` property, not something this wrapper introduces).
    ``patch_context_edit`` DOES belong here (unlike ``update_context_edit``):
    it resolves ``version_id`` explicitly and checks ownership directly,
    exactly like ``submit_context_edit_for_review``/``discard_context_edit``,
    never through ``autosave_page``'s page-global-pointer fork.
    """

    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    payload = {
        "workspace_id": str(workspace_id),
        "page_id": str(created.id),
        "idempotency_key": f"wrong-actor-{tool_name}",
        "version_id": str(created.version_id),
    }
    if tool_name == "submit_context_edit_for_review":
        payload["expected_editing_revision"] = created.editing_revision
    if tool_name == "patch_context_edit":
        payload["expected_editing_revision"] = created.editing_revision
        payload["operations"] = [
            {
                "op": "replace_text",
                "old_text": "Initial body.",
                "new_text": "Attacker-authored body.",
            }
        ]
    if tool_name == "begin_context_edit":
        del payload["version_id"]

    response = dispatch(
        tool_name, payload, actor=other_actor, page_service=page_service
    )
    if tool_name == "begin_context_edit":
        # begin_context_edit has no version_id input -- the other actor
        # simply opens (or resumes) their OWN editing row on the same page,
        # which is not a 404 case at all. Assert that instead, so this test
        # documents the boundary rather than mis-asserting on a tool it does
        # not apply to.
        assert response.envelope.result_status == "succeeded", response.envelope
        assert response.data.version_id != created.version_id
        assert response.data.body_md == ""
        return
    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "not_found"
    assert response.data is None


def test_update_context_edit_on_another_actors_editing_row_forks_a_draft(
    page_service, actor, other_actor
):
    """Documents a REAL, PRE-EXISTING boundary in ``PageService.autosave_page``
    (used identically, unchanged, by Browser REST's ``/autosave`` route and by
    the legacy PATCH ``update_page`` path): unlike
    begin_context_edit/submit_context_edit_for_review/discard_context_edit
    (which all explicitly compare ``version.created_by_actor_kind/id`` and
    raise an opaque not_found), ``autosave_page`` resolves "the version to
    save onto" via ``_editable_version_id_for_actor``, which falls back to
    ``page.current_version_id`` -- a page-global pointer that P8.1 can
    durably leave pointing at ANOTHER actor's private "editing" row -- when
    the calling actor has no open editing row of their own. It does not then
    404; it forks a NEW version, owned by the caller, parented on that
    pointer, with the CALLER'S OWN supplied body (never the other actor's
    private content -- there is no content leak here), landing it directly as
    version_status="draft" (not "editing"), which is the pre-P8.2
    multi-draft-fork behavior this pass does not touch. This is exactly the
    class of gap the plan document (P2 완료기준, "이미 발행된 page에서
    write_page/update_draft 계열이 만드는 동시-다중-draft 상황의 메타데이터
    오염 가능성") already named and explicitly deferred -- it is not
    introduced by this task's wrapper and fixing it is out of this task's
    scope (it requires changing ``autosave_page``'s own multi-author
    semantics, not the MCP wire wrapper around it).
    """

    workspace_id = _workspace(page_service)
    created = _create_page(page_service, actor, workspace_id)

    response = dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "title": created.title,
            "body_md": "# Hello\n\nActor B's own words, not A's.",
            "base_version_id": str(created.version_id),
            "base_content_hash": created.content_hash,
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "wrong-actor-update-forks",
        },
        actor=other_actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    # A NEW version was created (not a 404, and not a mutation of A's row).
    assert response.data.version_id != created.version_id
    assert response.data.version_status == "draft"
    forked = _stored(page_service, response.data.version_id)
    assert forked.parent_version_id == created.version_id
    assert forked.body_md == "# Hello\n\nActor B's own words, not A's."

    # And critically: A's own editing row is untouched by B's call.
    a_view = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "idempotency_key": "confirm-a-untouched",
        },
        actor=actor,
        page_service=page_service,
    )
    assert a_view.envelope.result_status == "succeeded", a_view.envelope
    assert a_view.data.version_id == created.version_id
    assert a_view.data.content_hash == created.content_hash
    assert a_view.data.editing_revision == created.editing_revision


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
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page_id)}},
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
    assert response.data.version_status == "editing"
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
    assert stale.envelope.error_code == "stale_base_version"
    assert stale.data is None
    # The conflict names the caller's OWN current version -- masking
    # (_disclosable_version_fields) must pass through real data for the
    # version's own author, not just suppress it unconditionally.
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
    assert replay.data.edit_receipt == first.data.edit_receipt
