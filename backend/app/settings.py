import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings

from backend.app.data_dir import data_dir, default_sqlite_url
from backend.app.version import package_version
from backend.policy.allowed_hosts import AllowedHostsError, effective_allowed_hosts

DEFAULT_BACKEND_PORT = 21080
DEFAULT_FRONTEND_DEV_PORT = 21173
PRODUCTION_ENVS = {"prod", "production"}
APP_MODES = {"solo_local", "solo_remote", "team"}
OAUTH_ACCESS_TOKEN_TTL_DEFAULT_SECONDS = 60 * 60
OAUTH_ACCESS_TOKEN_TTL_MIN_SECONDS = 60
OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS = 60 * 60 * 24
# MCP-05: restored to the pre-branch 30-day default. 66cfe175 introduced the
# ability to turn sliding OFF (a real feature) but silently widened the idle
# window a stale, unused connection stays resumable in along with it (30d ->
# 180d). Sliding renewal is unaffected -- an actively-used connection was
# always kept alive every rotation either way; this only bounds how long an
# UNUSED one stays alive. Operators still control the instance ceiling
# (oauth_refresh_token_ttl_seconds) directly, 1-365 days.
OAUTH_REFRESH_TOKEN_TTL_DEFAULT_SECONDS = 60 * 60 * 24 * 30
OAUTH_REFRESH_TOKEN_TTL_MIN_SECONDS = 60 * 60 * 24
OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS = 60 * 60 * 24 * 365
SESSION_TTL_DEFAULT_SECONDS = 60 * 60 * 12
SESSION_REMEMBERED_TTL_DEFAULT_SECONDS = 60 * 60 * 24 * 30
SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS = 60 * 60 * 24 * 7
SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS = 60 * 60 * 24 * 90
AUTH_REAUTHENTICATION_MAX_AGE_DEFAULT_SECONDS = 60 * 60
# Every request body is bounded before routing and authentication; the
# upload routes that legitimately need more carry their own, larger bounds
# (backend/api/middleware/request_body_limit.py).
DEFAULT_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

# Session secret (signs browser sessions, recent-reauthentication proofs,
# list cursors, MCP round state and the email outbox). solo_local installs
# keep a generated one in this file inside the data directory.
SESSION_SECRET_FILE_NAME = "session_secret"
MIN_REMOTE_SESSION_SECRET_LENGTH = 32
_SESSION_SECRET_FILE_MAX_BYTES = 64 * 1024
# SHA-256 of the development fallback that releases before 0.1.1 signed every
# unconfigured install's sessions with. That value is public, so it is refused
# wherever it is configured; only its digest is kept so the literal can never
# become a signing key again.
_RETIRED_DEFAULT_SESSION_SECRET_SHA256 = (
    "4a2c406a513a7d39768444571f948c649f89eeb2a93cdd1a219f53e09daf2440"
)
_SESSION_SECRET_GUIDANCE = (
    'Generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(64))"'
)

logger = logging.getLogger("dtxwiki.startup")

REMOVED_ENV_GUIDANCE = {
    "DTXWIKI_UPLOAD_WORKSPACE_QUOTA_BYTES": (
        "configure each workspace quota through the "
        "context_media_logical_quota_usage.limit_bytes ledger instead"
    ),
    "DTXWIKI_WORKSPACE_SLUG": (
        "workspace selection now comes from the authenticated user or request"
    ),
    "DTXWIKI_MCP_CLIENT_ID": (
        "MCP clients now connect to the authenticated /mcp endpoint"
    ),
    "DTXWIKI_MCP_LOG_LEVEL": (
        "MCP now uses the server application's logging configuration"
    ),
    "DTXWIKI_MCP_TOKEN": (
        "provide PAT or OAuth credentials to the remote MCP client, not the server"
    ),
    "DTXWIKI_WORKSPACE_TOKENS": (
        "each remote MCP client now authenticates directly with one PAT or OAuth grant"
    ),
    "DTXWIKI_DESKTOP_MCP_COMMAND": (
        "the desktop app now registers only authenticated HTTP MCP endpoints"
    ),
}
REMOVED_ENV_KEYS = frozenset(REMOVED_ENV_GUIDANCE)


@lru_cache
def _warn_removed_env(env_key: str) -> None:
    """Warn once per process and key when a retired env var is ignored."""

    logger.warning(
        "%s is no longer supported and is ignored; %s.",
        env_key,
        REMOVED_ENV_GUIDANCE[env_key],
    )


def default_signup_enabled(app_mode: str) -> bool:
    """Return the fail-closed self-signup default for a deployment mode.

    Local-only installs retain the zero-config onboarding flow. Any mode that
    can serve remote clients requires an explicit operator opt-in, including an
    unknown mode that will later be rejected by the startup validator.
    """

    return app_mode == "solo_local"


# Operational env vars read directly via os.environ by free functions OUTSIDE
# Settings (e.g. backend.app.data_dir.data_dir and
# backend.services.library_storage). They are NOT pydantic Settings fields but ARE
# legitimate in production, so the unknown-DTXWIKI_ typo guard (guard (h)) must
# not reject them. DTXWIKI_DEV_TOKEN is deliberately excluded — guard (f) rejects
# it on purpose.
OPERATIONAL_ENV_KEYS = frozenset(
    {
        "DTXWIKI_DATA_DIR",
        "DTXWIKI_LIBRARY_FILE_ROOTS",
    }
)


