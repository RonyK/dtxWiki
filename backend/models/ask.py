from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class AskSession(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "ask_session"
    __table_args__ = (
        Index("ask_session_workspace_started_idx", "workspace_id", "started_at"),
        UniqueConstraint("workspace_id", "id", name="uq_ask_session_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just node-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "default_context_parent_tree_node_id"],
            ["workspace_tree_node.workspace_id", "workspace_tree_node.id"],
            name=conv(
                "fk_ask_session_default_context_parent_tree_node_id_workspace_scoped"
            ),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "placement_tree_node_id"],
            ["workspace_tree_node.workspace_id", "workspace_tree_node.id"],
            name="fk_ask_session_placement_tree_node_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    title_user_edited: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    default_context_parent_tree_node_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    default_topic_tags: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    placement_tree_node_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class AskTurn(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "ask_turn"
    __table_args__ = (
        UniqueConstraint(
            "ask_session_id",
            "turn_no",
            name="ask_turn_session_no_uq",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_ask_turn_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "promoted_context_page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_ask_turn_promoted_context_page_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # session-/node-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "ask_session_id"],
            ["ask_session.workspace_id", "ask_session.id"],
            name="fk_ask_turn_ask_session_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "recommended_placement_tree_node_id"],
            ["workspace_tree_node.workspace_id", "workspace_tree_node.id"],
            name="fk_ask_turn_recommended_placement_tree_node_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # bundle-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "used_agent_input_bundle_id"],
            ["agent_input_bundle.workspace_id", "agent_input_bundle.id"],
            name="fk_ask_turn_used_agent_input_bundle_id_workspace_scoped",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `ask_session` (see
    # AskSession.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    ask_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    turn_no: Mapped[int] = mapped_column(nullable=False)
    user_message: Mapped[str | None] = mapped_column(Text)
    assistant_message: Mapped[str | None] = mapped_column(Text)
    used_agent_input_bundle_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    topic_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    promoted_context_page_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    recommended_placement_tree_node_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    placement_confidence: Mapped[str | None] = mapped_column(Text)
    promotion_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="not_promoted",
    )
    retrieval_trace: Mapped[list[dict[str, str]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    tried_lanes: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    fallback_disclosure: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    warning_codes: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )


class AskTurnCitation(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Normalized turn→evidence relation (G0 §19.5.7 item 1).

    Replaces the AskTurn.cited_context_page_ids / cited_library_artifact_ids
    JSON arrays: rows are queryable/joinable for usage edges, rank preserves
    the citation order the answer presented.
    """

    __tablename__ = "ask_turn_citation"
    __table_args__ = (
        UniqueConstraint(
            "ask_turn_id",
            "target_kind",
            "target_id",
            name="ask_turn_citation_uq",
        ),
        Index("idx_ask_turn_citation_target", "target_kind", "target_id"),
        CheckConstraint(
            "target_kind IN ('context_page', 'library_artifact')",
            name="ask_turn_citation_target_kind_check",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just turn-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "ask_turn_id"],
            ["ask_turn.workspace_id", "ask_turn.id"],
            name="fk_ask_turn_citation_ask_turn_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `ask_turn` (see
    # AskTurn.workspace_id, itself denormalized from ask_session).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    ask_turn_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ContextIngressProposal(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "context_ingress_proposal"
    __table_args__ = (
        CheckConstraint(
            "proposed_save_mode <> 'review_only'",
            name="context_ingress_save_mode_p0_allowed",
        ),
        Index(
            "context_ingress_proposal_status_idx",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index("idx_context_ingress_source_ask_turn", "source_ask_turn_id"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_context_ingress_proposal_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just set-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "accepted_change_set_id"],
            ["change_set.workspace_id", "change_set.id"],
            name=conv(
                "fk_context_ingress_proposal_accepted_change_set_id_workspace_scoped"
            ),
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # turn-/node-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "source_ask_turn_id"],
            ["ask_turn.workspace_id", "ask_turn.id"],
            name="fk_context_ingress_proposal_source_ask_turn_id_workspace_scoped",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposed_parent_tree_node_id"],
            ["workspace_tree_node.workspace_id", "workspace_tree_node.id"],
            name=conv(
                "fk_context_ingress_proposal_proposed_parent_tree_node_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_ask_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    proposed_context_page_type: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_parent_tree_node_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    proposed_title: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_save_mode: Mapped[str] = mapped_column(Text, nullable=False)
    placement_reason: Mapped[str | None] = mapped_column(Text)
    placement_confidence: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="medium",
    )
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    accepted_change_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProposalSourceItem(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Normalized proposal→library-item relation (G0 §19.5.7 items 2+5).

    Replaces ContextIngressProposal.library_item_ids, which for ask_turn
    proposals misused the field to hold the source turn id (now
    source_ask_turn_id). Library-sourced proposals record their input items
    here, rank preserving the proposal's item order.
    """

    __tablename__ = "proposal_source_item"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "library_item_id",
            name="proposal_source_item_uq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just item-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_id"],
            ["library_item.workspace_id", "library_item.id"],
            name="fk_proposal_source_item_library_item_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just proposal-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            [
                "context_ingress_proposal.workspace_id",
                "context_ingress_proposal.id",
            ],
            name="fk_proposal_source_item_proposal_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `proposal` (see
    # ContextIngressProposal.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    library_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
