"""Deterministic, supply-chain-gated registry for Preview providers.

The registry stores descriptor snapshots only.  It does not discover Python
modules, import uploaded code, or turn Workspace configuration into executable
commands.  Optional subprocess/sidecar adapters must be composed by the worker
from instance-admin-approved artifacts before registration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from backend.spi.preview import (
    PREVIEW_OUTPUT_SCHEMA_VERSION,
    PreviewInputKind,
    PreviewProviderDescriptor,
    PreviewProviderPlugin,
    validate_descriptor_shape,
)

_PLACEHOLDER_DIGESTS = frozenset(
    {
        "0" * 64,
        "f" * 64,
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(b"placeholder").hexdigest(),
    }
)


class PreviewProviderRegistryError(ValueError):
    """Safe provider configuration error raised before execution."""


@dataclass(frozen=True, slots=True)
class PreviewProviderHealth:
    provider_id: str
    provider_version: str
    enabled: bool
    ready: bool
    reason_code: str | None
    formats: tuple[str, ...]
    input_kinds: tuple[str, ...]
    renderer_contracts: tuple[str, ...]
    execution_mode: str
    distribution_digest: str | None
    license_expression: str
    sbom_sha256: str | None
    signature_identity: str | None
    sandbox_profile: str | None


class PreviewProviderRegistry:
    """Validated provider collection with stable deterministic ordering."""

    def __init__(
        self,
        plugins: tuple[PreviewProviderPlugin, ...] = (),
        *,
        os_sandboxed: bool = False,
        network_allowed: bool = False,
    ) -> None:
        if type(os_sandboxed) is not bool or type(network_allowed) is not bool:
            raise PreviewProviderRegistryError("preview host policy is invalid")
        self._plugins: dict[str, PreviewProviderPlugin] = {}
        self._descriptors: dict[str, PreviewProviderDescriptor] = {}
        self._os_sandboxed = os_sandboxed
        self._network_allowed = network_allowed
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: PreviewProviderPlugin) -> None:
        if not all(
            callable(getattr(plugin, method, None))
            for method in ("descriptor", "probe", "generate")
        ):
            raise PreviewProviderRegistryError(
                "preview provider does not implement the SPI"
            )
        try:
            descriptor = plugin.descriptor()
            validate_descriptor_shape(descriptor)
            self._validate_supply_chain(descriptor)
        except PreviewProviderRegistryError:
            raise
        except Exception as exc:
            raise PreviewProviderRegistryError(
                "preview provider descriptor is invalid"
            ) from exc
        if descriptor.provider_id in self._plugins:
            raise PreviewProviderRegistryError("duplicate preview provider id")
        self._plugins[descriptor.provider_id] = plugin
        self._descriptors[descriptor.provider_id] = descriptor

    def descriptor(self, provider_id: str) -> PreviewProviderDescriptor:
        try:
            return self._descriptors[provider_id]
        except KeyError as exc:
            raise PreviewProviderRegistryError(
                "preview provider is not registered"
            ) from exc

    def plugin_for_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> PreviewProviderPlugin:
        provider_id = snapshot.get("provider_id")
        if type(provider_id) is not str:
            raise PreviewProviderRegistryError("preview provider snapshot is invalid")
        descriptor = self.descriptor(provider_id)
        expected = self._snapshot(descriptor)
        if snapshot != expected:
            raise PreviewProviderRegistryError(
                "preview provider installation changed after scheduling"
            )
        return self._plugins[provider_id]

    def health(self, provider_id: str) -> PreviewProviderHealth:
        descriptor = self.descriptor(provider_id)
        ready = True
        reason_code = None
        callback = getattr(self._plugins[provider_id], "health", None)
        if callable(callback):
            try:
                result = callback()
                if type(result) is not bool:
                    raise TypeError("health result must be a boolean")
                ready = result
                reason_code = None if ready else "provider_unhealthy"
            except Exception:
                ready = False
                reason_code = "provider_health_failed"
        return PreviewProviderHealth(
            provider_id=descriptor.provider_id,
            provider_version=descriptor.provider_version,
            enabled=True,
            ready=ready,
            reason_code=reason_code,
            formats=tuple(sorted(descriptor.formats)),
            input_kinds=tuple(sorted(descriptor.input_kinds)),
            renderer_contracts=tuple(sorted(descriptor.output_renderer_contracts)),
            execution_mode=descriptor.execution_mode,
            distribution_digest=descriptor.distribution_digest,
            license_expression=descriptor.license_expression,
            sbom_sha256=descriptor.sbom_sha256,
            signature_identity=descriptor.signature_identity,
            sandbox_profile=descriptor.sandbox_profile,
        )

    def provider_chain_snapshot(
        self,
        *,
        format_id: str,
        media_type: str,
        input_kind: PreviewInputKind,
        preferred_ids: tuple[str, ...] = (),
        ready_only: bool = True,
    ) -> list[dict[str, Any]]:
        preferred_rank = {
            provider_id: index for index, provider_id in enumerate(preferred_ids)
        }
        candidates = [
            descriptor
            for descriptor in self._descriptors.values()
            if format_id in descriptor.formats
            and media_type in descriptor.media_types
            and input_kind in descriptor.input_kinds
            and (not descriptor.network_required or self._network_allowed)
            and (not ready_only or self.health(descriptor.provider_id).ready)
        ]
        candidates.sort(
            key=lambda descriptor: (
                preferred_rank.get(descriptor.provider_id, len(preferred_rank)),
                -descriptor.priority,
                descriptor.provider_id,
                descriptor.provider_version,
            )
        )
        return [self._snapshot(descriptor) for descriptor in candidates]

    def catalog_health(self) -> tuple[PreviewProviderHealth, ...]:
        return tuple(self.health(provider_id) for provider_id in sorted(self._plugins))

    @staticmethod
    def _snapshot(descriptor: PreviewProviderDescriptor) -> dict[str, Any]:
        return {
            "provider_id": descriptor.provider_id,
            "provider_version": descriptor.provider_version,
            "spi_version": descriptor.spi_version,
            "priority": descriptor.priority,
            "execution_mode": descriptor.execution_mode,
            "network_required": descriptor.network_required,
            "deterministic": descriptor.deterministic,
            "license_expression": descriptor.license_expression,
            "distribution_digest": descriptor.distribution_digest,
            "sbom_sha256": descriptor.sbom_sha256,
            "signature_identity": descriptor.signature_identity,
            "sandbox_profile": descriptor.sandbox_profile,
            "max_input_bytes": descriptor.max_input_bytes,
            "output_schema_version": PREVIEW_OUTPUT_SCHEMA_VERSION,
            "renderer_contracts": sorted(descriptor.output_renderer_contracts),
            "output_media_types": sorted(descriptor.output_media_types),
        }

    def _validate_supply_chain(self, descriptor: PreviewProviderDescriptor) -> None:
        distribution_digest = descriptor.distribution_digest
        if (
            type(distribution_digest) is not str
            or len(distribution_digest) != 64
            or distribution_digest in _PLACEHOLDER_DIGESTS
        ):
            raise PreviewProviderRegistryError(
                "preview providers require a non-placeholder distribution digest"
            )
        if descriptor.execution_mode == "builtin":
            if descriptor.network_required:
                raise PreviewProviderRegistryError(
                    "builtin preview providers cannot require network"
                )
            return
        # Descriptor provenance and an operator sandbox flag are not an
        # execution boundary. Until Preview has a dedicated host protocol with
        # enforced timeout/process-tree termination, accepting an external
        # plugin would execute its Python object in the worker.
        raise PreviewProviderRegistryError(
            "external preview execution host is unavailable"
        )


def snapshot_hash(snapshot: object) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PreviewProviderHealth",
    "PreviewProviderRegistry",
    "PreviewProviderRegistryError",
    "snapshot_hash",
]