class Settings(BaseSettings):
    _plugin_registry: object | None = PrivateAttr(default=None)
    app_name: str = "dtxWiki"
    app_version: str = Field(default_factory=package_version)
    app_env: str = "development"
    app_mode: str = "solo_local"
    database_url: str = Field(default_factory=default_sqlite_url)
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_dev_port: int = DEFAULT_FRONTEND_DEV_PORT
    cors_origins: str | None = None
    auto_bootstrap: bool = True
    plugins: str = ""
    auth_providers_file: str | None = None
    auth_password_login_enabled: bool = True
    # Ordinary launches own their worker. Service-managed deployments select
    # external when their service manager starts the worker separately.
    worker_mode: Literal["managed", "external"] = "managed"
    # SPA asset directory override (R7′ assembly contract): lets an overlay
    # ship its own built frontend without patching every create_app call.
    static_root: str | None = None
    public_base_url: str | None = None
    # Host names (comma-separated, ports ignored) this deployment answers to
    # besides the loopback names and the DTXWIKI_PUBLIC_BASE_URL host. Every
    # other Host is refused, which is what stops DNS rebinding. "*" turns the
    # check off.
    allowed_hosts: str | None = None
    # Ceiling for any request body, enforced before routing and
    # authentication. Library and Context media uploads use their own
    # bounds derived from their upload caps.
    max_request_body_bytes: int = Field(
        default=DEFAULT_MAX_REQUEST_BODY_BYTES,
        ge=1024,
        le=4 * 1024 * 1024 * 1024,
    )
    session_secret: str | None = None
    # Read when DTXWIKI_SESSION_SECRET is empty: the file's stripped contents
    # are the secret. Lets every process in a container (including
    # `docker compose exec` maintenance commands) share one generated secret.
    session_secret_file: str | None = None
    # gt=0 matches the remembered fields below. SessionTokenCodec rejects a
    # non-positive ttl in __post_init__, but that runs inside the
    # get_session_service request dependency: without this constraint
    # DTXWIKI_SESSION_TTL_SECONDS=0 boots cleanly and then 500s every auth
    # request with no startup diagnostic.
    session_ttl_seconds: int = Field(
        default=SESSION_TTL_DEFAULT_SECONDS,
        gt=0,
    )
    session_remembered_ttl_seconds: int = Field(
        default=SESSION_REMEMBERED_TTL_DEFAULT_SECONDS,
        gt=0,
    )
    session_remembered_refresh_threshold_seconds: int = Field(
        default=SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS,
        gt=0,
    )
    session_remembered_absolute_ttl_seconds: int = Field(
        default=SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS,
        gt=0,
    )
    session_cookie_name: str = "dtxwiki_session"
    auth_reauthentication_max_age_seconds: int = Field(
        default=AUTH_REAUTHENTICATION_MAX_AGE_DEFAULT_SECONDS,
        gt=0,
    )
    session_cookie_secure: bool | None = None
    # Keep the raw operator value nullable so mode-derived defaults remain
    # distinguishable from an explicit true/false override.
    signup_enabled: bool | None = None
    # Identity & Recovery mail delivery. ``none`` keeps self-service recovery
    # hidden while the server-admin/CLI one-time-link path remains available
    # whenever a canonical HTTPS URL is configured.  SMTP credentials are
    # intentionally exposed only to the provider factory; deployment metadata
    # reports a redacted configured/not-configured state.
    mail_provider: Literal["none", "smtp"] = "none"
    mail_from_address: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    oauth_allow_dcr: bool = True
    oauth_access_token_ttl_seconds: int = OAUTH_ACCESS_TOKEN_TTL_DEFAULT_SECONDS
    oauth_refresh_token_ttl_seconds: int = OAUTH_REFRESH_TOKEN_TTL_DEFAULT_SECONDS
    oauth_refresh_token_sliding_enabled: bool = True
    oauth_jwt_private_key_pem: str | None = None
    oauth_jwt_private_key_path: str | None = None
    behind_proxy: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    # Honor X-Real-IP from a trusted proxy hop. Off by default: enable ONLY when
    # the proxy is known to OVERWRITE (never forward) the header — a proxy that
    # passes it through lets clients spoof the identity used for rate limits,
    # the metrics allowlist and audit logs. The shipped Caddy/nginx configs set
    # X-Forwarded-For as well, so the default still resolves the client IP.
    trust_x_real_ip: bool = False
    metrics_enabled: bool = False
    metrics_bearer_token: str | None = None
    metrics_allowlist: str | None = None
    force_security_headers: bool = False
    mcp_http_mode: Literal["json_stateless", "streamable", "auto"] = "streamable"
    mcp_preview_2026_07_28: bool = False
    llm_provider: str = "none"
    # Epic-1 RAG embedding selection (chosen at `dtxwiki setup`):
    #   none | deterministic | local | openai_compatible
    embedding_provider: str = "none"
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_dim: int = 0
    # Epic-1 Sprint-2 RAG reranking: an external cross-encoder rerank endpoint
    # (TEI / Infinity / vLLM /rerank, or a Cohere/Voyage rerank API). dtxWiki
    # only connects to it. Default none keeps retrieval ranking unchanged.
    #   none | api
    rerank_provider: str = "none"
    rerank_model: str | None = None
    rerank_base_url: str | None = None
    rerank_api_key: str | None = None
    rerank_candidate_k: int = 50
    rerank_top_n: int = 8
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # Lease after which a 'running' job is considered abandoned by its worker
    # and reclaimed (requeued or terminally failed) by the worker loop.
    worker_lease_timeout_seconds: int = 600
    # Library source blobs and converter execution.  External converter
    # manifests are instance-owned JSON, never Workspace/user input.  They stay
    # disabled unless an operator has supplied an OS sandbox boundary.
    library_blob_root: str | None = None
    library_blob_ingest_enabled: bool = True
    library_conversion_enabled: bool = True
    library_upload_hard_cap_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024 * 1024,
    )
    library_conversion_max_output_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        le=256 * 1024 * 1024,
    )
    library_conversion_max_asset_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        le=1024 * 1024 * 1024,
    )
    library_conversion_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=3600,
    )
    library_converter_manifests_json: str = "[]"
    library_converter_chains_json: str = "{}"
    library_converter_os_sandboxed: bool = False
    library_converter_network_allowed: bool = False
    lint_tick_interval_hours: int = Field(default=24, ge=0)

    trail_tick_interval_hours: int = Field(default=24, ge=0)
    librarian_tick_interval_days: int = Field(default=7, ge=0)
    search_associative_lane: bool = False

    # How long a Context media asset must sit as a derived orphan before the sweep
    # reclaims it. The retention clock runs BEFORE tombstoning -- the asset
    # stays active and rescuable (simply by being used) for this whole
    # window; nothing is at risk until it elapses.
    #
    # For the session-bound shape (uploaded in a chat, never claimed onto a
    # Page) this is a genuine cleanup delay. For the page-bound shape
    # (claimed onto a Page, never referenced by any version's Markdown --
    # currently the ONLY reachable page-bound orphan, see
    # backend/services/context_media_orphan_sweep.py's module docstring)
    # this setting IS the author's editing grace period, not a cleanup
    # delay: turning it down shortens how long an in-progress upload
    # survives before an author writes it into the page.
    #
    # 168h (7 days) mirrors librarian_tick_interval_days' week-scale
    # cadence -- long enough that a user who uploads an image and comes
    # back a few days later to finish the Page does not lose it, short
    # enough to keep storage from accumulating abandoned uploads
    # indefinitely. 0 disables reclamation (the derived orphan listing
    # still works; only the automatic sweep is gated).
    #
    # No separate "how often does the sweep check" setting: worker/tick.py
    # derives that cadence from this same value (bounded to [1h, 24h]) --
    # a day-scale retention window has no independent product decision
    # about check frequency, only "often enough not to matter" relative to
    # the window, and that is exactly the kind of extra knob this codebase
    # avoids introducing without a real reason to.
    context_media_orphan_retention_hours: int = Field(default=168, ge=0)

    # Self-update / update-notification (read by `dtxwiki update`/`info` and the
    # admin banner; not on the request hot path). Declared as real fields so the
    # DTXWIKI_* typo guard (guard (h)) accepts them and they surface in
    # `config show` / valid_env_keys(). update_channel is a free string
    # (interpreted by the update-check service: anything but "prerelease" == stable)
    # to avoid a Literal mismatch crashing startup on a stray value.
    no_update_notifier: bool = False
    update_index_url: str | None = None
    update_channel: str = "stable"
    update_keep_backups: int = 5

    # Internal-network / no-public-domain deployment knobs (written by
    # `dtxwiki setup production`). tls_mode is DISPLAY-ONLY metadata
    # (acme|internal|custom|external|none) so the deployment surface can show the
    # chosen mode + tailored reconfigure commands; it does not affect runtime
    # (Caddy owns TLS termination). allow_insecure_http is the single,
    # off-by-default switch that relaxes the https requirement (guard (a)) for
    # the eval-only plain-HTTP mode and is warned about loudly at startup. Both
    # are real fields so the DTXWIKI_* typo guard (h) accepts them.
    tls_mode: str = "acme"
    allow_insecure_http: bool = False

    model_config = ConfigDict(env_prefix="DTXWIKI_", extra="ignore")

    @model_validator(mode="after")
    def _validate_remembered_session_lifetimes(self):
        if (
            self.session_remembered_refresh_threshold_seconds
            >= self.session_remembered_ttl_seconds
        ):
            raise ValueError(
                "DTXWIKI_SESSION_REMEMBERED_REFRESH_THRESHOLD_SECONDS must be "
                "less than DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS"
            )
        if (
            self.session_remembered_ttl_seconds
            > self.session_remembered_absolute_ttl_seconds
        ):
            raise ValueError(
                "DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS must be less than or "
                "equal to DTXWIKI_SESSION_REMEMBERED_ABSOLUTE_TTL_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _require_secure_smtp_transport(self):
        if self.mail_provider == "smtp" and (self.smtp_starttls == self.smtp_use_ssl):
            raise ValueError(
                "SMTP requires exactly one secure transport mode: "
                "DTXWIKI_SMTP_STARTTLS or DTXWIKI_SMTP_USE_SSL"
            )
        return self

    @model_validator(mode="after")
    def _validate_library_converter_configuration(self):
        try:
            manifests = json.loads(self.library_converter_manifests_json)
            chains = json.loads(self.library_converter_chains_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Library converter manifests and chains must be valid JSON"
            ) from exc
        if not isinstance(manifests, list):
            raise ValueError("Library converter manifests must be a JSON list")
        if not isinstance(chains, dict):
            raise ValueError("Library converter chains must be a JSON object")
        if manifests and not self.library_converter_os_sandboxed:
            raise ValueError(
                "External Library converters require "
                "DTXWIKI_LIBRARY_CONVERTER_OS_SANDBOXED=true"
            )
        return self

    @field_validator("update_channel", mode="before")
    @classmethod
    def _normalize_update_channel(cls, value):
        # Codex #41 — ACCEPTED TRADEOFF (closed by decision), not a gap:
        # runtime env stays lenient (warn + coerce) so an operator's env typo can
        # never crash-loop production startup, while `dtxwiki config set` rejects
        # bad values strictly (prod_setup.validate_config_value) to catch typos on
        # the admin mutation path. update_channel / update_keep_backups are
        # operational-convenience settings (not security boundaries), so leniency
        # here matches the project's "an env typo must not kill prod" principle
        # (settings guard (h) / OPERATIONAL_ENV_KEYS). Do NOT convert these to a
        # strict Literal / ge=1 field: that would re-introduce the crash-loop.
        if value in (None, ""):
            return "stable"
        text = str(value).strip().lower()
        if text not in {"stable", "prerelease"}:
            import warnings

            warnings.warn(
                f"DTXWIKI_UPDATE_CHANNEL={value!r} is not 'stable' or "
                "'prerelease'; treating as 'stable'.",
                stacklevel=2,
            )
            return "stable"
        return text

    @field_validator("update_keep_backups", mode="before")
    @classmethod
    def _coerce_update_keep_backups(cls, value):
        if value is None or value == "":
            return 5
        try:
            count = int(value)
        except (TypeError, ValueError):
            import warnings

            warnings.warn(
                f"DTXWIKI_UPDATE_KEEP_BACKUPS={value!r} is not an integer; using 5.",
                stacklevel=2,
            )
            return 5
        if count < 1:
            import warnings

            warnings.warn(
                f"DTXWIKI_UPDATE_KEEP_BACKUPS={count} is < 1; using 1.",
                stacklevel=2,
            )
            return 1
        return count

    @field_validator("metrics_enabled", mode="before")
    @classmethod
    def _strict_metrics_flag(cls, value):
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            return False
        if value == 0 or value == "0":
            return False
        if value == 1 or value == "1":
            return True
        raise ValueError(
            "DTXWIKI_METRICS_ENABLED must be exactly '1' or '0'; "
            f"got {value!r} (true/yes/on not accepted)"
        )

    @field_validator("oauth_access_token_ttl_seconds", mode="before")
    @classmethod
    def _validate_oauth_access_token_ttl(cls, value):
        return _validated_oauth_ttl(
            value,
            env_key="DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS",
            minimum=OAUTH_ACCESS_TOKEN_TTL_MIN_SECONDS,
            maximum=OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS,
        )

    @field_validator("oauth_refresh_token_ttl_seconds", mode="before")
    @classmethod
    def _validate_oauth_refresh_token_ttl(cls, value):
        return _validated_oauth_ttl(
            value,
            env_key="DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS",
            minimum=OAUTH_REFRESH_TOKEN_TTL_MIN_SECONDS,
            maximum=OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS,
        )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in PRODUCTION_ENVS

    @property
    def is_remote_mode(self) -> bool:
        """True when the deployment is remote-shaped (serves non-local clients)."""
        return self.app_mode != "solo_local"

    @property
    def effective_signup_enabled(self) -> bool:
        """Resolve explicit signup configuration before the mode default."""

        if self.signup_enabled is not None:
            return self.signup_enabled
        return default_signup_enabled(self.app_mode)

    @property
    def canonical_https_ready(self) -> bool:
        value = (self.public_base_url or "").strip()
        if not value:
            return False
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)

    @property
    def mail_configured(self) -> bool:
        """Whether the configured provider can build recovery messages.

        This is deliberately a deterministic configuration check, not a
        network probe. Delivery health is tracked by the transactional outbox
        and worker status instead of making public capability reads contact an
        SMTP server.
        """

        if self.mail_provider != "smtp":
            return False
        return bool(
            self.smtp_host
            and self.mail_from_address
            and self.canonical_https_ready
            and self.smtp_starttls != self.smtp_use_ssl
        )

    @property
    def effective_password_recovery_mode(
        self,
    ) -> Literal["email", "admin_assisted", "disabled"]:
        if self.mail_configured:
            return "email"
        if self.canonical_https_ready:
            return "admin_assisted"
        return "disabled"

    @property
    def password_recovery_admin_assisted(self) -> bool:
        return self.canonical_https_ready

    @property
    def effective_session_cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        if self.public_base_url is not None:
            return self.public_base_url.lower().startswith("https://")
        return self.is_production

    @property
    def effective_session_secret(self) -> str:
        return self.resolved_session_secret.value

    @cached_property
    def resolved_session_secret(self) -> "ResolvedSessionSecret":
        """The signing secret, resolved once per Settings instance.

        Order: a non-empty DTXWIKI_SESSION_SECRET; else the stripped contents
        of DTXWIKI_SESSION_SECRET_FILE; else, only in solo_local, the
        ``session_secret`` file in the data directory, generated on first use.
        Remote modes with neither refuse to start. Every candidate passes
        :func:`session_secret_problem` first. Raises RuntimeError; a failure
        is not cached, so the next access retries.
        """

        resolved = _resolve_session_secret(self, create=True)
        assert resolved is not None
        return resolved

    @cached_property
    def effective_allowed_hosts(self) -> frozenset[str] | None:
        """Hosts requests may name, or ``None`` when DTXWIKI_ALLOWED_HOSTS=*."""

        try:
            return effective_allowed_hosts(
                configured=self.allowed_hosts,
                public_base_url=self.public_base_url,
            )
        except AllowedHostsError as exc:
            raise RuntimeError(f"DTXWIKI_ALLOWED_HOSTS invalid: {exc}") from exc

    @cached_property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins is None:
            # Production default: same-origin only (the app's own canonical URL),
            # so operators set the domain once via DTXWIKI_PUBLIC_BASE_URL and do
            # not repeat it here. Dev falls back to the local frontend/backend ports.
            if self.is_production and self.public_base_url:
                from urllib.parse import urlsplit

                parts = urlsplit(self.public_base_url)
                return [f"{parts.scheme}://{parts.netloc}"]
            return [
                f"http://127.0.0.1:{self.backend_port}",
                f"http://localhost:{self.backend_port}",
                f"http://127.0.0.1:{self.frontend_dev_port}",
                f"http://localhost:{self.frontend_dev_port}",
            ]
        return _parse_cors_origins(self.cors_origins)

    def validate_for_startup(self) -> None:
        from backend.app.plugins import load_plugins

        plugin_registry = load_plugins(self)
        for env_key in sorted(REMOVED_ENV_KEYS.intersection(os.environ)):
            _warn_removed_env(env_key)

        lifetimes = oauth_token_lifetime_report(self)
        logger.info(
            "OAuth token lifetimes: access_token_ttl_seconds=%s source=%s; "
            "refresh_token_ttl_seconds=%s source=%s; "
            "refresh_token_sliding_enabled=%s source=%s",
            lifetimes["access_token"]["seconds"],
            lifetimes["access_token"]["source"],
            lifetimes["refresh_token"]["seconds"],
            lifetimes["refresh_token"]["source"],
            lifetimes["refresh_sliding"]["enabled"],
            lifetimes["refresh_sliding"]["source"],
        )

        if self.app_mode not in APP_MODES:
            raise RuntimeError(
                "DTXWIKI_APP_MODE invalid: expected one of "
                f"{', '.join(sorted(APP_MODES))}"
            )

        self.validate_session_secret_invariant()

        if self.effective_allowed_hosts is None:
            logger.warning(
                "DTXWIKI_ALLOWED_HOSTS=* disables Host header validation: any "
                "host name that resolves to this server, including one an "
                "attacker controls (DNS rebinding), is answered."
            )

        origins = self.cors_origin_list
        if not origins:
            raise RuntimeError(
                "DTXWIKI_CORS_ORIGINS resolved to an empty list. "
                "Either unset the env var to use the default derived list, "
                "or set it to a comma-separated list of valid origins. "
                "An empty string is not allowed."
            )
        if "*" in origins:
            raise RuntimeError(
                "DTXWIKI_CORS_ORIGINS invalid: '*' is not allowed while CORS "
                "credentials are enabled"
            )

        if not self.is_production:
            # Cross-check app_mode vs app_env: a remote-shaped deployment left
            # at app_env=development silently skips every production guard
            # below (https public_base_url, HTTPS redirect, CORS hardening, …).
            # Warn loudly instead of failing so dev/e2e team-mode runs and the
            # blessed `dtxwiki setup production` path keep working.
            if self.is_remote_mode:
                import warnings

                warnings.warn(
                    f"DTXWIKI_APP_MODE={self.app_mode} with "
                    f"DTXWIKI_APP_ENV={self.app_env}: production guards "
                    "(HTTPS redirect, https public_base_url, CORS and metrics "
                    "invariants) are DISABLED. Set DTXWIKI_APP_ENV=production "
                    "for remote deployments.",
                    stacklevel=2,
                )
            return

        # Value-based production guards (a–e, g-value, i, j). Shared with
        # `dtxwiki config set` so a value that would break startup is rejected
        # before it is written, not only at the next restart.
        self.validate_production_invariants(check_key_file=True)

        # Loud, every-boot warning for the eval-only plain-HTTP opt-in. The guard
        # above already let http:// through because allow_insecure_http is set;
        # make sure the operator can never miss that TLS is off.
        if self.allow_insecure_http and (self.public_base_url or "").lower().startswith(
            "http://"
        ):
            import warnings

            warnings.warn(
                "INSECURE: TLS is disabled (DTXWIKI_TLS_MODE=none / "
                "DTXWIKI_ALLOW_INSECURE_HTTP=true). Session cookies, access keys "
                "and OAuth tokens travel in PLAINTEXT over the network. This is "
                "for throwaway LAN evaluation only — never for real multi-user "
                "data. Use an internal-CA (DTXWIKI_TLS_MODE=internal) deployment "
                "instead.",
                stacklevel=2,
            )

        # Guards that inspect the LIVE process env (os.environ), not config
        # values — these belong only to the running-process startup path.

        # guard (f) — DTXWIKI_DEV_TOKEN reserved env
        if os.environ.get("DTXWIKI_DEV_TOKEN") is not None:
            raise RuntimeError(
                "DTXWIKI_DEV_TOKEN is a reserved env name and must be unset "
                "in production"
            )

        # guard (g, raw) — metrics env must be exactly 0/1 (the value-based
        # protection check is in _validate_production_invariants)
        raw_metrics_enabled = os.environ.get("DTXWIKI_METRICS_ENABLED")
        if raw_metrics_enabled is not None and raw_metrics_enabled.strip() not in {
            "0",
            "1",
        }:
            raise RuntimeError(
                "DTXWIKI_METRICS_ENABLED must be set to 0 or 1 in production"
            )

        # guard (h) — unknown DTXWIKI_* env (typo detection). Operational env
        # vars read outside Settings (OPERATIONAL_ENV_KEYS) are legitimate and
        # excluded, otherwise e.g. DTXWIKI_DATA_DIR / DTXWIKI_LIBRARY_FILE_ROOTS
        # would crash a correctly-configured production server.
        _known_fields = {f"DTXWIKI_{k.upper()}" for k in Settings.model_fields}
        _env_dtxwiki = {
            k
            for k in os.environ
            if k.startswith("DTXWIKI_") and not k.startswith("DTXWIKI_SERVICE_")
        }
        _unknown = (
            _env_dtxwiki
            - _known_fields
            - OPERATIONAL_ENV_KEYS
            - REMOVED_ENV_KEYS
            - plugin_registry.environment_keys
        )
        if _unknown:
            raise RuntimeError(
                f"unknown DTXWIKI_ env detected: {', '.join(sorted(_unknown))}. "
                "Possible typo. Set DTXWIKI_APP_ENV=development to bypass."
            )

    def validate_session_secret_invariant(self) -> None:
        """Refuse an unusable session secret configuration.

        Unlike guard (b) this applies in EVERY app_env and mode: a solo_local
        install gets a generated secret in its data directory, while
        solo_remote/team must be given one, because they sign session cookies
        for remote clients. A published or placeholder value is refused
        everywhere since anyone could forge a session with it.

        Checks only; it never writes. The solo_local secret file is generated
        on first use of ``resolved_session_secret`` (create_app forces that at
        startup), so a skipped bootstrap leaves the data directory untouched.
        """
        _resolve_session_secret(self, create=False)

    def validate_production_invariants(self, *, check_key_file: bool = True) -> None:
        """Production guards derived purely from settings VALUES (no os.environ).

        Shared by :meth:`validate_for_startup` (live process) and
        ``dtxwiki config set`` pre-write validation. ``check_key_file`` is
        disabled by ``config set`` — it validates the value being written, not
        the signing key on disk.
        """
        import warnings

        # guard (a) — public_base_url. https is required unless the operator has
        # explicitly opted into the eval-only plain-HTTP mode
        # (DTXWIKI_ALLOW_INSECURE_HTTP=true); that opt-in is warned about loudly
        # in validate_for_startup.
        if not self.public_base_url:
            raise RuntimeError("DTXWIKI_PUBLIC_BASE_URL must be set")
        _url = self.public_base_url.lower()
        scheme_ok = _url.startswith("https://") or (
            self.allow_insecure_http and _url.startswith("http://")
        )
        if not scheme_ok:
            raise RuntimeError(
                "DTXWIKI_PUBLIC_BASE_URL must use https:// scheme "
                "(or set DTXWIKI_ALLOW_INSECURE_HTTP=true for eval-only plain HTTP)"
            )

        # guard (b) — production signs sessions with an operator-provided
        # secret (the value, or a file holding it), never the solo_local
        # generated one.
        inline_secret = (self.session_secret or "").strip()
        if not inline_secret and not (self.session_secret_file or "").strip():
            raise RuntimeError(
                "DTXWIKI_SESSION_SECRET (or DTXWIKI_SESSION_SECRET_FILE) must "
                "be set to a non-default value in production. "
                f"{_SESSION_SECRET_GUIDANCE}"
            )
        if inline_secret:
            problem = session_secret_problem(inline_secret, remote=self.is_remote_mode)
            if problem is not None:
                raise RuntimeError(
                    f"DTXWIKI_SESSION_SECRET {problem}. {_SESSION_SECRET_GUIDANCE}"
                )
        elif check_key_file:
            self.validate_session_secret_invariant()

        # guard (c) — cors must resolve to explicit, non-wildcard origins.
        # When DTXWIKI_CORS_ORIGINS is unset it derives from DTXWIKI_PUBLIC_BASE_URL
        # (same-origin), so operators do not repeat the domain. Set it explicitly
        # only to allow additional cross-origin browser clients.
        resolved_origins = self.cors_origin_list
        if not resolved_origins or "*" in resolved_origins:
            raise RuntimeError(
                "DTXWIKI_CORS_ORIGINS resolved to an empty list or wildcard. "
                "Leave it unset to default to DTXWIKI_PUBLIC_BASE_URL, or set an "
                "explicit comma-separated origin list without '*'."
            )

        local_origins = [o for o in resolved_origins if _is_local_origin(o)]
        if local_origins:
            raise RuntimeError(
                "DTXWIKI_CORS_ORIGINS must not include localhost or 127.0.0.1 "
                "when DTXWIKI_APP_ENV=production"
            )

        # guard (d) — forwarded_allow_ips wildcard
        if self.forwarded_allow_ips.strip() == "*":
            raise RuntimeError(
                "DTXWIKI_FORWARDED_ALLOW_IPS must list explicit proxy IPs in production"
            )

        # guard (e) — non-sqlite DB warning (Sprint_5 freeze)
        if not self.database_url.lower().startswith("sqlite"):
            warnings.warn(
                "Sprint_5 supports SQLite only; PG support reserved for "
                "Sprint_5 epilogue (ADR A14, items E1-E8). See "
                "DEPLOYMENT.md.",
                stacklevel=2,
            )

        # guard (g, value) — metrics enabled without protection
        if self.metrics_enabled and not (
            self.metrics_bearer_token or self.metrics_allowlist
        ):
            raise RuntimeError(
                "DTXWIKI_METRICS_ENABLED=1 requires "
                "DTXWIKI_METRICS_BEARER_TOKEN or DTXWIKI_METRICS_ALLOWLIST"
            )

        # guard (i) — JWT key path unset or unreadable
        if check_key_file:
            if not self.oauth_jwt_private_key_path:
                raise RuntimeError(
                    "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH must point to a readable "
                    "stable PEM file. "
                    "Generate with 'dtxwiki keygen --output /etc/dtxwiki/jwt.pem'."
                )
            try:
                from pathlib import Path

                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import rsa

                pem_data = Path(self.oauth_jwt_private_key_path).read_bytes()
                private_key = serialization.load_pem_private_key(
                    pem_data,
                    password=None,
                )
                if not isinstance(private_key, rsa.RSAPrivateKey):
                    raise ValueError("not an RSA private key")
            except Exception as exc:
                raise RuntimeError(
                    "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH is not readable or not a "
                    f"valid PEM: {exc}. "
                    "Generate with 'dtxwiki keygen --output /etc/dtxwiki/jwt.pem'."
                ) from exc

        # guard (j) — auto_bootstrap true
        if self.auto_bootstrap is True:
            raise RuntimeError(
                "DTXWIKI_AUTO_BOOTSTRAP must be false in production. "
                "Run 'dtxwiki bootstrap' once manually after first install."
            )


