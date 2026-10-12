"""Pin how every GitHub workflow gets its toolchain and scans its history.

Four rules. Each has a checking function that takes parsed workflows, and a
counterexample that hands that function a violating workflow:

1. Every ``uv sync`` installs exactly what uv.lock records (``--locked``).
   Without the flag a sync silently re-resolves whenever pyproject.toml and
   uv.lock disagree, so CI tests -- and the release job builds -- dependency
   versions nobody locked or reviewed.
2. Every ``actions/setup-node`` pins the node in ``.nvmrc``, and every job that
   runs node sets it up first instead of inheriting whatever the runner image
   ships (`make test-static` and the release-gate tests start node too).
3. The leak scan runs the MIT-licensed gitleaks CLI from an image pinned by tag
   and digest, in ``git`` mode over the whole fetched history, redacted, and
   able to fail the job. gitleaks-action is not an option: it requires a
   GITLEAKS_LICENSE on an organization repository and exits 1 without one.
4. The CLA workflow runs on ``pull_request_target`` with write-scoped tokens,
   so every action in it is pinned to a commit rather than a movable tag, and
   it does not run on ``closed`` (only the disabled lock-after-merge feature
   needs that event). Its wiring is pinned too: this repository's token can
   only read code, comment and re-run; the signatures live on the main branch
   of the private signature repository and are written with a GitHub App token
   minted for that one repository with contents access only, never with a
   stored personal access token; the triggers and job gate the acceptance
   statement relies on; the protected cla-v1 tag as the document; and an
   allowlist without wildcards or bot names, which anyone can put in a commit's
   author field.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
NODE_PIN = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()

# The oldest gitleaks this pipeline was validated with in `git` mode.
MIN_GITLEAKS = (8, 24, 3)
GITLEAKS_IMAGE = re.compile(
    r"ghcr\.io/gitleaks/gitleaks:v(?P<version>\d+\.\d+\.\d+)"
    r"@sha256:(?P<digest>[0-9a-f]{64})(?![\w])"
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
# What the pinned CLA action does with this repository's token: re-run the
# workflow, read commits, comment on pull requests. It never touches commit
# statuses, and the signature file is written elsewhere with an App token.
CLA_PERMISSIONS = {"actions": "write", "contents": "read", "pull-requests": "write"}
CLA_DOCUMENT = "https://github.com/${{ github.repository }}/blob/cla-v1/CLA.md"
CLA_OWNER = "${{ github.repository_owner }}"
CLA_SIGNATURE_REPO = "dtxwiki-cla-signatures"
CLA_APP_KEY = "${{ secrets.CLA_APP_PRIVATE_KEY }}"
UV_SYNC = re.compile(r"\buv\s+sync\b[^\n;&|]*")
# A run step starts node directly, or through a make target / test tree that
# does: tests/static runs the i18n coverage gate (and the e2e self-runners when
# node_modules exists), and the release-gate tests start the gate script,
# which runs node.
RUNS_NODE = re.compile(
    r"(?<![\w./-])(?:node|npm|npx)(?![\w-])"
    r"|\bmake\s+(?:[\w=./-]+\s+)*?"
    r"(?:test-static|test-release-gate|test-frontend|test-e2e|test-packaged"
    r"|bundle-frontend|build-wheel|verify-generated-schemas|verify-gen-client"
    r"|gen-client|check-i18n|lint)\b"
    r"|\btests/static\b"
)


def load_workflows(directory: Path = WORKFLOW_DIR) -> dict[str, dict]:
    paths = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths
    }


def triggers(workflow: dict) -> dict:
    """The workflow's ``on:`` table. YAML 1.1 reads a bare ``on`` key as True."""

    value = workflow["on"] if "on" in workflow else workflow.get(True)
    if isinstance(value, str):
        return {value: None}
    if isinstance(value, list):
        return dict.fromkeys(value)
    return dict(value or {})


