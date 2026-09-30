"""Static guard for the MCP app catalog's data-only extension boundary."""

from __future__ import annotations

import ast
import json
import re

from tests.static._python_ast import ROOT, parse_python_file

CATALOG = json.loads(
    (ROOT / "backend/catalog/mcp_apps/catalog.v1.json").read_text(encoding="utf-8")
)
KNOWN_APP_IDS = frozenset(app["id"] for app in CATALOG["apps"])
KNOWN_APP_NAME_HEURISTICS = frozenset(
    app["runtimeBrand"].casefold()
    for app in CATALOG["apps"]
    if app.get("runtimeBrand")
)


def test_python_production_has_no_known_mcp_app_id_control_flow():
    violations: list[str] = []
    for path in (ROOT / "backend").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if "/tests/" in f"/{relative}" or "/migrations/" in f"/{relative}":
            continue
        tree = parse_python_file(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _known_ids(node):
                violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.MatchValue) and _known_ids(node):
                violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.MatchOr) and _known_ids(node):
                violations.append(f"{relative}:{node.lineno}")

    assert not violations, (
        "Known MCP app IDs must be declarative catalog/adapter data, not Python "
        f"control flow: {violations}"
    )


def test_frontend_production_has_no_known_mcp_app_branch_or_name_heuristic():
    comparison_patterns = [
        re.compile(
            rf"(?:===|!==|==|!=)\s*['\"]{re.escape(app_id)}['\"]"
            rf"|['\"]{re.escape(app_id)}['\"]\s*(?:===|!==|==|!=)"
            rf"|\bcase\s+['\"]{re.escape(app_id)}['\"]"
        )
        for app_id in KNOWN_APP_IDS
    ]
    heuristic_patterns = [
        re.compile(rf"\.includes\(\s*['\"]{name}['\"]\s*\)", re.IGNORECASE)
        for name in KNOWN_APP_NAME_HEURISTICS
    ]
    violations: list[str] = []
    for path in (ROOT / "frontend/src").rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or "tests" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if path.name == "types.gen.ts":
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if any(pattern.search(line) for pattern in comparison_patterns):
                violations.append(f"{relative}:{line_number}")
            if any(pattern.search(line) for pattern in heuristic_patterns):
                violations.append(f"{relative}:{line_number}")

    assert not violations, (
        "Known MCP apps must be selected by catalog capability, never frontend "
        f"branches or fuzzy display-name inference: {violations}"
    )


def _known_ids(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value in KNOWN_APP_IDS
    }
