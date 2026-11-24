"""Store account and Workspace LLM connections without executing providers."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from backend.llm.adapter_registry import (
    LLMAdapterEndpointRequiredError,
    LLMAdapterRegistry,
    LLMAdapterRegistryError,
    normalize_base_url,
)
from backend.llm.credential_vault import (
    ConnectionScope,
    CredentialBinding,
    CredentialVault,
    CredentialVaultError,
    EncryptedCredential,
    FileCredentialVault,
)
from backend.llm.endpoint_profiles import EndpointProfile, parse_endpoint_profiles
from backend.models.base import utc_now
from backend.models.llm_connection import UserLLMConnection, WorkspaceLLMConnection
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rbac import normalize_role
from backend.schemas.llm_connection import (
    LLMConnectionCreate,
    LLMConnectionUpdate,
    LLMCredentialReplace,
)
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.spi.types import ActorContext

if TYPE_CHECKING:
    from backend.app.settings import Settings

StoredConnection = UserLLMConnection | WorkspaceLLMConnection
VerificationTarget = Literal["active", "candidate"]


class LLMConnectionError(RuntimeError):
    def __init__(self, error_code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class LLMConnectionService:
    def __init__(
        self,
        *,
        registry: LLMAdapterRegistry,
        vault: CredentialVault,
        endpoint_profiles: tuple[EndpointProfile, ...] = (),
    ) -> None:
        self._registry = registry
        self._vault = vault
        self._endpoint_profiles = endpoint_profiles
        self._audit = AuditService()

    def catalog(self) -> list[dict]:
        """Installed adapters with the endpoints a connection may use."""
        return [
            {
                "adapter_id": descriptor.adapter_id,
                "adapter_version": descriptor.adapter_version,
                "display_name": dict(descriptor.display_name),
                "config_schema": dict(descriptor.config_schema),
                "credential_schema": dict(descriptor.credential_schema),
                "options": sorted(descriptor.options),
                "endpoints": [
                    {
                        "id": endpoint.get("id"),
                        "label": self._endpoint_label(endpoint),
                        "base_url": endpoint["base_url"],
                    }
                    for endpoint in self._allowed_endpoints(descriptor)
                ],
            }
            for descriptor in self._registry.descriptors()
        ]

    def endpoint_allowed(self, adapter_id: str, endpoint: dict) -> bool:
        """Whether a stored endpoint is still one the adapter may use."""
        try:
            descriptor = self._registry.descriptor(adapter_id)
        except LLMAdapterRegistryError:
            return False
        return endpoint in self._allowed_endpoints(descriptor)

    @property
    def vault(self) -> CredentialVault:
        return self._vault

    def authorize_write(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
    ) -> None:
        """Browser session of the owner (or a Workspace owner), on control."""
        _lock_owner(session, scope, owner_id, actor)

    def list(
        self, session: Session, *, scope: ConnectionScope, owner_id: UUID
    ) -> list[dict]:
        model = _connection_model(scope)
        statement = _select_owned(model, owner_id).order_by(model.created_at, model.id)
        return [
            _view(connection, has_credential=has_credential)
            for connection, has_credential in session.execute(statement)
        ]

    def get(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        connection_id: UUID,
    ) -> dict:
        connection, has_credential = _get_owned(session, scope, owner_id, connection_id)
        return _view(connection, has_credential=has_credential)

    def create(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        request: LLMConnectionCreate,
    ) -> dict:
        if scope == "user":
            _lock_owner(session, scope, owner_id, actor)
        else:
            acquire_workspace_write_intent(session)
        descriptor = self._descriptor(request.adapter_id)
        credential = {
            name: value.get_secret_value() for name, value in request.credential.items()
        }
        profiles = [
            profile.model_dump(mode="json") for profile in request.model_profiles
        ]
        endpoint = self._endpoint(descriptor, request.endpoint_profile_id)
        self._validate(
            descriptor.adapter_id,
            request.provider_options,
            profiles,
            credential,
            base_url=endpoint["base_url"],
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
        connection.endpoint_profile = endpoint
        connection.provider_options = request.provider_options
        connection.model_profiles = profiles
        if credential:
            binding = CredentialBinding(scope, owner_id, connection.id, 1)
            connection.active_credential = self._encrypt(credential, binding)
            connection.secret_revision = binding.secret_revision
            connection.credential_updated_at = utc_now()
        session.add(connection)
        session.flush()
        self._emit(session, scope, owner_id, actor, connection, "created")
        return _view(connection, has_credential=bool(credential))

    def update(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        request: LLMConnectionUpdate,
    ) -> dict:
        if scope == "user":
            _lock_owner(session, scope, owner_id, actor)
        else:
            acquire_workspace_write_intent(session)
        connection, has_credential = _get_owned(
            session, scope, owner_id, connection_id, lock=True
        )
        _require_revision(connection, request.expected_revision)
        if request.name is not None:
            connection.name = request.name
        if request.provider_options is not None or request.model_profiles is not None:
            descriptor = self._descriptor(connection.adapter_id)
            if descriptor.config_schema_version != connection.config_schema_version:
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
            endpoint = connection.endpoint_profile
            if endpoint.get("kind") == "adapter_default":
                # The built-in endpoint follows the installed adapter version.
                endpoint = self._endpoint(descriptor, None)
            self._validate(
                connection.adapter_id,
                config,
                profiles,
                None,
                base_url=endpoint["base_url"],
            )
            connection.endpoint_profile = endpoint
            connection.adapter_implementation_version = descriptor.adapter_version
            connection.provider_options = config
            connection.model_profiles = profiles
            connection.verification_status = "unverified"
            connection.verified_at = None
        if request.status is not None:
            # Only a verified configuration can be switched on.
            if (
                request.status == "enabled"
                and connection.verification_status != "ready"
            ):
                raise LLMConnectionError(
                    "connection_not_verified",
                    "Verify the LLM connection before enabling it.",
                    409,
                )
            connection.status = request.status
        connection.revision += 1
        self._emit(session, scope, owner_id, actor, connection, "updated")
        return _view(connection, has_credential=has_credential)

    def active_target(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        connection_id: UUID,
        expected_revision: int,
    ) -> str:
        """The configuration hash a verification of the stored setup checks."""
        connection, _ = _get_owned(session, scope, owner_id, connection_id)
        _require_revision(connection, expected_revision)
        return verification_hash(connection, "active")

    def stage_candidate(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        request: LLMCredentialReplace,
    ) -> str:
        """Store a replacement credential (and endpoint) beside the active one.

        The candidate is used only by its verification: success swaps it in,
        failure discards it. Returns the hash that verification checks.
        """
        if scope == "user":
            _lock_owner(session, scope, owner_id, actor)
        else:
            acquire_workspace_write_intent(session)
        connection, _ = _get_owned(session, scope, owner_id, connection_id, lock=True)
        _require_revision(connection, request.expected_revision)
        descriptor = self._descriptor(connection.adapter_id)
        if descriptor.config_schema_version != connection.config_schema_version:
            raise LLMConnectionError(
                "adapter_version_unavailable",
                "The connection's adapter version is unavailable.",
                409,
            )
        endpoint = (
            self._endpoint(descriptor, request.endpoint_profile_id)
            if "endpoint_profile_id" in request.model_fields_set
            else connection.endpoint_profile
        )
        credential = {
            name: value.get_secret_value() for name, value in request.credential.items()
        }
        self._validate(
            connection.adapter_id,
            connection.provider_options,
            connection.model_profiles,
            credential,
            base_url=endpoint["base_url"],
        )
        binding = CredentialBinding(
            scope,
            owner_id,
            connection.id,
            connection.secret_revision + 1,
            purpose="pending",
        )
        connection.pending_credential = {
            "envelope": self._encrypt(credential, binding),
            "endpoint_profile": endpoint,
        }
        return verification_hash(connection, "candidate")

    def _encrypt(
        self, credential: dict[str, str], binding: CredentialBinding
    ) -> EncryptedCredential:
        try:
            return self._vault.encrypt(credential, binding=binding)
        except CredentialVaultError:
            raise LLMConnectionError(
                "llm_vault_unavailable",
                "LLM credential storage is unavailable.",
                503,
            ) from None

    def _allowed_endpoints(self, descriptor) -> list[dict]:
        endpoints: list[dict] = []
        if descriptor.default_base_url is not None:
            endpoints.append(
                {
                    "kind": "adapter_default",
                    "base_url": normalize_base_url(descriptor.default_base_url),
                }
            )
        endpoints.extend(
            {"kind": "operator", "id": profile.id, "base_url": profile.base_url}
            for profile in self._endpoint_profiles
            if profile.adapter_id == descriptor.adapter_id
        )
        return endpoints

    def _endpoint_label(self, endpoint: dict) -> str | None:
        return next(
            (
                profile.label
                for profile in self._endpoint_profiles
                if profile.id == endpoint.get("id")
            ),
            None,
        )

    def _endpoint(self, descriptor, profile_id: str | None) -> dict:
        wanted = "adapter_default" if profile_id is None else "operator"
        for endpoint in self._allowed_endpoints(descriptor):
            if endpoint["kind"] == wanted and endpoint.get("id") == profile_id:
                return endpoint
        if profile_id is None:
            raise LLMConnectionError(
                "adapter_endpoint_required",
                "The adapter requires an endpoint profile.",
                422,
            )
        raise LLMConnectionError(
            "endpoint_profile_unavailable",
            "The endpoint profile is not available for this adapter.",
            422,
        )

    def delete(
        self,
        session: Session,
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        expected_revision: int,
    ) -> dict:
        if scope == "user":
            _lock_owner(session, scope, owner_id, actor)
        else:
            acquire_workspace_write_intent(session)
        connection, _ = _get_owned(session, scope, owner_id, connection_id, lock=True)
        _require_revision(connection, expected_revision)
        connection.status = "deleted"
        connection.verification_status = "unverified"
        connection.verified_at = None
        connection.active_credential = None
        connection.pending_credential = None
        connection.revision += 1
        self._emit(session, scope, owner_id, actor, connection, "deleted")
        return _view(connection, has_credential=False)

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
        *,
        base_url: str,
    ) -> None:
        try:
            json.dumps(config, allow_nan=False)
            self._registry.validate_registration(
                adapter_id,
                config=config,
                credential=credential,
                model_options={
                    option for profile in profiles for option in profile["options"]
                },
                base_url=base_url,
            )
        except LLMAdapterEndpointRequiredError:
            raise LLMConnectionError(
                "adapter_endpoint_required",
                "The adapter requires an endpoint profile.",
                422,
            ) from None
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


def build_llm_connection_service(
    settings: Settings, registry: LLMAdapterRegistry
) -> LLMConnectionService:
    return LLMConnectionService(
        registry=registry,
        vault=FileCredentialVault(
            keyring_file=settings.llm_credential_keyring_file,
            solo_local=settings.app_mode == "solo_local",
        ),
        endpoint_profiles=parse_endpoint_profiles(settings.llm_endpoint_profiles_json),
    )


def _connection_model(scope: ConnectionScope) -> type[StoredConnection]:
    return UserLLMConnection if scope == "user" else WorkspaceLLMConnection


def _select_owned(model: type[StoredConnection], owner_id: UUID):
    owner_column = model.user_id if model is UserLLMConnection else model.workspace_id
    return (
        select(model, model.active_credential.is_not(None).label("has_credential"))
        .options(
            defer(model.active_credential, raiseload=True),
            defer(model.pending_credential, raiseload=True),
        )
        .where(owner_column == owner_id, model.status != "deleted")
    )


def _get_owned(
    session: Session,
    scope: ConnectionScope,
    owner_id: UUID,
    connection_id: UUID,
    *,
    lock: bool = False,
) -> tuple[StoredConnection, bool]:
    model = _connection_model(scope)
    statement = _select_owned(model, owner_id).where(model.id == connection_id)
    row = session.execute(
        statement.with_for_update() if lock else statement
    ).one_or_none()
    if row is None:
        raise LLMConnectionError(
            "connection_unavailable", "LLM connection not found.", 404
        )
    connection, has_credential = row
    return connection, has_credential


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
        workspace = session.scalar(select(Workspace.id).where(Workspace.id == owner_id))
        if workspace is None:
            raise LLMConnectionError("workspace_not_found", "Workspace not found.", 404)
    else:
        acquire_write_intent(session)
    user_query = select(User).where(User.id == actor.user_id)
    user = session.scalar(
        user_query.with_for_update() if scope == "user" else user_query
    )
    if user is None or user.status != "active":
        raise LLMConnectionError(
            "human_session_required", "Active browser session required.", 403
        )
    if scope == "workspace":
        role = session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == owner_id,
                WorkspaceMembership.user_id == actor.user_id,
            )
        )
        if role is None or normalize_role(role) != "owner":
            raise LLMConnectionError(
                "workspace_role_insufficient",
                "Workspace owner access is required.",
                403,
            )


def verification_hash(connection: StoredConnection, target: VerificationTarget) -> str:
    """Name what a verification checks: configuration, endpoint and secret.

    A result is applied only while this is unchanged, so an edit or a newer
    key made during the call is never marked as verified. The name is not
    part of it.
    """
    if target == "active":
        endpoint = connection.endpoint_profile
        secret: object = connection.secret_revision
    else:
        pending = connection.pending_credential or {}
        endpoint = pending.get("endpoint_profile")
        secret = (pending.get("envelope") or {}).get("nonce")
    state = {
        "target": target,
        "adapter_id": connection.adapter_id,
        "adapter_implementation_version": connection.adapter_implementation_version,
        "config_schema_version": connection.config_schema_version,
        "provider_options": connection.provider_options,
        "model_profiles": connection.model_profiles,
        "endpoint_profile": endpoint,
        "secret": secret,
    }
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _view(connection: StoredConnection, *, has_credential: bool) -> dict:
    # Keep the vault envelope, endpoint, and internal revision out of both
    # account and Workspace responses.
    return {
        "id": connection.id,
        "name": connection.name,
        "adapter_id": connection.adapter_id,
        "adapter_implementation_version": connection.adapter_implementation_version,
        "config_schema_version": connection.config_schema_version,
        "api_profile": connection.api_profile,
        "endpoint_profile_id": connection.endpoint_profile.get("id"),
        "provider_options": connection.provider_options,
        "model_profiles": connection.model_profiles,
        "status": connection.status,
        "verification_status": connection.verification_status,
        "verified_at": connection.verified_at,
        "credential_updated_at": connection.credential_updated_at,
        "has_credential": has_credential,
        "revision": connection.revision,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }
