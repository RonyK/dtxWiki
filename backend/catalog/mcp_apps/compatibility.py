"""Compatibility policy over catalog variants, independent of app identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from packaging.version import InvalidVersion, Version

from backend.catalog.mcp_apps.models import (
    EndpointReachability,
    McpConnectionVariant,
    McpTransport,
    Platform,
    Shell,
)

type CompatibilityReasonCode = Literal[
    "catalog_app_unavailable",
    "transport_unavailable",
    "server_oauth_unavailable",
    "endpoint_unreachable",
    "platform_required",
    "platform_unsupported",
    "shell_required",
    "shell_unsupported",
    "app_version_required",
    "app_version_too_old",
]


@dataclass(frozen=True)
class ServerMcpCapabilities:
    oauth_available: bool
    transports: frozenset[McpTransport]
    endpoint_reachability: frozenset[EndpointReachability]


@dataclass(frozen=True)
class SetupContext:
    endpoint_reachability: EndpointReachability
    platform: Platform | None = None
    shell: Shell | None = None
    app_version: str | None = None


@dataclass(frozen=True)
class ConnectionOption:
    variant_id: str
    auth: str
    transport: str
    surface: str
    default_scope_preset: str
    available: bool
    reason_codes: tuple[CompatibilityReasonCode, ...]
    priority: int


class CompatibilityEvaluator:
    """Evaluate only closed capabilities; never authorize by catalog app ID."""

    def evaluate(
        self,
        variants: tuple[McpConnectionVariant, ...],
        server: ServerMcpCapabilities,
        context: SetupContext,
        *,
        lifecycle: str = "active",
    ) -> tuple[ConnectionOption, ...]:
        options = [
            self._evaluate_one(
                variant,
                server=server,
                context=context,
                lifecycle=lifecycle,
            )
            for variant in variants
        ]
        return tuple(sorted(options, key=lambda item: (item.priority, item.variant_id)))

    def _evaluate_one(
        self,
        variant: McpConnectionVariant,
        *,
        server: ServerMcpCapabilities,
        context: SetupContext,
        lifecycle: str,
    ) -> ConnectionOption:
        reasons: list[CompatibilityReasonCode] = []
        if lifecycle != "active":
            reasons.append("catalog_app_unavailable")
        if variant.transport not in server.transports:
            reasons.append("transport_unavailable")
        if variant.requires.server_oauth and not server.oauth_available:
            reasons.append("server_oauth_unavailable")
        if (
            context.endpoint_reachability not in variant.endpoint_reachability
            or context.endpoint_reachability not in server.endpoint_reachability
        ):
            reasons.append("endpoint_unreachable")
        if (
            variant.requires.platform is not None
            and context.platform not in variant.requires.platform
        ):
            reasons.append(
                "platform_required"
                if context.platform is None
                else "platform_unsupported"
            )
        if (
            variant.requires.shell is not None
            and context.shell not in variant.requires.shell
        ):
            reasons.append(
                "shell_required" if context.shell is None else "shell_unsupported"
            )
        if variant.requires.minimum_app_version is not None:
            if context.app_version is None:
                reasons.append("app_version_required")
            elif not _version_at_least(
                context.app_version,
                variant.requires.minimum_app_version,
            ):
                reasons.append("app_version_too_old")
        return ConnectionOption(
            variant_id=variant.id,
            auth=variant.auth,
            transport=variant.transport,
            surface=variant.surface,
            default_scope_preset=variant.default_scope_preset,
            available=not reasons,
            reason_codes=tuple(reasons),
            priority=variant.priority,
        )


def _version_at_least(actual: str, minimum: str) -> bool:
    try:
        return Version(actual) >= Version(minimum)
    except InvalidVersion:
        return False


__all__ = [
    "CompatibilityEvaluator",
    "CompatibilityReasonCode",
    "ConnectionOption",
    "ServerMcpCapabilities",
    "SetupContext",
]
