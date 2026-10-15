"""Pin the artifact gate in front of PyPI.

release.yml fires on any ``v*`` tag while ci.yml runs only on pull requests and
pushes to main, so a tag can reach `pypa/gh-action-pypi-publish` on a commit no
other workflow ever built. These tests keep the gate from being narrowed back
to the three `test -f` calls it used to be.
"""

import shlex
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _build_steps():
    return _workflow()["jobs"]["build"]["steps"]


def _upload_index(steps):
    for index, step in enumerate(steps):
        if str(step.get("uses", "")).startswith("actions/upload-artifact"):
            return index
    raise AssertionError("the build job never uploads the dist artifact")


def _verify_index(steps):
    verify = [
        index
        for index, step in enumerate(steps)
        if "scripts/verify_wheel_artifact.py" in step.get("run", "")
    ]
    assert len(verify) == 1, "the build job must verify the artifacts exactly once"
    return verify[0]


def _assert_strict_artifact_gate(workflow):
    build = workflow["jobs"]["build"]
    assert "if" not in build, "The release build must not be conditional."
    assert not build.get("continue-on-error", False)
    steps = build["steps"]
    index = _verify_index(steps)
    assert index < _upload_index(steps)
    step = steps[index]
    assert "if" not in step, "The release artifact gate must not be conditional."
    assert not step.get("continue-on-error", False)
    arguments = shlex.split(step["run"])
    assert len(arguments) == 5, (
        "Run the strict verifier directly, without shell bypasses."
    )
    assert arguments[:2] == [
        ".venv/bin/python",
        "scripts/verify_wheel_artifact.py",
    ]
    uploaded = steps[_upload_index(steps)]["with"]["path"]
    assert arguments[2].rstrip("/") == uploaded.rstrip("/"), (
        f"verify is handed {arguments[2]!r} but upload-artifact publishes {uploaded!r}"
    )
    assert arguments[3:] == ["--release-version", "${GITHUB_REF_NAME#v}"], (
        "The artifact metadata and effective schema head must match the pushed tag."
    )


def test_release_build_verifies_the_artifacts_before_uploading_them():
    steps = _build_steps()

    assert _verify_index(steps) < _upload_index(steps)


def test_release_verifies_everything_it_uploads_not_one_glob_out_of_it():
    """`upload-artifact` uploads a directory; publish-pypi ships all of it.

    `python -m build` writes both dist/*.whl and dist/*.tar.gz, so a verify
    step handed `$(ls dist/dtxwiki-*.whl)` published the sdist unverified. The
    only argument that keeps the verified set equal to the published set is the
    uploaded path itself.
    """

    _assert_strict_artifact_gate(_workflow())


@pytest.mark.counterexample(
    "a skipped, nonblocking or unbound release artifact verification is rejected"
)
@pytest.mark.parametrize(
    "violation",
    [
        "missing_gate",
        "missing_option",
        "fixed_version",
        "other_dist",
        "wheel_glob",
        "conditional",
        "nonblocking",
        "build_conditional",
        "build_nonblocking",
        "shell_success",
        "commented_gate",
        "after_upload",
    ],
)
def test_release_guard_rejects_bypassed_strict_artifact_verification(violation):
    workflow = deepcopy(_workflow())
    build = workflow["jobs"]["build"]
    steps = build["steps"]
    step = steps[_verify_index(steps)]
    if violation == "missing_gate":
        steps.remove(step)
    elif violation == "missing_option":
        step["run"] = ".venv/bin/python scripts/verify_wheel_artifact.py dist"
    elif violation == "fixed_version":
        step["run"] = step["run"].replace("${GITHUB_REF_NAME#v}", "0.1.3")
    elif violation == "other_dist":
        step["run"] = step["run"].replace(" dist ", " previous-dist ")
    elif violation == "wheel_glob":
        step["run"] = step["run"].replace(" dist ", " dist/*.whl ")
    elif violation == "conditional":
        step["if"] = "github.event_name == 'workflow_dispatch'"
    elif violation == "nonblocking":
        step["continue-on-error"] = True
    elif violation == "build_conditional":
        build["if"] = "github.event_name == 'workflow_dispatch'"
    elif violation == "build_nonblocking":
        build["continue-on-error"] = True
    elif violation == "shell_success":
        step["run"] += " || true"
    elif violation == "commented_gate":
        step["run"] = "# " + step["run"]
    else:
        steps.remove(step)
        steps.append(step)
    with pytest.raises(AssertionError):
        _assert_strict_artifact_gate(workflow)


def test_release_build_runs_packaging_and_static_tests_before_publish():
    steps = _build_steps()
    suites = [
        index for index, step in enumerate(steps) if "-m pytest" in step.get("run", "")
    ]

    assert len(suites) == 1, "the build job must run the packaged test suites"
    command = steps[suites[0]]["run"]
    assert "tests/packaging" in command
    assert "tests/static" in command
    assert suites[0] < _upload_index(steps)


def test_release_bundles_the_frontend_with_the_synced_interpreter():
    # bundle-frontend expands $(PYTHON); a VENV_PYTHON override is silently
    # ignored and the bundle is built by the runner's system python3.
    commands = [
        step.get("run", "")
        for step in _build_steps()
        if "make bundle-frontend" in step.get("run", "")
    ]

    assert commands == ["make bundle-frontend PYTHON=.venv/bin/python"]


def test_publish_job_only_consumes_the_verified_build_artifact():
    publish = _workflow()["jobs"]["publish-pypi"]
    needs = publish["needs"]
    needs = [needs] if isinstance(needs, str) else list(needs)

    assert needs == ["build"]
    for step in publish["steps"]:
        # A `run` here would be a second build, routing around the gate the
        # other tests in this module pin onto the build job.
        assert "run" not in step
        assert str(step["uses"]).startswith(
            ("actions/download-artifact", "pypa/gh-action-pypi-publish")
        )


def test_every_deselected_node_id_still_exists():
    """A stale ``--deselect`` is silently a no-op, so it must name a real test.

    pytest does not error on a node id that matches nothing: it just runs the
    test the flag was meant to skip. The release job deselects one case that
    shells out to a second ``python -m build`` -- a rename or a move would
    quietly restore that second network build on every tag, and nothing else
    in the tree would notice.
    """

    deselected = []
    for step in _build_steps():
        command = step.get("run", "")
        parts = command.split()
        for index, token in enumerate(parts):
            if token == "--deselect" and index + 1 < len(parts):
                deselected.append(parts[index + 1])
            elif token.startswith("--deselect="):
                deselected.append(token.split("=", 1)[1])

    assert deselected, "expected the release job to deselect the rebuild case"
    for node_id in deselected:
        path, _, test_name = node_id.partition("::")
        source = ROOT / path
        assert source.exists(), f"--deselect names a missing file: {node_id}"
        assert f"def {test_name}(" in source.read_text(encoding="utf-8"), (
            f"--deselect names a test that no longer exists: {node_id}"
        )
