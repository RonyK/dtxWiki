"""Schemas for the read-only admin Setup (deployment status) surface."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.schemas.common import RESTResponseBase
from backend.schemas.health import HealthComponents


class SuggestedCommand(BaseModel):
    """A copy/paste reconfiguration command for the admin Setup page.

    The web app cannot run these (it has no write access to ``/etc`` under the
    systemd sandbox); it surfaces the exact command for the operator to run.
    """

    title: str
    description: str
    command: str
    blast_radius: str

    model_config = ConfigDict(extra="forbid")


class OAuthLifetimeValue(BaseModel):
    seconds: int
    source: str

    model_config = ConfigDict(extra="forbid")


class SignupConfigurationStatus(BaseModel):
    configured: bool | None
    effective: bool
    source: Literal["explicit_environment", "mode_default"]
    mode_defaults: dict[str, bool]
    restart_required: bool
    impact: Literal["new_signups_only"]
    systemd_command: str
    compose_command: str

    model_config = ConfigDict(extra="forbid")


class OAuthSlidingValue(BaseModel):
    enabled: bool
    source: str

    model_config = ConfigDict(extra="forbid")


class OAuthTokenLifetimeReport(BaseModel):
    access_token: OAuthLifetimeValue
    refresh_token: OAuthLifetimeValue
    refresh_sliding: OAuthSlidingValue

    model_config = ConfigDict(extra="forbid")


class RecoveryConfigurationStatus(BaseModel):
    mode: Literal["email", "admin_assisted", "disabled"]
    admin_assisted: bool
    link_delivery_available: bool
    mail_provider: Literal["none", "smtp"]
    mail_status: Literal["configured", "not_configured", "incomplete"]
    mail_connection_status: Literal[
        "not_configured", "not_checked", "succeeded", "failed"
    ]
    mail_last_attempt_at: datetime | None
    mail_from_address: str | None
    smtp_credentials_configured: bool
    canonical_public_url: str | None
    https_ready: bool
    outbox_queued: int
    outbox_failed: int
    worker_required: bool
    restart_required: bool
    systemd_command: str
    compose_command: str

    model_config = ConfigDict(extra="forbid")


class DeploymentStatus(BaseModel):
    """Current, read-only deployment state derived from process settings."""

    public_base_url: str | None
    mcp_url: str | None
    app_mode: str
    app_env: str
    is_production: bool
    behind_proxy: bool
    https_configured: bool
    # Display-only TLS handling mode (acme|internal|custom|external|none).
    tls_mode: str
    # Whether MCP OAuth can be offered at all (https issuer); reason when not.
    oauth_available: bool
    oauth_unavailable_reason: str | None
    # MCP-10: advisory only -- DCR off does not make oauth_available False
    # (CIMD/URL-client-id authorization does not need DCR).
    oauth_dcr_enabled: bool
    oauth_token_lifetimes: OAuthTokenLifetimeReport
    health_ready: bool
    health_components: HealthComponents
    env_file_hint: str
    signup: SignupConfigurationStatus
    recovery: RecoveryConfigurationStatus
    suggested_commands: list[SuggestedCommand]
    # Update-available banner (cache-only, fail-open; see update_check_service).
    current_version: str | None = None
    latest_version: str | None = None
    update_available: bool = False

    model_config = ConfigDict(extra="forbid")


class DeploymentStatusResponse(RESTResponseBase):
    data: DeploymentStatus

    model_config = ConfigDict(extra="forbid")
