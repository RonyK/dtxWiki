"""Calculate and read MCP tool contract metadata for releases."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

TOOL_CONTRACT = "backend/schemas/common.py"
_VERSION_NUMBER = r"(?:0|[1-9][0-9]*)"
RELEASE_VERSION_PATTERN = re.compile(
    rf"{_VERSION_NUMBER}\.{_VERSION_NUMBER}\.{_VERSION_NUMBER}"
    rf"(?:-(?:alpha|beta|rc)\.{_VERSION_NUMBER})?"
)
_TOOL_CONTRACT_FIELDS = (
    re.compile(r"^(MCP_TOOL_CONTRACT_REVISION = )([0-9]+)$", re.M),
    re.compile(r"^(MCP_RELEASED_TOOL_CONTRACT_REVISION = )([0-9]+)$", re.M),
    re.compile(r'^(MCP_RELEASED_TOOL_CONTRACT_DIGEST = )"([0-9a-f]{12})"$', re.M),
)

_DIGEST_PROBE = """
from importlib.machinery import PathFinder
from pathlib import Path
import sys

root = Path(sys.argv[1])

class LocalBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "backend":
            path = [str(root)]
        elif not fullname.startswith("backend."):
            return None
        spec = PathFinder.find_spec(fullname, path)
        origins = [spec.origin] if spec and spec.origin else (
            spec.submodule_search_locations if spec else ()
        )
        if not origins or any(
            not Path(origin).resolve().is_relative_to(root)
            for origin in origins
        ):
            raise ImportError(f"{fullname} is missing from {root}")
        return spec

if "backend" in sys.modules:
    raise ImportError("did not start with a clean backend import")
sys.meta_path.insert(0, LocalBackend())
from backend.mcp.protocol.tool_descriptors import tool_contract_digest

print(tool_contract_digest())
"""


def tool_contract_digest(root: Path) -> str:
    """Compute the digest using the catalog and schemas from this root."""
    root = root.resolve()
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _DIGEST_PROBE, str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    digest = process.stdout.strip()
    if process.returncode or not re.fullmatch(r"[0-9a-f]{12}", digest):
        raise ValueError(
            f"MCP tool contract digest failed in {root}:\n{process.stderr[-2000:]}"
        )
    return digest


def _parse_tool_contract(text: str, source: str) -> tuple[int, int, str]:
    fields = [pattern.findall(text) for pattern in _TOOL_CONTRACT_FIELDS]
    if any(len(matches) != 1 for matches in fields):
        raise ValueError(
            f"{source}: missing or invalid MCP tool contract release metadata"
        )
    current, revision, digest = (matches[0][1] for matches in fields)
    return int(current), int(revision), digest


def read_recorded_tool_contract(root: Path) -> tuple[int, int, str]:
    """Read the live revision, recorded revision, and recorded digest."""
    path = root / TOOL_CONTRACT
    return _parse_tool_contract(path.read_text(encoding="utf-8"), str(path))


def released_tool_contract_text(root: Path, digest: str, *, revision: int) -> str:
    """Render both revision fields and the digest before writing a release."""
    path = root / TOOL_CONTRACT
    text = path.read_text(encoding="utf-8")
    _parse_tool_contract(text, str(path))
    values = (str(revision), str(revision), f'"{digest}"')
    for pattern, value in zip(_TOOL_CONTRACT_FIELDS, values, strict=True):
        text = pattern.sub(r"\g<1>" + value, text)
    return text


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def _git_output(root: Path, *args: str) -> str:
    process = _git(root, *args)
    if process.returncode:
        raise ValueError(
            f"cannot inspect previous release tags: {process.stderr.strip()}"
        )
    return process.stdout.strip()


def _has_prepared_release_parent(root: Path, tag: str) -> bool:
    # A publication branch can merge the release preparation from dev.
    # Check the merged parent, whose package version identifies this release.
    commit = f"refs/tags/{tag}^{{commit}}"
    ancestry = _git_output(root, "rev-list", "--parents", "-n1", commit).split()
    release_parents = ancestry[2:]
    for parent in release_parents:
        if _git(root, "merge-base", "--is-ancestor", parent, "HEAD").returncode != 0:
            continue
        project = _git(root, "show", f"{parent}:pyproject.toml")
        if project.returncode == 0 and (
            tomllib.loads(project.stdout).get("project", {}).get("version") == tag[1:]
        ):
            return True
    return False


def _previous_release_tag(root: Path, target_version: str) -> str:
    repo = Path(_git_output(root, "rev-parse", "--show-toplevel"))
    if repo.resolve() != root.resolve():
        raise ValueError(
            f"cannot inspect previous release tags: {root} is not a Git root"
        )
    target = Version(target_version)
    candidates = [
        (Version(tag[1:]), tag)
        for tag in _git_output(root, "tag", "--list", "v*").splitlines()
        if RELEASE_VERSION_PATTERN.fullmatch(tag[1:])
    ]
    merged = set(
        _git_output(root, "tag", "--merged", "HEAD", "--list", "v*").splitlines()
    )
    for version, tag in sorted(candidates, reverse=True):
        if version >= target:
            continue
        if tag not in merged and not _has_prepared_release_parent(root, tag):
            continue
        return tag
    raise ValueError(
        f"previous release tag for v{target_version} is missing from this Git lineage"
    )


def previous_release_tool_contract(root: Path, target_version: str) -> tuple[int, str]:
    """Read the prior tagged release in this checkout's release lineage."""
    try:
        tag = _previous_release_tag(root, target_version)
        source = f"refs/tags/{tag}:{TOOL_CONTRACT}"
        text = _git_output(root, "show", source)
        current, revision, digest = _parse_tool_contract(text, source)
        if current != revision:
            raise ValueError(f"{source}: MCP tool contract revision was not finalized")
    except ValueError as exc:
        raise ValueError(
            f"{exc}\nEnsure the previous published release tag is available locally."
        ) from exc
    return revision, digest


def next_tool_contract_revision(
    previous_revision: int, previous_digest: str, digest: str
) -> int:
    return previous_revision + int(digest != previous_digest)
