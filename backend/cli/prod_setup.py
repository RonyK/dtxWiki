"""Production install & reconfiguration helpers behind `dtxwiki setup`/`config`.

The deployment source-of-truth is two files: the env file
(``/etc/dtxwiki/dtxwiki.env`` by default) and the reverse-proxy Caddyfile. Every
mutating helper here follows backup -> write-atomically. OS-mutating steps (apt,
systemctl, caddy) run only on Linux and are skipped under ``dry_run``, so the
pure logic (env editing, templating, preflight) is exercised everywhere.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from backend.app.settings import APP_MODES, Settings, default_signup_enabled
from backend.cli.integrations._common import atomic_write_text, backup_once
from backend.services.deployment import env_to_field, is_service_env_key, valid_env_keys

DEFAULT_ENV_FILE = Path("/etc/dtxwiki/dtxwiki.env")
DEFAULT_JWT_KEY = Path("/etc/dtxwiki/jwt.pem")
DEFAULT_CADDYFILE = Path("/etc/caddy/Caddyfile")
DEFAULT_DATABASE_URL = "sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"
# Writable state root (uploads/library/logs). MUST be inside the systemd unit's
# ReadWritePaths (/var/lib/dtxwiki); the default (~/.dtxwiki) is read-only under
# ProtectSystem=strict, which would 500 every image upload.
DEFAULT_DATA_DIR = "/var/lib/dtxwiki"
# Where `setup production` installs the bundled units. Read at call time so tests
# can point it at a temporary directory.
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
BACKEND_UNIT = "dtxwiki.service"
WORKER_UNIT = "dtxwiki-worker.service"
# The backend unit's listen address. Loopback unless a TLS proxy or load
# balancer on ANOTHER machine has to reach the app (`--bind-host`).
DEFAULT_BIND_HOST = "127.0.0.1"
# The env file holds the session secret. root writes it; the service group may
# read it, so `sudo -u dtxwiki ... source /etc/dtxwiki/dtxwiki.env` works.
ENV_FILE_MODE = 0o640
# Caddy runs as its own user and must read the Caddyfile (it holds no secret).
CADDYFILE_MODE = 0o644
UNIT_FILE_MODE = 0o644
DATA_DIR_MODE = 0o750
CADDY_PLACEHOLDER_DOMAIN = "wiki.example.com"
# Placeholders substituted in the bring-your-own-cert (custom) Caddy template.
CADDY_PLACEHOLDER_CERT = "/path/to/fullchain.pem"
CADDY_PLACEHOLDER_KEY = "/path/to/privkey.pem"
# Where Caddy's built-in `tls internal` issuer stores its local root CA (systemd
# host install). Operators export this and install it into client trust stores.
DEFAULT_CADDY_ROOT_CA = Path(
    "/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"
)


# --------------------------------------------------------------------------- #
# dotenv read / write (source of truth = the env file)
# --------------------------------------------------------------------------- #
def _parse_key(line: str) -> str:
    """Extract the KEY from a stripped ``KEY=VALUE`` line, tolerating ``export``.

    Some hand-edited env files prefix assignments with ``export``; without this
    the key would parse as ``"export DTXWIKI_X"`` and never match a real setting.
    """

    head = line.split("=", 1)[0].strip()
    if head.startswith("export ") or head.startswith("export\t"):
        head = head[len("export ") :].strip()
    return head


def _strip_quotes(value: str) -> str:
    """Drop one pair of surrounding quotes, mirroring systemd ``EnvironmentFile``.

    A hand-edited ``KEY="value"`` works for the running service (systemd strips
    the quotes), so it must parse to the same value here — otherwise update/
    rollback/config would operate on a quoted string the service never sees
    (e.g. a DATABASE_URL that ``make_url`` rejects, silently skipping the
    pre-update DB backup).
    """

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def read_env_file_from_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict; comments/blank lines ignored."""

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, value = line.split("=", 1)
        values[_parse_key(line)] = _strip_quotes(value.strip())
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """Parse an env file into a dict; comments/blank lines ignored."""

    if not path.exists():
        return {}
    return read_env_file_from_text(path.read_text(encoding="utf-8"))


