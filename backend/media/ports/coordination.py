"""Outbox, retention, and universal lock-order ports."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.media.domain import BlobReference


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    workspace_id: UUID
    idempotency_key: str
    topic: str
    schema_revision: str
    payload: tuple[tuple[str, str], ...]
    blob: BlobReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        for name in ("idempotency_key", "topic", "schema_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 160:
                raise ValueError(f"{name} must be non-blank and bounded")
        if not isinstance(self.payload, tuple) or len(self.payload) > 32:
            raise ValueError("outbox payload must be a bounded tuple")
        keys: set[str] = set()
        for item in self.payload:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or not item[0]
                or len(item[0]) > 80
                or len(item[1]) > 512
            ):
                raise ValueError("outbox payload entries must be bounded string pairs")
            if item[0] in keys:
                raise ValueError("outbox payload keys must be unique")
            keys.add(item[0])
        if self.blob is not None and (
            not isinstance(self.blob, BlobReference)
            or self.blob.workspace_id != self.workspace_id
        ):
            raise ValueError("blob must belong to the outbox workspace")


class Outbox(Protocol):
    def add(self, message: OutboxMessage) -> None: ...


class RetentionDecision(StrEnum):
    RELEASE = "release"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class RetentionResult:
    decision: RetentionDecision
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RetentionDecision):
            raise ValueError("decision must be a RetentionDecision")
        if (
            not isinstance(self.reason_code, str)
            or not self.reason_code
            or len(self.reason_code) > 160
        ):
            raise ValueError("reason_code must be non-blank and bounded")


class BlobRetentionGuard(Protocol):
    name: str

    def evaluate(self, blob: BlobReference) -> RetentionResult: ...


class RetentionGuardRegistry(Protocol):
    def evaluate_all(self, blob: BlobReference) -> tuple[RetentionResult, ...]: ...


class UniversalLockCoordinator(Protocol):
    def acquire_workspace_intent(self, workspace_id: UUID) -> None: ...

    def acquire_quota_or_lease(self, key: str) -> None: ...

    def acquire_domain_aggregate(self, *, rank: int, aggregate_id: str) -> None: ...

    def acquire_digest_and_incarnation(self, blob: BlobReference) -> None: ...

    def acquire_association(self, association_id: str) -> None: ...

    def acquire_outbox(self) -> None: ...
