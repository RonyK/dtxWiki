#!/usr/bin/env bash
# tests/packaging/production_smoke.sh
# Usage: production_smoke.sh <public-origin-url> [ca-cert-path]
#   e.g.: production_smoke.sh https://wiki.example.com
#   CI self-signed cert: production_smoke.sh https://dtxwiki.test ./dev-ca.pem
# Exit 0 = all 5 checks OK. Exit 1 = any failure.

set -euo pipefail

ORIGIN="${1:-}"
if [[ -z "$ORIGIN" || "$ORIGIN" != https://* ]]; then
    echo "ERROR: argument must be https:// public origin URL" >&2
    exit 1
fi
ORIGIN="${ORIGIN%/}"
CACERT="${2:-${DTXWIKI_SMOKE_CACERT:-}}"
CURL_TLS_ARGS=()
if [[ -n "$CACERT" ]]; then
    CURL_TLS_ARGS=(--cacert "$CACERT")
fi

AUTHORITY="${ORIGIN#https://}"
AUTHORITY="${AUTHORITY%%/*}"
TLS_HOST="${AUTHORITY%%:*}"
TLS_PORT="443"
if [[ "$AUTHORITY" == *:* ]]; then
    TLS_PORT="${AUTHORITY##*:}"
fi
TLS_CONNECT_HOST="$TLS_HOST"
if [[ -n "${DTXWIKI_SMOKE_RESOLVE_IP:-}" ]]; then
    CURL_TLS_ARGS+=(--resolve "$TLS_HOST:$TLS_PORT:$DTXWIKI_SMOKE_RESOLVE_IP")
    TLS_CONNECT_HOST="$DTXWIKI_SMOKE_RESOLVE_IP"
fi

# --- Check (a): /health/live root path (ADR A10) ---
echo "[a] /health/live"
resp=$(curl "${CURL_TLS_ARGS[@]}" -fsS "$ORIGIN/health/live")
echo "$resp" | grep -q '"status":"alive"' || { echo "FAIL: live status"; exit 1; }
echo "  OK"

# --- Check (b): /health/ready root path (ADR A7) ---
echo "[b] /health/ready"
resp=$(curl "${CURL_TLS_ARGS[@]}" -fsS "$ORIGIN/health/ready")
echo "$resp" | grep -q '"status":"ready"' || { echo "FAIL: ready status"; exit 1; }
echo "$resp" | grep -q '"db":"ok"' || { echo "FAIL: db component"; exit 1; }
echo "  OK"

# --- Check (c): OAuth metadata resource field == public MCP resource ---
echo "[c] /.well-known/oauth-protected-resource"
resp=$(curl "${CURL_TLS_ARGS[@]}" -fsS "$ORIGIN/.well-known/oauth-protected-resource")
echo "$resp" | grep -q "\"resource\":\"$ORIGIN/mcp\"" || { echo "FAIL: resource mismatch"; exit 1; }
echo "  OK"

# --- Check (d): TLS certificate valid ---
echo "[d] TLS cert"
OPENSSL_ARGS=(
    s_client
    -servername "$TLS_HOST"
    -connect "$TLS_CONNECT_HOST:$TLS_PORT"
    -verify_return_error
)
if [[ -n "$CACERT" ]]; then
    OPENSSL_ARGS+=(-CAfile "$CACERT")
fi
if ! TLS_CERT_OUTPUT=$(openssl "${OPENSSL_ARGS[@]}" </dev/null 2>/dev/null); then
    echo "FAIL: TLS handshake or certificate verification failed"
    exit 1
fi
if ! openssl x509 -noout -dates -checkend 86400 <<<"$TLS_CERT_OUTPUT"; then
    echo "FAIL: cert invalid or expires within 24h"
    exit 1
fi
echo "  OK"

# --- Check (e): /mcp 401 + WWW-Authenticate metadata URL ---
echo "[e] /mcp 401 + WWW-Authenticate"
hdr=$(curl "${CURL_TLS_ARGS[@]}" -sS -o /dev/null -D - "$ORIGIN/mcp")
echo "$hdr" | grep -qE "^HTTP/[0-9.]+ 401" || { echo "FAIL: /mcp not 401"; exit 1; }
echo "$hdr" | grep -qi "WWW-Authenticate:.*resource_metadata.*$ORIGIN" \
    || { echo "FAIL: WWW-Authenticate missing metadata URL"; exit 1; }
echo "  OK"

echo ""
echo "ALL CHECKS PASSED ($ORIGIN)"
exit 0
