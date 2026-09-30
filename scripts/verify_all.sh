#!/usr/bin/env bash
# Verify operator release evidence. Use --gate-c-only to check the
# Gate C (operator readiness) slice. Sprint/phase tracking is delegated
# to git history; this script targets the current contract on HEAD.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Pytest log directory — auto-captured by CI (.github/workflows/ci.yml
# packaging job) or by manual local runs that tee output here.
# Operator audit artifacts (vm-smoke.md) live in internal/audit/.
EVIDENCE_DIR="${EVIDENCE_DIR:-$ROOT_DIR/.ci-evidence}"
OPERATOR_AUDIT_DIR="${OPERATOR_AUDIT_DIR:-$ROOT_DIR/internal/audit}"

gate_c_only=0
if [ "${1:-}" = "--gate-c-only" ] || [ "${1:-}" = "--phase6-only" ]; then
  # --phase6-only kept as a deprecated alias for historical callers.
  gate_c_only=1
elif [ "${1:-}" != "" ]; then
  echo "Usage: $0 [--gate-c-only]" >&2
  exit 2
fi

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="$PYTHON"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN="python"
fi

failures=0

pass() {
  echo "PASS: $*"
}

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

require_file() {
  local path="$1"
  if [ -f "$path" ]; then
    pass "file exists: ${path#$ROOT_DIR/}"
  else
    fail "missing file: ${path#$ROOT_DIR/}"
    return 1
  fi
}

require_grep() {
  local pattern="$1"
  local path="$2"
  local label="$3"
  if grep -Eq "$pattern" "$path"; then
    pass "$label"
  else
    fail "$label"
  fi
}

reject_grep() {
  local pattern="$1"
  local path="$2"
  local label="$3"
  if grep -Eiq "$pattern" "$path"; then
    fail "$label"
  else
    pass "$label"
  fi
}

run_gate_c_tests() {
  (
    cd "$ROOT_DIR"
    "$PYTHON_BIN" -m pytest \
      backend/tests/unit/test_health.py \
      backend/tests/integration/test_request_log.py \
      backend/tests/unit/test_metrics_endpoint.py \
      -q
  )
}

check_gate_c_log() {
  local path="$1"
  local passed_pattern="$2"
  local label="$3"
  require_file "$path" || return
  require_grep "$passed_pattern" "$path" "$label"
  reject_grep "[1-9][0-9]*[[:space:]]+(failed|errors?|skipped|xfailed|xpassed)" "$path" "$label has no failed/skipped summary"
}

