"""A module removed on purpose must not quietly come back.

Nine backend modules were deleted as part of Epic 1: the stdio MCP transport
and its per-client installers, the legacy Pages MCP tools and their schemas,
and two REST routers that were never mounted. Only two of them had anything
watching for their return, and that watcher was
``scripts/verify_wheel_artifact.py``, which runs at ``v*`` tag time -- so the
other seven could have been restored and nobody would have learned about it
until a release, if then.

This guard reads the same list the release gate does, so the two cannot drift,
and it runs on every CI job rather than only at tag time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "scripts" / "verify_wheel_artifact.py"


def _retired_modules() -> tuple[str, ...]:
    """Load the release gate's own list rather than restating it."""

    spec = importlib.util.spec_from_file_location("_release_gate", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(VERIFIER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(VERIFIER.parent))
    return tuple(path.as_posix() for path in module.RETIRED_MODULES)


def _resurrected_modules(root: Path) -> list[str]:
    return [
        relative_path
        for relative_path in _retired_modules()
        if (root / relative_path).exists()
    ]


@pytest.mark.parametrize("relative_path", _retired_modules())
def test_retired_module_stays_deleted(relative_path: str):
    assert not (ROOT / relative_path).exists(), (
        f"{relative_path} is on the release gate's retired list but exists in "
        "the source tree. Restoring it is a decision to be argued in "
        "scripts/verify_wheel_artifact.py's RETIRED_MODULES, not a file that "
        "reappears."
    )


@pytest.mark.counterexample(reason="a resurrected retired module must be rejected")
def test_guard_rejects_a_resurrected_module(tmp_path):
    resurrected = tmp_path / "backend" / "mcp" / "page_tools.py"
    resurrected.parent.mkdir(parents=True)
    resurrected.write_text("def list_pages():\n    pass\n", encoding="utf-8")

    assert _resurrected_modules(tmp_path) == ["backend/mcp/page_tools.py"]


def test_the_retired_list_covers_the_stdio_transport_and_the_legacy_pages_surface():
    """The list is the contract; pin what it must contain.

    Without this, a future edit could shrink RETIRED_MODULES to nothing and
    every parametrized case above would vanish silently -- a guard that proves
    less and less while still reporting green.
    """

    assert set(_retired_modules()) == {
        "backend/api/pages.py",
        "backend/api/uploads.py",
        "backend/cli/integrations/claude.py",
        "backend/cli/integrations/codex.py",
        "backend/cli/integrations/cursor.py",
        "backend/mcp/entry.py",
        "backend/mcp/page_tools.py",
        "backend/mcp/stdio_server.py",
        "backend/schemas/mcp/page_tools.py",
    }
