"""Trusted SSO addresses that collide with an unverified existing account.

The requester gets no account while the collision stands, so no second
Personal workspace appears. The owner may link the identity after signing in,
or a server admin may release the address. Releasing deactivates the old
account and clears its address; its data stays for a later deletion design.
"""

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.auth_identity import UserIdentity
from backend.models.auth_verification import EmailConflictRequest
from backend.models.base import utc_now
from backend.models.server_admin import ServerAdmin
from backend.models.user import User
from backend.services.audit_service import AuditService
from backend.services.auth.account_credentials import revoke_user_credentials
from backend.services.auth.login_transactions import AuthFlowError
from backend.services.db_write_intent import acquire_write_intent

TICKET_TTL_SECONDS = 10 * 60
TICKET_MAX_BYTES = 4096


class EmailConflictRequired(AuthFlowError):
    """Carries the verified facts the browser may submit as a release request."""

    def __init__(self, facts: dict[str, str]):
        super().__init__("account_email_conflict")
        self.facts = facts


class EmailConflictError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409):
        self.error_code = code
        self.status_code = status_code
        super().__init__(code)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class EmailConflictTicketCodec:
    """Short-lived proof that this browser completed the colliding IdP login."""

    secret: str

    def _sign(self, payload: str) -> str:
        key = hmac.digest(self.secret.encode(), b"dtxwiki.email_conflict.v1", "sha256")
        return _encode(hmac.digest(key, payload.encode(), "sha256"))

    def issue(self, facts: dict[str, str], now: datetime | None = None) -> str:
        now = now or utc_now()
        expires = int((now + timedelta(seconds=TICKET_TTL_SECONDS)).timestamp())
        payload = _encode(
            json.dumps({**facts, "exp": expires}, sort_keys=True).encode()
        )
        return f"{payload}.{self._sign(payload)}"

    def verify(self, ticket: str | None, now: datetime | None = None) -> dict[str, str]:
        now = now or utc_now()
        if not ticket or len(ticket) > TICKET_MAX_BYTES or ticket.count(".") != 1:
            raise EmailConflictError("email_conflict_ticket_invalid", 400)
        payload, signature = ticket.split(".")
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise EmailConflictError("email_conflict_ticket_invalid", 400)
        try:
            facts = json.loads(_decode(payload))
        except ValueError:
            raise EmailConflictError("email_conflict_ticket_invalid", 400) from None
        if not isinstance(facts, dict) or facts.get("exp", 0) <= now.timestamp():
            raise EmailConflictError("email_conflict_ticket_invalid", 400)
        return facts


def _audit(session, *, actor_kind, actor_id, action, target_id, payload):
    AuditService().emit(
        session=session,
        workspace_id=None,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=action,
        target_kind="user",
        target_id=target_id,
        payload=payload,
        retention_class="sensitive",
    )


def release_account(
    session: Session,
    user: User,
    now: datetime,
    *,
    actor_kind: str,
    actor_id: str,
    via: str,
    request_id: UUID,
) -> None:
    """Deactivate an account and free its address; its data is kept."""
    released = user.email
    user.status = "disabled"
    user.email = None
    user.email_verified_at = None
    revoke_user_credentials(session, user, now)
    session.flush()
    _audit(
        session,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action="auth.account.released",
        target_id=user.id,
        payload={
            "released_email": released,
            "via": via,
            "request_id": str(request_id),
            "sessions_and_credentials_revoked": True,
        },
    )


def releasable(session: Session, user: User, email: str) -> bool:
    """Only an unverified holder that is not a server admin may lose it."""
    return (
        user.email == email
        and user.email_verified_at is None
        and session.get(ServerAdmin, user.id) is None
    )


def auto_release(session, provider, assertion, owner, email, now) -> None:
    """Provider policy chose SSO priority: release within the login transaction."""
    actor = f"provider:{provider.slug}"
    request = EmailConflictRequest(
        target_user_id=owner.id,
        email=email,
        provider_slug=provider.slug,
        issuer=assertion.issuer,
        subject=assertion.subject,
        status="auto_released",
        decided_by=actor,
        decided_at=now,
    )
    session.add(request)
    session.flush()
    release_account(
        session,
        owner,
        now,
        actor_kind="system",
        actor_id=actor,
        via="auto_release",
        request_id=request.id,
    )


