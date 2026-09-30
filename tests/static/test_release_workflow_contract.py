"""Pin the artifact gate in front of PyPI.

release.yml fires on any ``v*`` tag while ci.yml runs only on pull requests and
pushes to main, so a tag can reach `pypa/gh-action-pypi-publish` on a commit no
other workflow ever built. These tests keep the gate from being narrowed back
to the three `test -f` calls it used to be.
"""

from pathlib import Path

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

    steps = _build_steps()
    command = steps[_verify_index(steps)]["run"]
    uploaded = steps[_upload_index(steps)]["with"]["path"]
    argument = command.split("scripts/verify_wheel_artifact.py", 1)[1].strip()

    assert argument.rstrip("/") == uploaded.rstrip("/"), (
        f"verify is handed {argument!r} but upload-artifact publishes "
        f"{uploaded!r}"
    )


def test_release_build_runs_packaging_and_static_tests_before_publish():
    steps = _build_steps()
    suites = [
        index
        for index, step in enumerate(steps)
        if "-m pytest" in step.get("run", "")
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
