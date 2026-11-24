"""Verify LLM connections with a tiny synthetic request.

Verification runs inside the request that asks for it. Each model profile gets
one small generation, all at once and each bounded by a timeout, and the
outcome is recorded before the response returns. No database transaction is
held while a provider answers. Two rules:

* An outcome is recorded only while the connection is still the revision
  that was checked. An edit or a key change in between wins with a revision
  conflict, whether or not the check passed; a deletion makes the connection
  unavailable.
* A replacement credential is checked before it is stored. Only one that
  verifies replaces the active credential; a refused one is never saved.

Access is checked before the provider is called and again before the result
is written, since the call can take a while.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from backend.llm import transport as llm_transport
from backend.llm.adapter_registry import LLMAdapterRegistry, LLMAdapterRegistryError
from backend.llm.credential_vault import (
    ConnectionScope,
    CredentialBinding,
    CredentialVault,
    CredentialVaultError,
)
from backend.schemas.llm_connection import LLMCredentialReplace
from backend.services.llm_connection_service import (
    LLMConnectionError,
    LLMConnectionService,
)
from backend.spi.llm import (
    CanonicalTextRequest,
    ExecutionContext,
    LLMAdapterError,
    LLMModelCapabilities,
    LLMTransport,
    TextMessage,
)
from backend.spi.types import ActorContext

VERIFICATION_TIMEOUT_SECONDS = 30.0
# Enough for any model to show it answers; nothing in the reply is used.
VERIFICATION_OUTPUT_TOKENS = 16
_PROMPT = "Reply with OK."
# A model's outcome, worst first: one refusal fails the whole check, and a
# call whose delivery is unknown makes the whole outcome unknown.
_SEVERITY = ("failed", "outcome_unknown", "succeeded")


@dataclass(frozen=True)
class _Target:
    """What one verification sends."""

    adapter_id: str
    base_url: str
    config: dict
    profiles: list[dict]
    credential: dict[str, str]


class LLMVerificationService:
    def __init__(
        self,
        *,
        connections: LLMConnectionService,
        registry: LLMAdapterRegistry,
        vault: CredentialVault,
        transport_factory: Callable[[], LLMTransport] | None = None,
        timeout_seconds: float = VERIFICATION_TIMEOUT_SECONDS,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._vault = vault
        self._transport_factory = transport_factory
        self._timeout_seconds = timeout_seconds

    def verify(
        self,
        plane: sessionmaker[Session],
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        expected_revision: int,
        before_write: Callable[[], None] = lambda: None,
    ) -> tuple[dict, dict]:
        """Check the stored setup and record whether it works.

        Returns the connection and the result. An outcome the provider did
        not confirm (``outcome_unknown``) leaves the recorded status as is.
        """
        with plane() as session:
            connection = self._connections.owned(
                session, scope=scope, owner_id=owner_id, connection_id=connection_id
            )
            if connection.revision != expected_revision:
                raise LLMConnectionError(
                    "connection_revision_conflict",
                    "LLM connection revision changed.",
                    409,
                )
            endpoint = connection.endpoint_profile
            code = self._unavailable(connection, endpoint)
            credential: dict[str, str] = {}
            if code is None and connection.active_credential is not None:
                try:
                    credential = self._vault.decrypt(
                        connection.active_credential,
                        binding=CredentialBinding(
                            scope, owner_id, connection.id, connection.secret_revision
                        ),
                    )
                except CredentialVaultError:
                    code = "credential_unavailable"
            target = _target(connection, endpoint, credential)
        result = _failed(code) if code is not None else self._check(target)
        before_write()
        with plane.begin() as session:
            view = self._connections.record_verification(
                session,
                scope=scope,
                owner_id=owner_id,
                actor=actor,
                connection_id=connection_id,
                revision=expected_revision,
                status=result["status"],
            )
        if view is None:
            raise _changed()
        return view, result

    def replace_credential(
        self,
        plane: sessionmaker[Session],
        *,
        scope: ConnectionScope,
        owner_id: UUID,
        actor: ActorContext,
        connection_id: UUID,
        request: LLMCredentialReplace,
        before_write: Callable[[], None] = lambda: None,
    ) -> tuple[dict, dict]:
        """Verify a new credential (and endpoint), then store it if it works."""
        credential = {
            name: value.get_secret_value() for name, value in request.credential.items()
        }
        with plane() as session:
            connection, endpoint = self._connections.candidate(
                session,
                scope=scope,
                owner_id=owner_id,
                connection_id=connection_id,
                request=request,
                credential=credential,
            )
            code = self._unavailable(connection, endpoint)
            target = _target(connection, endpoint, credential)
        result = _failed(code) if code is not None else self._check(target)
        before_write()
        with plane.begin() as session:
            if result["status"] == "succeeded":
                view = self._connections.swap_credential(
                    session,
                    scope=scope,
                    owner_id=owner_id,
                    actor=actor,
                    connection_id=connection_id,
                    revision=request.expected_revision,
                    credential=credential,
                    endpoint=endpoint,
                )
                if view is None:
                    raise _changed()
            else:
                view = self._connections.get(
                    session,
                    scope=scope,
                    owner_id=owner_id,
                    connection_id=connection_id,
                )
                # Nothing was stored, but a client retrying from this answer
                # must not adopt an edit it has not seen.
                if view["revision"] != request.expected_revision:
                    raise _changed()
        return view, result

    def _unavailable(self, connection, endpoint: dict) -> str | None:
        try:
            descriptor = self._registry.descriptor(connection.adapter_id)
        except LLMAdapterRegistryError:
            return "adapter_unavailable"
        if descriptor.config_schema_version != connection.config_schema_version:
            return "adapter_version_unavailable"
        if not self._connections.endpoint_allowed(connection.adapter_id, endpoint):
            return "endpoint_unavailable"
        return None

    def _check(self, target: _Target) -> dict:
        results = asyncio.run(self._check_all(target))
        worst = min(results, key=lambda item: _SEVERITY.index(item["outcome"]))
        return {
            "status": worst["outcome"],
            "error_code": worst["error_code"],
            "results": results,
        }

    async def _check_all(self, target: _Target) -> list[dict]:
        return list(
            await asyncio.gather(
                *(self._check_model(target, profile) for profile in target.profiles)
            )
        )

    async def _check_model(self, target: _Target, profile: dict) -> dict:
        result = {"model_profile_id": profile["id"]}
        limit = profile.get("max_output_tokens")
        transport = (self._transport_factory or llm_transport.HttpxLLMTransport)()
        try:
            port = self._registry.create(
                target.adapter_id,
                base_url=target.base_url,
                config=target.config,
                credential=target.credential,
                model=profile["model_id"],
                capabilities=LLMModelCapabilities(
                    max_output_tokens=limit,
                    options=frozenset(profile.get("options", ())),
                ),
                transport=transport,
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
        context = ExecutionContext(timeout_seconds=self._timeout_seconds)
        try:
            # The context bounds each network step; this bounds the call.
            reply = await asyncio.wait_for(
                port.generate(request, context), timeout=self._timeout_seconds
            )
        except TimeoutError:
            return {**result, "outcome": "outcome_unknown", "error_code": "timeout"}
        except LLMAdapterError as exc:
            outcome = "outcome_unknown" if exc.delivery == "unknown" else "failed"
            return {**result, "outcome": outcome, "error_code": exc.code}
        # A truncated reply still proves the model answers this key.
        if reply.termination not in ("completed", "truncated"):
            return {**result, "outcome": "failed", "error_code": "unexpected_result"}
        return {**result, "outcome": "succeeded", "error_code": None}


def _target(connection, endpoint: dict, credential: dict[str, str]) -> _Target:
    return _Target(
        adapter_id=connection.adapter_id,
        base_url=endpoint["base_url"],
        config=dict(connection.provider_options),
        profiles=list(connection.model_profiles),
        credential=credential,
    )


def _failed(code: str) -> dict:
    return {"status": "failed", "error_code": code, "results": []}


def _changed() -> LLMConnectionError:
    return LLMConnectionError(
        "connection_revision_conflict",
        "LLM connection changed during verification; nothing was saved.",
        409,
    )


__all__ = ["LLMVerificationService"]
