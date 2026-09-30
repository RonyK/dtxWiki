"""E2E-02: the suite may not delete or re-roll its own failure evidence.

Two structural guards plus one execution guard:

1. No spec mutates an already-recorded evidence array. Splicing recorded
   entries out before ``expect(pageErrors).toEqual([])`` turns the gate into a
   formality. The protected arrays are DISCOVERED rather than named: anything a
   browser-event handler pushes into, plus anything a spec asserts empty. The
   first version of this guard hard-coded the single name ``pageErrors``, so
   renaming the array -- or using the one in ``ui_fix_acceptance.spec.ts``,
   which is called ``failures`` -- walked straight past it.
2. No semantic assertion runs inside a ``retryOnceAfterNetworkChange`` closure,
   and every call site declares the request identity it is recovering. A retry
   that wraps an assertion can convert a real failure into a pass on the second
   roll.
3. ``tests/e2e/network_retry.test.mjs`` -- the behavioural regression set for
   both of the above -- actually runs and passes.

WHAT GUARD 2's ASSERTION SCAN CAN AND CANNOT SEE. It reads the retried closure's
text, and then resolves each identifier the closure CALLS against the function
definitions in the same spec and in the sibling ``tests/e2e/*.ts`` helper
modules, repeating to ``HELPER_DEPTH`` levels. That is what catches the shape
this file previously missed: a one-line ``await assertSurfaceRendered(page,
surface)`` in the closure with the ``expect`` one level down in the helper --
which is already the house style in the guarded spec (``expectViewportFits``).

It is still a TRIPWIRE AGAINST THE OBVIOUS SHAPE, NOT A PROOF. It does not
resolve a call made through a variable, an object method, an imported
non-``tests/e2e`` module, a callback passed in as an argument, or anything
built at run time; a helper reached by any of those routes can carry an
assertion into the retried closure and this guard will not see it. Two other
layers, not this one, are what make invariant (b) hold at run time:
``isAssertionFailure`` in ``tests/e2e/network_retry.ts`` refuses to re-roll an
assertion failure however it got there, and ``network_retry.test.mjs`` pins
that behaviour. Do not read a green run of this file as "no assertion can be
retried".
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
SPECS = sorted(E2E_DIR.glob("*.spec.ts"))
HELPER_MODULES = sorted(
    path for path in E2E_DIR.glob("*.ts") if not path.name.endswith(".spec.ts")
)
RETRY_HELPER = "retryOnceAfterNetworkChange("
EXPECTED_RETRY_CALL_SITES = 2
# (page, expected identity/identities, operation) -- see the signature at
# tests/e2e/network_retry.ts. The pre-fix shape was (page, operation): two
# arguments, no declared identity, so ANY failed request licensed the retry.
RETRY_HELPER_ARITY = 3
# `expect(` and `expect.poll(` both. The first version matched only `expect\s*\(`
# and so could not see `expect.poll`, which is exactly what `expectViewportFits`
# is built from.
ASSERTION_CALL = re.compile(r"\bexpect\s*[(.]")
# How many levels of locally-defined helper the assertion scan follows.
HELPER_DEPTH = 3
# Browser-event handlers whose payload is failure evidence.
EVIDENCE_EVENTS = (
    "pageerror",
    "console",
    "requestfailed",
    "response",
    "request",
    "websocket",
    "crash",
)
# Emptiness gates: `expect(<expr>[, message]).toEqual([])` / `.toHaveLength(0)`.
EVIDENCE_GATE = re.compile(
    r"expect\(\s*(?:await\s+)?([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)"
    r"(?![\w$.]|\s*\()[^;]{0,400}?\)\s*\.\s*"
    r"(?:toEqual\(\s*\[\s*\]\s*\)|toStrictEqual\(\s*\[\s*\]\s*\)"
    r"|toHaveLength\(\s*0\s*\))"
)
EVIDENCE_PUSH = re.compile(
    r"([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\.\s*push\s*\("
)
ON_CALL = re.compile(r"\.\s*on\s*\(")
DEFINED_FUNCTION = re.compile(
    r"(?:^|[^\w$.])(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
)
DEFINED_ARROW = re.compile(
    r"(?:^|[^\w$.])(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
    r"(?::[^=;\n]*)?=\s*(?:async\s+)?"
    r"(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=;\n]*)?=>"
)
CALLED_IDENTIFIER = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
CALL_KEYWORDS = frozenset(
    {
        "await",
        "catch",
        "for",
        "function",
        "if",
        "return",
        "switch",
        "typeof",
        "while",
        "new",
    }
)
# Node ships unflagged TypeScript type stripping from v22.18.
MIN_NODE_FOR_TYPE_STRIPPING = (22, 18)


def _strip_comments(source: str) -> str:
    """Blank out comments, preserving every character offset and line number.

    String- and template-literal-aware, so `"https://example"` survives intact.
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


