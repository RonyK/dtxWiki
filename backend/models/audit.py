from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from backend.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


class AuditEvent(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(
            "retention_class IN ('standard', 'sensitive', 'legal_hold')",
            name="audit_event_retention_class_check",
        ),
        Index("idx_audit_ws_time", "workspace_id", "created_at"),
        # Epic-3 D-3 final schema: parent side of the
        # composite FK mcp_tool_call.audit_event_id now uses.
        # workspace_id is nullable here (global/DCR events); SQLite and
        # Postgres both allow a UNIQUE constraint over a nullable column
        # (each NULL is its own distinct value).
        UniqueConstraint("workspace_id", "id", name="uq_audit_event_workspace_id"),
    )

    # Nullable for global OAuth events; DCR ``oauth.client.registered`` rows
    # have no workspace yet. Also nullable because a deleted workspace detaches
    # (never deletes) its audit history: ``ondelete="SET NULL"`` lets
    # ``WorkspaceWriteService.delete_workspace`` remove the workspace row while
    # this append-only table keeps every row (WS-02).
    workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    event_summary: Mapped[str | None] = mapped_column(Text)
    event_payload_ref: Mapped[str | None] = mapped_column(Text)
    retention_class: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="standard",
    )


def _reject_audit_mutation(*_) -> None:
    raise ValueError("audit_event is append-only")


def _reject_audit_bulk_mutation(orm_execute_state: ORMExecuteState) -> None:
    # Mapper-level before_update/before_delete only fire for unit-of-work
    # flushes; ``session.execute(update(AuditEvent)...)`` style bulk DML would
    # bypass them. This session-level guard is the only backstop on both
    # SQLite and PostgreSQL -- the schema-declared ``workspace_id`` FK
    # ``ON DELETE SET NULL`` is the sole intentional exception, applied by the
    # database itself when a workspace row is deleted, never by application
    # code. Connection-level core DML (``session.connection().execute(...)``)
    # must never target ``audit_event``.
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    table = getattr(orm_execute_state.statement, "table", None)
    if table is not None and table.name == AuditEvent.__tablename__:
        raise ValueError("audit_event is append-only")


event.listen(AuditEvent, "before_update", _reject_audit_mutation)
event.listen(AuditEvent, "before_delete", _reject_audit_mutation)
event.listen(Session, "do_orm_execute", _reject_audit_bulk_mutation)
