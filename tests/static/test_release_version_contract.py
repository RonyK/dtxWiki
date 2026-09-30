"""Pin the release version, the toolchain pins and the release pipeline order.

The version lives in pyproject.toml. Everything else that ships it -- the
desktop workspace, both lock files, the exported OpenAPI document, the two
agent-plugin manifests and the npm launcher package -- must carry the same
string. Nothing in this module
spells out what that string is, so a version bump never edits a test.

release.yml is pinned on three points the artifact gate in
test_release_workflow_contract.py does not cover: the tag must equal
``v`` + that version before anything is built (PyPI never takes a version
twice), the GitHub Release is created only after PyPI accepted the upload and
is the only job that can write, and no tag starts the desktop installer
workflow, because installers are not part of this release line.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
DESKTOP_RELEASE = ROOT / ".github" / "workflows" / "release-desktop.yml"

OPENAPI = "shared/schemas/rest/v1/openapi.json"
CLAUDE_PLUGIN = (
    "integrations/claude-plugin/plugins/dtxwiki/.claude-plugin/plugin.json"
)
CODEX_PLUGIN = "integrations/codex-plugin/plugins/dtxwiki/.codex-plugin/plugin.json"
NPM_LAUNCHER = "npm/dtxwiki/package.json"


def _json(root: Path, relative: str) -> dict:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _uv_lock_version(root: Path) -> str:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    return next(p["version"] for p in lock["package"] if p["name"] == "dtxwiki")


# Every file that ships the release version, and how to read it there.
VERSION_SITES = {
    "desktop/package.json": lambda root: _json(root, "desktop/package.json")[
        "version"
    ],
    "package-lock.json (desktop workspace)": lambda root: _json(
        root, "package-lock.json"
    )["packages"]["desktop"]["version"],
    "uv.lock (dtxwiki)": _uv_lock_version,
    OPENAPI: lambda root: _json(root, OPENAPI)["info"]["version"],
    "claude plugin manifest": lambda root: _json(root, CLAUDE_PLUGIN)["version"],
    "codex plugin manifest": lambda root: _json(root, CODEX_PLUGIN)["version"],
    # The launcher runs dtxwiki==<this version> from PyPI.
    "npm launcher package": lambda root: _json(root, NPM_LAUNCHER)["version"],
}


def project(root: Path = ROOT) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def version_mismatches(root: Path = ROOT) -> dict[str, str]:
    """{site: version found there} for every site that disagrees with pyproject."""

    expected = project(root)["version"]
    found = {site: read(root) for site, read in VERSION_SITES.items()}
    return {site: value for site, value in found.items() if value != expected}


def toolchain_pin_problems(root: Path = ROOT) -> list[str]:
    """.python-version and .nvmrc must match the floors the manifests declare."""

    problems = []
    python_pin = (root / ".python-version").read_text(encoding="utf-8").strip()
    metadata = project(root)
    floor = re.fullmatch(r">=\s*(\d+\.\d+)", metadata["requires-python"].strip())
    if floor is None or floor.group(1) != python_pin:
        problems.append(
            f".python-version {python_pin!r} is not the requires-python floor "
            f"{metadata['requires-python']!r}"
        )
    classifier = f"Programming Language :: Python :: {python_pin}"
    if classifier not in metadata.get("classifiers", []):
        problems.append(f"no `{classifier}` classifier for the pinned interpreter")

    node_pin = (root / ".nvmrc").read_text(encoding="utf-8").strip()
    engines = json.loads((root / "package.json").read_text(encoding="utf-8")).get(
        "engines", {}
    )
    if engines.get("node") != f">={node_pin}":
        problems.append(
            f"package.json engines.node {engines.get('node')!r} is not "
            f"'>={node_pin}' from .nvmrc"
        )
    return problems


def _steps_matching(steps: list[dict], needle: str) -> list[int]:
    return [index for index, step in enumerate(steps) if needle in step.get("run", "")]


def tag_gate_position_problems(build_steps: list[dict]) -> list[str]:
    gates = [
        index
        for index, step in enumerate(build_steps)
        if "GITHUB_REF_NAME" in step.get("run", "")
        and "pyproject.toml" in step.get("run", "")
    ]
    if len(gates) != 1:
        return [f"expected one tag/version gate step, found {len(gates)}"]
    builders = _steps_matching(build_steps, "make bundle-frontend") + (
        _steps_matching(build_steps, "-m build")
    )
    if not builders:
        return ["the build job no longer builds anything; re-read this module"]
    if gates[0] > min(builders):
        return ["the tag/version gate runs after the build has started"]
    return []


def github_release_problems(workflow: dict) -> list[str]:
    jobs = workflow.get("jobs") or {}
    releasing = [
        job_id
        for job_id, job in jobs.items()
        if any("gh release create" in step.get("run", "") for step in job["steps"])
    ]
    if len(releasing) != 1:
        return [f"expected one job creating the GitHub Release, found {releasing}"]
    job_id = releasing[0]
    job = jobs[job_id]
    problems = []

    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    if "publish-pypi" not in needs:
        problems.append(f"{job_id} must wait for publish-pypi, needs {needs}")
    if job.get("permissions") != {"contents": "write"}:
        problems.append(f"{job_id} permissions {job.get('permissions')!r}")
    if (workflow.get("permissions") or {}).get("contents") not in (None, "read"):
        problems.append("the workflow default grants more than contents: read")
    if workflow.get("permissions") is None:
        problems.append("set workflow permissions so only the release job writes")
    for other_id, other in jobs.items():
        if other_id != job_id and (other.get("permissions") or {}).get(
            "contents"
        ) == "write":
            problems.append(f"{other_id} can write repository contents too")

    command = next(
        step["run"]
        for step in job["steps"]
        if "gh release create" in step.get("run", "")
    )
    if "--verify-tag" not in command:
        problems.append("`gh release create` must --verify-tag, not create a tag")
    if "dist/*" not in command:
        problems.append("the release must attach the wheel and sdist from dist/")
    downloads = [
        step.get("with", {}).get("name")
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    ]
    uploads = [
        step.get("with", {}).get("name")
        for build in jobs.values()
        for step in build["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    if not downloads or downloads[0] not in uploads:
        problems.append("the release must attach the artifact the build uploaded")
    return problems


def tag_triggers(workflow: dict) -> list[str]:
    """Every trigger that would start the workflow without a human dispatching it."""

    value = workflow["on"] if "on" in workflow else workflow.get(True)
    if isinstance(value, str):
        value = {value: None}
    elif isinstance(value, list):
        value = dict.fromkeys(value)
    return sorted(set(value or {}) - {"workflow_dispatch"})


def _release() -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The tree.
# ---------------------------------------------------------------------------


def test_every_shipped_manifest_carries_the_pyproject_version():
    assert version_mismatches() == {}


def test_project_urls_point_at_the_public_project():
    urls = project()["urls"]

    assert set(urls) == {"Homepage", "Repository", "Issues", "Changelog"}
    assert all(url.startswith("https://") for url in urls.values()), urls
    repository = urls["Repository"].rstrip("/")
    assert urls["Issues"].startswith(repository + "/")
    assert urls["Changelog"].startswith(repository + "/")


def test_toolchain_pins_match_the_declared_floors():
    assert toolchain_pin_problems() == []


def test_release_checks_the_tag_before_anything_is_built():
    assert tag_gate_position_problems(_release()["jobs"]["build"]["steps"]) == []


def _gate_script() -> str:
    steps = _release()["jobs"]["build"]["steps"]
    (index,) = [
        index
        for index, step in enumerate(steps)
        if "GITHUB_REF_NAME" in step.get("run", "")
        and "pyproject.toml" in step.get("run", "")
    ]
    return steps[index]["run"]


def _run_gate(tmp_path: Path, *, pyproject_version: str, tag: str):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "dtxwiki"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    if not interpreter.exists():
        interpreter.symlink_to(sys.executable)
    return subprocess.run(
        ["bash", "-e", "-c", _gate_script()],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": tag},
        capture_output=True,
        text=True,
        check=False,
    )


requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="the gate is a bash step on the Linux runner",
)


@requires_bash
def test_release_gate_accepts_the_tag_of_the_package_version(tmp_path):
    result = _run_gate(tmp_path, pyproject_version="7.8.9", tag="v7.8.9")

    assert result.returncode == 0, result.stdout + result.stderr


@requires_bash
@pytest.mark.parametrize("tag", ["v7.8.10", "7.8.9", "v7.8.9rc1", "v7.8"])
@pytest.mark.counterexample(
    "the release.yml gate step itself, run under bash, exits non-zero when the "
    "pushed tag is not exactly v + the pyproject version"
)
def test_release_gate_rejects_a_tag_that_is_not_the_package_version(tmp_path, tag):
    result = _run_gate(tmp_path, pyproject_version="7.8.9", tag=tag)

    assert result.returncode != 0
    assert f"tag {tag} does not match pyproject.toml version 7.8.9" in (
        result.stdout + result.stderr
    )


def test_github_release_follows_pypi_and_is_the_only_writer():
    assert github_release_problems(_release()) == []


def test_desktop_installers_are_not_released_by_a_tag():
    workflow = yaml.safe_load(DESKTOP_RELEASE.read_text(encoding="utf-8"))

    assert tag_triggers(workflow) == []


# ---------------------------------------------------------------------------
# Counterexamples for the pure checking functions.
# ---------------------------------------------------------------------------


@pytest.mark.counterexample(
    "version_mismatches reports a plugin manifest and a lock entry left on the "
    "previous version"
)
def test_a_stale_version_site_is_reported(tmp_path):
    for relative in [
        "pyproject.toml",
        "desktop/package.json",
        "package-lock.json",
        "uv.lock",
        OPENAPI,
        CLAUDE_PLUGIN,
        CODEX_PLUGIN,
        NPM_LAUNCHER,
    ]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    current = project()["version"]
    stale = current + ".dev0"
    manifest = tmp_path / CODEX_PLUGIN
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = stale
    manifest.write_text(json.dumps(data), encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            f'name = "dtxwiki"\nversion = "{current}"',
            f'name = "dtxwiki"\nversion = "{stale}"',
        ),
        encoding="utf-8",
    )

    assert version_mismatches(tmp_path) == {
        "codex plugin manifest": stale,
        "uv.lock (dtxwiki)": stale,
    }


@pytest.mark.counterexample(
    "toolchain_pin_problems reports a Python pin above the requires-python floor "
    "and an engines floor that is not .nvmrc"
)
def test_toolchain_pin_drift_is_reported(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1"\nrequires-python = ">=3.12"\n'
        'classifiers = ["Programming Language :: Python :: 3.12"]\n',
        encoding="utf-8",
    )
    (tmp_path / ".python-version").write_text("3.14\n", encoding="utf-8")
    (tmp_path / ".nvmrc").write_text("22.18\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"engines": {"node": ">=20"}}', encoding="utf-8"
    )

    problems = toolchain_pin_problems(tmp_path)

    assert any("requires-python floor" in problem for problem in problems), problems
    assert any("Python :: 3.14" in problem for problem in problems), problems
    assert any("engines.node" in problem for problem in problems), problems


@pytest.mark.counterexample(
    "tag_gate_position_problems reports a gate that runs after `python -m build`"
)
def test_a_late_tag_gate_is_reported():
    steps = [
        {"run": "uv sync --locked --extra dev"},
        {"run": ".venv/bin/python -m build"},
        {"run": 'test "$GITHUB_REF_NAME" = "v$(read pyproject.toml)"'},
    ]

    assert tag_gate_position_problems(steps) == [
        "the tag/version gate runs after the build has started"
    ]


@pytest.mark.counterexample(
    "github_release_problems reports a release job that does not wait for PyPI, "
    "a second writer and a release that would create its own tag"
)
def test_github_release_regressions_are_reported():
    workflow = {
        "permissions": {"contents": "read"},
        "jobs": {
            "build": {
                "permissions": {"contents": "write"},
                "steps": [
                    {"uses": "actions/upload-artifact@v4", "with": {"name": "dist"}}
                ],
            },
            "publish-pypi": {"needs": "build", "steps": []},
            "github-release": {
                "needs": "build",
                "permissions": {"contents": "write"},
                "steps": [
                    {
                        "uses": "actions/download-artifact@v4",
                        "with": {"name": "dist"},
                    },
                    {"run": 'gh release create "$GITHUB_REF_NAME" dist/*'},
                ],
            },
        },
    }

    problems = github_release_problems(workflow)

    assert any("must wait for publish-pypi" in problem for problem in problems)
    assert any("build can write repository contents" in p for p in problems)
    assert any("--verify-tag" in problem for problem in problems), problems


@pytest.mark.counterexample(
    "tag_triggers reports the `push: tags` trigger the desktop workflow used to "
    "have, in both the `on` and the YAML-1.1 `True` spelling"
)
def test_a_tag_trigger_on_the_desktop_workflow_is_reported():
    parsed = yaml.safe_load(
        "on:\n  push:\n    tags:\n      - 'v*'\n  workflow_dispatch:\n"
    )

    assert tag_triggers(parsed) == ["push"]
    assert tag_triggers({"on": ["workflow_dispatch", "release"]}) == ["release"]
