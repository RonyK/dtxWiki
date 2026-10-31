"""The Knowledge Map is switched per workspace, never by a build or process flag.

Two flags used to gate it: a frontend build flag that registered the route and
menu, and a backend process flag that answered the API. Either one could
disagree with the other and with the owner's Settings switch. This guard keeps
them from coming back into shipped code, configuration, or setup docs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

RETIRED = (
    "VITE_WORKSPACE_GRAPH_ENABLED",
    "DTXWIKI_WORKSPACE_GRAPH_ENABLED",
    "workspaceGraphFeatureFlag",
)

# Places that must keep naming a retired flag: this guard, the release note
# that announces the retirement, and the test proving the old env is ignored.
ALLOWED = {
    "tests/static/test_no_workspace_graph_build_flag.py",
    "CHANGELOG.md",
    "backend/tests/unit/test_settings_workspace_graph.py",
}


def retired_flag_references(files: Iterable[tuple[str, str]]) -> list[str]:
    """Return ``path: flag`` for every retired flag named outside ``ALLOWED``."""

    offenders: list[str] = []
    for name, text in files:
        if name in ALLOWED or name.startswith("internal/audit/"):
            continue
        offenders.extend(f"{name}: {flag}" for flag in RETIRED if flag in text)
    return offenders


def _tracked_texts() -> Iterable[tuple[str, str]]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for name in output.decode("utf-8").split("\0"):
        path = ROOT / name
        if not name or not path.is_file():
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_retired_workspace_graph_flags_are_not_referenced():
    assert retired_flag_references(_tracked_texts()) == []


@pytest.mark.counterexample(reason="a reintroduced build flag must be reported")
def test_guard_reports_a_reintroduced_flag_but_not_the_allowed_history():
    files = [
        ("frontend/.env.example", "VITE_WORKSPACE_GRAPH_ENABLED=true\n"),
        ("playwright.config.ts", "DTXWIKI_WORKSPACE_GRAPH_ENABLED: 'true'"),
        ("CHANGELOG.md", "`DTXWIKI_WORKSPACE_GRAPH_ENABLED` is retired"),
        ("internal/audit/note.md", "VITE_WORKSPACE_GRAPH_ENABLED was removed"),
    ]

    assert retired_flag_references(files) == [
        "frontend/.env.example: VITE_WORKSPACE_GRAPH_ENABLED",
        "playwright.config.ts: DTXWIKI_WORKSPACE_GRAPH_ENABLED",
    ]