def _jobs(workflows: dict[str, dict]):
    for name, workflow in workflows.items():
        for job_id, job in ((workflow or {}).get("jobs") or {}).items():
            yield f"{name}:{job_id}", job


def _run(step: dict) -> str:
    # A trailing backslash continues a shell line; join so a flag on the next
    # line still counts as part of the same command.
    return str(step.get("run", "")).replace("\\\n", " ")


def _uses(step: dict) -> str:
    return str(step.get("uses", ""))


def uv_syncs(workflows: dict[str, dict]) -> list[tuple[str, str]]:
    return [
        (where, command.strip())
        for where, job in _jobs(workflows)
        for step in job.get("steps") or []
        for command in UV_SYNC.findall(_run(step))
    ]


def unlocked_uv_syncs(workflows: dict[str, dict]) -> list[str]:
    return [
        f"{where}: {command}"
        for where, command in uv_syncs(workflows)
        if "--locked" not in command.split()
    ]


def misplaced_node_pins(workflows: dict[str, dict], expected: str) -> list[str]:
    offenders = []
    for where, job in _jobs(workflows):
        for step in job.get("steps") or []:
            if not _uses(step).startswith("actions/setup-node@"):
                continue
            version = (step.get("with") or {}).get("node-version")
            # A bare 22.18 in YAML is the float 22.18 and a bare 22.10 would
            # request node 22.1, so only the quoted string form is accepted.
            if version != expected:
                offenders.append(f"{where}: node-version {version!r}")
    return offenders


def jobs_running_node(workflows: dict[str, dict]) -> set[str]:
    return {
        where
        for where, job in _jobs(workflows)
        if any(RUNS_NODE.search(_run(step)) for step in job.get("steps") or [])
    }


def node_runs_before_setup(workflows: dict[str, dict]) -> list[str]:
    offenders = []
    for where, job in _jobs(workflows):
        for step in job.get("steps") or []:
            if _uses(step).startswith("actions/setup-node@"):
                break
            if RUNS_NODE.search(_run(step)):
                first_line = _run(step).strip().splitlines()[0]
                offenders.append(f"{where}: `{first_line}` runs before setup-node")
                break
    return offenders


def leak_scan_problems(workflow: dict, job_id: str = "leak-scan") -> list[str]:
    job = (workflow.get("jobs") or {}).get(job_id)
    if job is None:
        return [f"there is no `{job_id}` job"]
    steps = job.get("steps") or []
    problems = []

    checkouts = [s for s in steps if _uses(s).startswith("actions/checkout@")]
    if not checkouts or (checkouts[0].get("with") or {}).get("fetch-depth") != 0:
        problems.append("the checkout must fetch the whole history (fetch-depth: 0)")
    if any("gitleaks-action" in _uses(step) for step in steps):
        problems.append(
            "gitleaks-action needs GITLEAKS_LICENSE on an organization "
            "repository and exits 1 without it; run the gitleaks CLI"
        )
    if "GITLEAKS_LICENSE" in yaml.safe_dump(job):
        problems.append("the CLI needs no GITLEAKS_LICENSE; do not wire one in")
    if job.get("continue-on-error"):
        problems.append("the leak-scan job must be able to fail the workflow")

    scans = [step for step in steps if "gitleaks" in _run(step)]
    if len(scans) != 1:
        problems.append(f"expected one gitleaks run step, found {len(scans)}")
        return problems
    step = scans[0]
    command = _run(step)
    tokens = command.split()
    if step.get("continue-on-error") or "||" in tokens:
        problems.append("a finding must fail the step, not be swallowed")

    image = GITLEAKS_IMAGE.search(command)
    if image is None:
        problems.append(
            "the gitleaks image must be pinned by version tag AND sha256 digest"
        )
        return problems
    version = tuple(int(part) for part in image.group("version").split("."))
    if version < MIN_GITLEAKS:
        problems.append(f"gitleaks {image.group('version')} predates {MIN_GITLEAKS}")
    arguments = tokens[tokens.index(image.group(0)) + 1 :]
    if not arguments or arguments[0] != "git":
        problems.append("gitleaks must scan in `git` mode (history, not files)")
    if not any(a == "--redact" or a.startswith("--redact=") for a in arguments):
        problems.append("--redact keeps a finding's value out of the public log")
    if any(a.startswith(("--log-opts", "--no-git")) for a in arguments):
        problems.append("--log-opts/--no-git narrow the scan below the full history")
    for index, argument in enumerate(arguments):
        value = None
        if argument == "--exit-code" and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif argument.startswith("--exit-code="):
            value = argument.split("=", 1)[1]
        if value == "0":
            problems.append("--exit-code 0 turns every finding into a pass")
    return problems


