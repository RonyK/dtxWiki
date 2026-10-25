"""Public login adapter contracts. Core owns accounts, state, and sessions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

AUTH_METHOD_SPI_VERSION = "1.0"


class AuthMethodError(RuntimeError):
    """An adapter could not validate an IdP response (never show its text)."""


@dataclass(frozen=True)
class MethodCapabilities:
    pkce: Literal["required", "supported", "unsupported"] = "required"
    id_token: Literal["signed", "none"] = "signed"
    nonce: Literal["supported", "unsupported"] = "supported"
    reauthentication: bool = False


@dataclass(frozen=True)
class Branding:
    label: Mapping[str, str] = field(default_factory=lambda: {"en": "Continue"})
    icon: bytes | None = None
    background: str | None = None
    foreground: str | None = None


@dataclass(frozen=True)
class AuthMethodDescriptor:
    method_id: str
    protocol: str = "oidc"
    interaction: Literal["redirect", "credential"] = "redirect"
    spi_version: str = AUTH_METHOD_SPI_VERSION
    capabilities: MethodCapabilities = field(default_factory=MethodCapabilities)
    branding: Branding = field(default_factory=Branding)
    config_schema: Mapping = field(default_factory=lambda: {"type": "object"})


@dataclass(frozen=True)
class ProviderContext:
    slug: str
    config: Mapping
    secrets: Callable[[str], str]
    callback_url: str


@dataclass(frozen=True)
class BeginRequest:
    purpose: Literal["login", "link", "reauth"]
    state: str
    nonce: str | None = None
    code_challenge: str | None = None
    force_reauthentication: bool = False


@dataclass(frozen=True)
class RedirectInstruction:
    url: str
    http_method: Literal["GET", "POST"] = "GET"
    form_fields: Mapping[str, str] = field(default_factory=dict)
    protocol_request_id: str | None = None


@dataclass(frozen=True)
class CallbackInput:
    http_method: str
    query: Mapping[str, str]
    form: Mapping[str, str] = field(default_factory=dict)
    expected_nonce: str | None = None
    pkce_verifier: str | None = None
    protocol_request_id: str | None = None


@dataclass(frozen=True)
class ExternalAssertion:
    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    authenticated_at: datetime | None = None
    amr: tuple[str, ...] = ()
    idp_session_id: str | None = None
    groups: tuple[str, ...] = ()
    nonce_verified: bool = False
    # Only a tenant established by the adapter's verified IdP evidence or
    # bounded server configuration belongs here. Core compares it with the
    # operator's directory policy before exposing company membership.
    tenant_id: str | None = None


class AuthMethodPlugin(Protocol):
    descriptor: AuthMethodDescriptor

    def validate_config(self, raw: Mapping) -> Mapping: ...

    def begin(
        self, provider: ProviderContext, request: BeginRequest
    ) -> RedirectInstruction: ...

    def complete(
        self, provider: ProviderContext, callback: CallbackInput
    ) -> ExternalAssertion: ...


@dataclass(frozen=True)
class LoginContext:
    user_id: UUID
    identity_id: UUID
    provider: ProviderContext
    assertion: ExternalAssertion
    created: bool


@dataclass(frozen=True)
class WorkspaceRoleRequest:
    workspace_id: UUID
    role: Literal["viewer", "editor"]


@dataclass(frozen=True)
class ProvisioningResult:
    """Add-only requests; core validates and applies them in its transaction.

    Existing different roles remain unchanged. Operator allowlists bound every
    request; plugins cannot request ownership, server administration or removal.
    """

    workspace_roles: tuple[WorkspaceRoleRequest, ...] = ()


class ProvisioningHook(Protocol):
    def on_login(self, context: LoginContext) -> ProvisioningResult: ...


class IdentityLifecycleHook(Protocol):
    """Post-commit notifications; failures do not undo core identity changes."""

    def on_identity_linked(self, provider_slug: str, identity_id: UUID) -> None: ...

    def on_identity_unlinked(self, provider_slug: str, identity_id: UUID) -> None: ...
