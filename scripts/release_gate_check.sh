#!/usr/bin/env bash
# Release gate. Two modes, one script:
#
#   PUBLIC (default, zero arguments): every input it reads is tracked at HEAD, so
#   it runs on a clean clone and actually judges the release. README, DEPLOYMENT,
#   i18n locale coverage and the evidence-redaction policy tests.
#
#   PRIVATE (opt-in): the remote-MCP acceptance evidence checks. Their inputs are
#   hand-made operator artifacts (internal/audit/*.log, the Sprint-5 overviews,
#   the remote client setup guide) that are deliberately NOT in the repository.
#   They have NO repository-relative defaults -- supply all four paths explicitly
#   via flags or DTXWIKI_* environment variables.
#
# Why the split: the private inputs used to be hard-required defaults, so a clean
# checkout got seven `FAIL: missing file:` lines and exit 1 before the gate could
# judge anything it was able to judge. A gate nobody can run gates nothing.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Public inputs: tracked at HEAD, present in any clone.
README_FILE="${DTXWIKI_README:-$ROOT_DIR/README.md}"
DEPLOYMENT_FILE="${DTXWIKI_DEPLOYMENT:-$ROOT_DIR/DEPLOYMENT.md}"

# Private inputs: no defaults on purpose. Empty means "not supplied".
EVIDENCE_DIR="${DTXWIKI_EVIDENCE_DIR:-}"
SETUP_DOC="${DTXWIKI_SETUP_DOC:-}"
PHASE5_OVERVIEW="${DTXWIKI_PHASE5_OVERVIEW:-}"
SPRINT5_OVERVIEW="${DTXWIKI_SPRINT5_OVERVIEW:-}"
CLIENT_LOG="${DTXWIKI_CLIENT_SMOKE_LOG:-}"
CLIENT_VALIDATOR="${DTXWIKI_CLIENT_SMOKE_VALIDATOR:-}"

CLI_EVIDENCE_TEST_FILES=(
  backend/tests/cli/test_mcp_register_http.py
  backend/tests/cli/test_mcp_doctor_http.py
  backend/tests/cli/test_users_issue_pat.py
)

failures=0
remote_evidence_blocker=0
manual_evidence_blocker=0
blocker_summary_printed=0
evidence_mode=0

usage() {
  cat >&2 <<'USAGE'
usage: release_gate_check.sh
       release_gate_check.sh --with-evidence \
           --evidence-dir DIR --setup-doc PATH \
           --phase5-overview PATH --sprint5-overview PATH \
           [--client-smoke-log PATH] [--validator PATH]

With no arguments this runs the public release gate, which reads only files
tracked in this repository.

The evidence gate has no repository defaults, by design: its inputs are private
operator artifacts that no clone contains, and defaulting them to repo paths is
what made this gate unrunnable. Supply all four explicitly, as flags or as
DTXWIKI_EVIDENCE_DIR, DTXWIKI_SETUP_DOC, DTXWIKI_PHASE5_OVERVIEW and
DTXWIKI_SPRINT5_OVERVIEW.
USAGE
}

# Any private input, from either channel, is a request for the evidence gate.
if [ -n "$EVIDENCE_DIR$SETUP_DOC$PHASE5_OVERVIEW$SPRINT5_OVERVIEW$CLIENT_LOG$CLIENT_VALIDATOR" ]; then
  evidence_mode=1
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-evidence)
      evidence_mode=1
      shift
      ;;
    --evidence-dir)
      EVIDENCE_DIR="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    --setup-doc)
      SETUP_DOC="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    --phase5-overview)
      PHASE5_OVERVIEW="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    --sprint5-overview)
      SPRINT5_OVERVIEW="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    --client-smoke-log)
      CLIENT_LOG="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    --validator)
      CLIENT_VALIDATOR="${2:-}"
      evidence_mode=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ "$evidence_mode" = "1" ]; then
  missing_inputs=()
  [ -n "$EVIDENCE_DIR" ] || missing_inputs+=("--evidence-dir")
  [ -n "$SETUP_DOC" ] || missing_inputs+=("--setup-doc")
  [ -n "$PHASE5_OVERVIEW" ] || missing_inputs+=("--phase5-overview")
  [ -n "$SPRINT5_OVERVIEW" ] || missing_inputs+=("--sprint5-overview")
  if [ "${#missing_inputs[@]}" -ne 0 ]; then
    echo "ERROR: release evidence gate is missing: ${missing_inputs[*]}" >&2
    usage
    exit 2
  fi
  # These two have a defensible default only once the required inputs exist.
  CLIENT_LOG="${CLIENT_LOG:-$EVIDENCE_DIR/client-smoke-log.md}"
  CLIENT_VALIDATOR="${CLIENT_VALIDATOR:-$ROOT_DIR/scripts/validate_client_smoke_log.py}"
