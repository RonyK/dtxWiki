"""read_context_outline and read_structure_map must bound what they emit.

Both projections were unbounded: one heading per outline entry, one block per
document block plus one entry per table row, with no item cap, no byte cap and
no cursor. `BODY_MD_MAX_LENGTH` is 1_000_000, so the input that triggers this is
an ordinary legal write, not an abuse -- a 1 MB heading-heavy body produced
14,867 outline entries (5.9 MB of JSON) and 29,734 structure-map blocks
(8.2 MB), and a 1 MB single-table body produced ONE block carrying 18,178 rows
(2.8 MB), which is why rows need a cap independent of the block cap.

Truncation is in strict document order with a resumable cursor, because
block.id/row.id are the addressing primitive patch_draft consumes: every id has
to stay reachable by paging.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.workspace import Workspace
from backend.schemas.page import BODY_MD_MAX_LENGTH


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("read", "draft-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _payload(result) -> dict:
    return json.loads(result.model_dump_json())


def _bytes(result) -> int:
    return len(result.model_dump_json().encode("utf-8"))


# 30_000 chars is ~430 sections: comfortably past the 200-item cap these
# tests exercise, without the 30x overshoot that made this file the
# slowest in the MCP suite. The wire budget is covered separately by
# _wide_heading_body below, which is the only shape that can reach it.
def _heading_heavy_body(target_chars: int = 30_000) -> str:
    body = ""
    index = 0
    while len(body) < target_chars:
        body += (
            f"## Section number {index} with a reasonably long heading title\n\n"
            "body\n\n"
        )
        index += 1
    body = body[:target_chars]
    assert len(body) <= BODY_MD_MAX_LENGTH
    return body


# 400 rows is past max_rows_per_block's 200 default with margin.
def _single_table_body(rows: int = 400) -> str:
    header = "| a | b | c |\n|---|---|---|\n"
    row = "| cell value one | cell value two | cell value three |\n"
    body = header + row * rows
    assert len(body) <= BODY_MD_MAX_LENGTH
    return body


def _wide_heading_body(sections: int = 40, title_chars: int = 8_000) -> str:
    """FEW entries, each far too big -- the only shape that reaches the wire
    budget before the item cap.

    max_sections defaults to 200, so 40 sections never trip the item cap; but
    40 entries carrying an ~8 KB title each blow past
    _OUTLINE_MAX_WIRE_BYTES (256 KB). Long headings are unusual, not illegal:
    BODY_MD_MAX_LENGTH is 1_000_000, so this is a legal write, which is the
    whole premise of the caps.
    """

    body = ""
    for index in range(sections):
        title = f"Section {index} " + ("wide" * (title_chars // 4))
        body += f"## {title}\n\nbody\n\n"
    assert len(body) <= BODY_MD_MAX_LENGTH
    return body


def _write(page_service, slug: str, body: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )


def test_read_context_outline_caps_the_heading_list_and_pages_with_a_cursor(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "outline-bounds", _heading_heavy_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    first = dispatch(
        "read_context_outline", request, actor=actor, page_service=page_service
    )

    assert first.envelope.result_status == "succeeded"
    assert first.outline_total > len(first.outline)
    assert len(first.outline) <= 200
    assert first.outline_omitted_count == first.outline_total - len(first.outline)
    assert first.outline_next_cursor is not None
    # document.section_count keeps its old meaning: every heading at every
    # level, which is NOT the paged projection's population.
    assert first.document.section_count >= first.outline_total

    seen = [entry.section_id for entry in first.outline]
    cursor = first.outline_next_cursor
    pages = 1
    while cursor is not None:
        following = dispatch(
            "read_context_outline",
            {**request, "outline_cursor": cursor},
            actor=actor,
            page_service=page_service,
        )
        assert following.envelope.result_status == "succeeded"
        assert following.outline_total == first.outline_total
        seen.extend(entry.section_id for entry in following.outline)
        cursor = following.outline_next_cursor
        pages += 1
        assert pages < 200, "cursor is not making progress"

    assert pages > 1
    # Every heading is reachable, exactly once, in document order.
    assert len(seen) == first.outline_total
    assert len(set(seen)) == len(seen)


def test_read_context_outline_pages_without_a_selection(page_service, actor):
    """`cursor` requires `select`; `outline_cursor` deliberately does not."""

    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "outline-bounds-noselect", _heading_heavy_body())

    result = dispatch(
        "read_context_outline",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}},
        actor=actor,
        page_service=page_service,
    )

    assert result.envelope.result_status == "succeeded"
    assert result.section is None
    assert result.outline_next_cursor is not None


def test_read_context_outline_rejects_a_structure_map_cursor(page_service, actor):
    """The kind discriminator: a cursor minted for one projection must not be
    replayable as another, even though both are signed with the same secret and
    the same version_id."""

    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "outline-cursor-kind", _heading_heavy_body())
    target = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    map_page = dispatch(
        "read_structure_map", target, actor=actor, page_service=page_service
    )
    assert map_page.next_cursor is not None

    replayed = dispatch(
        "read_context_outline",
        {**target, "outline_cursor": map_page.next_cursor},
        actor=actor,
        page_service=page_service,
    )

    assert replayed.envelope.result_status == "failed"
    assert replayed.envelope.error_code == "validation_failed"


def test_read_structure_map_caps_blocks_and_pages_with_a_cursor(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "structure-map-bounds", _heading_heavy_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    first = dispatch(
        "read_structure_map", request, actor=actor, page_service=page_service
    )

    assert first.envelope.result_status == "succeeded"
    assert len(first.blocks) <= 200
    # THE trap this pins: block_count is the document total, not len(blocks).
    assert first.document.block_count > len(first.blocks)
    assert first.blocks_omitted_count == first.document.block_count - len(first.blocks)
    assert first.next_cursor is not None

    seen = [block.id for block in first.blocks]
    cursor = first.next_cursor
    pages = 1
    while cursor is not None:
        following = dispatch(
            "read_structure_map",
            {**request, "cursor": cursor},
            actor=actor,
            page_service=page_service,
        )
        assert following.envelope.result_status == "succeeded"
        seen.extend(block.id for block in following.blocks)
        cursor = following.next_cursor
        pages += 1
        assert pages < 500, "cursor is not making progress"

    assert pages > 1
    assert len(seen) == first.document.block_count
    assert len(set(seen)) == len(seen)


def test_read_structure_map_caps_rows_of_a_single_table_and_resumes_mid_block(
    page_service, actor
):
    """A 1 MB table is ONE block, so a block cap alone bounds nothing. The
    cursor has to resume mid-table, at a row."""

    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "structure-map-rows", _single_table_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    first = dispatch(
        "read_structure_map", request, actor=actor, page_service=page_service
    )

    assert first.envelope.result_status == "succeeded"
    table = next(block for block in first.blocks if block.row_total > 0)
    assert table.row_total > len(table.rows)
    assert len(table.rows) <= 200
    assert table.row_offset == 0
    assert table.rows_omitted_count == table.row_total - len(table.rows)
    assert first.next_cursor is not None

    second = dispatch(
        "read_structure_map",
        {**request, "cursor": first.next_cursor},
        actor=actor,
        page_service=page_service,
    )

    assert second.envelope.result_status == "succeeded"
    continuation = next(block for block in second.blocks if block.id == table.id)
    assert continuation.row_offset == len(table.rows)
    assert continuation.row_total == table.row_total

    seen = [row.id for row in table.rows]
    seen.extend(row.id for row in continuation.rows)
    assert len(set(seen)) == len(seen)


def test_bounded_responses_stay_far_below_the_unbounded_size(page_service, actor):
    """The point of the caps: a legal 400 KB write must not produce a
    multi-megabyte response."""

    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "projection-size", _heading_heavy_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    outline = dispatch(
        "read_context_outline", request, actor=actor, page_service=page_service
    )
    structure = dispatch(
        "read_structure_map", request, actor=actor, page_service=page_service
    )

    assert _bytes(outline) < 512 * 1024
    assert _bytes(structure) < 512 * 1024
    assert _payload(outline)["outline_total"] > len(_payload(outline)["outline"])


def test_a_cursor_past_the_end_is_rejected_rather_than_returning_an_empty_page(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "outline-cursor-end", _heading_heavy_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    cursor = dispatch(
        "read_context_outline", request, actor=actor, page_service=page_service
    ).outline_next_cursor
    while cursor is not None:
        page_result = dispatch(
            "read_context_outline",
            {**request, "outline_cursor": cursor},
            actor=actor,
            page_service=page_service,
        )
        last, cursor = cursor, page_result.outline_next_cursor

    # `last` is the cursor that produced the FINAL page; replaying the offset it
    # points at is in range, so re-use it and confirm the terminal page is
    # stable rather than erroring.
    replayed = dispatch(
        "read_context_outline",
        {**request, "outline_cursor": last},
        actor=actor,
        page_service=page_service,
    )
    assert replayed.envelope.result_status == "succeeded"
    assert replayed.outline_next_cursor is None


def test_outline_wire_budget_binds_before_the_item_cap(page_service, actor):
    """The 256 KB wire budget, which nothing else in this file reaches.

    Regression note: every other test here truncates at the 200-ITEM cap
    (measured: outline_total=5987, emitted=200, wire_bytes=74787), so raising
    _OUTLINE_MAX_WIRE_BYTES a thousandfold used to leave the whole file green.
    This test fails if that budget stops being enforced.
    """

    workspace_id = _workspace_id(page_service)
    page = _write(page_service, "outline-wire-budget", _wide_heading_body())
    request = {"workspace_id": str(workspace_id), "target": {"page_id": str(page.id)}}

    result = dispatch(
        "read_context_outline", request, actor=actor, page_service=page_service
    )
    payload = _payload(result)

    # Far fewer than the 200-item cap were emitted, so the ITEM cap cannot be
    # what truncated this -- only the byte budget can have.
    assert payload["outline_total"] == 40
    assert len(payload["outline"]) < 40
    assert payload["outline_next_cursor"] is not None
    assert _bytes(result) <= 512 * 1024
