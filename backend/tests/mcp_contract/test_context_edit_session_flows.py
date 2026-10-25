"""Whole MCP editing sessions, not single calls.

Every test in test_context_edit_tools.py makes one write and stops. The
failures agents actually hit came from the second and later writes in a
session: a whole-body update left the block map describing the previous body,
so the next block patch was refused; a second update forked a draft; two
unscoped replace_text ops in one batch always "overlapped". These tests drive
the realistic sequences end to end.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.domain.input_policy import InputPolicyError
from backend.mcp.router import dispatch
from backend.models.change import ChangeOperation
from backend.models.context import ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.utils.block_identity import compute_block_map_with_ids


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(
        actor_id="session-flow-actor", scopes=("draft-write", "read")
    )


def _workspace(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _create(page_service, actor, workspace_id, body_md):
    response = dispatch(
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "Session Flow Page",
            "body_md": body_md,
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    return response.data


def _update(page_service, actor, workspace_id, base, body_md, key):
    return dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{workspace_id}/{base.id}",
            "title": base.title,
            "body_md": body_md,
            "base_version_id": str(base.version_id),
            "base_content_hash": base.content_hash,
            "expected_editing_revision": base.editing_revision,
            "idempotency_key": key,
        },
        actor=actor,
        page_service=page_service,
    )


def _patch(page_service, actor, workspace_id, base, operations, key):
    return dispatch(
        "patch_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{base.id}",
            "version_id": str(base.version_id),
            "expected_editing_revision": base.editing_revision,
            "operations": operations,
            "idempotency_key": key,
        },
        actor=actor,
        page_service=page_service,
    )


def _structure_map(page_service, actor, workspace_id, page_id):
    response = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": f"/p/{workspace_id}/{page_id}"},
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    return response


def _block(structure, preview):
    return next(block for block in structure.blocks if block.preview == preview)


def _review_statuses_for_page(page_service, page_id):
    with page_service.session_factory() as session:
        return sorted(
            session.scalars(
                select(ReviewItem.status)
                .join(
                    ChangeOperation,
                    ChangeOperation.change_set_id == ReviewItem.change_set_id,
                )
                .where(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                )
            ).all()
        )


def _save(page_service, actor, workspace_id, page_id, body_md, revision, key, **extra):
    """update_context_edit as an agent sends it: no base, only the revision
    it last saw (and, when given, the edit's version_id)."""

    return dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{workspace_id}/{page_id}",
            "body_md": body_md,
            "expected_editing_revision": revision,
            "idempotency_key": key,
            **extra,
        },
        actor=actor,
        page_service=page_service,
    )


def _open_edit_details(envelope) -> tuple[str, int]:
    """The (version_id, editing_revision) a stale-write conflict names."""

    by_field = {detail.field: detail for detail in envelope.error_details}
    assert {"version_id", "expected_editing_revision"} <= set(by_field), envelope
    version_id = by_field["version_id"].message.split("version_id=")[1].rstrip(".")
    revision = by_field["expected_editing_revision"].message.split("editing_revision=")[
        1
    ]
    return version_id, int(revision.rstrip("."))


def _stored(page_service, version_id) -> ContextPageVersion:
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None
        session.expunge(version)
        return version


