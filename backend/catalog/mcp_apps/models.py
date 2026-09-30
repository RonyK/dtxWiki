"""Closed vocabulary for the MCP application catalog.

The catalog is deliberately data-driven only inside these reviewed unions.  A
new application may compose existing variants and artifacts without changing
production code; a new authentication, transport, secret sink, or executable
mode requires an explicit schema and policy change.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CatalogModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


CatalogId = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
McpAuthMethod = Literal["oauth", "access-key"]
McpTransport = Literal["streamable-http", "sse"]
EndpointReachability = Literal["loopback", "private-network", "public-https"]
McpSurface = Literal["cli", "desktop", "ide", "web"]
ScopePreset = Literal["read-only", "agent-draft", "authoring"]
Platform = Literal["windows", "macos", "linux"]
Shell = Literal["powershell", "bash", "zsh"]
SetupStep = Literal["register", "authenticate"]
LocalConfigAdapterId = Literal[
    "antigravity-json",
    "claude-json",
    "codex-toml",
    "gemini-json",
]


class LocalizedText(CatalogModel):
    en: str = Field(min_length=1, max_length=160)
    ko: str = Field(min_length=1, max_length=160)

    @field_validator("en", "ko")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("localized text must not be blank")
        return value.strip()


class CatalogIcon(CatalogModel):
    asset_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["image/svg+xml", "image/png"]


class ExecutableDefinition(CatalogModel):
    id: CatalogId
    command_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    invocation: Literal["direct-argv"]


class VerifiedOAuthIdentity(CatalogModel):
    kind: Literal["exact-registration-profile"]
    normalized_metadata_fingerprint: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    provenance_ref: str = Field(min_length=1, max_length=300)


class VerificationProvenance(CatalogModel):
    source_url: str = Field(min_length=1, max_length=500)
    checked_at: str = Field(min_length=10, max_length=40)


class AppVerification(CatalogModel):
    tier: Literal["verified", "community"]
    verified_at: str | None = None
    provenance: tuple[VerificationProvenance, ...]


class VariantRequirements(CatalogModel):
    server_oauth: Literal[True] | None = Field(default=None, alias="serverOAuth")
    platform: tuple[Platform, ...] | None = None
    shell: tuple[Shell, ...] | None = None
    minimum_app_version: str | None = None


class McpConnectionVariant(CatalogModel):
    id: CatalogId
    surface: McpSurface
    transport: McpTransport
    auth: McpAuthMethod
    endpoint_reachability: tuple[EndpointReachability, ...] = Field(min_length=1)
    requires: VariantRequirements = Field(default_factory=VariantRequirements)
    default_scope_preset: ScopePreset
    recipe_ids: tuple[CatalogId, ...] = Field(min_length=1)
    priority: int = Field(ge=0, le=10_000)


class LiteralAtom(CatalogModel):
    literal: str


class McpUrlAtom(CatalogModel):
    slot: Literal["mcp-url"]


class ServerNameAtom(CatalogModel):
    slot: Literal["server-name"]


class AccessTokenAtom(CatalogModel):
    slot: Literal[
        "access-token",
        "access-token-env",
        "authorization-bearer",
    ]


PublicTemplateAtom = LiteralAtom | McpUrlAtom | ServerNameAtom
SecretTemplateAtom = LiteralAtom | McpUrlAtom | ServerNameAtom | AccessTokenAtom


class SetupArtifactBase(CatalogModel):
    """Every artifact declares which setup step it belongs to.

    Registration and authentication are separate acts for every reviewed
    client: ``claude mcp add`` only writes configuration, and the sign-in
    that follows is a second command (or an in-app prompt). Carrying the
    step in the vocabulary keeps the surface from presenting an unlabelled
    run of "setup commands" that hides the login step.
    """

    step: SetupStep = "register"


class CopyMcpUrlArtifact(SetupArtifactBase):
    kind: Literal["copy-mcp-url"]
    label: LocalizedText
    value: tuple[PublicTemplateAtom, ...] = Field(min_length=1)


class CommandArtifact(SetupArtifactBase):
    kind: Literal["command"]
    executable_id: CatalogId
    argv: tuple[tuple[SecretTemplateAtom, ...], ...]
    env: dict[str, tuple[SecretTemplateAtom, ...]] | None = None


class JsonConfigArtifact(SetupArtifactBase):
    kind: Literal["json-config"]
    label: LocalizedText
    # Pydantic validates the outer catalog shape; CatalogRepository recursively
    # validates this JSON-only tree and its closed template atoms so no arbitrary
    # Python value or unknown slot can cross the setup boundary.
    value: Any


class DeepLinkArtifact(SetupArtifactBase):
    kind: Literal["deep-link"]
    label: LocalizedText
    url: tuple[PublicTemplateAtom, ...] = Field(min_length=1)


class AppCommandArtifact(SetupArtifactBase):
    """A command typed inside the AI app itself, such as ``/mcp auth``.

    Some clients run their OAuth flow only from their own prompt, so the
    step has no shell command to copy. Rendering it as a shell command
    would be a lie, and burying it in prose is what hid the login step in
    the first place, so it is its own kind. The value is public-only: an
    in-app prompt is not an approved credential sink, and the repository
    additionally pins it to slash-command form so this kind can never be
    used to smuggle a shell invocation past the executable registry.
    """

    kind: Literal["app-command"]
    value: tuple[PublicTemplateAtom, ...] = Field(min_length=1)


class InstructionArtifact(SetupArtifactBase):
    kind: Literal["instruction"]
    body: LocalizedText


SetupArtifact = Annotated[
    CopyMcpUrlArtifact
    | CommandArtifact
    | AppCommandArtifact
    | JsonConfigArtifact
    | DeepLinkArtifact
    | InstructionArtifact,
    Field(discriminator="kind"),
]


class RecipeSelectors(CatalogModel):
    platform: Platform | None = None
    shell: Shell | None = None
    app_version: str | None = None


class RecipeVerification(CatalogModel):
    kind: Literal["first-mcp-call"]
    instruction: LocalizedText


class McpSetupRecipe(CatalogModel):
    id: CatalogId
    version: int = Field(ge=1)
    variant_id: CatalogId
    selectors: RecipeSelectors = Field(default_factory=RecipeSelectors)
    artifacts: tuple[SetupArtifact, ...] = Field(min_length=1)
    verify: RecipeVerification


class McpAppDefinition(CatalogModel):
    id: CatalogId
    labels: LocalizedText
    aliases: tuple[str, ...] = ()
    icon: CatalogIcon
    vendor: str | None = None
    runtime_brand: str | None = Field(default=None, max_length=80)
    local_config_adapter_id: LocalConfigAdapterId | None = None
    featured_rank: int | None = Field(default=None, ge=1)
    lifecycle: Literal["active", "disabled", "tombstone"]
    verification: AppVerification
    variants: tuple[McpConnectionVariant, ...] = Field(min_length=1)
    oauth_identities: tuple[VerifiedOAuthIdentity, ...] | None = None


class McpGenericProfile(CatalogModel):
    id: CatalogId
    labels: LocalizedText
    icon: CatalogIcon
    variants: tuple[McpConnectionVariant, ...] = Field(min_length=1)


class McpAppCatalogV1(CatalogModel):
    schema_major: Literal[1]
    catalog_revision: str = Field(min_length=1, max_length=80)
    apps: tuple[McpAppDefinition, ...]
    generic_profiles: tuple[McpGenericProfile, ...]
    executables: tuple[ExecutableDefinition, ...]
    recipes: tuple[McpSetupRecipe, ...]


__all__ = [
    "AccessTokenAtom",
    "AppCommandArtifact",
    "CatalogIcon",
    "CommandArtifact",
    "CopyMcpUrlArtifact",
    "DeepLinkArtifact",
    "EndpointReachability",
    "ExecutableDefinition",
    "InstructionArtifact",
    "JsonConfigArtifact",
    "LiteralAtom",
    "LocalConfigAdapterId",
    "LocalizedText",
    "McpAppCatalogV1",
    "McpAppDefinition",
    "McpAuthMethod",
    "McpConnectionVariant",
    "McpGenericProfile",
    "McpSetupRecipe",
    "McpSurface",
    "McpTransport",
    "McpUrlAtom",
    "Platform",
    "ScopePreset",
    "ServerNameAtom",
    "SetupArtifact",
    "SetupArtifactBase",
    "SetupStep",
    "Shell",
]
