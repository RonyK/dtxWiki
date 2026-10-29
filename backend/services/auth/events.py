"""Authentication audit events and isolated post-commit provider notifications."""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session, sessionmaker

from backend.services.audit_service import AuditService
from backend.services.auth.provider_registry import AuthProvider

logger = logging.getLogger(__name__)


@dataclass
class AuthEvents:
    session_factory: sessionmaker[Session]
    audit_service: AuditService = field(default_factory=AuditService)

    def user_event(
        self, session: Session, user_id: UUID, action: str, payload: dict[str, Any]
    ) -> None:
        self.audit_service.emit(
            session=session,
            workspace_id=None,
            actor_kind="human",
            actor_id=str(user_id),
            action=action,
            target_kind="user",
            target_id=user_id,
            payload=payload,
            retention_class="sensitive",
        )

    def login_failed(self, slug: str, reason: str) -> None:
        try:
            with self.session_factory.begin() as session:
                self.audit_service.emit(
                    session=session,
                    workspace_id=None,
                    actor_kind="human",
                    actor_id="anonymous",
                    action="auth.sso.login.failed",
                    target_kind="auth_provider",
                    target_id=uuid5(NAMESPACE_URL, slug),
                    payload={"provider_slug": slug, "reason": reason},
                    retention_class="sensitive",
                )
        except Exception:
            logger.warning("auth failure audit could not be recorded")

    def notify(self, provider: AuthProvider, name: str, *args: Any) -> None:
        hook = getattr(provider.method, name, None)
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:
            # IdP SDK errors can contain tokens. Record only stable identifiers.
            logger.warning("auth lifecycle hook failed: %s/%s", provider.slug, name)
            try:
                with self.session_factory.begin() as session:
                    self.audit_service.emit(
                        session=session,
                        workspace_id=None,
                        actor_kind="system",
                        actor_id="auth.lifecycle",
                        action="auth.lifecycle.hook_failed",
                        target_kind="auth_provider",
                        target_id=uuid5(NAMESPACE_URL, provider.slug),
                        payload={"provider_slug": provider.slug, "hook": name},
                        retention_class="sensitive",
                    )
            except Exception:
                logger.warning("auth lifecycle failure audit could not be recorded")
