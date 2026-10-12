from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import func, select

from backend.mcp.router import dispatch
from backend.models.base import utc_now
from backend.models.change import ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_edit import PatchDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.utils.block_map import compute_blocks, serialize_blocks

# P8.6: patch_draft is no longer a callable MCP tool, but the durable
# idempotency ledger it writes to (MCPWriteIdempotency, keyed by tool_name
# "patch_draft") is written by ContextWriteService.patch_draft itself,
# regardless of caller -- so this matrix calls the engine directly. See
# test_patch_draft_push_semantics.py for the same pattern.


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("read", "draft-write"))


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def _structure_map(page_service, actor, workspace_id, page_id):
    result = dispatch(
        "read_structure_map",
        {"workspace_id": str(workspace_id), "target": {"page_id": str(page_id)}},
        actor=actor,
        page_service=page_service,
    )
    assert result.document is not None
    return result


def _patch_draft(page_service, actor, workspace_id, **payload):
    """Engine-level equivalent of ``dispatch("patch_draft", {...})``."""
    with page_service.session_factory.begin() as session:
        return ContextWriteService().patch_draft(
            session=session,
            workspace_id=workspace_id,
            request=PatchDraftRequest(**payload),
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )


def _version_count(page_service, page_id) -> int:
    with page_service.session_factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == page_id)
        )


def _derived_map(block_map: list[dict]) -> list[dict]:
    return [
        {
            key: ([{"hash": row["hash"]} for row in value] if key == "rows" else value)
            for key, value in entry.items()
            if key != "id"
        }
        for entry in block_map
    ]


def _assert_page_graph_is_consistent(page_service, page_id) -> ContextPageVersion:
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        assert page.latest_draft_version_id is not None
        head = session.get(ContextPageVersion, page.latest_draft_version_id)
        assert head is not None
        versions = session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.page_id == page_id)
        ).all()
        assert head in versions
        for version in versions:
            assert version.block_map is not None
            assert _derived_map(version.block_map) == serialize_blocks(
                compute_blocks(version.body_md)
            )
            ids = [entry["id"] for entry in version.block_map]
            ids.extend(
                row["id"]
                for entry in version.block_map
                for row in entry.get("rows", [])
            )
            assert len(ids) == len(set(ids))
        return head


def test_expired_patch_draft_ledger_entry_reenters_the_current_dag_without_replay(
    page_service,
    actor,
    monkeypatch,
):
    # The first receipt expires one second before this pinned "now", and the
    # ledger judges both patches against that same "now": the host clock steps
    # (NTP, a resumed VM), so backdating against one read of it and deciding
    # against another would put the receipt on either side of its own deadline.
    now = utc_now()
    monkeypatch.setattr("backend.services.mcp_write_idempotency.utc_now", lambda: now)
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug="expired-patch-ledger-dag",
        title="Expired Patch Ledger DAG",
        body_md="# Title\n\noriginal paragraph\n",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure = _structure_map(page_service, actor, workspace_id, page.id)
    paragraph = next(block for block in structure.blocks if block.kind == "p")
    payload = {
        "page_id": page.id,
        "base": str(structure.document.version_id),
        "reason": "exercise the expired durable patch ledger",
        "idempotency_key": "expired-patch-ledger-dag-key",
        "operations": [
            {
                "op": "replace_block",
                "block": paragraph.id,
                "expected": paragraph.hash,
                "content": "edited exactly once",
            }
        ],
    }

    first = _patch_draft(page_service, actor, workspace_id, **payload)
    assert first.push_outcome == "committed"
    versions_after_first = _version_count(page_service, page.id)

    with page_service.session_factory.begin() as session:
        row = session.scalar(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.workspace_id == workspace_id,
                MCPWriteIdempotency.actor_id == actor.actor_id,
                MCPWriteIdempotency.tool_name == "patch_draft",
                MCPWriteIdempotency.idempotency_key == payload["idempotency_key"],
            )
        )
        assert row is not None
        expired_row_id = row.id
        row.expires_at = now - timedelta(seconds=1)

    second = _patch_draft(page_service, actor, workspace_id, **payload)
    assert second.push_outcome == "diverged"
    assert second.operation_receipts[0].outcome == "applied"
    assert second.alternative_draft_id == second.draft_version_id

    with page_service.session_factory() as session:
        rows = session.scalars(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.workspace_id == workspace_id,
                MCPWriteIdempotency.actor_id == actor.actor_id,
                MCPWriteIdempotency.tool_name == "patch_draft",
                MCPWriteIdempotency.idempotency_key == payload["idempotency_key"],
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].id != expired_row_id
        assert rows[0].result_payload["push_outcome"] != "replayed"

    head = _assert_page_graph_is_consistent(page_service, page.id)
    assert head.id == first.draft_version_id
    assert head.body_md.count("edited exactly once") == 1
    assert "original paragraph" not in head.body_md
    assert _version_count(page_service, page.id) == versions_after_first + 1


@dataclass(frozen=True)
class _BlockCase:
    name: str
    body: str
    target_kind: str
    operation: Literal["replace_block", "insert_after", "insert_row", "delete_block"]
    content: str | None
    effect_marker: str
    distinct_key_outcome: Literal["rebased", "diverged"]


