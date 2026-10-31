"""The real-API 2K case must execute, not quietly skip, on pull requests."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_E2E_COMMAND = "npm run test:e2e -- ${{ matrix.projects }}"
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


def assert_real_api_2k_ci_step(steps):
    runs = [step for step in steps if step.get("run", "").strip() == CI_E2E_COMMAND]
    assert len(runs) == 1
    run = runs[0]
    assert run.get("env", {}).get("DTXWIKI_E2E_GRAPH_2K") == "1"
    assert "if" not in run, "The 2K acceptance must not be manual or conditional."
    assert not run.get("continue-on-error", False)


def test_ci_activates_the_real_api_2k_corpus_and_browser_case_together():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert_real_api_2k_ci_step(workflow["jobs"]["e2e"]["steps"])


@pytest.mark.counterexample(
    "a skipped, disabled or nonblocking real-API 2K CI step is rejected"
)
@pytest.mark.parametrize(
    "violation", ["missing_flag", "disabled_flag", "conditional", "nonblocking"]
)
def test_ci_guard_rejects_a_silently_skipped_or_nonblocking_case(violation):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = deepcopy(workflow["jobs"]["e2e"]["steps"])
    step = next(
        step for step in steps if step.get("run", "").strip() == CI_E2E_COMMAND
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
        assert_real_api_2k_ci_step(steps)


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
        if step.get("run", "").strip()
        == "npx playwright install --with-deps chromium"
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
