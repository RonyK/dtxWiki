"""Shared deployment / reconfiguration metadata.

This module is the single source of truth describing *what each production
config key controls* and *the blast radius of changing it*. It is consumed by:

- the ``dtxwiki setup`` / ``dtxwiki config`` CLI (to warn before a change and to
  render the current configuration), and
- the admin read-only "Setup" API (to surface deployment state and generate
  ``dtxwiki config set ...`` commands the operator can copy/run).

The app process reads settings from the environment only (see
``backend.app.settings.Settings``) and runs under a systemd sandbox that cannot
write ``/etc`` — so the web surface never mutates configuration directly. It
only *describes* it and hands back the exact command to run. Keeping the
metadata here means the CLI and the API agree on impact wording.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from backend.app.settings import Settings, default_signup_enabled
from backend.policy.public_origin import PublicOrigin, resolve_public_origin

ENV_PREFIX = "DTXWIKI_"
SERVICE_ENV_PREFIX = "DTXWIKI_SERVICE_"

# Ordered from "safe" to "most destructive". The CLI prints the matching
# warning before applying a change; the API echoes it next to each suggested
# command so the operator sees the cost before running anything.
BlastRadius = Literal[
    "reload",  # reverse-proxy only; no app restart needed
    "restart",  # env is read once at process start — restart to apply
    "session_invalidation",  # all logged-in users are signed out
    "token_invalidation",  # all OAuth/MCP tokens must re-authorize
    "data_migration",  # data does not follow the URL — migrate first
]

BLAST_RADIUS_LABEL: dict[BlastRadius, str] = {
    "reload": "reverse-proxy reload",
    "restart": "service restart required",
    "session_invalidation": "all users signed out",
    "token_invalidation": "all OAuth/MCP clients must re-authenticate",
    "data_migration": "data migration required first",
}


@dataclass(frozen=True)
class ConfigKeySpec:
    """Describes one operator-facing production config key."""

    field: str
    env: str
    summary: str
    blast_radius: BlastRadius
    warning: str
    dangerous: bool = False


def field_to_env(field: str) -> str:
    """``public_base_url`` -> ``DTXWIKI_PUBLIC_BASE_URL`` (pydantic-settings rule)."""

    return f"{ENV_PREFIX}{field.upper()}"


def env_to_field(env: str) -> str | None:
    """Inverse of :func:`field_to_env`; ``None`` if not a DTXWIKI_ key."""

    if not env.startswith(ENV_PREFIX) or is_service_env_key(env):
        return None
    return env[len(ENV_PREFIX) :].lower()


def is_service_env_key(env: str) -> bool:
    """Return whether ``env`` belongs to the external service overlay namespace."""

    return env.startswith(SERVICE_ENV_PREFIX) and len(env) > len(SERVICE_ENV_PREFIX)


def valid_env_keys() -> set[str]:
    """Every env var name that maps onto a real ``Settings`` field."""

    return {field_to_env(name) for name in Settings.model_fields}


# Curated subset of Settings that operators actually reconfigure. Not every
# Settings field is here — only the ones with operational meaning + a known
# blast radius. Anything in Settings is still settable via `config set`, but
# only these get a tailored warning (others default to "restart").
_KEY_SPECS: tuple[ConfigKeySpec, ...] = (
    ConfigKeySpec(
        field="public_base_url",
        env="DTXWIKI_PUBLIC_BASE_URL",
        summary="Public HTTPS origin (OAuth/MCP metadata, cookie security).",
        blast_radius="restart",
        warning=(
            "Changing the public URL rewrites OAuth/MCP metadata. Reload the "
            "reverse proxy for the new cert/domain and re-register MCP clients "
            "that point at the old URL."
        ),
    ),
    ConfigKeySpec(
        field="cors_origins",
        env="DTXWIKI_CORS_ORIGINS",
        summary="Allowed browser origins (comma-separated).",
        blast_radius="restart",
        warning="Adjusts which browser origins may call the API.",
    ),
    ConfigKeySpec(
        field="session_secret",
        env="DTXWIKI_SESSION_SECRET",
        summary="Signing secret for login session cookies.",
        blast_radius="session_invalidation",
        warning="Rotating the session secret signs everyone out immediately.",
        dangerous=True,
    ),
    ConfigKeySpec(
        field="oauth_jwt_private_key_path",
        env="DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH",
        summary="Path to the RSA PEM that signs OAuth/MCP access tokens.",
        blast_radius="token_invalidation",
        warning=(
            "Pointing at a new key invalidates every issued OAuth/MCP token; "
            "all clients must re-authenticate. Personal API tokens are DB-based "
            "and unaffected."
        ),
        dangerous=True,
    ),
    ConfigKeySpec(
        field="database_url",
        env="DTXWIKI_DATABASE_URL",
        summary="SQLAlchemy database URL.",
        blast_radius="data_migration",
        warning=(
            "Changing the database URL does NOT move data. Run "
            "'dtxwiki migrate-data --from <old> --to <new>' first, or the new "
            "database starts empty."
        ),
        dangerous=True,
    ),
    ConfigKeySpec(
        field="backend_port",
        env="DTXWIKI_BACKEND_PORT",
        summary="Localhost port uvicorn binds (proxied; not internet-facing).",
        blast_radius="restart",
        warning=(
            "Keep the systemd ExecStart and reverse-proxy upstream in sync with "
            "this port."
        ),
    ),
    ConfigKeySpec(
        field="forwarded_allow_ips",
        env="DTXWIKI_FORWARDED_ALLOW_IPS",
        summary="Trusted reverse-proxy IPs for forwarded headers.",
        blast_radius="restart",
        warning="Must list the reverse proxy's IP(s); '*' is rejected in prod.",
    ),
    ConfigKeySpec(
        field="app_mode",
        env="DTXWIKI_APP_MODE",
        summary="Deployment mode: solo_local | solo_remote | team.",
        blast_radius="restart",
        warning="Switches single-user vs multi-user authentication behavior.",
    ),
    ConfigKeySpec(
        field="signup_enabled",
        env="DTXWIKI_SIGNUP_ENABLED",
        summary="Allow public self-service account registration.",
        blast_radius="restart",
        warning=(
            "Only new signup requests are affected. Existing sessions, users, "
            "and workspace memberships are unchanged."
        ),
    ),
    ConfigKeySpec(
        field="mail_provider",
        env="DTXWIKI_MAIL_PROVIDER",
        summary="Account recovery mail provider: none | smtp.",
        blast_radius="restart",
        warning="Enables verified-email recovery after the mail settings are complete.",
    ),
    ConfigKeySpec(
        field="smtp_password",
        env="DTXWIKI_SMTP_PASSWORD",
        summary="SMTP credential (always redacted from web status).",
        blast_radius="restart",
        warning="Changing the SMTP credential affects new deliveries and retries.",
        dangerous=True,
    ),
    ConfigKeySpec(
        field="mcp_http_mode",
        env="DTXWIKI_MCP_HTTP_MODE",
        summary="MCP HTTP transport: streamable | json_stateless | auto.",
        blast_radius="restart",
        warning="Affects how remote MCP clients connect.",
    ),
    ConfigKeySpec(
        field="app_name",
        env="DTXWIKI_APP_NAME",
        summary="Display name of the wiki.",
        blast_radius="restart",
        warning="Cosmetic; applied on next restart.",
    ),
)

KEY_SPECS: dict[str, ConfigKeySpec] = {spec.env: spec for spec in _KEY_SPECS}


def spec_for(env_key: str) -> ConfigKeySpec | None:
    """Tailored spec for ``env_key`` (``None`` if no tailored guidance)."""

    return KEY_SPECS.get(env_key.upper())


def blast_radius_for(env_key: str) -> BlastRadius:
    """Blast radius for any DTXWIKI_ key; defaults to ``restart``."""

    spec = spec_for(env_key)
    return spec.blast_radius if spec else "restart"


@dataclass(frozen=True)
class SuggestedCommand:
    """A ready-to-run reconfiguration command for the admin Setup page."""

    title: str
    description: str
    command: str
    blast_radius: BlastRadius


@dataclass(frozen=True)
class SignupConfiguration:
    configured: bool | None
    effective: bool
    source: Literal["explicit_environment", "mode_default"]
    mode_defaults: dict[str, bool]
    restart_required: bool
    impact: Literal["new_signups_only"]
    systemd_command: str
    compose_command: str


@dataclass(frozen=True)
class RecoveryConfiguration:
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


def signup_configuration(settings: Settings) -> SignupConfiguration:
    """Return redacted signup policy metadata and scoped change procedures."""

    target = not settings.effective_signup_enabled
    target_text = str(target).lower()
    return SignupConfiguration(
        configured=settings.signup_enabled,
        effective=settings.effective_signup_enabled,
        source=(
            "explicit_environment"
            if settings.signup_enabled is not None
            else "mode_default"
        ),
        mode_defaults={
            mode: default_signup_enabled(mode)
            for mode in ("solo_local", "solo_remote", "team")
        },
        restart_required=True,
        impact="new_signups_only",
        systemd_command=(
            "sudo dtxwiki config set "
            f"DTXWIKI_SIGNUP_ENABLED={target_text}\n"
            "sudo systemctl restart dtxwiki dtxwiki-worker"
        ),
        compose_command=(
            "# infra/prod/.env\n"
            f"DTXWIKI_SIGNUP_ENABLED={target_text}\n"
            "docker compose up -d --force-recreate app worker"
        ),
    )


def recovery_configuration(
    settings: Settings,
    *,
    outbox_queued: int = 0,
    outbox_failed: int = 0,
    latest_delivery_status: str | None = None,
    latest_delivery_attempt_at: datetime | None = None,
) -> RecoveryConfiguration:
    if settings.mail_provider == "none":
        mail_status = "not_configured"
    elif settings.mail_configured:
        mail_status = "configured"
    else:
        mail_status = "incomplete"
    if settings.mail_provider == "none":
        mail_connection_status = "not_configured"
    elif latest_delivery_status == "sent":
        mail_connection_status = "succeeded"
    elif latest_delivery_status == "failed":
        mail_connection_status = "failed"
    else:
        mail_connection_status = "not_checked"
    return RecoveryConfiguration(
        mode=settings.effective_password_recovery_mode,
        admin_assisted=settings.password_recovery_admin_assisted,
        link_delivery_available=settings.password_recovery_admin_assisted,
        mail_provider=settings.mail_provider,
        mail_status=mail_status,
        mail_connection_status=mail_connection_status,
        mail_last_attempt_at=latest_delivery_attempt_at,
        mail_from_address=settings.mail_from_address,
        smtp_credentials_configured=bool(
            settings.smtp_username and settings.smtp_password
        ),
        canonical_public_url=settings.public_base_url,
        https_ready=settings.canonical_https_ready,
        outbox_queued=outbox_queued,
        outbox_failed=outbox_failed,
        # The worker is an Identity & Recovery dependency only when an email
        # provider is selected. Administrator-assisted one-time links are
        # issued synchronously and do not enqueue mail, so reporting a required
        # delivery worker for the default ``mail_provider=none`` mode is
        # operationally misleading.
        worker_required=settings.mail_provider == "smtp",
        restart_required=True,
        systemd_command=(
            "sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com\n"
            "sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp\n"
            "sudo dtxwiki config set DTXWIKI_MAIL_FROM_ADDRESS=wiki@example.com\n"
            "sudo dtxwiki config set DTXWIKI_SMTP_HOST=smtp.example.com\n"
            "sudo dtxwiki config set DTXWIKI_SMTP_PORT=587\n"
            "sudo dtxwiki config set DTXWIKI_SMTP_USERNAME='<username>'\n"
            "sudo dtxwiki config set DTXWIKI_SMTP_PASSWORD='<secret>' --i-understand\n"
            "sudo systemctl restart dtxwiki dtxwiki-worker"
        ),
        compose_command=(
            "# infra/prod/.env\n"
            "DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com\n"
            "DTXWIKI_MAIL_PROVIDER=smtp\n"
            "DTXWIKI_MAIL_FROM_ADDRESS=wiki@example.com\n"
            "DTXWIKI_SMTP_HOST=smtp.example.com\n"
            "DTXWIKI_SMTP_PORT=587\n"
            "DTXWIKI_SMTP_USERNAME=<username>\n"
            "DTXWIKI_SMTP_PASSWORD=<secret>\n"
            "docker compose up -d --force-recreate app worker"
        ),
    )


def build_suggested_commands(settings: Settings) -> list[SuggestedCommand]:
    """Common Day-2 reconfiguration commands, contextualized to ``settings``.

    These are *examples* the operator copies and edits — the web app cannot run
    them (it has no write access to ``/etc``), so it hands back the command.
    """

    current_domain = domain_of(settings.public_base_url) or "wiki.example.com"
    suggestions = [
        SuggestedCommand(
            title="Change domain",
            description=(
                "Point the wiki at a new HTTPS domain. CORS follows the base URL "
                "automatically (set DTXWIKI_CORS_ORIGINS only for extra origins). "
                "Also update the reverse proxy + re-register MCP clients."
            ),
            command=(
                "sudo dtxwiki config set "
                "DTXWIKI_PUBLIC_BASE_URL=https://new.example.com"
            ),
            blast_radius="restart",
        ),
        SuggestedCommand(
            title="Re-run full setup",
            description=(
                "Re-apply all production config idempotently (keeps existing "
                "keys/secret), e.g. after editing several values."
            ),
            command=(
                "sudo dtxwiki setup production --reconfigure "
                f"--domain {shlex.quote(current_domain)}"
            ),
            blast_radius="restart",
        ),
        SuggestedCommand(
            title="Change TLS / certificate mode",
            description=(
                "Switch how this server gets HTTPS (acme|internal|custom|"
                "external|none). 'internal' issues from a local CA for an "
                "internal name/IP (no public domain); clients trust the exported "
                "root CA. Re-run setup to apply, then reload the reverse proxy."
            ),
            command=(
                "sudo dtxwiki setup production --reconfigure "
                f"--tls internal --domain {shlex.quote(current_domain)}"
            ),
            blast_radius="restart",
        ),
        SuggestedCommand(
            title="Rotate session secret",
            description="Sign everyone out and issue a fresh session secret.",
            command=(
                "sudo dtxwiki config set "
                "DTXWIKI_SESSION_SECRET=$(python3 -c "
                "'import secrets; print(secrets.token_urlsafe(64))') "
                "--i-understand"
            ),
            blast_radius="session_invalidation",
        ),
    ]
    return suggestions


def public_origin(settings: Settings) -> PublicOrigin:
    """The resolved public origin. One policy, no network I/O."""

    return resolve_public_origin(settings.public_base_url)


def https_configured(settings: Settings) -> bool:
    """The configured public origin is well-formed and uses https.

    Named for what it checks. It was ``tls_https_ready``, which read as "TLS
    has been verified end to end" while the implementation was
    ``url.startswith("https://")`` on a configuration string -- so a public URL
    nobody owns, or one that cannot resolve at all, reported ready. Whether the
    origin really answers over TLS is the post-setup diagnostic's job; see
    ``backend/policy/public_origin``.
    """

    return public_origin(settings).https_configured


def mcp_url(settings: Settings) -> str | None:
    """Public MCP endpoint derived from the configured public origin."""

    return public_origin(settings).mcp_url


def oauth_unavailable_reason(settings: Settings) -> str | None:
    """Why MCP OAuth cannot be offered, or ``None`` when it is available.

    OAuth needs a well-formed https public origin (the JWT issuer;
    standards-compliant clients refuse a non-https authorization server). DCR
    is NOT required: ``_resolve_authorization_client`` (oauth_service.py) never
    reads ``oauth_allow_dcr`` — a client using an https URL client identifier
    (CIMD) authorizes fine with DCR off. MCP-10: DCR used to be folded into
    this hard-unavailable reason, which put the whole OAuth surface (and the
    UI's "OAuth vs Access Key" recommendation) behind a flag that only
    actually blocks self-registering clients. See ``oauth_dcr_enabled``.
    """

    return public_origin(settings).message


def oauth_available(settings: Settings) -> bool:
    """The configuration permits offering OAuth at the canonical origin.

    Deliberately not "OAuth was proven to work": no DNS lookup, no TLS
    handshake, no certificate check happens on this path. See
    ``backend/policy/public_origin`` for why a capability read must not probe.
    """

    return public_origin(settings).oauth_available


def oauth_dcr_enabled(settings: Settings) -> bool:
    """True when MCP clients may self-register (DCR).

    A client using an https URL client identifier (CIMD) authorizes either
    way -- this is advisory (a UI hint), not a hard OAuth-availability gate.
    """

    return bool(settings.oauth_allow_dcr)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    authority = url.split("://", 1)[-1]
    return authority.split("/", 1)[0].split(":", 1)[0] or None