_BLOCK_CASES = (
    _BlockCase(
        "paragraph-replace",
        "# Title\n\nparagraph target\n",
        "p",
        "replace_block",
        "paragraph replacement marker",
        "paragraph replacement marker",
        "diverged",
    ),
    _BlockCase(
        "list-replace",
        "# Title\n\n- first item\n- second item\n",
        "list",
        "replace_block",
        "- first item changed\n- second item",
        "first item changed",
        "diverged",
    ),
    _BlockCase(
        "heading-replace",
        "# Title\n\n## Heading target\n\nbody under heading\n",
        "h2",
        "replace_block",
        "## Heading replacement marker",
        "Heading replacement marker",
        "diverged",
    ),
    _BlockCase(
        "table-insert-row",
        "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        "table",
        "insert_row",
        "| 3 | table-row-marker |",
        "table-row-marker",
        "rebased",
    ),
    _BlockCase(
        "fence-replace",
        "# Title\n\n```python\nprint('old')\n```\n",
        "code",
        "replace_block",
        "```python\nprint('fence replacement marker')\n```",
        "fence replacement marker",
        "diverged",
    ),
    _BlockCase(
        "blockquote-delete",
        "# Title\n\n> blockquote deletion marker\n\nafter quote\n",
        "blockquote",
        "delete_block",
        None,
        "blockquote deletion marker",
        "diverged",
    ),
    _BlockCase(
        "raw-html-insert-after",
        "# Title\n\n<aside>raw html target</aside>\n",
        "html",
        "insert_after",
        "<section>raw html insertion marker</section>",
        "raw html insertion marker",
        "rebased",
    ),
)


def _operation_for(case: _BlockCase, target) -> dict:
    if case.operation == "insert_row":
        row = target.rows[0]
        return {
            "op": "insert_row",
            "row": row.id,
            "expected": row.hash,
            "content": case.content,
        }
    operation = {
        "op": case.operation,
        "block": target.id,
        "expected": target.hash,
    }
    if case.content is not None:
        operation["content"] = case.content
    return operation


@pytest.mark.parametrize("case", _BLOCK_CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("key_state", ("none", "same", "different"))
def test_patch_draft_ledger_owned_state_transition_matrix(
    page_service,
    actor,
    case: _BlockCase,
    key_state: Literal["none", "same", "different"],
):
    workspace_id = _workspace_id(page_service)
    page = page_service.write_page(
        workspace_slug="default",
        slug=f"ledger-matrix-{key_state}-{case.name}",
        title=f"Ledger Matrix {key_state} {case.name}",
        body_md=case.body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    structure = _structure_map(page_service, actor, workspace_id, page.id)
    target = next(block for block in structure.blocks if block.kind == case.target_kind)
    operation = _operation_for(case, target)
    payload = {
        "page_id": page.id,
        "base": str(structure.document.version_id),
        "reason": f"ledger matrix {case.name}",
        "operations": [operation],
    }
    if key_state != "none":
        payload["idempotency_key"] = f"ledger-matrix-{case.name}-first"

    first = _patch_draft(page_service, actor, workspace_id, **payload)
    assert first.push_outcome == "committed"
    assert first.operation_receipts[0].outcome == "applied"
    assert first.change_set_id is not None
    assert first.review_item_id is not None
    versions_after_first = _version_count(page_service, page.id)
    first_head = _assert_page_graph_is_consistent(page_service, page.id)

    retry_payload = dict(payload)
    if key_state == "different":
        retry_payload["idempotency_key"] = f"ledger-matrix-{case.name}-second"
    second = _patch_draft(page_service, actor, workspace_id, **retry_payload)

    final_head = _assert_page_graph_is_consistent(page_service, page.id)
    if key_state in {"none", "same"}:
        assert second.push_outcome == "replayed"
        assert second.draft_version_id == first.draft_version_id
        assert second.operation_receipts == first.operation_receipts
        assert _version_count(page_service, page.id) == versions_after_first
        assert final_head.id == first_head.id
        assert final_head.body_md == first_head.body_md
    else:
        assert second.push_outcome == case.distinct_key_outcome
        assert second.operation_receipts[0].outcome == "applied"
        assert _version_count(page_service, page.id) == versions_after_first + 1
        if case.distinct_key_outcome == "diverged":
            assert second.alternative_draft_id == second.draft_version_id
            assert final_head.id == first_head.id
        else:
            assert second.alternative_draft_id is None
            assert final_head.id == second.draft_version_id

    marker_count = final_head.body_md.count(case.effect_marker)
    if case.operation == "delete_block":
        assert marker_count == 0
    elif key_state == "different" and case.operation in {
        "insert_after",
        "insert_row",
    }:
        assert marker_count == 2
    else:
        assert marker_count == 1

    with page_service.session_factory() as session:
        assert session.get(ChangeSet, first.change_set_id) is not None
        assert session.get(ReviewItem, first.review_item_id) is not None
        ledger_rows = session.scalars(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.workspace_id == workspace_id,
                MCPWriteIdempotency.actor_id == actor.actor_id,
                MCPWriteIdempotency.tool_name == "patch_draft",
            )
        ).all()
        assert len(ledger_rows) == (2 if key_state == "different" else 1)