def test_repeated_updates_stay_on_one_editing_row_until_submit(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Hello\n\nInitial body.")
    short_link = f"/p/{created.id}"
    full_link = f"/p/{workspace_id}/{created.id}"

    first = _update(
        page_service, actor, workspace_id, created,
        f"# Hello\n\nFirst save. [Self]({short_link})", "u-1"
    )
    assert first.envelope.result_status == "succeeded", first.envelope
    second = _update(
        page_service, actor, workspace_id, first.data,
        f"# Hello\n\nSecond save. [Self]({short_link})", "u-2"
    )
    assert second.envelope.result_status == "succeeded", second.envelope

    # A second autosave must not fork a draft: same row, still editing.
    assert second.data.version_id == created.version_id
    assert second.data.version_status == "editing"
    assert second.data.editing_revision == created.editing_revision + 2
    # Autosaves of a private editing row are not review events.
    assert _review_statuses_for_page(page_service, created.id) == []
    assert second.data.review_item_id is None

    operations = [{
        "op": "replace_text",
        "old_text": f"Second save. [Self]({full_link})",
        "new_text": f"Patched. [Self]({short_link})",
    }]
    patched = _patch(
        page_service,
        actor,
        workspace_id,
        second.data,
        operations,
        "p-1",
    )
    assert patched.envelope.result_status == "succeeded", patched.envelope
    # An open edit is the page's draft head while it is being edited; that is
    # the normal state, not an inconsistency to report on every save.
    for response in (first, second, patched):
        assert response.data.history_status == "current_editing"
        assert response.data.inconsistency_reasons == []
        assert response.data.lint_report == {
            "fixes": [{
                "module_id": "links", "rule_id": "link.internal-noncanonical",
                "before": short_link, "after": full_link, "category": "format",
            }],
            "findings": [],
        }

    # Existing session fixture covers three persistence boundaries: MCP update,
    # typed patch, and browser autosave. Parser/preparer units cannot prove that
    # the final block map, response report and idempotency ledger agree.
    patch_replay = _patch(
        page_service, actor, workspace_id, second.data, operations, "p-1"
    )
    assert patch_replay.data == patched.data
    structure = _structure_map(page_service, actor, workspace_id, created.id)
    stored = _stored(page_service, created.version_id)
    assert not structure.document.map_repaired
    assert (
        structure.document.root_hash == patched.data.content_hash == stored.content_hash
    )
    assert {block["id"]: block["hash"] for block in stored.block_map} == {
        block.id: block.hash for block in structure.blocks
    }
    assert stored.body_md == f"# Hello\n\nPatched. [Self]({full_link})"
    save_args = dict(
        page_id=created.id, workspace_id=workspace_id,
        body_md=f"# Hello\n\nAutosave. [Self]({short_link})",
        title=created.title, actor_kind=actor.actor_kind, actor_id=actor.actor_id,
        actor_provenance="browser_session", base_version_id=created.version_id,
        base_content_hash=patched.data.content_hash,
        expected_editing_revision=patched.data.editing_revision,
        idempotency_key="browser-link-report", with_edit_receipt=True,
    )
    saved = page_service.autosave_page(**save_args)
    replayed = page_service.autosave_page(**save_args)
    assert saved.lint_report == patched.data.lint_report == replayed.lint_report
    assert saved._edit_receipt == replayed._edit_receipt
    assert saved.body_md == f"# Hello\n\nAutosave. [Self]({full_link})"

    submitted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": f"/p/{created.id}",
            "version_id": str(created.version_id),
            "expected_editing_revision": saved.editing_revision,
            "idempotency_key": "s-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert submitted.envelope.result_status == "succeeded", submitted.envelope
    assert submitted.data.lint_report == {"fixes": [], "findings": []}
    assert _review_statuses_for_page(page_service, created.id) == ["pending"]


def test_resubmitting_invalid_legacy_edit_is_rejected_atomically(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Valid\n\nOriginal body.")
    # Simulate an editing draft written before the policy existed. Historical
    # text stays stored and readable; submitting it is a new write boundary.
    # The Context-only H1 rule proves the full stored-body validator runs; its
    # body, tag-count and NUL rules are unit-tested in test_content_input_policy.
    legacy_body = "# " + "x" * 257
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, created.version_id)
        version.body_md = legacy_body

    with pytest.raises(InputPolicyError):
        dispatch(
            "submit_context_edit_for_review",
            {
                "workspace_id": str(workspace_id),
                "page_id": str(created.id),
                "version_id": str(created.version_id),
                "expected_editing_revision": created.editing_revision,
                "idempotency_key": "reject-legacy-submit",
            },
            actor=actor,
            page_service=page_service,
        )
    stored = _stored(page_service, created.version_id)
    assert stored.version_status == "editing"
    assert stored.body_md == legacy_body
    assert stored.editing_revision == created.editing_revision
    assert _review_statuses_for_page(page_service, created.id) == []


def test_a_whole_body_update_stores_the_map_of_its_own_body(page_service, actor):
    """The block map is a durable claim about the body; an update must rewrite
    it with the body. Checked right after the update: a later patch would
    rebuild a stale map and hide the defect."""

    from backend.utils.block_identity import validated_block_map_for_version

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Hello\n\nInitial body.")
    updated = _update(
        page_service,
        actor,
        workspace_id,
        created,
        "# Hello\n\nOne.\n\nTwo.\n\nThree.",
        "u-1",
    )
    assert updated.envelope.result_status == "succeeded", updated.envelope

    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, created.version_id)
        assert version is not None
        assert version.body_md == "# Hello\n\nOne.\n\nTwo.\n\nThree."
        # Raises BlockMapTransitionError when the map describes another body.
        validated_block_map_for_version(version.block_map, version.body_md)