def _parse_cors_origins(raw_value: str) -> list[str]:
    value = raw_value.strip()
    if not value:
        return []
    try:
        if value.startswith("["):
            parsed = json.loads(value)
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError("JSON value must be a list of strings")
            origins = parsed
        else:
            origins = value.split(",")
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"DTXWIKI_CORS_ORIGINS invalid: {exc}") from exc
    return [origin.strip() for origin in origins if origin.strip()]


def _is_local_origin(origin: str) -> bool:
    return "://localhost" in origin or "://127.0.0.1" in origin


@dataclass(frozen=True)
class ResolvedSessionSecret:
    value: str
    source: Literal["environment", "file", "data_dir"]
    path: Path | None = None


def session_secret_problem(value: str, *, remote: bool) -> str | None:
    """Why ``value`` must not sign sessions, or ``None`` when it may.

    Refused in every mode: the retired built-in development secret and
    anything that still looks like a template placeholder (``<...>``,
    ``replace-with``, ``change-me``) -- all of them are public. Remote modes
    additionally require at least MIN_REMOTE_SESSION_SECRET_LENGTH characters.
    """

    candidate = value.strip()
    if not candidate:
        return "is empty"
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    if digest == _RETIRED_DEFAULT_SESSION_SECRET_SHA256:
        return "is the built-in development secret published by earlier releases"
    lowered = candidate.lower()
    if (
        (candidate.startswith("<") and candidate.endswith(">"))
        or "replace-with" in lowered
        or "change-me" in lowered
    ):
        return "is a template placeholder, not a secret"
    if remote and len(candidate) < MIN_REMOTE_SESSION_SECRET_LENGTH:
        return (
            f"is shorter than {MIN_REMOTE_SESSION_SECRET_LENGTH} characters, "
            "the minimum for remote modes"
        )
    return None