fi

if [ -n "${DTXWIKI_PYTHON:-}" ]; then
  PYTHON_BIN="$DTXWIKI_PYTHON"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1 && python --version >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "ERROR: no usable Python found; set DTXWIKI_PYTHON" >&2
  exit 1
fi

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

pass() {
  echo "PASS: $*"
}

require_file() {
  local path="$1"
  if [ ! -f "$path" ]; then
    fail "missing file: $path"
    return 1
  fi
  return 0
}

require_grep() {
  local pattern="$1"
  local path="$2"
  local label="$3"
  if grep -Eq "$pattern" "$path"; then
    pass "$label"
  else
    fail "$label not satisfied in $path"
  fi
}

reject_grep() {
  local pattern="$1"
  local path="$2"
  local label="$3"
  if grep -Eiq "$pattern" "$path"; then
    fail "$label found in $path"
  else
    pass "$label absent"
  fi
}

require_heading_count_ge() {
  local path="$1"
  local minimum="$2"
  local label="$3"
  local count
  count="$(grep -c '^## ' "$path" || true)"
  if [ "$count" -ge "$minimum" ]; then
    pass "$label"
  else
    fail "$label expected >= $minimum headings, found $count"
  fi
}

check_log() {
  local path="$1"
  local pattern="$2"
  local label="$3"
  if require_file "$path"; then
    require_grep "$pattern" "$path" "$label"
  fi
}

check_cli_evidence() {
  local path="$1"
  local marker_count
  local test_file

  if ! require_file "$path"; then
    return
  fi
  if grep -Fxq "DTXWIKI_CLI_TRANSPORT=streamable-http" "$path"; then
    pass "CLI suite Streamable HTTP transport marker"
  else
    fail "CLI suite Streamable HTTP transport marker not satisfied in $path"
  fi
  for test_file in "${CLI_EVIDENCE_TEST_FILES[@]}"; do
    if grep -Fxq "DTXWIKI_CLI_TEST_FILE=$test_file" "$path"; then
      pass "CLI suite manifest: $test_file"
    else
      fail "CLI suite manifest missing $test_file in $path"
    fi
  done
  marker_count="$(grep -c '^DTXWIKI_CLI_TEST_FILE=' "$path" || true)"
  if [ "$marker_count" -eq "${#CLI_EVIDENCE_TEST_FILES[@]}" ]; then
    pass "CLI suite manifest contains exactly the supported test files"
  else
    fail "CLI suite manifest contains $marker_count test files, expected ${#CLI_EVIDENCE_TEST_FILES[@]} in $path"
  fi
  if grep -Eq "(^|[=[:space:]])[1-9][0-9]* passed([,[:space:]]|$)" "$path"; then
    pass "CLI suite completed with a nonzero passing result"
  else
    fail "CLI suite has no nonzero passing result in $path"
  fi
  reject_grep "[1-9][0-9]*[[:space:]]+(deselected|skipped|failed|errors?|xfailed|xpassed)" "$path" "CLI suite skip/fail marker"
}

