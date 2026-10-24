from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class ReviewAudienceKind(StrEnum):
    """Who a ``request_changes`` decision routes the fix to.

    Stored as Text on ``ReviewDecision.audience_kind`` to match existing review
    columns; mirrors the ``McpChat*`` StrEnum convention in
    ``models/mcp_chat.py``.
    """

    CHAT_OWNER = "chat_owner"
    REQUESTER = "requester"
    WORKSPACE_EDITORS = "workspace_editors"


class ReviewItem(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "review_item"
    __table_args__ = (
        # Epic-3 D-3 final schema: workspace-scoped, not just set-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "change_set_id"],
            ["change_set.workspace_id", "change_set.id"],
            name="fk_review_item_change_set_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Parent side of the composite FK review_decision now uses.
        UniqueConstraint("workspace_id", "id", name="uq_review_item_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    review_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    risk_level: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    risk_policy_version: Mapped[str | None] = mapped_column(Text)
    risk_resolution_summary: Mapped[str | None] = mapped_column(Text)
    risk_signals: Mapped[dict | None] = mapped_column(JSON)
    risk_policy_snapshot: Mapped[dict | None] = mapped_column(JSON)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decision_summary: Mapped[str | None] = mapped_column(Text)
    # I18N-08a: a stable machine code for the fixed-copy `decision_summary`
    # cases (e.g. "context draft requires review"), so the frontend can
    # localize instead of rendering the persisted English prose. NULL for
    # rows written before this column existed and for caller-supplied free
    # text (e.g. `risk_notes`) that has no fixed translation -- callers fall
    # back to `decision_summary` whenever this is unset.
    decision_summary_code: Mapped[str | None] = mapped_column(Text)
    # Soft-claim (D1): the principal currently working a broadcast/open
    # changes_requested item, cleared on resolution or after CLAIM_TTL. Bare
    # Uuid (no FK), like actor_id.
    claimed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    decisions: Mapped[list["ReviewDecision"]] = relationship(
        back_populates="review_item",
        cascade="all, delete-orphan",
    )


class ReviewDecision(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "review_decision"
    __table_args__ = (
        # Match history/latest-decision reads, including timestamp ties.
        Index(
            "ix_review_decision_review_created_id",
            "review_item_id",
            column("created_at").desc(),
            column("id").desc(),
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just item-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "review_item_id"],
            ["review_item.workspace_id", "review_item.id"],
            name="fk_review_decision_review_item_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `review_item` (see
    # ReviewItem.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    review_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    # Routing target of a request_changes decision (ⓐ). Meaningful only when
    # decision_type == 'request_changes'; NULL for approve/reject. audience_kind
    # is one of ReviewAudienceKind; audience_user_id snapshots the resolved
    # principal for chat_owner/requester (NULL for workspace_editors, evaluated
    # dynamically at query time). Bare Uuid (no FK), like actor_id.
    audience_kind: Mapped[str | None] = mapped_column(Text)
    audience_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
    )
    resulting_review_status: Mapped[str] = mapped_column(Text, nullable=False)
    resulting_change_set_status: Mapped[str] = mapped_column(Text, nullable=False)

    review_item: Mapped[ReviewItem] = relationship(back_populates="decisions")