def get_env_value(path: Path, key: str) -> str | None:
    return read_env_file(path).get(key)


def _owner_of(path: Path) -> tuple[int, int] | None:
    if sys.platform == "win32" or not path.exists():
        return None
    stat = path.stat()
    return stat.st_uid, stat.st_gid


def _restore_owner(path: Path, owner: tuple[int, int] | None) -> None:
    """Give an atomically replaced file back its previous owner and group.

    The replacement is a new file owned by whoever wrote it, so a root
    `config set` would otherwise turn a ``root:dtxwiki 0640`` env file into
    ``root:root 0640`` and lock the service group out of it. Best effort: a
    non-root writer cannot change ownership and does not need to.
    """

    if owner is None or _owner_of(path) == owner:
        return
    try:
        os.chown(path, *owner)
    except PermissionError:
        pass


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    """Update keys in-place, preserving comments/order; append new keys.

    Backs up the file once (``.bak``) and writes atomically via a temp file,
    keeping the file's mode and owner/group.
    """

    owner = _owner_of(path)
    backup_once(path)
    existing_lines = (
        path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    )
    remaining = dict(updates)
    updated_keys: set[str] = set()
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = _parse_key(stripped)
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                updated_keys.add(key)
                continue
            if key in updated_keys:
                # Drop later duplicate lines of a key we just updated. Readers
                # (and systemd EnvironmentFile) take the LAST occurrence, so a
                # stale duplicate further down would silently shadow the update.
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    atomic_write_text(path, "\n".join(out) + "\n")
    _restore_owner(path, owner)


def validate_env_key(key: str) -> None:
    """Raise ``ValueError`` if ``key`` is not a real ``DTXWIKI_`` setting."""

    if not key.startswith("DTXWIKI_"):
        raise ValueError(f"config keys must start with DTXWIKI_: {key!r}")
    if is_service_env_key(key):
        return
    if key not in valid_env_keys():
        raise ValueError(
            f"unknown config key {key!r} (possible typo). "
            "See 'dtxwiki config show' for valid keys."
        )


def parse_assignment(assignment: str) -> tuple[str, str]:
    """Split ``KEY=VALUE`` (value may contain ``=``)."""

    if "=" not in assignment:
        raise ValueError(f"expected KEY=VALUE, got {assignment!r}")
    key, value = assignment.split("=", 1)
    return key.strip(), value


def validate_config_value(key: str, value: str) -> None:
    """Reject a bad value BEFORE it is written to the source-of-truth env file.

    Raises ``ValueError`` if ``value`` would not parse into ``Settings`` (type
    errors like ``BACKEND_PORT=abc`` / ``MCP_HTTP_MODE=banana``) or fails a
    curated semantic check (``APP_MODE`` enum, ``PUBLIC_BASE_URL`` /
    ``CORS_ORIGINS`` must be http(s) URLs). Uncurated string keys still get the
    pydantic type check; only their free-form semantics are not asserted.
    """

    field = env_to_field(key)
    if is_service_env_key(key):
        return
    if field is None or field not in Settings.model_fields:
        raise ValueError(f"unknown config key {key!r}")

    # 1) pydantic type / Literal validation (e.g. int ports, mcp_http_mode enum,
    #    metrics_enabled 0/1). Build with only this field set, isolated from the
    #    operator's shell env so an unrelated malformed DTXWIKI_* var cannot make
    #    a valid value look invalid (and blame the wrong key).
    try:
        _build_settings_isolated({field: value})
    except Exception as exc:  # surface any validation error
        raise ValueError(f"invalid value for {key}: {exc}") from exc

    # 2) curated semantic checks for keys whose Python type is too loose.
    if key == "DTXWIKI_APP_MODE" and value not in APP_MODES:
        raise ValueError(
            f"invalid value for {key}: expected one of "
            f"{', '.join(sorted(APP_MODES))}, got {value!r}"
        )
    if key in {"DTXWIKI_PUBLIC_BASE_URL", "DTXWIKI_CORS_ORIGINS"}:
        for origin in value.split(","):
            origin = origin.strip()
            if origin and not origin.lower().startswith(("http://", "https://")):
                raise ValueError(
                    f"invalid value for {key}: {origin!r} is not an http(s) URL"
                )
    # update_channel / update_keep_backups are coerced leniently at runtime (so a
    # stray env value cannot crash startup); reject bad values strictly HERE so
    # `config set` fails fast instead of silently writing a no-op.
    if key == "DTXWIKI_UPDATE_CHANNEL" and value.strip().lower() not in {
        "stable",
        "prerelease",
    }:
        raise ValueError(
            f"invalid value for {key}: expected 'stable' or 'prerelease', "
            f"got {value!r}"
        )
    if key == "DTXWIKI_UPDATE_KEEP_BACKUPS":
        try:
            count = int(value)
        except ValueError:
            raise ValueError(
                f"invalid value for {key}: expected a positive integer, "
                f"got {value!r}"
            ) from None
        if count < 1:
            raise ValueError(f"invalid value for {key}: must be >= 1, got {count}")