def test_two_unscoped_replace_texts_apply_together(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "# Hello\n\nThe alpha word lives here.\n\nThe omega word lives there.",
    )
    response = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {"op": "replace_text", "old_text": "alpha", "new_text": "ALPHA"},
            {"op": "replace_text", "old_text": "omega", "new_text": "OMEGA"},
        ],
        "p-1",
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    assert response.data.editing_revision == created.editing_revision + 1
    body = response.data.body_md
    assert "The ALPHA word lives here." in body
    assert "The OMEGA word lives there." in body
    # Both edits in one splice: the document is not duplicated.
    assert body.count("# Hello") == 1
    assert body.count("lives here") == 1


def test_really_overlapping_replace_texts_still_reject_with_indices(
    page_service, actor
):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Hello\n\nThe alpha omega phrase."
    )
    response = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {"op": "replace_text", "old_text": "alpha omega", "new_text": "x"},
            {"op": "replace_text", "old_text": "omega phrase", "new_text": "y"},
        ],
        "p-1",
    )
    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "operations_overlap"
    details = response.envelope.error_details
    assert details, "an overlap must say which operations collided"
    assert details[0].field == "operations[1]"
    assert "operations[0]" in details[0].message
    assert "operations[1]" in details[0].message


def test_replace_block_does_not_swallow_an_adjacent_block(page_service, actor):
    """A heading may be followed directly by a paragraph. Replacing the
    heading with a paragraph must not lazily merge the neighbour into it."""

    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        "# Title\nText right under the heading.\n\nTail paragraph.",
    )
    structure = _structure_map(page_service, actor, workspace_id, created.id)
    heading = _block(structure, "# Title")
    response = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_block",
                "block": heading.id,
                "expected": heading.hash,
                "content": "Plain paragraph now.",
            }
        ],
        "p-1",
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    after = _structure_map(page_service, actor, workspace_id, created.id)
    previews = [block.preview for block in after.blocks]
    assert "Plain paragraph now." in previews
    assert "Text right under the heading." in previews


def test_replace_block_with_an_unclosed_fence_is_a_reflow_error(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Hello\n\nFirst.\n\nSecond.\n\nThird."
    )
    structure = _structure_map(page_service, actor, workspace_id, created.id)
    first = _block(structure, "First.")
    response = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_block",
                "block": first.id,
                "expected": first.hash,
                "content": "```python\nprint('never closed')",
            }
        ],
        "p-1",
    )
    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "block_reflow"


def test_a_drifted_map_on_an_open_edit_heals_instead_of_failing(page_service, actor):
    """Rows written before the map was kept in step still carry a map for an
    older body. For the author's own open edit that is recoverable: unchanged
    blocks keep their ids, changed ones get deterministic fresh ids."""

    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Hello\n\nOne.\n\nTwo.\n\nThree."
    )
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, created.version_id)
        stale = compute_block_map_with_ids(
            "# Hello\n\nOld one.\n\nTwo.\n\nThree.",
            previous_body_md=None,
            previous_block_map=None,
        )
        version.block_map = stale
    stale_ids = {entry["hash"]: entry["id"] for entry in stale}

    structure = _structure_map(page_service, actor, workspace_id, created.id)
    assert structure.document.map_repaired is True
    two = _block(structure, "Two.")
    assert two.id == stale_ids[two.hash]
    one = _block(structure, "One.")
    response = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_block",
                "block": one.id,
                "expected": one.hash,
                "content": "One, patched.",
            }
        ],
        "p-1",
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    assert "One, patched." in response.data.body_md


