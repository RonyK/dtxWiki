"""Per-user Inbox entries.

One table per plane holds two row modes. A ``stored`` row is a message a
producer wrote, together with the recipient's personal marks. A ``mark`` row
holds only the recipient's marks for an item a computed source derives on
read, so the item itself is never stored. Marks record the revision the
recipient saw: an entry is unread while ``read_revision`` differs from the
current revision.

A workspace recipient row serializes message producers with final account
cleanup. Its deletion marker stays after personal entries are removed, so
retained shared history cannot recreate an inbox for a deleted account.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, utc_now

_MODE_CHECK = "mode IN ('stored', 'mark')"
_MODE_COLUMNS_CHECK = (
    "(mode = 'stored' AND revision IS NOT NULL AND event_at IS NOT NULL)"
    " OR (mode = 'mark' AND revision IS NULL AND ref IS NULL"
    " AND event_at IS NULL)"
)


class WorkspaceInboxRecipient(Base):
    __tablename__ = "workspace_inbox_recipient"

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WorkspaceInboxEntry(Base):
    __tablename__ = "workspace_inbox_entry"
    __table_args__ = (
        CheckConstraint(_MODE_CHECK, name="workspace_inbox_entry_mode_check"),
        CheckConstraint(
            _MODE_COLUMNS_CHECK, name="workspace_inbox_entry_mode_columns_check"
        ),
        Index(
            "ix_workspace_inbox_entry_list",
            "workspace_id",
            "user_id",
            "mode",
            "event_at",
            "kind",
            "item_key",
        ),
    )

    # Bare UUIDs keep this per-user workspace data shard-resident, as with
    # user_favorite: identities are authenticated on the control plane.
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    item_key: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str | None] = mapped_column(Text)
    # Kind-defined identifiers only; display text is projected on read.
    ref: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    read_revision: Mapped[str | None] = mapped_column(Text)
    archived_revision: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )


class AccountInboxEntry(Base):
    __tablename__ = "account_inbox_entry"
    __table_args__ = (
        CheckConstraint(_MODE_CHECK, name="account_inbox_entry_mode_check"),
        CheckConstraint(
            _MODE_COLUMNS_CHECK, name="account_inbox_entry_mode_columns_check"
        ),
        Index(
            "ix_account_inbox_entry_list",
            "user_id",
            "mode",
            "event_at",
            "kind",
            "item_key",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    item_key: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str | None] = mapped_column(Text)
    ref: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    read_revision: Mapped[str | None] = mapped_column(Text)
    archived_revision: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
