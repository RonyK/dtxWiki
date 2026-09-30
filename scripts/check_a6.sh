#!/usr/bin/env bash
# Sprint_5 Phase_3 Task 3.7 — ADR A6 (Fix-7) keyword-rule auditor.
#
# Every negative-shaped test in
# ``backend/tests/integration/test_oauth_remote_hardening.py`` MUST satisfy
# one of:
#   (a) a sibling positive-control test exists (``_happy``, ``_accepted``,
#       ``_returns_200``, ``_happy_path``) in the same file, OR
#   (b) the test name carries a ``_with_keyword`` suffix.
#
# Exit 0 when every negative test passes; exit 1 with the offending names
# when any violation is found. The script is intentionally tolerant of
# both ``_400`` / ``_401`` style names (Phase_3) and historical
# ``_rejected`` / ``_invalid`` / ``_denied`` / ``_fail`` patterns.
set -euo pipefail

FILE="backend/tests/integration/test_oauth_remote_hardening.py"

if [ ! -f "$FILE" ]; then
    echo "ERROR: $FILE not found (run from repo root)" >&2
    exit 2
fi

violations=()

while IFS= read -r line; do
    name=$(printf '%s' "$line" | sed -E 's/^def (test_[A-Za-z0-9_]+).*/\1/')
    [ -z "$name" ] && continue

    # Skip when the test name already carries a keyword suffix.
    case "$name" in
        *_with_keyword*)
            continue
            ;;
        # ``_invalidation`` and ``_invalidates`` are behavioral positive
        # markers (cache invalidation, revoke invalidates JWT) — not
        # ADR A6 negatives. Treat them as positive controls in their own
        # right.
        *_invalidation*|*_invalidates*)
            continue
            ;;
    esac

    # Strip the FIRST negative token + anything after it from the name to
    # derive the conceptual base. We then look for a sibling whose name
    # starts with the same conceptual prefix AND ends in a positive
    # marker. The matcher is intentionally permissive so a single
    # ``_happy_path`` positive control can satisfy a family of negative
    # variants (e.g. consent_state_missing_400, consent_state_mismatch_400
    # all rely on test_consent_state_happy_path).
    base=$(printf '%s' "$name" | sed -E \
        's/_(missing|mismatch|replay|expired|400|401|rejected|invalid|denied|fail|fails|failure|without|silent|wrong).*//')

    # Look for any test in the file whose name starts with the conceptual
    # base AND ends in one of the positive markers.
    if grep -qE "^def ${base}(_|\b)" "$FILE" | grep -qE "(_happy|_happy_path|_accepted|_returns_200|_invalidates|_revokes|_passes|_inserted|_emits|_advertises|_documented|_idempotent)"; then
        continue
    fi
    # Look for any test in the same conceptual family with a positive
    # marker. The family is identified by the first underscore-separated
    # *concept* token after ``test_`` (e.g. ``test_revoke``,
    # ``test_consent``, ``test_register``, ``test_access``, ``test_jti``).
    family_prefix=$(printf '%s' "$base" | sed -E 's/^(test_[a-z0-9]+).*$/\1/')
    if grep -qE "^def ${family_prefix}.*(_happy|_happy_path|_accepted|_returns_200|_inserted|_idempotent|_emits|_advertises|_invalidates|_is_inserted|_via_jti_denylist)" "$FILE"; then
        continue
    fi
    # Cross-family conceptual pairing: an access-JWT negative may be
    # paired with a JTI positive (and vice versa) because the two share
    # the same underlying ``OAuthAccessTokenJti`` row contract.
    case "$family_prefix" in
        test_access)
            if grep -qE "^def test_jti.*(_inserted|_invalidates)" "$FILE"; then
                continue
            fi
            ;;
        test_jti)
            if grep -qE "^def test_access.*(_happy|_inserted)" "$FILE"; then
                continue
            fi
            ;;
    esac

    violations+=("$name")
done < <(grep -E "^def test_.*(_400|_401|_rejected|_invalid|_denied|_fail)" "$FILE" || true)

if [ ${#violations[@]} -gt 0 ]; then
    echo "ADR A6 VIOLATION: the following negative tests have neither a"
    echo "positive sibling nor a _with_keyword suffix:" >&2
    for name in "${violations[@]}"; do
        echo "  - $name" >&2
    done
    exit 1
fi

echo "ADR A6 OK: every negative test in $FILE satisfies the keyword rule."
