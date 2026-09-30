"""Cross-domain liveness policy for durable Shared Media association holds."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.media.ports.install import BlobAssociationHoldProducerKind
from backend.models.library import LibraryAttempt
from backend.models.media import MediaBlobGcHold

_ACTIVE_ATTEMPT_STATES = frozenset({"queued", "running"})


def media_blob_gc_hold_is_active(
    session: Session,
    hold: MediaBlobGcHold,
    *,
    now: datetime,
) -> bool:
    """Decide whether an exact producer generation can still create an owner.

    Synchronous Context association uses a durable bounded lease. Conversion
    and Preview are not time-guessed: their exact attempt generation remains a
    fence for as long as that generation is queued or running. The producer's
    terminal transition and its blob-reference writes commit atomically, so a
    GC transaction can never observe "terminal and ownerless" before the
    successful association commit.
    """

    kind = BlobAssociationHoldProducerKind(hold.producer_kind)
    if kind is BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD:
        return hold.expires_at is not None and hold.expires_at > now

    stage = (
        "conversion"
        if kind is BlobAssociationHoldProducerKind.LIBRARY_CONVERSION_ATTEMPT
        else "preview_generation"
    )
    state = session.execute(
        select(
            LibraryAttempt.status, LibraryAttempt.execution_generation
        ).where(
            LibraryAttempt.workspace_id == hold.workspace_id,
            LibraryAttempt.id == hold.producer_id,
            LibraryAttempt.stage == stage,
        )
    ).one_or_none()
    return state is not None and (
        state.status in _ACTIVE_ATTEMPT_STATES
        and state.execution_generation == hold.producer_generation
    )


__all__ = ["media_blob_gc_hold_is_active"]