def _build_settings_isolated(fields: dict[str, str]) -> Settings:
    """Construct ``Settings`` from ``fields`` only, ignoring the process env.

    pydantic-settings reads every unset field from ``os.environ``; during CLI
    validation only the file/argument values should matter, so DTXWIKI_* env
    vars are cleared for the construction and restored afterwards.
    """

    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("DTXWIKI_")}
    try:
        return Settings(**fields)
    finally:
        os.environ.update(saved)


def validate_production_config(env_file: Path, key: str, value: str) -> None:
    """Reject a value that would break PRODUCTION startup, before writing it.

    Builds the merged config (current file + the new ``key=value``) and, when it
    resolves to a production environment, runs the value-based production guards
    (e.g. ``AUTO_BOOTSTRAP`` must be false, ``FORWARDED_ALLOW_IPS`` != ``*``,
    ``PUBLIC_BASE_URL`` https). The signing-key file check is skipped — this
    validates the value being written, not the key on disk. No-op for
    non-production configs.
    """

    merged = read_env_file(env_file)
    merged[key] = value
    fields: dict[str, str] = {}
    for env_key, env_value in merged.items():
        field = env_to_field(env_key)
        if field in Settings.model_fields:
            fields[field] = env_value
    candidate = _build_settings_isolated(fields)
    if not candidate.is_production:
        return
    try:
        candidate.validate_production_invariants(check_key_file=False)
    except RuntimeError as exc:
        raise ValueError(
            f"{key}={value} would break production startup: {exc}"
        ) from exc


def generate_session_secret() -> str:
    return secrets.token_urlsafe(64)


# Remote modes need at least this many characters (the startup guard's rule).
MIN_REMOTE_SESSION_SECRET_LENGTH = 32
_PLACEHOLDER_MARKERS = ("replace-with", "change-me")
_LEGACY_DEVELOPMENT_SECRET = "dtxwiki-development-session-secret"


def session_secret_needs_replacement(value: str, *, app_mode: str) -> bool:
    """True when `setup production` must generate a session secret.

    Mirrors the startup guard: missing or empty (the env templates ship it
    empty), the old built-in development value, a template placeholder
    (``<...>``, ``replace-with``, ``change-me``) or, outside solo_local, shorter
    than 32 characters. dtxWiki refuses to start with any of those, so keeping
    one would only produce a server that cannot boot.
    """

    secret = value.strip()
    if not secret or secret == _LEGACY_DEVELOPMENT_SECRET:
        return True
    lowered = secret.lower()
    if secret.startswith("<") and secret.endswith(">"):
        return True
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    return app_mode != "solo_local" and len(secret) < MIN_REMOTE_SESSION_SECRET_LENGTH


def write_env_text(path: Path, content: str) -> None:
    """Backup (once) then atomically write a full file (env or Caddyfile).

    An existing file keeps its mode and owner/group; a new one starts 0600.
    """

    owner = _owner_of(path)
    backup_once(path)
    atomic_write_text(path, content)
    _restore_owner(path, owner)


