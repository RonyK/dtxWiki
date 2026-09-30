"""Bind the two Epic-1 contract clauses Phase 6 amended to the code they describe.

`doc/design/epic1-core-contract.md` is gitignored by design, so no test here can
read it; a hash pin over it only proved the author re-hashed whatever they wrote.
This module guards the tracked end instead: it asserts the PROPERTIES the amended
clauses claim, against the tracked tree, so if the CODE stops matching what the
contract says, these fail in CI.

Neither of these duplicates an existing gate. tests/static/test_schema_drift.py
compares exported artifacts to the registry; nothing asserts that the wire
advertises no output schema, and nothing asserts the migration graph's shape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend" / "migrations" / "versions"
DESCRIPTORS = ROOT / "backend" / "mcp" / "protocol" / "tool_descriptors.py"


def test_the_migration_graph_is_the_single_rebaselined_chain() -> None:
    """Contract clause: the chain starts `0001_initial_release -> 0002_epic1`.

    The contract used to say the chain ended at 0008 and that 0003/0004 were
    immutable. Revisions 0003-0015 were deleted by the Epic-1 rebaseline.
    Converting an existing deployed database from that retired range is an
    explicitly deferred migration-branch responsibility; this build refuses
    such a stamp instead of silently adopting or rewriting it.

    0.1.1 published that pair and froze it (backend/migrations/
    frozen_revisions.json). Schema changes after it are new revisions that
    continue the same single linear chain; a second base, a branch or a merge
    would make the lineage clause false again.
    """

    def _assigned(path: Path, name: str) -> object:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        raise AssertionError(f"{path.name} does not assign {name}")

    lineage = {
        _assigned(path, "revision"): _assigned(path, "down_revision")
        for path in VERSIONS.glob("*.py")
    }

    assert lineage.get("0001_initial_release", "missing") is None, (
        "0001_initial_release is the base of the rebaselined graph"
    )
    assert lineage.get("0002_epic1") == "0001_initial_release", (
        "0002_epic1 must sit directly on 0001_initial_release"
    )
    roots = sorted(revision for revision, down in lineage.items() if down is None)
    assert roots == ["0001_initial_release"], f"more than one base: {roots}"
    merges = sorted(
        revision
        for revision, down in lineage.items()
        if down is not None and not isinstance(down, str)
    )
    assert merges == [], f"merge revisions: {merges}"
    parents = sorted(down for down in lineage.values() if down is not None)
    assert len(parents) == len(set(parents)), (
        f"a revision has more than one child, so the chain branches: {parents}"
    )
    assert set(parents) <= set(lineage), "a revision descends from a missing one"


def test_tools_list_advertises_no_output_schema() -> None:
    """Contract clause: `tools/list` carries input schemas only.

    The superseded wording promised both schemas "so a conforming client can
    validate every tool result". The 260901 design removed the wire schema and
    named runner-side validation as its replacement; Phase 4 shipped that
    replacement fail-closed. The wire half is what this asserts.
    """

    source = DESCRIPTORS.read_text(encoding="utf-8")
    assert re.search(r"^\s*del output_schema\s*$", source, re.MULTILINE), (
        "tool_descriptors no longer drops output_schema before building the "
        "wire Tool. Restoring it re-advertises a schema the contract says is "
        "absent, and re-inflates tools/list by ~74%"
    )

    tree = ast.parse(source)
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "outputSchema"
    ]
    assert offenders == [], (
        "an `outputSchema=` keyword reached a wire object at line(s) "
        f"{offenders}; the contract states tools/list advertises none"
    )