def test_a_patch_receipt_is_enough_to_chain_the_next_block_patch(page_service, actor):
    """The receipt names every changed block's new id/hash and the unchanged
    ones keep theirs, so an agent can keep patching without re-reading."""

    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Hello\n\nOne.\n\nTwo.\n\nThree."
    )
    structure = _structure_map(page_service, actor, workspace_id, created.id)
    one = _block(structure, "One.")
    two = _block(structure, "Two.")
    first = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_block",
                "block": one.id,
                "expected": one.hash,
                "content": "One, edited.",
            }
        ],
        "p-1",
    )
    assert first.envelope.result_status == "succeeded", first.envelope
    receipt = first.data.edit_receipt
    assert receipt is not None
    assert receipt.edit_mode == "patch"
    assert receipt.editing_revision_after == first.data.editing_revision
    assert receipt.operations[0].op == "replace_block"
    assert receipt.operations[0].affected_block_ids == [one.id]
    assert [block.id for block in receipt.changed_blocks] == [one.id]
    assert receipt.first_change is not None
    assert receipt.first_change.after_preview.startswith("One, edited.")

    # `two` was untouched, so the id/hash read before the first patch still
    # addresses it.
    second = _patch(
        page_service,
        actor,
        workspace_id,
        first.data,
        [
            {
                "op": "replace_block",
                "block": two.id,
                "expected": two.hash,
                "content": "Two, edited.",
            }
        ],
        "p-2",
    )
    assert second.envelope.result_status == "succeeded", second.envelope
    # And the block the first patch changed is addressable from the receipt.
    changed = first.data.edit_receipt.changed_blocks[0]
    third = _patch(
        page_service,
        actor,
        workspace_id,
        second.data,
        [
            {
                "op": "replace_block",
                "block": changed.id,
                "expected": changed.hash,
                "content": "One, edited twice.",
            }
        ],
        "p-3",
    )
    assert third.envelope.result_status == "succeeded", third.envelope
    assert "One, edited twice." in third.data.body_md


def test_update_receipt_and_update_without_base_fields(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Hello\n\n## Part A\n\nKeep.\n"
    )
    response = dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "body_md": "# Hello\n\n## Part A\n\nKeep.\n\n## Part B\n\nNew text.\n",
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "u-no-base",
        },
        actor=actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded", response.envelope
    receipt = response.data.edit_receipt
    assert receipt.edit_mode == "whole_body"
    assert receipt.editing_revision_before == created.editing_revision
    assert receipt.lines_added >= 3
    assert ["Hello", "Part B"] in receipt.changed_sections
    assert "New text." in receipt.first_change.after_preview

    stale = dispatch(
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "body_md": "# Hello\n\nLost update.\n",
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "u-no-base-stale",
        },
        actor=actor,
        page_service=page_service,
    )
    assert stale.envelope.result_status == "conflict"
    assert (
        f"server_editing_revision={response.data.editing_revision}"
        in stale.envelope.error_message
    )


def test_patch_conflict_names_the_current_revision(page_service, actor):
    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Hello\n\nBody.")
    first = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [{"op": "replace_text", "old_text": "Body.", "new_text": "Body 1."}],
        "p-1",
    )
    stale = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [{"op": "replace_text", "old_text": "Body 1.", "new_text": "Body 2."}],
        "p-2",
    )
    assert stale.envelope.result_status == "conflict"
    assert (
        f"server_editing_revision={first.data.editing_revision}"
        in stale.envelope.error_message
    )


def test_structure_map_my_edit_reads_the_open_edit_without_opening_one(
    page_service, actor, mcp_default_actor
):
    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Hello\n\nMine.")
    mine = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(created.id)},
            "version": "my_edit",
        },
        actor=actor,
        page_service=page_service,
    )
    assert mine.envelope.result_status == "succeeded", mine.envelope
    assert mine.document.version_id == created.version_id
    assert mine.document.resolved_version_source == "my_edit"
    assert mine.document.editing_revision == created.editing_revision

    other = mcp_default_actor(actor_id="session-flow-other", scopes=("read",))
    theirs = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(created.id)},
            "version": "my_edit",
        },
        actor=other,
        page_service=page_service,
    )
    assert theirs.envelope.result_status == "failed"
    assert theirs.envelope.error_code == "not_found"
    with page_service.session_factory() as session:
        editing_rows = session.scalars(
            select(ContextPageVersion).where(
                ContextPageVersion.page_id == created.id,
                ContextPageVersion.version_status == "editing",
            )
        ).all()
    assert len(editing_rows) == 1


def _read_visible(page_service, reader, workspace_id, page_id):
    return dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page_id)}},
        actor=reader,
        page_service=page_service,
    )


