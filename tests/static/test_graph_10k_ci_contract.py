"""A green CI must include actual PC10K and dense capacity browser cases."""

import re
import shlex
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPECS = (
    "tests/e2e/workspace_graph_10k_real.spec.ts",
    "tests/e2e/workspace_graph_10k_motion.spec.ts",
    "tests/e2e/workspace_graph_10k_aggregated.spec.ts",
)
# The only options the graph-10k run may carry. Any other (--grep, --grep-invert,
# --only-changed, --pass-with-no-tests, --shard, --last-failed, -u, ...) can let a
# failing case exit 0, so a new one needs a decision here. `--workers=0` runs
# nothing and exits 0; without `--forbid-only` a committed `test.only` runs alone
# and the lane stays green (both measured on Playwright 1.59.1).
ALLOWED_OPTIONS = ("--project=chromium", "--retries=0", "--workers=1", "--forbid-only")
SEEDER = "tests/e2e/prepare_backend_db.py"
FLAG = r"DTXWIKI_E2E_GRAPH_10K\w*"


def assert_run_hides_no_case(run):
    tokens = shlex.split(run)
    assert tokens[:4] == ["npm", "run", "test:e2e", "--"], (
        f"unexpected start {tokens[:4]}"
    )
    options = [t for t in tokens[4:] if t.startswith("-")]
    for option in options:
        assert option in ALLOWED_OPTIONS or option.startswith("--reporter="), (
            f"`{option}` may skip a case while the lane stays green"
        )
    assert options.count("--forbid-only") == 1, (
        "the run must carry `--forbid-only` once"
    )
    files = [t for t in tokens[4:] if not t.startswith("-")]
    assert sorted(files) == sorted(SPECS), (
        f"the run must name exactly the 10K spec files, got {files}"
    )


def assert_pc_10k_job(job):
    assert "if" not in job
    assert not job.get("continue-on-error", False)
    runs = [
        step for step in job["steps"] if "npm run test:e2e --" in step.get("run", "")
    ]
    assert len(runs) == 1
    step = runs[0]
    for flag in (
        "DTXWIKI_E2E_GRAPH_10K",
        "DTXWIKI_E2E_GRAPH_10K_DENSE",
        "DTXWIKI_E2E_GRAPH_10K_AGGREGATED",
    ):
        assert step.get("env", {}).get(flag) == "1"
    for argument in (
        "tests/e2e/workspace_graph_10k_real.spec.ts",
        "tests/e2e/workspace_graph_10k_motion.spec.ts",
        "tests/e2e/workspace_graph_10k_aggregated.spec.ts",
        "--project=chromium",
        "--retries=0",
    ):
        assert argument in step["run"]
    assert_run_hides_no_case(step["run"])
    assert "if" not in step
    assert not step.get("continue-on-error", False)
    upload = next(
        index
        for index, entry in enumerate(job["steps"])
        if entry.get("uses", "").startswith("actions/upload-artifact")
    )
    redact = next(
        index
        for index, entry in enumerate(job["steps"])
        if "bash scripts/redact_evidence.sh --dir playwright-report"
        in entry.get("run", "")
    )
    assert redact < upload
    assert job["steps"][upload]["with"]["path"] == "playwright-report"


def graph_10k_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return deepcopy(workflow["jobs"]["graph-10k"])


def lane_step(job):
    return next(s for s in job["steps"] if "npm run test:e2e --" in s.get("run", ""))


def test_pc_10k_ci_executes_original_and_dense_real_api_cases():
    assert_pc_10k_job(graph_10k_job())


