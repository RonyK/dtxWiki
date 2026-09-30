#!/usr/bin/env bash
# infra/prod/install.sh
# One-command dtxWiki production install for a fresh Ubuntu 22.04+ VM.
#
#   # Copy a verified wheel without renaming it + this script from the same
#   # source checkout first. pip validates wheel filenames.
#   chmod 0644 /tmp/dtxwiki-<version>-py3-none-any.whl
#   sudo bash /tmp/install.sh --domain wiki.example.com --email admin@example.com \
#       --tls acme --wheel /tmp/dtxwiki-<version>-py3-none-any.whl
#
# Does only what a not-yet-installed CLI cannot: system packages + system user +
# venv + wheel install, plus stable `dtxwiki`/`dtxwiki-worker` wrappers on
# PATH. It then hands off to `dtxwiki setup production` (keygen, session secret,
# env file, database bootstrap + admin, systemd units, Caddyfile, smoke).
# Re-run safe.
#
# The service user ('dtxwiki') and install dir ('/opt/dtxwiki') are FIXED to
# match the bundled systemd units (infra/prod/dtxwiki*.service). Do not
# parameterize them without also templating those units in `setup production`.
set -euo pipefail

DOMAIN=""
EMAIL=""
TLS=""                        # acme|internal|custom|external|none (default acme)
TLS_CERT=""                   # certificate path (custom mode)
TLS_KEY=""                    # private key path (custom mode)
INSECURE_EVAL=""              # set to confirm the eval-only 'none' (plain HTTP) mode
FORWARDED_IP=""               # upstream proxy IP to trust (external mode)
BIND_HOST=""                  # app listen address (setup's default: 127.0.0.1)
WHEEL=""                      # recommended now: verified local release wheel
INDEX_URL=""                  # optional: private package index for `pip install`
PYPI_SPEC="dtxwiki"           # package name used with a trusted private index
DTXWIKI_USER="dtxwiki"        # must match infra/prod/dtxwiki*.service
INSTALL_DIR="/opt/dtxwiki"    # must match infra/prod/dtxwiki*.service
DATA_DIR="/var/lib/dtxwiki"   # must match infra/prod/dtxwiki*.service
# Caddy's official apt repository (Cloudsmith "stable"). Added only when the
# distribution itself has no caddy package, which is the case on Ubuntu 22.04.
CADDY_REPO_URL="https://dl.cloudsmith.io/public/caddy/stable"
CADDY_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
CADDY_SOURCES="/etc/apt/sources.list.d/caddy-stable.list"
SETUP_ARGS=()