def test_discarding_an_edited_new_page_archives_it_and_hides_it(
    page_service, actor, mcp_default_actor
):
    """Saving an open edit points the page's draft pointer at it. A discard
    that left the pointer there kept a never-published page alive and served
    the thrown-away text to every editor as the page's draft."""

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Secret\n\nFirst.")
    patched = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [{"op": "replace_text", "old_text": "First.", "new_text": "Private."}],
        "p-1",
    )
    assert patched.envelope.result_status == "succeeded", patched.envelope

    discarded = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "idempotency_key": "d-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert discarded.envelope.result_status == "succeeded", discarded.envelope
    assert discarded.data.page_archived is True

    other_editor = mcp_default_actor(
        actor_id="session-flow-other-editor", scopes=("draft-write", "read")
    )
    seen = _read_visible(page_service, other_editor, workspace_id, created.id)
    assert seen.envelope.result_status == "failed"
    assert seen.envelope.error_code == "not_found"


def test_discarding_an_edit_of_a_published_page_restores_its_head(
    page_service, actor, mcp_default_actor
):
    workspace_id = _workspace(page_service)
    published = page_service.write_page(
        workspace_slug="default",
        slug="discard-restores-head",
        title="Discard Restores Head",
        body_md="# Public\n\nPublished text.",
        actor_kind="human",
        actor_id="session-flow-publisher",
        actor_provenance="browser_session",
    )
    begun = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(published.id),
            "idempotency_key": "b-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert begun.envelope.result_status == "succeeded", begun.envelope
    patched = _patch(
        page_service,
        actor,
        workspace_id,
        begun.data,
        [
            {
                "op": "replace_text",
                "old_text": "Published text.",
                "new_text": "Private rewrite.",
            }
        ],
        "p-1",
    )
    assert patched.envelope.result_status == "succeeded", patched.envelope

    discarded = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(published.id),
            "version_id": str(begun.data.version_id),
            "idempotency_key": "d-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert discarded.envelope.result_status == "succeeded", discarded.envelope
    assert discarded.data.page_archived is False

    other_editor = mcp_default_actor(
        actor_id="session-flow-other-editor-2", scopes=("draft-write", "read")
    )
    seen = _read_visible(page_service, other_editor, workspace_id, published.id)
    assert seen.envelope.result_status == "succeeded", seen.envelope
    assert seen.document.version_id == published.version_id
    assert "Private rewrite." not in "".join(block.preview for block in seen.blocks)
    # Naming the discarded row directly does not reveal it either: it was a
    # private edit, and discarding it must not publish it to other editors.
    by_id = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"version_id": str(begun.data.version_id)},
        },
        actor=other_editor,
        page_service=page_service,
    )
    assert by_id.envelope.result_status == "failed"
    assert by_id.envelope.error_code == "not_found"
    with page_service.session_factory() as session:
        from backend.models.context import ContextPage

        page = session.get(ContextPage, published.id)
        assert page.latest_draft_version_id is None
        assert page.current_version_id == published.version_id


def test_submit_conflict_names_the_current_revision(page_service, actor):
    """Every conflict on the caller's own open edit names where that edit
    stands now -- submit included, not only patch and update."""

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Hello\n\nBody.")
    patched = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [{"op": "replace_text", "old_text": "Body.", "new_text": "Body 1."}],
        "p-1",
    )
    assert patched.envelope.result_status == "succeeded", patched.envelope

    stale = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": created.editing_revision,
            "idempotency_key": "s-stale",
        },
        actor=actor,
        page_service=page_service,
    )
    assert stale.envelope.result_status == "conflict"
    assert stale.envelope.error_code == "stale_base_version"
    assert (
        f"server_editing_revision={patched.data.editing_revision}"
        in stale.envelope.error_message
    )
    assert _open_edit_details(stale.envelope) == (
        str(created.version_id),
        patched.data.editing_revision,
    )

    retried = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": patched.data.editing_revision,
            "idempotency_key": "s-retry",
        },
        actor=actor,
        page_service=page_service,
    )
    assert retried.envelope.result_status == "succeeded", retried.envelope


