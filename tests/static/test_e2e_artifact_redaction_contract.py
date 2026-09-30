"""E2E-03: what the e2e harness is allowed to write into a CI artifact.

The Playwright HTML reporter copies every attachment body and trace file into
``playwright-report/data/`` (node_modules/playwright/lib/reporters/html.js), and
both ``.github/workflows/ci.yml`` and ``.github/workflows/e2e-browsers.yml``
upload that directory with ``actions/upload-artifact``. So anything the fixture
retains about a failed request is published for 14 days, and the suite itself
drives ``/reset-password?token=<one-time token>``.

Four structural guards plus one execution guard:

1. No raw request URL is retained anywhere in the e2e helper modules, and none
   reaches a ``testInfo.attach`` body. This is a dataflow check over the WHOLE
   file, not a denylist of one variable name: the first version banned the
   literal identifier ``failedUrls`` and ran its ``request.url()`` check over
   ``fixtures.split("export const test")[1].split("});")[0]`` -- a slice that
   stops BEFORE the attach call -- so the shipped defect was reintroduced under
   a different name, in a second attachment, with all four guards green.
2. Every recovery control the fixture may click is addressed by a stable test
   id that exists on the element that performs the recovery -- not by a
   presentation class that happened to match two of the four.
3. The trace mode stays failure-gated. A Playwright trace records request URLs
   and POST bodies verbatim with no redaction hook, so ``trace: "on"`` would
   publish one for every green test as well.
4. No route puts a secret in a path segment. ``redact-network.ts`` retains
   ``pathname`` verbatim, and says in a comment that this is safe because every
   credential-bearing route carries its credential in the query string. That
   comment is a claim about the ROUTE TABLE, which is elsewhere; this guard is
   the tie that makes it checked rather than asserted. The redactor's own
   ``CREDENTIAL_PREFIXES`` scrubber is a net under this, not a replacement for
   it: it catches a segment that starts with a prefix this app mints, and
   nothing else.
5. ``tests/e2e/redact-network.test.mjs`` -- the behavioural regression set for
   guard 1 and for the one-click-per-test bound -- actually runs and passes.

WHAT GUARD 1 CANNOT SEE. It resolves a raw URL to the sink that consumes it and
to the names it is bound to within the enclosing block. It does not follow a
raw URL through a function's RETURN value, through a property of an object
literal that is then returned, or into another module. It also covers only the
two helper modules named in ``REDACTION_SOURCES``: in particular
``tests/e2e/ui_fix_acceptance.spec.ts`` retains ``request.url()`` and
``response.url()`` in its own ``saveJson`` attachment and is NOT covered here
(reviewed as S7, owned separately). Guard 1 is what makes the fixture's
redaction contract mechanical; it is not a proof that no URL can reach an
artifact by any route.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = ROOT / "tests/e2e"
FIXTURES = E2E_DIR / "fixtures.ts"
NETWORK_RETRY = E2E_DIR / "network_retry.ts"
REDACTOR = E2E_DIR / "redact-network.ts"
APP_TSX = ROOT / "frontend/src/app/App.tsx"
SHELL_TSX = ROOT / "frontend/src/shared/components/Shell.tsx"
BACKEND_DIR = ROOT / "backend"
PLAYWRIGHT_CONFIG = ROOT / "playwright.config.ts"
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/e2e-browsers.yml",
)

# The helper modules whose retention this contract covers. Both feed the
# fixture's published attachment.
REDACTION_SOURCES = (FIXTURES, NETWORK_RETRY)

# id -> the file that must own the element carrying it.
EXPECTED_RECOVERY_IDS = {
    "session-verify-retry": APP_TSX,
    "workspace-access-retry": APP_TSX,
    "route-error-reload": APP_TSX,
    "workspace-resolution-retry": SHELL_TSX,
}

# A trace records URLs and POST bodies with no redaction hook. These modes only
# write one for a test that already failed; "on" and "on-all-retries" write one
# for every test in the run.
#
# This bounds trace VOLUME, not secret exposure, and this comment used to imply
# otherwise. The HTML reporter base64-inlines every step title into
# playwright-report/index.html on every run including green ones, so a
# `page.goto("/reset-password?token=...")` title is published regardless of what
# this set allows -- reproduced with trace:"off" on a passing test. Keeping "on"
# and "on-all-retries" out is still worth doing; it is not what makes the
# published report safe.
FAILURE_GATED_TRACE_MODES = {
    "retain-on-failure",
    "on-first-retry",
    "retain-on-first-failure",
    "off",
}
TRACE_SETTING = re.compile(r"^\s*trace:\s*\"([a-z-]+)\",\s*$", re.MULTILINE)

# --- guard 1 -------------------------------------------------------------
# `request.url()` / `response.url()`: the raw, query-bearing string.
RAW_URL_ACCESS = re.compile(r"\.\s*url\s*\(\s*\)")
# The only calls allowed to consume one. Each drops the query by construction:
# `sanitizeUrl` and `requestIdentity` rebuild from `new URL(...).origin +
# .pathname`, and `record` / `redactFailedRequest` go through
# redact-network.ts. A new laundering function has to be added here
# deliberately, which is the review point this guard exists to create.
LAUNDERING_SINKS = {
    "sanitizeUrl()": r"\bsanitizeUrl\s*\(",
    "recordNetworkChange()": r"\brecordNetworkChange\s*\(",
    "requestIdentity()": r"\brequestIdentity\s*\(",
    "redactFailedRequest()": r"\bredactFailedRequest\s*\(",
    ".record()": r"\.\s*record\s*\(",
}
ATTACH_CALL = re.compile(r"\btestInfo\s*\.\s*attach\s*\(")

# --- guard 4 -------------------------------------------------------------
FRONTEND_ROUTE = re.compile(r'path="([^"]+)"')
BACKEND_ROUTE = re.compile(
    r"@(?:app|router)\.(?:get|post|put|patch|delete|head|options)\("
    r"\s*(?:path\s*=\s*)?[\"']([^\"']+)[\"']"
)
FRONTEND_PARAM = re.compile(r":([A-Za-z0-9_]+)")
BACKEND_PARAM = re.compile(r"\{([^}:]+)")
# A path parameter whose name says it carries credential material.
SECRET_SHAPED_PARAM = re.compile(
    r"token|secret|password|passwd|credential|api_?key|nonce|signature"
    r"|otp|jwt|\bpat\b|bearer",
    re.IGNORECASE,
)
# Secret-SHAPED names that are verified not to be secrets. Each needs a reason,
# and each must still exist in the route table (asserted below) so a stale
# exemption cannot outlive the route it was written for.
ALLOWED_SECRET_SHAPED_PARAMS = {
    # backend/api/auth.py:639 -- `token_id: Annotated[UUID, Path()]`. The row id
    # of an API-token record, not the token: the secret is shown once at
    # creation and stored only as a hash (backend/services/credential_hashing.py).
    "token_id": "opaque UUID row id of an API-token record, never the token",
}
# The redactor keeps `pathname`; that is what makes guard 4 load-bearing.
PATHNAME_RETAINED = re.compile(r"pathname\s*=\s*[^;\n]*parsed\s*\.\s*pathname")

# Node ships unflagged TypeScript type stripping from v22.18.
MIN_NODE_FOR_TYPE_STRIPPING = (22, 18)


def _strip_comments(source: str) -> str:
    """Blank out comments, preserving every character offset and line number.

    String-aware, so a guard cannot match its own rationale and a URL inside a
    string literal is not truncated at its `//`.
    """
    out = list(source)
    index = 0
    length = len(source)
    quote: str | None = None
    while index < length:
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
            for position in range(index, end):
                out[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end == -1 else end + 2
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue
        index += 1
    return "".join(out)


def _code_skeleton(source: str) -> str:
    """Comments blanked and `'...'` / `"..."` contents blanked, offsets kept.

    Braces and parens then balance, so the structural scans below can match
    them without a parser. Template literals are left intact: their `${}` are
    balanced, and blanking them would hide code written inside an
    interpolation.
    """
    code = _strip_comments(source)
    out = list(code)
    index = 0
    while index < len(code):
        char = code[index]
        if char in "\"'":
            index += 1
            while index < len(code) and code[index] != char:
                if code[index] == "\\":
                    out[index] = " "
                    index += 1
                if index < len(code) and code[index] != "\n":
                    out[index] = " "
                index += 1
        index += 1
    return "".join(out)


def _matching(code: str, open_index: int, opener: str, closer: str) -> int:
    depth = 0
    for index in range(open_index, len(code)):
        if code[index] == opener:
            depth += 1
        elif code[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _call_spans(code: str, callee: re.Pattern[str]) -> list[tuple[int, int]]:
    """(first index inside the parens, index of the closing paren) per call."""
    spans: list[tuple[int, int]] = []
    for match in re.finditer(callee, code):
        open_paren = code.rindex("(", match.start(), match.end())
        close = _matching(code, open_paren, "(", ")")
        if close != -1:
            spans.append((open_paren + 1, close))
    return spans


def _enclosing_block(code: str, index: int) -> tuple[int, int]:
    """The innermost `{...}` containing `index`, else the whole file."""
    depth = 0
    for position in range(index, -1, -1):
        if code[position] == "}":
            depth += 1
        elif code[position] == "{":
            if depth == 0:
                end = _matching(code, position, "{", "}")
                return (position, len(code) if end == -1 else end)
            depth -= 1
    return (0, len(code))


def _statement_bounds(code: str, index: int) -> tuple[int, int]:
    start = max(code.rfind(char, 0, index) for char in ";{}") + 1
    end = code.find(";", index)
    return (start, len(code) if end == -1 else end)


def _line(code: str, index: int) -> int:
    return code.count("\n", 0, index) + 1


def _raw_url_flow(path: Path) -> tuple[int, set[str], list[str]]:
    """(raw-URL reads, names bound to one, offenders) for one helper module."""
    code = _code_skeleton(path.read_text(encoding="utf-8"))
    sinks = [
        span
        for pattern in LAUNDERING_SINKS.values()
        for span in _call_spans(code, re.compile(pattern))
    ]

    def laundered(index: int) -> bool:
        return any(start <= index < end for start, end in sinks)

    reads = 0
    bound: dict[str, tuple[int, int, int]] = {}
    offenders: list[str] = []
    for match in RAW_URL_ACCESS.finditer(code):
        reads += 1
        if laundered(match.start()):
            continue
        start, end = _statement_bounds(code, match.start())
        declaration = re.match(
            r"\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=",
            code[start:end],
        )
        if declaration is None:
            offenders.append(
                f"{path.name}:{_line(code, match.start())} retains a raw URL "
                f"({code[start:end].strip()[:70]!r}) without handing it to any "
                "of " + ", ".join(sorted(LAUNDERING_SINKS))
            )
            continue
        bound[declaration.group(1)] = (start, end, match.start())

    for name, (start, end, index) in bound.items():
        block_start, block_end = _enclosing_block(code, index)
        for use in re.finditer(rf"\b{re.escape(name)}\b", code[block_start:block_end]):
            position = block_start + use.start()
            if start <= position < end or laundered(position):
                continue
            offenders.append(
                f"{path.name}:{_line(code, position)} uses the raw URL bound to "
                f"`{name}` outside a redaction sink"
            )
    return reads, set(bound), offenders


def _recovery_ids_declared_by_the_fixture() -> list[str]:
    source = FIXTURES.read_text(encoding="utf-8")
    match = re.search(
        r"export const RECOVERY_TEST_IDS = \[(.*?)\]", source, re.DOTALL
    )
    assert match is not None, (
        "tests/e2e/fixtures.ts must declare RECOVERY_TEST_IDS; the fixture "
        "cannot be kept in sync with the frontend without it"
    )
    return re.findall(r"\"([a-z0-9-]+)\"", match.group(1))


def test_the_e2e_fixture_records_only_redacted_failed_requests():
    fixtures = _strip_comments(FIXTURES.read_text(encoding="utf-8"))

    assert "redact-network.ts" in fixtures
    assert "createFailedRequestLog(" in fixtures
    # The historical name of the raw-URL array. Kept as a cheap tripwire; the
    # dataflow guard below is what actually enforces this, under any name.
    assert "failedUrls" not in fixtures, (
        "the raw-URL array is what put a one-time reset token into the "
        "network-change-requests attachment"
    )
    assert "recorder.report()" in fixtures, (
        "the attachment body must come from the redaction log, not from any "
        "other collection of failed requests"
    )


def test_no_raw_request_url_is_retained_or_reaches_a_published_attachment():
    total_reads = 0
    offenders: list[str] = []
    attachments: list[tuple[Path, int, str]] = []
    for path in REDACTION_SOURCES:
        reads, bound, module_offenders = _raw_url_flow(path)
        total_reads += reads
        offenders.extend(module_offenders)

        code = _code_skeleton(path.read_text(encoding="utf-8"))
        for start, end in _call_spans(code, ATTACH_CALL):
            arguments = code[start:end]
            attachments.append((path, _line(code, start), arguments))
            reached = sorted(
                name
                for name in bound
                if re.search(rf"\b{re.escape(name)}\b", arguments)
            )
            if RAW_URL_ACCESS.search(arguments):
                reached.append("an inline .url() call")
            if reached:
                offenders.append(
                    f"{path.name}:{_line(code, start)} attaches raw URL "
                    f"material ({', '.join(reached)}) to a published artifact"
                )

    # Anti-vacuity: both halves of this guard must still have something to
    # look at. A renamed accessor or a moved attachment would otherwise leave
    # it passing over nothing.
    assert total_reads > 0, (
        "no raw URL read was found in "
        f"{[str(path.relative_to(ROOT)) for path in REDACTION_SOURCES]}; "
        "the scanner has stopped matching and this guard proves nothing"
    )
    assert any(path == FIXTURES for path, _, _ in attachments), (
        "tests/e2e/fixtures.ts no longer attaches anything the reporter would "
        "publish; re-read this guard's reasoning before relaxing it"
    )

    assert offenders == [], (
        "a failed request may leave behind only the redacted (method, origin, "
        "pathname, errorText) tuple, because the attachment is copied into "
        f"playwright-report/data/ and uploaded for 14 days: {offenders}"
    )


def test_every_route_recovery_control_is_reachable_from_the_e2e_fixture():
    fixtures = FIXTURES.read_text(encoding="utf-8")
    declared = _recovery_ids_declared_by_the_fixture()

    assert declared, "RECOVERY_TEST_IDS must not be empty"
    assert set(declared) == set(EXPECTED_RECOVERY_IDS), (
        "the fixture's recovery ids drifted from the controls this contract "
        f"covers: {sorted(declared)}"
    )

    missing = [
        f"{test_id} (expected in {owner.relative_to(ROOT)})"
        for test_id, owner in EXPECTED_RECOVERY_IDS.items()
        if f'data-testid="{test_id}"' not in owner.read_text(encoding="utf-8")
    ]
    assert missing == [], (
        "the fixture would wait 2s and then silently give up on these "
        f"controls: {missing}"
    )

    code = _strip_comments(fixtures)
    assert "button.primary" not in code, (
        "a styling class is not a contract: `button.primary` matched "
        "RootRouteError and the workspace-resolution card, and silently "
        "missed both `icon-button text-button` retry banners"
    )

    # The declared ids only mean something if the locator is actually built
    # from them -- otherwise the list drifts into decoration.
    recover = code.split("async function recoverFromNetworkChange")[-1]
    locator = re.search(r"\.locator\((.*?)\)\s*\n", recover, re.DOTALL)
    assert locator is not None, "recoverFromNetworkChange must build a locator"
    assert "RECOVERY_TEST_IDS" in locator.group(1), (
        "the recovery locator must be derived from RECOVERY_TEST_IDS, not "
        f"hand-written: {locator.group(1).strip()}"
    )
    assert "data-testid" in locator.group(1)


def test_playwright_traces_stay_failure_gated_while_the_report_is_uploaded():
    config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    modes = TRACE_SETTING.findall(config)

    assert modes, "playwright.config.ts must set an explicit trace mode"
    offenders = [mode for mode in modes if mode not in FAILURE_GATED_TRACE_MODES]
    assert offenders == [], (
        "a Playwright trace records request URLs and POST bodies verbatim and "
        "has no redaction hook, and playwright-report is uploaded by "
        f"{[str(path.relative_to(ROOT)) for path in WORKFLOWS]}; these modes "
        f"would write one for every test in the run: {offenders}"
    )

    # Anti-vacuity: the guard only means something while the report is
    # actually published.
    uploaded = [
        path
        for path in WORKFLOWS
        if "playwright-report" in path.read_text(encoding="utf-8")
    ]
    assert uploaded, (
        "no workflow publishes playwright-report any more; re-read this "
        "guard's reasoning before relaxing it"
    )


def test_no_route_puts_a_secret_in_a_path_segment():
    """Tie redact-network.ts's retained `pathname` to the actual route table.

    The redactor drops the query and the fragment but keeps the path verbatim,
    and says in a comment that this is safe because every credential-bearing
    route in this app carries its credential in the query string
    (`/reset-password?token=`, `/verify-email?token=`). That comment is a claim
    about routes, and until this guard existed nothing checked it: a future
    `/reset-password/:token` would have published a one-time token into
    `playwright-report/data/` with every static guard green.
    """
    frontend_routes = FRONTEND_ROUTE.findall(APP_TSX.read_text(encoding="utf-8"))
    backend_routes: list[tuple[Path, str]] = []
    for module in sorted(BACKEND_DIR.rglob("*.py")):
        backend_routes.extend(
            (module, route)
            for route in BACKEND_ROUTE.findall(module.read_text(encoding="utf-8"))
        )

    # Anti-vacuity: a guard over an empty route table proves nothing, and this
    # one is only load-bearing while the redactor still keeps the path.
    assert len(frontend_routes) > 10, (
        f"only {len(frontend_routes)} routes found in {APP_TSX.relative_to(ROOT)}; "
        "the route scanner has stopped matching"
    )
    assert len(backend_routes) > 50, (
        f"only {len(backend_routes)} routes found under backend/; the route "
        "scanner has stopped matching"
    )
    redactor = REDACTOR.read_text(encoding="utf-8")
    assert PATHNAME_RETAINED.search(redactor), (
        f"{REDACTOR.relative_to(ROOT)} no longer retains the pathname; this "
        "guard exists only to check the precondition that made retaining it "
        "safe, so re-read it before deleting either"
    )

    parameters: list[tuple[str, str]] = [
        (parameter, f"{APP_TSX.relative_to(ROOT)} {route}")
        for route in frontend_routes
        for parameter in FRONTEND_PARAM.findall(route)
    ] + [
        (parameter, f"{module.relative_to(ROOT)} {route}")
        for module, route in backend_routes
        for parameter in BACKEND_PARAM.findall(route)
    ]

    known = {parameter for parameter, _ in parameters}
    stale = sorted(set(ALLOWED_SECRET_SHAPED_PARAMS) - known)
    assert stale == [], (
        "these exemptions no longer name a real path parameter; delete them "
        f"rather than leaving a blanket exemption behind: {stale}"
    )

    offenders = sorted(
        {
            f"{parameter} ({where})"
            for parameter, where in parameters
            if SECRET_SHAPED_PARAM.search(parameter)
            and parameter not in ALLOWED_SECRET_SHAPED_PARAMS
        }
    )
    assert offenders == [], (
        "tests/e2e/redact-network.ts retains the pathname of every failed "
        "request and publishes it in playwright-report/data/, on the stated "
        "precondition that no route carries a secret in a path segment. These "
        "path parameters look like they break it -- either they do, and the "
        "redactor needs to scrub the segment, or they do not, and they belong "
        f"in ALLOWED_SECRET_SHAPED_PARAMS with the reason: {offenders}"
    )


def test_redact_network_self_runner_passes():
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node is not installed; run `node tests/e2e/redact-network.test.mjs`"
        )
    if not (ROOT / "node_modules/@playwright/test").is_dir():
        # tests/e2e/fixtures.ts imports @playwright/test, so this needs a real
        # `npm ci`. Skipping keeps `make test-static` usable in a Python-only
        # checkout; .github/workflows/ci.yml runs the self-runner in the
        # `frontend` job, which has both node 22 and node_modules.
        pytest.skip("node_modules/@playwright/test is absent; run `npm ci`")

    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    parsed = re.match(r"v(\d+)\.(\d+)", version)
    if parsed is None or (
        int(parsed.group(1)),
        int(parsed.group(2)),
    ) < MIN_NODE_FOR_TYPE_STRIPPING:
        pytest.skip(
            f"node {version} predates unflagged TypeScript type stripping "
            f"(v{MIN_NODE_FOR_TYPE_STRIPPING[0]}.{MIN_NODE_FOR_TYPE_STRIPPING[1]})"
        )

    completed = subprocess.run(
        [node, "redact-network.test.mjs"],
        cwd=E2E_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    assert completed.returncode == 0, (
        "tests/e2e/redact-network.test.mjs failed:\n"
        + completed.stdout
        + completed.stderr
    )