def write_caddyfile(path: Path, content: str) -> None:
    """Write the Caddyfile readable by the ``caddy`` service account.

    A fresh file from :func:`write_env_text` would be 0600 root, and Caddy (which
    runs as its own user) could not read its configuration. It holds no secret:
    certificate *paths* at most.
    """

    write_env_text(path, content)
    if sys.platform != "win32":
        os.chmod(path, CADDYFILE_MODE)


# --------------------------------------------------------------------------- #
# preflight: the real failure mode is DNS/ports, not dtxWiki itself
# --------------------------------------------------------------------------- #
PreflightStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: PreflightStatus
    message: str


def resolve_host(domain: str) -> list[str]:
    """Resolve ``domain`` to a sorted list of IPs (monkeypatchable in tests)."""

    infos = socket.getaddrinfo(domain, None)
    return sorted({info[4][0] for info in infos})


def local_egress_ip() -> str | None:
    """Best-effort local egress IP (no packets sent). ``None`` on failure."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def probe_port_bindable(port: int) -> tuple[PreflightStatus, str]:
    """Can the reverse proxy bind ``port`` here? in-use/denied are warnings."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        return "ok", f"port {port} is free for the reverse proxy"
    except PermissionError:
        return (
            "warn",
            f"port {port} needs root / CAP_NET_BIND_SERVICE — run setup with sudo",
        )
    except OSError:
        return (
            "warn",
            f"port {port} is already in use (expected if the proxy is running)",
        )
    finally:
        sock.close()


def preflight(
    domain: str,
    *,
    ports: Sequence[int] = (80, 443),
    resolver: Callable[[str], list[str]] | None = None,
    egress: Callable[[], str | None] | None = None,
    port_probe: Callable[[int], tuple[PreflightStatus, str]] | None = None,
) -> list[PreflightCheck]:
    """Check the two prerequisites Let's Encrypt actually needs: DNS + ports.

    The callables resolve to the module-level defaults at call time (so tests
    can monkeypatch ``resolve_host`` etc.) but may also be injected directly.
    """

    resolver = resolver or resolve_host
    egress = egress or local_egress_ip
    port_probe = port_probe or probe_port_bindable
    checks: list[PreflightCheck] = []
    try:
        ips = resolver(domain)
    except OSError as exc:
        checks.append(
            PreflightCheck(
                "dns",
                "fail",
                f"could not resolve {domain} ({exc}). Point its DNS A record at "
                "this server's public IP before issuing a certificate.",
            )
        )
        return checks
    checks.append(
        PreflightCheck("dns", "ok", f"{domain} resolves to {', '.join(ips)}")
    )

    host_ip = egress()
    if host_ip and host_ip not in ips:
        checks.append(
            PreflightCheck(
                "dns_match",
                "warn",
                f"{domain} -> {', '.join(ips)} but this host's egress IP is "
                f"{host_ip}. Behind NAT/CGNAT this can still work; confirm the "
                "A record points at the public IP and 80/443 are forwarded.",
            )
        )

    for port in ports:
        status, message = port_probe(port)
        checks.append(PreflightCheck(f"port_{port}", status, message))
    return checks


def worst_status(checks: Iterable[PreflightCheck]) -> PreflightStatus:
    order: dict[PreflightStatus, int] = {"ok": 0, "warn": 1, "fail": 2}
    worst: PreflightStatus = "ok"
    for check in checks:
        if order[check.status] > order[worst]:
            worst = check.status
    return worst


# --------------------------------------------------------------------------- #
# templating: env file + Caddyfile
# --------------------------------------------------------------------------- #
def render_caddyfile(
    template_text: str,
    domain: str,
    *,
    cert: str | None = None,
    key: str | None = None,
) -> str:
    """Substitute the example domain (and, for the bring-your-own-cert template,
    the cert/key file placeholders) in a Caddyfile template."""

    rendered = template_text.replace(CADDY_PLACEHOLDER_DOMAIN, domain)
    if cert is not None:
        rendered = rendered.replace(CADDY_PLACEHOLDER_CERT, cert)
    if key is not None:
        rendered = rendered.replace(CADDY_PLACEHOLDER_KEY, key)
    return rendered


