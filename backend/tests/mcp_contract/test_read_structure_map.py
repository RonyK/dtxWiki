import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.workspace import Workspace


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("read", "draft-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def test_read_structure_map_covers_every_block_kind_with_ids_and_hashes(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = (
        "# Title\n\nIntro paragraph.\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    )
    page = page_service.write_page(
        workspace_slug="default",
        slug="structure-map-basic",
        title="Structure Map Basic",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    result = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
        },
        actor=actor,
        page_service=page_service,
    )

    assert result.envelope.result_status == "succeeded"
    assert result.content_trust == "untrusted_user_content"
    assert result.document.page_id == page.id
    assert result.document.version_id == page.version_id
    assert result.document.root_hash == page.content_hash
    assert result.document.block_count == len(result.blocks) == 3

    kinds = [block.kind for block in result.blocks]
    assert kinds == ["h1", "p", "table"]
    assert len({block.id for block in result.blocks}) == 3
    assert all(block.id.startswith("b_") for block in result.blocks)

    table = result.blocks[2]
    assert len(table.rows) == 2
    assert table.rows[0].id != table.rows[1].id
    assert all(row.id.startswith("r_") for row in table.rows)

    heading = result.blocks[0]
    assert heading.preview == "# Title"
    assert heading.line_span == (1, 2)


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_read_structure_map_preview_uses_the_shared_raw_line_table(
    page_service, actor, newline
):
    workspace_id = _workspace_id(page_service)
    body = newline.join(("# First", "", "second paragraph", "", "## Third", ""))
    page = page_service.write_page(
        workspace_slug="default",
        slug=f"structure-map-newline-{len(newline)}",
        title="Structure Map Newlines",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    result = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
        },
        actor=actor,
        page_service=page_service,
    )

    assert result.envelope.result_status == "succeeded"
    assert [(block.line_span, block.preview) for block in result.blocks] == [
        ((1, 2), "# First"),
        ((3, 4), "second paragraph"),
        ((5, 6), "## Third"),
    ]


def test_read_structure_map_carries_ids_forward_across_an_unrelated_edit(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = "# A\n\nfirst\n\n# B\n\nsecond\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="structure-map-carry-forward",
        title="Structure Map Carry Forward",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    before = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )

    edited_body = "# A\n\nfirst\n\n# B\n\nsecond, now edited with much more text\n"
    page_service.write_page(
        workspace_slug="default",
        slug="structure-map-carry-forward",
        title="Structure Map Carry Forward",
        body_md=edited_body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    after = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )

    before_ids = {block.kind: block.id for block in before.blocks}
    after_ids = {block.kind: block.id for block in after.blocks}
    # Both "# A" and "# B" headings are untouched by the edit -- ids survive.
    before_headings = [b.id for b in before.blocks if b.kind == "h1"]
    after_headings = [b.id for b in after.blocks if b.kind == "h1"]
    assert before_headings == after_headings
    assert before_ids["p"] != after_ids.get("p")  # the edited paragraph is gone
    # "first" (unedited) keeps its id; only "second" changed.
    before_first = next(b for b in before.blocks if b.preview == "first")
    after_first = next(b for b in after.blocks if b.preview == "first")
    assert before_first.id == after_first.id


def test_read_structure_map_returns_not_found_for_a_missing_page(page_service, actor):
    workspace_id = _workspace_id(page_service)

    result = dispatch(
        "read_structure_map",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": "00000000-0000-0000-0000-000000000099"},
        },
        actor=actor,
        page_service=page_service,
    )

    assert result.envelope.result_status == "failed"
    assert result.envelope.error_code == "not_found"