def _matching_paren(source: str, open_index: int) -> int:
    """Index of the `)` closing the `(` at `open_index`, or -1."""
    depth = 0
    index = open_index
    quote: str | None = None
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _call_arguments(source: str, callee: re.Pattern[str]) -> list[tuple[int, str]]:
    """(1-indexed line, argument text) for each call matching `callee`.

    `callee` must match up to and including the call's opening paren.
    """
    calls: list[tuple[int, str]] = []
    for match in re.finditer(callee, source):
        open_paren = source.rindex("(", match.start(), match.end())
        close = _matching_paren(source, open_paren)
        if close == -1:
            continue
        calls.append(
            (
                source.count("\n", 0, match.start()) + 1,
                source[open_paren + 1 : close],
            )
        )
    return calls


def _call_bodies(source: str) -> list[tuple[int, str]]:
    """Yield (1-indexed line, argument text) for each retry-helper call."""
    return _call_arguments(
        _strip_comments(source), re.compile(re.escape(RETRY_HELPER))
    )


def _split_top_level_arguments(body: str) -> list[str]:
    """Split a call's argument text on commas that are not nested."""
    arguments: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            current.append(char)
            if char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    arguments.append("".join(current))
    return [argument for argument in arguments if argument.strip()]


def _defines_a_function(argument: str) -> bool:
    """True when the argument text is itself a function literal.

    Nested brackets are blanked first, so an arrow inside a call the argument
    makes (`identities.map((one) => one.href)`) does not count.
    """
    depth = 0
    top_level: list[str] = []
    for char in argument:
        if char in "([{":
            depth += 1
            top_level.append(" ")
            continue
        if char in ")]}":
            depth -= 1
            top_level.append(" ")
            continue
        top_level.append(char if depth == 0 else " ")
    return re.search(r"=>|\bfunction\b", "".join(top_level)) is not None


def _brace_body(source: str, from_index: int) -> str:
    """Text of the `{...}` block starting at or after `from_index`, else ''."""
    open_brace = source.find("{", from_index)
    if open_brace == -1:
        return ""
    depth = 0
    index = open_brace
    quote: str | None = None
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : index + 1]
        index += 1
    return source[open_brace:]


def _function_definitions(source: str) -> dict[str, str]:
    """name -> body text, for functions and arrow consts defined in `source`."""
    code = _strip_comments(source)
    definitions: dict[str, str] = {}
    for match in DEFINED_FUNCTION.finditer(code):
        open_paren = code.index("(", match.end() - 1)
        close = _matching_paren(code, open_paren)
        if close == -1:
            continue
        definitions.setdefault(match.group(1), _brace_body(code, close))
    for match in DEFINED_ARROW.finditer(code):
        tail = code[match.end() :]
        stripped = tail.lstrip()
        if stripped.startswith("{"):
            definitions.setdefault(match.group(1), _brace_body(tail, 0))
        else:
            # Expression-bodied arrow: everything up to the statement end.
            definitions.setdefault(match.group(1), stripped.split(";")[0])
    return definitions


def _assertion_path(
    body: str,
    definitions: dict[str, str],
    depth: int,
    seen: frozenset[str] = frozenset(),
) -> list[str] | None:
    """The chain of calls by which an `expect` is reachable from `body`."""
    if ASSERTION_CALL.search(body):
        return []
    if depth <= 0:
        return None
    for match in CALLED_IDENTIFIER.finditer(body):
        name = match.group(1)
        if name in CALL_KEYWORDS or name in seen or name not in definitions:
            continue
        nested = _assertion_path(
            definitions[name], definitions, depth - 1, seen | {name}
        )
        if nested is not None:
            return [name, *nested]
    return None


def _evidence_arrays(code: str) -> tuple[set[str], set[str]]:
    """(names pushed to by a browser-event handler, names asserted empty)."""
    recorded: set[str] = set()
    for _, arguments in _call_arguments(code, ON_CALL):
        event = re.match(r"\s*\"([a-z]+)\"", arguments)
        if event is None or event.group(1) not in EVIDENCE_EVENTS:
            continue
        for push in EVIDENCE_PUSH.finditer(arguments):
            recorded.add(re.split(r"\s*\.\s*", push.group(1))[-1])
    gated = {
        re.split(r"\s*\.\s*", match.group(1))[-1]
        for match in EVIDENCE_GATE.finditer(code)
    }
    return recorded, gated