remote_evidence_ready() {
  local path="$1"
  local node
  local remote_nodes=(
    test_pat_immediate_connect_list_tools
    test_pat_read_page_round_trip
    test_pat_write_agent_page_draft_round_trip
    test_oauth_full_flow_tool_round_trip
    test_oauth_refresh_rotation_and_reuse_denied_via_jti
    test_oauth_revoke_invalidates_access_jti
    test_cross_workspace_denied_with_keyword
    test_restricted_library_denied_with_keyword
  )
  [ -f "$path" ] || return 1
  grep -Fxq "DTXWIKI_REMOTE_SMOKE_MODE=remote" "$path" || return 1
  grep -Eq "^DTXWIKI_REMOTE_CLIENT=[A-Za-z0-9._-]+$" "$path" || return 1
  grep -Fxq "DTXWIKI_REMOTE_HARNESS=docker-compose" "$path" || return 1
  grep -Eq "^DTXWIKI_REMOTE_ORIGIN=https://[^[:space:]]+$" "$path" || return 1
  grep -Fxq "DTXWIKI_REMOTE_TRANSPORT=streamable" "$path" || return 1
  grep -Fxq "DTXWIKI_REMOTE_TLS_VERIFY=ca-file" "$path" || return 1
  grep -Fxq "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" "$path" || return 1
  grep -Eq "(^|[=[:space:]])8 passed([,[:space:]]|$)" "$path" || return 1
  for node in "${remote_nodes[@]}"; do
    grep -Eq "remote_mcp_smoke.py::$node[[:space:]]+PASSED" "$path" || return 1
  done
  if grep -Eiq "DTXWIKI_REMOTE_SMOKE_MODE=local|--remote-smoke-mode=local" "$path"; then
    return 1
  fi
  if grep -Eiq "[1-9][0-9]*[[:space:]]+(skipped|failed|errors?|xfailed|xpassed)" "$path"; then
    return 1
  fi
  return 0
}

manual_evidence_ready() {
  [ -f "$CLIENT_LOG" ] || return 1
  "$CLIENT_VALIDATOR" "$CLIENT_LOG" >/dev/null 2>&1
}

print_blocker_summary() {
  if [ "$remote_evidence_blocker" != "1" ] && [ "$manual_evidence_blocker" != "1" ]; then
    return
  fi
  echo "Remote MCP release blocker summary:" >&2
  if [ "$remote_evidence_blocker" = "1" ]; then
    echo "- Remote smoke evidence is incomplete; run scripts/release_remote_smoke.sh --preflight, then regenerate remote-smoke.log with scripts/release_remote_smoke.sh on a Docker-capable host." >&2
  fi
  if [ "$manual_evidence_blocker" = "1" ]; then
    echo "- Manual external-client evidence is incomplete; fill client-smoke-log.md with real Claude Code/Codex CLI rows and pass scripts/validate_client_smoke_log.py." >&2
  fi
  blocker_summary_printed=1
}

