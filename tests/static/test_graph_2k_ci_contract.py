"""The real-API 2K case must execute, not quietly skip, on pull requests."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def assert_real_api_2k_ci_step(steps):
    runs = [step for step in steps if step.get("run", "").strip() == "npm run test:e2e"]
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
        step for step in steps if step.get("run", "").strip() == "npm run test:e2e"
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
