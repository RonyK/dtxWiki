"""The inboxes that exist today: one per workspace membership, one per account."""

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.inbox import AccountInboxEntry, WorkspaceInboxEntry
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.services.inbox.base import InboxDisplay, InboxScope
from backend.services.inbox.recipients import writable_workspace_recipients


@dataclass(frozen=True)
class WorkspaceInbox(InboxScope):
    workspace_id: UUID

    name: ClassVar[str] = "workspace"
    model: ClassVar[type] = WorkspaceInboxEntry

    def owner(self, user_id: UUID) -> dict[str, UUID]:
        return {"workspace_id": self.workspace_id, "user_id": user_id}

    def acquire_write(self, session: Session) -> None:
        acquire_workspace_write_intent(session)

    def writable_recipients(
        self, session: Session, recipient_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        return writable_workspace_recipients(
            session, workspace_id=self.workspace_id, recipient_ids=recipient_ids
        )

    def check_display(self, display: InboxDisplay) -> None:
        # Entity links name their workspace; nothing resolves against the
        # workspace the browser happens to have selected.
        prefix = f"/workspaces/{self.workspace_id}/"
        if display.href is not None and not display.href.startswith(prefix):
            raise ValueError(f"inbox link {display.href!r} must start with {prefix}")


@dataclass(frozen=True)
class AccountInbox(InboxScope):
    name: ClassVar[str] = "account"
    model: ClassVar[type] = AccountInboxEntry

    def owner(self, user_id: UUID) -> dict[str, UUID]:
        return {"user_id": user_id}

    def acquire_write(self, session: Session) -> None:
        acquire_write_intent(session)
