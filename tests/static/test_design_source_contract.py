"""Check public source tracking and private-document exclusion using Git.

These guards inspect tracked files and ignore rules without loading private
documents. Only internal/audit/ is retained outside the shipped source tree;
tests/packaging/test_evidence_redaction.py scans that operator evidence.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Directories whose contents are detailed internal documents.
UNTRACKED_DOCUMENT_ROOTS = ("doc/", "docs/", "review/")

# Nested probes, not just top-level ones: a rule written `review/*` would ignore
# `review/x.md` and miss `review/evidence/dev-latest/x.png`, which is where the
# bulk of a review tree actually lives.
IGNORE_PROBES = (
    ("doc/", "doc/__ignore_probe__.md"),
    ("doc/", "doc/design/nested/__ignore_probe__.md"),
    ("docs/", "docs/__ignore_probe__.md"),
    ("docs/", "docs/design/nested/__ignore_probe__.md"),
    ("review/", "review/__ignore_probe__.md"),
    ("review/", "review/evidence/dev-latest/__ignore_probe__.png"),
)

# Every path `.gitignore` deliberately re-includes with a `!` negation. Git
# reports no error when a parent-directory rule silently voids a negation, so
# keep a behavioural guard for the remaining publishable configuration files.
PUBLISHABLE_EXCEPTIONS = tuple(
    sorted(
        {
            ".vscode/extensions.json",
            ".vscode/settings.json.example",
            ".env.example",
            ".env.docker.example",
        }
    )
)

# The single deliberate exception: hand-written operator audit notes, kept in the
# published tree and secret-scanned by tests/packaging/test_evidence_redaction.py.
RETAINED_EVIDENCE_ROOT = "internal/audit/"

# Tracked top-level directories that make up the shipped source surface. Adding a
# legitimate new one is an intentional edit here; adding a review or evidence
# tree by habit fails instead.
SOURCE_TREE_DIRS = frozenset(
    {
        ".github",
        "backend",
        "desktop",
        "frontend",
        "infra",
        "integrations",
        "npm",
        "scripts",
        "shared",
        "tests",
    }
)


def is_ignored(path: str) -> bool:
    """Ask git, not a pattern parser. `-q` (not `-v`) is load-bearing.

    With `--verbose`, `git check-ignore` exits 0 whenever ANY pattern matched --
    including a negation -- so a `-v` based check reads a re-included file as
    ignored. Without it, exit 0 means ignored and 1 means not.
    """

    return (
        subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [entry for entry in result.stdout.split("\0") if entry]


def test_detailed_design_documents_are_not_tracked() -> None:
    tracked = tracked_paths()
    offenders = sorted(
        path
        for path in tracked
        if path.startswith(UNTRACKED_DOCUMENT_ROOTS)
    )

    assert offenders == [], (
        "Detailed design/plan documents must not be tracked in this public "
        f"repository: {offenders}. Remove them with `git rm -r --cached <dir>`, "
        "keep the files on disk, and share the document through dtxWiki instead."
    )


@pytest.mark.parametrize(("directory", "probe"), IGNORE_PROBES)
def test_gitignore_covers_document_roots(directory: str, probe: str) -> None:
    """Ignore rules and tracking must agree, so a new file is not added by habit."""

    assert is_ignored(probe), (
        f"{probe} is not covered by .gitignore, so a newly created document "
        f"under {directory} would be committed by accident."
    )


@pytest.mark.parametrize("path", PUBLISHABLE_EXCEPTIONS)
def test_publishable_exceptions_are_actually_publishable(path: str) -> None:
    """A negation silently voided by a parent rule is worse than no rule."""

    assert not is_ignored(path), (
        f"{path} is an intentional publishable exception, but .gitignore still "
        "excludes it. A parent-directory rule such as `.vscode/` cannot be "
        "undone by a later negation -- write `.vscode/*` so git descends into "
        "the directory and evaluates the children."
    )


def test_only_internal_audit_is_retained_outside_the_source_tree() -> None:
    """The general form of the review/ leak: any new tracked root is a decision.

    A hardcoded list of private roots cannot see a directory nobody thought to
    add to it. Inverting the question -- what IS tracked at the top level? --
    catches `evidence/`, `notes/` or `audit-2026/` on the commit that adds them.
    """

    # Exempt the retention root ITSELF, not its parent. Subtracting the bare
    # top-level segment "internal" exempted every depth under it, so
    # `internal/reviews/epic1-defect-roster.md` -- the next iteration of exactly
    # the leak this test exists for -- passed green. Verified by mutation: two
    # such paths injected into tracked_paths() survived all three guards.
    tracked_top_dirs = set()
    for path in tracked_paths():
        if "/" not in path:
            continue
        if path.startswith(RETAINED_EVIDENCE_ROOT):
            continue
        head, _, _ = path.partition("/")
        # Report the offender at the depth a reader can act on: a stray tree
        # under the retention root is named in full, not collapsed to
        # "internal".
        retention_parent = RETAINED_EVIDENCE_ROOT.split("/")[0]
        tracked_top_dirs.add(
            path.rsplit("/", 1)[0] if head == retention_parent else head
        )
    unexpected = sorted(tracked_top_dirs - SOURCE_TREE_DIRS)

    assert unexpected == [], (
        f"Unexpected tracked top-level directories: {unexpected}. This is a "
        "published repository: review reports, design documents and evidence "
        "trees belong on disk and in dtxWiki, not in git. Untrack with "
        "`git rm -r --cached <dir>` (the files stay on disk) and add the "
        "directory to the private block in .gitignore. If the directory really "
        "is shipped source, add it to SOURCE_TREE_DIRS in the same edit."
    )


def test_retention_exception_is_declared_and_secret_scanned() -> None:
    """internal/audit/ is kept ON PURPOSE, and only because it is scanned.

    Pinning the reason here is what makes untracking review/ safe: a later
    cleanup that treats every non-source directory as a leak has to confront
    this test rather than silently deleting the operator record, and a cleanup
    that keeps the directory but drops the secret scan fails too.
    """

    assert not is_ignored(f"{RETAINED_EVIDENCE_ROOT}__ignore_probe__.md"), (
        f"{RETAINED_EVIDENCE_ROOT} is the one deliberate retention exception; "
        "ignoring it would make the operator audit record undeliverable."
    )

    retained = [
        path for path in tracked_paths() if path.startswith(RETAINED_EVIDENCE_ROOT)
    ]
    assert retained, (
        f"No tracked file under {RETAINED_EVIDENCE_ROOT}. Either the operator "
        "audit record was untracked by mistake, or the exception should be "
        "removed from .gitignore and AGENTS.md deliberately."
    )

    redaction_src = (ROOT / "tests/packaging/test_evidence_redaction.py").read_text(
        encoding="utf-8"
    )
    assert 'ROOT / "internal/audit"' in redaction_src, (
        "tests/packaging/test_evidence_redaction.py no longer scans "
        f"{RETAINED_EVIDENCE_ROOT}. That scan is the entire justification for "
        "keeping this directory in a published repository."
    )


def test_gitignore_has_no_duplicate_rules() -> None:
    """A duplicated rule means one of the two comments above it is a lie.

    `docs/` appeared twice; the trailing one was what actually matched, so the
    documented block above the first one read as authoritative while being dead
    code. Editing dead rules is how intent and behaviour drift apart.
    """

    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    patterns = [
        stripped
        for stripped in (line.strip() for line in lines)
        if stripped and not stripped.startswith("#")
    ]
    duplicates = sorted(
        pattern for pattern, count in Counter(patterns).items() if count > 1
    )

    assert duplicates == [], (
        f".gitignore repeats these rules: {duplicates}. Keep one occurrence, "
        "under the comment that explains it."
    )


# The guards above answer "is a document tracked". The resolver below answers
# "does a tracked file tell a reader to open a document this repository is
# required never to track".

_PATH_CONTINUATION = re.compile(r"[ \t]*\r?\n[ \t]*(?:(?:#|//|\*)[ \t]*)?")
_PATH_WRAP = rf"(?:{_PATH_CONTINUATION.pattern})?"
CITATION = re.compile(
    rf"(?<![A-Za-z0-9_/.-])(?:doc|docs|review)/{_PATH_WRAP}"
    rf"(?:[A-Za-z0-9_.-]+/{_PATH_WRAP})*[A-Za-z0-9_.-]+\.[a-z]{{2,5}}\b"
)

# The leading lookbehind is what separates a citation from a URL: the character
# before `docs/` in an https path is `/`, so the pattern does not match.

# This module uses synthetic paths to test the guard, not real private documents.
SELF = "tests/static/test_design_source_contract.py"


def unresolvable_citations(paths: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Tracked files citing a document root git is required never to track.

    Proves only that the citation cannot resolve *in this repository*. It says
    nothing about whether the cited document exists on the author's disk, and
    that is the point: a reader who clones this repository cannot open it.
    """

    found: dict[str, list[tuple[int, str]]] = {}
    for path in paths:
        if path == SELF or path == ".gitignore":
            continue
        target = ROOT / path
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [
            (
                text.count("\n", 0, match.start()) + 1,
                _PATH_CONTINUATION.sub("", match.group(0)),
            )
            for match in CITATION.finditer(text)
        ]
        if hits:
            found[path] = hits
    return found


