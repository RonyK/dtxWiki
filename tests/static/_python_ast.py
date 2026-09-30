"""Shared Python parsing for repository-wide static gates.

This module deliberately does not match pytest's test-module naming pattern.
Keeping the cache here gives every gate one module identity even though pytest
may import test files by both a bare name and their namespace-package name.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_RESOLVED_ROOT = ROOT.resolve()

# Repository sources are immutable for one pytest process. Synthetic roots are
# often rewritten inside a test and must never reuse an earlier tree.
_REPOSITORY_TREES: dict[Path, ast.Module] = {}


def parse_python_file(path: Path, *, root: Path = ROOT) -> ast.Module:
    """Return the parsed module for ``path``, caching repository sources only."""

    resolved = path.resolve()
    if root.resolve() != _RESOLVED_ROOT or not resolved.is_relative_to(
        _RESOLVED_ROOT
    ):
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    cached = _REPOSITORY_TREES.get(resolved)
    if cached is None:
        # Keep the filename on SyntaxError so a broken backend source identifies
        # itself instead of appearing to come from this shared helper.
        cached = ast.parse(
            resolved.read_text(encoding="utf-8"), filename=str(resolved)
        )
        _REPOSITORY_TREES[resolved] = cached
    return cached
