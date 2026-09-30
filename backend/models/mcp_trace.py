from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, UuidPrimaryKeyMixin, utc_now


class MCPToolCall(UuidPrimaryKeyMixin, Base):
    __tablename__ = "mcp_tool_call"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "request_id",
            name="mcp_tool_call_workspace_request_uq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just client-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "client_id"],
            ["mcp_client.workspace_id", "mcp_client.id"],
            name="fk_mcp_tool_call_client_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_mcp_tool_call_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just set-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "change_set_id"],
            ["change_set.workspace_id", "change_set.id"],
            name="fk_mcp_tool_call_change_set_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # bundle-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "agent_input_bundle_id"],
            ["agent_input_bundle.workspace_id", "agent_input_bundle.id"],
            name="fk_mcp_tool_call_agent_input_bundle_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # event-scoped. audit_event.workspace_id is nullable (global/DCR
        # events), but every audit_event this column actually references is
        # created workspace-scoped in the same call path -- verified by the
        # migration's pre-flight, not assumed.
        ForeignKeyConstraint(
            ["workspace_id", "audit_event_id"],
            ["audit_event.workspace_id", "audit_event.id"],
            name="fk_mcp_tool_call_audit_event_id_workspace_scoped",
        ),
    )

    client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mcp_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="llmwiki.mcp.v1",
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    input_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    result_status: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    # Structured, allowlisted facts from the call's response that Chats shows
    # (a context edit's receipt: counts, changed sections, first-change
    # excerpts). Never the response body itself; see
    # mcp_trace_redaction.redact_edit_receipt.
    result_detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    audit_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    change_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_input_bundle_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    model_id: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    model_version: Mapped[str | None] = mapped_column(Text)
    params_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    client_self_reported: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    outcome: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class MCPWriteIdempotency(UuidPrimaryKeyMixin, Base):
    __tablename__ = "mcp_write_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "actor_kind",
            "actor_id",
            "tool_name",
            "idempotency_key",
            name="uq_mcp_write_idempotency_scope",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # P8.5: actor_id alone is opaque per actor_kind (a human user id and an AI
    # agent id are drawn from different namespaces but are both plain text),
    # so scoping by actor_id alone risks two different actor_kinds colliding
    # on the same string. Ledger identity is workspace + actor_kind +
    # actor_id, matching every other actor-scoped privacy predicate in this
    # codebase (e.g. `foreign_editing_version_exists`).
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