# --------------------------------------------------------------------------- #
# systemd units: the backend's listen address (--bind-host)
# --------------------------------------------------------------------------- #
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_EXEC_START_HOST = re.compile(r"(?m)^(ExecStart=.*\s--host\s+)(\S+)(.*)$")


def validate_bind_host(value: str) -> str:
    """Return ``value`` if it is an IP literal or a host name, else raise.

    It is written verbatim into the unit's ExecStart line, so anything beyond an
    address (spaces, quotes, a second option) is refused rather than escaped.
    """

    host = value.strip()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if _HOSTNAME.fullmatch(host):
        return host
    raise ValueError(
        f"--bind-host must be an IP address or a host name, got {value!r}"
    )


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def only_loopback_proxies(forwarded_allow_ips: str) -> bool:
    """True when DTXWIKI_FORWARDED_ALLOW_IPS trusts nothing but this host."""

    entries = [entry.strip() for entry in forwarded_allow_ips.split(",")]
    entries = [entry for entry in entries if entry]
    return bool(entries) and all(is_loopback_host(entry) for entry in entries)


def render_backend_unit(template_text: str, *, bind_host: str) -> str:
    """Set the ``--host`` of the backend unit's ExecStart to ``bind_host``."""

    host = validate_bind_host(bind_host)
    rendered, count = _EXEC_START_HOST.subn(
        lambda match: f"{match.group(1)}{host}{match.group(3)}", template_text
    )
    if count != 1:
        raise ValueError(
            "backend unit template must have exactly one ExecStart with --host"
        )
    return rendered


def installed_bind_host(unit_path: Path) -> str | None:
    """The ``--host`` of an installed backend unit, so a reconfigure keeps it."""

    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _EXEC_START_HOST.search(text)
    if match is None:
        return None
    try:
        return validate_bind_host(match.group(2))
    except ValueError:
        return None


def write_unit_file(path: Path, content: str) -> None:
    """Atomically install a unit file world-readable, as systemd expects."""

    atomic_write_text(path, content)
    if sys.platform != "win32":
        os.chmod(path, UNIT_FILE_MODE)


def render_production_env(
    *,
    domain: str,
    session_secret: str,
    jwt_key_path: str | Path,
    database_url: str = DEFAULT_DATABASE_URL,
    data_dir: str = DEFAULT_DATA_DIR,
    app_name: str = "dtxWiki",
    mcp_http_mode: str = "streamable",
    tls_mode: str = "acme",
    forwarded_allow_ips: str = "127.0.0.1",
) -> str:
    """Render a complete production env file that passes the startup guards.

    ``tls_mode`` selects how TLS is handled (acme|internal|custom|external|none)
    and is recorded as display-only metadata. Only ``none`` changes the running
    app: it switches the public origin to http and sets the loud, off-by-default
    ``DTXWIKI_ALLOW_INSECURE_HTTP`` opt-in (eval-only). ``forwarded_allow_ips``
    lets the ``external`` mode trust an upstream proxy other than 127.0.0.1.
    """

    insecure = tls_mode == "none"
    scheme = "http" if insecure else "https"
    origin = f"{scheme}://{domain}"
    lines = [
        "# Managed by 'dtxwiki setup production'. Edit via 'dtxwiki config set'.",
        f"DTXWIKI_APP_NAME={app_name}",
        "DTXWIKI_APP_ENV=production",
        "DTXWIKI_APP_MODE=team",
        f"DTXWIKI_PUBLIC_BASE_URL={origin}",
        f"DTXWIKI_TLS_MODE={tls_mode}",
        "DTXWIKI_BEHIND_PROXY=true",
        f"DTXWIKI_FORWARDED_ALLOW_IPS={forwarded_allow_ips}",
        f"DTXWIKI_CORS_ORIGINS={origin}",
        f"DTXWIKI_SESSION_SECRET={session_secret}",
        f"DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH={jwt_key_path}",
        f"DTXWIKI_DATABASE_URL={database_url}",
        # Writable state inside the systemd ReadWritePaths (uploads/library/logs).
        f"DTXWIKI_DATA_DIR={data_dir}",
        "DTXWIKI_AUTO_BOOTSTRAP=false",
        f"DTXWIKI_MCP_HTTP_MODE={mcp_http_mode}",
        f"DTXWIKI_SIGNUP_ENABLED={str(default_signup_enabled('team')).lower()}",
        "DTXWIKI_MAIL_PROVIDER=none",
    ]
    if insecure:
        lines.append("DTXWIKI_ALLOW_INSECURE_HTTP=true")
    return "\n".join(lines) + "\n"


