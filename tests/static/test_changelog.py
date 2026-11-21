"""Exercise the changelog from real merge history to the GitHub Release notes."""

import os

import pytest

from scripts import changelog


@pytest.mark.counterexample(
    "an unreadable --since revision fails with git's reason, not a partial draft"
)
def test_changelog_keeps_written_notes_from_draft_to_release(
    tmp_path, monkeypatch, capsys
):
    # The git subprocesses, including the ones changelog itself runs, inherit
    # this environment. Inherited GIT_DIR/GIT_INDEX_FILE (exported to hooks)
    # would write into another repository, and global or system signing, hooks
    # and includes would fail or rewrite these commits.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # One history traversal covers squash titles, ordinary merge PRs, and a
    # release integration. Separate repositories would repeat the same mechanism.
    def git(*args):
        return changelog.git(tmp_path, *args)

    def commit(subject):
        git("commit", "--allow-empty", "-m", subject)
        return git("rev-parse", "HEAD")

    git("init", "-b", "dev")
    git("config", "user.name", "Changelog Test")
    git("config", "user.email", "changelog@example.invalid")
    baseline = commit("chore(release): prepare 1.0.0")
    commit("feat(graph)!: retain positions (#12)")
    git("checkout", "-b", "feature")
    commit("fix!: internal implementation")
    git("checkout", "dev")
    git(
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "Merge pull request #13 from example/feature\n\nfix(mcp): preserve retries",
    )
    git("checkout", "-b", "api")
    commit(
        "feat(api): remove the legacy endpoint\n\nBREAKING CHANGE: use the new endpoint"
    )
    git("checkout", "dev")
    git(
        "merge",
        "--no-ff",
        "api",
        "-m",
        "Merge pull request #15 from example/api\n\nUpdate the API",
    )
    commit("fix(migrations): restore upgrades- #14")
    git("checkout", "-b", "release/1.0.1")
    fallback = commit("docs: explain upgrade")
    commit("chore(release): prepare 1.0.1")
    git("checkout", "dev")
    git("merge", "--no-ff", "release/1.0.1", "-m", "Merge release/1.0.1 into dev")

    generated = changelog.generate(baseline, root=tmp_path)
    assert generated.startswith("## Unreleased\n\n### Features\n")
    assert "**Breaking:** **graph**: retain positions ([#12]" in generated
    assert "**Breaking:** **mcp**: preserve retries ([#13]" in generated
    assert "**Breaking:** Update the API ([#15]" in generated
    assert "- **migrations**: restore upgrades ([#14]" in generated
    assert f"/commit/{fallback}" in generated
    assert "internal implementation" not in generated
    assert "remove the legacy endpoint" not in generated
    assert "Merge " not in generated
    assert "prepare 1.0.1" not in generated

    # Regeneration keeps what PR titles cannot say: Important changes and the
    # indented notes under an entry that is still generated, in place.
    historical = "## 1.0.0 — 2026-01-01\n\nKeep released text verbatim.\n"
    important = "### Important changes\n\n- Refresh `tools/list`.\n  - Detail.\n"
    pr12 = next(line for line in generated.splitlines() if "[#12]" in line)
    note = "  - Run `dtxwiki backup` first.\n"
    original = (
        "# Changelog\n\n## Unreleased\n\n### Features\n\n"
        + pr12
        + "\n"
        + note
        + "- Hand-written line.\n  - Dropped with its line.\n\n"
        + important
        + "\n"
        + historical
    )
    updated = changelog.update_unreleased(original, generated)
    expected = generated.replace(
        "## Unreleased\n", "## Unreleased\n\n" + important, 1
    ).replace(pr12 + "\n", pr12 + "\n" + note, 1)
    assert updated == "# Changelog\n\n" + expected + "\n" + historical
    assert changelog.update_unreleased(updated, generated) == updated

    # A tag reads its confirmed entry, notes included; without one the release
    # still gets the install text and the CHANGELOG link.
    confirmed = updated.replace("## Unreleased", "## 1.0.1 — 2026-01-02", 1)
    section = changelog.version_section(confirmed, "1.0.1")
    assert section.startswith("### Important changes") and note.strip() in section
    assert "Keep released text" not in section
    assert changelog.release_notes("1.0.1", section).endswith(
        "\n## Changelog\n\n" + section + "\n"
    )
    assert changelog.version_section(confirmed, "1.0.2") == ""
    assert "## Changelog" not in changelog.release_notes("1.0.2", "")
    rc_notes = changelog.release_notes("1.0.1-rc.1", section)
    assert "https://pypi.org/project/dtxwiki/1.0.1rc1/" in rc_notes
    assert "https://www.npmjs.com/package/@dtxsuite/dtxwiki/v/1.0.1-rc.1" in rc_notes

    # Git's own reason reaches the caller when a revision cannot be read.
    monkeypatch.setattr(changelog, "ROOT", tmp_path)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setattr("sys.argv", ["changelog", "generate", "--since", "v9.9.9"])
    assert changelog.main() == 1
    assert "fatal: Needed a single revision" in capsys.readouterr().err
