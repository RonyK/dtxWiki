#!/usr/bin/env python3
"""Build the npm launcher package ``@dtxsuite/dtxwiki`` and check what it holds.

The npm package carries no copy of dtxWiki: ``bin/dtxwiki.js`` runs
``dtxwiki==<its own version>`` from PyPI. Its version therefore has to be the
pyproject version, or ``npx @dtxsuite/dtxwiki`` would start a different
release than the one it was published with, or one PyPI does not have.

``npm/dtxwiki/`` holds the launcher's own files. The license texts exist once,
at the repository root, and are copied into a staging directory next to them,
so the tarball cannot drift from the licenses the wheel ships. release.yml
runs this in the build job and publishes the one tarball it writes; the checks
below are what stands between a tag and npm.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path("npm/dtxwiki")
NAME = "@dtxsuite/dtxwiki"
BIN = {"dtxwiki": "bin/dtxwiki.js"}
PACKAGE_FILES = ("package.json", "README.md", "bin/dtxwiki.js")
LICENSE_FILES = ("LICENSE", "LICENSE-AGPL-3.0", "LICENSE-ELv2", "NOTICE")
# A launcher that pulls nothing in has no dependency to compromise.
DEPENDENCY_FIELDS = (
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
    "bundleDependencies",
    "bundledDependencies",
)


def tarball_name(version: str) -> str:
    """What ``npm pack`` names the tarball of ``NAME`` at ``version``."""

    return f"{NAME.lstrip('@').replace('/', '-')}-{version}.tgz"


def manifest_problems(root: Path = ROOT) -> list[str]:
    """Every way ``npm/dtxwiki/package.json`` disagrees with pyproject.toml."""

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    manifest = json.loads(
        (root / PACKAGE_DIR / "package.json").read_text(encoding="utf-8")
    )
    problems = []
    if manifest.get("name") != NAME:
        problems.append(f"package name {manifest.get('name')!r}, expected {NAME!r}")
    for field in ("version", "license"):
        if manifest.get(field) != project[field]:
            problems.append(
                f"package.json {field} {manifest.get(field)!r} is not the "
                f"pyproject.toml {field} {project[field]!r}"
            )
    if manifest.get("bin") != BIN:
        problems.append(f"package.json bin {manifest.get('bin')!r}, expected {BIN!r}")
    # npm refuses a provenance statement whose repository is not the one the
    # workflow ran in, so a mismatch would only surface at publish time.
    repository = (manifest.get("repository") or {}).get("url")
    expected = f"git+{project['urls']['Repository'].rstrip('/')}.git"
    if repository != expected:
        problems.append(
            f"package.json repository.url {repository!r}, expected {expected!r}"
        )
    present = [field for field in DEPENDENCY_FIELDS if manifest.get(field)]
    if present:
        problems.append(f"the launcher must not depend on anything: {present}")
    if manifest.get("private"):
        problems.append("package.json is private: npm would refuse to publish it")
    return problems


def stage(root: Path, staging: Path) -> None:
    for relative in PACKAGE_FILES:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / PACKAGE_DIR / relative, target)
    for name in LICENSE_FILES:
        shutil.copy2(root / name, staging / name)


def pack(staging: Path, out: Path, version: str) -> tuple[Path | None, list[str]]:
    """Run ``npm pack`` on ``staging`` into ``out``; return the tarball and problems."""

    result = subprocess.run(
        ["npm", "pack", "--json", "--ignore-scripts", "--pack-destination", str(out)],
        cwd=staging,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, [f"npm pack failed ({result.returncode}): {result.stderr.strip()}"]
    (report,) = json.loads(result.stdout)
    problems = []
    packed = sorted(entry["path"] for entry in report["files"])
    expected = sorted(PACKAGE_FILES + LICENSE_FILES)
    if packed != expected:
        problems.append(f"the tarball holds {packed}, expected exactly {expected}")
    if report["filename"] != tarball_name(version):
        problems.append(
            f"npm pack wrote {report['filename']!r}, expected {tarball_name(version)!r}"
        )
    return out / report["filename"], problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and check the @dtxsuite/dtxwiki npm launcher package."
    )
    parser.add_argument(
        "out", help="empty or missing directory to write the one tarball into"
    )
    arguments = parser.parse_args(argv)
    out = Path(arguments.out).resolve()
    # The release uploads this directory and publishes the tarball by name, so
    # a leftover from an earlier run must not ride along unchecked.
    if out.exists() and any(out.iterdir()):
        parser.error(f"{arguments.out} is not empty")
    out.mkdir(parents=True, exist_ok=True)

    problems = manifest_problems()
    tarball = None
    if not problems:
        version = json.loads(
            (ROOT / PACKAGE_DIR / "package.json").read_text(encoding="utf-8")
        )["version"]
        with tempfile.TemporaryDirectory() as staging:
            stage(ROOT, Path(staging))
            tarball, problems = pack(Path(staging), out, version)

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(tarball)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
