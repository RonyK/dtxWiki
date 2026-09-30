#!/usr/bin/env bash
# Run remote MCP smoke on a Docker-capable host.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/compose.remote.yml"
DOCKERFILE="${DTXWIKI_REMOTE_E2E_DOCKERFILE:-$ROOT_DIR/Dockerfile}"
PROJECT_NAME="${DTXWIKI_REMOTE_E2E_PROJECT:-dtxwiki-remote-e2e}"
IMAGE_NAME="${DTXWIKI_REMOTE_E2E_IMAGE:-dtxwiki:test}"
REMOTE_CLIENT="${DTXWIKI_REMOTE_CLIENT:-codex-oauth-harness}"
PREFLIGHT=0

if [ "${1:-}" = "--preflight" ]; then
  PREFLIGHT=1
  shift
fi

EVIDENCE_FILE="${1:-$ROOT_DIR/.ci-evidence/remote-smoke.log}"

compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose "$@"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
    return
  fi
  echo "ERROR: Docker Compose is required for remote MCP smoke." >&2
  exit 2
}

require_docker_cli() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi
  cat >&2 <<'EOF'
ERROR: docker CLI is required to build the remote MCP smoke image.

Install Docker on the smoke host, or set DTXWIKI_REMOTE_E2E_BUILD=0 to reuse
an existing image on a host where Docker Compose is available.
EOF
  exit 2
}

require_dockerfile() {
  if [ -f "$DOCKERFILE" ]; then
    return
  fi
  cat >&2 <<'EOF'
ERROR: Dockerfile is required to build the remote MCP smoke image.

Create a production-capable image definition at the repository root, or set
DTXWIKI_REMOTE_E2E_BUILD=0 to reuse an existing image on a Docker Compose host.
EOF
  exit 2
}

require_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    return
  fi
  echo "ERROR: Docker Compose is required for remote MCP smoke." >&2
  exit 2
}

python_bin() {
  if [ -n "${DTXWIKI_PYTHON:-}" ]; then
    printf '%s\n' "$DTXWIKI_PYTHON"
    return
  fi
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  return 1
}

hostname_resolves() {
  if command -v getent >/dev/null 2>&1; then
    if getent hosts dtxwiki.test >/dev/null 2>&1; then
      getent hosts dtxwiki.test | awk '{print $1}' | grep -qx '127.0.0.1'
      return
    fi
  fi
  if [ -r /etc/hosts ] && command -v grep >/dev/null 2>&1; then
    grep -Eq '(^|[[:space:]])127\.0\.0\.1[[:space:]].*dtxwiki\.test' /etc/hosts
    return
  fi
  return 1
}

require_hostname() {
  if hostname_resolves; then
    return
  fi
  cat >&2 <<'EOF'
ERROR: dtxwiki.test must resolve to 127.0.0.1 before remote smoke.

Run this once on the Docker-capable host:
  echo "127.0.0.1 dtxwiki.test" | sudo tee -a /etc/hosts
EOF
  exit 2
}

require_local_pytest() {
  local py
  if ! py="$(python_bin)"; then
    echo "ERROR: python3 or python is required on the smoke host." >&2
    exit 2
  fi
  if ! "$py" -m pytest --version >/dev/null 2>&1; then
    echo "ERROR: pytest is required on the smoke host Python: $py" >&2
    exit 2
  fi
}

has_docker_cli() {
  command -v docker >/dev/null 2>&1
}

has_dockerfile() {
  [ -f "$DOCKERFILE" ]
}

has_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  command -v docker-compose >/dev/null 2>&1
}

has_local_pytest() {
  local py
  py="$(python_bin)" || return 1
  "$py" -m pytest --version >/dev/null 2>&1
}

has_file() {
  [ -f "$1" ]
}

preflight_check() {
  local label="$1"
  shift
  if "$@"; then
    echo "OK: $label"
  else
    echo "MISSING: $label" >&2
    preflight_failures=1
  fi
}

run_preflight() {
  preflight_failures=0
  if [ "${DTXWIKI_REMOTE_E2E_BUILD:-1}" = "1" ]; then
    preflight_check "docker CLI for image build" has_docker_cli
    preflight_check "Dockerfile for image build" has_dockerfile
  else
    echo "OK: docker image build disabled by DTXWIKI_REMOTE_E2E_BUILD=0"
  fi
  preflight_check "Docker Compose runtime" has_compose
  preflight_check "dtxwiki.test resolves to 127.0.0.1" hostname_resolves
  preflight_check "host Python has pytest" has_local_pytest
  preflight_check "remote compose file" has_file "$COMPOSE_FILE"
  preflight_check "remote smoke pytest file" has_file "$ROOT_DIR/tests/e2e/remote_mcp_smoke.py"
  preflight_check "evidence redaction script" has_file "$ROOT_DIR/scripts/redact_evidence.sh"
  if [ "$preflight_failures" -ne 0 ]; then
    echo "remote MCP smoke preflight: FAILED" >&2
    exit 2
  fi
  echo "remote MCP smoke preflight: OK"
}

