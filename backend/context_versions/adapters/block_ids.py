"""Reuse the shared Markdown identity implementation behind a small port."""

from collections.abc import Sequence
from copy import deepcopy

from backend.context_versions.domain import MergeSnapshot
from backend.utils.block_identity import (
    block_map_is_stale,
    compute_merged_block_map_with_ids,
)


class MarkdownBlockIds:
    def assign(
        self,
        body_md: str,
        *,
        published: MergeSnapshot,
        draft: MergeSnapshot,
        identity_seed: str,
        conflict_ranges: Sequence[tuple[int, int]] = (),
    ) -> list[dict]:
        return compute_merged_block_map_with_ids(
            body_md,
            published_body_md=published.body_md,
            published_block_map=published.block_map,
            draft_body_md=draft.body_md,
            draft_block_map=draft.block_map,
            identity_seed=identity_seed,
            conflict_ranges=conflict_ranges,
        )

    def for_snapshot(
        self, snapshot: MergeSnapshot, *, identity_seed: str
    ) -> list[dict]:
        if not block_map_is_stale(snapshot.block_map, snapshot.body_md):
            return deepcopy(snapshot.block_map)
        return self.assign(
            snapshot.body_md,
            published=snapshot,
            draft=snapshot,
            identity_seed=identity_seed,
        )