def test_a_copy_slip_is_forgiven_in_a_block_and_refused_without_one(
    page_service, actor
):
    """A lower-tier model copies a sentence with one wrong letter. Scoped to
    the block it read, the edit lands and the receipt says it was forgiven;
    unscoped, nothing changes and the refusal points at the block to use."""

    workspace_id = _workspace(page_service)
    sentence = "Install the package and restart the service afterwards."
    typo = sentence.replace("restart", "restat")
    created = _create(
        page_service,
        actor,
        workspace_id,
        f"# Guide\n\n{sentence}\n\nKeep the default port as it is.",
    )

    refused = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {"op": "replace_text", "old_text": "Guide", "new_text": "Manual"},
            {"op": "replace_text", "old_text": typo, "new_text": "Replaced."},
        ],
        "p-unscoped",
    )
    assert refused.envelope.result_status == "failed"
    assert refused.envelope.error_code == "text_match_not_found"
    detail = refused.envelope.error_details[0]
    assert detail.operation_index == 1
    structure = _structure_map(page_service, actor, workspace_id, created.id)
    block = _block(structure, sentence)
    assert detail.candidates[0].block_id == block.id
    # A refusal is atomic: the first, valid operation did not land either.
    assert structure.document.editing_revision == created.editing_revision
    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, created.version_id)
        assert stored.body_md.startswith("# Guide")
        assert stored.editing_revision == created.editing_revision

    forgiven = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_text",
                "old_text": typo,
                "new_text": "Install the package, then restart the service.",
                "at": {"by": "block", "block": block.id, "expected": block.hash},
            }
        ],
        "p-block",
    )
    assert forgiven.envelope.result_status == "succeeded", forgiven.envelope
    assert "Install the package, then restart the service." in forgiven.data.body_md
    assert sentence not in forgiven.data.body_md
    receipt_op = forgiven.data.edit_receipt.operations[0]
    assert receipt_op.match_kind == "fuzzy"
    assert 0.9 < receipt_op.match_similarity < 1


def test_an_ai_save_never_lands_on_an_edit_begun_after_the_one_it_saw(
    page_service, actor
):
    """B-5. One person works through an AI client and the browser; both are
    the same actor. The AI saves its edit; in the browser the person discards
    it, begins a new one and types. The AI's next save carries the revision
    it last saw. Every new editing row used to restart at revision 1, so that
    revision matched the browser's row and the AI's text silently replaced
    the person's new work."""

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Doc\n\nFirst body.\n")
    ai_saved = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nAI draft text.\n",
        created.editing_revision,
        "ai-1",
    )
    assert ai_saved.envelope.result_status == "succeeded", ai_saved.envelope
    seen_by_ai = ai_saved.data.editing_revision

    # The browser, as the same actor.
    page_service.discard_context_edit(
        page_id=created.id,
        version_id=created.version_id,
        workspace_id=workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        idempotency_key="browser-discard",
    )
    begun = page_service.begin_context_edit(
        page_id=created.id,
        workspace_id=workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance="browser_session",
        idempotency_key="browser-begin",
    )
    assert begun.version_id != created.version_id
    # The new row starts above every revision this author had on the page.
    assert begun.editing_revision > seen_by_ai
    typed = page_service.autosave_page(
        page_id=created.id,
        workspace_id=workspace_id,
        title=None,
        body_md="# Doc\n\nThe person's new work.\n",
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        base_version_id=begun.version_id,
        base_content_hash=begun.content_hash,
        actor_provenance="browser_session",
        idempotency_key="browser-save",
        expected_editing_revision=begun.editing_revision,
    )

    stale = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nAI draft text, second pass.\n",
        seen_by_ai,
        "ai-2",
    )
    assert stale.envelope.result_status == "conflict", stale.envelope
    assert stale.envelope.error_code == "stale_base_version"
    stored = _stored(page_service, begun.version_id)
    assert stored.body_md == typed.body_md
    assert stored.editing_revision == typed.editing_revision
    # It names the edit that is open now, and says to re-read it -- not to
    # resend the same body with the new revision.
    assert _open_edit_details(stale.envelope) == (
        str(begun.version_id),
        typed.editing_revision,
    )
    assert "Re-read" in stale.envelope.result_summary