@dataclass
class EmailConflictService:
    session_factory: object

    def submit(self, facts: dict[str, str]) -> dict:
        """Record the requester's release request once; repeats are idempotent."""
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            target = session.scalar(
                select(User)
                .where(User.id == UUID(str(facts["target_user_id"])))
                .with_for_update()
            )
            linked = session.scalar(
                select(UserIdentity.id).where(
                    UserIdentity.issuer == facts["issuer"],
                    UserIdentity.subject == facts["subject"],
                )
            )
            if (
                target is None
                or linked is not None
                or not releasable(session, target, facts["email"])
            ):
                # The address moved or the identity was linked meanwhile: a new
                # SSO login now decides the outcome without an admin.
                return {"status": "resolved", "request_id": None}
            existing = session.scalar(
                select(EmailConflictRequest).where(
                    EmailConflictRequest.target_user_id == target.id,
                    EmailConflictRequest.issuer == facts["issuer"],
                    EmailConflictRequest.subject == facts["subject"],
                    EmailConflictRequest.status == "pending",
                )
            )
            if existing is not None:
                return {"status": "pending", "request_id": existing.id}
            request = EmailConflictRequest(
                target_user_id=target.id,
                email=facts["email"],
                provider_slug=facts["provider_slug"],
                issuer=facts["issuer"],
                subject=facts["subject"],
                status="pending",
            )
            session.add(request)
            session.flush()
            _audit(
                session,
                actor_kind="system",
                actor_id=f"provider:{facts['provider_slug']}",
                action="auth.email_conflict.requested",
                target_id=target.id,
                payload={"request_id": str(request.id), "email": facts["email"]},
            )
            return {"status": "pending", "request_id": request.id}

    def list_requests(self, *, status: str | None, limit: int, offset: int):
        with self.session_factory() as session:
            predicates = (
                [EmailConflictRequest.status == status] if status is not None else []
            )
            total = session.scalar(
                select(func.count())
                .select_from(EmailConflictRequest)
                .where(*predicates)
            )
            rows = session.execute(
                select(EmailConflictRequest, User)
                .join(User, User.id == EmailConflictRequest.target_user_id)
                .where(*predicates)
                .order_by(
                    EmailConflictRequest.created_at.desc(), EmailConflictRequest.id
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return total or 0, [
                {
                    "request_id": request.id,
                    "status": request.status,
                    "email": request.email,
                    "provider_slug": request.provider_slug,
                    "subject": request.subject,
                    "created_at": request.created_at,
                    "decided_at": request.decided_at,
                    "decided_by": request.decided_by,
                    "reason": request.reason,
                    "target": {
                        "user_id": user.id,
                        "display_name": user.display_name,
                        "status": user.status,
                        "created_at": user.created_at,
                    },
                }
                for request, user in rows
            ]

    def decide(
        self,
        request_id: UUID,
        *,
        approve: bool,
        reason: str,
        actor_user_id: UUID,
        actor_session_generation: int,
    ) -> dict:
        reason = reason.strip()
        if not 0 < len(reason) <= 1000:
            raise EmailConflictError("invalid_email_conflict_decision", 422)
        now = utc_now()
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            request = session.scalar(
                select(EmailConflictRequest)
                .where(EmailConflictRequest.id == request_id)
                .with_for_update()
            )
            if request is None:
                raise EmailConflictError("email_conflict_not_found", 404)
            locked = {}
            for identifier in sorted({actor_user_id, request.target_user_id}, key=str):
                locked[identifier] = session.scalar(
                    select(User).where(User.id == identifier).with_for_update()
                )
            actor = locked[actor_user_id]
            if (
                actor is None
                or actor.status != "active"
                or actor.session_generation != actor_session_generation
            ):
                raise EmailConflictError("recent_reauthentication_required", 401)
            if session.get(ServerAdmin, actor_user_id) is None:
                raise EmailConflictError("server_admin_required", 403)
            if request.status != "pending":
                raise EmailConflictError("email_conflict_already_decided")
            target = locked[request.target_user_id]
            if approve and (
                target is None or not releasable(session, target, request.email)
            ):
                request.status = "resolved"
            else:
                request.status = "approved" if approve else "rejected"
            request.reason = reason
            request.decided_by = str(actor_user_id)
            request.decided_at = now
            session.flush()
            _audit(
                session,
                actor_kind="human",
                actor_id=str(actor_user_id),
                action=f"auth.email_conflict.{request.status}",
                target_id=request.target_user_id,
                payload={
                    "request_id": str(request.id),
                    "email": request.email,
                    "reason": reason,
                },
            )
            if request.status == "approved":
                release_account(
                    session,
                    target,
                    now,
                    actor_kind="human",
                    actor_id=str(actor_user_id),
                    via="admin_approval",
                    request_id=request.id,
                )
            return {"request_id": request.id, "status": request.status}
