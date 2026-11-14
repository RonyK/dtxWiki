"""Snapshot preparation and merge calculation; publication stays with callers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.context_versions.domain import (
    ContentPayload,
    ContextMergeError,
    ContextVersionStorageError,
    MergeCandidate,
    MergeOutcome,
    MergeSide,
    MergeSnapshot,
    PageMetadata,
    SnapshotRecord,
    VersionNode,
)
from backend.context_versions.history import HistoryGraph
from backend.context_versions.merge import (
    MetadataOptions,
    merge_metadata,
    merge_metadata_options,
    prefer_ours,
    result_hash,
    saved_at_utc,
)
from backend.context_versions.ports import (
    BlockIdAssigner,
    SnapshotReader,
    SnapshotWriter,
    TextMerger,
)


class ContextVersionWriter:
    def __init__(self, reader: SnapshotReader, writer: SnapshotWriter) -> None:
        self.reader = reader
        self.writer = writer

    def repository_exists(self, workspace_id: UUID) -> bool:
        return self.reader.exists(workspace_id)

    def prepare_snapshot(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        version_id: UUID,
        created_at: datetime,
        payload: ContentPayload,
        parents: Sequence[str],
        existing_oid: str | None = None,
    ) -> SnapshotRecord:
        if existing_oid is None:
            oid = self.writer.commit(
                workspace_id=workspace_id,
                page_id=page_id,
                version_id=version_id,
                created_at=created_at,
                payload=payload,
                parents=parents,
            )
        else:
            oid = existing_oid
            stored = self.reader.read(
                workspace_id=workspace_id,
                page_id=page_id,
                commit_oid=oid,
                descriptor=payload.descriptor,
            )
            if stored != payload.data:
                raise ContextVersionStorageError("context_snapshot_invalid")
            if payload.block_map is not None:
                stored_map = self.reader.read_block_map(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    commit_oid=oid,
                    descriptor=payload.descriptor,
                )
                # Legacy commits remain immutable and keep their SQL map.
                if stored_map is not None and stored_map != payload.block_map:
                    raise ContextVersionStorageError("context_snapshot_invalid")
        return SnapshotRecord(oid, len(payload.data), payload.descriptor)

    def align_head(
        self,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str | None,
    ) -> None:
        self.writer.set_head(workspace_id, page_id, commit_oid)


class ContextMergeService:
    """Calculate reviewable candidates, without publication or persistence."""

    def __init__(self, text_merger: TextMerger, block_ids: BlockIdAssigner) -> None:
        self.text_merger = text_merger
        self.block_ids = block_ids

    def merge(
        self,
        base: MergeSnapshot | None,
        ours: MergeSnapshot,
        theirs: MergeSnapshot,
        *,
        published_side: MergeSide,
        identity_seed: str,
        base_metadata_options: MetadataOptions | None = None,
    ) -> MergeOutcome:
        scope = (ours.workspace_id, ours.page_id)
        if any(
            (value.workspace_id, value.page_id) != scope
            for value in (base, theirs)
            if value is not None
        ):
            raise ContextMergeError("context_merge_invalid_history")
        if published_side not in {"ours", "theirs"}:
            raise ContextMergeError("context_merge_invalid_input")
        mine_is_latest = prefer_ours(ours, theirs)
        text = self.text_merger.merge(
            base.body_md if base else "",
            ours.body_md,
            theirs.body_md,
            prefer_ours=mine_is_latest,
        )
        metadata, metadata_conflicts = merge_metadata(
            base.metadata if base else PageMetadata(),
            ours.metadata,
            theirs.metadata,
            prefer_ours=mine_is_latest,
            base_options=base_metadata_options,
        )
        published, draft = (
            (ours, theirs) if published_side == "ours" else (theirs, ours)
        )

        def candidate(body: str, *, keep_both: bool) -> MergeCandidate:
            block_map = self.block_ids.assign(
                body,
                published=published,
                draft=draft,
                identity_seed=identity_seed,
                conflict_ranges=tuple(
                    (item.keep_both_start, item.keep_both_end)
                    for item in text.conflicts
                )
                if keep_both
                else (),
            )
            return MergeCandidate(
                body,
                metadata,
                block_map,
                result_hash(body, metadata, block_map),
                text.conflicts,
            )

        both = candidate(text.keep_both, keep_both=True)
        latest = candidate(text.latest, keep_both=False)
        clean = both if text.clean is not None and not metadata_conflicts else None
        return MergeOutcome(clean, both, latest, metadata_conflicts)

    def merge_from_history(
        self,
        history: HistoryGraph,
        snapshots: Mapping[UUID, MergeSnapshot],
        ours: UUID,
        theirs: UUID,
        *,
        published_side: MergeSide,
        identity_seed: str,
    ) -> MergeOutcome:
        if published_side not in {"ours", "theirs"}:
            raise ContextMergeError("context_merge_invalid_input")
        return _HistoryMerge(self, history, snapshots, identity_seed).preview(
            ours, theirs, published_side
        )


@dataclass(frozen=True)
class _VirtualBase:
    snapshot: MergeSnapshot
    metadata_options: MetadataOptions = field(default_factory=dict)
    had_conflicts: bool = False


@dataclass
class _BaseFrame:
    key: tuple[UUID, UUID]
    bases: tuple[UUID, ...]
    current: _VirtualBase | None
    next_index: int = 1


class _HistoryMerge:
    """Request-local recursive bases resolved with an explicit dependency stack."""

    def __init__(
        self,
        service: ContextMergeService,
        history: HistoryGraph,
        snapshots: Mapping[UUID, MergeSnapshot],
        identity_seed: str,
    ) -> None:
        self.service = service
        self.history = history
        self.snapshots = dict(snapshots)
        self.identity_seed = identity_seed
        self._bases: dict[tuple[UUID, UUID], _VirtualBase | None] = {}
        self._virtuals: dict[UUID, _VirtualBase] = {}
        self._marker = f"dtx-virtual-base-{uuid5(NAMESPACE_URL, identity_seed)}"
        while any(self._marker in value.body_md for value in self.snapshots.values()):
            self._marker += "_"

    def snapshot(self, version_id: UUID) -> MergeSnapshot:
        node = self.history.node(version_id)
        value = self.snapshots.get(version_id)
        if value is None or (value.version_id, value.workspace_id, value.page_id) != (
            node.version_id,
            node.workspace_id,
            node.page_id,
        ):
            raise ContextMergeError("context_merge_invalid_history")
        return value

    def preview(
        self, ours: UUID, theirs: UUID, published_side: MergeSide
    ) -> MergeOutcome:
        mine, other = self.snapshot(ours), self.snapshot(theirs)
        if self.history.is_ancestor(ours, theirs):
            return self.unchanged(other)
        if self.history.is_ancestor(theirs, ours):
            return self.unchanged(mine)
        base = self.base(ours, theirs)
        outcome = self.service.merge(
            base.snapshot if base else None,
            mine,
            other,
            published_side=published_side,
            identity_seed=self.identity_seed,
            base_metadata_options=base.metadata_options if base else None,
        )
        # Scratch-only ancestor markers cannot be authored by either real tip.
        if any(
            self._marker in candidate.body_md
            for candidate in (outcome.keep_both, outcome.latest)
        ):
            raise ContextMergeError("context_merge_invalid_result")
        base_conflicted = base.had_conflicts if base else False
        return MergeOutcome(
            outcome.clean if not base_conflicted else None,
            outcome.keep_both,
            outcome.latest,
            outcome.metadata_conflicts,
            base_conflicted,
        )

    def unchanged(self, snapshot: MergeSnapshot) -> MergeOutcome:
        """An ancestor relationship selects the descendant's exact identities."""
        block_map = self.service.block_ids.for_snapshot(
            snapshot, identity_seed=self.identity_seed
        )
        candidate = MergeCandidate(
            snapshot.body_md,
            snapshot.metadata,
            block_map,
            result_hash(snapshot.body_md, snapshot.metadata, block_map),
        )
        return MergeOutcome(candidate, candidate, candidate)

    def _as_base(self, version_id: UUID) -> _VirtualBase:
        return self._virtuals.get(version_id) or _VirtualBase(self.snapshot(version_id))

    def _frame(self, ours: UUID, theirs: UUID) -> _BaseFrame:
        bases = self.history.merge_bases(ours, theirs)
        return _BaseFrame(
            (ours, theirs), bases, self._as_base(bases[0]) if bases else None
        )

    def base(self, ours: UUID, theirs: UUID) -> _VirtualBase | None:
        key = (ours, theirs)
        stack = [self._frame(ours, theirs)]
        while stack:
            frame = stack[-1]
            if frame.next_index >= len(frame.bases):
                self._bases[frame.key] = frame.current
                stack.pop()
                continue
            current = frame.current
            assert current is not None
            other_id = frame.bases[frame.next_index]
            dependency = (current.snapshot.version_id, other_id)
            if dependency not in self._bases:
                stack.append(self._frame(*dependency))
                continue
            frame.current = self._combine(
                self._bases[dependency], current, self._as_base(other_id)
            )
            frame.next_index += 1
        return self._bases[key]

    def _combine(
        self, base: _VirtualBase | None, ours: _VirtualBase, theirs: _VirtualBase
    ) -> _VirtualBase:
        mine, other = ours.snapshot, theirs.snapshot
        outcome = self.service.merge(
            base.snapshot if base else None,
            mine,
            other,
            published_side="ours",
            identity_seed=f"{self.identity_seed}:base:{mine.version_id}:{other.version_id}",
            base_metadata_options=base.metadata_options if base else None,
        )
        options = merge_metadata_options(
            base.snapshot.metadata if base else PageMetadata(),
            mine.metadata,
            other.metadata,
            base_options=base.metadata_options if base else None,
            ours_options=ours.metadata_options,
            theirs_options=theirs.metadata_options,
        )
        # Preserve the conflict, not either ancestral resolution. Otherwise a
        # final tip that chose the opposite resolution looks like the only edit.
        body = self._unresolved_body(outcome.keep_both)
        metadata = outcome.keep_both.metadata
        options_key = tuple(
            (name, tuple(sorted(map(repr, values)))) for name, values in options.items()
        )
        virtual_id = uuid5(
            NAMESPACE_URL,
            f"context-merge:{mine.version_id}:{other.version_id}:"
            f"{result_hash(body, metadata, [])}:{options_key}",
        )
        if virtual_id in self._virtuals:
            return self._virtuals[virtual_id]
        snapshot = MergeSnapshot(
            mine.workspace_id,
            mine.page_id,
            virtual_id,
            body,
            metadata,
            max(saved_at_utc(mine.last_saved_at), saved_at_utc(other.last_saved_at)),
        )
        virtual = _VirtualBase(
            snapshot,
            options,
            outcome.requires_choice
            or ours.had_conflicts
            or theirs.had_conflicts
            or (base.had_conflicts if base else False),
        )
        self.history = self.history.with_node(
            VersionNode(
                mine.workspace_id,
                mine.page_id,
                virtual_id,
                (mine.version_id, other.version_id),
            )
        )
        self.snapshots[virtual_id] = snapshot
        self._virtuals[virtual_id] = virtual
        return virtual

    def _unresolved_body(self, candidate: MergeCandidate) -> str:
        parts = []
        offset = 0
        for conflict in candidate.conflicts:
            parts.append(candidate.body_md[offset : conflict.keep_both_start])
            parts.append(f"<<<<<<< {self._marker}\n")
            parts.append(_terminated(conflict.ours))
            parts.append("=======\n")
            parts.append(_terminated(conflict.theirs))
            parts.append(f">>>>>>> {self._marker}\n")
            offset = conflict.keep_both_end
        parts.append(candidate.body_md[offset:])
        return "".join(parts)


def _terminated(text: str) -> str:
    return text + "\n" if text and not text.endswith("\n") else text
