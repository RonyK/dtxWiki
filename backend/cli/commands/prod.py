"""Production provisioning + Day-2 config commands (`dtxwiki setup` / `config`).

Extracted from the monolithic cli/main.py (R3). main.py registers ``setup_app``
and ``config_app`` from this module.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from sqlalchemy import select

from backend.app.bootstrap import bootstrap as bootstrap_app
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings, default_signup_enabled
from backend.cli import prod_setup
from backend.models.user import User
from backend.services import deployment, server_admin_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.ops import service_control
from backend.services.session_service import (
    DuplicateEmailError,
    EmailInputPolicyError,
    normalize_email,
)
from backend.services.user_admin_service import UserAdminService
from backend.utils.redact import redact_url

setup_app = typer.Typer(help="Provision dtxWiki for production.")
config_app = typer.Typer(help="View and change production configuration.")

# Env keys whose values must never be echoed in 'config show'.
_SECRET_ENV_KEYS = {
    "DTXWIKI_SESSION_SECRET",
    "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PEM",
    "DTXWIKI_METRICS_BEARER_TOKEN",
    "DTXWIKI_ANTHROPIC_API_KEY",
    "DTXWIKI_OPENAI_API_KEY",
    "DTXWIKI_EMBEDDING_API_KEY",
    "DTXWIKI_SMTP_PASSWORD",
}


def _display_value(key: str, value: str) -> str:
    if key in _SECRET_ENV_KEYS:
        return "****"
    if key == "DTXWIKI_DATABASE_URL":
        # Mask only the embedded credentials; the operator still needs to see
        # the host/db (so a full "****" would hide too much).
        return redact_url(value)
    return value


@config_app.command(name="show")
def config_show(
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="Production env file path."),
    ] = prod_setup.DEFAULT_ENV_FILE,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show production config from the env file (secrets redacted)."""
    values = prod_setup.read_env_file(env_file)
    last_applied = None
    if env_file.exists():
        last_applied = (
            datetime.fromtimestamp(env_file.stat().st_mtime, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    entries = []
    for key in sorted(values):
        spec = deployment.spec_for(key)
        entries.append({
            "key": key,
            "value": _display_value(key, values[key]),
            "blast_radius": spec.blast_radius if spec else None,
            "summary": spec.summary if spec else None,
        })
    if json_output:
        typer.echo(json.dumps(
            {
                "env_file": str(env_file),
                "last_applied": last_applied,
                "entries": entries,
            },
            indent=2,
        ))
        return
    if not env_file.exists():
        typer.echo(
            f"No config file at {env_file}. "
            "Run 'dtxwiki setup production' first."
        )
        raise typer.Exit(1)
    typer.echo(f"config file:  {env_file}")
    typer.echo(f"last applied: {last_applied}")
    typer.echo("")
    for entry in entries:
        line = f"  {entry['key']}={entry['value']}"
        if entry["blast_radius"]:
            line += f"   (change impact: {entry['blast_radius']})"
        typer.echo(line)


@config_app.command(name="set")
def config_set(
    assignment: Annotated[
        str,
        typer.Argument(
            help="KEY=VALUE, e.g. DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com",
        ),
    ],
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="Production env file path."),
    ] = prod_setup.DEFAULT_ENV_FILE,
    caddyfile: Annotated[
        Path,
        typer.Option(
            "--caddyfile",
            help="Keep this Caddyfile's domain in sync on PUBLIC_BASE_URL change.",
        ),
    ] = prod_setup.DEFAULT_CADDYFILE,
    i_understand: bool = typer.Option(
        False,
        "--i-understand",
        help="Required for changes that sign users out / invalidate tokens.",
    ),
) -> None:
    """Set one production config value (atomic, backed up, with impact warning)."""
    try:
        key, value = prod_setup.parse_assignment(assignment)
        prod_setup.validate_env_key(key)
        # Validate the VALUE before touching the source-of-truth env file, so a
        # bad value (e.g. BACKEND_PORT=abc, APP_MODE=banana) is rejected up front
        # instead of only failing at the next service restart.
        prod_setup.validate_config_value(key, value)
        # Also reject values that pass type-checking but would fail the PRODUCTION
        # startup guards (e.g. AUTO_BOOTSTRAP=true, FORWARDED_ALLOW_IPS=*), so the
        # failure surfaces now rather than on the next restart.
        prod_setup.validate_production_config(env_file, key, value)
    except ValueError as exc:
        typer.echo(f"config set failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    spec = deployment.spec_for(key)
    blast = spec.blast_radius if spec else "restart"
    if spec and spec.dangerous and not i_understand:
        typer.echo(
            f"REFUSED: {key} is destructive "
            f"({deployment.BLAST_RADIUS_LABEL[blast]}).",
            err=True,
        )
        typer.echo(f"  {spec.warning}", err=True)
        typer.echo("  Re-run with --i-understand to proceed.", err=True)
        raise typer.Exit(3)

    old = prod_setup.get_env_value(env_file, key)
    prod_setup.set_env_values(env_file, {key: value})

    caddy_synced = False
    if key == "DTXWIKI_PUBLIC_BASE_URL" and caddyfile.exists():
        new_domain = deployment.domain_of(value)
        old_domain = deployment.domain_of(old)
        if new_domain and old_domain and old_domain != new_domain:
            text = caddyfile.read_text(encoding="utf-8")
            if old_domain in text:
                prod_setup.write_env_text(
                    caddyfile, text.replace(old_domain, new_domain)
                )
                caddy_synced = True

    typer.echo(f"set {key} ({'was unset' if old is None else 'updated'})")
    typer.echo(f"impact: {deployment.BLAST_RADIUS_LABEL[blast]}")
    if spec:
        typer.echo(f"  {spec.warning}")
    if blast != "reload":
        typer.echo("  apply: sudo systemctl restart dtxwiki dtxwiki-worker")
    if key == "DTXWIKI_PUBLIC_BASE_URL":
        typer.echo("  reverse proxy: sudo systemctl reload-or-restart caddy")
    if caddy_synced:
        typer.echo(f"  Caddyfile domain synced: {old_domain} -> {new_domain}")


TLS_MODES = ("acme", "internal", "custom", "external", "none")
# Detailed, English help shown in the interactive flat menu (CLI i18n is out of
# scope — keep the guidance thorough so the operator can self-select correctly).
TLS_MODE_HELP = {
    "acme": (
        "Public domain + automatic Let's Encrypt. Caddy obtains and renews the "
        "cert for you. Needs: a DNS A record pointing here + ports 80/443 open. "
        "WARNING: if another proxy already uses 80/443, or you are attaching a "
        "subdomain behind an EXISTING proxy, pick 'external' to avoid cert/port "
        "conflicts."
    ),
    "internal": (
        "No public domain (internal network). Caddy issues from its OWN local CA "
        "for an internal hostname (e.g. wiki.lan) or IP. Every client (browsers, "
        "MCP clients) must trust the exported root CA once — this command prints "
        "the exact per-OS steps after setup."
    ),
    "custom": (
        "Bring your own certificate (corporate PKI / mkcert / openssl IP-SAN). "
        "Pass --tls-cert <path> --tls-key <path>; Caddy terminates TLS with them. "
        "Best for Docker+IP, where 'internal' IP-SAN issuance is finicky."
    ),
    "external": (
        "An existing reverse proxy / load balancer / ingress already terminates "
        "TLS (e.g. a subdomain of your real domain). dtxWiki issues NO certificate "
        "and does not run Caddy; point your proxy at 127.0.0.1:21080 with "
        "X-Forwarded-* headers. The correct choice when you already have a domain."
    ),
    "none": (
        "Plain HTTP, NO TLS. EVAL ONLY — session cookies, access keys and OAuth "
        "tokens travel in PLAINTEXT, and team OAuth will not work. Never for real "
        "multi-user data; use 'internal' instead."
    ),
}


def _resolve_tls_mode(
    flag: str | None,
    existing: dict[str, str],
    *,
    dry_run: bool,
    insecure_eval: bool,
) -> str:
    """Resolve the TLS mode from the flag, the existing env, or an interactive
    menu. Confirms the eval-only 'none' mode explicitly."""

    mode = flag or existing.get("DTXWIKI_TLS_MODE")
    # Only prompt a real interactive terminal; non-interactive callers (tests,
    # install.sh, CI) must pass --tls and otherwise default to the historical
    # acme behaviour.
    interactive = not dry_run and sys.stdin.isatty()
    if mode is None and interactive:
        typer.echo("\nTLS / certificate handling — how should this server get HTTPS?")
        for key in TLS_MODES:
            typer.echo(f"  - {key}: {TLS_MODE_HELP[key]}")
        mode = typer.prompt("\nTLS mode", default="acme").strip()
    mode = (mode or "acme").strip()
    if mode not in TLS_MODES:
        typer.echo(
            f"setup failed: unknown --tls mode '{mode}' "
            f"(expected one of: {', '.join(TLS_MODES)}).",
            err=True,
        )
        raise typer.Exit(2)
    if mode == "none" and not insecure_eval and not dry_run:
        if not interactive:
            typer.echo(
                "setup failed: 'none' (plain HTTP) is eval-only and must be "
                "confirmed with --insecure-eval when run non-interactively.",
                err=True,
            )
            raise typer.Exit(2)
        confirm = typer.prompt(
            "\n!!! PLAIN HTTP (no TLS) is EVALUATION ONLY — auth travels in "
            "plaintext and team OAuth will not work. Type INSECURE to proceed",
            default="",
        ).strip()
        if confirm != "INSECURE":
            typer.echo("aborted: 'none' (plain HTTP) mode was not confirmed.", err=True)
            raise typer.Exit(2)
    return mode


# Everything `setup production` loads through prod_setup.share_file. An installed
# wheel provides these from its data-files (pyproject [tool.setuptools.data-files]);
# tests/packaging checks that each one resolves there, not from a checkout.
_CADDY_TEMPLATE = {
    "acme": "infra/prod/Caddyfile.example",
    "internal": "infra/prod/Caddyfile.internal.example",
    "custom": "infra/prod/Caddyfile.custom.example",
    "none": "infra/prod/Caddyfile.insecure.example",
}
_SYSTEMD_UNITS = {
    unit: f"infra/prod/{unit}"
    for unit in (prod_setup.BACKEND_UNIT, prod_setup.WORKER_UNIT)
}
_SMOKE_SCRIPT = "tests/packaging/production_smoke.sh"


def _print_internal_ca_guidance(domain: str) -> None:
    """Export Caddy's internal root CA and print the per-OS client-trust steps.
    The app cannot reach into client machines, so the CLI's responsibility is to
    hand the operator the exact commands."""

    dest = Path("/etc/dtxwiki/dtxwiki-root-ca.crt")
    exported = prod_setup.export_caddy_root_ca(dest)
    typer.echo("  Internal-CA HTTPS configured. Every client must trust the root CA:")
    if exported is None:
        typer.echo(
            "    Caddy creates the root CA on first request — after opening "
            f"https://{domain} once, export it with:\n"
            f"      sudo cp {prod_setup.DEFAULT_CADDY_ROOT_CA} ./dtxwiki-root-ca.crt"
        )
    else:
        typer.echo(f"    Exported root CA -> {exported}")
    typer.echo(
        "    Copy that file to each client and install it once:\n"
        "      Linux:   sudo cp dtxwiki-root-ca.crt /usr/local/share/ca-certificates/ "
        "&& sudo update-ca-certificates\n"
        "      macOS:   sudo security add-trusted-cert -d -r trustRoot "
        "-k /Library/Keychains/System.keychain dtxwiki-root-ca.crt\n"
        "      Windows: Import-Certificate -FilePath dtxwiki-root-ca.crt "
        "-CertStoreLocation Cert:\\LocalMachine\\Root\n"
        "      Firefox: Settings -> Privacy & Security -> Certificates -> "
        "Authorities -> Import\n"
        "      MCP clients (Claude Desktop/Code, Node/Electron): set "
        "NODE_EXTRA_CA_CERTS=/path/dtxwiki-root-ca.crt\n"
        "      curl / python:  CURL_CA_BUNDLE / SSL_CERT_FILE=/path/dtxwiki-root-ca.crt"
    )


@setup_app.command(name="production")
def setup_production(
    domain: Annotated[
        str | None,
        typer.Option("--domain", help="Public HTTPS domain, e.g. wiki.example.com"),
    ] = None,
    email: Annotated[
        str | None,
        typer.Option("--email", help="Initial admin (owner) email."),
    ] = None,
    env_file: Annotated[
        Path, typer.Option("--env-file", help="Production env file path.")
    ] = prod_setup.DEFAULT_ENV_FILE,
    jwt_key: Annotated[
        Path, typer.Option("--jwt-key", help="OAuth JWT signing key path.")
    ] = prod_setup.DEFAULT_JWT_KEY,
    caddyfile: Annotated[
        Path, typer.Option("--caddyfile", help="Caddyfile output path.")
    ] = prod_setup.DEFAULT_CADDYFILE,
    database_url: Annotated[
        str, typer.Option("--database-url", help="SQLAlchemy database URL.")
    ] = prod_setup.DEFAULT_DATABASE_URL,
    service_user: Annotated[
        str,
        typer.Option(
            "--service-user",
            help="System account the service runs as (for JWT key group-read).",
        ),
    ] = "dtxwiki",
    reconfigure: bool = typer.Option(
        False,
        "--reconfigure",
        help="Re-apply config idempotently; keep existing keys/secret.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without changing anything."
    ),
    skip_preflight: bool = typer.Option(False, "--skip-preflight"),
    systemd: bool = typer.Option(
        True, "--systemd/--no-systemd", help="Install + enable systemd units."
    ),
    caddy: bool = typer.Option(
        True, "--caddy/--no-caddy", help="Write Caddyfile + reload."
    ),
    do_bootstrap: bool = typer.Option(True, "--bootstrap/--no-bootstrap"),
    do_admin: bool = typer.Option(
        True, "--admin/--no-admin", help="Create the initial owner user."
    ),
    smoke: bool = typer.Option(
        True, "--smoke/--no-smoke", help="Run production smoke after start."
    ),
    tls: Annotated[
        str | None,
        typer.Option(
            "--tls",
            help="TLS mode: acme|internal|custom|external|none (prompted if omitted).",
        ),
    ] = None,
    tls_cert: Annotated[
        Path | None,
        typer.Option("--tls-cert", help="Certificate file path (custom mode)."),
    ] = None,
    tls_key: Annotated[
        Path | None,
        typer.Option("--tls-key", help="Private key file path (custom mode)."),
    ] = None,
    insecure_eval: bool = typer.Option(
        False,
        "--insecure-eval",
        help="Confirm the eval-only plain-HTTP ('none') mode non-interactively.",
    ),
    forwarded_ip: Annotated[
        str | None,
        typer.Option(
            "--forwarded-ip",
            help="Upstream proxy IP to trust for X-Forwarded-* (external mode).",
        ),
    ] = None,
    bind_host: Annotated[
        str | None,
        typer.Option(
            "--bind-host",
            help=(
                "Address dtxWiki listens on (default 127.0.0.1, or the installed "
                "unit's). Only for a TLS proxy / load balancer on ANOTHER "
                "machine; pair it with --forwarded-ip."
            ),
        ),
    ] = None,
) -> None:
    """One command to stand up (or reconfigure) a production dtxWiki server."""
    if do_admin and email is not None:
        try:
            email = normalize_email(email)
        except EmailInputPolicyError as exc:
            raise typer.BadParameter(str(exc), param_hint="--email") from exc
    existing = prod_setup.read_env_file(env_file)
    if domain is None:
        domain = deployment.domain_of(existing.get("DTXWIKI_PUBLIC_BASE_URL"))
    if not domain:
        typer.echo(
            "setup failed: --domain is required "
            "(no existing DTXWIKI_PUBLIC_BASE_URL to reuse).",
            err=True,
        )
        raise typer.Exit(2)
    is_fresh = not env_file.exists()
    install_systemd = systemd and sys.platform != "win32"

    tls_mode = _resolve_tls_mode(
        tls, existing, dry_run=dry_run, insecure_eval=insecure_eval
    )
    if tls_mode == "external":
        # An upstream proxy terminates TLS; dtxWiki issues no cert / runs no Caddy.
        caddy = False
    forwarded_allow_ips = (
        forwarded_ip or existing.get("DTXWIKI_FORWARDED_ALLOW_IPS") or "127.0.0.1"
    )
    if tls_mode == "custom" and (tls_cert is None or tls_key is None) and not dry_run:
        typer.echo(
            "setup failed: --tls custom requires --tls-cert and --tls-key.",
            err=True,
        )
        raise typer.Exit(2)
    unit_dir = prod_setup.SYSTEMD_UNIT_DIR
    listen_host = _resolve_bind_host(
        bind_host,
        installed=prod_setup.installed_bind_host(unit_dir / prod_setup.BACKEND_UNIT),
        tls_mode=tls_mode,
        forwarded_allow_ips=forwarded_allow_ips,
    )

    suffix = " (dry-run)" if dry_run else ""
    typer.echo(f"dtxWiki production setup — domain={domain} tls={tls_mode}{suffix}")

    # 1/7 preflight — the real failure mode is DNS/ports, not dtxWiki.
    if skip_preflight:
        typer.echo("[1/7] preflight skipped")
    else:
        typer.echo("[1/7] preflight (DNS + ports)")
        # Only the acme mode needs Let's Encrypt's public-DNS + 80/443 checks;
        # the other modes use an internal name/IP or terminate TLS upstream.
        checks = (
            prod_setup.preflight(domain)
            if tls_mode == "acme"
            else prod_setup.preflight_internal(domain)
        )
        for check in checks:
            typer.echo(f"  [{check.status.upper()}] {check.name}: {check.message}")
        if prod_setup.worst_status(checks) == "fail" and not dry_run:
            typer.echo(
                "preflight failed — fix DNS/ports above and re-run "
                "(or --skip-preflight).",
                err=True,
            )
            raise typer.Exit(2)

    runner = prod_setup.Runner(dry_run=dry_run, echo=typer.echo)
    # Blocking problems that mean the server will NOT come up correctly. Collected
    # so the final line reports the real outcome instead of always "complete".
    blocking: list[str] = []
    smoke_outcome: SmokeOutcome = "skipped"
    is_root = bool(getattr(os, "geteuid", lambda: -1)() == 0)

    # 2/7 signing key + session secret (preserved on reconfigure)
    typer.echo("[2/7] signing key + session secret")

    def _ensure_key() -> None:
        wrote = prod_setup.generate_jwt_key(jwt_key)
        typer.echo(
            "    JWT key generated" if wrote else "    JWT key exists — kept"
        )
        # A root-written key is root:root 0600 — unreadable by the service
        # account, which would fail the production JWT-key startup guard. Make it
        # group-readable by the service group when running as root.
        applied = prod_setup.set_key_ownership(jwt_key, service_user, echo=typer.echo)
        if is_root and not applied:
            blocking.append(
                f"JWT key {jwt_key} could not be made readable by '{service_user}'"
                " — the service will fail the production JWT-key startup guard"
            )

    runner.action(
        f"ensure JWT key at {jwt_key} (readable by {service_user})", _ensure_key
    )
    app_mode = existing.get("DTXWIKI_APP_MODE", "team")
    session_secret = existing.get("DTXWIKI_SESSION_SECRET") or ""
    replace_secret = prod_setup.session_secret_needs_replacement(
        session_secret, app_mode=app_mode
    )
    if replace_secret:
        if session_secret:
            typer.echo(
                "    existing DTXWIKI_SESSION_SECRET is a template placeholder or "
                "too short — generating a new one (existing sessions end)"
            )
        session_secret = prod_setup.generate_session_secret()

    # 3/7 env file (full write when fresh; targeted update on reconfigure)
    typer.echo("[3/7] env file")
    db_url = existing.get("DTXWIKI_DATABASE_URL") or database_url
    data_dir = Path(existing.get("DTXWIKI_DATA_DIR") or prod_setup.DEFAULT_DATA_DIR)
    if is_fresh:
        content = prod_setup.render_production_env(
            domain=domain,
            session_secret=session_secret,
            jwt_key_path=jwt_key,
            database_url=db_url,
            tls_mode=tls_mode,
            forwarded_allow_ips=forwarded_allow_ips,
            worker_mode="external" if install_systemd else "managed",
        )
        runner.action(
            f"write {env_file}",
            lambda: prod_setup.write_env_text(env_file, content),
        )
    else:
        scheme = "http" if tls_mode == "none" else "https"
        origin = f"{scheme}://{domain}"
        signup_enabled = existing.get("DTXWIKI_SIGNUP_ENABLED")
        if signup_enabled is None:
            signup_enabled = str(default_signup_enabled(app_mode)).lower()
        updates = {
            "DTXWIKI_PUBLIC_BASE_URL": origin,
            "DTXWIKI_CORS_ORIGINS": origin,
            "DTXWIKI_TLS_MODE": tls_mode,
            # Clear the insecure opt-in unless we are (re)configuring 'none'.
            "DTXWIKI_ALLOW_INSECURE_HTTP": (
                "true" if tls_mode == "none" else "false"
            ),
            "DTXWIKI_FORWARDED_ALLOW_IPS": forwarded_allow_ips,
            "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH": str(jwt_key),
            "DTXWIKI_DATABASE_URL": db_url,
            # Codex #21: backfill DTXWIKI_DATA_DIR so a pre-Fix-9 env file that
            # never had it does not fall back to ~/.dtxwiki (read-only under the
            # systemd ProtectSystem=strict sandbox -> upload 500s). Preserve a
            # custom value if the operator already set one.
            "DTXWIKI_DATA_DIR": existing.get(
                "DTXWIKI_DATA_DIR", prod_setup.DEFAULT_DATA_DIR
            ),
            "DTXWIKI_APP_ENV": "production",
            "DTXWIKI_APP_MODE": app_mode,
            "DTXWIKI_WORKER_MODE": (
                "external" if install_systemd
                else existing.get("DTXWIKI_WORKER_MODE", "managed")
            ),
            # Backfill the mode-derived safe default for legacy env files, but
            # never replace an operator's explicit true/false choice.
            "DTXWIKI_SIGNUP_ENABLED": signup_enabled,
            "DTXWIKI_BEHIND_PROXY": "true",
            "DTXWIKI_AUTO_BOOTSTRAP": "false",
        }
        if replace_secret:
            # Missing, EMPTY (the .env.example template ships it empty) or a
            # placeholder: dtxWiki would refuse to start with it.
            updates["DTXWIKI_SESSION_SECRET"] = session_secret
        runner.action(
            f"update {env_file}",
            lambda: prod_setup.set_env_values(env_file, updates),
        )

    def _secure_env_file() -> None:
        # root:<service group> 0640, so `sudo -u dtxwiki ... source <env file>`
        # really loads the production env instead of silently running without it.
        applied = prod_setup.set_env_file_ownership(
            env_file, service_user, echo=typer.echo
        )
        if is_root and not applied:
            blocking.append(
                f"env file {env_file} could not be made readable by '{service_user}'"
            )

    runner.action(
        f"make {env_file} root:{service_user} 0640", _secure_env_file
    )

    # 4/7 database: migrate and create the owner BEFORE any unit starts. With
    # DTXWIKI_AUTO_BOOTSTRAP=false dtxWiki refuses to start on a database that
    # is not at the code's head revision, and it must not be able to race the
    # bootstrap either.
    typer.echo("[4/7] database bootstrap + admin")

    def database_settings() -> Settings:
        # The values the service will run with, not whatever this root shell
        # happens to export.
        return Settings(
            database_url=db_url,
            app_mode=app_mode,
            session_secret=session_secret,
            auto_bootstrap=True,
        )

    if do_bootstrap:
        runner.action(
            "apply migrations (bootstrap)",
            lambda: bootstrap_app(database_settings()),
        )
    else:
        typer.echo(
            "  bootstrap skipped (--no-bootstrap): the services refuse to start "
            "until 'dtxwiki bootstrap' has migrated the database."
        )
    if do_admin and email is None:
        typer.echo("  WARN: --email not given; skipping admin creation", err=True)
    elif do_admin:
        runner.action(
            f"ensure local user {email}",
            lambda: _ensure_admin_user(email, database_settings()),
        )

    def _hand_over_data() -> None:
        # Everything above ran as root; the units run as the service account.
        problems = prod_setup.hand_data_to_service_user(
            data_dir,
            service_user,
            database_file=prod_setup.sqlite_database_file(db_url),
            echo=typer.echo,
        )
        for problem in problems:
            typer.echo(f"  WARN: {problem}", err=True)
        blocking.extend(problems)

    runner.action(f"hand {data_dir} to {service_user}", _hand_over_data)

    # 5/7 systemd units — enabled and (re)started only now, on a migrated
    # database, with the configuration just written.
    if not systemd:
        typer.echo("[5/7] systemd skipped")
    elif sys.platform == "win32":
        typer.echo("[5/7] systemd not available on this platform; skipping")
    else:
        typer.echo(f"[5/7] systemd units (listen {listen_host}:21080)")
        unit = prod_setup.share_file(_SYSTEMD_UNITS[prod_setup.BACKEND_UNIT])
        worker = prod_setup.share_file(_SYSTEMD_UNITS[prod_setup.WORKER_UNIT])
        if unit and worker:
            backend_unit = prod_setup.render_backend_unit(
                unit.read_text(encoding="utf-8"), bind_host=listen_host
            )
            worker_unit = worker.read_text(encoding="utf-8")
            runner.action(
                f"install {unit_dir / prod_setup.BACKEND_UNIT}",
                lambda: prod_setup.write_unit_file(
                    unit_dir / prod_setup.BACKEND_UNIT, backend_unit
                ),
            )
            runner.action(
                f"install {unit_dir / prod_setup.WORKER_UNIT}",
                lambda: prod_setup.write_unit_file(
                    unit_dir / prod_setup.WORKER_UNIT, worker_unit
                ),
            )
            runner.shell("daemon-reload", ["systemctl", "daemon-reload"])
            runner.shell("enable", service_control.systemctl_argv("enable"))
            # restart, not `enable --now`: a reconfigure must replace processes
            # that still run with the previous env file and unit.
            runner.shell("(re)start", service_control.systemctl_argv("restart"))
        else:
            msg = "packaged systemd units not found — service not installed"
            typer.echo(f"  WARN: {msg}", err=True)
            blocking.append(msg)

    # 6/7 reverse proxy (Caddy) — selected by TLS mode; 'external' skips Caddy.
    if not caddy:
        typer.echo("[6/7] reverse proxy skipped")
        if tls_mode == "external":
            typer.echo(
                "  TLS is terminated by your existing proxy / load balancer. "
                "Configure it to:\n"
                f"    - serve {domain} over HTTPS with its own certificate\n"
                f"    - reverse-proxy to port 21080 of this host (dtxWiki "
                f"listens on {listen_host})\n"
                "    - forward X-Forwarded-Proto / X-Forwarded-Host / X-Forwarded-For\n"
                f"  dtxWiki trusts forwarded headers only from: {forwarded_allow_ips} "
                "(set with --forwarded-ip)."
            )
    else:
        typer.echo(f"[6/7] reverse proxy (Caddy, tls={tls_mode})")
        template = prod_setup.share_file(_CADDY_TEMPLATE[tls_mode])
        if template:
            rendered = prod_setup.render_caddyfile(
                template.read_text(encoding="utf-8"),
                domain,
                cert=str(tls_cert) if tls_cert else None,
                key=str(tls_key) if tls_key else None,
            )
            runner.action(
                f"write {caddyfile}",
                lambda: prod_setup.write_caddyfile(caddyfile, rendered),
            )
            if sys.platform != "win32":
                runner.shell(
                    "reload caddy", ["systemctl", "reload-or-restart", "caddy"]
                )
            if tls_mode == "internal" and not dry_run:
                _print_internal_ca_guidance(domain)
            elif tls_mode == "none":
                typer.echo(
                    "  WARNING: plain HTTP (no TLS) — auth travels in plaintext. "
                    "Eval only; never for real data."
                )
        else:
            msg = (
                f"packaged {_CADDY_TEMPLATE[tls_mode]} not found "
                "— reverse proxy not configured"
            )
            typer.echo(f"  WARN: {msg}", err=True)
            blocking.append(msg)

    # 7/7 smoke
    typer.echo("[7/7] smoke")
    if tls_mode == "none":
        if smoke:
            typer.echo("  smoke skipped (plain HTTP / 'none' mode)")
    elif smoke and not dry_run and sys.platform != "win32":
        # internal-CA: hand the freshly-exported root CA to the smoke script so
        # its TLS checks trust the self-signed leaf.
        ca = (
            Path("/etc/dtxwiki/dtxwiki-root-ca.crt")
            if tls_mode == "internal"
            else None
        )
        smoke_outcome = _run_smoke(domain, ca=ca)
    elif smoke:
        typer.echo("  smoke skipped (dry-run or unsupported platform)")

    typer.echo("")
    if dry_run:
        typer.echo(
            f"DRY-RUN complete — {len(runner.results)} steps planned. "
            "Re-run without --dry-run to apply."
        )
    elif blocking:
        # Don't claim success when a required step failed — the box is only
        # half-configured and the service likely won't start.
        typer.echo(
            f"Setup finished with {len(blocking)} blocking issue(s) — the "
            "service may not start:"
        )
        for item in blocking:
            typer.echo(f"  - {item}")
        typer.echo(
            "Resolve the above, then re-run "
            f"'dtxwiki setup production --reconfigure --domain {domain}'."
        )
        raise typer.Exit(1)
    else:
        # tls=none runs over plain HTTP; pointing the operator at https would
        # be a dead link (merged I13).
        scheme = "http" if tls_mode == "none" else "https"
        typer.echo(f"Setup complete. Visit {scheme}://{domain}")
        if smoke_outcome == "failed":
            typer.echo(
                "  Note: smoke checks were not all green — TLS/DNS may still be "
                f"settling. Re-check {scheme}://{domain} shortly."
            )
        elif smoke_outcome == "skipped":
            # Distinct from a pass on purpose: nothing was checked, so the
            # operator has no evidence either way about this origin.
            typer.echo(
                "  Note: the connection diagnostic did not run, so nothing has "
                f"verified {scheme}://{domain} end to end yet."
            )


def _resolve_bind_host(
    flag: str | None,
    *,
    installed: str | None,
    tls_mode: str,
    forwarded_allow_ips: str,
) -> str:
    """Pick the backend's listen address: the flag, else the installed unit's.

    A non-loopback address is for a TLS proxy or load balancer on another
    machine. Such a proxy connects from a non-loopback address, so trusting
    forwarded headers from 127.0.0.1 alone would make every request look like
    plain HTTP and redirect to https forever; that combination is refused.
    """

    requested = flag if flag is not None else installed
    try:
        host = prod_setup.validate_bind_host(
            requested if requested is not None else prod_setup.DEFAULT_BIND_HOST
        )
    except ValueError as exc:
        typer.echo(f"setup failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    if prod_setup.is_loopback_host(host):
        return host
    if tls_mode == "external" and prod_setup.only_loopback_proxies(
        forwarded_allow_ips
    ):
        typer.echo(
            f"setup failed: --bind-host {host} lets a proxy on another machine "
            "reach dtxWiki; also pass --forwarded-ip <that proxy's address> so its "
            "X-Forwarded-Proto/Host are trusted.",
            err=True,
        )
        raise typer.Exit(2)
    if tls_mode != "external":
        typer.echo(
            f"  WARN: --bind-host {host} also exposes the plain-HTTP app port "
            "beyond the local Caddy, which already reaches it on 127.0.0.1.",
            err=True,
        )
    return host


def _ensure_admin_user(email: str, settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    try:
        factory = make_session_factory(engine)
        service = UserAdminService(
            session_factory=factory,
            credential_hasher=CredentialHasher(),
        )
        temp_password = secrets.token_urlsafe(16)
        try:
            service.add_user(email=email, password=temp_password)
            typer.echo(f"    admin {email} created")
            typer.echo(f"    temporary password: {temp_password}")
            typer.echo(
                "    change it after first login (dtxwiki users reset-password)."
            )
        except DuplicateEmailError:
            typer.echo(f"    admin {email} already exists — kept")
        # Grant instance-level server-admin to the initial owner so they can reach
        # the system/deployment settings. Idempotent, and also covers the
        # existing-user path on a --reconfigure.
        with factory.begin() as session:
            user = session.scalar(
                select(User).where(User.email == normalize_email(email))
            )
            if user is not None:
                server_admin_service.grant_server_admin(
                    session, user.id, granted_by="setup"
                )
                typer.echo(f"    granted server-admin to {email}")
    finally:
        # Close the connection (and with it SQLite's -wal/-shm, created by root)
        # before the data directory is handed to the service account and the
        # units start.
        engine.dispose()


SmokeOutcome = Literal["passed", "failed", "skipped"]

# The whole diagnostic, DNS included. An unreachable name must not hold the
# installer open indefinitely.
_SMOKE_TIMEOUT_SECONDS = 30


def _run_smoke(domain: str, *, ca: Path | None = None) -> SmokeOutcome:
    """Run the production smoke diagnostic against the public origin.

    Three outcomes, not two. It used to return ``True`` both when the checks
    passed AND when the script could not be found at all, so a packaging gap
    reported as a clean run -- the one case where an operator most needs to know
    that nothing was verified.

    ``ca`` is the trust anchor for the TLS checks (internal-CA mode hands over
    the exported root). A CA that was asked for but cannot be read is an error:
    quietly re-running without it would verify against a different trust store
    than the operator specified and still call the result a pass.
    """

    origin = f"https://{domain}"
    smoke_script = prod_setup.share_file(_SMOKE_SCRIPT)
    if smoke_script is None:
        typer.echo(
            "  WARN: smoke script not found — NOTHING was verified about "
            f"{origin}. Run the diagnostic manually once the proxy is up.",
            err=True,
        )
        return "skipped"
    cmd = ["bash", str(smoke_script), origin]
    if ca is not None:
        if not ca.exists():
            typer.echo(
                f"  WARN: CA bundle {ca} was requested but is missing; refusing "
                "to verify against a different trust store.",
                err=True,
            )
            return "failed"
        cmd.append(str(ca))
    typer.echo(f"  running smoke against {origin}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SMOKE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        typer.echo(
            f"  WARN: smoke checks exceeded {_SMOKE_TIMEOUT_SECONDS}s against "
            f"{origin} and were stopped.",
            err=True,
        )
        return "failed"
    if result.stdout:
        typer.echo(result.stdout)
    if result.returncode != 0:
        typer.echo(
            "  WARN: smoke checks did not all pass "
            "(TLS/DNS may still be settling).",
            err=True,
        )
        if result.stderr:
            typer.echo(result.stderr, err=True)
        return "failed"
    return "passed"