def preflight_internal(
    domain: str,
    *,
    ports: Sequence[int] = (80, 443),
    port_probe: Callable[[int], tuple[PreflightStatus, str]] | None = None,
) -> list[PreflightCheck]:
    """Lightweight preflight for the non-ACME TLS modes (internal/custom/external/
    none). Public DNS resolution and Let's Encrypt's 80/443 reachability are NOT
    prerequisites here (the name is internal, or TLS is terminated elsewhere), so
    this only sanity-checks that the reverse-proxy ports are bindable."""

    port_probe = port_probe or probe_port_bindable
    checks: list[PreflightCheck] = [
        PreflightCheck(
            "dns",
            "ok",
            f"{domain}: non-ACME TLS mode — skipping public DNS + ACME port checks.",
        )
    ]
    for port in ports:
        status, message = port_probe(port)
        checks.append(PreflightCheck(f"port_{port}", status, message))
    return checks


def export_caddy_root_ca(
    dest: Path, *, source: Path = DEFAULT_CADDY_ROOT_CA
) -> Path | None:
    """Copy Caddy's `tls internal` root CA certificate to ``dest`` so the operator
    can distribute it to client trust stores. Returns ``dest`` on success, or
    ``None`` if Caddy has not generated the root yet (it is created the first time
    Caddy serves the internal site)."""

    if not source.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return dest


# --------------------------------------------------------------------------- #
# packaged-resource resolution (systemd units, Caddyfile, smoke script)
# --------------------------------------------------------------------------- #
def share_file(relpath: str) -> Path | None:
    """Locate a file shipped under ``share/dtxwiki`` or the repo (dev) tree.

    ``relpath`` is relative to ``share/dtxwiki`` (e.g.
    ``infra/prod/Caddyfile.example``).
    """

    candidates: list[Path] = []
    try:
        data_root = Path(sysconfig.get_path("data"))
        candidates.append(data_root / "share" / "dtxwiki" / relpath)
    except (KeyError, ValueError):
        pass
    candidates.append(Path(sys.prefix) / "share" / "dtxwiki" / relpath)
    # dev fallback: repo root (two levels up from this file: backend/cli/..)
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / relpath)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# command runner (echoes; subprocess only off dry-run + Linux)
# --------------------------------------------------------------------------- #
def generate_jwt_key(path: Path, *, key_size: int = 2048, force: bool = False) -> bool:
    """Write an RSA PKCS8 PEM at ``path`` (chmod 600 on POSIX).

    Returns ``True`` if a key was written, ``False`` if an existing key was kept
    (the reconfigure-safe default — overwriting invalidates all OAuth tokens).
    """

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if path.exists() and not force:
        return False
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_bytes(pem)
    else:
        # Create the key 0600 from the very first byte. write_bytes() would honor
        # the umask (typically 0644) and leave the private key briefly world-
        # readable until the chmod; opening with mode 0600 closes that window
        # (umask can only tighten it further). chmod still covers the force-
        # overwrite case where the file already existed with looser perms.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    return True


def set_key_ownership(
    path: Path,
    service_group: str,
    *,
    echo: Callable[[str], None] = lambda _msg: None,
    geteuid: Callable[[], int] | None = None,
    chown: Callable[..., None] | None = None,
    chmod: Callable[[Path, int], None] | None = None,
) -> bool:
    """Make a root-generated JWT key readable by the systemd service account.

    The service runs as ``User=dtxwiki`` / ``Group=<service_group>`` but a key
    written by root defaults to ``root:root 0600`` — unreadable by the service,
    which then fails the production JWT-key startup guard. When running as root
    on POSIX, set the key to ``root:<service_group> 0640`` (group-readable).
    Returns ``True`` if ownership was applied. No-op (returns ``False``) when not
    root, so manual/local installs keep their own perms. Callables are injectable
    for testing.
    """

    return _set_root_group_readable(
        path,
        service_group,
        label="key",
        echo=echo,
        geteuid=geteuid,
        chown=chown,
        chmod=chmod,
    )


