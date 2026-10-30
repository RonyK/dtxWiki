"""Append-only observation ledger operations for temporal Graph View reads."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.graph import GraphHistoryCoverage, GraphObservation
from backend.models.workspace import Workspace
from backend.services.read_errors import ReadNotFoundError, ReadValidationError

_OPERATIONS = {
    "node_created",
    "edge_created",
    "edge_removed",
    "action_occurred",
}
_RETENTION_CLASSES = {"standard", "sensitive", "legal_hold"}
_COVERAGE_BASES = {"domain_events", "observations", "mixed"}
_COVERAGE_STATUSES = {"complete", "partial", "reconstructed", "unsupported"}


def graph_observation_idempotency_key(*parts: object) -> str:
    """Return a versioned, fixed-width key from immutable source identity."""

    canonical = "|".join(str(part) for part in parts)
    return sha256(f"dtxwiki.graph-observation.v1|{canonical}".encode()).hexdigest()


class GraphObservationService:
    """Write/read observation facts without weakening append-only storage.

    ``retention_class`` is classification metadata only. Redaction is not
    implemented in this milestone, and reads deliberately do not pretend
    otherwise. A future append-only tombstone stream is required; there is no
    mutable pseudo-redaction column on this immutable ledger.
    """

    def record(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        operation: str,
        source_kind: str,
        source_id: UUID,
        occurred_at: datetime,
        idempotency_key: str,
        target_kind: str | None = None,
        target_id: UUID | None = None,
        edge_kind: str | None = None,
        action_kind: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        evidence_kind: str | None = None,
        evidence_id: UUID | None = None,
        evidence_ref: str | None = None,
        source_version_id: UUID | None = None,
        weight_delta: float | None = None,
        confidence: str | None = None,
        extractor_version: str = "graph-observation.v1",
        retention_class: str = "standard",
    ) -> GraphObservation:
        """Append one idempotent observation and allocate a workspace order.

        The caller supplies an immutable source-event key. It is hashed before
        storage so request text, prompts, and tool arguments cannot be smuggled
        into the graph ledger through the idempotency column.
        """

        self._validate_record(
            operation=operation,
            source_kind=source_kind,
            target_kind=target_kind,
            target_id=target_id,
            edge_kind=edge_kind,
            action_kind=action_kind,
            occurred_at=occurred_at,
            valid_from=valid_from,
            valid_to=valid_to,
            evidence_ref=evidence_ref,
            extractor_version=extractor_version,
            retention_class=retention_class,
            idempotency_key=idempotency_key,
        )
        stored_key = graph_observation_idempotency_key(idempotency_key)
        values = {
            "workspace_id": workspace_id,
            "operation": operation,
            "source_kind": source_kind,
            "source_id": source_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "edge_kind": edge_kind,
            "action_kind": action_kind,
            "occurred_at": occurred_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "evidence_kind": evidence_kind,
            "evidence_id": evidence_id,
            "evidence_ref": evidence_ref,
            "source_version_id": source_version_id,
            "weight_delta": weight_delta,
            "confidence": confidence,
            "extractor_version": extractor_version,
            "retention_class": retention_class,
            "idempotency_key": stored_key,
        }

        existing = self._by_idempotency_key(session, workspace_id, stored_key)
        if existing is not None:
            self._assert_same(existing, values)
            return existing

        observation: GraphObservation | None = None
        try:
            # The counter increment and append share one savepoint. A duplicate
            # race rolls both back, so retries never burn sequence numbers.
            with session.begin_nested():
                sequence = session.scalar(
                    update(Workspace)
                    .where(Workspace.id == workspace_id)
                    .values(
                        graph_observation_sequence=(
                            Workspace.graph_observation_sequence + 1
                        )
                    )
                    .returning(Workspace.graph_observation_sequence)
                )
                if sequence is None:
                    raise ReadNotFoundError("workspace not found")
                observation = GraphObservation(
                    **values,
                    workspace_sequence=sequence,
                    recorded_at=utc_now(),
                )
                session.add(observation)
                session.flush()
        except IntegrityError:
            existing = self._by_idempotency_key(session, workspace_id, stored_key)
            if existing is None:
                raise
            self._assert_same(existing, values)
            return existing

        assert observation is not None
        return observation

    def set_coverage(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        stream_kind: str,
        stream_id: UUID,
        basis: str,
        status: str,
        extractor_version: str,
        coverage_from: datetime | None = None,
        watermark: datetime | None = None,
    ) -> GraphHistoryCoverage:
        """Upsert explicit replay coverage for one independently proven stream."""

        if not stream_kind or len(stream_kind) > 64:
            raise ReadValidationError("stream_kind must be 1..64 characters")
        if basis not in _COVERAGE_BASES:
            raise ReadValidationError("unsupported graph history coverage basis")
        if status not in _COVERAGE_STATUSES:
            raise ReadValidationError("unsupported graph history coverage status")
        if not extractor_version or len(extractor_version) > 128:
            raise ReadValidationError("extractor_version must be 1..128 characters")
        if coverage_from is not None and watermark is not None:
            if coverage_from > watermark:
                raise ReadValidationError("coverage_from must not exceed watermark")

        coverage = session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == stream_kind,
                GraphHistoryCoverage.stream_id == stream_id,
            )
        )
        if coverage is None:
            if session.get(Workspace, workspace_id) is None:
                raise ReadNotFoundError("workspace not found")
            coverage = GraphHistoryCoverage(
                workspace_id=workspace_id,
                stream_kind=stream_kind,
                stream_id=stream_id,
                basis=basis,
                status=status,
                coverage_from=coverage_from,
                watermark=watermark,
                extractor_version=extractor_version,
            )
            session.add(coverage)
        else:
            coverage.basis = basis
            coverage.status = status
            coverage.coverage_from = coverage_from
            coverage.watermark = watermark
            coverage.extractor_version = extractor_version
            coverage.updated_at = utc_now()
        session.flush()
        return coverage

    def coverage_for(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        stream_kind: str,
        stream_id: UUID,
    ) -> GraphHistoryCoverage | None:
        return session.scalar(
            select(GraphHistoryCoverage).where(
                GraphHistoryCoverage.workspace_id == workspace_id,
                GraphHistoryCoverage.stream_kind == stream_kind,
                GraphHistoryCoverage.stream_id == stream_id,
            )
        )

    def list_for_seed(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        seed_kind: str,
        seed_id: UUID,
        through: datetime | None = None,
    ) -> list[GraphObservation]:
        statement = select(GraphObservation).where(
            GraphObservation.workspace_id == workspace_id,
            or_(
                (
                    (GraphObservation.source_kind == seed_kind)
                    & (GraphObservation.source_id == seed_id)
                ),
                (
                    (GraphObservation.target_kind == seed_kind)
                    & (GraphObservation.target_id == seed_id)
                ),
            ),
        )
        if through is not None:
            statement = statement.where(GraphObservation.occurred_at <= through)
        return list(
            session.scalars(
                statement.order_by(
                    GraphObservation.occurred_at,
                    GraphObservation.workspace_sequence,
                    GraphObservation.id,
                )
            ).all()
        )

    @staticmethod
    def _by_idempotency_key(
        session: Session,
        workspace_id: UUID,
        stored_key: str,
    ) -> GraphObservation | None:
        return session.scalar(
            select(GraphObservation).where(
                GraphObservation.workspace_id == workspace_id,
                GraphObservation.idempotency_key == stored_key,
            )
        )

    @staticmethod
    def _assert_same(
        existing: GraphObservation,
        values: dict[str, object],
    ) -> None:
        for name, expected in values.items():
            if getattr(existing, name) != expected:
                raise ReadValidationError(
                    "graph observation idempotency key was reused with different facts"
                )

    @staticmethod
    def _validate_record(
        *,
        operation: str,
        source_kind: str,
        target_kind: str | None,
        target_id: UUID | None,
        edge_kind: str | None,
        action_kind: str | None,
        occurred_at: datetime,
        valid_from: datetime | None,
        valid_to: datetime | None,
        evidence_ref: str | None,
        extractor_version: str,
        retention_class: str,
        idempotency_key: str,
    ) -> None:
        if operation not in _OPERATIONS:
            raise ReadValidationError("unsupported graph observation operation")
        if not source_kind or len(source_kind) > 64:
            raise ReadValidationError("source_kind must be 1..64 characters")
        if (target_kind is None) != (target_id is None):
            raise ReadValidationError(
                "target_kind and target_id must be supplied together"
            )
        if target_kind is not None and len(target_kind) > 64:
            raise ReadValidationError("target_kind must be at most 64 characters")
        if operation in {"edge_created", "edge_removed"}:
            if target_kind is None or not edge_kind:
                raise ReadValidationError(
                    "edge observations require a target and edge_kind"
                )
        if operation == "action_occurred" and not action_kind:
            raise ReadValidationError("action observations require action_kind")
        if edge_kind is not None and len(edge_kind) > 64:
            raise ReadValidationError("edge_kind must be at most 64 characters")
        if action_kind is not None and len(action_kind) > 64:
            raise ReadValidationError("action_kind must be at most 64 characters")
        if occurred_at is None:
            raise ReadValidationError("occurred_at is required")
        if valid_from is not None and valid_to is not None and valid_from > valid_to:
            raise ReadValidationError("valid_from must not exceed valid_to")
        if evidence_ref is not None and len(evidence_ref) > 512:
            raise ReadValidationError("evidence_ref must be at most 512 characters")
        if not extractor_version or len(extractor_version) > 128:
            raise ReadValidationError("extractor_version must be 1..128 characters")
        if retention_class not in _RETENTION_CLASSES:
            raise ReadValidationError("unsupported graph observation retention class")
        if not idempotency_key or len(idempotency_key) > 512:
            raise ReadValidationError("idempotency_key must be 1..512 characters")
