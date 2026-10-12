"""Pin the npm launcher package ``@dtxsuite/dtxwiki`` and the job that publishes it.

The package carries no copy of dtxWiki: ``npm/dtxwiki/bin/dtxwiki.js`` runs
``dtxwiki==<its own version>`` from PyPI through ``uvx`` or ``pipx run``. The
tests run the real launcher under node against stand-in runners on an
otherwise empty PATH, so they check the argv, the environment, the exit status
and the signals a user's runner would see, without a network.

release.yml publishes the tarball scripts/build_npm_package.py builds and
checks, only after PyPI took the same version, with no build of its own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "npm" / "dtxwiki" / "bin" / "dtxwiki.js"
BUILDER = ROOT / "scripts" / "build_npm_package.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
NODE = shutil.which("node")
NPM = shutil.which("npm")

requires_node = pytest.mark.skipif(
    NODE is None or os.name == "nt",
    reason="runs the launcher under node with POSIX stand-in runners",
)


def _builder():
    spec = importlib.util.spec_from_file_location("_npm_builder_under_test", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _version() -> str:
    manifest = ROOT / "npm" / "dtxwiki" / "package.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


# ---------------------------------------------------------------------------
# The manifest and the tarball.
# ---------------------------------------------------------------------------


def test_launcher_manifest_agrees_with_pyproject():
    assert _builder().manifest_problems() == []


@pytest.mark.counterexample(
    "manifest_problems reports a launcher left on the previous version, a "
    "different license, a repository npm provenance would reject, a dependency "
    "and a private flag"
)
def test_a_drifted_launcher_manifest_is_reported(tmp_path):
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    manifest = json.loads(
        (ROOT / "npm" / "dtxwiki" / "package.json").read_text(encoding="utf-8")
    )
    manifest.update(
        version=_version() + ".dev0",
        license="MIT",
        repository={"type": "git", "url": "git+https://github.com/someone/fork.git"},
        dependencies={"left-pad": "1.3.0"},
        private=True,
    )
    target = tmp_path / "npm" / "dtxwiki" / "package.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    problems = "\n".join(_builder().manifest_problems(tmp_path))

    for needle in ("version", "license", "repository.url", "dependencies", "private"):
        assert needle in problems, problems


@pytest.mark.skipif(NPM is None, reason="npm pack builds the tarball")
def test_the_tarball_holds_the_launcher_and_the_license_texts(tmp_path, capsys):
    builder = _builder()
    out = tmp_path / "dist-npm"

    assert builder.main([str(out)]) == 0
    tarball = out / builder.tarball_name(_version())
    assert capsys.readouterr().out.strip() == str(tarball)
    assert [path.name for path in out.iterdir()] == [tarball.name]
    with tarfile.open(tarball) as archive:
        packed = {member.name: archive.extractfile(member).read() for member in archive}
    for name in builder.LICENSE_FILES:
        assert packed[f"package/{name}"] == (ROOT / name).read_bytes(), name
    assert packed["package/bin/dtxwiki.js"] == LAUNCHER.read_bytes()


def test_the_builder_refuses_an_output_directory_with_leftovers(tmp_path):
    (tmp_path / "dtxsuite-dtxwiki-0.0.0.tgz").write_bytes(b"stale")

    with pytest.raises(SystemExit) as raised:
        _builder().main([str(tmp_path)])

    assert raised.value.code == 2


# ---------------------------------------------------------------------------
# The launcher under node.
# ---------------------------------------------------------------------------

RUNNER = """#!{python}
import json, os, pathlib, signal, sys, time

record = {{
    "runner": pathlib.Path(sys.argv[0]).name,
    "argv": sys.argv[1:],
    "launcher": os.environ.get("DTXWIKI_LAUNCHER"),
}}
log = pathlib.Path(os.environ["RUNNER_LOG"])
mode = os.environ.get("RUNNER_MODE", "exit")
if mode == "exit":
    log.write_text(json.dumps(record))
    sys.exit(int(os.environ.get("RUNNER_EXIT", "0")))
if mode == "die":
    log.write_text(json.dumps(record))
    os.kill(os.getpid(), signal.SIGTERM)
received = []
def note(signum, frame):
    received.append(signal.Signals(signum).name)
signal.signal(signal.SIGINT, note)
signal.signal(signal.SIGTERM, note)
pathlib.Path(os.environ["RUNNER_READY"]).write_text("ready")
deadline = time.monotonic() + 20
while not received and time.monotonic() < deadline:
    time.sleep(0.05)