usage() {
    local status="${1:-1}"
    cat >&2 <<EOF
Usage: install.sh --domain <host> --email <admin-email> [--tls <mode>] [...]

Required:
  --domain <host>   Server name or IP. For --tls acme it must be a public FQDN
                    whose DNS A record points here; for internal/custom/external
                    it may be an internal name (wiki.lan) or IP.
  --email <email>   Initial admin (owner) account email.

TLS / certificate (default: acme):
  --tls <mode>      acme | internal | custom | external | none
                      acme     Caddy auto Let's Encrypt (public domain, 80/443).
                      internal Caddy local CA (no public domain); clients trust
                               the exported root CA. Steps are printed afterward.
                      custom   Bring your own cert: --tls-cert + --tls-key.
                      external Existing proxy/LB terminates TLS (Caddy skipped);
                               optionally --forwarded-ip <proxy-ip>.
                      none     Plain HTTP, EVAL ONLY (requires --insecure-eval).
  --tls-cert <path> Certificate file (custom mode).
  --tls-key <path>  Private key file (custom mode).
  --forwarded-ip <ip>  Upstream proxy IP to trust (external mode).
  --bind-host <addr>   Address dtxWiki listens on (default 127.0.0.1). Only for
                       --tls external with a proxy/LB on another machine: use
                       this host's private address (or 0.0.0.0) together with
                       --forwarded-ip <that proxy's address>.
  --insecure-eval   Confirm the eval-only plain-HTTP ('none') mode.

Install source (choose exactly one):
  --wheel <path>      Install this verified local wheel (current release path).
  --index-url <url>   Install '${PYPI_SPEC}' and dependencies from this trusted
                      private package index or PyPI mirror.
  -h, --help          Show this help.

The wheel pins the dtxWiki artifact, but pip may resolve its dependencies from
the configured default index. For an isolated network, use a trusted mirror
containing dtxWiki and all dependencies via --index-url.

Note: the service user ('${DTXWIKI_USER}') and install dir ('${INSTALL_DIR}') are
fixed to match the bundled systemd units.
EOF
    exit "$status"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --domain) DOMAIN="${2:?--domain requires a host}"; shift 2 ;;
            --email) EMAIL="${2:?--email requires an address}"; shift 2 ;;
            --tls) TLS="${2:?--tls requires a mode}"; shift 2 ;;
            --tls-cert) TLS_CERT="${2:?--tls-cert requires a path}"; shift 2 ;;
            --tls-key) TLS_KEY="${2:?--tls-key requires a path}"; shift 2 ;;
            --forwarded-ip) FORWARDED_IP="${2:?--forwarded-ip requires an IP}"; shift 2 ;;
            --bind-host) BIND_HOST="${2:?--bind-host requires an address}"; shift 2 ;;
            --insecure-eval) INSECURE_EVAL="1"; shift ;;
            --wheel) WHEEL="${2:?--wheel requires a path}"; shift 2 ;;
            --index-url) INDEX_URL="${2:?--index-url requires a URL}"; shift 2 ;;
            -h|--help) usage 0 ;;
            *) echo "unknown argument: $1" >&2; usage ;;
        esac
    done
}

# Arguments handed to `dtxwiki setup production`. --service-user must match the
# systemd unit User so the JWT key and env file are group-readable by it.
setup_production_args() {
    SETUP_ARGS=(--domain "$DOMAIN" --email "$EMAIL" --service-user "$DTXWIKI_USER")
    if [[ -n "$TLS" ]]; then SETUP_ARGS+=(--tls "$TLS"); fi
    if [[ -n "$TLS_CERT" ]]; then SETUP_ARGS+=(--tls-cert "$TLS_CERT"); fi
    if [[ -n "$TLS_KEY" ]]; then SETUP_ARGS+=(--tls-key "$TLS_KEY"); fi
    if [[ -n "$FORWARDED_IP" ]]; then SETUP_ARGS+=(--forwarded-ip "$FORWARDED_IP"); fi
    if [[ -n "$BIND_HOST" ]]; then SETUP_ARGS+=(--bind-host "$BIND_HOST"); fi
    if [[ -n "$INSECURE_EVAL" ]]; then SETUP_ARGS+=(--insecure-eval); fi
}

has_python312() {
    command -v python3.12 >/dev/null 2>&1
}

# The version apt would install for a package, or nothing when the configured
# archives have no candidate. LC_ALL=C: the "Candidate:" label is translated.
apt_candidate() {
    LC_ALL=C apt-cache policy "$1" 2>/dev/null \
        | awk '$1 == "Candidate:" && $2 != "(none)" { print $2; exit }'
}

# Caddy's documented Debian/Ubuntu install: the repository is trusted only
# through its own keyring (signed-by), never through the global apt keyring.
# The sources line is written here rather than downloaded.
add_caddy_apt_repo() {
    apt-get install -y ca-certificates curl gnupg
    curl -fsSL "${CADDY_REPO_URL}/gpg.key" | gpg --dearmor --yes -o "$CADDY_KEYRING"
    chmod 0644 "$CADDY_KEYRING"
    printf 'deb [signed-by=%s] %s/deb/debian any-version main\n' \
        "$CADDY_KEYRING" "$CADDY_REPO_URL" > "$CADDY_SOURCES"
    chmod 0644 "$CADDY_SOURCES"
    apt-get update -y
}

