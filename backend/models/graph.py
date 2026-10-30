from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class KnowledgeNodeStat(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_node_stat"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "kind",
            "entity_id",
            name="uq_knowledge_node_stat_entity",
        ),
        Index("idx_knowledge_node_stat_authority", "workspace_id", "authority"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    in_degree: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class GraphObservation(UuidPrimaryKeyMixin, Base):
    """Append-only event evidence used only when domain rows are insufficient.

    ``EdgeProjection`` remains the unique, rebuildable current-state aggregate.
    Observations deliberately live in a separate table so repeated occurrences
    and removals can be replayed without weakening that projection contract.
    """

    __tablename__ = "graph_observation"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_graph_observation_idempotency",
        ),
        UniqueConstraint(
            "workspace_id",
            "workspace_sequence",
            name="uq_graph_observation_workspace_sequence",
        ),
        CheckConstraint(
            "operation IN ('node_created', 'edge_created', 'edge_removed', "
            "'action_occurred')",
            name="graph_observation_operation_check",
        ),
        CheckConstraint(
            "retention_class IN ('standard', 'sensitive', 'legal_hold')",
            name="graph_observation_retention_class_check",
        ),
        CheckConstraint(
            "(target_kind IS NULL AND target_id IS NULL) OR "
            "(target_kind IS NOT NULL AND target_id IS NOT NULL)",
            name="graph_observation_target_pair_check",
        ),
        Index(
            "idx_graph_observation_ws_time",
            "workspace_id",
            "occurred_at",
            "id",
        ),
        Index(
            "idx_graph_observation_source_time",
            "workspace_id",
            "source_kind",
            "source_id",
            "occurred_at",
        ),
        Index(
            "idx_graph_observation_target_time",
            "workspace_id",
            "target_kind",
            "target_id",
            "occurred_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_kind: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    edge_kind: Mapped[str | None] = mapped_column(Text)
    action_kind: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime())
    evidence_kind: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    weight_delta: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="graph-observation.v1",
    )
    retention_class: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="standard",
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)


# The one sanctioned DELETE: workspace teardown, issued after the workspace
# row itself is gone. The database trigger admits exactly that shape (see
# migrations/versions/v0_2/9aeab6944f25_add_graph_view.py); this option tells the
# ORM guard the same thing, so an ordinary session.execute(delete(...)) from
# anywhere else still fails closed.
GRAPH_OBSERVATION_TEARDOWN_OPTION = "graph_observation_teardown"


def _reject_graph_observation_mutation(*_) -> None:
    raise ValueError("graph_observation is append-only")


def _reject_graph_observation_bulk_mutation(
    orm_execute_state: ORMExecuteState,
) -> None:
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    table = getattr(orm_execute_state.statement, "table", None)
    if table is None or table.name != GraphObservation.__tablename__:
        return
    if orm_execute_state.is_delete and orm_execute_state.execution_options.get(
        GRAPH_OBSERVATION_TEARDOWN_OPTION
    ):
        return
    raise ValueError("graph_observation is append-only")


event.listen(GraphObservation, "before_update", _reject_graph_observation_mutation)
event.listen(GraphObservation, "before_delete", _reject_graph_observation_mutation)
event.listen(Session, "do_orm_execute", _reject_graph_observation_bulk_mutation)


class GraphHistoryCoverage(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Explicit evidence for whether a temporal stream can be replayed."""

    __tablename__ = "graph_history_coverage"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "stream_kind",
            "stream_id",
            name="uq_graph_history_coverage_stream",
        ),
        CheckConstraint(
            "basis IN ('domain_events', 'observations', 'mixed')",
            name="graph_history_coverage_basis_check",
        ),
        CheckConstraint(
            "status IN ('complete', 'partial', 'reconstructed', 'unsupported')",
            name="graph_history_coverage_status_check",
        ),
        Index(
            "idx_graph_history_coverage_workspace",
            "workspace_id",
            "status",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    stream_kind: Mapped[str] = mapped_column(Text, nullable=False)
    stream_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    watermark: Mapped[datetime | None] = mapped_column(UTCDateTime())
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
