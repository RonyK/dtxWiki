"""Unit tests for the instance-level server-admin grant service."""

from __future__ import annotations

from backend.models.user import User
from backend.services import server_admin_service as svc


def _make_user(session, email: str = "admin@example.com") -> User:
    user = User(
        email=email,
        password_hash="x",
        display_name="Admin",
        status="active",
    )
    session.add(user)
    session.flush()
    return user


def test_grant_is_list_revoke_lifecycle(session_factory):
    with session_factory.begin() as session:
        uid = _make_user(session).id

        assert svc.is_server_admin(session, uid) is False
        assert svc.list_server_admins(session) == []

        record = svc.grant_server_admin(session, uid, granted_by="cli")
        assert record.user_id == uid
        assert record.granted_by == "cli"
        assert record.granted_at is not None
        assert svc.is_server_admin(session, uid) is True
        assert [r.user_id for r in svc.list_server_admins(session)] == [uid]

        assert svc.revoke_server_admin(session, uid) is True
        assert svc.is_server_admin(session, uid) is False
        assert svc.list_server_admins(session) == []


def test_grant_is_idempotent_and_keeps_original_granter(session_factory):
    with session_factory.begin() as session:
        uid = _make_user(session).id
        svc.grant_server_admin(session, uid, granted_by="cli")
        again = svc.grant_server_admin(session, uid, granted_by="someone-else")
        # Re-granting is a no-op: the original grant (and granter) is preserved.
        assert again.granted_by == "cli"
        assert len(svc.list_server_admins(session)) == 1


def test_revoke_missing_grant_returns_false(session_factory):
    with session_factory.begin() as session:
        uid = _make_user(session).id
        assert svc.revoke_server_admin(session, uid) is False