def test_an_update_naming_its_edit_refuses_any_other_one(page_service, actor):
    """version_id binds update_context_edit to the edit the caller read: a
    revision that happens to match the edit open now is not enough."""

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Doc\n\nFirst body.\n")
    bound = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nSaved on the first edit.\n",
        created.editing_revision,
        "u-1",
        version_id=str(created.version_id),
    )
    assert bound.envelope.result_status == "succeeded", bound.envelope
    assert bound.data.version_id == created.version_id

    discarded = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "idempotency_key": "d-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert discarded.envelope.result_status == "succeeded", discarded.envelope

    # No edit is open: naming the discarded one says so.
    none_open = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nLost.\n",
        bound.data.editing_revision,
        "u-2",
        version_id=str(created.version_id),
    )
    assert none_open.envelope.result_status == "conflict", none_open.envelope
    assert [detail.field for detail in none_open.envelope.error_details] == [
        "version_id"
    ]
    assert "no open edit" in none_open.envelope.error_details[0].message

    begun = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "idempotency_key": "b-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert begun.envelope.result_status == "succeeded", begun.envelope
    second = begun.data

    # The right revision for the open edit, but the old edit's id: refused.
    wrong_row = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nMeant for the discarded edit.\n",
        second.editing_revision,
        "u-3",
        version_id=str(created.version_id),
    )
    assert wrong_row.envelope.result_status == "conflict", wrong_row.envelope
    assert _open_edit_details(wrong_row.envelope) == (
        str(second.version_id),
        second.editing_revision,
    )
    assert _stored(page_service, second.version_id).body_md == second.body_md

    right_row = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nSaved on the second edit.\n",
        second.editing_revision,
        "u-4",
        version_id=str(second.version_id),
    )
    assert right_row.envelope.result_status == "succeeded", right_row.envelope
    assert right_row.data.version_id == second.version_id
    assert right_row.data.editing_revision == second.editing_revision + 1


def test_a_revision_seen_before_a_submit_does_not_reach_the_next_edit(
    page_service, actor
):
    """A submitted edit's revisions stay behind it too: the edit begun after
    it starts above them, so a save carrying one of them is a conflict."""

    workspace_id = _workspace(page_service)
    created = _create(page_service, actor, workspace_id, "# Doc\n\nFirst body.\n")
    saved = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nSubmitted text.\n",
        created.editing_revision,
        "u-1",
    )
    submitted = dispatch(
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "version_id": str(created.version_id),
            "expected_editing_revision": saved.data.editing_revision,
            "idempotency_key": "s-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert submitted.envelope.result_status == "succeeded", submitted.envelope
    begun = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(created.id),
            "idempotency_key": "b-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert begun.envelope.result_status == "succeeded", begun.envelope
    assert begun.data.version_id != created.version_id
    assert begun.data.editing_revision == saved.data.editing_revision + 1

    stale = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Doc\n\nA save meant for the submitted edit.\n",
        saved.data.editing_revision,
        "u-2",
    )
    assert stale.envelope.result_status == "conflict", stale.envelope
    assert _stored(page_service, begun.data.version_id).body_md == begun.data.body_md


def test_a_hard_break_only_save_is_stored(page_service, actor):
    """M-7. The content hash ignores trailing spaces, and the no-op check
    compared hashes: adding or removing a hard break was answered as
    succeeded and never stored -- the browser's autosave lost it the same
    way."""

    workspace_id = _workspace(page_service)
    created = _create(
        page_service, actor, workspace_id, "# Poem\n\nline one\nline two\n"
    )
    added = _save(
        page_service,
        actor,
        workspace_id,
        created.id,
        "# Poem\n\nline one  \nline two\n",
        created.editing_revision,
        "u-1",
    )
    assert added.envelope.result_status == "succeeded", added.envelope
    assert added.data.editing_revision == created.editing_revision + 1
    receipt = added.data.edit_receipt
    assert receipt.chars_added - receipt.chars_removed == 2
    stored = _stored(page_service, created.version_id)
    assert stored.body_md == "# Poem\n\nline one  \nline two\n"
    assert stored.editing_revision == added.data.editing_revision

    removed = page_service.autosave_page(
        page_id=created.id,
        workspace_id=workspace_id,
        title=None,
        body_md="# Poem\n\nline one\nline two\n",
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        base_version_id=created.version_id,
        base_content_hash=stored.content_hash,
        actor_provenance="browser_session",
        idempotency_key="browser-save",
        expected_editing_revision=stored.editing_revision,
    )
    assert removed.editing_revision == stored.editing_revision + 1
    assert _stored(page_service, created.version_id).body_md == (
        "# Poem\n\nline one\nline two\n"
    )


