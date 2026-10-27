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
from backend.models.user import User
from backend.services.audit_service import AuditService


def is_server_admin(session: Session, user_id: UUID) -> bool:
    """True when ``user_id`` holds a server-admin grant."""

    return session.get(ServerAdmin, user_id) is not None


class ServerAdminGrantError(ValueError):
    """Only an existing active account can hold server-admin."""


def _audit(session: Session, user_id: UUID, action: str, actor_id: str, via):
    # The grant row is only current state; revocation deletes it. History
    # lives in the server-level audit log that only server admins can read.
    # Grants come from local operator commands, never from a web session.
    AuditService().emit(
        session=session,
        workspace_id=None,
        actor_kind="system",
        actor_id=actor_id,
        action=action,
        target_kind="user",
        target_id=user_id,
        payload={"via": via},
        retention_class="sensitive",
    )


def grant_server_admin(
    session: Session,
    user_id: UUID,
    *,
    granted_by: str | None = None,
    actor_id: str = "system",
) -> ServerAdmin:
    """Grant server-admin to ``user_id`` (idempotent — re-granting is a no-op
    that returns the existing row)."""

    existing = session.get(ServerAdmin, user_id)
    if existing is not None:
        return existing
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        raise ServerAdminGrantError("server-admin requires an active user")
    record = ServerAdmin(user_id=user_id, granted_by=granted_by)
    session.add(record)
    session.flush()
    _audit(session, user_id, "server_admin.granted", actor_id, granted_by)
    return record


def revoke_server_admin(
    session: Session, user_id: UUID, *, actor_id: str = "system", via: str = "cli"
) -> bool:
    """Revoke server-admin from ``user_id``. Returns True if a grant was removed,
    False if the user was not an admin."""

    existing = session.get(ServerAdmin, user_id)
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    _audit(session, user_id, "server_admin.revoked", actor_id, via)
    return True


def list_server_admins(session: Session) -> list[ServerAdmin]:
    """All server-admin grants, oldest grant first."""

    return list(
        session.scalars(select(ServerAdmin).order_by(ServerAdmin.granted_at))
    )
