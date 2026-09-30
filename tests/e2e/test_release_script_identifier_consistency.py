"""Consistency guard: release/smoke scripts must reference identifiers that still
exist after renames.

Why this exists: the ``source -> library`` domain rename (5f7f4004) renamed the
error code ``restricted_source_denied -> restricted_library_denied`` and the e2e
node ``test_restricted_source_denied_with_keyword -> ..._library_...``, but left
three release/smoke scripts pinned to the old names. The release gate then
silently rejected otherwise-valid evidence (the gate greps for a node/keyword
that no longer existed). These static checks fail fast whenever a script
references an e2e test node or an error code that is not defined in the code,
catching that whole rename-leftover class at lint time instead of at release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "tests" / "e2e" / "remote_mcp_smoke.py"
GATE = ROOT / "scripts" / "release_gate_check.sh"
REMOTE = ROOT / "scripts" / "release_remote_smoke.sh"
VALIDATOR = ROOT / "scripts" / "validate_client_smoke_log.py"
ERROR_CODES = ROOT / "shared" / "error_codes.json"


def _e2e_test_functions() -> set[str]:
    text = SMOKE.read_text(encoding="utf-8")
    return set(re.findall(r"^(?:async\s+)?def\s+(test_\w+)\s*\(", text, re.MULTILINE))


def _referenced_remote_nodes(text: str) -> set[str]:
    nodes: set[str] = set()
    # require_grep "...remote_mcp_smoke.py::test_xxx ... PASSED"
    nodes |= set(re.findall(r"remote_mcp_smoke\.py::(test_\w+)", text))
    # bare entries inside a `remote_nodes=( ... )` bash array
    for block in re.findall(r"remote_nodes=\((.*?)\)", text, re.DOTALL):
        nodes |= set(re.findall(r"\b(test_\w+)\b", block))
    return nodes


def _all_error_codes() -> set[str]:
    data = json.loads(ERROR_CODES.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for value in data.values():
        if isinstance(value, list):
            codes.update(str(item) for item in value)
    return codes


def test_release_scripts_reference_real_e2e_nodes() -> None:
    real = _e2e_test_functions()
    assert real, "no test functions parsed from remote_mcp_smoke.py (regex drift?)"
    for script in (GATE, REMOTE):
        referenced = _referenced_remote_nodes(script.read_text(encoding="utf-8"))
        assert referenced, f"{script.name}: no remote nodes parsed (regex drift?)"
        missing = sorted(node for node in referenced if node not in real)
        assert not missing, (
            f"{script.name} references e2e nodes that do not exist in "
            f"remote_mcp_smoke.py (rename leftover?): {missing}"
        )


def test_smoke_log_validator_references_real_error_codes() -> None:
    codes = _all_error_codes()
    assert "restricted_library_denied" in codes, "sanity: error_codes.json parsed"
    text = VALIDATOR.read_text(encoding="utf-8")
    # The validator asserts on quoted error-code strings like "..._denied".
    referenced = set(re.findall(r'"(\w+_denied)"', text))
    assert referenced, "validator: no *_denied codes parsed (regex drift?)"
    missing = sorted(code for code in referenced if code not in codes)
    assert not missing, (
        "validate_client_smoke_log.py references error codes not in "
        f"shared/error_codes.json (rename leftover?): {missing}"
    )
