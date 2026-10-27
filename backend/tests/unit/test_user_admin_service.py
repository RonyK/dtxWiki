import pytest
from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.user_admin_service import UserAdminService


class _FailingAuditService(AuditService):
    def emit(self, *args, **kwargs):
        raise RuntimeError("simulated audit failure")


def test_cli_user_creation_reuses_personal_provisioner(session_factory):
    service = UserAdminService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
    )

    created = service.add_user(
        email="cli-personal@example.com",
        password="correct-password",
    )

    with session_factory() as session:
        user = session.get(User, created.user_id)
        assert user is not None
        assert user.default_workspace_id is not None
        roles = dict(
            session.execute(
                select(Workspace.slug, WorkspaceMembership.role)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.workspace_id == Workspace.id,
                )
                .where(WorkspaceMembership.user_id == user.id)
            ).all()
        )
        personal = session.get(Workspace, user.default_workspace_id)
        assert personal is not None
        assert roles == {personal.slug: "owner"}


def test_cli_user_creation_rolls_back_when_personal_provisioning_fails(
    session_factory,
    monkeypatch,
):
    service = UserAdminService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
    )

    def fail_provisioning(*args, **kwargs):
        raise RuntimeError("simulated Personal provisioning failure")

    monkeypatch.setattr(
        "backend.services.user_admin_service.create_personal_workspace",
        fail_provisioning,
    )

    with pytest.raises(RuntimeError, match="Personal provisioning failure"):
        service.add_user(
            email="cli-rollback@example.com",
            password="correct-password",
        )

    with session_factory() as session:
        assert session.scalar(
            select(User).where(User.email == "cli-rollback@example.com")
        ) is None


def test_admin_password_reset_rolls_back_when_audit_fails(session_factory):
    hasher = CredentialHasher()
    service = UserAdminService(
        session_factory=session_factory,
        credential_hasher=hasher,
        audit_service=_FailingAuditService(),
    )
    created = service.add_user(
        email="rollback@example.com",
        password="original-password",
    )

    with pytest.raises(RuntimeError, match="simulated audit failure"):
        service.reset_password(
            email=created.email,
            password="replacement-password",
        )

    with session_factory() as session:
        user = session.scalar(select(User).where(User.id == created.user_id))
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.password.admin_reset"
            )
        ).all()
        assert user is not None
        assert hasher.verify_password("original-password", user.password_hash)
        assert not hasher.verify_password("replacement-password", user.password_hash)
        assert user.session_generation == 0
        assert audits == []
