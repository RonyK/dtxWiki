"""Two observable merge failures: adjacent conflict structure and DAG identity."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from backend.context_versions.adapters.block_ids import MarkdownBlockIds
from backend.context_versions.adapters.git_merge import Libgit2TextMerge
from backend.context_versions.application import ContextMergeService
from backend.context_versions.domain import MergeSnapshot, PageMetadata, VersionNode
from backend.context_versions.history import HistoryGraph
from backend.utils.block_identity import compute_block_map_with_ids
from backend.utils.block_map import compute_blocks


def test_adjacent_conflicts_keep_one_table_and_preserve_uncontested_edits():
    base = "# 제목\n\n첫 문단.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n끝 문단.\n"
    ours = base.replace("첫 문단.", "첫 문단을 내가 고침.").replace(
        "| 1 | 2 |", "| 1 | 20 |"
    )
    theirs = (
        base.replace("첫 문단.", "첫 문단을 상대가 고침.")
        .replace("| 1 | 2 |", "| 1 | 2 |\n| 3 | 4 |")
        .replace("끝 문단.", "끝 문단. 상대 추가.")
    )
    result = Libgit2TextMerge().merge(base, ours, theirs, prefer_ours=True)
    assert result.clean is None
    assert result.keep_both == (
        "# 제목\n\n첫 문단을 내가 고침.\n\n첫 문단을 상대가 고침.\n\n"
        "| a | b |\n|---|---|\n| 1 | 20 |\n| 1 | 2 |\n| 3 | 4 |\n\n"
        "끝 문단. 상대 추가.\n"
    )
    assert result.latest == ours.replace("끝 문단.", "끝 문단. 상대 추가.")
    table = next(
        block for block in compute_blocks(result.keep_both) if block.kind == "table"
    )
    assert len(table.rows) == 3
    conflict = result.conflicts[0]
    assert result.keep_both[conflict.keep_both_start : conflict.keep_both_end] == (
        "첫 문단을 내가 고침.\n\n첫 문단을 상대가 고침.\n\n"
        "| a | b |\n|---|---|\n| 1 | 20 |\n| 1 | 2 |\n| 3 | 4 |\n"
    )
    assert result.latest[conflict.latest_start : conflict.latest_end] == conflict.ours


def test_recursive_bases_bind_metadata_and_two_source_block_identity():
    workspace, page = UUID(int=100), UUID(int=101)
    instant = datetime(2026, 11, 14, tzinfo=UTC)
    original = (
        "# Title\n\nShared paragraph.\n\n"
        "| key | value |\n| --- | --- |\n| stable | same |\n\nTail.\n"
    )
    root = MergeSnapshot(
        workspace,
        page,
        UUID(int=1),
        original,
        PageMetadata("Root", "concept", ("keep", "delete"), ("old",)),
        instant,
        compute_block_map_with_ids(
            original,
            previous_body_md=None,
            previous_block_map=None,
            identity_seed="root",
        ),
    )

    def snapshot(number, body, metadata, parent, saved):
        return MergeSnapshot(
            workspace,
            page,
            UUID(int=number),
            body,
            metadata,
            saved,
            compute_block_map_with_ids(
                body,
                previous_body_md=parent.body_md,
                previous_block_map=parent.block_map,
                identity_seed=str(number),
            ),
        )

    left = snapshot(
        2, original, PageMetadata("Left", "concept", ("keep", "left")), root, instant
    )
    right = snapshot(
        3,
        original,
        PageMetadata("Right", "concept", ("keep", "delete", "right")),
        root,
        instant,
    )
    ours = snapshot(
        4,
        original.replace("Shared paragraph.", "Shared paragraph from ours."),
        PageMetadata(
            "Mine", "guide", ("keep", "left", "mine"), ("mine",), UUID(int=201)
        ),
        left,
        instant + timedelta(hours=1),
    )
    theirs = snapshot(
        5,
        original.replace("Shared paragraph.", "Shared paragraph from theirs.").replace(
            "| stable | same |", "| stable | same |\n| added | theirs |"
        )
        + "\nOnly in the draft.\n",
        PageMetadata(
            "Other", "source", ("keep", "right", "other"), ("other",), UUID(int=202)
        ),
        right,
        instant + timedelta(hours=2),
    )
    history = HistoryGraph(
        (
            VersionNode(workspace, page, root.version_id),
            VersionNode(workspace, page, left.version_id, (root.version_id,)),
            VersionNode(workspace, page, right.version_id, (root.version_id,)),
            VersionNode(
                workspace, page, ours.version_id, (left.version_id, right.version_id)
            ),
            VersionNode(
                workspace, page, theirs.version_id, (right.version_id, left.version_id)
            ),
        )
    )
    assert history.is_ancestor(root.version_id, ours.version_id)
    assert not history.is_ancestor(ours.version_id, theirs.version_id)
    assert history.merge_bases(ours.version_id, theirs.version_id) == (
        left.version_id,
        right.version_id,
    )
    snapshots = {item.version_id: item for item in (root, left, right, ours, theirs)}
    service = ContextMergeService(Libgit2TextMerge(), MarkdownBlockIds())
    result = service.merge_from_history(
        history,
        snapshots,
        ours.version_id,
        theirs.version_id,
        published_side="ours",
        identity_seed="review-request",
    )
    assert result.requires_choice and result.base_conflicted
    assert result.keep_both.metadata == PageMetadata(
        "Other", "source", ("keep", "mine", "other"), ("mine", "other"), UUID(int=202)
    )
    assert result.latest.metadata == result.keep_both.metadata
    assert "Shared paragraph from theirs." in result.latest.body_md
    assert "Shared paragraph from ours." not in result.latest.body_md
    assert "Shared paragraph from ours." in result.keep_both.body_md
    assert "Shared paragraph from theirs." in result.keep_both.body_md
    assert result.keep_both.block_map[1]["id"] == ours.block_map[1]["id"]
    assert result.keep_both.block_map[2]["id"] != ours.block_map[1]["id"]
    table = next(
        entry for entry in result.keep_both.block_map if entry["kind"] == "table"
    )
    assert table["id"] == ours.block_map[2]["id"]
    assert table["rows"][0]["id"] == ours.block_map[2]["rows"][0]["id"]
    assert table["rows"][1]["id"] == theirs.block_map[2]["rows"][1]["id"]
    assert result.keep_both.block_map[-1]["id"] == theirs.block_map[-1]["id"]
    identities = [entry["id"] for entry in result.keep_both.block_map]
    identities += [
        row["id"]
        for entry in result.keep_both.block_map
        for row in entry.get("rows", ())
    ]
    assert len(set(identities)) == len(identities)
    assert (
        service.merge_from_history(
            history,
            snapshots,
            ours.version_id,
            theirs.version_id,
            published_side="ours",
            identity_seed="review-request",
        )
        == result
    )
    assert result.keep_both.result_hash != result.latest.result_hash