def set_env_file_ownership(
    path: Path,
    service_group: str,
    *,
    echo: Callable[[str], None] = lambda _msg: None,
    geteuid: Callable[[], int] | None = None,
    chown: Callable[..., None] | None = None,
    chmod: Callable[[Path, int], None] | None = None,
) -> bool:
    """Make the production env file ``root:<service_group> 0640``.

    systemd reads ``EnvironmentFile=`` as root, so the service itself never
    needed this. Operators do: the documented
    ``sudo -u dtxwiki bash -c 'set -a; source /etc/dtxwiki/dtxwiki.env; ...'``
    silently ran WITHOUT the production env when the file was
    ``root:root 0600``, and then created a separate empty database in the
    service account's home. Same contract and injectables as
    :func:`set_key_ownership`.
    """

    return _set_root_group_readable(
        path,
        service_group,
        label="env file",
        echo=echo,
        geteuid=geteuid,
        chown=chown,
        chmod=chmod,
    )


def _set_root_group_readable(
    path: Path,
    service_group: str,
    *,
    label: str,
    echo: Callable[[str], None],
    geteuid: Callable[[], int] | None,
    chown: Callable[..., None] | None,
    chmod: Callable[[Path, int], None] | None,
) -> bool:
    geteuid = geteuid or getattr(os, "geteuid", lambda: -1)
    chown = chown or shutil.chown
    chmod = chmod or os.chmod

    if geteuid() != 0:
        echo(
            f"    not root: leaving {path} ownership unchanged "
            f"(the '{service_group}' service account must be able to read it)"
        )
        return False
    try:
        chown(path, user="root", group=service_group)
        chmod(path, ENV_FILE_MODE)
    except (LookupError, PermissionError, OSError) as exc:
        echo(
            f"    WARN: could not set {path} to root:{service_group} 0640 ({exc}); "
            f"ensure the service account can read the {label}"
        )
        return False
    echo(f"    {label} permissions: root:{service_group} 0640")
    return True


_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Recursively re-owning one of these would break the host, whatever a
# mistyped DTXWIKI_DATA_DIR says.
_SYSTEM_DIRECTORIES = frozenset(
    Path(path)
    for path in (
        "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt",
        "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr",
        "/usr/local", "/var", "/var/lib", "/var/tmp",
    )
)


def sqlite_database_file(database_url: str) -> Path | None:
    """The file behind a SQLite URL, or ``None`` for memory/other databases."""

    try:
        url = make_url(database_url)
    except ArgumentError:
        return None
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:" or url.database.startswith("file:"):
        return None
    return Path(url.database)


