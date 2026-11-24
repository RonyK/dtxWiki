"""Verify LLM connections with a tiny synthetic request, off the request path.

A request records a queued operation on the control plane and returns. The
worker's verification lane claims it, sends one small generation per model
profile, and applies the outcome. The rules stay few:

* A connection has at most one verification in progress; asking again while
  it runs returns that same operation instead of paying for a second call.
* An outcome applies only while the configuration and credential it checked
  are unchanged (``config_hash``).
* A replacement credential replaces the active one when its verification
  succeeds and is discarded otherwise, so a failed replacement keeps the
  working setup.
* An operation whose worker stopped answering ends as ``outcome_unknown``:
  the provider may have been called, and nothing is sent again by itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from backend.llm.adapter_registry import LLMAdapterRegistry, LLMAdapterRegistryError
from backend.llm.credential_vault import (
    ConnectionScope,
    CredentialBinding,
    CredentialVault,
    CredentialVaultError,
)
from backend.llm.transport import HttpxLLMTransport
from backend.models.base import utc_now
from backend.models.llm_connection import (
    LLMVerificationOperation,
    UserLLMConnection,
    WorkspaceLLMConnection,
)
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.policy.rbac import normalize_role
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.services.llm_connection_service import (
    LLMConnectionError,
    LLMConnectionService,
    VerificationTarget,
    verification_hash,
)
from backend.spi.llm import (
    CanonicalTextRequest,
    ExecutionContext,
    LLMAdapterError,
    LLMModelCapabilities,
    LLMTransport,
    TextMessage,
)

logger = logging.getLogger(__name__)

IN_PROGRESS = ("queued", "running")
VERIFICATION_TIMEOUT_SECONDS = 30.0
# Enough for any model to show it answers; nothing in the reply is used.
VERIFICATION_OUTPUT_TOKENS = 16
_LEASE_MARGIN_SECONDS = 30.0
_PROMPT = "Reply with OK."

WorkspaceSessions = Callable[[UUID], sessionmaker[Session]]


class LLMVerificationService:
    def __init__(
        self,
        *,
        connections: LLMConnectionService,
        registry: LLMAdapterRegistry,
        vault: CredentialVault,
        transport_factory: Callable[[], LLMTransport] = HttpxLLMTransport,
        timeout_seconds: float = VERIFICATION_TIMEOUT_SECONDS,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._vault = vault
        self._transport_factory = transport_factory
        self._timeout_seconds = timeout_seconds
        self._audit = AuditService()

    # -- request side (control session) ------------------------------------

    def request(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        target: VerificationTarget,
        config_hash: str,
    ) -> dict:
        """Queue a verification, or return the same one already in progress."""
        acquire_write_intent(session)
        existing = self._in_progress(session, connection_id)
        if existing is None:
            operation = LLMVerificationOperation(
                id=uuid4(),
                scope=scope,
                user_id=user_id,
                workspace_id=owner_id if scope == "workspace" else None,
                connection_id=connection_id,
                target=target,
                config_hash=config_hash,
                status="queued",
                results=[],
            )
            try:
                with session.begin_nested():
                    session.add(operation)
            except IntegrityError:
                existing = self._in_progress(session, connection_id)
                if existing is None:
                    raise
            else:
                return verification_view(operation)
        if (existing.target, existing.config_hash) != (target, config_hash):
            raise LLMConnectionError(
                "verification_in_progress",
                "Another verification of this connection is in progress.",
                409,
            )
        return verification_view(existing)

    def ensure_idle(self, session: Session, *, connection_id: UUID) -> None:
        if self._in_progress(session, connection_id) is not None:
            raise LLMConnectionError(
                "verification_in_progress",
                "Another verification of this connection is in progress.",
                409,
            )

    def get(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        operation_id: UUID,
    ) -> dict:
        return verification_view(self._owned(session, scope, owner_id, operation_id))

    def cancel(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        operation_id: UUID,
    ) -> dict:
        """Stop a verification. A call already sent may still be billed."""
        acquire_write_intent(session)
        operation = self._owned(session, scope, owner_id, operation_id, lock=True)
        if operation.status in IN_PROGRESS:
            operation.status = "cancelled"
            operation.error_code = "cancelled"
            operation.lease_token = None
            operation.finished_at = utc_now()
        return verification_view(operation)

    def latest(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        connection_ids: list[UUID],
    ) -> dict[UUID, dict]:
        if not connection_ids:
            return {}
        ranked = (
            _owned_operations(scope, owner_id)
            .where(LLMVerificationOperation.connection_id.in_(connection_ids))
            .add_columns(
                func.row_number()
                .over(
                    partition_by=LLMVerificationOperation.connection_id,
                    order_by=(
                        LLMVerificationOperation.created_at.desc(),
                        LLMVerificationOperation.id.desc(),
                    ),
                )
                .label("rank")
            )
            .subquery()
        )
        newest = aliased(LLMVerificationOperation, ranked)
        rows = session.scalars(select(newest).where(ranked.c.rank == 1))
        return {
            operation.connection_id: verification_view(operation) for operation in rows
        }

    @staticmethod
    def _in_progress(
        session: Session, connection_id: UUID
    ) -> LLMVerificationOperation | None:
        return session.scalar(
            select(LLMVerificationOperation).where(
                LLMVerificationOperation.connection_id == connection_id,
                LLMVerificationOperation.status.in_(IN_PROGRESS),
            )
        )

    @staticmethod
    def _owned(
        session: Session,
        scope: ConnectionScope,
        owner_id: UUID,
        operation_id: UUID,
        *,
        lock: bool = False,
    ) -> LLMVerificationOperation:
        statement = _owned_operations(scope, owner_id).where(
            LLMVerificationOperation.id == operation_id
        )
        operation = session.scalar(statement.with_for_update() if lock else statement)
        if operation is None:
            raise LLMConnectionError(
                "verification_not_found", "Verification not found.", 404
            )
        return operation

    # -- worker side --------------------------------------------------------

    def expire_stale(self, control: sessionmaker[Session]) -> None:
        """A running operation whose lease ran out has lost its worker."""
        now = utc_now()
        stale = (
            LLMVerificationOperation.status == "running",
            LLMVerificationOperation.lease_expires_at < now,
        )
        # Read first: an idle lane must not take the SQLite writer lock
        # every second.
        with control() as session:
            if (
                session.scalar(select(LLMVerificationOperation.id).where(*stale))
                is None
            ):
                return
        with control.begin() as session:
            acquire_write_intent(session)
            session.execute(
                update(LLMVerificationOperation)
                .where(*stale)
                .values(
                    status="outcome_unknown",
                    error_code="outcome_unknown",
                    lease_token=None,
                    finished_at=now,
                )
            )

    def claim(
        self, control: sessionmaker[Session], *, limit: int
    ) -> list[tuple[UUID, UUID]]:
        with control() as session:
            queued = list(
                session.scalars(
                    select(LLMVerificationOperation.id)
                    .where(LLMVerificationOperation.status == "queued")
                    .order_by(LLMVerificationOperation.created_at)
                    .limit(limit)
                )
            )
        claimed = []
        for operation_id in queued:
            token = uuid4()
            now = utc_now()
            with control.begin() as session:
                acquire_write_intent(session)
                won = session.execute(
                    update(LLMVerificationOperation)
                    .where(
                        LLMVerificationOperation.id == operation_id,
                        LLMVerificationOperation.status == "queued",
                    )
                    .values(
                        status="running",
                        lease_token=token,
                        lease_expires_at=now + self._lease(),
                        started_at=now,
                    )
                ).rowcount
            if won:
                claimed.append((operation_id, token))
        return claimed

    def execute(
        self,
        control: sessionmaker[Session],
        workspace_sessions: WorkspaceSessions,
        operation_id: UUID,
        token: UUID,
    ) -> None:
        with control() as session:
            operation = session.get(LLMVerificationOperation, operation_id)
            if operation is None or operation.lease_token != token:
                return
            scope = operation.scope
            user_id = operation.user_id
            workspace_id = operation.workspace_id
            connection_id = operation.connection_id
            target = operation.target
            config_hash = operation.config_hash
            allowed = _requester_allowed(session, scope, user_id, workspace_id)
        if not allowed:
            self._finish(control, operation_id, token, "failed", "permission_revoked")
            return
        owner_id = user_id if scope == "user" else workspace_id
        plane = control if scope == "user" else workspace_sessions(workspace_id)
        with plane() as session:
            connection = _load_connection(session, scope, owner_id, connection_id)
            if connection is None:
                code = "connection_unavailable"
            elif verification_hash(connection, target) != config_hash:
                code = "connection_changed"
            else:
                code = None
                adapter_id = connection.adapter_id
                schema_version = connection.config_schema_version
                config = dict(connection.provider_options)
                profiles = list(connection.model_profiles)
                if target == "active":
                    endpoint = connection.endpoint_profile
                    envelope = connection.active_credential
                    binding = CredentialBinding(
                        scope, owner_id, connection_id, connection.secret_revision
                    )
                else:
                    endpoint = connection.pending_credential["endpoint_profile"]
                    envelope = connection.pending_credential["envelope"]
                    binding = CredentialBinding(
                        scope,
                        owner_id,
                        connection_id,
                        connection.secret_revision + 1,
                        purpose="pending",
                    )
        if code is None:
            code = self._preflight(adapter_id, schema_version, endpoint)
        if code is not None:
            self._settle(
                control,
                plane,
                operation_id,
                token,
                scope,
                owner_id,
                connection_id,
                target,
                config_hash,
                user_id,
                status="failed",
                code=code,
                results=[],
                credential=None,
            )
            return
        try:
            credential = (
                {}
                if envelope is None
                else self._vault.decrypt(envelope, binding=binding)
            )
        except CredentialVaultError:
            self._settle(
                control,
                plane,
                operation_id,
                token,
                scope,
                owner_id,
                connection_id,
                target,
                config_hash,
                user_id,
                status="failed",
                code="credential_unavailable",
                results=[],
                credential=None,
            )
            return

        status, code, results = "succeeded", None, []
        for profile in profiles:
            if not self._renew(control, operation_id, token):
                return  # Cancelled: discard whatever this run learned.
            outcome = self._verify_model(
                adapter_id, endpoint["base_url"], config, credential, profile
            )
            results.append(outcome)
            if outcome["outcome"] != "succeeded":
                status = "outcome_unknown" if outcome.pop("unknown") else "failed"
                code = outcome["error_code"]
                break
            outcome.pop("unknown")
        self._settle(
            control,
            plane,
            operation_id,
            token,
            scope,
            owner_id,
            connection_id,
            target,
            config_hash,
            user_id,
            status=status,
            code=code,
            results=results,
            credential=credential if status == "succeeded" else None,
        )

    def _preflight(
        self, adapter_id: str, schema_version: int, endpoint: dict
    ) -> str | None:
        try:
            descriptor = self._registry.descriptor(adapter_id)
        except LLMAdapterRegistryError:
            return "adapter_unavailable"
        if descriptor.config_schema_version != schema_version:
            return "adapter_version_unavailable"
        if not self._connections.endpoint_allowed(adapter_id, endpoint):
            return "endpoint_unavailable"
        return None

    def _verify_model(
        self,
        adapter_id: str,
        base_url: str,
        config: dict,
        credential: dict[str, str],
        profile: dict,
    ) -> dict:
        result = {"model_profile_id": profile["id"], "unknown": False}
        limit = profile.get("max_output_tokens")
        try:
            port = self._registry.create(
                adapter_id,
                base_url=base_url,
                config=config,
                credential=credential,
                model=profile["model_id"],
                capabilities=LLMModelCapabilities(
                    max_output_tokens=limit,
                    options=frozenset(profile.get("options", ())),
                ),
                transport=self._transport_factory(),
            )
        except LLMAdapterRegistryError:
            return {
                **result,
                "outcome": "failed",
                "error_code": "config_schema_mismatch",
            }
        request = CanonicalTextRequest(
            model=profile["model_id"],
            messages=(TextMessage(role="user", text=_PROMPT),),
            max_output_tokens=min(
                limit or VERIFICATION_OUTPUT_TOKENS, VERIFICATION_OUTPUT_TOKENS
            ),
        )
        try:
            reply = asyncio.run(
                port.generate(
                    request, ExecutionContext(timeout_seconds=self._timeout_seconds)
                )
            )
        except LLMAdapterError as exc:
            return {
                **result,
                "outcome": "failed",
                "error_code": exc.code,
                "unknown": exc.delivery == "unknown",
            }
        usage = {
            "input_tokens": reply.usage.input_tokens,
            "output_tokens": reply.usage.output_tokens,
        }
        # A truncated reply still proves the model answers this key.
        if reply.termination not in ("completed", "truncated"):
            return {
                **result,
                "outcome": "failed",
                "error_code": "unexpected_result",
                "usage": usage,
            }
        return {**result, "outcome": "succeeded", "error_code": None, "usage": usage}

    def _settle(
        self,
        control: sessionmaker[Session],
        plane: sessionmaker[Session],
        operation_id: UUID,
        token: UUID,
        scope: ConnectionScope,
        owner_id: UUID,
        connection_id: UUID,
        target: VerificationTarget,
        config_hash: str,
        user_id: UUID,
        *,
        status: str,
        code: str | None,
        results: list[dict],
        credential: dict[str, str] | None,
    ) -> None:
        # The connection first: it is what later requests read. If the
        # operation was cancelled meanwhile, its result is dropped below.
        if not self._still_running(control, operation_id, token):
            return
        if status != "outcome_unknown" and code not in (
            "connection_unavailable",
            "connection_changed",
            "permission_revoked",
        ):
            applied = self._apply(
                plane,
                scope,
                owner_id,
                connection_id,
                target,
                config_hash,
                user_id,
                succeeded=status == "succeeded",
                credential=credential,
            )
            if not applied:
                status, code = "failed", "connection_changed"
        self._finish(control, operation_id, token, status, code, results)

    def _apply(
        self,
        plane: sessionmaker[Session],
        scope: ConnectionScope,
        owner_id: UUID,
        connection_id: UUID,
        target: VerificationTarget,
        config_hash: str,
        user_id: UUID,
        *,
        succeeded: bool,
        credential: dict[str, str] | None,
    ) -> bool:
        now = utc_now()
        with plane.begin() as session:
            if scope == "user":
                acquire_write_intent(session)
            else:
                acquire_workspace_write_intent(session)
            connection = _load_connection(
                session, scope, owner_id, connection_id, lock=True
            )
            if (
                connection is None
                or verification_hash(connection, target) != config_hash
            ):
                return False
            if target == "active":
                connection.verification_status = "ready" if succeeded else "failed"
                connection.verified_at = now if succeeded else None
                action = "verified"
            elif succeeded:
                pending = connection.pending_credential
                secret_revision = connection.secret_revision + 1
                connection.active_credential = (
                    self._vault.encrypt(
                        credential,
                        binding=CredentialBinding(
                            scope, owner_id, connection_id, secret_revision
                        ),
                    )
                    if credential
                    else None
                )
                connection.endpoint_profile = pending["endpoint_profile"]
                connection.pending_credential = None
                connection.secret_revision = secret_revision
                connection.credential_updated_at = now
                connection.verification_status = "ready"
                connection.verified_at = now
                connection.revision += 1
                action = "credential_rotated"
            else:
                connection.pending_credential = None
                return True
            self._audit.emit(
                session,
                workspace_id=owner_id if scope == "workspace" else None,
                actor_kind="human",
                actor_id=str(user_id),
                action=f"llm.connection.{action}",
                target_kind=f"{scope}_llm_connection",
                target_id=connection_id,
                payload={
                    "revision": connection.revision,
                    "status": connection.status,
                    "verification_status": connection.verification_status,
                },
            )
        return True

    def _renew(
        self, control: sessionmaker[Session], operation_id: UUID, token: UUID
    ) -> bool:
        with control.begin() as session:
            acquire_write_intent(session)
            return bool(
                session.execute(
                    update(LLMVerificationOperation)
                    .where(
                        LLMVerificationOperation.id == operation_id,
                        LLMVerificationOperation.status == "running",
                        LLMVerificationOperation.lease_token == token,
                    )
                    .values(lease_expires_at=utc_now() + self._lease())
                ).rowcount
            )

    def _still_running(
        self, control: sessionmaker[Session], operation_id: UUID, token: UUID
    ) -> bool:
        with control() as session:
            return (
                session.scalar(
                    select(LLMVerificationOperation.id).where(
                        LLMVerificationOperation.id == operation_id,
                        LLMVerificationOperation.status == "running",
                        LLMVerificationOperation.lease_token == token,
                    )
                )
                is not None
            )

    def _finish(
        self,
        control: sessionmaker[Session],
        operation_id: UUID,
        token: UUID,
        status: str,
        code: str | None,
        results: list[dict] | None = None,
    ) -> None:
        with control.begin() as session:
            acquire_write_intent(session)
            session.execute(
                update(LLMVerificationOperation)
                .where(
                    LLMVerificationOperation.id == operation_id,
                    LLMVerificationOperation.status == "running",
                    LLMVerificationOperation.lease_token == token,
                )
                .values(
                    status=status,
                    error_code=code,
                    results=results or [],
                    lease_token=None,
                    finished_at=utc_now(),
                )
            )

    def _lease(self) -> timedelta:
        return timedelta(seconds=self._timeout_seconds + _LEASE_MARGIN_SECONDS)


def _owned_operations(scope: ConnectionScope, owner_id: UUID):
    owner = (
        LLMVerificationOperation.user_id
        if scope == "user"
        else LLMVerificationOperation.workspace_id
    )
    return select(LLMVerificationOperation).where(
        LLMVerificationOperation.scope == scope, owner == owner_id
    )


def _requester_allowed(
    session: Session,
    scope: ConnectionScope,
    user_id: UUID,
    workspace_id: UUID | None,
) -> bool:
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        return False
    if scope == "user":
        return True
    role = session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    return role is not None and normalize_role(role) == "owner"


def _load_connection(
    session: Session,
    scope: ConnectionScope,
    owner_id: UUID,
    connection_id: UUID,
    *,
    lock: bool = False,
) -> UserLLMConnection | WorkspaceLLMConnection | None:
    model = UserLLMConnection if scope == "user" else WorkspaceLLMConnection
    owner = model.user_id if scope == "user" else model.workspace_id
    statement = select(model).where(
        model.id == connection_id, owner == owner_id, model.status != "deleted"
    )
    return session.scalar(statement.with_for_update() if lock else statement)


def verification_view(operation: LLMVerificationOperation) -> dict:
    return {
        "id": operation.id,
        "connection_id": operation.connection_id,
        "target": operation.target,
        "status": operation.status,
        "error_code": operation.error_code,
        "results": [
            {
                "model_profile_id": item["model_profile_id"],
                "outcome": item["outcome"],
                "error_code": item.get("error_code"),
            }
            for item in operation.results or []
        ],
        "created_at": operation.created_at,
        "finished_at": operation.finished_at,
    }


__all__ = ["LLMVerificationService", "verification_view"]
