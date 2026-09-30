"""Detailed design and plan documents must never be tracked by this repository.

This repository is published. Detailed internal design, planning, and review
documents are shared through dtxWiki instead, so they must not reach git.

This replaces an earlier pair of tests that enforced the opposite invariant --
that `docs/design/epic1-core-contract.md` was a *tracked* public baseline. That
premise was reversed by an explicit 2026-08-26 decision to exclude `docs/` from
the repository. The lesson worth keeping from the old tests is that a documented
intention drifts unless something checks it, so the intention is re-encoded here
in its new direction rather than simply deleted.

`.gitignore` alone is not sufficient evidence: a path already committed stays
tracked no matter what `.gitignore` later says, which is exactly how `docs/`
survived while everyone believed it was excluded. Ask git what it actually
tracks.

Two decisions this file now pins so they are not relitigated:

* `review/` is private. It held 355 tracked files of adversarial review reports
  -- reviewed branch names, HEAD and baseline SHAs, unfixed defect rosters --
  published for months while this guard reported green, because its root list
  named only `doc/` and `docs/`. Rooted lists rot; the allowlist test below
  fails on ANY new tracked top-level directory instead.
* `internal/audit/` is the one deliberate retention exception. It is the
  hand-written, redaction-scanned operator record: `.gitignore` names it as the
  destination for operator artifacts, `tests/packaging/test_evidence_redaction.py`
  secret-scans it, and `scripts/verify_all.sh` reads its `vm-smoke.md`. It stays
  tracked and un-ignored, and a test here says so, so that a later cleanup
  sweeping up `review/`-like trees cannot quietly take it too.
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
    ("doc/", "doc/Epic-1/nested/__ignore_probe__.md"),
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

CITATION = re.compile(
    r"(?<![A-Za-z0-9_/.-])(?:doc|docs|review)/[A-Za-z0-9_./-]+\.[a-z]{2,5}\b"
)

# The leading lookbehind is what separates a citation from a URL: the character
# before `docs/` in an https path is `/`, so the pattern does not match.

# This module has to spell the roots out in order to guard them.
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
        hits: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in CITATION.finditer(line):
                hits.append((lineno, match.group(0)))
        if hits:
            found[path] = hits
    return found


def citation_pairs(paths: list[str]) -> set[tuple[str, str]]:
    """Each distinct (tracked file, private path it cites) pair."""

    return {
        (path, cited)
        for path, hits in unresolvable_citations(paths).items()
        for _, cited in hits
    }


def citations_outside(
    baseline: frozenset[tuple[str, str]], paths: list[str]
) -> list[tuple[str, str]]:
    return sorted(citation_pairs(paths) - baseline)


# Generated from the tree, not hand-written. May only shrink. Keyed by the cited
# path, not the file: a file-level entry let a grandfathered file add new private
# citations without failing.
CITATION_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        ("AGENTS.md", "doc/design/epic1-core-contract.md"),
        (
            "backend/api/context_media.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        ("backend/api/deps.py", "doc/design/workspace-database-separation-plan.md"),
        (
            "backend/app/container.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        ("backend/app/settings.py", "doc/design/workspace-database-separation-plan.md"),
        (
            "backend/context_media/ports/repository.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        ("backend/mcp/read_tools.py", "doc/design/block-provenance-contract.md"),
        (
            "backend/mcp/upload_tools.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/models/context_media.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        ("backend/models/job.py", "doc/design/workspace-database-separation-plan.md"),
        ("backend/schemas/common.py", "doc/design/block-provenance-contract.md"),
        (
            "backend/schemas/common.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/services/context_media_access.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/services/context_media_orphan_sweep.py",
            "doc/design/api-surface-redesign.md",
        ),
        (
            "backend/services/context_media_orphan_sweep.py",
            "review/2026-09-04-minor-fixes-vs-feature-epic1-code-review.md",
        ),
        ("backend/services/context_push.py", "doc/design/block-provenance-contract.md"),
        (
            "backend/services/job_hints.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/services/job_queue.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/services/job_read_service.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/services/library_content_service.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_admin_jobs_fanout.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_cloud_composition.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_context_media_orphan_api.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_context_media_orphan_sweep.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_context_media_orphan_sweep_worker.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/integration/test_edge_registry.py",
            "doc/design/api-surface-redesign.md",
        ),
        (
            "backend/tests/integration/test_edge_registry.py",
            "doc/design/workspace-tenancy-contract.md",
        ),
        (
            "backend/tests/integration/test_job_visibility_api.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/mcp_contract/test_upload_tools.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/unit/test_overlay_contract_surface.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "backend/tests/unit/test_restricted_library_policy.py",
            "doc/design/workspace-tenancy-contract.md",
        ),
        (
            "backend/tests/unit/test_settings_mcp_http_mode.py",
            "doc/design/mcp-transport-contract.md",
        ),
        (
            "backend/tests/utils/test_markdown_outline.py",
            "doc/design/block-provenance-contract.md",
        ),
        (
            "backend/utils/actor_display.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
        ("backend/utils/block_map.py", "doc/design/block-provenance-contract.md"),
        (
            "backend/utils/markdown_outline.py",
            "doc/design/block-provenance-contract.md",
        ),
        ("backend/worker/entry.py", "doc/design/workspace-database-separation-plan.md"),
        (
            "frontend/src/features/settings/AdminJobsSection.tsx",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "frontend/src/features/settings/tests/AdminJobsSection.test.tsx",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "frontend/src/shared/api/adminJobs.ts",
            "doc/design/workspace-database-separation-plan.md",
        ),
        (
            "frontend/src/shared/lib/activityLabels.ts",
            "doc/design/activity-classification-contract.md",
        ),
        ("playwright.config.ts", "docs/reviews/phase5/FINAL-REVIEW.md"),
        (
            "shared/fixtures/content_hash_vectors.json",
            "doc/sw-design/06_mcp_and_agent_contracts.md",
        ),
        ("tests/e2e/test_release_gate_check.py", "doc/setup/remote-mcp-clients.md"),
        (
            "tests/fixtures/client-matrix.md",
            "doc/Epic-0/Sprint-5/evidence/phase0-client-matrix.md",
        ),
        (
            "tests/static/test_epic1_contract_claims.py",
            "doc/design/epic1-core-contract.md",
        ),
        ("tests/static/test_public_schema_path_leak.py", "doc/Epic-2/...md"),
        (
            "tests/static/test_public_schema_path_leak.py",
            "doc/Epic-2/260828_workspace_db_isolation_plan.md",
        ),
        ("tests/static/test_public_schema_path_leak.py", "doc/Epic-2/plan.md"),
        (
            "tests/static/test_release_gate_contract.py",
            "doc/setup/remote-mcp-clients.md",
        ),
        (
            "tests/static/test_workspace_plane_boundary.py",
            "doc/design/workspace-database-separation-plan.md",
        ),
    }
)


def _format_pairs(pairs: list[tuple[str, str]]) -> str:
    return "\n  ".join(f"{path}: {cited}" for path, cited in pairs)


def test_no_tracked_file_cites_an_untrackable_document_root() -> None:
    new = citations_outside(CITATION_BASELINE, tracked_paths())
    assert new == [], (
        "These tracked files tell a reader to open a document under doc/, docs/ "
        "or review/. This repository is required never to track those roots "
        "(see test_detailed_design_documents_are_not_tracked), so the citation "
        "cannot resolve for anyone who clones it:\n  " + _format_pairs(new)
    )


def test_the_citation_baseline_only_shrinks() -> None:
    stale = sorted(CITATION_BASELINE - citation_pairs(tracked_paths()))
    assert stale == [], (
        "These baseline entries no longer occur -- the citation was removed or "
        "the file deleted. Remove them from CITATION_BASELINE so it keeps "
        "shrinking:\n  " + _format_pairs(stale)
    )


@pytest.mark.counterexample(
    "unresolvable_citations reports a bare citation of doc/ and does not report "
    "the same path inside an http URL; both halves die if CITATION's leading "
    "lookbehind is removed"
)
def test_the_resolver_separates_a_citation_from_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calls the production function. A counterexample that re-implements the
    logic it is checking passes no matter what the production copy does -- which
    is the defect class this registry exists to catch."""

    (tmp_path / "cites.py").write_text(
        "# see doc/design/whatever.md for the rationale\n", encoding="utf-8"
    )
    (tmp_path / "links.py").write_text(
        "# see https://example.com/repo/blob/main/docs/tools/thing.md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tests.static.test_design_source_contract.ROOT", tmp_path, raising=False
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)

    reported = unresolvable_citations(["cites.py", "links.py"])

    assert reported == {"cites.py": [(1, "doc/design/whatever.md")]}, (
        "unresolvable_citations must report the bare citation and must not "
        f"report the one inside a URL. Got: {reported}"
    )


@pytest.mark.counterexample(
    "a grandfathered file that gains a new private citation is reported; dies if "
    "the baseline is keyed by file instead of by (file, cited path)"
)
def test_a_baselined_file_cannot_add_a_new_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "old.py").write_text(
        "# see doc/design/old.md\n# see doc/design/new.md\n", encoding="utf-8"
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)

    baseline = frozenset({("old.py", "doc/design/old.md")})

    assert citations_outside(baseline, ["old.py"]) == [("old.py", "doc/design/new.md")]