def test_discarding_an_edit_never_brings_back_a_rejected_draft(page_service, actor):
    """M-7. Rejection moves the page's pointers off the rejected draft, but
    the row keeps its "draft" status. Discarding a later edit used to pick it
    up as the page's live draft, so the refused text came back as the page."""

    from uuid import UUID

    from backend.models.context import ContextPage
    from backend.schemas.context_write import ReviewDecisionRequest, WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService
    from backend.services.review_service import ReviewService

    owner = str(actor.user_id)
    published = page_service.write_page(
        workspace_slug="default",
        slug="discard-after-reject",
        title="Discard After Reject",
        body_md="# Discard After Reject\n\nPublished text.",
        actor_kind="human",
        actor_id=owner,
        actor_provenance="browser_session",
    )
    workspace_id = _workspace(page_service)
    with page_service.session_factory.begin() as session:
        proposal = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title=published.title,
                page_type="overview",
                body_markdown=f"{published.body_md}\n\nREJECTED PROPOSAL.",
                reason="proposal",
            ),
            actor_kind="mcp_client",
            actor_id=owner,
            actor_role="editor",
        )
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=proposal.review_item_id,
            request=ReviewDecisionRequest(decision_type="reject", decision_reason="no"),
            actor_kind="human",
            actor_id=owner,
            actor_user_id=UUID(owner),
            actor_role="owner",
            actor_provenance="browser_session",
        )

    begun = dispatch(
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(published.id),
            "idempotency_key": "b-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert begun.envelope.result_status == "succeeded", begun.envelope
    saved = _save(
        page_service,
        actor,
        workspace_id,
        published.id,
        "# Discard After Reject\n\nMy private edit.",
        begun.data.editing_revision,
        "u-1",
    )
    assert saved.envelope.result_status == "succeeded", saved.envelope
    discarded = dispatch(
        "discard_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(published.id),
            "version_id": str(begun.data.version_id),
            "idempotency_key": "d-1",
        },
        actor=actor,
        page_service=page_service,
    )
    assert discarded.envelope.result_status == "succeeded", discarded.envelope

    with page_service.session_factory() as session:
        page = session.get(ContextPage, published.id)
        assert page.latest_draft_version_id is None
        assert page.current_version_id == published.version_id
        rejected = session.get(ContextPageVersion, proposal.draft_version_id)
        assert rejected.version_status == "draft"


def test_a_stale_block_id_after_reordered_groups_is_not_found(page_service, actor):
    """B-6. Each group carries the same label. After two groups swap, the
    label ids could no longer follow their groups; one was handed to another
    group's label, and a replace_block with the id read before the swap
    edited the wrong section. It must fail as block_not_found instead."""

    label = "**Acceptance criteria:**"
    workspace_id = _workspace(page_service)
    created = _create(
        page_service,
        actor,
        workspace_id,
        f"# Spec\n\n{label}\n\n- login works for alpha\n\n{label}\n\n"
        f"- export works for beta\n\n{label}\n\n- sync works for gamma\n",
    )
    before = _structure_map(page_service, actor, workspace_id, created.id).blocks
    gamma_label = before[5]
    assert gamma_label.preview == label
    assert before[6].preview == "- sync works for gamma"

    swapped = _patch(
        page_service,
        actor,
        workspace_id,
        created,
        [
            {
                "op": "replace_text",
                "old_text": (
                    f"- login works for alpha\n\n{label}\n\n- export works for beta"
                ),
                "new_text": (
                    f"- export works for beta\n\n{label}\n\n- login works for alpha"
                ),
            }
        ],
        "p-1",
    )
    assert swapped.envelope.result_status == "succeeded", swapped.envelope

    stale = _patch(
        page_service,
        actor,
        workspace_id,
        swapped.data,
        [
            {
                "op": "replace_block",
                "block": gamma_label.id,
                "expected": gamma_label.hash,
                "content": "**Gamma acceptance criteria:**",
            }
        ],
        "p-2",
    )
    assert stale.envelope.result_status == "failed", stale.envelope
    assert stale.envelope.error_code == "block_not_found"
    assert _stored(page_service, created.version_id).body_md == swapped.data.body_md
