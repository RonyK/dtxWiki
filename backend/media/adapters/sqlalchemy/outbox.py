"""Workspace-scoped SQLAlchemy outbox adapter."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.media.ports.contracts import IdempotencyConflictError
from backend.media.ports.coordination import OutboxMessage
from backend.models.media import MediaOutboxRow

from .coordination import lock_workspace_media_intent


class SqlAlchemyMediaOutbox:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, message: OutboxMessage) -> None:
        if not isinstance(message, OutboxMessage):
            raise ValueError("message must be an OutboxMessage")
        lock_workspace_media_intent(
            self._session,
            message.workspace_id,
            allow_deleting=False,
        )
        payload = dict(message.payload)
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self._session.scalar(
            select(MediaOutboxRow).where(
                MediaOutboxRow.workspace_id == message.workspace_id,
                MediaOutboxRow.topic == message.topic,
                MediaOutboxRow.idempotency_key == message.idempotency_key,
            )
        )
        blob_id = None if message.blob is None else message.blob.blob_id
        blob_epoch = None if message.blob is None else message.blob.epoch.value
        expected = (
            message.schema_revision,
            payload,
            payload_hash,
            blob_id,
            blob_epoch,
        )
        if existing is not None:
            actual = (
                existing.schema_revision,
                existing.payload_json,
                existing.payload_hash,
                existing.blob_id,
                existing.blob_epoch,
            )
            if actual != expected:
                raise IdempotencyConflictError("outbox")
            return
        event_id = uuid5(
            NAMESPACE_URL,
            f"media-outbox:{message.workspace_id}:{message.topic}:"
            f"{message.idempotency_key}",
        )
        self._session.add(
            MediaOutboxRow(
                workspace_id=message.workspace_id,
                id=event_id,
                topic=message.topic,
                schema_revision=message.schema_revision,
                idempotency_key=message.idempotency_key,
                payload_json=payload,
                payload_hash=payload_hash,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
                status="queued",
                attempt_count=0,
                created_at=datetime.now(UTC),
            )
        )
        self._session.flush()
