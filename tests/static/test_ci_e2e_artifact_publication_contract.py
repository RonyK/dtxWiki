"""E2E-03 follow-up (S1 residual, S13, S14): what CI is allowed to publish.

``tests/static/test_e2e_artifact_redaction_contract.py`` guards what the e2e
*harness* writes. This module guards the step after that one: what the two e2e
jobs hand to ``actions/upload-artifact``.

The gap it closes. The Playwright HTML reporter appends the entire report as a
base64-encoded zip inside ``<template id="playwrightReportBase64">`` in
``index.html`` (node_modules/playwright/lib/reporters/html.js:335). Every step
title goes in there on **every** run, passing runs included and regardless of
the trace or screenshot mode -- so
``resetPage.goto("/reset-password?token=<one-time token>")`` is published by a
green run, ``if: always()``, for 14 days. The token is nowhere in the file as
plaintext, so grepping the report finds nothing and a stdin->stdout pipe over
it changes nothing.

Three things therefore have to hold together, and each is pinned below:

1. Both e2e jobs run ``scripts/redact_evidence.sh --dir`` over the directory
   they are about to upload, before the upload step.
2. That mode actually reaches the base64 payload -- decode, redact, rebuild --
   and leaves the reporter's own application bytes alone.
3. The break-glass hard-fail (production secret env name, PEM private key)
   survives in both modes.

Plus S13: the frontend job's node is pinned at or above 22.18, because that
job is the only place the e2e helper self-runners execute and they need
unflagged TypeScript type stripping.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REDACTOR = ROOT / "scripts/redact_evidence.sh"
CI = ROOT / ".github/workflows/ci.yml"
E2E_BROWSERS = ROOT / ".github/workflows/e2e-browsers.yml"

# (workflow file, job id) -- every job that publishes a Playwright report.
PUBLISHING_JOBS = ((CI, "e2e"), (E2E_BROWSERS, "exhaustive"))

# Node ships unflagged TypeScript type stripping from v22.18.
MIN_NODE_FOR_TYPE_STRIPPING = (22, 18)

TOKEN = "dtxr_1_STATIC_CONTRACT_ONETIME_TOKEN"
REPORT_PAYLOAD = re.compile(
    r'(<template id="playwrightReportBase64">data:application/zip;base64,)'
    r"(.*?)(</template>)",
    re.DOTALL,
)


def _steps(workflow: Path, job: str) -> list[dict]:
    loaded = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert job in loaded["jobs"], (
        f"{workflow.relative_to(ROOT)} no longer has a `{job}` job; this "
        "contract is addressed at the jobs that publish a Playwright report"
    )
    return loaded["jobs"][job]["steps"]


def _upload_step(steps: list[dict]) -> tuple[int, dict]:
    for index, step in enumerate(steps):
        if str(step.get("uses", "")).startswith("actions/upload-artifact"):
            return index, step
    raise AssertionError("the job no longer uploads an artifact")


# ---------------------------------------------------------------------------
# 1. The workflows redact before they publish.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("workflow", "job"),
    PUBLISHING_JOBS,
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_the_e2e_jobs_redact_the_report_before_uploading_it(workflow, job):
    steps = _steps(workflow, job)
    upload_index, upload = _upload_step(steps)
    published = str(upload["with"]["path"]).strip()

    # Anti-vacuity: this guard only means something while the report is the
    # thing being published.
    assert "playwright-report" in published, (
        f"{workflow.relative_to(ROOT)}:{job} no longer uploads the Playwright "
        f"report (it uploads {published!r}); re-read this module's docstring "
        "before relaxing anything here"
    )

    redactions = [
        (index, step)
        for index, step in enumerate(steps)
        if "scripts/redact_evidence.sh" in step.get("run", "")
    ]
    assert len(redactions) == 1, (
        f"{workflow.relative_to(ROOT)}:{job} must scrub the report exactly "
        "once before publishing it: the HTML reporter base64-inlines every "
        "`page.goto('/reset-password?token=...')` step title into index.html "
        "on every run, green ones included"
    )
    index, step = redactions[0]
    command = step["run"]

    assert index < upload_index, (
        "the redaction step runs after upload-artifact, so the unredacted "
        "report is what gets published"
    )
    assert "--dir" in command, (
        "a stdin->stdout pipe cannot reach the report: the token lives in a "
        "base64-encoded zip inside index.html, not in any plaintext line. "
        f"got: {command!r}"
    )
    # NOT `published in command`: that is a substring test, and
    # `--dir playwright-report/data` contains "playwright-report" while leaving
    # index.html -- the carrier this whole module exists for -- unscrubbed.
    # A mutation doing exactly that passed all six tests here.
    scrubbed = _dir_argument(command)
    assert scrubbed is not None, (
        f"could not read the --dir target out of the redaction step: {command!r}"
    )
    assert PurePosixPath(scrubbed) == PurePosixPath(published.strip()), (
        "the redaction step must scrub the EXACT path that is uploaded, not a "
        "subdirectory of it: index.html carries the base64 report payload and "
        f"lives at the root. uploads {published!r}, scrubs {scrubbed!r}"
    )
    assert str(step.get("if", "")).strip() == "always()", (
        "the upload is `if: always()`, so the redaction must be too -- a "
        "failing run is the run with the most attachments to scrub"
    )


def _dir_argument(command: str) -> str | None:
    """The path handed to `--dir`, however the step spells the invocation."""

    tokens = command.split()
    for index, token in enumerate(tokens):
        if token == "--dir" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--dir="):
            return token.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# 2. The mode the workflows call actually reaches the carriers.
# ---------------------------------------------------------------------------

# The redactor is a bash script. On Linux and macOS -- and on the GitHub
# runners that execute both e2e jobs and `make test-static` -- bash is always
# present, so this is a Windows-developer accommodation and not a CI skip.
HAS_BASH = shutil.which("bash") is not None
requires_bash = pytest.mark.skipif(
    not HAS_BASH, reason="bash is not on PATH; scripts/redact_evidence.sh needs it"
)


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REDACTOR), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )


def _report_payload(html: str) -> zipfile.ZipFile:
    match = REPORT_PAYLOAD.search(html)
    assert match is not None, "the miniature report lost its base64 template"
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(match.group(2))))


def _build_miniature_report(directory: Path) -> None:
    """A structural stand-in for what the HTML reporter emits.

    Same three carriers, same shapes: the base64 zip payload, a text
    attachment copied into ``data/``, and a screenshot. The
    ``eyJ``-prefixed literal in the inlined script is not decoration -- the
    shipped ``report.js`` contains exactly one such blob, which is why the
    redactor must not run the replacement list over the reporter's own
    application code.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as payload:
        payload.writestr(
            "abc123.json",
            '{"title":"Navigate to \\"/reset-password?token=' + TOKEN + '\\""}',
        )
    blob = base64.b64encode(buffer.getvalue()).decode("ascii")

    (directory / "data").mkdir(parents=True)
    (directory / "index.html").write_text(
        "<html><script type=\"module\">const asset='eyJ"
        + "A" * 40
        + "';</script>"
        + '<template id="playwrightReportBase64">data:application/zip;base64,'
        + blob
        + "</template></html>",
        encoding="utf-8",
    )
    (directory / "data/context.md").write_text(
        '- textbox "Recovery link": http://127.0.0.1:21997/reset-password'
        f"?token={TOKEN}\n",
        encoding="utf-8",
    )
    (directory / "data/shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + TOKEN.encode())
    (directory / "report.js").write_text(
        "const asset='eyJ" + "B" * 40 + "';\n", encoding="utf-8"
    )

    # A trace zip, which the reporter copies into data/ whenever a test fails.
    # This is the carrier Codex raised as `trace-retention-exfil`, and without
    # it the script's `.zip` branch had ZERO coverage: replacing that branch
    # with a bare `continue` left the whole static suite green while three live
    # carriers went back out. The non-UTF-8 entry is here too, because the
    # branch must copy those through byte-for-byte rather than mangling them.
    trace = io.BytesIO()
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr(
            "test.trace",
            '{"type":"action","title":"Navigate to \\"/reset-password?token='
            + TOKEN
            + '\\""}\n',
        )
        archive.writestr("resources/shot.png", b"\x89PNG\r\n\x1a\n\xff\xfe")
    (directory / "data/5b8e187.zip").write_bytes(trace.getvalue())


@requires_bash
def test_dir_mode_reaches_the_base64_report_payload_a_pipe_cannot_see(tmp_path):
    report = tmp_path / "playwright-report"
    report.mkdir()
    _build_miniature_report(report)
    before = (report / "index.html").read_text(encoding="utf-8")

    # The carrier is invisible to a text scrubber: prove that first, so the
    # assertion below is measuring the base64 path and not a plaintext hit.
    assert TOKEN not in before, (
        "the token must not be plaintext in index.html, or this test is not "
        "exercising the base64 payload at all"
    )

    result = _run("--dir", str(report))

    assert result.returncode == 0, result.stderr
    after = (report / "index.html").read_text(encoding="utf-8")
    entries = _report_payload(after)
    published = b"".join(entries.read(name) for name in entries.namelist())
    assert TOKEN.encode() not in published, (
        "the one-time reset token survived inside the base64-inlined report "
        f"payload: {published!r}"
    )
    assert b'?token=<redacted>' in published

    # The reporter's own application must come through untouched: a JWT-shaped
    # literal in the inlined script is the shape that would be eaten.
    match_before = REPORT_PAYLOAD.search(before)
    match_after = REPORT_PAYLOAD.search(after)
    assert before[: match_before.start(2)] == after[: match_after.start(2)]
    assert before[match_before.end(2) :] == after[match_after.end(2) :]
    assert (report / "report.js").read_text(encoding="utf-8") == (
        "const asset='eyJ" + "B" * 40 + "';\n"
    )

    # The text attachment is reached...
    context = (report / "data/context.md").read_text(encoding="utf-8")
    assert TOKEN not in context
    assert "<redacted>" in context

    # ...and the screenshot is not. Say so out loud rather than implying the
    # directory is clean.
    assert (report / "data/shot.png").read_bytes().endswith(TOKEN.encode()), (
        "text substitution cannot redact rendered pixels; if this ever starts "
        "passing, the script grew an image path and this contract is stale"
    )
    assert ".png" in result.stdout, (
        "the summary must name what it could not reach, so the residual gap "
        f"is visible in the job log. got: {result.stdout!r}"
    )


@requires_bash
def test_the_scrubber_matches_this_lane_s_secret_shapes(tmp_path):
    """S14: the shipped scrubber matched neither ``dtxr_`` nor ``token=``."""

    sample = (
        f"reset link: http://host/reset-password?token={TOKEN}\n"
        f"bare token: {TOKEN}\n"
        "oauth callback: /callback?code=AUTHCODE123&state=OPAQUESTATE456\n"
        "keep this line intact\n"
    )
    result = _run(stdin=sample)

    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout
    assert "AUTHCODE123" not in result.stdout
    assert "OPAQUESTATE456" not in result.stdout
    # Anti-vacuity: the scrubber redacts secrets, it does not empty the stream.
    assert "keep this line intact" in result.stdout


@requires_bash
def test_dir_mode_keeps_the_break_glass_hard_fail(tmp_path):
    """The hard-fail must survive the new mode, and must not publish.

    Stream mode refuses to emit anything. In-place there is no such option, so
    the bytes are scrubbed AND the process exits non-zero: the job goes red
    while the ``if: always()`` upload publishes a safe report.
    """

    report = tmp_path / "playwright-report"
    (report / "data").mkdir(parents=True)
    (report / "data/leak.md").write_text(
        "DTXWIKI_SESSION_SECRET=do-not-commit\n", encoding="utf-8"
    )

    result = _run("--dir", str(report))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "production-secret env name" in result.stderr
    scrubbed = (report / "data/leak.md").read_text(encoding="utf-8")
    assert "DTXWIKI_SESSION_SECRET" not in scrubbed

    # Stream mode's contract is unchanged: refuse, emit nothing.
    piped = _run(stdin="DTXWIKI_SESSION_SECRET=do-not-commit\n")
    assert piped.returncode == 1
    assert "production-secret env name" in piped.stderr
    assert piped.stdout == ""


# ---------------------------------------------------------------------------
# 3. S13 -- the one job that runs the behavioural cases must be able to.
# ---------------------------------------------------------------------------


def test_the_self_runner_job_pins_a_node_that_can_strip_types():
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["frontend"]
    self_runners = [
        step
        for step in job["steps"]
        if "tests/e2e/network_retry.test.mjs" in step.get("run", "")
    ]
    assert self_runners, (
        "the `frontend` job is the only job that executes the e2e helper "
        "self-runners: the `backend` job that runs `make test-static` has no "
        "node, so both tests/static wrappers skip there"
    )

    setups = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-node")
    ]
    assert len(setups) == 1
    spec = setups[0]["with"]["node-version"]

    assert isinstance(spec, str), (
        "quote the node version: YAML reads a bare 22.10 as the float 22.1, "
        f"which requests node 22.1. got {spec!r}"
    )
    parsed = re.match(r"^(\d+)\.(\d+)", spec)
    assert parsed is not None, (
        "the node version must pin a minor, not float on a major: the "
        "self-runners import tests/e2e/*.ts and need unflagged TypeScript "
        f"type stripping (node >= {'.'.join(map(str, MIN_NODE_FOR_TYPE_STRIPPING))}). "
        f"got {spec!r}"
    )
    floor = (int(parsed.group(1)), int(parsed.group(2)))
    assert floor >= MIN_NODE_FOR_TYPE_STRIPPING, (
        f"node {spec} predates unflagged TypeScript type stripping "
        f"(v{MIN_NODE_FOR_TYPE_STRIPPING[0]}.{MIN_NODE_FOR_TYPE_STRIPPING[1]}); "
        "the self-runner step would fail to import tests/e2e/*.ts"
    )