def _resolve_session_secret(
    settings: "Settings",
    *,
    create: bool,
) -> ResolvedSessionSecret | None:
    """Resolve (and validate) the session secret; see resolved_session_secret.

    With ``create=False`` an absent solo_local data-directory file is not
    generated and ``None`` is returned instead.
    """

    remote = settings.app_mode != "solo_local"

    configured = settings.session_secret
    if configured is not None and configured.strip():
        _require_usable_session_secret(
            configured, origin="DTXWIKI_SESSION_SECRET", remote=remote
        )
        return ResolvedSessionSecret(value=configured, source="environment")

    file_setting = (settings.session_secret_file or "").strip()
    if file_setting:
        path = Path(file_setting).expanduser()
        try:
            value = _read_session_secret_file(path)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"DTXWIKI_SESSION_SECRET_FILE points to {path}, which does not exist."
            ) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"DTXWIKI_SESSION_SECRET_FILE ({path}) cannot be used: {exc}"
            ) from exc
        _require_usable_session_secret(
            value,
            origin=f"DTXWIKI_SESSION_SECRET_FILE ({path})",
            remote=remote,
        )
        return ResolvedSessionSecret(value=value, source="file", path=path)

    if not remote:
        path = data_dir() / SESSION_SECRET_FILE_NAME
        value = _load_or_create_local_session_secret(path, create=create)
        if value is None:
            return None
        _require_usable_session_secret(value, origin=str(path), remote=False)
        return ResolvedSessionSecret(value=value, source="data_dir", path=path)

    raise RuntimeError(
        "DTXWIKI_SESSION_SECRET (or DTXWIKI_SESSION_SECRET_FILE) must be set "
        f"to a non-default value when DTXWIKI_APP_MODE={settings.app_mode}. "
        f"{_SESSION_SECRET_GUIDANCE}"
    )