def cla_problems(workflow: dict) -> list[str]:
    problems = []
    target = triggers(workflow).get("pull_request_target") or {}
    if "closed" in (target.get("types") or []):
        problems.append(
            "pull_request_target runs on `closed`, which only the disabled "
            "lock-after-merge feature needs"
        )
    actions = [
        _uses(step)
        for _, job in _jobs({"cla": workflow})
        for step in job.get("steps") or []
        if _uses(step)
    ]
    if not any(
        uses.startswith("contributor-assistant/github-action@") for uses in actions
    ):
        problems.append("no contributor-assistant/github-action step")
    for uses in actions:
        if not COMMIT_SHA.match(uses.partition("@")[2]):
            problems.append(f"{uses} must be pinned to a 40-character commit SHA")
    return problems + cla_wiring_problems(workflow)


def _participant_guard_refuses_unsafe_lists(script: str) -> bool:
    match = re.search(r"^python3 - <<'PY'\n(.*?)^PY$", script, re.M | re.S)
    if match is None:
        return False
    try:
        statements = ast.parse(match.group(1)).body
    except SyntaxError:
        return False

    def expression(source: str) -> str:
        return ast.dump(ast.parse(source, mode="eval").body)

    reject = expression("sys.exit(1)")
    rejecting_conditions = {
        ast.dump(statement.test)
        for statement in statements
        if isinstance(statement, ast.If)
        and any(
            isinstance(item, ast.Expr) and ast.dump(item.value) == reject
            for item in statement.body
        )
    }
    unlinked = expression(
        '[commit["sha"] for commit in commits if commit.get("author") is None]'
    )
    return {expression("not 1 <= count <= 100"), expression("unlinked")} <= (
        rejecting_conditions
    ) and any(
        isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "unlinked"
            for target in statement.targets
        )
        and ast.dump(statement.value) == unlinked
        for statement in statements
    )


