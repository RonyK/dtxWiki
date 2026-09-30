from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import conv

from backend.models.base import Base, UTCDateTime, UuidPrimaryKeyMixin, utc_now


class McpChatArtifactKind(StrEnum):
    CONTEXT_PAGE = "context_page"
    LIBRARY_ITEM = "library_item"
    AGENT_PAGE = "agent_page"
    # Keyed by ContextMediaAsset.id. Every asset is page-owned since P3 (no
    # more session-bound ownership pending a later claim), but the id itself
    # predates that and was already stable, so old rows referencing it here
    # still resolve correctly.
    CONTEXT_MEDIA_ASSET = "context_media_asset"


class McpChatArtifactVerb(StrEnum):
    CREATED = "created"
    EDITED = "edited"
    CITED = "cited"
    REFERENCED = "referenced"
    SEARCH_MATCH = "search_match"


# Every artifact relationship must belong to exactly one semantic class. Graph
# and trail projections consume only CONSUMPTION_VERBS; an item merely exposed
# in search results must never acquire read/co-access semantics.
CONSUMPTION_VERBS = frozenset(
    {McpChatArtifactVerb.REFERENCED, McpChatArtifactVerb.CITED}
)
AUTHORING_VERBS = frozenset(
    {McpChatArtifactVerb.CREATED, McpChatArtifactVerb.EDITED}
)
EXPOSURE_VERBS = frozenset({McpChatArtifactVerb.SEARCH_MATCH})


class McpChatSession(UuidPrimaryKeyMixin, Base):
    """One external MCP conversation observed through dtxWiki tools."""

    __tablename__ = "mcp_chat_session"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "mcp_session_id",
            name="uq_mcp_chat_session_per_workspace",
        ),
        Index(
            "ix_mcp_chat_session_workspace_last_activity",
            "workspace_id",
            "last_activity_at",
        ),
        Index(
            "ix_mcp_chat_session_workspace_conversation_group",
            "workspace_id",
            "conversation_group_id",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just client-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "client_id"],
            ["mcp_client.workspace_id", "mcp_client.id"],
            name="fk_mcp_chat_session_client_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_mcp_chat_session_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    mcp_session_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    write_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # `error_count` keeps summing all three failure statuses for compatibility;
    # these split the two an auditor acts on differently -- a tool that broke
    # versus a call workspace policy refused.
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    rejected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    title: Mapped[str | None] = mapped_column(Text)
    title_user_edited: Mapped[bool] = mapped_column(nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text)
    frozen_summary: Mapped[str | None] = mapped_column(Text)
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    conversation_group_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    artifact_links: Mapped[list["McpChatArtifactLink"]] = relationship(
        back_populates="chat_session",
        cascade="all, delete-orphan",
    )


class McpChatArtifactLink(UuidPrimaryKeyMixin, Base):
    """Rollup link between an external MCP chat and a dtxWiki artifact."""

    __tablename__ = "mcp_chat_artifact_link"
    __table_args__ = (
        UniqueConstraint(
            "chat_session_id",
            "resource_kind",
            "resource_id",
            "verb",
            name="uq_mcp_chat_artifact_link",
        ),
        Index("ix_mcp_chat_artifact_link_resource", "resource_kind", "resource_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # session/tool-call-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "chat_session_id"],
            ["mcp_chat_session.workspace_id", "mcp_chat_session.id"],
            name="fk_mcp_chat_artifact_link_chat_session_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "last_mcp_tool_call_id"],
            ["mcp_tool_call.workspace_id", "mcp_tool_call.id"],
            name=conv(
                "fk_mcp_chat_artifact_link_last_mcp_tool_call_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "last_read_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name=("fk_mcp_chat_artifact_link_last_read_version_id_workspace_scoped"),
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `mcp_chat_session` (see
    # McpChatSession.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    chat_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    resource_kind: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    first_occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    last_occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_mcp_tool_call_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    # Correctness-bearing conversation base ledger. Ordinary reference touches
    # do not overwrite its independent timestamp or version/root identity.
    last_read_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_read_root_hash: Mapped[str | None] = mapped_column(Text)
    last_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    chat_session: Mapped[McpChatSession] = relationship(back_populates="artifact_links")


class McpChatToolCallArtifact(UuidPrimaryKeyMixin, Base):
    """Per-call edge: which artifact a single MCP tool call touched, and how.

    The rollup ``McpChatArtifactLink`` keeps only ``last_mcp_tool_call_id``, so
    an artifact touched by several calls would attach to the latest call only.
    This edge persists one row per (tool call, artifact, verb) so the per-call
    timeline can render every call that touched the artifact, with its headline.
    """

    __tablename__ = "mcp_chat_tool_call_artifact"
    __table_args__ = (
        UniqueConstraint(
            "mcp_tool_call_id",
            "resource_kind",
            "resource_id",
            "verb",
            name="uq_mcp_chat_tool_call_artifact",
        ),
        Index(
            "ix_mcp_chat_tool_call_artifact_resource",
            "resource_kind",
            "resource_id",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # session/tool-call-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "chat_session_id"],
            ["mcp_chat_session.workspace_id", "mcp_chat_session.id"],
            name="fk_mcp_chat_tool_call_artifact_chat_session_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "mcp_tool_call_id"],
            ["mcp_tool_call.workspace_id", "mcp_tool_call.id"],
            name=conv(
                "fk_mcp_chat_tool_call_artifact_mcp_tool_call_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `mcp_chat_session` (see
    # McpChatSession.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    chat_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    mcp_tool_call_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    resource_kind: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