def test_e2e_specs_never_mutate_recorded_pageerror_evidence():
    recorded: set[str] = set()
    gated: set[str] = set()
    per_spec: dict[Path, tuple[str, set[str]]] = {}
    for spec in SPECS:
        code = _strip_comments(spec.read_text(encoding="utf-8"))
        spec_recorded, spec_gated = _evidence_arrays(code)
        recorded |= spec_recorded
        gated |= spec_gated
        per_spec[spec] = (code, spec_recorded | spec_gated)

    # Anti-vacuity: both discovery routes must still find evidence arrays. If a
    # scanner silently stops matching, this guard would otherwise protect the
    # empty set and pass forever.
    assert recorded, (
        "no browser-event handler in tests/e2e/*.spec.ts was found recording "
        "evidence; the handler scanner has stopped matching"
    )
    assert gated, (
        "no spec was found asserting an evidence array empty; the gate scanner "
        "has stopped matching"
    )

    offenders: list[str] = []
    for spec, (code, protected) in per_spec.items():
        for name in protected:
            escaped = re.escape(name)
            # `\s*` rather than a per-line scan: `pageErrors\n  .splice(` is the
            # same deletion.
            for match in re.finditer(
                rf"\b{escaped}\s*\.\s*(?:splice|shift|pop)\s*\("
                rf"|\b{escaped}\s*\.\s*length\s*=(?!=)"
                rf"|\b{escaped}\s*=(?!=)",
                code,
            ):
                declaration = re.search(
                    rf"\b(?:const|let|var)\s+{escaped}\s*$",
                    code[max(0, match.start() - 40) : match.start() + len(name)],
                )
                if declaration:
                    continue
                offenders.append(
                    f"{spec.name}:{code.count(chr(10), 0, match.start()) + 1} "
                    f"({name})"
                )

    assert offenders == [], (
        "these lines delete already-recorded failure evidence before the "
        "emptiness gate can see it (the protected arrays are the ones a "
        f"browser-event handler records into or a spec asserts empty): "
        f"{offenders}"
    )


def test_no_semantic_assertion_runs_inside_a_network_retry_wrapper():
    shared_definitions: dict[str, str] = {}
    for module in HELPER_MODULES:
        shared_definitions.update(_function_definitions(module.read_text("utf-8")))

    call_sites: list[tuple[str, int, str, dict[str, str]]] = []
    for spec in SPECS:
        source = spec.read_text(encoding="utf-8")
        definitions = {**shared_definitions, **_function_definitions(source)}
        for line, body in _call_bodies(source):
            call_sites.append((spec.name, line, body, definitions))

    # Guard against a silently vacuous pass if the helper is renamed or the
    # scanner stops matching.
    assert len(call_sites) == EXPECTED_RETRY_CALL_SITES, (
        "expected exactly "
        f"{EXPECTED_RETRY_CALL_SITES} {RETRY_HELPER} call sites, found "
        f"{[(name, line) for name, line, _, _ in call_sites]}"
    )

    nested_assertions = []
    for name, line, body, definitions in call_sites:
        path = _assertion_path(body, definitions, HELPER_DEPTH)
        if path is None:
            continue
        via = " -> ".join(path) if path else "directly"
        nested_assertions.append(f"{name}:{line} (via {via})")
    assert nested_assertions == [], (
        "a retry must not be able to re-roll a verdict: move these assertions "
        f"outside the retried closure: {nested_assertions}"
    )

    # Arity, not the spelling of the first argument. The pre-fix call was
    # `retryOnceAfterNetworkChange(page, async () => {...})`, and a guard that
    # matched the literal identifier `page` was evaded by renaming it to
    # `ownerPage` -- a name workspace_v3_acceptance.spec.ts already uses.
    undeclared = []
    for name, line, body, _ in call_sites:
        arguments = _split_top_level_arguments(body)
        # Arity alone would still pass `(page, async () => {...}, undefined)`,
        # so the operation must also be the LAST argument. Only a top-level
        # function literal counts: `identities.map((one) => ...)` as the
        # declared identity is legitimate.
        operation_first = any(
            _defines_a_function(argument)
            for argument in arguments[: RETRY_HELPER_ARITY - 1]
        )
        if len(arguments) != RETRY_HELPER_ARITY or operation_first:
            undeclared.append(
                f"{name}:{line} ({len(arguments)} argument(s): "
                f"{[argument.strip()[:40] for argument in arguments]})"
            )
    assert undeclared == [], (
        "every call site must pass the "
        f"{RETRY_HELPER_ARITY}-argument (page, identity, operation) form and "
        "so declare the request identity it is recovering, otherwise any "
        f"failed request licenses the retry: {undeclared}"
    )


def test_network_retry_self_runner_passes():
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node is not installed; run `node tests/e2e/network_retry.test.mjs`"
        )
    if not (ROOT / "node_modules/@playwright/test").is_dir():
        # network_retry.test.mjs imports tests/e2e/fixtures.ts, which imports
        # @playwright/test, so this needs a real `npm ci`. Skipping keeps
        # `make test-static` usable in a Python-only checkout, exactly like
        # test_redact_network_self_runner_passes; .github/workflows/ci.yml runs
        # the self-runner in the `frontend` job, which has node_modules.
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
        [node, "network_retry.test.mjs"],
        cwd=E2E_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    assert completed.returncode == 0, (
        "tests/e2e/network_retry.test.mjs failed:\n"
        + completed.stdout
        + completed.stderr
    )
