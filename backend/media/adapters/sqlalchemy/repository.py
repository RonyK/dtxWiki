"""SQLAlchemy adapter for the media aggregate repository port."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.media.domain import (
    BlobEpoch,
    BlobIncarnation,
    BlobState,
    Sha256Digest,
    StoredBlob,
    WorkspaceBlobIdentity,
)
from backend.media.ports.contracts import IdempotencyConflictError
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobActiveIncarnationRow,
    StoredBlobRow,
)

from .coordination import lock_workspace_media_intent


class SqlAlchemyMediaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_blob_id(
        self, workspace_id: UUID, blob_id: UUID
    ) -> StoredBlob | None:
        root = self._session.get(
            StoredBlobRow,
            {"workspace_id": workspace_id, "id": blob_id},
        )
        return None if root is None else self._to_domain(root)

    def find_by_identity(
        self, identity: WorkspaceBlobIdentity
    ) -> StoredBlob | None:
        root = self._session.scalar(
            select(StoredBlobRow).where(
                StoredBlobRow.workspace_id == identity.workspace_id,
                StoredBlobRow.sha256 == identity.sha256.value,
            )
        )
        return None if root is None else self._to_domain(root)

    def put(self, blob: StoredBlob) -> None:
        if not isinstance(blob, StoredBlob):
            raise ValueError("blob must be a StoredBlob")
        workspace_id = blob.identity.workspace_id
        lock_workspace_media_intent(
            self._session,
            workspace_id,
            allow_deleting=False,
        )
        root = self._session.scalar(
            select(StoredBlobRow)
            .where(
                StoredBlobRow.workspace_id == workspace_id,
                StoredBlobRow.id == blob.blob_id,
            )
            .with_for_update()
        )
        duplicate = self._session.scalar(
            select(StoredBlobRow.id).where(
                StoredBlobRow.workspace_id == workspace_id,
                StoredBlobRow.sha256 == blob.identity.sha256.value,
                StoredBlobRow.id != blob.blob_id,
            )
        )
        if duplicate is not None:
            raise IdempotencyConflictError("workspace_blob_identity")

        if root is None:
            self._insert_new(blob)
            return
        if root.sha256 != blob.identity.sha256.value:
            raise IdempotencyConflictError("blob_identity")
        self._update_existing(root, blob)

    def _insert_new(self, blob: StoredBlob) -> None:
        if blob.active is None and blob.next_epoch.value != 1:
            raise ValueError("new inactive blob must start at epoch one")
        if blob.active is not None and (
            blob.active.epoch.value != 1 or blob.next_epoch.value != 2
        ):
            raise ValueError("first incarnation must allocate epoch one exactly")
        now = datetime.now(UTC)
        self._session.add(
            StoredBlobRow(
                workspace_id=blob.identity.workspace_id,
                id=blob.blob_id,
                sha256=blob.identity.sha256.value,
                next_epoch=blob.next_epoch.value,
                created_at=now,
                updated_at=now,
            )
        )
        self._session.flush()
        if blob.active is not None:
            self._insert_incarnation(blob.active, now=now)

    def _update_existing(self, root: StoredBlobRow, blob: StoredBlob) -> None:
        pointer = self._session.scalar(
            select(StoredBlobActiveIncarnationRow)
            .where(
                StoredBlobActiveIncarnationRow.workspace_id == root.workspace_id,
                StoredBlobActiveIncarnationRow.blob_id == root.id,
            )
            .with_for_update()
        )
        if pointer is None:
            if blob.active is None:
                if blob.next_epoch.value != root.next_epoch:
                    raise ValueError("inactive epoch seed cannot change")
                return
            if (
                blob.active.epoch.value != root.next_epoch
                or blob.next_epoch.value != root.next_epoch + 1
            ):
                raise ValueError("new incarnation must consume next_epoch exactly")
            now = datetime.now(UTC)
            self._insert_incarnation(blob.active, now=now)
            root.next_epoch = blob.next_epoch.value
            root.updated_at = now
            return

        current = self._session.get(
            BlobIncarnationRow,
            {
                "workspace_id": root.workspace_id,
                "blob_id": root.id,
                "blob_epoch": pointer.blob_epoch,
            },
        )
        if current is None:
            raise RuntimeError("active_blob_incarnation_missing")
        if blob.active is None:
            if current.state not in {BlobState.TRASHED, BlobState.MISSING}:
                raise ValueError("only trashed or missing incarnation may be purged")
            if blob.next_epoch.value != root.next_epoch:
                raise ValueError("purge cannot change the epoch seed")
            current.state = BlobState.PURGED.value
            current.purged_at = datetime.now(UTC)
            self._session.delete(pointer)
            root.updated_at = datetime.now(UTC)
            return
        if blob.active.epoch.value != pointer.blob_epoch:
            raise IdempotencyConflictError("active_blob_epoch")
        if blob.next_epoch.value != root.next_epoch:
            raise ValueError("active transition cannot change the epoch seed")
        if (
            blob.active.byte_size != current.byte_size
            or blob.active.storage_key != current.storage_key
        ):
            raise ValueError("incarnation immutable storage facts changed")
        old_state = BlobState(current.state)
        current.state = blob.active.state.value
        current.publish_generation = blob.active.publish_generation
        current.security_generation = blob.active.security_generation
        current.gc_generation = blob.active.gc_generation
        now = datetime.now(UTC)
        if old_state is BlobState.PENDING_INSTALL and (
            blob.active.state is BlobState.AVAILABLE
        ):
            current.available_at = now
        if blob.active.state is BlobState.TRASHED and current.trashed_at is None:
            current.trashed_at = now
        pointer.updated_at = now
        root.updated_at = now

    def _insert_incarnation(
        self,
        incarnation: BlobIncarnation,
        *,
        now: datetime,
    ) -> None:
        if incarnation.state is BlobState.PURGED:
            raise ValueError("purged incarnation cannot become active")
        self._session.add(
            BlobIncarnationRow(
                workspace_id=incarnation.workspace_id,
                blob_id=incarnation.blob_id,
                blob_epoch=incarnation.epoch.value,
                byte_size=incarnation.byte_size,
                storage_key=incarnation.storage_key,
                state=incarnation.state.value,
                publish_generation=incarnation.publish_generation,
                security_generation=incarnation.security_generation,
                gc_generation=incarnation.gc_generation,
                created_at=now,
                available_at=(
                    now if incarnation.state is BlobState.AVAILABLE else None
                ),
            )
        )
        self._session.flush()
        self._session.add(
            StoredBlobActiveIncarnationRow(
                workspace_id=incarnation.workspace_id,
                blob_id=incarnation.blob_id,
                blob_epoch=incarnation.epoch.value,
                activated_at=now,
                updated_at=now,
            )
        )

    def _to_domain(self, root: StoredBlobRow) -> StoredBlob:
        pointer = self._session.get(
            StoredBlobActiveIncarnationRow,
            {"workspace_id": root.workspace_id, "blob_id": root.id},
        )
        active = None
        if pointer is not None:
            row = self._session.get(
                BlobIncarnationRow,
                {
                    "workspace_id": root.workspace_id,
                    "blob_id": root.id,
                    "blob_epoch": pointer.blob_epoch,
                },
            )
            if row is None:
                raise RuntimeError("active_blob_incarnation_missing")
            active = BlobIncarnation(
                workspace_id=row.workspace_id,
                blob_id=row.blob_id,
                epoch=BlobEpoch(row.blob_epoch),
                byte_size=row.byte_size,
                storage_key=row.storage_key,
                state=BlobState(row.state),
                publish_generation=row.publish_generation,
                security_generation=row.security_generation,
                gc_generation=row.gc_generation,
            )
        return StoredBlob(
            blob_id=root.id,
            identity=WorkspaceBlobIdentity(
                root.workspace_id,
                Sha256Digest(root.sha256),
            ),
            next_epoch=BlobEpoch(root.next_epoch),
            active=active,
        )