install_system_packages() {
    apt-get update -y
    # Ubuntu 22.04 needs deadsnakes for python3.12; 24.04+ has it in base.
    if ! has_python312; then
        apt-get install -y software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -y
    fi
    local packages=(python3.12 python3.12-venv sqlite3 curl)
    # tls=external means an existing reverse proxy / load balancer terminates
    # TLS, so dtxWiki does not manage Caddy; installing it would contend for
    # ports 80/443 with the operator's proxy (merged I6).
    if [[ "$TLS" == "external" ]]; then
        echo "    tls=external: skipping Caddy (your proxy/LB terminates TLS)"
    else
        if [[ -z "$(apt_candidate caddy)" ]]; then
            echo "    no caddy package in this distribution: adding Caddy's official apt repository"
            add_caddy_apt_repo
        fi
        packages+=(caddy)
    fi
    apt-get install -y "${packages[@]}"
}

# Sourced (tests): stop after the definitions above.
if [[ "${BASH_SOURCE[0]:-$0}" != "$0" ]]; then
    # shellcheck disable=SC2317  # reachable when the file is sourced
    return 0
fi

parse_args "$@"

[[ -n "$DOMAIN" ]] || { echo "ERROR: --domain is required" >&2; usage; }
[[ -n "$EMAIL" ]] || { echo "ERROR: --email is required" >&2; usage; }
[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }
if [[ -n "$WHEEL" && -n "$INDEX_URL" ]]; then
    echo "ERROR: --wheel and --index-url are mutually exclusive." >&2
    exit 1
fi
if [[ -n "$WHEEL" ]]; then
    [[ -f "$WHEEL" ]] || {
        echo "ERROR: --wheel file does not exist: ${WHEEL}" >&2
        exit 1
    }
    WHEEL="$(readlink -f -- "$WHEEL")"
elif [[ -z "$INDEX_URL" ]]; then
    echo "ERROR: one of --wheel or --index-url is required for this release." >&2
    echo "  Build a verified artifact with 'make test-packaged' and pass --wheel." >&2
    exit 1
fi

VENV="${INSTALL_DIR}/.venv"
PREVIOUS_VENV="${INSTALL_DIR}/.venv.previous.$$"
PIP="${VENV}/bin/pip"
DTXWIKI="${VENV}/bin/dtxwiki"
PYTHON="${VENV}/bin/python"
VENV_MUTATED=""

# Called with the failing command's status (the ERR trap passes it explicitly).
restore_venv() {
    local status="$1"
    trap - ERR
    if [[ -n "$VENV_MUTATED" ]]; then
        rm -rf "$VENV"
        if [[ -d "$PREVIOUS_VENV" ]]; then
            mv "$PREVIOUS_VENV" "$VENV"
        fi
    fi
    exit "$status"
}
trap 'restore_venv $?' ERR

echo "==> [1/5] System packages (python3.12, venv, caddy)"
install_system_packages

echo "==> [2/5] System user + directories"
id -u "$DTXWIKI_USER" >/dev/null 2>&1 \
    || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$DTXWIKI_USER"
mkdir -p "$INSTALL_DIR" /etc/dtxwiki "$DATA_DIR"
chown -R "$DTXWIKI_USER:$DTXWIKI_USER" "$INSTALL_DIR" "$DATA_DIR"
chown root:"$DTXWIKI_USER" /etc/dtxwiki
chmod 750 /etc/dtxwiki "$DATA_DIR"
if [[ -n "$WHEEL" ]] && ! sudo -u "$DTXWIKI_USER" test -r "$WHEEL"; then
    echo "ERROR: wheel is not readable by service user '${DTXWIKI_USER}': ${WHEEL}" >&2
    echo "  Copy it to /tmp and run: chmod 0644 /tmp/$(basename "$WHEEL")" >&2
    exit 1
fi

echo "==> [3/5] venv + dtxWiki install"
rm -rf "$PREVIOUS_VENV"
if [[ -d "$VENV" ]]; then
    cp -a "$VENV" "$PREVIOUS_VENV"
