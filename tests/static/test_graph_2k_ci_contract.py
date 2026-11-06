"""Graph 2K runs during release qualification, outside ordinary CI."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECK_2K_COMMAND = (
    "npm run test:e2e -- tests/e2e/workspace_graph_2k_real.spec.ts "
    "tests/e2e/workspace_graph_reading_real.spec.ts "
    "tests/e2e/workspace_graph_first_view.spec.ts "
    "--project=chromium"
)
# Every spec that skips without the 2K corpus must be named by the release job.
CHECK_2K_SPECS = sorted(
    f"tests/e2e/{path.name}"
    for path in (ROOT / "tests/e2e").glob("*.spec.ts")
    if 'process.env.DTXWIKI_E2E_GRAPH_2K !== "1"' in path.read_text()
)
RELEASE_2K_COMMAND = (
    "npm run test:e2e -- tests/e2e/workspace_graph_2k_real.spec.ts "
    "tests/e2e/workspace_graph_reading_real.spec.ts "
    "tests/e2e/workspace_graph_first_view.spec.ts "
    "--project=chromium --workers=1 --retries=0 --reporter=line"
)
RELEASE_WORKER_COMMAND = (
    "npm run test:graph-webgl -- workspace-cpu-layout.spec.ts --reporter=line"
)
RELEASE_INSTALLED_WHEEL_COMMAND = (
    ".venv/bin/python tests/packaging/packaged_browser_smoke.py"
)


def assert_real_api_2k_check_step(steps):
    runs = [step for step in steps if step.get("run", "").strip() == CHECK_2K_COMMAND]
    assert len(runs) == 1
    run = runs[0]
    assert run.get("env", {}).get("DTXWIKI_E2E_GRAPH_2K") == "1"
    assert "if" not in run, "The 2K acceptance must not be manual or conditional."
    assert not run.get("continue-on-error", False)
    named = sorted(t for t in CHECK_2K_COMMAND.split() if t.endswith(".spec.ts"))
    assert named == CHECK_2K_SPECS, f"the 2K job runs {named}, gated: {CHECK_2K_SPECS}"


def test_2k_runs_only_in_release_checks_with_the_corpus_and_browser_together():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert "graph-2k" not in ci["jobs"]
    for job in ci["jobs"].values():
        assert "DTXWIKI_E2E_GRAPH_2K" not in job.get("env", {})
        for step in job.get("steps", []):
            assert "DTXWIKI_E2E_GRAPH_2K" not in step.get("env", {})
            assert "test:graph-webgl" not in step.get("run", "")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-checks.yml").read_text()
    )
    job = workflow["jobs"]["graph-2k"]
    assert "if" not in job
    assert not job.get("continue-on-error", False)
    assert_real_api_2k_check_step(job["steps"])
    webgl = [
        step
        for step in job["steps"]
        if step.get("run", "").strip()
        == "npm run test:graph-webgl -- --project=chromium"
    ]
    assert len(webgl) == 1
    assert "if" not in webgl[0]
    assert not webgl[0].get("continue-on-error", False)


@pytest.mark.counterexample(
    "a skipped, disabled or nonblocking real-API 2K release-check step is rejected"
)
@pytest.mark.parametrize(
    "violation", ["missing_flag", "disabled_flag", "conditional", "nonblocking"]
)
def test_release_check_guard_rejects_a_silently_skipped_or_nonblocking_case(violation):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-checks.yml").read_text()
    )
    steps = deepcopy(workflow["jobs"]["graph-2k"]["steps"])
    step = next(
        step for step in steps if step.get("run", "").strip() == CHECK_2K_COMMAND
    )
    if violation == "missing_flag":
        step.pop("env")
    elif violation == "disabled_flag":
        step["env"]["DTXWIKI_E2E_GRAPH_2K"] = "0"
    elif violation == "conditional":
        step["if"] = "github.event_name == 'workflow_dispatch'"
    else:
        step["continue-on-error"] = True
    with pytest.raises(AssertionError):
        assert_real_api_2k_check_step(steps)


def test_shared_2k_opt_in_reaches_both_fixture_and_real_browser_test():
    seeder = (ROOT / "tests/e2e/prepare_backend_db.py").read_text()
    browser = (ROOT / "tests/e2e/workspace_graph_2k_real.spec.ts").read_text()
    assert 'os.environ.get("DTXWIKI_E2E_GRAPH_2K") == "1"' in seeder
    assert 'process.env.DTXWIKI_E2E_GRAPH_2K !== "1"' in browser
    assert 'testMatch: "**/*.spec.ts"' in (ROOT / "playwright.config.ts").read_text()
    assert "page.route(" not in browser.replace("No page.route()", "No interception")


def assert_release_2k_steps(steps):
    upload = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )
    browser = [
        index
        for index, step in enumerate(steps)
        if step.get("run", "").strip() == "npx playwright install --with-deps chromium"
    ]
    assert len(browser) == 1
    for command in (
        RELEASE_2K_COMMAND,
        RELEASE_WORKER_COMMAND,
        RELEASE_INSTALLED_WHEEL_COMMAND,
    ):
        matches = [
            (index, step)
            for index, step in enumerate(steps)
            if step.get("run", "").strip() == command
        ]
        assert len(matches) == 1, "All Graph runtime gates must run on release tags."
        index, step = matches[0]
        assert browser[0] < index < upload
        assert "if" not in step, "The release 2K gate must not be conditional."
        assert not step.get("continue-on-error", False)
        if command == RELEASE_2K_COMMAND:
            assert step.get("env", {}).get("DTXWIKI_E2E_GRAPH_2K") == "1"


def test_release_tags_cannot_publish_without_the_real_2k_and_worker_csp_gates():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert_release_2k_steps(workflow["jobs"]["build"]["steps"])


@pytest.mark.parametrize(
    "violation",
    [
        "missing_real_gate",
        "missing_worker_gate",
        "missing_flag",
        "disabled_flag",
        "conditional",
        "nonblocking",
        "after_upload",
        "worker_conditional",
        "worker_nonblocking",
    ],
)
def test_release_guard_rejects_skipped_or_bypassed_2k_gates(violation):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = deepcopy(workflow["jobs"]["build"]["steps"])
    step = next(
        step for step in steps if step.get("run", "").strip() == RELEASE_2K_COMMAND
    )
    if violation == "missing_real_gate":
        steps.remove(step)
    elif violation == "missing_worker_gate":
        steps[:] = [
            candidate
            for candidate in steps
            if candidate.get("run", "").strip() != RELEASE_WORKER_COMMAND
        ]
    elif violation == "missing_flag":
        step.pop("env")
    elif violation == "disabled_flag":
        step["env"]["DTXWIKI_E2E_GRAPH_2K"] = "0"
    elif violation == "conditional":
        step["if"] = "github.event_name == 'workflow_dispatch'"
    elif violation == "nonblocking":
        step["continue-on-error"] = True
    elif violation in {"worker_conditional", "worker_nonblocking"}:
        worker_step = next(
            candidate
            for candidate in steps
            if candidate.get("run", "").strip() == RELEASE_WORKER_COMMAND
        )
        if violation == "worker_conditional":
            worker_step["if"] = "github.event_name == 'workflow_dispatch'"
        else:
            worker_step["continue-on-error"] = True
    else:
        steps.remove(step)
        steps.append(step)
    with pytest.raises(AssertionError):
        assert_release_2k_steps(steps)


@pytest.mark.parametrize("violation", ["missing", "conditional", "nonblocking", "late"])
def test_release_guard_rejects_a_bypassed_installed_wheel_runtime(violation):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = deepcopy(workflow["jobs"]["build"]["steps"])
    step = next(
        step
        for step in steps
        if step.get("run", "").strip() == RELEASE_INSTALLED_WHEEL_COMMAND
    )
    if violation == "missing":
        steps.remove(step)
    elif violation == "conditional":
        step["if"] = "github.event_name == 'workflow_dispatch'"
    elif violation == "nonblocking":
        step["continue-on-error"] = True
    else:
        steps.remove(step)
        steps.append(step)
    with pytest.raises(AssertionError):
        assert_release_2k_steps(steps)