verify_gate_c() {
  local health_log="$EVIDENCE_DIR/health.log"
  local request_log="$EVIDENCE_DIR/request-log.log"
  local metrics_log="$EVIDENCE_DIR/metrics.log"
  local vm_log="$OPERATOR_AUDIT_DIR/vm-smoke.md"

  echo "== Gate C (operator readiness) checks =="
  if run_gate_c_tests; then
    pass "Gate C pytest selection"
  else
    fail "Gate C pytest selection"
  fi

  check_gate_c_log "$health_log" "(^|[=[:space:]])11 passed([[:space:]]|$)" "Gate C.14 health evidence"
  if [ -f "$health_log" ]; then
    require_grep "test_live_no_db_call[[:space:]]+PASSED" "$health_log" "C14 live has no DB dependency test"
    require_grep "test_existing_api_v1_health_compat[[:space:]]+PASSED" "$health_log" "C14 legacy compat test"
    require_grep "test_ready_mcp_na_in_json_stateless_200[[:space:]]+PASSED" "$health_log" "C14 json_stateless mode-aware test"
    require_grep "test_ready_mcp_manager_missing_in_streamable_503[[:space:]]+PASSED" "$health_log" "C14 streamable manager test"
  fi

  check_gate_c_log "$request_log" "(^|[=[:space:]])4 passed([[:space:]]|$)" "Gate C.15 request-log evidence"
  if [ -f "$request_log" ]; then
    require_grep "test_log_redacts_authorization_header[[:space:]]+PASSED" "$request_log" "C15 authorization redaction test"
    require_grep "test_log_uses_proxy_header_remote_ip[[:space:]]+PASSED" "$request_log" "C15 proxy remote-ip test"
  fi

  check_gate_c_log "$metrics_log" "(^|[=[:space:]])7 passed([[:space:]]|$)" "Gate C.16 metrics evidence"
  if [ -f "$metrics_log" ]; then
    require_grep "test_disabled_returns_403_not_html[[:space:]]+PASSED" "$metrics_log" "C16 disabled metrics is not HTML"
    require_grep "test_three_counters_present[[:space:]]+PASSED" "$metrics_log" "C16 three counters test"
    require_grep "test_no_user_label_cardinality[[:space:]]+PASSED" "$metrics_log" "C16 cardinality test"
  fi

  require_file "$vm_log" || return
  require_grep 'Timestamp: `20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+09:00`' "$vm_log" "C17 KST timestamp"
  require_grep "Ubuntu 22\\.04" "$vm_log" "C17 Ubuntu 22.04 environment"
  require_grep "Step 1 .* OK" "$vm_log" "C17 step 1 OK"
  require_grep "Step 2 .* OK" "$vm_log" "C17 step 2 OK"
  require_grep "Step 3 .* OK" "$vm_log" "C17 step 3 OK"
  require_grep "Step 4 .* OK" "$vm_log" "C17 step 4 OK"
  require_grep "Step 4\\.5 .* OK" "$vm_log" "C17 step 4.5 OK"
  require_grep "Step 5 .* OK" "$vm_log" "C17 step 5 OK"
  require_grep "Step 6 .* OK" "$vm_log" "C17 step 6 OK"
  require_grep "Step 7 .* OK" "$vm_log" "C17 step 7 OK"
  require_grep "Step 8 .* OK" "$vm_log" "C17 step 8 OK"
  require_grep 'health_live=\{"status":"alive","version":"[^"]+"\}' "$vm_log" "C17 /health/live output"
  require_grep 'health_ready=\{"status":"ready","components":\{"db":"ok","app_version":"ok","mcp":"ok","oauth_jwk":"ok"\}\}' "$vm_log" "C17 /health/ready output"
  require_grep "HTTP/1\\.1 401 Unauthorized" "$vm_log" "C17 /mcp 401 output"
  require_grep "ALL CHECKS PASSED \\(https://dtxwiki\\.test\\)" "$vm_log" "C17 production smoke passed"
  reject_grep "<placeholder>|-----BEGIN|Bearer [A-Za-z0-9._~-]{24,}|access_token|refresh_token" "$vm_log" "C17 secret or placeholder leakage"
}

verify_release_smoke() {
  echo "== Release smoke pre-checks =="
  if [ ! -x "$ROOT_DIR/scripts/release_gate_check.sh" ]; then
    fail "missing scripts/release_gate_check.sh"
    return
  fi
  # `--with-evidence` is not optional here. This script is the operator's
  # "verify release evidence" command, and the gate's default mode was made
  # PUBLIC (clean-clone runnable) in Phase 6 -- which means a bare call now
  # prints "SKIP: release evidence checks" and then "Release gate check: OK",
  # having verified no remote smoke, no CLI suite, no security headers, no
  # client smoke log and no Sprint-5 overviews. That is the opposite of what
  # this function is for. The gate exits 2 when evidence mode is requested but
  # its inputs are not configured, so a missing DTXWIKI_* here fails loudly
  # rather than reporting a release as verified.
  "$ROOT_DIR/scripts/release_gate_check.sh" --with-evidence
}

# I18N-05: check:i18n runs in CI (.github/workflows/ci.yml) but was missing
# from both local verify scripts, so a translation regression could pass
# every gate a contributor actually runs before pushing.
verify_i18n() {
  echo "== i18n locale coverage =="
  if (cd "$ROOT_DIR" && node scripts/check-i18n-coverage.mjs); then
    pass "i18n locale coverage"
  else
    fail "i18n locale coverage"
  fi
}

main() {
  if [ "$gate_c_only" -eq 0 ]; then
    verify_release_smoke || failures=$((failures + 1))
  fi
  verify_i18n
  verify_gate_c

  if [ "$failures" -eq 0 ]; then
    echo "Release verification: OK"
    exit 0
  fi
  echo "Release verification: FAILED ($failures issue(s))" >&2
  exit 1
}

main
