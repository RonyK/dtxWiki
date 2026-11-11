"""Snapshot preparation; transaction and publication policy stay with callers."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from backend.context_versions.domain import (
    ContentPayload,
    ContextVersionStorageError,
    SnapshotRecord,
)
from backend.context_versions.ports import SnapshotReader, SnapshotWriter


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
