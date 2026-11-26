"""Resolve a registered LLM connection for one authorized Ask execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from backend.llm.adapter_registry import LLMAdapterRegistry, LLMAdapterRegistryError
from backend.llm.credential_vault import (
    ConnectionScope,
    CredentialBinding,
    CredentialVaultError,
)
from backend.llm.streaming import supports_native_steer
from backend.llm.transport import HttpxLLMTransport
from backend.services.llm_connection_service import (
    LLMConnectionError,
    LLMConnectionService,
)
from backend.services.settings_service import llm_custom_endpoints_enabled
from backend.spi.llm import LLMModelCapabilities, LLMTransport, TextGenerationPort
from backend.spi.types import ActorContext

if TYPE_CHECKING:
    from backend.schemas.ask import AskModelSelection


@dataclass(frozen=True)
class AskGenerationBinding:
    """A request-local port with an identity that is safe to store with history."""

    port: TextGenerationPort = field(repr=False)
    target: dict[str, str]
    model: str
    max_output_tokens: int
    context_window: int | None
    steer: bool


class AskConnectionService:
    def __init__(
        self,
        connections: LLMConnectionService,
        registry: LLMAdapterRegistry,
        *,
        transport_factory: Callable[[], LLMTransport] = HttpxLLMTransport,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._transport_factory = transport_factory

    def list_choices(
        self,
        control_factory: sessionmaker[Session],
        workspace_factory: sessionmaker[Session],
        workspace_id: UUID,
        actor: ActorContext,
    ) -> list[dict]:
        choices = []
        scopes: list[tuple[str, ConnectionScope, UUID, sessionmaker[Session]]] = [
            ("workspace", "workspace", workspace_id, workspace_factory)
        ]
        if actor.user_id is not None:
            scopes.append(("personal", "user", actor.user_id, control_factory))
        for scope, stored_scope, owner_id, factory in scopes:
            with factory() as session:
                views = self._connections.list(
                    session,
                    scope=stored_scope,
                    owner_id=owner_id,
                    custom_endpoints=_custom_endpoints_allowed(
                        control_factory, workspace_id, stored_scope
                    ),
                )
                for view in views:
                    if (
                        view["status"] != "enabled"
                        or view["verification_status"] != "ready"
                        or view["unavailable_reason"] is not None
                    ):
                        continue
                    # Only native adapter defaults can advertise live steering.
                    # The management projection already checked this exact URL.
                    adapter_id = view["adapter_id"]
                    base_url = (
                        self._registry.descriptor(adapter_id).default_base_url or ""
                        if view["endpoint_profile_id"] is None
                        and view["endpoint_url"] is None
                        else ""
                    )
                    for profile in view["model_profiles"]:
                        choices.append(
                            {
                                "scope": scope,
                                "connection_id": view["id"],
                                "model_profile_id": profile["id"],
                                "connection_label": view["name"],
                                "model_label": profile.get("display_name")
                                or profile["model_id"],
                                "steer": supports_native_steer(
                                    adapter_id, base_url, profile["model_id"]
                                ),
                            }
                        )
        return choices

    def resolve(
        self,
        control_factory: sessionmaker[Session],
        workspace_factory: sessionmaker[Session],
        workspace_id: UUID,
        actor: ActorContext,
        selection: AskModelSelection,
    ) -> AskGenerationBinding:
        if selection.scope == "personal":
            if actor.user_id is None:
                raise _unavailable()
            scope, owner_id, factory = "user", actor.user_id, control_factory
        else:
            scope, owner_id, factory = "workspace", workspace_id, workspace_factory
        custom_endpoints = _custom_endpoints_allowed(
            control_factory, workspace_id, scope
        )
        with factory() as session:
            connection = self._connections.owned(
                session,
                scope=scope,
                owner_id=owner_id,
                connection_id=selection.connection_id,
            )
            if (
                connection.status != "enabled"
                or connection.verification_status != "ready"
            ):
                raise _unavailable()
            endpoint = connection.endpoint_profile
            if code := self._connections.unavailable(
                connection, endpoint, custom_endpoints=custom_endpoints
            ):
                raise LLMConnectionError(code, "The Ask model is unavailable.", 409)
            profile = next(
                (
                    item
                    for item in connection.model_profiles
                    if item["id"] == selection.model_profile_id
                ),
                None,
            )
            if profile is None:
                raise _unavailable()
            try:
                # A connection verified without a key (a local server) runs
                # without one; the adapter's schema decides what it needs.
                credential = (
                    self._connections.vault.decrypt(
                        connection.active_credential,
                        binding=CredentialBinding(
                            scope, owner_id, connection.id, connection.secret_revision
                        ),
                    )
                    if connection.active_credential is not None
                    else {}
                )
            except CredentialVaultError:
                raise LLMConnectionError(
                    "credential_unavailable", "The Ask credential is unavailable.", 409
                ) from None
            adapter_id = connection.adapter_id
            base_url = endpoint["base_url"]
            config = dict(connection.provider_options)
            target = {
                "kind": selection.scope,
                "connection_id": str(connection.id),
                "adapter_id": adapter_id,
                "model_profile_id": profile["id"],
                "model_id": profile["model_id"],
                "endpoint": sha256(base_url.encode()).hexdigest(),
                "secret_revision": str(connection.secret_revision),
            }
        model = profile["model_id"]
        try:
            port = self._registry.create(
                adapter_id,
                base_url=base_url,
                config=config,
                credential=credential,
                model=model,
                capabilities=LLMModelCapabilities(
                    max_output_tokens=profile.get("max_output_tokens"),
                    options=frozenset(profile.get("options", ())),
                ),
                transport=self._transport_factory(),
            )
        except (LLMAdapterRegistryError, ValueError):
            raise _unavailable() from None
        return AskGenerationBinding(
            port=port,
            target=target,
            model=model,
            max_output_tokens=min(1024, profile.get("max_output_tokens") or 1024),
            context_window=profile.get("context_window"),
            steer=(
                endpoint["kind"] == "adapter_default"
                and supports_native_steer(adapter_id, base_url, model)
            ),
        )


def _custom_endpoints_allowed(
    control_factory: sessionmaker[Session], workspace_id: UUID, scope: ConnectionScope
) -> bool:
    """A person's own connections may use any address; the Workspace's switch
    governs only the Workspace's connections."""

    if scope == "user":
        return True
    with control_factory() as control:
        return llm_custom_endpoints_enabled(control, workspace_id)


def _unavailable() -> LLMConnectionError:
    return LLMConnectionError(
        "connection_unavailable", "The Ask model is unavailable.", 409
    )