def cla_wiring_problems(workflow: dict) -> list[str]:
    problems = []
    if workflow.get("permissions") != CLA_PERMISSIONS:
        problems.append(
            "permissions must be exactly actions: write, contents: read and "
            "pull-requests: write (no statuses)"
        )
    on = triggers(workflow)
    if on.get("pull_request_target") != {
        "types": ["opened", "reopened", "synchronize"]
    }:
        problems.append(
            "pull_request_target must run on exactly opened, reopened and synchronize"
        )
    if on.get("issue_comment") != {"types": ["created"]}:
        problems.append("issue_comment must run on exactly created")
    job = (workflow.get("jobs") or {}).get("cla") or {}
    steps = job.get("steps") or []
    cla_step = next(
        (
            step
            for step in steps
            if _uses(step).startswith("contributor-assistant/github-action@")
        ),
        {},
    )
    with_ = cla_step.get("with") or {}
    token_steps = [
        step
        for step in steps
        if _uses(step).startswith("actions/create-github-app-token@")
    ]
    token_step = token_steps[0] if len(token_steps) == 1 else {}
    if not token_step or steps.index(token_step) > steps.index(cla_step or token_step):
        problems.append(
            "exactly one actions/create-github-app-token step must mint the "
            "signature token before the CLA step"
        )
    token_with = token_step.get("with") or {}
    permissions = {key for key in token_with if key.startswith("permission-")}
    if permissions != {"permission-contents"} or token_with.get(
        "permission-contents"
    ) != "write":
        problems.append("the App token must ask for contents: write and nothing else")
    if (
        token_with.get("owner") != CLA_OWNER
        or token_with.get("repositories") != CLA_SIGNATURE_REPO
    ):
        problems.append(
            "the App token must be scoped to the signature repository alone"
        )
    text = yaml.safe_dump(workflow)
    if token_with.get("private-key") != CLA_APP_KEY or text.count(
        "CLA_APP_PRIVATE_KEY"
    ) != 1:
        problems.append(
            "the App private key may be passed only to the token-minting step"
        )
    if (
        with_.get("remote-organization-name") != CLA_OWNER
        or with_.get("remote-repository-name") != CLA_SIGNATURE_REPO
        or with_.get("branch") != "main"
    ):
        problems.append(
            "signatures must go to the main branch of the private signature "
            "repository"
        )
    minted = "${{ steps." + str(token_step.get("id")) + ".outputs.token }}"
    if (cla_step.get("env") or {}).get("PERSONAL_ACCESS_TOKEN") != minted or (
        "secrets.PERSONAL_ACCESS_TOKEN" in text
    ):
        problems.append(
            "the action must write signatures with the minted App token, not a "
            "stored personal access token"
        )
    expected_gate = (
        "github.event_name == 'pull_request_target' || "
        "(github.event.issue.pull_request && "
        "(contains(github.event.comment.body, 'recheck') || "
        "contains(github.event.comment.body, "
        f"'{with_.get('custom-pr-sign-comment')}')))"
    )
    if " ".join(str(job.get("if", "")).split()) != expected_gate:
        problems.append(
            "the job gate must be: every pull_request_target event, or a PR "
            "comment containing `recheck` or the acceptance statement"
        )
    if with_.get("path-to-document") != CLA_DOCUMENT:
        problems.append(
            "path-to-document must be CLA.md at this repository's protected "
            "cla-v1 tag"
        )
    allowlist = [
        entry.strip() for entry in str(with_.get("allowlist", "")).split(",")
    ]
    if any("*" in entry for entry in allowlist):
        problems.append(
            "a wildcard allowlist switches the CLA check off for the matching "
            "committers"
        )
    if any(entry.endswith("[bot]") for entry in allowlist):
        problems.append(
            "a bot name in the allowlist lets anyone skip the CLA by authoring "
            "commits under that name"
        )
    # Untrusted event text (comments, descriptions, branch names) must reach a
    # script only through the event file, never through a ${{ }} expression
    # that the runner pastes into the script.
    if any("${{" in str(step.get("run", "")) for step in steps):
        problems.append("a run script must not contain a ${{ }} expression")
    # The guard runs first: it refuses pull requests whose commit authors the
    # action would guess from the committer or a free-text name.
    guard = str((steps[0] if steps else {}).get("run", ""))
    if not _participant_guard_refuses_unsafe_lists(guard):
        problems.append(
            "the first step must refuse more than 100 commits and commit "
            "authors that are not linked to a GitHub account"
        )
    evidence = next(
        (
            step
            for step in steps
            if "signatures/version1/evidence/" in str(step.get("run", ""))
        ),
        {},
    )
    evidence_env = evidence.get("env") or {}
    after_failure = (
        "${{ !cancelled() && steps."
        + str(token_step.get("id"))
        + ".outcome == 'success' }}"
    )
    if (
        evidence.get("if") != after_failure
        or evidence_env.get("STATEMENT") != with_.get("custom-pr-sign-comment")
        or evidence_env.get("SIG_TOKEN") != minted
        or cla_step not in steps
        or steps.index(evidence) < steps.index(cla_step)
    ):
        problems.append(
            "a step after the action must store the evidence of every recorded "
            "acceptance with the minted App token, even when the action fails"
        )
    # The action examines commit authors, which anyone can fake with an email
    # GitHub links to another account. The account that opened the pull request
    # is the individual who Submits (CLA.md section 1): a step after the action
    # must require its acceptance, exempting only the allowlisted maintainer.
    author = next(
        (
            step
            for step in steps
            if "signedContributors" in str(step.get("run", ""))
            and "signatures/version1/evidence/" not in str(step.get("run", ""))
        ),
        {},
    )
    author_env = author.get("env") or {}
    if (
        not author
        or author_env.get("SIG_TOKEN") != minted
        or author_env.get("EXEMPT") != with_.get("allowlist")
        or cla_step not in steps
        or steps.index(author) < steps.index(cla_step)
    ):
        problems.append(
            "a step after the action must require the acceptance of the account "
            "that opened the pull request"
        )
    return problems