time.sleep(0.5)  # a relayed duplicate of the signal would land here
record["signals"] = received
log.write_text(json.dumps(record))
"""


def _runners(tmp_path: Path, *names: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        runner = bin_dir / name
        runner.write_text(RUNNER.format(python=sys.executable), encoding="utf-8")
        runner.chmod(0o755)
    return bin_dir


def _env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    # PATH holds the stand-ins and nothing else, so a real uv or pipx on the
    # machine can never be the runner the test observes.
    return {
        "PATH": str(bin_dir),
        "RUNNER_LOG": str(tmp_path / "runner.json"),
        "RUNNER_READY": str(tmp_path / "ready"),
        **extra,
    }


def _launch(tmp_path, bin_dir, *args, **extra):
    return subprocess.run(
        [NODE, str(LAUNCHER), *args],
        env=_env(tmp_path, bin_dir, **extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "runner.json").read_text(encoding="utf-8"))


@requires_node
def test_uvx_runs_the_pypi_release_of_the_same_version(tmp_path):
    bin_dir = _runners(tmp_path, "uvx", "pipx")

    result = _launch(
        tmp_path, bin_dir, "serve", "--port", "21999", "a b", RUNNER_EXIT="7"
    )

    assert result.returncode == 7, result.stderr
    assert _record(tmp_path) == {
        "runner": "uvx",
        "argv": [
            "--python",
            "3.12",
            "--from",
            f"dtxwiki=={_version()}",
            "dtxwiki",
            "serve",
            "--port",
            "21999",
            "a b",
        ],
        "launcher": "npm",
    }


@requires_node
def test_pipx_runs_it_when_uv_is_missing(tmp_path):
    bin_dir = _runners(tmp_path, "pipx")

    result = _launch(tmp_path, bin_dir, "--version")

    assert result.returncode == 0, result.stderr
    assert _record(tmp_path) == {
        "runner": "pipx",
        "argv": ["run", "--spec", f"dtxwiki=={_version()}", "dtxwiki", "--version"],
        "launcher": "npm",
    }


@requires_node
def test_no_runner_says_what_to_install(tmp_path):
    bin_dir = _runners(tmp_path)

    result = _launch(tmp_path, bin_dir, "--version")

    assert result.returncode == 1
    assert "neither is on PATH" in result.stderr
    assert "https://docs.astral.sh/uv/" in result.stderr
    assert f"pipx install dtxwiki=={_version()}" in result.stderr


@requires_node
def test_a_runner_killed_by_a_signal_kills_the_launcher_alike(tmp_path):
    bin_dir = _runners(tmp_path, "uvx")

    result = _launch(tmp_path, bin_dir, RUNNER_MODE="die")

    assert result.returncode == -signal.SIGTERM, result.stderr


def _start_waiting(tmp_path: Path) -> subprocess.Popen:
    bin_dir = _runners(tmp_path, "uvx")
    launcher = subprocess.Popen(
        [NODE, str(LAUNCHER)],
        env=_env(tmp_path, bin_dir, RUNNER_MODE="wait"),
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while not (tmp_path / "ready").exists():
        assert launcher.poll() is None, "the launcher exited before its runner"
        assert time.monotonic() < deadline, "the runner never started"
        time.sleep(0.05)
    return launcher


@requires_node
def test_ctrl_c_reaches_the_runner_once(tmp_path):
    # A terminal signals the whole foreground process group; a launcher that
    # relayed SIGINT as well would make dtxWiki see a second Ctrl+C.
    launcher = _start_waiting(tmp_path)

    os.killpg(launcher.pid, signal.SIGINT)

    assert launcher.wait(timeout=30) == 0
    assert _record(tmp_path)["signals"] == ["SIGINT"]


@requires_node
def test_sigterm_to_the_launcher_reaches_the_runner(tmp_path):
    launcher = _start_waiting(tmp_path)

    launcher.send_signal(signal.SIGTERM)

    assert launcher.wait(timeout=30) == 0
    assert _record(tmp_path)["signals"] == ["SIGTERM"]


# ---------------------------------------------------------------------------
# release.yml
# ---------------------------------------------------------------------------

NPM_TOKEN = "${{ secrets.NPM_TOKEN }}"


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(action + "@")


def npm_publish_problems(workflow: dict) -> list[str]:
    jobs = workflow.get("jobs") or {}
    build = jobs.get("build", {}).get("steps", [])
    builders = [
        index
        for index, step in enumerate(build)
        if "scripts/build_npm_package.py" in step.get("run", "")
    ]
    if len(builders) != 1:
        return [f"expected one npm package build step, found {len(builders)}"]
    out = build[builders[0]]["run"].split("scripts/build_npm_package.py", 1)[1]
    out = out.strip().rstrip("/")
    uploaded = [
        (index, step["with"]["name"])
        for index, step in enumerate(build)
        if _uses(step, "actions/upload-artifact")
        and step["with"]["path"].rstrip("/") == out
    ]
    if len(uploaded) != 1 or uploaded[0][0] < builders[0]:
        return [f"the build job must upload {out} once, after building it"]
    artifact = uploaded[0][1]

    job = jobs.get("publish-npm")
    if job is None:
        return ["no publish-npm job"]
    problems = []
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    if needs != ["publish-pypi"]:
        problems.append(f"publish-npm must wait for publish-pypi only, needs {needs}")
    if job.get("permissions") != {"id-token": "write"}:
        problems.append(f"publish-npm permissions {job.get('permissions')!r}")
    if job.get("environment") != "npm":
        problems.append(f"publish-npm environment {job.get('environment')!r}")

    downloads = [
        step.get("with", {})
        for step in job["steps"]
        if _uses(step, "actions/download-artifact")
    ]
    if [download.get("name") for download in downloads] != [artifact]:
        problems.append(f"publish-npm must download only the {artifact} artifact")
    publishes = [step for step in job["steps"] if "npm publish" in step.get("run", "")]
    for step in job["steps"]:
        command = step.get("run")
        # Anything else that runs here would be a second build, routing around
        # the checks the build job ran on the tarball.
        if command is not None and not command.startswith(
            ("npm install --global npm@", "npm publish ")
        ):
            problems.append(f"publish-npm runs {command!r}")
        if command is None and not any(
            _uses(step, action)
            for action in ("actions/download-artifact", "actions/setup-node")
        ):
            problems.append(f"publish-npm uses {step.get('uses')!r}")
    if len(publishes) != 1:
        problems.append(f"expected one npm publish step, found {len(publishes)}")
    else:
        command = publishes[0]["run"]
        path = downloads[0].get("path", "").rstrip("/") if downloads else ""
        if f'"./{path}/' not in command:
            problems.append("npm publish must publish the downloaded tarball")
        for flag in ("--access public", "--provenance"):
            if flag not in command:
                problems.append(f"npm publish lacks {flag}")
        if publishes[0].get("env") != {"NODE_AUTH_TOKEN": NPM_TOKEN}:
            problems.append("only the publish step may see NPM_TOKEN")

    releasing = [
        job
        for job in jobs.values()
        if any("gh release create" in step.get("run", "") for step in job["steps"])
    ]
    for release in releasing:
        release_needs = release.get("needs") or []
        if "publish-npm" not in release_needs:
            problems.append("the GitHub Release announces npm before npm has it")
    return problems


def test_the_npm_launcher_is_published_from_the_checked_tarball_after_pypi():
    assert npm_publish_problems(yaml.safe_load(RELEASE.read_text("utf-8"))) == []


def test_npm_publish_rejects_a_path_that_can_be_parsed_as_github_shorthand():
    workflow = yaml.safe_load(RELEASE.read_text("utf-8"))
    for step in workflow["jobs"]["publish-npm"]["steps"]:
        if "npm publish" in step.get("run", ""):
            step["run"] = step["run"].replace('"./dist-npm/', '"dist-npm/')
    assert "npm publish must publish the downloaded tarball" in npm_publish_problems(
        workflow
    )


def test_npm_token_is_read_only_by_the_publish_step():
    assert RELEASE.read_text(encoding="utf-8").count("secrets.NPM_TOKEN") == 1


@pytest.mark.counterexample(
    "npm_publish_problems reports a publish job that does not wait for PyPI, "
    "can write the repository, packs its own tarball and drops provenance"
)
def test_npm_publish_regressions_are_reported():
    workflow = {
        "jobs": {
            "build": {
                "steps": [
                    {"run": ".venv/bin/python scripts/build_npm_package.py dist-npm"},
                    {
                        "uses": "actions/upload-artifact@v4",
                        "with": {"name": "npm-package", "path": "dist-npm/"},
                    },
                ]
            },
            "publish-npm": {
                "needs": "build",
                "permissions": {"id-token": "write", "contents": "write"},
                "environment": "npm",
                "steps": [
                    {"uses": "actions/checkout@v4"},
                    {"run": "cd npm/dtxwiki && npm pack"},
                    {
                        "run": "npm publish dtxsuite-dtxwiki-0.1.1.tgz --access public",
                        "env": {"NODE_AUTH_TOKEN": NPM_TOKEN},
                    },
                ],
            },
            "github-release": {
                "needs": "publish-pypi",
                "steps": [{"run": 'gh release create "$GITHUB_REF_NAME" dist/*'}],
            },
        }
    }

    problems = "\n".join(npm_publish_problems(workflow))

    for needle in (
        "must wait for publish-pypi",
        "permissions",
        "download only",
        "runs 'cd npm/dtxwiki && npm pack'",
        "uses 'actions/checkout@v4'",
        "the downloaded tarball",
        "lacks --provenance",
        "announces npm before npm has it",
    ):
        assert needle in problems, problems
