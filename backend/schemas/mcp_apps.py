"""Secret-free REST projections for the MCP application catalog."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.catalog.mcp_apps.compatibility import CompatibilityReasonCode
from backend.catalog.mcp_apps.models import (
    AppCommandArtifact,
    CommandArtifact,
    CopyMcpUrlArtifact,
    DeepLinkArtifact,
    InstructionArtifact,
    JsonConfigArtifact,
)
from backend.schemas.common import RESTResponseBase


class McpServerCapabilitiesData(BaseModel):
    """The MCP screen's single snapshot of what this server can offer.

    ``oauth_available`` means the configuration permits offering OAuth at
    ``mcp_url`` -- not that the origin was reached or its certificate checked.
    ``endpoint_reachability`` is likewise read off the configured scheme.
    """

    oauth_available: bool
    oauth_unavailable_reason: str | None = None
    oauth_dcr_enabled: bool
    # The canonical endpoint every surface must show and every recipe must
    # register: the issuer's origin, never the host the browser reached.
    mcp_url: str
    transports: list[Literal["streamable-http", "sse"]]
    endpoint_reachability: list[
        Literal["loopback", "private-network", "public-https"]
    ]

    model_config = ConfigDict(extra="forbid")


class McpConnectionOptionData(BaseModel):
    variant_id: str
    auth: Literal["oauth", "access-key"]
    transport: Literal["streamable-http", "sse"]
    surface: Literal["cli", "desktop", "ide", "web"]
    default_scope_preset: Literal["read-only", "agent-draft", "authoring"]
    available: bool
    reason_codes: list[CompatibilityReasonCode]
    priority: int

    model_config = ConfigDict(extra="forbid")


class McpAppSummaryData(BaseModel):
    id: str
    label: str
    aliases: list[str]
    icon_url: str
    vendor: str | None = None
    featured_rank: int | None = None
    lifecycle: Literal["active", "disabled"]
    verification_tier: Literal["verified", "community"]
    options: list[McpConnectionOptionData]

    model_config = ConfigDict(extra="forbid")


class McpGenericProfileData(BaseModel):
    id: str
    label: str
    icon_url: str
    options: list[McpConnectionOptionData]

    model_config = ConfigDict(extra="forbid")


class McpAppCatalogData(BaseModel):
    schema_major: Literal[1] = 1
    catalog_revision: str
    apps: list[McpAppSummaryData]
    generic_profiles: list[McpGenericProfileData]
    server_capabilities: McpServerCapabilitiesData

    model_config = ConfigDict(extra="forbid")


class McpAppCatalogResponse(RESTResponseBase):
    data: McpAppCatalogData

    model_config = ConfigDict(extra="forbid")


class McpSetupPlanRequest(BaseModel):
    variant_id: str = Field(min_length=1, max_length=128)
    platform: Literal["windows", "macos", "linux"] | None = None
    shell: Literal["powershell", "bash", "zsh"] | None = None
    app_version: str | None = Field(default=None, min_length=1, max_length=80)
    endpoint_reachability: Literal[
        "loopback", "private-network", "public-https"
    ] | None = None
    locale: Literal["en", "ko"] = "en"

    model_config = ConfigDict(extra="forbid")


class McpSetupCommandArtifactData(CommandArtifact):
    # Resolved by the backend from the validated executable registry. Consumers
    # never need a separate executable catalog or an app-ID command branch.
    command_name: str = Field(alias="commandName", min_length=1, max_length=128)


McpSetupArtifactData = Annotated[
    McpSetupCommandArtifactData
    | AppCommandArtifact
    | CopyMcpUrlArtifact
    | JsonConfigArtifact
    | DeepLinkArtifact
    | InstructionArtifact,
    Field(discriminator="kind"),
]


class McpSetupPlanData(BaseModel):
    catalog_revision: str
    owner_kind: Literal["app", "generic-profile"]
    owner_id: str
    variant_id: str
    recipe_id: str
    recipe_version: int = Field(ge=1)
    auth: Literal["oauth", "access-key"]
    transport: Literal["streamable-http", "sse"]
    surface: Literal["cli", "desktop", "ide", "web"]
    default_scope_preset: Literal["read-only", "agent-draft", "authoring"]
    mcp_url: str
    artifacts: list[McpSetupArtifactData]
    verification_instruction: str

    model_config = ConfigDict(extra="forbid")


class McpSetupPlanResponse(RESTResponseBase):
    data: McpSetupPlanData

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "McpAppCatalogResponse",
    "McpSetupPlanRequest",
    "McpSetupPlanResponse",
]