# ---------------------------------------------------------------------------
# The tree.
# ---------------------------------------------------------------------------


def test_every_uv_sync_installs_from_the_lock():
    workflows = load_workflows()

    assert uv_syncs(workflows), "found no `uv sync` at all; the scan is broken"
    assert unlocked_uv_syncs(workflows) == []


def test_every_setup_node_pins_the_nvmrc_version():
    workflows = load_workflows()

    assert NODE_PIN, ".nvmrc is empty"
    assert misplaced_node_pins(workflows, NODE_PIN) == []


def test_every_job_that_runs_node_sets_it_up_first():
    workflows = load_workflows()

    # Anti-vacuity: the detector must see the jobs that motivated the rule,
    # including the two whose node is only implicit.
    assert {
        "ci.yml:backend",
        "ci.yml:packaging",
        "ci.yml:frontend",
        "release.yml:build",
    } <= jobs_running_node(workflows)
    assert node_runs_before_setup(workflows) == []


def test_leak_scan_runs_the_pinned_gitleaks_cli_over_the_full_history():
    workflows = load_workflows()

    assert leak_scan_problems(workflows["ci.yml"]) == []
    for name, workflow in workflows.items():
        text = yaml.safe_dump(workflow)
        assert "gitleaks-action" not in text, name
        assert "GITLEAKS_LICENSE" not in text, name


def test_cla_action_is_pinned_to_a_commit_and_skips_closed_events():
    assert cla_problems(load_workflows()["cla.yml"]) == []


# ---------------------------------------------------------------------------
# Counterexamples: each checking function rejects the shape it exists for.
# ---------------------------------------------------------------------------


def _workflow(*steps: dict, job: str = "build", **job_fields) -> dict:
    return {"on": {"push": None}, "jobs": {job: {"steps": list(steps), **job_fields}}}


@pytest.mark.counterexample(
    "unlocked_uv_syncs reports `uv sync --extra dev` and a sync whose --locked "
    "sits on another command, and accepts --locked on a continuation line"
)
def test_unlocked_uv_sync_is_reported():
    workflows = {
        "a.yml": _workflow(
            {"run": "uv sync --extra dev"},
            {"run": "uv sync --extra desktop\nnpm ci --locked"},
            {"run": "uv sync \\\n  --locked --extra dev"},
        )
    }

    assert unlocked_uv_syncs(workflows) == [
        "a.yml:build: uv sync --extra dev",
        "a.yml:build: uv sync --extra desktop",
    ]


