"""Synchronize vendor-neutral dtxWiki skills and the integrations license into
the Claude and Codex bundles."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

SKILL_NAMES = (
    "dtxwiki-conventions",
    "dtxwiki-retrieval",
    "dtxwiki-authoring-review",
    "dtxwiki-maintenance",
    "wiki-capture",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "integrations" / "agent-skills"
BUNDLE_ROOTS = (
    REPO_ROOT / "integrations" / "claude-plugin" / "plugins" / "dtxwiki" / "skills",
    REPO_ROOT / "integrations" / "codex-plugin" / "plugins" / "dtxwiki" / "skills",
)
# A marketplace installs only the plugin directory, so the Apache-2.0 text and
# notice under integrations/ must travel inside each plugin root.
LEGAL_FILES = tuple(
    REPO_ROOT / "integrations" / name for name in ("LICENSE", "NOTICE")
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_files() -> dict[str, Path]:
    expected = set(SKILL_NAMES)
    actual = {path.name for path in SOURCE_ROOT.iterdir() if path.is_dir()}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"source skill set mismatch: missing={missing}, extra={extra}"
        )

    files: dict[str, Path] = {}
    for name in SKILL_NAMES:
        skill_dir = SOURCE_ROOT / name
        entries = {path.name for path in skill_dir.iterdir()}
        if entries != {"SKILL.md"}:
            raise RuntimeError(
                f"{skill_dir} must contain only SKILL.md; found {sorted(entries)}"
            )
        files[name] = skill_dir / "SKILL.md"
    return files


def _assert_bundle_root(root: Path) -> None:
    resolved = root.resolve()
    integrations_root = (REPO_ROOT / "integrations").resolve()
    if integrations_root not in resolved.parents:
        raise RuntimeError(
            f"refusing to modify bundle outside integrations: {resolved}"
        )


def synchronize() -> None:
    sources = _source_files()
    expected = set(SKILL_NAMES)
    for root in BUNDLE_ROOTS:
        _assert_bundle_root(root)
        root.mkdir(parents=True, exist_ok=True)
        for entry in root.iterdir():
            if entry.name not in expected:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        for name, source in sources.items():
            target_dir = root / name
            target_dir.mkdir(parents=True, exist_ok=True)
            for entry in target_dir.iterdir():
                if entry.name != "SKILL.md":
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            target = target_dir / "SKILL.md"
            content = source.read_bytes()
            if not target.exists() or target.read_bytes() != content:
                target.write_bytes(content)
        for source in LEGAL_FILES:
            target = root.parent / source.name
            content = source.read_bytes()
            if not target.exists() or target.read_bytes() != content:
                target.write_bytes(content)


def verify() -> list[str]:
    sources = _source_files()
    failures: list[str] = []
    expected = set(SKILL_NAMES)
    for root in BUNDLE_ROOTS:
        actual = (
            {path.name for path in root.iterdir() if path.is_dir()}
            if root.exists()
            else set()
        )
        if actual != expected:
            failures.append(
                f"{root}: skill set mismatch: missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )
            continue
        for name, source in sources.items():
            target_dir = root / name
            entries = {path.name for path in target_dir.iterdir()}
            if entries != {"SKILL.md"}:
                failures.append(
                    f"{target_dir}: expected only SKILL.md, found {sorted(entries)}"
                )
                continue
            target = target_dir / "SKILL.md"
            source_hash = _digest(source)
            target_hash = _digest(target)
            if source_hash != target_hash:
                failures.append(
                    f"{target}: sha256 {target_hash} != source sha256 {source_hash}"
                )
        for source in LEGAL_FILES:
            target = root.parent / source.name
            if not target.is_file():
                failures.append(f"{target}: missing")
            elif _digest(target) != _digest(source):
                failures.append(f"{target}: differs from {source}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated skill copies without changing files",
    )
    args = parser.parse_args()

    if not args.check:
        synchronize()
    failures = verify()
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1

    for name in SKILL_NAMES:
        print(f"{name} {_digest(SOURCE_ROOT / name / 'SKILL.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
