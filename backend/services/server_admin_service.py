"""Instance-level (server) admin grants.

A ``server_admins`` row's existence is the grant (sparse table). These helpers
take an open ``Session`` so they compose inside a request (the
``require_server_admin`` policy dependency) and inside a CLI transaction
(``session_factory.begin()``). They are intentionally workspace-agnostic —
server-admin governs instance/system settings only, never workspace content.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.server_admin import ServerAdmin


def is_server_admin(session: Session, user_id: UUID) -> bool:
    """True when ``user_id`` holds a server-admin grant."""

    return session.get(ServerAdmin, user_id) is not None


def grant_server_admin(
    session: Session, user_id: UUID, *, granted_by: str | None = None
) -> ServerAdmin:
    """Grant server-admin to ``user_id`` (idempotent — re-granting is a no-op
    that returns the existing row)."""

    existing = session.get(ServerAdmin, user_id)
    if existing is not None:
        return existing
    record = ServerAdmin(user_id=user_id, granted_by=granted_by)
    session.add(record)
    session.flush()
    return record


def revoke_server_admin(session: Session, user_id: UUID) -> bool:
    """Revoke server-admin from ``user_id``. Returns True if a grant was removed,
    False if the user was not an admin."""

    existing = session.get(ServerAdmin, user_id)
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    return True


def list_server_admins(session: Session) -> list[ServerAdmin]:
    """All server-admin grants, oldest grant first."""

    return list(
        session.scalars(select(ServerAdmin).order_by(ServerAdmin.granted_at))
    )
