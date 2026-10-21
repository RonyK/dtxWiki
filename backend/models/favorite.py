from uuid import UUID

from sqlalchemy import CheckConstraint, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin


class UserFavorite(CreatedAtMixin, Base):
    __tablename__ = "user_favorite"
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('context_page', 'library_item', 'agent_page')",
            name="user_favorite_target_kind_check",
        ),
    )

    # Principal/workspace identities are authenticated on the control plane.
    # Bare UUIDs keep this personal workspace-local preference shard-resident.
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    target_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
