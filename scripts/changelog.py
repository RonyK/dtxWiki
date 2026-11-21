"""Generate concise PR-linked changelogs and read confirmed release notes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "dtxSuite/dtxWiki"
GROUPS = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
}
CONVENTIONAL = re.compile(r"^(\w+)(?:\(([^)]+)\))?(!)?:\s*(.+)$")
BREAKING_CHANGE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
PR_SUFFIX = re.compile(r"\s*(?:\(#(\d+)\)|-?\s+#(\d+))$")
MERGE_PR = re.compile(r"^Merge pull request #(\d+) from ")
ENTRY_LINK = re.compile(r"^- .*\]\((\S+)\)\)$")
RELEASE = re.compile(
    r"^chore\(release\):|^Release\s+v?\d+\.\d+|"
    r"^Merge\b.*\brelease[/ -]v?\d+\.\d+",
    re.IGNORECASE,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _is_breaking(conventional: re.Match[str] | None, body: str) -> bool:
    return conventional is not None and bool(
        conventional[3] or BREAKING_CHANGE.search(body)
    )


def generate(since: str, until: str = "HEAD", root: Path = ROOT) -> str:
    # Resolve refs before building a revision range; missing tags must not silently
    # produce an incomplete changelog. Full traversal retains changes in merges.
    start = git(root, "rev-parse", "--verify", f"{since}^{{commit}}")
    end = git(root, "rev-parse", "--verify", f"{until}^{{commit}}")
    records = git(
        root,
        "log",
        "--topo-order",
        "--format=%H%x00%P%x00%s%x00%b%x00%x1e",
        f"{start}..{end}",
    )
    commits = [
        record.strip().split("\0")[:4]
        for record in records.split("\x1e")
        if record.strip()
    ]
    breaking_commits = {
        sha
        for sha, _, subject, body in commits
        if _is_breaking(CONVENTIONAL.match(subject), body)
    }
    grouped: dict[str, list[str]] = defaultdict(list)
    covered: set[str] = set()
    seen_prs: set[str] = set()
    for sha, parents, subject, body in commits:
        if sha in covered or RELEASE.search(subject) or RELEASE.search(body):
            continue
        pr = None
        branch_breaking = False
        if len(parents.split()) > 1:
            # A regular merge PR represents its branch once. Release merges are
            # skipped above, leaving their constituent changes available.
            merge = MERGE_PR.match(subject)
            if not merge:
                continue
            pr = merge[1]
            parent_refs = parents.split()
            merged_commits = git(
                root, "rev-list", *parent_refs[1:], f"^{parent_refs[0]}"
            ).splitlines()
            covered.update(merged_commits)
            branch_breaking = not breaking_commits.isdisjoint(merged_commits)
            subject = next(
                (line.strip() for line in body.splitlines() if line.strip()), subject
            )
        suffix = PR_SUFFIX.search(subject)
        if suffix:
            pr = pr or suffix[1] or suffix[2]
            subject = subject[: suffix.start()].rstrip()
        if pr:
            if pr in seen_prs:
                continue
            seen_prs.add(pr)
        conventional = CONVENTIONAL.match(subject)
        group = "Other changes"
        if conventional:
            kind, scope, _, subject = conventional.groups()
            group = GROUPS.get(kind, "Other changes")
            if scope:
                subject = f"**{scope}**: {subject}"
        if branch_breaking or _is_breaking(conventional, body):
            subject = f"**Breaking:** {subject}"
        link = (
            f"[#{pr}](https://github.com/{REPOSITORY}/pull/{pr})"
            if pr
            else f"[{sha[:7]}](https://github.com/{REPOSITORY}/commit/{sha})"
        )
        grouped[group].append(f"- {subject} ({link})")
    sections = ["## Unreleased"]
    for group in (*GROUPS.values(), "Other changes"):
        if grouped[group]:
            sections.append(f"### {group}\n\n" + "\n".join(grouped[group]))
    return "\n\n".join(sections) + "\n"


def _entry_notes(section: str) -> dict[str, str]:
    """Map each entry's link to the indented lines written under it."""
    notes: dict[str, list[str]] = defaultdict(list)
    link = None
    for line in section.splitlines():
        entry = ENTRY_LINK.match(line)
        if entry:
            link = entry[1]
        elif link and (line.startswith((" ", "\t")) or not line.strip()):
            notes[link].append(line)
        else:
            link = None
    return {
        link: block
        for link, lines in notes.items()
        if (block := "\n".join(lines).rstrip())
    }