def _require_usable_session_secret(value: str, *, origin: str, remote: bool) -> None:
    problem = session_secret_problem(value, remote=remote)
    if problem is not None:
        raise RuntimeError(
            f"{origin}: the session secret {problem}. {_SESSION_SECRET_GUIDANCE}"
        )


def _read_session_secret_file(path: Path) -> str:
    """Return the stripped secret stored in ``path``.

    FileNotFoundError propagates so the caller can tell "absent" from
    "unusable"; every other problem is an OSError or ValueError.
    """

    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("it is not a regular file")
    with path.open("rb") as handle:
        raw = handle.read(_SESSION_SECRET_FILE_MAX_BYTES + 1)
    if len(raw) > _SESSION_SECRET_FILE_MAX_BYTES:
        raise ValueError("it is larger than 64 KiB")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("it is not UTF-8 text") from exc
    if not value:
        raise ValueError("it is empty")
    return value


def _load_or_create_local_session_secret(path: Path, *, create: bool) -> str | None:
    """Read the solo_local data-directory secret, generating it once."""

    try:
        value = _read_session_secret_file(path)
    except FileNotFoundError:
        if not create:
            return None
        value = _publish_local_session_secret(path)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"The session secret file {path} cannot be used: {exc}. Delete it "
            "to have dtxWiki generate a new one (this signs every browser "
            "session out)."
        ) from exc
    if create:
        _restrict_to_owner(path)
    return value