def hand_data_to_service_user(
    data_dir: Path,
    service_user: str,
    *,
    database_file: Path | None = None,
    echo: Callable[[str], None] = lambda _msg: None,
    geteuid: Callable[[], int] | None = None,
    lchown: Callable[[Path, int, int], None] | None = None,
    chmod: Callable[[Path, int], None] | None = None,
    lookup_ids: Callable[[str], tuple[int, int]] | None = None,
) -> list[str]:
    """Give the data directory and the SQLite files to the service account.

    `setup production` runs as root and has just bootstrapped the database and
    created the initial admin, so the database, its WAL sidecars and any
    directory bootstrap created are root-owned. dtxWiki (``User=dtxwiki``) could
    not write them, and starting the units first would let them fail on that.
    Returns the problems found (empty on success or when nothing had to be
    done). No-op when not root: a non-root run already owns what it created.
    Links are never followed, and a system directory is never re-owned.
    """

    geteuid = geteuid or getattr(os, "geteuid", lambda: -1)
    if geteuid() != 0:
        echo(
            f"    not root: leaving {data_dir} ownership unchanged "
            f"(it must be writable by '{service_user}')"
        )
        return []
    lchown = lchown or (
        lambda path, uid, gid: os.chown(path, uid, gid, follow_symlinks=False)
    )
    chmod = chmod or os.chmod
    lookup_ids = lookup_ids or _service_ids
    try:
        uid, gid = lookup_ids(service_user)
    except KeyError:
        return [f"service account '{service_user}' does not exist"]

    problems: list[str] = []
    root = Path(os.path.abspath(data_dir))
    if root in _SYSTEM_DIRECTORIES or len(root.parts) < 3:
        problems.append(
            f"DTXWIKI_DATA_DIR={data_dir} is a system directory; refusing to "
            f"hand it to '{service_user}'"
        )
    elif root.is_dir() and not root.is_symlink():
        try:
            _lchown_tree(root, uid, gid, lchown)
            chmod(root, DATA_DIR_MODE)
        except OSError as exc:
            problems.append(f"could not hand {root} to '{service_user}': {exc}")
        else:
            echo(f"    data directory: {root} -> {service_user} (0750)")

    if database_file is not None:
        database = Path(os.path.abspath(database_file))
        if not database.is_relative_to(root):
            sidecars = (Path(f"{database}{suffix}") for suffix in _SQLITE_SIDECARS)
            for candidate in (database, *sidecars):
                if candidate.is_file() and not candidate.is_symlink():
                    try:
                        lchown(candidate, uid, gid)
                    except OSError as exc:
                        problems.append(
                            f"could not hand {candidate} to '{service_user}': {exc}"
                        )
            echo(
                f"    database {database} is outside {root}: its directory must "
                f"also be writable by '{service_user}' (SQLite adds -wal/-shm files)"
            )
    return problems


def _service_ids(service_user: str) -> tuple[int, int]:
    import pwd

    entry = pwd.getpwnam(service_user)
    return entry.pw_uid, entry.pw_gid


def _lchown_tree(
    root: Path, uid: int, gid: int, lchown: Callable[[Path, int, int], None]
) -> None:
    """Re-own ``root`` and everything below it that differs, never via links."""

    def own(path: Path) -> None:
        stat = path.lstat()
        if (stat.st_uid, stat.st_gid) != (uid, gid):
            lchown(path, uid, gid)

    own(root)
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in (*subdirectories, *files):
            own(base / name)


@dataclass
class StepResult:
    description: str
    ran: bool


class Runner:
    """Echoes each step; executes shell steps only when not in dry-run.

    A failing step (systemctl/cp/caddy exiting non-zero, a ``PermissionError``
    writing under ``/etc`` without sudo, a missing binary) aborts the setup
    with a curated error message and exit code 1 — never a raw traceback.
    """

    def __init__(
        self,
        *,
        dry_run: bool,
        echo: Callable[[str], None],
    ) -> None:
        self.dry_run = dry_run
        self.echo = echo
        self.results: list[StepResult] = []

    def shell(self, description: str, argv: Sequence[str]) -> None:
        prefix = "DRY-RUN" if self.dry_run else "RUN"
        self.echo(f"  [{prefix}] {description}: {' '.join(argv)}")
        if not self.dry_run:
            try:
                subprocess.run(list(argv), check=True)
            except (OSError, subprocess.SubprocessError) as exc:
                self._abort(description, exc)
        self.results.append(StepResult(description, ran=not self.dry_run))

    def action(self, description: str, func: Callable[[], None]) -> None:
        prefix = "DRY-RUN" if self.dry_run else "DO"
        self.echo(f"  [{prefix}] {description}")
        if not self.dry_run:
            try:
                func()
            except OSError as exc:
                self._abort(description, exc)
        self.results.append(StepResult(description, ran=not self.dry_run))

    def _abort(self, description: str, exc: Exception) -> NoReturn:
        """Fail the whole setup with a clean error + exit 1 (no traceback)."""

        self.echo(f"  ERROR: step failed — {description}: {exc}")
        if isinstance(exc, PermissionError):
            self.echo("  permission denied — re-run with sudo.")
        self.echo(
            "Setup aborted — the server is NOT fully configured. Fix the issue "
            "above, then re-run 'dtxwiki setup production --reconfigure'."
        )
        raise SystemExit(1)
