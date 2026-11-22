"""Release version changes preserve dependencies, history, and unrelated fields."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import release_version

MANIFESTS = (
    "desktop/package.json",
    "npm/dtxwiki/package.json",
    "integrations/claude-plugin/plugins/dtxwiki/.claude-plugin/plugin.json",
    "integrations/codex-plugin/plugins/dtxwiki/.codex-plugin/plugin.json",
)
READMES = ("README.md", "README.ko.md", "npm/dtxwiki/README.md")
OPENAPI = "shared/schemas/rest/v1/openapi.json"
TOOL_CONTRACT = "backend/schemas/common.py"
PRIOR_DIGEST = "70000000000a"
CHANGED_DIGEST = "80000000000b"


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json(root: Path, relative: str) -> dict:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0] != ".git"
    }


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release test",
            "-c",
            "user.email=release-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "core.hooksPath=",
            "-c",
            "init.templateDir=",
            *args,
        ],
        cwd=root,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
        },
        check=True,
        capture_output=True,
        text=True,
    )


def _contract(revision: int, digest: str, *, current: int | None = None) -> str:
    return (
        f"MCP_RELEASED_TOOL_CONTRACT_REVISION = {revision}\n"
        f'MCP_RELEASED_TOOL_CONTRACT_DIGEST = "{digest}"\n'
        f"MCP_TOOL_CONTRACT_REVISION = {revision if current is None else current}\n"
    )


def _catalog(root: Path, digest: str) -> None:
    _write(
        root,
        "backend/mcp/protocol/tool_descriptors.py",
        f"def tool_contract_digest():\n    return {digest!r}\n",
    )


def _commit_release(
    root: Path, project: str, version: str, revision: int, digest: str
) -> None:
    _write(root, "pyproject.toml", project.replace("1.2.3", version, 1))
    _write(root, TOOL_CONTRACT, _contract(revision, digest))
    _git(root, "add", "pyproject.toml", TOOL_CONTRACT)
    _git(root, "commit", "--quiet", "-m", f"prepare {version}")


@pytest.fixture
def release_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "dtxwiki"\nversion = "1.2.3"\n'
        'dependencies = ["example==1.2.3"]\n'
        '\n[tool.example]\nversion = "1.2.3"\n',
    )
    for relative in MANIFESTS:
        _write(
            tmp_path,
            relative,
            json.dumps({"version": "1.2.3", "description": "Retain 1.2.3"}) + "\n",
        )
    _write(
        tmp_path,
        "uv.lock",
        'version = 1\n\n[[package]]\nname = "example"\nversion = "1.2.3"\n'
        'source = { registry = "https://example.invalid/simple" }\n'
        '\n[[package]]\nname = "dtxwiki"\nversion = "1.2.3"\n'
        'source = { editable = "." }\n'
        'dependencies = [{ name = "example" }]\n',
    )
    _write(
        tmp_path,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "desktop": {"version": "1.2.3", "name": "@dtxwiki/desktop"},
                    "node_modules/example": {
                        "version": "1.2.3",
                        "integrity": "sha512-unchanged",
                    },
                },
            }
        )
        + "\n",
    )
    _write(
        tmp_path,
        OPENAPI,
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "dtxWiki", "version": "1.2.3"},
                "paths": {"/health/live": {"get": {"summary": "Retain 1.2.3"}}},
            }
        )
        + "\n",
    )
    for output in READMES:
        text = (
            f"# {output}\n\n"
            "This guide is for **dtxWiki 1.2.3 (Beta)**.\n"
            "이 안내는 **dtxWiki 1.2.3 (Beta)** 기준입니다.\n"
            "Read [Upgrading to 1.2.3](#upgrading).\n"
            "[1.2.3로 업데이트하기](#upgrading)를 읽으세요.\n"
            "[1.2.3으로 업데이트하기](#upgrading)를 읽으세요.\n"
            "## Upgrading to 1.2.3\n"
            "## 1.2.3로 업데이트하기\n"
            "## 1.2.3으로 업데이트하기\n"
            "Install 1.2.3 using your existing installation method.\n"
            "기존 설치 방식을 사용해 1.2.3를 설치하되 새 서버를 시작하지 마세요.\n"
            "기존 설치 방식을 사용해 1.2.3을 설치하되 새 서버를 시작하지 마세요.\n"
            "On the server, download the 1.2.3 installer.\n"
            "서버에서 1.2.3 설치 프로그램과 wheel을 받으세요.\n"
            "Complete the 1.2.3 README's audit before starting.\n"
            "covers setup for 1.2.3. See the documentation.\n"
            'python -m pip install "dtxwiki==1.2.3"\n'
            "npx @dtxsuite/dtxwiki@1.2.3\n"
            "git clone --branch v1.2.3 https://github.com/dtxSuite/dtxWiki.git\n"
            "https://github.com/dtxSuite/dtxWiki/blob/v1.2.3/README.md\n"
            "https://raw.githubusercontent.com/dtxSuite/dtxWiki/v1.2.3/infra/prod/install.sh\n"
            "https://github.com/dtxSuite/dtxWiki/releases/download/v1.2.3/dtxwiki-1.2.3-py3-none-any.whl\n"
            "wheel=/tmp/dtxwiki-1.2.3-py3-none-any.whl\n"
            "\n## Extra guidance\n"
            "Handwritten explanation: schema 0.1.4; dependency example==1.2.3.\n"
            "https://raw.githubusercontent.com/dtxSuite/dtxWiki/dev/.github/assets/readme/preview.png\n"
            "https://docs.dtxwiki.com/docs/1.2/administration/backup-upgrades/\n"
            "https://github.com/example/dependency/blob/v1.2.3/README.md\n"
            '<a id="upgrade-013"></a>\n'
            "\n## Historical releases\n"
            'python -m pip install "dtxwiki==0.1.4"\n'
            "npx @dtxsuite/dtxwiki@0.1.4\n"
            "https://github.com/dtxSuite/dtxWiki/blob/v0.1.4/README.md\n"
            "wheel=/tmp/dtxwiki-0.1.4-py3-none-any.whl\n"
            "### Legacy installation\n"
            "npx @dtxsuite/dtxwiki@0.1.3\n"
            "### Other evidence\n"
            "npx @dtxsuite/dtxwiki@0.1.2\n"
            "\n## Current notes\n"
            "Historical example: `dtxwiki==0.1.3`.\n"
        )
        _write(tmp_path, output, text)
    for package in ("backend", "backend/mcp", "backend/mcp/protocol"):
        _write(tmp_path, f"{package}/__init__.py", "")
    _catalog(tmp_path, CHANGED_DIGEST)
    project = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    _git(tmp_path, "init", "--quiet")
    for version, revision, digest in (
        ("1.2.1", 6, "60000000000a"),
        ("1.2.2", 7, PRIOR_DIGEST),
    ):
        _commit_release(tmp_path, project, version, revision, digest)
        _git(tmp_path, "tag", f"v{version}")

    # A higher version from a release line that was not merged into this
    # checkout cannot supply its baseline.
    _git(tmp_path, "switch", "--quiet", "--detach", "v1.2.1")
    _commit_release(tmp_path, project, "2.4.5", 90, "99000000000a")
    _git(tmp_path, "tag", "v2.4.5")
    _git(tmp_path, "switch", "--quiet", "-")

    # Publication merges the prepared version from this checkout. The tag
    # itself is a merge commit on the publication branch.
    _commit_release(tmp_path, project, "2.3.5", 11, PRIOR_DIGEST)
    _git(tmp_path, "switch", "--quiet", "--detach", "v1.2.1")
    _git(tmp_path, "merge", "--quiet", "--no-ff", "-m", "publish 2.3.5", "@{-1}")
    _git(tmp_path, "tag", "v2.3.5")
    _git(tmp_path, "switch", "--quiet", "-")

    _write(tmp_path, "pyproject.toml", project)
    _write(tmp_path, TOOL_CONTRACT, _contract(7, PRIOR_DIGEST, current=21))
    _write(tmp_path, "CHANGELOG.md", "## 0.1.4\n\nReleased before 1.2.3.\n")
    _write(
        tmp_path,
        "backend/migrations/release_manifest.json",
        '{"schema_releases": {"0.1.4": {"head": "historical"}}}\n',
    )
    monkeypatch.setattr(
        release_version, "export_openapi", lambda root: _json(root, OPENAPI)
    )
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return tmp_path


def test_version_sync_updates_consumers_without_rewriting_history_or_dependencies(
    release_tree: Path,
) -> None:
    before = _snapshot(release_tree)
    original_schema = _json(release_tree, OPENAPI)

    release_version.synchronize(root=release_tree, version="2.4.6")

    assert release_version.source_version(release_tree) == "2.4.6"
    for relative in MANIFESTS:
        assert _json(release_tree, relative) == {
            "version": "2.4.6",
            "description": "Retain 1.2.3",
        }
    metadata = tomllib.loads((release_tree / "pyproject.toml").read_text())
    assert metadata["project"]["dependencies"] == ["example==1.2.3"]
    assert metadata["tool"]["example"]["version"] == "1.2.3"
    lock = tomllib.loads((release_tree / "uv.lock").read_text())
    assert [(p["name"], p["version"]) for p in lock["package"]] == [
        ("example", "1.2.3"),
        ("dtxwiki", "2.4.6"),
    ]
    npm = _json(release_tree, "package-lock.json")
    assert npm["packages"]["desktop"]["version"] == "2.4.6"
    assert npm["packages"]["node_modules/example"] == {
        "version": "1.2.3",
        "integrity": "sha512-unchanged",
    }
    expected_schema = original_schema | {
        "info": original_schema["info"] | {"version": "2.4.6"}
    }
    assert _json(release_tree, OPENAPI) == expected_schema
    for output in READMES:
        rendered = (release_tree / output).read_text()
        assert "dtxwiki==2.4.6" in rendered
        # All current-reference contexts above this heading should follow the
        # new version; the author's other prose and historical examples should
        # remain byte-for-byte intact, including the independent Docs series.
        original_current, preserved = (
            before[output].decode().split("\n## Extra guidance", 1)
        )
        assert rendered == (
            original_current.replace("1.2.3", "2.4.6")
            + "\n## Extra guidance"
            + preserved
        )
    assert (release_tree / "CHANGELOG.md").read_bytes() == before["CHANGELOG.md"]
    manifest = "backend/migrations/release_manifest.json"
    assert (release_tree / manifest).read_bytes() == before[manifest]
    contract = release_tree / TOOL_CONTRACT
    # v2.3.5 was prepared here and tagged after merging into the publication
    # branch. Changing its digest advances its revision once across versions.
    assert contract.read_text() == _contract(12, CHANGED_DIGEST)
    assert release_version.check(root=release_tree) == []

    # Record a pending release commit but no tag. Re-running set keeps using
    # the published baseline, including when another edit arrives before tag.
    _git(release_tree, "add", "pyproject.toml", TOOL_CONTRACT)
    _git(release_tree, "commit", "--quiet", "-m", "prepare release 2.4.6")
    assert release_version.synchronize(root=release_tree, version="2.4.6") == []

    later_digest = "90000000000c"
    _catalog(release_tree, later_digest)
    after = _snapshot(release_tree)
    assert release_version.synchronize(root=release_tree) == []
    assert _snapshot(release_tree) == after
    assert release_version.synchronize(root=release_tree, version="2.4.6") == [
        TOOL_CONTRACT
    ]
    assert contract.read_text() == _contract(12, later_digest)

    # Reverting to the previous release's catalog reuses its revision.
    _catalog(release_tree, PRIOR_DIGEST)
    assert release_version.synchronize(root=release_tree, version="2.4.6") == [
        TOOL_CONTRACT
    ]
    assert contract.read_text() == _contract(11, PRIOR_DIGEST)


def test_release_candidate_round_trip_uses_the_registry_version_spellings(
    release_tree: Path,
) -> None:
    readme = release_tree / "README.md"
    history = "\n## v1.2.2-rc.1\n\nInstall with `pipx install dtxwiki==1.2.2rc1`.\n"
    original_readme = readme.read_text(encoding="utf-8") + history
    readme.write_text(original_readme, encoding="utf-8")

    release_version.synchronize(root=release_tree, version="1.2.4-rc.1")

    assert release_version.source_version(release_tree) == "1.2.4-rc.1"
    assert _json(release_tree, "npm/dtxwiki/package.json")["version"] == "1.2.4-rc.1"
    assert (
        _json(release_tree, "package-lock.json")["packages"]["desktop"]["version"]
        == "1.2.4-rc.1"
    )
    assert _json(release_tree, OPENAPI)["info"]["version"] == "1.2.4-rc.1"
    lock = tomllib.loads((release_tree / "uv.lock").read_text(encoding="utf-8"))
    assert lock["package"][1]["version"] == "1.2.4rc1"
    rendered = readme.read_text(encoding="utf-8")
    assert 'pip install "dtxwiki==1.2.4rc1"' in rendered
    assert "npx @dtxsuite/dtxwiki@1.2.4-rc.1" in rendered
    assert "/download/v1.2.4-rc.1/dtxwiki-1.2.4rc1-py3-none-any.whl" in rendered
    assert rendered.endswith(history)
    assert release_version.check(root=release_tree) == []
    assert release_version.synchronize(root=release_tree) == []

    # Detect and repair a package spelling accidentally used in a Git tag.
    readme.write_text(rendered.replace("v1.2.4-rc.1", "v1.2.4rc1", 1), encoding="utf-8")
    assert any(
        "expected release version 1.2.4-rc.1, found 1.2.4rc1" in problem
        for problem in release_version.check(release_tree)
    )
    assert release_version.synchronize(root=release_tree) == ["README.md"]

    release_version.synchronize(root=release_tree, version="1.2.3")

    assert readme.read_text(encoding="utf-8") == original_readme
    assert release_version.check(root=release_tree) == []


@pytest.mark.counterexample("mixed README versions and invalid input cannot pass")
def test_version_check_reports_drift_and_failed_preflight_keeps_every_file(
    release_tree: Path,
) -> None:
    stale = release_tree / "desktop/package.json"
    stale.write_text('{"version": "0.1.4"}\n', encoding="utf-8")
    project = release_tree / "pyproject.toml"
    project.write_text(
        project.read_text().replace('version = "1.2.3"', 'version = "3.5.7"', 1)
    )
    readme = release_tree / "README.md"
    # Simulate a direct canonical-version edit followed by only one corrected
    # README command. The other app references must be found independently of
    # the source's old version, without rejecting this new prose or history.
    readme.write_text(
        readme.read_text().replace("dtxwiki==1.2.3", "dtxwiki==3.5.7", 1)
        + "\nAdditional handwritten guidance can be edited here directly.\n"
    )
    before = _snapshot(release_tree)

    problems = release_version.check(release_tree)
    assert any("desktop/package.json" in p for p in problems)
    assert any("README.md:" in p and "found 1.2.3" in p for p in problems)
    assert not any("found 0.1.4" in p or "found 0.1.3" in p for p in problems)
    assert _snapshot(release_tree) == before
    with pytest.raises(ValueError):
        release_version.synchronize(root=release_tree, version="02.4.6")
    assert _snapshot(release_tree) == before

    _git(release_tree, "tag", "--delete", "v2.3.5", "v1.2.2", "v1.2.1")
    with pytest.raises(ValueError, match="tag"):
        release_version.synchronize(root=release_tree, version="2.4.6")
    assert _snapshot(release_tree) == before
    _git(release_tree, "tag", "v1.2.2", "HEAD~1")

    (release_tree / "README.ko.md").unlink()
    before_missing = _snapshot(release_tree)
    with pytest.raises(FileNotFoundError, match="README.ko.md"):
        release_version.check(release_tree)
    with pytest.raises((ValueError, FileNotFoundError)):
        release_version.synchronize(root=release_tree, version="2.4.6")
    assert _snapshot(release_tree) == before_missing