check_release_evidence() {
  local cli_log="$EVIDENCE_DIR/cli.log"
  local security_log="$EVIDENCE_DIR/security-headers.log"
  local remote_log="$EVIDENCE_DIR/remote-smoke.log"
  local remote_failures_before
  local manual_failures_before

  if ! remote_evidence_ready "$remote_log"; then
    remote_evidence_blocker=1
  fi
  if ! manual_evidence_ready; then
    manual_evidence_blocker=1
  fi
  print_blocker_summary

  check_cli_evidence "$cli_log"
  check_log "$security_log" "(^|[=[:space:]])7 passed([,[:space:]]|$)" "security headers suite"

  remote_failures_before="$failures"
  if require_file "$remote_log"; then
    require_grep "^DTXWIKI_REMOTE_SMOKE_MODE=remote$" "$remote_log" "remote smoke mode marker"
    require_grep "^DTXWIKI_REMOTE_CLIENT=[A-Za-z0-9._-]+$" "$remote_log" "remote smoke client marker"
    require_grep "^DTXWIKI_REMOTE_HARNESS=docker-compose$" "$remote_log" "remote smoke Docker Compose marker"
    require_grep "^DTXWIKI_REMOTE_ORIGIN=https://[^[:space:]]+$" "$remote_log" "remote smoke HTTPS origin marker"
    require_grep "^DTXWIKI_REMOTE_TRANSPORT=streamable$" "$remote_log" "remote smoke streamable marker"
    require_grep "^DTXWIKI_REMOTE_TLS_VERIFY=ca-file$" "$remote_log" "remote smoke TLS CA marker"
    require_grep "^DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok$" "$remote_log" "remote smoke compose teardown marker"
    require_grep "(^|[=[:space:]])8 passed([,[:space:]]|$)" "$remote_log" "remote smoke 8 scenarios"
    require_grep "remote_mcp_smoke.py::test_pat_immediate_connect_list_tools[[:space:]]+PASSED" "$remote_log" "remote smoke PAT list_tools scenario"
    require_grep "remote_mcp_smoke.py::test_pat_read_page_round_trip[[:space:]]+PASSED" "$remote_log" "remote smoke PAT read_page scenario"
    require_grep "remote_mcp_smoke.py::test_pat_write_agent_page_draft_round_trip[[:space:]]+PASSED" "$remote_log" "remote smoke PAT agent draft scenario"
    require_grep "remote_mcp_smoke.py::test_oauth_full_flow_tool_round_trip[[:space:]]+PASSED" "$remote_log" "remote smoke OAuth full-flow scenario"
    require_grep "remote_mcp_smoke.py::test_oauth_refresh_rotation_and_reuse_denied_via_jti[[:space:]]+PASSED" "$remote_log" "remote smoke OAuth refresh reuse scenario"
    require_grep "remote_mcp_smoke.py::test_oauth_revoke_invalidates_access_jti[[:space:]]+PASSED" "$remote_log" "remote smoke OAuth revoke scenario"
    require_grep "remote_mcp_smoke.py::test_cross_workspace_denied_with_keyword[[:space:]]+PASSED" "$remote_log" "remote smoke cross-workspace scenario"
    require_grep "remote_mcp_smoke.py::test_restricted_library_denied_with_keyword[[:space:]]+PASSED" "$remote_log" "remote smoke restricted-library scenario"
    reject_grep "DTXWIKI_REMOTE_SMOKE_MODE=local|--remote-smoke-mode=local" "$remote_log" "local fallback smoke marker"
    reject_grep "[1-9][0-9]*[[:space:]]+(skipped|failed|errors?|xfailed|xpassed)" "$remote_log" "remote smoke skip/fail marker"
  fi
  if [ "$failures" -ne "$remote_failures_before" ]; then
    remote_evidence_blocker=1
  fi

  manual_failures_before="$failures"
  if require_file "$CLIENT_LOG"; then
    if "$PYTHON_BIN" "$CLIENT_VALIDATOR" "$CLIENT_LOG"; then
      pass "manual client smoke log"
    else
      fail "manual client smoke log validator failed"
    fi
  fi
  if [ "$failures" -ne "$manual_failures_before" ]; then
    manual_evidence_blocker=1
  fi

  if require_file "$SETUP_DOC"; then
    require_heading_count_ge "$SETUP_DOC" 5 "Remote MCP setup doc headings"
    require_grep "Streamable HTTP" "$SETUP_DOC" "Streamable HTTP-only setup contract"
    require_grep "PAT" "$SETUP_DOC" "PAT setup contract"
    require_grep "OAuth" "$SETUP_DOC" "OAuth setup contract"
    require_grep "%USERPROFILE%\\\\.codex\\\\config\\.toml" "$SETUP_DOC" "Windows Codex client path coverage"
    require_grep "~/.claude.json|~/.codex/config.toml" "$SETUP_DOC" "Linux client path coverage"
    require_grep "DTXWIKI_REMOTE_SMOKE_MODE=remote" "$SETUP_DOC" "setup doc remote smoke marker"
    require_grep "DTXWIKI_REMOTE_CLIENT=" "$SETUP_DOC" "setup doc remote client marker"
    require_grep "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" "$SETUP_DOC" "setup doc remote teardown marker"
    require_grep "register-http" "$SETUP_DOC" "HTTP register helper docs"
    require_grep "doctor --http" "$SETUP_DOC" "HTTP doctor docs"
    require_grep "claude mcp add --scope user --transport http" "$SETUP_DOC" "native Claude CLI user-scope registration command"
    reject_grep "pipx install dtxwiki" "$SETUP_DOC" "no client-side dtxwiki install required"
    reject_grep "stdio|dtxwiki-mcp|mcp[[:space:]]+register-remote|DTXWIKI_MCP_(CLIENT_ID|LOG_LEVEL|TOKEN)|DTXWIKI_WORKSPACE_TOKENS|DTXWIKI_DESKTOP_MCP_COMMAND|%APPDATA%\\\\codex" "$SETUP_DOC" "legacy MCP transport/setup"
    require_grep "client-smoke-log\\.md" "$SETUP_DOC" "setup doc manual smoke log"
    require_grep "elapsed_to_list_tools" "$SETUP_DOC" "setup doc manual elapsed field"
    require_grep "<=[[:space:]]*30m" "$SETUP_DOC" "setup doc manual elapsed limit"
    require_grep "external dev PC" "$SETUP_DOC" "setup doc external dev PC evidence"
    require_grep "validate_client_smoke_log\\.py" "$SETUP_DOC" "setup doc manual validator"
  fi

  if require_file "$PHASE5_OVERVIEW"; then
    require_grep "~/.claude\\.json" "$PHASE5_OVERVIEW" "Phase_5 overview Claude Linux path"
    require_grep "%USERPROFILE%\\\\.claude\\.json" "$PHASE5_OVERVIEW" "Phase_5 overview Claude Windows path"
    require_grep "~/.codex/config\\.toml" "$PHASE5_OVERVIEW" "Phase_5 overview Codex Linux path"
    require_grep "%USERPROFILE%\\\\.codex\\\\config\\.toml" "$PHASE5_OVERVIEW" "Phase_5 overview Codex Windows path"
    require_grep "DTXWIKI_REMOTE_SMOKE_MODE=remote" "$PHASE5_OVERVIEW" "Phase_5 overview remote smoke marker"
    require_grep "DTXWIKI_REMOTE_CLIENT=" "$PHASE5_OVERVIEW" "Phase_5 overview remote client marker"
    require_grep "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" "$PHASE5_OVERVIEW" "Phase_5 overview remote teardown marker"
    require_grep "pat_or_dcr_started_at" "$PHASE5_OVERVIEW" "Phase_5 overview manual smoke start timestamp"
    require_grep "elapsed_to_list_tools" "$PHASE5_OVERVIEW" "Phase_5 overview manual smoke elapsed field"
    require_grep "full-file placeholder" "$PHASE5_OVERVIEW" "Phase_5 overview manual placeholder scope"
    require_grep "\\+09:00" "$PHASE5_OVERVIEW" "Phase_5 overview manual KST offset"
    require_grep "external dev PC" "$PHASE5_OVERVIEW" "Phase_5 overview external dev PC evidence"
    require_grep "validate_client_smoke_log.py" "$PHASE5_OVERVIEW" "Phase_5 overview manual smoke validator"
    reject_grep "~/.config/claude|%APPDATA%\\\\Claude|%APPDATA%\\\\codex|mcp_config\\.json" "$PHASE5_OVERVIEW" "stale client config path"
  fi

  if require_file "$SPRINT5_OVERVIEW"; then
    require_grep "Gate B" "$SPRINT5_OVERVIEW" "Sprint 5 overview Gate B contract"
    require_grep "Required client smoke" "$SPRINT5_OVERVIEW" "Sprint 5 overview client smoke contract"
    require_grep "localhost .*Gate B" "$SPRINT5_OVERVIEW" "Sprint 5 overview remote evidence rule"
    require_grep "~/.claude\\.json" "$SPRINT5_OVERVIEW" "Sprint 5 overview Claude Linux path"
    require_grep "%USERPROFILE%\\\\.claude\\.json" "$SPRINT5_OVERVIEW" "Sprint 5 overview Claude Windows path"
    require_grep "test_oauth_full_flow_tool_round_trip" "$SPRINT5_OVERVIEW" "Sprint 5 overview OAuth full-flow node"
    require_grep "scripts/release_remote_smoke\\.sh" "$SPRINT5_OVERVIEW" "Sprint 5 overview remote smoke wrapper"
    require_grep "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" "$SPRINT5_OVERVIEW" "Sprint 5 overview remote teardown marker"
    require_grep "validate_client_smoke_log\\.py" "$SPRINT5_OVERVIEW" "Sprint 5 overview manual validator"
    require_grep "elapsed_to_list_tools[[:space:]]*<=[[:space:]]*30m" "$SPRINT5_OVERVIEW" "Sprint 5 overview manual elapsed limit"
    reject_grep "~/.config/claude|%APPDATA%\\\\Claude|test_oauth_full_flow_dcr_authorize_token|pytest -m cross_transport_remote tests/e2e/remote_mcp_smoke\\.py -v > evidence/remote-smoke\\.log" "$SPRINT5_OVERVIEW" "stale Sprint 5 overview Phase 5 contract"
  fi
}