@pytest.mark.counterexample(
    "a skipped, disabled, incomplete or nonblocking PC10K CI lane is rejected"
)
@pytest.mark.parametrize(
    "violation",
    [
        "missing_flag",
        "disabled_dense",
        "missing_aggregated",
        "conditional",
        "nonblocking",
        "missing_motion",
    ],
)
def test_pc_10k_ci_guard_rejects_silent_skips(violation):
    job = graph_10k_job()
    step = lane_step(job)
    if violation == "missing_flag":
        step["env"].pop("DTXWIKI_E2E_GRAPH_10K")
    elif violation == "disabled_dense":
        step["env"]["DTXWIKI_E2E_GRAPH_10K_DENSE"] = "0"
    elif violation == "missing_aggregated":
        step["env"].pop("DTXWIKI_E2E_GRAPH_10K_AGGREGATED")
    elif violation == "conditional":
        job["if"] = "false"
    elif violation == "nonblocking":
        step["continue-on-error"] = True
    else:
        step["run"] = step["run"].replace(
            "tests/e2e/workspace_graph_10k_motion.spec.ts", ""
        )
    with pytest.raises(AssertionError):
        assert_pc_10k_job(job)


@pytest.mark.counterexample(
    "a Playwright option, a line filter or a missing --forbid-only that lets a "
    "failing 10K case exit 0 is rejected"
)
@pytest.mark.parametrize(
    ("edit", "complaint"),
    [
        (lambda run: f"{run} --grep-invert=10K", "`--grep-invert=10K`"),
        (lambda run: f"{run} --only-changed", "`--only-changed`"),
        (lambda run: f"{run} --pass-with-no-tests", "`--pass-with-no-tests`"),
        (lambda run: run.replace("--workers=1", "--workers=0"), "`--workers=0`"),
        (lambda run: run.replace(" --forbid-only", ""), "carry `--forbid-only`"),
        (
            lambda run: run.replace("motion.spec.ts", "motion.spec.ts:165"),
            "10K spec files",
        ),
    ],
    ids=[
        "grep-invert",
        "only-changed",
        "pass-with-no-tests",
        "workers-0",
        "no-forbid-only",
        "line-filter",
    ],
)
def test_pc_10k_ci_guard_rejects_options_that_can_hide_a_case(edit, complaint):
    run = lane_step(graph_10k_job())["run"]
    assert edit(run) != run
    with pytest.raises(AssertionError, match=re.escape(complaint)):
        assert_run_hides_no_case(edit(run))


def flags_agree(job, specs, seeder):
    """Flags the job sets = flags the specs skip on; the seeder acts on each."""
    set_by_job = {
        k: v for k, v in lane_step(job).get("env", {}).items() if re.fullmatch(FLAG, k)
    }
    skipped_on = set(re.findall(rf'process\.env\.({FLAG})\s*!==\s*"1"', "".join(specs)))
    seeded = set(re.findall(rf'os\.environ\.get\("({FLAG})"\)\s*==\s*"1"', seeder))
    assert skipped_on, "no 10K spec skips on an opt-in flag"
    assert skipped_on == set(set_by_job), (
        f"the specs skip on {sorted(skipped_on)} but the job sets {sorted(set_by_job)}"
    )
    assert set(set_by_job.values()) == {"1"}, (
        f"every flag must be set to 1: {set_by_job}"
    )
    assert skipped_on <= seeded, (
        f"the seeder does not act on {sorted(skipped_on - seeded)}"
    )


def rename_flag(text):
    return re.sub(r"\bDTXWIKI_E2E_GRAPH_10K\b", "DTXWIKI_E2E_GRAPH_10K_RENAMED", text)


def test_pc_10k_flags_reach_the_seeder_and_every_spec():
    specs = [(ROOT / spec).read_text() for spec in SPECS]
    seeder = (ROOT / SEEDER).read_text()
    flags_agree(graph_10k_job(), specs, seeder)
    with pytest.raises(AssertionError, match="the specs skip on"):
        flags_agree(
            graph_10k_job(), [specs[0], rename_flag(specs[1]), specs[2]], seeder
        )
    with pytest.raises(AssertionError, match="does not act on"):
        flags_agree(graph_10k_job(), specs, rename_flag(seeder))