@requires_bash
def test_dir_mode_redacts_the_trace_zip_the_reporter_copies_into_data(tmp_path):
    """The `.zip` branch is the carrier Codex named, and it was untested.

    `retain-on-failure` writes a trace for every failed test and the HTML
    reporter copies it into ``playwright-report/data/<sha>.zip``. Replacing the
    script's ``.zip`` branch with a bare ``continue`` left ``tests/static`` and
    ``tests/packaging`` at 332 passed -- byte-for-byte the unmutated result --
    while the mutant republished three live carriers out of a real failing run.
    """

    report = tmp_path / "playwright-report"
    report.mkdir()
    _build_miniature_report(report)
    archive = report / "data/5b8e187.zip"

    result = _run("--dir", str(report))
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(archive) as rewritten:
        names = rewritten.namelist()
        trace_text = rewritten.read("test.trace").decode("utf-8")
        binary = rewritten.read("resources/shot.png")

    assert names == ["test.trace", "resources/shot.png"], (
        f"the rewrite must preserve entry names and order, got {names}"
    )
    assert TOKEN not in trace_text, (
        "the trace entry still carries the one-time token after --dir: "
        f"{trace_text!r}"
    )
    assert "<redacted>" in trace_text
    assert json.loads(trace_text)["title"], "the rewritten entry must stay valid JSON"
    assert binary == b"\x89PNG\r\n\x1a\n\xff\xfe", (
        "a non-UTF-8 entry must be copied through byte-for-byte, not mangled"
    )