check_public_release_surface() {
  # Every path below is tracked at HEAD, so this half runs on a clean clone.
  if require_file "$README_FILE"; then
    require_grep "^## 원격 서버 배포와 팀 MCP 연결$" "$README_FILE" "README remote MCP section"
    require_grep "Streamable HTTP" "$README_FILE" "README Streamable HTTP contract"
    require_grep "PAT.*OAuth|OAuth.*PAT" "$README_FILE" "README PAT/OAuth authentication contract"
    require_grep "mcp register-http" "$README_FILE" "README register-http command"
    require_grep "mcp doctor" "$README_FILE" "README doctor command"
    require_grep "DTXWIKI_WORKSPACE_SLUG.*(폐기|retired)" "$README_FILE" "README retired workspace slug warning"
    reject_grep "stdio|dtxwiki-mcp|backend\.mcp\.stdio_server|dtxwiki[[:space:]]+(claude|cursor|codex|gemini|antigravity)[[:space:]]+(install|uninstall)|mcp[[:space:]]+register-remote|mcp[[:space:]]+register[[:space:]]+--|%APPDATA%\\\\codex" "$README_FILE" "README legacy MCP transport/setup"
  fi

  if require_file "$DEPLOYMENT_FILE"; then
    require_heading_count_ge "$DEPLOYMENT_FILE" 5 "DEPLOYMENT runbook headings"
    require_grep "Streamable HTTP" "$DEPLOYMENT_FILE" "DEPLOYMENT Streamable HTTP contract"
    require_grep "DTXWIKI_WORKSPACE_SLUG.*폐기" "$DEPLOYMENT_FILE" "DEPLOYMENT retired workspace slug warning"
    reject_grep "stdio|dtxwiki-mcp|backend\.mcp\.stdio_server|mcp[[:space:]]+register-remote|%APPDATA%\\\\codex" "$DEPLOYMENT_FILE" "DEPLOYMENT legacy MCP transport/setup"
  fi

  # I18N-05: check:i18n runs in CI but was missing from both local release
  # gate scripts, so a translation regression could clear this gate locally.
  if (cd "$ROOT_DIR" && node scripts/check-i18n-coverage.mjs); then
    pass "i18n locale coverage"
  else
    fail "i18n locale coverage"
  fi

  if (
    cd "$ROOT_DIR"
    "$PYTHON_BIN" -m pytest \
      tests/packaging/test_evidence_redaction.py::test_no_bearer_in_evidence_logs \
      tests/packaging/test_evidence_redaction.py::test_no_jwt_full_in_evidence_logs \
      tests/packaging/test_evidence_redaction.py::test_no_session_secret_in_evidence_logs \
      -q
  ); then
    pass "Evidence redaction policy tests"
  else
    fail "Evidence redaction policy tests failed"
  fi
}

main() {
  if [ "$evidence_mode" = "1" ]; then
    check_release_evidence
  else
    echo "SKIP: release evidence checks (private inputs not supplied; pass --with-evidence with --evidence-dir/--setup-doc/--phase5-overview/--sprint5-overview)"
  fi

  check_public_release_surface

  if [ "$failures" -ne 0 ]; then
    if [ "$blocker_summary_printed" != "1" ]; then
      if [ "$remote_evidence_blocker" = "1" ] || [ "$manual_evidence_blocker" = "1" ]; then
        print_blocker_summary
      fi
    fi
    echo "Release gate check: FAILED ($failures issue(s))" >&2
    return 1
  fi
  echo "Release gate check: OK"
}

main