def test_no_tracked_file_cites_an_untrackable_document_root() -> None:
    citations = unresolvable_citations(tracked_paths())
    details = "\n  ".join(
        f"{path}:{line}: {cited}"
        for path, hits in sorted(citations.items())
        for line, cited in hits
    )
    assert citations == {}, (
        "Tracked files must not cite documents under doc/, docs/ or review/. "
        "Those roots are private and absent from a clean clone. Describe the "
        "behavior directly or cite tracked source instead:\n  " + details
    )


@pytest.mark.counterexample(
    "bare citations and paths wrapped in Python/TypeScript comments are reported "
    "without opening targets; bare and wrapped URLs are excluded"
)
def test_the_resolver_separates_a_citation_from_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use synthetic paths whose target documents do not exist."""

    (tmp_path / "cites.py").write_text(
        "# see doc/\n"
        "# example.md for the rationale\n"
        "# see docs/nested/example.md for the rationale\n"
        '"""see review/\n'
        '    example.md for the rationale"""\n',
        encoding="utf-8",
    )
    (tmp_path / "links.py").write_text(
        "# see https://example.com/repo/blob/main/docs/tools/example.md\n",
        encoding="utf-8",
    )
    (tmp_path / "cites.ts").write_text(
        "// see doc/design/\n"
        "// example.md for the rationale\n"
        "/** see review/\n"
        " * nested/\n"
        " * example.md for the rationale\n"
        " */\n"
        "// see https://example.com/docs/\n"
        "// tools/example.md\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)

    reported = unresolvable_citations(["cites.py", "links.py", "cites.ts"])

    assert reported == {
        "cites.py": [
            (1, "doc/example.md"),
            (3, "docs/nested/example.md"),
            (4, "review/example.md"),
        ],
        "cites.ts": [
            (1, "doc/design/example.md"),
            (3, "review/nested/example.md"),
        ],
    }, (
        "unresolvable_citations must report every bare citation and exclude "
        f"the URL. Got: {reported}"
    )
