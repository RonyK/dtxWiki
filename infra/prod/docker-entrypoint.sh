#!/bin/sh
# dtxWiki container entrypoint.
#
# Ensures the two stateful secrets that production startup guards require exist
# on the data volume and are STABLE across restarts (rotating them would sign
# users out / invalidate tokens), then applies migrations once before exec-ing
# the given command.
#
# Both secrets reach dtxWiki as FILE PATHS that docker-compose.yml sets for
# every service (DTXWIKI_SESSION_SECRET_FILE, DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH),
# never as exported values: `docker compose exec app dtxwiki ...` does not run
# this script, so a value exported here would be invisible to it. Use
# `docker compose run --rm app dtxwiki ...` when the app container is down.
set -eu

DATA_DIR="${DTXWIKI_DATA_DIR:-/var/lib/dtxwiki}"
KEY_PATH="${DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH:-${DATA_DIR}/jwt.pem}"

# Same file-mode policy as the systemd units (UMask=0027): nothing dtxWiki
# writes on the data volume is world-readable.
umask 027
mkdir -p "${DATA_DIR}"

# Stable session secret. An explicit DTXWIKI_SESSION_SECRET always wins (dtxWiki
# reads it before the file); otherwise dtxWiki reads DTXWIKI_SESSION_SECRET_FILE,
# created here once (mktemp makes it 0600, owned by the container user). This
# runs before any `dtxwiki` command because dtxWiki refuses to start while that
# file is missing or empty. Compose runs the `init` service alone first, so app
# and worker never race to create it.
if [ -z "${DTXWIKI_SESSION_SECRET:-}" ]; then
    SECRET_FILE="${DTXWIKI_SESSION_SECRET_FILE:-${DATA_DIR}/session_secret}"
    if [ ! -s "${SECRET_FILE}" ]; then
        secret_tmp="$(mktemp "${SECRET_FILE}.XXXXXX")"
        python -c "import secrets; print(secrets.token_urlsafe(64))" > "${secret_tmp}"
        mv -f "${secret_tmp}" "${SECRET_FILE}"
    fi
    export DTXWIKI_SESSION_SECRET_FILE="${SECRET_FILE}"
fi

# Stable OAuth/MCP signing key — generated once, persisted on the data volume.
# Never overwritten (rotating invalidates every issued token).
if [ ! -f "${KEY_PATH}" ]; then
    dtxwiki keygen --output "${KEY_PATH}"
fi
export DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH="${KEY_PATH}"

# Apply migrations once. `dtxwiki bootstrap` is the operator maintenance
# command (allowed while DTXWIKI_AUTO_BOOTSTRAP=false); sidecars (app/worker) set
# SKIP_BOOTSTRAP=1 and wait on the init service instead. NB: the flag is
# deliberately NOT DTXWIKI_-prefixed — the production startup guard rejects
# unknown DTXWIKI_* env, so a DTXWIKI_SKIP_BOOTSTRAP would crash-loop the app.
if [ "${SKIP_BOOTSTRAP:-0}" != "1" ]; then
    dtxwiki bootstrap
fi

exec "$@"
