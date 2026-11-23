"""Store account and Workspace LLM connections without executing providers."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.llm.adapter_registry import LLMAdapterRegistry, LLMAdapterRegistryError
from backend.llm.credential_vault import (
    ConnectionScope,
    CredentialBinding,
    CredentialVault,
    CredentialVaultError,
)
from backend.models.base import utc_now
from backend.models.llm_connection import UserLLMConnection, WorkspaceLLMConnection
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rbac import normalize_role
from backend.schemas.llm_connection import LLMConnectionCreate, LLMConnectionUpdate
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.spi.types import ActorContext

StoredConnection = UserLLMConnection | WorkspaceLLMConnection


class LLMConnectionError(RuntimeError):
    def __init__(self, error_code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class LLMConnectionService:
    def __init__(self, *, registry: LLMAdapterRegistry, vault: CredentialVault) -> None:
        self._registry = registry
        self._vault = vault
        self._audit = AuditService()

    def list(
        self, session: Session, *, scope: ConnectionScope, owner_id: UUID
    ) -> list[dict]:
        model = _connection_model(scope)
        statement = _select_owned(model, owner_id).order_by(model.created_at, model.id)
        return [_view(connection) for connection in session.scalars(statement)]

    def get(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        connection_id: UUID,
    ) -> dict:
        return _view(_get_owned(session, scope, owner_id, connection_id))

    def create(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        request: LLMConnectionCreate,
        expected_session_generation: int | None = None,
    ) -> dict:
        _lock_owner(session, scope, owner_id, actor, expected_session_generation)
        descriptor = self._descriptor(request.adapter_id)
        credential = {
            name: value.get_secret_value() for name, value in request.credential.items()
        }
        profiles = [
            profile.model_dump(mode="json") for profile in request.model_profiles
        ]
        endpoint = self._validate(
            descriptor.adapter_id, request.provider_options, profiles, credential
        )
        connection = (
            UserLLMConnection(user_id=owner_id)
            if scope == "user"
            else WorkspaceLLMConnection(workspace_id=owner_id)
        )
        connection.id = uuid4()
        connection.name = request.name
        connection.adapter_id = descriptor.adapter_id
        connection.adapter_implementation_version = descriptor.adapter_version
        connection.config_schema_version = descriptor.config_schema_version
        connection.api_profile = descriptor.adapter_id
        connection.endpoint_profile = {"kind": "adapter_default", "base_url": endpoint}
        connection.provider_options = request.provider_options
        connection.model_profiles = profiles
        if credential:
            binding = CredentialBinding(scope, owner_id, connection.id, 1)
            try:
                connection.active_credential = self._vault.encrypt(
                    credential, binding=binding
                )
            except CredentialVaultError:
                raise LLMConnectionError(
                    "llm_vault_unavailable",
                    "LLM credential storage is unavailable.",
                    503,
                ) from None
            connection.secret_revision = binding.secret_revision
            connection.credential_updated_at = utc_now()
        session.add(connection)
        session.flush()
        self._emit(session, scope, owner_id, actor, connection, "created")
        return _view(connection)

    def update(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        request: LLMConnectionUpdate,
        expected_session_generation: int | None = None,
    ) -> dict:
        _lock_owner(session, scope, owner_id, actor, expected_session_generation)
        connection = _get_owned(session, scope, owner_id, connection_id, lock=True)
        _require_revision(connection, request.expected_revision)
        if request.name is not None:
            connection.name = request.name
        if request.provider_options is not None or request.model_profiles is not None:
            descriptor = self._descriptor(connection.adapter_id)
            if (
                descriptor.config_schema_version != connection.config_schema_version
                or descriptor.adapter_version
                != connection.adapter_implementation_version
            ):
                raise LLMConnectionError(
                    "adapter_version_unavailable",
                    "The connection's adapter version is unavailable.",
                    409,
                )
            config = (
                connection.provider_options
                if request.provider_options is None
                else request.provider_options
            )
            profiles = (
                connection.model_profiles
                if request.model_profiles is None
                else [
                    profile.model_dump(mode="json")
                    for profile in request.model_profiles
                ]
            )
            self._validate(connection.adapter_id, config, profiles, None)
            connection.provider_options = config
            connection.model_profiles = profiles
            connection.verification_status = "unverified"
            connection.verified_at = None
        connection.revision += 1
        self._emit(session, scope, owner_id, actor, connection, "updated")
        return _view(connection)

    def delete(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        expected_revision: int,
        expected_session_generation: int | None = None,
    ) -> dict:
        _lock_owner(session, scope, owner_id, actor, expected_session_generation)
        connection = _get_owned(session, scope, owner_id, connection_id, lock=True)
        _require_revision(connection, expected_revision)
        connection.status = "deleted"
        connection.verification_status = "unverified"
        connection.verified_at = None
        connection.active_credential = None
        connection.pending_credential = None
        connection.revision += 1
        self._emit(session, scope, owner_id, actor, connection, "deleted")
        return _view(connection)

    def _descriptor(self, adapter_id: str):
        try:
            return self._registry.descriptor(adapter_id)
        except LLMAdapterRegistryError:
            raise LLMConnectionError(
                "adapter_unavailable", "The LLM adapter is unavailable.", 422
            ) from None

    def _validate(
        self,
        adapter_id: str,
        config: dict,
        profiles: list[dict],
        credential: dict | None,
    ) -> str:
        try:
            json.dumps(config, allow_nan=False)
            return self._registry.validate_registration(
                adapter_id,
                config=config,
                credential=credential,
                model_options={
                    option for profile in profiles for option in profile["options"]
                },
            )
        except (ValueError, RecursionError):
            raise LLMConnectionError(
                "config_schema_mismatch",
                "The LLM connection does not match the adapter's registration schema.",
                422,
            ) from None

    def _emit(
        self,
        session: Session,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection: StoredConnection,
        event: str,
    ) -> None:
        self._audit.emit(
            session,
            workspace_id=owner_id if scope == "workspace" else None,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action=f"llm.connection.{event}",
            target_kind=f"{scope}_llm_connection",
            target_id=connection.id,
            payload={"revision": connection.revision, "status": connection.status},
        )


def _connection_model(scope: ConnectionScope) -> type[StoredConnection]:
    return UserLLMConnection if scope == "user" else WorkspaceLLMConnection


def _select_owned(model: type[StoredConnection], owner_id: UUID):
    owner_column = model.user_id if model is UserLLMConnection else model.workspace_id
    return select(model).where(owner_column == owner_id, model.status != "deleted")


def _get_owned(
    session: Session,
    scope: ConnectionScope,
    owner_id: UUID,
    connection_id: UUID,
    *,
    lock: bool = False,
) -> StoredConnection:
    model = _connection_model(scope)
    statement = _select_owned(model, owner_id).where(model.id == connection_id)
    connection = session.scalar(statement.with_for_update() if lock else statement)
    if connection is None:
        raise LLMConnectionError(
            "connection_unavailable", "LLM connection not found.", 404
        )
    return connection


def _require_revision(connection: StoredConnection, expected_revision: int) -> None:
    if connection.revision != expected_revision:
        raise LLMConnectionError(
            "connection_revision_conflict", "LLM connection revision changed.", 409
        )


def _lock_owner(
    session: Session,
    scope: ConnectionScope,
    owner_id: UUID,
    actor: ActorContext,
    expected_session_generation: int | None,
) -> None:
    if (
        actor.token_kind != "session"
        or actor.provenance != "browser_session"
        or actor.user_id is None
        or (scope == "user" and actor.user_id != owner_id)
    ):
        raise LLMConnectionError(
            "human_session_required", "Browser session required.", 403
        )
    if scope == "workspace":
        acquire_workspace_write_intent(session)
        # The shipped single-database profile can serialize mutations with
        # Workspace teardown and role changes in workspace -> user -> member order.
        workspace = session.scalar(
            select(Workspace.id).where(Workspace.id == owner_id).with_for_update()
        )
        if workspace is None:
            raise LLMConnectionError("workspace_not_found", "Workspace not found.", 404)
    else:
        acquire_write_intent(session)
    user = session.scalar(
        select(User).where(User.id == actor.user_id).with_for_update()
    )
    if user is None or user.status != "active":
        raise LLMConnectionError(
            "human_session_required", "Active browser session required.", 403
        )
    if (
        expected_session_generation is not None
        and user.session_generation != expected_session_generation
    ):
        raise LLMConnectionError(
            "recent_reauthentication_required",
            "Recent account verification is required.",
            403,
        )
    if scope == "workspace":
        role = session.scalar(
            select(WorkspaceMembership.role)
            .where(
                WorkspaceMembership.workspace_id == owner_id,
                WorkspaceMembership.user_id == actor.user_id,
            )
            .with_for_update()
        )
        if role is None or normalize_role(role) != "owner":
            raise LLMConnectionError(
                "workspace_role_insufficient",
                "Workspace owner access is required.",
                403,
            )


def _view(connection: StoredConnection) -> dict:
    # Keep the vault envelope, endpoint, and internal revision out of both
    # account and Workspace responses.
    return {
        "id": connection.id,
        "name": connection.name,
        "adapter_id": connection.adapter_id,
        "adapter_implementation_version": connection.adapter_implementation_version,
        "config_schema_version": connection.config_schema_version,
        "api_profile": connection.api_profile,
        "provider_options": connection.provider_options,
        "model_profiles": connection.model_profiles,
        "status": connection.status,
        "verification_status": connection.verification_status,
        "verified_at": connection.verified_at,
        "credential_updated_at": connection.credential_updated_at,
        "has_credential": connection.active_credential is not None,
        "revision": connection.revision,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }
