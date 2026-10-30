"""Workspace-local recipient lifecycle shared by producers and account cleanup.

Both paths lock the same recipient row. Final cleanup leaves its deletion
marker in place, while producers never revive one. Source rows must be locked
before recipients; personal Inbox entries are written or removed afterward.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.inbox import WorkspaceInboxRecipient
from backend.repositories.workspace_scoped import select_in_workspace
from backend.services.db_write_intent import acquire_workspace_write_intent


def _locked_recipients(
    session: Session, workspace_id: UUID, recipient_ids: Iterable[UUID]
) -> tuple[WorkspaceInboxRecipient, ...]:
    recipients = tuple(sorted(set(recipient_ids)))
    if not recipients:
        return ()
    acquire_workspace_write_intent(session)
    dialect = session.get_bind().dialect.name
    inserts = {"postgresql": postgresql_insert, "sqlite": sqlite_insert}
    if dialect not in inserts:
        raise RuntimeError(f"inbox writes are not supported on {dialect}")
    locked = []
    for user_id in recipients:
        # Insert and lock each key before moving to the next one, including
        # when some rows already exist and others are created concurrently.
        session.execute(
            inserts[dialect](WorkspaceInboxRecipient)
            .values(workspace_id=workspace_id, user_id=user_id)
            .on_conflict_do_nothing(index_elements=["workspace_id", "user_id"])
        )
        locked.append(
            session.scalars(
                select_in_workspace(
                    WorkspaceInboxRecipient,
                    workspace_id=workspace_id,
                    conditions=(WorkspaceInboxRecipient.user_id == user_id,),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
        )
    return tuple(locked)


def writable_workspace_recipients(
    session: Session, *, workspace_id: UUID, recipient_ids: Iterable[UUID]
) -> tuple[UUID, ...]:
    """Lock recipients through the caller's transaction and omit deleted ones."""

    return tuple(
        recipient.user_id
        for recipient in _locked_recipients(session, workspace_id, recipient_ids)
        if recipient.deleted_at is None
    )


def retire_workspace_recipient(
    session: Session, *, workspace_id: UUID, user_id: UUID
) -> None:
    """Permanently stop new messages before removing this user's Inbox entries."""

    (recipient,) = _locked_recipients(session, workspace_id, (user_id,))
    if recipient.deleted_at is None:
        recipient.deleted_at = utc_now()
        session.flush()
