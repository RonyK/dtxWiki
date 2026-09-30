"""Keep private overlay identifiers out of the public core surfaces."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]

_DENIED_LITERALS = ("dtxwiki_cloud", "dtxwiki-cloud")
_CONTRACT_EXCEPTIONS = {
    PurePosixPath("tests/static/test_architecture_boundaries.py"),
    PurePosixPath("tests/static/test_leak_denylist.py"),
}


def _tracked_files() -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        PurePosixPath(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def _contains_denied_literal(content: bytes, denied: str) -> bool:
    normalized = content.lower()
    return any(
        encoded in normalized
        for encoded in (
            denied.encode("utf-8"),
            denied.encode("utf-16-le"),
            denied.encode("utf-16-be"),
        )
    )


def leaked_literals(
    relative_paths: list[PurePosixPath], root: Path = ROOT
) -> list[str]:
    """Every denied literal found in a path name or in a file's bytes.

    Extracted from the test body so a counterexample can hand it a violating
    input directly. Inline, the path-name branch below had no caller that could
    reach it with a matching name, so nothing ever exercised it.
    """

    violations: list[str] = []
    for relative_path in relative_paths:
        if relative_path in _CONTRACT_EXCEPTIONS:
            continue
        normalized_path = relative_path.as_posix().casefold()
        for denied in _DENIED_LITERALS:
            if denied in normalized_path:
                violations.append(f"{relative_path}: path contains {denied}")
        try:
            content = (root / Path(relative_path.as_posix())).read_bytes()
        except FileNotFoundError:
            # Tracked in the index but absent from the working tree (mid-rebase,
            # sparse checkout). Nothing to scan; the filename check above still
            # guards the path.
            continue
        for denied in _DENIED_LITERALS:
            if _contains_denied_literal(content, denied):
                violations.append(f"{relative_path}: contains {denied}")
    return violations


def test_private_overlay_names_do_not_leak_into_public_surfaces():
    assert leaked_literals(_tracked_files()) == []


@pytest.mark.counterexample(
    "leaked_literals reports both branches: a denied literal in a file's bytes, "
    "and a denied literal in the path name of a file that does not exist on disk"
)
def test_leaked_literals_reports_a_violating_path_and_a_violating_body(
    tmp_path: Path,
) -> None:
    denied = _DENIED_LITERALS[0]
    (tmp_path / "body.txt").write_bytes(f"prefix {denied.upper()} suffix".encode())

    reported = leaked_literals(
        [
            PurePosixPath("body.txt"),
            PurePosixPath(f"{denied}/never-written.txt"),
            PurePosixPath("clean.txt"),
        ],
        root=tmp_path,
    )

    assert reported == [
        f"body.txt: contains {denied}",
        f"{denied}/never-written.txt: path contains {denied}",
    ], (
        "leaked_literals must report the path-name hit even when the file is "
        f"absent from disk, and must casefold the body scan. Got: {reported}"
    )


def test_private_overlay_names_are_detected_in_utf16_files():
    for encoding in ("utf-16-le", "utf-16-be"):
        content = ("prefix " + _DENIED_LITERALS[0] + " suffix").encode(encoding)
        assert _contains_denied_literal(content, _DENIED_LITERALS[0])


if __name__ == "__main__":
    test_private_overlay_names_do_not_leak_into_public_surfaces()