fi
VENV_MUTATED="1"
if [[ ! -x "${VENV}/bin/python" ]]; then
    sudo -u "$DTXWIKI_USER" python3.12 -m venv "$VENV"
fi
PIP_INDEX_ARGS=()
if [[ -n "$INDEX_URL" ]]; then
    PIP_INDEX_ARGS=(--index-url "$INDEX_URL")
fi
sudo -u "$DTXWIKI_USER" "$PIP" install "${PIP_INDEX_ARGS[@]}" --upgrade pip
if [[ -n "$WHEEL" ]]; then
    # Operators sometimes rename a transferred artifact (for example,
    # dtxwiki-release.whl). pip correctly rejects that non-wheel filename even
    # when its contents are valid. Derive a canonical filename from the wheel's
    # own metadata and stage it in a root-owned directory before installation.
    WHEEL_STAGE_DIR="/usr/local/share/dtxwiki/wheels"
    install -d -o root -g root -m 0755 "$WHEEL_STAGE_DIR"
    WHEEL_BASENAME="$("$PYTHON" - "$WHEEL" <<'PY'
from __future__ import annotations

from email.parser import BytesParser
import re
import sys
from zipfile import ZipFile

wheel_path = sys.argv[1]
with ZipFile(wheel_path) as archive:
    names = archive.namelist()
    metadata_path = next((name for name in names if name.endswith(".dist-info/METADATA")), None)
    wheel_path_meta = next((name for name in names if name.endswith(".dist-info/WHEEL")), None)
    if metadata_path is None or wheel_path_meta is None:
        raise SystemExit("wheel metadata is missing")
    metadata = BytesParser().parsebytes(archive.read(metadata_path))
    name = metadata.get("Name")
    version = metadata.get("Version")
    tag = next(
        (line.partition(":")[2].strip() for line in archive.read(wheel_path_meta).decode("utf-8").splitlines()
         if line.startswith("Tag:")),
        None,
    )

if not name or not version or not tag:
    raise SystemExit("wheel metadata is incomplete")
distribution = re.sub(r"[-_.]+", "_", name)
version = version.replace("-", "_")
if not re.fullmatch(r"[A-Za-z0-9_]+", distribution):
    raise SystemExit("wheel distribution is unsafe")
if not re.fullmatch(r"[A-Za-z0-9_.+!]+", version):
    raise SystemExit("wheel version is unsafe")
if not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
    raise SystemExit("wheel tag is unsafe")
print(f"{distribution}-{version}-{tag}.whl")
PY
)"
    PIP_WHEEL="${WHEEL_STAGE_DIR}/${WHEEL_BASENAME}"
    install -o root -g root -m 0644 "$WHEEL" "$PIP_WHEEL"
    sudo -u "$DTXWIKI_USER" "$PIP" install --force-reinstall "$PIP_WHEEL"
elif ! sudo -u "$DTXWIKI_USER" "$PIP" install "${PIP_INDEX_ARGS[@]}" "$PYPI_SPEC"; then
    echo "ERROR: 'pip install ${PYPI_SPEC}' failed." >&2
    echo "  If this release is not available from the selected index, install a local wheel:" >&2
    echo "    sudo bash install.sh --domain ${DOMAIN} --email ${EMAIL} --wheel /path/to/dtxwiki-<version>-py3-none-any.whl" >&2
    # Codex #22: build the wheel with 'make build-wheel', NOT raw 'python -m build'.
    # backend/static (the bundled SPA) is gitignored, so a raw build on a fresh
    # checkout produces a wheel with no frontend. 'make build-wheel' runs
    # bundle-frontend first so the wheel includes static/index.html.
    echo "  (build one in a checkout with:  make build-wheel  ->  dist/dtxwiki-*.whl)" >&2
    restore_venv 1
fi
sudo -u "$DTXWIKI_USER" "$DTXWIKI" --version
# The new code is verified: from here on a failure must NOT swap the previous
# venv back. `setup production` may already have migrated the database or
# enabled units that point at this venv, and older code refuses a newer
# database. Fix the reported problem and re-run instead.
rm -rf "$PREVIOUS_VENV"
VENV_MUTATED=""
trap - ERR

