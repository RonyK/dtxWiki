"""Small capabilities; callers receive only the operations they need."""

from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.context_versions.domain import (
    ContentDescriptor,
    ContentPayload,
    MergeSnapshot,
    PackResult,
    PruneResult,
    RepositoryStats,
    TextMergeResult,
)


class SnapshotReader(Protocol):
    def exists(self, workspace_id: UUID) -> bool: ...

    def read(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str,
        descriptor: ContentDescriptor,
    ) -> bytes: ...

    def read_block_map(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str,
        descriptor: ContentDescriptor,
    ) -> list[dict] | None: ...


class SnapshotWriter(Protocol):
    def commit(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        version_id: UUID,
        created_at: datetime,
        payload: ContentPayload,
        parents: Sequence[str],
    ) -> str: ...

    def set_head(
        self,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str | None,
    ) -> None: ...


class RepositoryMaintenance(Protocol):
    def list_heads(self, workspace_id: UUID) -> dict[UUID, str]: ...

    def inspect(self, workspace_id: UUID) -> RepositoryStats: ...

    def management_guard(self, workspace_id: UUID) -> AbstractContextManager: ...

    def pack(self, workspace_id: UUID, root_oids: Iterable[str]) -> PackResult: ...

    def prune_packed(self, workspace_id: UUID, result: PackResult) -> PruneResult: ...


class WorkspaceRepositoryPurger(Protocol):
    def management_guard(self, workspace_id: UUID) -> AbstractContextManager: ...

    def purge_workspace(self, workspace_id: UUID) -> list[str]: ...


class TextMerger(Protocol):
    def merge(
        self, base: str, ours: str, theirs: str, *, prefer_ours: bool
    ) -> TextMergeResult: ...


class BlockIdAssigner(Protocol):
    def assign(
        self,
        body_md: str,
        *,
        published: MergeSnapshot,
        draft: MergeSnapshot,
        identity_seed: str,
        conflict_ranges: Sequence[tuple[int, int]],
    ) -> list[dict]: ...

    def for_snapshot(
        self, snapshot: MergeSnapshot, *, identity_seed: str
    ) -> list[dict]: ...