main() {
  cd "$ROOT_DIR"
  if [ "$PREFLIGHT" = "1" ]; then
    run_preflight
    return
  fi
  if [ "${DTXWIKI_REMOTE_E2E_BUILD:-1}" = "1" ]; then
    require_docker_cli
    require_dockerfile
  fi
  require_compose
  require_hostname
  require_local_pytest

  if [ "${DTXWIKI_REMOTE_E2E_BUILD:-1}" = "1" ]; then
    docker build -f "$DOCKERFILE" -t "$IMAGE_NAME" "$ROOT_DIR"
  fi

  if [ "${DTXWIKI_REMOTE_E2E_RESET:-0}" = "1" ]; then
    compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true
  fi

  remote_env="$(mktemp "${TMPDIR:-/tmp}/dtxwiki-remote-smoke.XXXXXX.env")"
  chmod 600 "$remote_env"
  cleanup_compose=1
  cleanup() {
    rm -f "$remote_env"
    if [ "$cleanup_compose" = "1" ]; then
      compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true
    fi
  }
  trap cleanup EXIT

  export DTXWIKI_REMOTE_E2E_IMAGE="$IMAGE_NAME"
  export DTXWIKI_REMOTE_E2E_PROJECT="$PROJECT_NAME"

  compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" run --rm dtxwiki \
    python -m backend.scripts.seed_remote_mcp_smoke --format env \
    > "$remote_env"

  set -a
  # shellcheck disable=SC1090
  . "$remote_env"
  set +a

  mkdir -p "$(dirname "$EVIDENCE_FILE")"
  {
    echo "DTXWIKI_REMOTE_SMOKE_MODE=remote"
    echo "DTXWIKI_REMOTE_CLIENT=$REMOTE_CLIENT"
    echo "DTXWIKI_REMOTE_HARNESS=docker-compose"
    echo "DTXWIKI_REMOTE_ORIGIN=${DTXWIKI_REMOTE_PUBLIC_BASE_URL:-https://dtxwiki.test:8443}"
    echo "DTXWIKI_REMOTE_TRANSPORT=streamable"
    echo "DTXWIKI_REMOTE_TLS_VERIFY=ca-file"
    "$(python_bin)" -m pytest -m cross_transport_remote tests/e2e/remote_mcp_smoke.py -v \
      --remote-smoke-mode=remote \
      --remote-client="$REMOTE_CLIENT"
  } 2>&1 | bash scripts/redact_evidence.sh > "$EVIDENCE_FILE"

  grep -Fxq "DTXWIKI_REMOTE_SMOKE_MODE=remote" "$EVIDENCE_FILE"
  grep -Fxq "DTXWIKI_REMOTE_CLIENT=$REMOTE_CLIENT" "$EVIDENCE_FILE"
  grep -Fxq "DTXWIKI_REMOTE_HARNESS=docker-compose" "$EVIDENCE_FILE"
  grep -Fxq "DTXWIKI_REMOTE_TRANSPORT=streamable" "$EVIDENCE_FILE"
  grep -Fxq "DTXWIKI_REMOTE_TLS_VERIFY=ca-file" "$EVIDENCE_FILE"
  remote_nodes=(
    test_pat_immediate_connect_list_tools
    test_pat_read_page_round_trip
    test_pat_write_agent_page_draft_round_trip
    test_oauth_full_flow_tool_round_trip
    test_oauth_refresh_rotation_and_reuse_denied_via_jti
    test_oauth_revoke_invalidates_access_jti
    test_cross_workspace_denied_with_keyword
    test_restricted_library_denied_with_keyword
  )
  for node in "${remote_nodes[@]}"; do
    grep -Eq "remote_mcp_smoke.py::$node[[:space:]]+PASSED" "$EVIDENCE_FILE"
  done
  grep -Eq "(^|[=[:space:]])8 passed([,[:space:]]|$)" "$EVIDENCE_FILE"
  compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1
  cleanup_compose=0
  echo "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" >> "$EVIDENCE_FILE"
  grep -Fxq "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" "$EVIDENCE_FILE"
  echo "remote MCP smoke evidence written: $EVIDENCE_FILE"
}

main "$@"