echo "==> [4/5] PATH wrappers (so 'sudo dtxwiki ...' Day-2 commands work)"
# The systemd units load /etc/dtxwiki/dtxwiki.env, but an interactive root CLI
# invocation does not. A plain symlink here would silently select root's default
# SQLite database, which is especially dangerous for user-management commands.
# Load the production env for every Day-2 command. Commands that open the
# database or write the data directory then drop to the service account, so
# SQLite files and their WAL sidecars keep the ownership the backend and worker
# need. setup/config/keygen/update/rollback stay root: they write /etc or drive
# systemctl.
rm -f /usr/local/bin/dtxwiki /usr/local/bin/dtxwiki-worker
install -o root -g root -m 0755 /dev/stdin /usr/local/bin/dtxwiki <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

PROD_ENV=/etc/dtxwiki/dtxwiki.env
CLI=/opt/dtxwiki/.venv/bin/dtxwiki
SERVICE_USER=dtxwiki
SERVICE_HOME=/opt/dtxwiki

if [[ -r "$PROD_ENV" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$PROD_ENV"
    set +a
fi

command=""
if (( $# > 0 )); then
    command="$1"
fi

case "$command" in
    ""|serve|bootstrap|users|workspace|admin|context|reindex|eval|info|migrate-data)
        exec setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_USER" --init-groups \
            env HOME="$SERVICE_HOME" "$CLI" "$@"
        ;;
    *)
        exec "$CLI" "$@"
        ;;
esac
EOF

install -o root -g root -m 0755 /dev/stdin /usr/local/bin/dtxwiki-worker <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

PROD_ENV=/etc/dtxwiki/dtxwiki.env
WORKER=/opt/dtxwiki/.venv/bin/dtxwiki-worker

if [[ -r "$PROD_ENV" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$PROD_ENV"
    set +a
fi

exec setpriv --reuid=dtxwiki --regid=dtxwiki --init-groups \
    env HOME=/opt/dtxwiki "$WORKER" "$@"
EOF

echo "==> [5/5] Application setup (dtxwiki setup production)"
# setup is idempotent — safe to re-run for reconfiguration. Its order matters:
# keygen + session secret, env file (root:dtxwiki 0640), database bootstrap +
# initial admin with the data directory handed to the service account, and only
# THEN the systemd units are enabled and (re)started — with
# DTXWIKI_AUTO_BOOTSTRAP=false dtxWiki refuses to start on a database that is
# not at its schema head. Caddyfile and smoke come last.
setup_production_args
if ! "$DTXWIKI" setup production "${SETUP_ARGS[@]}"; then
    echo "ERROR: 'dtxwiki setup production' did not finish (see above)." >&2
    echo "  dtxWiki itself is installed in ${VENV}. Fix the reported problem, then re-run" >&2
    echo "  this installer or: sudo dtxwiki setup production --reconfigure --domain ${DOMAIN}" >&2
    exit 1
fi

echo ""
SCHEME="https"; [[ "$TLS" == "none" ]] && SCHEME="http"
echo "Done. If preflight + smoke passed, visit: ${SCHEME}://${DOMAIN}"
case "$TLS" in
    internal) echo "internal CA: install the root CA on each client (steps printed above)." ;;
    external) echo "external TLS: point your existing proxy at port 21080 of this host (dtxWiki listens on ${BIND_HOST:-127.0.0.1})." ;;
    none) echo "WARNING: plain HTTP (eval only) — auth travels in plaintext." ;;
esac
echo "Change configuration later (now on PATH): 'sudo dtxwiki config set KEY=VALUE'"
echo "                                      or: 'sudo dtxwiki setup production --reconfigure'"
echo "Set a private admin password now: 'sudo dtxwiki users issue-password-reset ${EMAIL} --delivery link' and open the one-time link."