def _publish_local_session_secret(path: Path) -> str:
    """Create ``path`` atomically with a fresh secret and return what it holds.

    The secret is written to a private temporary file and hard-linked into
    place, so no reader ever sees a partial file and a secret another process
    published first is never replaced: whichever link wins is the secret both
    processes go on to use.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(64)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{generated}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_to_owner(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this filesystem: exclusive creation still never
            # replaces a concurrently published secret.
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                with os.fdopen(os.open(path, flags, 0o600), "w") as handle:
                    handle.write(f"{generated}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                pass
    finally:
        temporary.unlink(missing_ok=True)
    return _read_session_secret_file(path)


def _restrict_to_owner(path: Path) -> None:
    """Best-effort: drop group/other access (0600); Windows keeps its ACLs."""

    if os.name == "nt":
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        logger.warning("could not restrict %s to mode 0600", path)


def _validated_oauth_ttl(
    value,
    *,
    env_key: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{env_key} must be an integer number of seconds between "
            f"{minimum} and {maximum}; got {value!r}"
        ) from exc
    if not minimum <= seconds <= maximum:
        raise ValueError(
            f"{env_key} must be between {minimum} and {maximum} seconds; got {seconds}"
        )
    return seconds


def oauth_token_lifetime_report(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, bool | int | str]]:
    """Return safe effective OAuth TTL values and their operator-facing source."""

    normalized_env_keys = {
        key.upper() for key in (os.environ if environ is None else environ)
    }

    def item(field_name: str) -> dict[str, int | str]:
        env_key = f"DTXWIKI_{field_name.upper()}"
        return {
            "seconds": int(getattr(settings, field_name)),
            "source": (
                "environment override"
                if env_key in normalized_env_keys
                else "built-in default"
            ),
        }

    return {
        "access_token": item("oauth_access_token_ttl_seconds"),
        "refresh_token": item("oauth_refresh_token_ttl_seconds"),
        "refresh_sliding": {
            "enabled": settings.oauth_refresh_token_sliding_enabled,
            "source": (
                "environment override"
                if "DTXWIKI_OAUTH_REFRESH_TOKEN_SLIDING_ENABLED" in normalized_env_keys
                else "built-in default"
            ),
        },
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