@requires_bash
def test_dir_mode_never_drops_an_attachment_without_naming_it(tmp_path):
    """Every file is redacted, or named as unreachable. Never neither.

    The `.html` and static-asset branches used to `continue` for any file they
    did not recognise, so a test-authored `data/attach.html` kept its token AND
    was absent from the summary -- the operator reading the job log saw a clean
    scan. The distinction is location, not suffix: the reporter writes its own
    chrome at the report root and copies attachments into `data/`.
    """

    report = tmp_path / "playwright-report"
    (report / "data").mkdir(parents=True)
    (report / "data/attach.html").write_text(
        f"<html>?token={TOKEN}</html>", encoding="utf-8"
    )
    (report / "data/diagram.svg").write_text(
        f"<svg>?token={TOKEN}</svg>", encoding="utf-8"
    )
    (report / "data/blob.dat").write_bytes(b"opaque\xff")
    # Reporter chrome at the root: must be left byte-identical, because
    # report.js carries a JWT-shaped blob the `eyJ` rule would eat.
    (report / "report.js").write_text(
        "const a='eyJ" + "A" * 40 + "';\n", encoding="utf-8"
    )
    (report / "icon.svg").write_text("<svg>icon</svg>", encoding="utf-8")
    chrome_before = (report / "report.js").read_bytes()

    result = _run("--dir", str(report))
    assert result.returncode == 0, result.stderr

    for name in ("data/attach.html", "data/diagram.svg"):
        assert TOKEN not in (report / name).read_text(encoding="utf-8"), (
            f"{name} is a test attachment and must be scrubbed"
        )
        assert f"redacted: {name}" in result.stdout, (
            f"{name} was scrubbed but not named in the summary:\n{result.stdout}"
        )

    assert ".dat" in result.stdout, (
        "an unreachable attachment must be NAMED, not silently dropped:\n"
        f"{result.stdout}"
    )
    assert (report / "report.js").read_bytes() == chrome_before, (
        "the reporter's own application code must not be rewritten"
    )
    assert (report / "icon.svg").read_text(encoding="utf-8") == "<svg>icon</svg>", (
        "a reporter asset at the report root is chrome, not an attachment"
    )