@pytest.mark.counterexample(
    "misplaced_node_pins reports a floating major, an unquoted float and a "
    "node-version-file in place of the pinned string"
)
def test_unpinned_node_is_reported():
    workflows = {
        "a.yml": _workflow(
            {"uses": "actions/setup-node@v4", "with": {"node-version": "22"}},
            {"uses": "actions/setup-node@v4", "with": {"node-version": 22.18}},
            {"uses": "actions/setup-node@v4", "with": {"node-version-file": "x"}},
            {"uses": "actions/setup-node@v4", "with": {"node-version": "22.18"}},
        )
    }

    assert misplaced_node_pins(workflows, "22.18") == [
        "a.yml:build: node-version '22'",
        "a.yml:build: node-version 22.18",
        "a.yml:build: node-version None",
    ]


@pytest.mark.counterexample(
    "node_runs_before_setup reports a job that reaches node only through "
    "`make test-static`, and one that sets node up after using it"
)
def test_node_without_setup_is_reported():
    workflows = {
        "a.yml": {
            "jobs": {
                "implicit": {"steps": [{"run": "VENV=.venv make test-static"}]},
                "late": {
                    "steps": [
                        {"run": "npm ci"},
                        {"uses": "actions/setup-node@v4"},
                    ]
                },
                "fine": {
                    "steps": [
                        {"uses": "actions/setup-node@v4"},
                        {"run": "node scripts/check-i18n-coverage.mjs"},
                    ]
                },
                "no-node": {
                    "steps": [{"run": "ls node_modules; make test-backend"}]
                },
            }
        }
    }

    assert jobs_running_node(workflows) == {
        "a.yml:implicit",
        "a.yml:late",
        "a.yml:fine",
    }
    assert node_runs_before_setup(workflows) == [
        "a.yml:implicit: `VENV=.venv make test-static` runs before setup-node",
        "a.yml:late: `npm ci` runs before setup-node",
    ]


_PINNED = (
    "ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:"
    "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
_CHECKOUT = {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}


def _scan(command: str, checkout: dict = _CHECKOUT, **job_fields) -> dict:
    return _workflow(checkout, {"run": command}, job="leak-scan", **job_fields)


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (
            _workflow(
                _CHECKOUT, {"uses": "gitleaks/gitleaks-action@v3"}, job="leak-scan"
            ),
            "gitleaks-action needs GITLEAKS_LICENSE",
        ),
        (
            _scan(
                "docker run -v .:/repo ghcr.io/gitleaks/gitleaks:v8.30.1 git /repo "
                "--redact"
            ),
            "pinned by version tag AND sha256 digest",
        ),
        (
            _scan(f"docker run -v .:/repo {_PINNED} dir --redact /repo"),
            "`git` mode",
        ),
        (_scan(f"docker run -v .:/repo {_PINNED} git /repo"), "--redact"),
        (
            _scan(f"docker run {_PINNED} git --redact --log-opts=-1 /repo"),
            "narrow the scan",
        ),
        (
            _scan(f"docker run {_PINNED} git --redact --exit-code 0 /repo"),
            "--exit-code 0",
        ),
        (
            _scan(f"docker run {_PINNED} git --redact /repo || true"),
            "must fail the step",
        ),
        (
            _scan(
                f"docker run {_PINNED} git --redact /repo",
                checkout={"uses": "actions/checkout@v4"},
            ),
            "fetch-depth: 0",
        ),
        (
            _scan(
                "docker run ghcr.io/gitleaks/gitleaks:v8.18.0@sha256:"
                + "0" * 64
                + " git --redact /repo"
            ),
            "predates",
        ),
    ],
)
@pytest.mark.counterexample(
    "leak_scan_problems rejects the licensed action, an unpinned or old image, "
    "non-git modes, unredacted, narrowed or non-failing scans and a shallow "
    "checkout"
)
def test_leak_scan_regressions_are_reported(workflow, expected):
    problems = leak_scan_problems(workflow)

    assert any(expected in problem for problem in problems), problems


def test_the_pinned_leak_scan_shape_itself_passes():
    # The positive control for the counterexamples above: the same helper
    # with every requirement met reports nothing.
    workflow = _scan(f'docker run --rm -v "$PWD:/repo" {_PINNED} git --redact /repo')

    assert leak_scan_problems(workflow) == []


