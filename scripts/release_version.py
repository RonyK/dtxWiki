"""Read, synchronize and check release identities from pyproject.toml.

Only the project's own lock entries change; dependency resolutions and schema
release boundaries are deliberately preserved. README files remain directly
editable: only recognizable references to the current app release are changed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_MANIFESTS = (
    "desktop/package.json",
    "npm/dtxwiki/package.json",
    "integrations/claude-plugin/plugins/dtxwiki/.claude-plugin/plugin.json",
    "integrations/codex-plugin/plugins/dtxwiki/.codex-plugin/plugin.json",
)
OPENAPI = "shared/schemas/rest/v1/openapi.json"
README_PATHS = ("README.md", "README.ko.md", "npm/dtxwiki/README.md")
_README_VERSION = r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
_README_PATTERNS = tuple(
    re.compile(pattern.replace("<VERSION>", _README_VERSION))
    for pattern in (
        r"(?<![\w-])dtxwiki==<VERSION>(?![\w.+-])",
        r"@dtxsuite/dtxwiki@<VERSION>(?![\w.+-])",
        r"https://github\.com/dtxSuite/dtxWiki/(?:blob|tree|releases/download)/v<VERSION>/",
        r"https://raw\.githubusercontent\.com/dtxSuite/dtxWiki/v<VERSION>/",
        r"(?<![\w-])dtxwiki-<VERSION>(?=-[^\s/\"'<>]*\.whl\b)",
        r"git clone --branch v<VERSION>(?=\s+https://github\.com/dtxSuite/dtxWiki(?:\.git)?(?:\s|$))",
        r"^This guide is for \*\*dtxWiki <VERSION> \(Beta\)",
        r"^이 안내는 \*\*dtxWiki <VERSION> \(Beta\)",
        r"Upgrading to <VERSION>(?=\]|\s*$)",
        r"(?:\[|^## )<VERSION>(?:으)?로 업데이트하기",
        r"^Install <VERSION> using your existing installation method",
        r"^기존 설치 방식을 사용해 <VERSION>(?:을|를) 설치하되",
        r"On the server, download the <VERSION> installer",
        r"서버에서 <VERSION> 설치 프로그램과",
        r"^Complete the <VERSION> README's audit",
        r"^covers setup for <VERSION>\.",
    )
)
_HISTORY_CONTEXT = re.compile(
    r"\b(?:history|historical|legacy|(?:previous|older) (?:versions?|releases?))\b"
    r"|과거|이전\s*(?:버전|릴리스)",
    re.IGNORECASE,
)


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("release version must be canonical MAJOR.MINOR.PATCH")
    return version


def source_version(root: Path = ROOT) -> str:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return _validate_version(metadata["project"]["version"])


def _json(root: Path, path: str) -> dict:
    return json.loads((root / path).read_text(encoding="utf-8"))


def _dump(value: dict, *, sort_keys: bool = False, ensure_ascii: bool = False) -> str:
    return (
        json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=ensure_ascii)
        + "\n"
    )


def _version_line(text: str, version: str) -> str:
    result, count = re.subn(
        r'(?m)^(version\s*=\s*)"[^"\n]*"',
        lambda match: f'{match[1]}"{version}"',
        text,
    )
    if count != 1:
        raise ValueError("expected exactly one version field in the TOML section")
    return result


def _project_text(root: Path, version: str) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    sections = re.split(r"(?m)(?=^\[)", text)
    matches = [
        i for i, section in enumerate(sections) if section.startswith("[project]\n")
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one [project] section")
    index = matches[0]
    sections[index] = _version_line(sections[index], version)
    return "".join(sections)


def _uv_package(root: Path) -> dict:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == "dtxwiki"]
    if len(packages) != 1 or packages[0].get("source") != {"editable": "."}:
        raise ValueError("uv.lock must contain one editable dtxwiki package")
    return packages[0]


def _uv_text(root: Path, version: str) -> str:
    _uv_package(root)
    text = (root / "uv.lock").read_text(encoding="utf-8")
    blocks = re.split(r"(?m)(?=^\[\[package\]\])", text)
    for i, block in enumerate(blocks):
        if re.search(r'(?m)^name = "dtxwiki"$', block):
            blocks[i] = _version_line(block, version)
            return "".join(blocks)
    raise ValueError("could not locate dtxwiki in uv.lock")


def _readme_references(text: str) -> list[tuple[int, int, str]]:
    """Locate current app references without treating every number as a version.

    Explicit history sections and historical inline examples remain evidence.
    Docs minor-series URLs, dependency versions, anchors and dev asset URLs have
    separate lifetimes and are not part of these app-release patterns.
    """
    references = {}
    offset = 0
    history_depth = None
    fence = None
    for line in text.splitlines(keepends=True):
        delimiter = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if delimiter:
            marker = delimiter[1]
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
        elif fence is None and (heading := re.match(r"^(#{1,6})\s+(.+)", line)):
            depth = len(heading[1])
            if history_depth is not None and depth <= history_depth:
                history_depth = None
            if history_depth is None and (
                _HISTORY_CONTEXT.search(heading[2])
                or re.match(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:\s|$)", heading[2])
            ):
                history_depth = depth
        if history_depth is None and not _HISTORY_CONTEXT.search(line):
            for pattern in _README_PATTERNS:
                for match in pattern.finditer(line):
                    start, end = match.span("version")
                    references[offset + start, offset + end] = match["version"]
        offset += len(line)
    return [(start, end, value) for (start, end), value in sorted(references.items())]


def _readme_outputs(root: Path, version: str) -> dict[str, str]:
    outputs = {}
    for path in README_PATHS:
        text = (root / path).read_text(encoding="utf-8")
        for start, end, _ in reversed(_readme_references(text)):
            text = text[:start] + version + text[end:]
        outputs[path] = text
    return outputs


def export_openapi(root: Path) -> dict:
    process = subprocess.run(
        [sys.executable, "-m", "backend.scripts.export_openapi"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise ValueError(f"OpenAPI export failed:\n{process.stderr[-2000:]}")
    return json.loads(process.stdout)


def synchronize(root: Path = ROOT, version: str | None = None) -> list[str]:
    current = source_version(root)
    target = _validate_version(version) if version is not None else current
    # Prepare every output before writing anything: malformed input, missing
    # README files or a broken exporter must not leave a half-updated release.
    outputs = {"pyproject.toml": _project_text(root, target)}
    for path in JSON_MANIFESTS:
        manifest = _json(root, path)
        manifest["version"] = target
        outputs[path] = _dump(manifest)
    lock = _json(root, "package-lock.json")
    lock["packages"]["desktop"]["version"] = target
    outputs["package-lock.json"] = _dump(lock)
    outputs["uv.lock"] = _uv_text(root, target)
    outputs.update(_readme_outputs(root, target))
    # Export the real API, rather than only patching a possibly stale snapshot.
    # A set operation has not written the new source version yet, so bind its
    # identity to the validated target after exporting the unchanged API.
    schema = export_openapi(root)
    schema["info"]["version"] = target
    outputs[OPENAPI] = _dump(schema, sort_keys=True, ensure_ascii=True)
    changed = []
    for path, text in outputs.items():
        destination = root / path
        if destination.read_text(encoding="utf-8") != text:
            changed.append(path)
    for path in changed:
        (root / path).write_text(outputs[path], encoding="utf-8")
    return changed


def check(root: Path = ROOT) -> list[str]:
    version = source_version(root)
    problems = []
    for path in JSON_MANIFESTS:
        if _json(root, path).get("version") != version:
            problems.append(f"{path}: expected version {version}")
    if _json(root, "package-lock.json")["packages"]["desktop"]["version"] != version:
        problems.append(f"package-lock.json: expected desktop version {version}")
    if _uv_package(root)["version"] != version:
        problems.append(f"uv.lock: expected dtxwiki version {version}")
    if _json(root, OPENAPI)["info"]["version"] != version:
        problems.append(f"{OPENAPI}: expected info.version {version}")
    for path in README_PATHS:
        text = (root / path).read_text(encoding="utf-8")
        for start, _, found in _readme_references(text):
            if found != version:
                line = text.count("\n", 0, start) + 1
                problems.append(
                    f"{path}:{line}: expected release version {version}, found {found}"
                )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="print the source release version")
    commands.add_parser("check", help="check mirrors and current README app references")
    commands.add_parser("sync", help="regenerate mirrors from the source version")
    setter = commands.add_parser("set", help="set a version and regenerate mirrors")
    setter.add_argument("version")
    args = parser.parse_args()
    try:
        if args.command == "show":
            print(source_version())
            return 0
        if args.command in {"set", "sync"}:
            changed = synchronize(version=getattr(args, "version", None))
            for path in changed:
                print(f"Updated {path}")
        problems = check()
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            print(
                "Run `make sync-version` to regenerate release files.", file=sys.stderr
            )
            return 1
        print(f"Release version {source_version()} is synchronized.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