def update_unreleased(text: str, generated: str) -> str:
    headings = list(re.finditer(r"^## .+$", text, re.MULTILINE))
    unreleased = [heading for heading in headings if heading[0] == "## Unreleased"]
    if len(unreleased) > 1:
        raise ValueError("CHANGELOG.md has multiple Unreleased sections")
    if not unreleased:
        position = headings[0].start() if headings else len(text)
        return (
            text[:position].rstrip()
            + "\n\n"
            + generated.rstrip()
            + "\n\n"
            + text[position:]
        )
    heading = unreleased[0]
    following = next(
        (item.start() for item in headings if item.start() > heading.start()),
        len(text),
    )
    section = text[heading.end() : following]
    # Hand-written text that PR titles cannot carry survives regeneration: the
    # Important changes subsection, and indented lines under an entry whose PR or
    # commit link is still generated.
    important = re.search(
        r"^### Important changes\n.*?(?=^### |\Z)", section, re.MULTILINE | re.DOTALL
    )
    if important:
        section = section[: important.start()] + section[important.end() :]
    notes = _entry_notes(section)
    lines = []
    for line in generated.rstrip().splitlines():
        lines.append(line)
        entry = ENTRY_LINK.match(line)
        if entry and entry[1] in notes:
            lines.append(notes[entry[1]])
    title, _, rest = "\n".join(lines).partition("\n")
    if important:
        title += "\n\n" + important[0].strip()
    body = (title + "\n" + rest).rstrip()
    return text[: heading.start()] + body + "\n\n" + text[following:]


def version_section(text: str, version: str) -> str:
    """Return the CHANGELOG entry headed `## <version>`, or "" without exactly one."""
    headings = list(re.finditer(r"^## .+$", text, re.MULTILINE))
    matches = [
        item
        for item in headings
        if re.fullmatch(rf"## {re.escape(version)}(?:\s.*)?", item[0])
    ]
    if len(matches) != 1:
        return ""
    end = next(
        (item.start() for item in headings if item.start() > matches[0].start()),
        len(text),
    )
    content = text[matches[0].end() : end].strip()
    if not any(
        line.strip() and not line.startswith("#") for line in content.splitlines()
    ):
        return ""
    return content


def release_notes(version: str, section: str) -> str:
    python_version = Version(version)
    notes = f"dtxWiki {version}\n\n" + (
        f"Install from PyPI: `pipx install dtxwiki=={python_version}`. "
        "The attached wheel and sdist are the files published to "
        f"https://pypi.org/project/dtxwiki/{python_version}/.\n\n"
        f"The launcher `@dtxsuite/dtxwiki` {version} is on "
        f"https://www.npmjs.com/package/@dtxsuite/dtxwiki/v/{version}.\n\n"
        f"Full changelog: https://github.com/{REPOSITORY}/blob/v{version}/CHANGELOG.md\n"
    )
    if section:
        notes += f"\n## Changelog\n\n{section}\n"
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", help="preview a Git-based draft")
    generate_parser.add_argument(
        "--since", required=True, help="previous release tag or commit"
    )
    generate_parser.add_argument("--until", default="HEAD")
    generate_parser.add_argument(
        "--write", action="store_true", help="replace only Unreleased"
    )
    notes_parser = commands.add_parser(
        "notes", help="read a confirmed version for GitHub Release"
    )
    notes_parser.add_argument("--version", required=True)
    args = parser.parse_args()
    path = ROOT / "CHANGELOG.md"
    try:
        if args.command == "notes":
            section = version_section(path.read_text(encoding="utf-8"), args.version)
            if not section:
                print(
                    f"changelog: warning: CHANGELOG.md has no single non-empty "
                    f"'## {args.version}' entry; the notes link to CHANGELOG.md only",
                    file=sys.stderr,
                )
            print(release_notes(args.version, section), end="")
        else:
            generated = generate(args.since, args.until, ROOT)
            if args.write:
                path.write_text(
                    update_unreleased(path.read_text(encoding="utf-8"), generated),
                    encoding="utf-8",
                )
            else:
                print(generated, end="")
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip() or str(error)
        print(f"changelog: {' '.join(error.cmd)}: {detail}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"changelog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