@pytest.mark.counterexample(
    "cla_problems reports a movable tag ref and a `closed` trigger"
)
def test_cla_regressions_are_reported():
    workflow = {
        "on": {"pull_request_target": {"types": ["opened", "closed"]}},
        "jobs": {
            "cla": {
                "steps": [{"uses": "contributor-assistant/github-action@v2.6.1"}]
            }
        },
    }

    problems = cla_problems(workflow)

    assert any("`closed`" in problem for problem in problems), problems
    assert any("40-character commit SHA" in problem for problem in problems), (
        problems
    )


@pytest.mark.counterexample(
    "cla_wiring_problems reports write access to this repository, an "
    "exact-match gate, the moving main/CLA.md link, wildcard or bot allowlist "
    "entries, a stored personal access token and an over-broad App token"
)
def test_cla_wiring_regressions_are_reported():
    workflow = load_workflows()["cla.yml"]
    job = workflow["jobs"]["cla"]
    step = next(
        step
        for step in job["steps"]
        if _uses(step).startswith("contributor-assistant/github-action@")
    )
    token = next(
        step
        for step in job["steps"]
        if _uses(step).startswith("actions/create-github-app-token@")
    )
    statement = step["with"]["custom-pr-sign-comment"]
    workflow["permissions"]["contents"] = "write"
    job["if"] = f"github.event.comment.body == '{statement}'"
    step["with"]["path-to-document"] = (
        "https://github.com/${{ github.repository }}/blob/main/CLA.md"
    )
    step["with"]["allowlist"] = "RonyK,*,dependabot[bot]"
    step["env"]["PERSONAL_ACCESS_TOKEN"] = "${{ secrets.PERSONAL_ACCESS_TOKEN }}"
    token["with"]["permission-issues"] = "write"
    del token["with"]["repositories"]

    problems = cla_wiring_problems(workflow)

    for expected in (
        "permissions must be exactly",
        "the job gate must be",
        "protected cla-v1 tag",
        "wildcard allowlist",
        "bot name in the allowlist",
        "not a stored personal access token",
        "contents: write and nothing else",
        "signature repository alone",
    ):
        assert any(expected in problem for problem in problems), (expected, problems)


@pytest.mark.counterexample(
    "cla_problems reports an App token action pinned to a movable tag"
)
def test_cla_token_action_must_be_pinned():
    workflow = load_workflows()["cla.yml"]
    token = next(
        step
        for step in workflow["jobs"]["cla"]["steps"]
        if _uses(step).startswith("actions/create-github-app-token@")
    )
    token["uses"] = "actions/create-github-app-token@v3"

    problems = cla_problems(workflow)

    assert any(
        "actions/create-github-app-token@v3 must be pinned" in problem
        for problem in problems
    ), problems


@pytest.mark.counterexample(
    "cla_wiring_problems reports an event expression in a script, a missing "
    "commit-author guard and evidence kept with another statement"
)
def test_cla_guard_and_evidence_regressions_are_reported():
    workflow = load_workflows()["cla.yml"]
    steps = workflow["jobs"]["cla"]["steps"]
    evidence = next(
        step
        for step in steps
        if "signatures/version1/evidence/" in str(step.get("run", ""))
    )
    evidence["run"] += "\necho ${{ github.event.comment.body }}\n"
    evidence["env"]["STATEMENT"] = "I agree"
    steps.pop(0)
    steps[:] = [step for step in steps if "EXEMPT" not in (step.get("env") or {})]

    problems = cla_wiring_problems(workflow)

    for expected in (
        "must not contain a ${{ }} expression",
        "first step must refuse",
        "store the evidence of every recorded acceptance",
        "require the acceptance of the account that opened the pull request",
    ):
        assert any(expected in problem for problem in problems), (expected, problems)
