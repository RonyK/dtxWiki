#!/usr/bin/env bash
# Redactor for CI evidence artifacts.
#
#   redact_evidence.sh                stdin -> stdout. Unchanged behaviour: the
#                                     `packaging` job pipes each captured .log
#                                     through it before upload-artifact.
#   redact_evidence.sh --dir <path>   in-place redaction of a directory tree,
#                                     for the Playwright report the two e2e
#                                     jobs upload. See DIRECTORY MODE below.
#
# Both modes hard-fail (exit 1) when a production secret environment-variable
# name or a PEM private-key body appears in the evidence.
#
# DIRECTORY MODE — what it reaches and what it does not.
#
# A Playwright report is not a log file, so a stdin->stdout pipe over `*.log`
# does not cover it. The report is a directory of mixed content and each kind
# needs its own treatment:
#
#   REACHED
#     * text attachments and copies (.md .txt .log .json .jsonl .csv .yml
#       .yaml .xml .har and the trace-internal .trace/.network/.stacks) are
#       read as UTF-8 and rewritten through the replacement list.
#     * index.html — the HTML reporter appends the ENTIRE report as a
#       base64-encoded zip inside `<template id="playwrightReportBase64">`
#       (node_modules/playwright/lib/reporters/html.js:335). Every step title,
#       including `page.goto("/reset-password#token=...")` (and the legacy
#       `?token=` form), is in there on EVERY run, passing runs included. A
#       fragment never reaches the server, but it is in the step title all the
#       same. That payload is decoded, each zip
#       entry that is valid UTF-8 is redacted, and the zip is rebuilt and
#       re-encoded. This is the carrier a text pipe cannot see: the token is
#       not plaintext anywhere in the file.
#     * .zip files (trace zips, and the copies the reporter drops in
#       playwright-report/data/) — same entry-by-entry treatment. Entries that
#       are not valid UTF-8 (trace resource bodies, screenshots inside the
#       trace) are copied through byte-for-byte.
#
#   NOT REACHED — and deliberately so
#     * Rendered pixels. A .png/.jpeg screenshot or a .webm video that shows a
#       secret on screen still shows it after this runs; text substitution
#       cannot touch an image. Those files are counted and named in the
#       summary this script prints, so the gap is visible in the job log
#       rather than assumed away. Masking the control that renders the secret
#       is the fix for that carrier, not this script.
#     * The part of index.html outside the report payload, and the reporter's
#       own .js/.css/.map/.svg/.ttf/.woff assets. Those are Playwright's built
#       application, they carry no test data, and running the replacement list
#       over them corrupts the report: the shipped report.js contains one
#       base64 blob that matches the JWT pattern.
set -euo pipefail

MODE="--stream"
TARGET=""
case "${1:-}" in
    "")
        ;;
    --dir)
        MODE="--dir"
        TARGET="${2:-}"
        if [ -z "$TARGET" ]; then
            echo "ERROR: --dir requires a directory path" >&2
            exit 2
        fi
        ;;
    *)
        echo "ERROR: unknown argument: ${1}" >&2
        exit 2
        ;;
esac

# Sprint_5 Phase_0 fix-round (XPLAT-2): Windows + some minimal Linux images
# ship only `python` (not `python3`). Probe with an actual --version probe so
# Microsoft Store stubs (which `command -v` resolves but which print "Python
# was not found" and exit non-zero) are skipped rather than silently emitting
# an unredacted stream.
PY_CMD=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" --version >/dev/null 2>&1; then
        PY_CMD="$candidate"
        break
    fi
done
if [ -z "$PY_CMD" ]; then
    echo "ERROR: redact_evidence.sh requires python3 or python on PATH" >&2
    exit 1
fi

if [ "$MODE" = "--stream" ]; then
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    cat >"$tmp"
    TARGET="$tmp"
fi

"$PY_CMD" - "$MODE" "$TARGET" <<'PY'
from pathlib import Path
import base64
import io
import re
import sys
import zipfile

# Sprint_5 Phase_2 fix-round-2 pinned UTF-8 on the *read* only. On Windows the
# write side still took the ANSI codepage, so a single non-ASCII evidence line
# (a Korean log message) raised UnicodeEncodeError, `set -euo pipefail` killed
# the script, and the caller received an empty stream -- a redactor that
# silently produces nothing is worse than one that produces too much. The .ps1
# sibling already forces BOM-less UTF-8 on every stream (XPLAT-NEW-2); this is
# the same pin. `newline=""` keeps the bytes identical to the .ps1 output
# instead of translating LF to CRLF.
sys.stdout.reconfigure(encoding="utf-8", newline="")

hard_fail = re.compile(
    r"DTXWIKI_SESSION_SECRET|"
    r"DTXWIKI_OAUTH_JWT_PRIVATE_KEY_(?:PEM|PATH)|"
    r"DTXWIKI_METRICS_BEARER_TOKEN|"
    r"DTXWIKI_SMTP_PASSWORD"
)

# Sprint_5 Phase_0~2 fix-round (SEC-3): PEM private-key bodies must never
# reach an evidence stream. Detect the BEGIN/END envelope and hard-fail
# rather than redact in place — a leaked private key in evidence is a
# break-glass incident, not a sanitization scenario.
pem_block = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

replacements = [
    # E2E-03 follow-up (S14/S1): credential-bearing query and fragment
    # parameters. The e2e suite drives `/reset-password#token=<one-time
    # token>` -- recovery links carry the token in the fragment since M-2 --
    # and still opens one legacy `?token=` link, and Playwright records both
    # verbatim in the step titles it publishes. The `#` anchor is what reaches
    # the fragment form; tests/packaging/test_evidence_redaction.py pins both.
    # Anchored on `?`, `&` or `#` so this only ever fires on a real query
    # string or fragment -- an unanchored `token=` matches minified
    # JavaScript and corrupts the report it is meant to sanitize.
    #
    # Runs FIRST so a value that a later rule would also match (`dtxr_`, a
    # JWT) collapses to one marker instead of two. `<`, `>`, `"`, `'` and `\`
    # are excluded from the value so the rule is idempotent (it will not
    # re-consume `<redacted>`) and so a JSON-escaped `\"` terminator keeps its
    # backslash -- eating that backslash produces invalid JSON, which is
    # exactly the Bot-B7 defect below in another shape. `#` is excluded for a
    # different reason: without it `?code=abc#state=xyz` consumed the fragment
    # into the redaction, so `#state=xyz` vanished instead of being redacted as
    # its own parameter -- diagnostic loss, and a fragment carrying a DIFFERENT
    # secret would have been silently swallowed rather than marked.
    (
        r"([?&#](?:access_token|refresh_token|id_token|code_verifier"
        r"|token|code|state|password|secret)=)[^&#\s\"'<>\\]+",
        r"\1<redacted>",
    ),
    (r"Bearer (?!resource_metadata\b)[A-Za-z0-9._\-]+", "Bearer <redacted>"),
    (r"\bdtxw_[A-Za-z0-9._\-]+", "dtxw_<redacted>"),
    # E2E-03 follow-up (S14): `dtxr_` is the one-time password-reset token
    # prefix (`tests/e2e/workspace_v3_acceptance.spec.ts` asserts /^dtxr_1_/).
    # The `dtxw_` PAT rule above never matched it.
    (r"\bdtxr_[A-Za-z0-9._\-]+", "dtxr_<redacted>"),
    (r"(Authorization: )[^\r\n]+", r"\1<redacted>"),
    (r"(Cookie|Set-Cookie): [^\r\n]+", r"\1: <redacted>"),
    (r"(Mcp-Session-Id: )[A-Za-z0-9._\-]+", r"\1<redacted>"),
    # Sprint_5 Phase_2 fix-round-3 (Bot-B7): the replacement string must be a
    # single-quoted raw literal so the closing ``"`` is emitted verbatim. The
    # earlier double-quoted form ``r"\1<redacted>\""`` embeds the Python
    # source-level escape ``\"`` (literal backslash + quote), which ``re.sub``
    # treats as an unknown group reference and copies through to the output,
    # producing ``"<redacted>\"}`` — invalid JSON.
    (
        r'("(?:access_token|refresh_token|client_secret|code|code_verifier)": ")[^"]+"',
        r'\1<redacted>"',
    ),
    (r"eyJ[A-Za-z0-9._\-]{20,}", "<jwt-redacted>"),
    (r"/home/[a-zA-Z0-9._\-]+", "~"),
    (r"/Users/[a-zA-Z0-9._\-]+", "~"),
    # Sprint_5 Phase_2 fix-round-2 (XPLAT-NEW-1, Sonnet Xplat 260517-1123):
    # Windows path matching must cover BOTH literal (C:\Users\foo, common
    # in evidence) AND JSON-escaped (C:\\Users\\foo, common in pytest /
    # SDK exception text). Previously this script matched only the
    # JSON-escaped form (r-string r"C:\\\\Users\\\\..." -> regex engine
    # sees \\ twice -> 2 backslashes), while .ps1 matched only the
    # literal form. Now both scripts match both forms identically.
    (r"C:\\Users\\[a-zA-Z0-9._\-]+", "~"),
    (r"C:\\\\Users\\\\[a-zA-Z0-9._\-]+", "~"),
]


def redact(text: str) -> str:
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    return text


mode, target = sys.argv[1], sys.argv[2]

if mode == "--stream":
    text = Path(target).read_text(encoding="utf-8", errors="ignore")
    if hard_fail.search(text):
        print("ERROR: production-secret env name in evidence stream", file=sys.stderr)
        raise SystemExit(1)
    if pem_block.search(text):
        print("ERROR: PEM private key body in evidence stream", file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.write(redact(text))
    raise SystemExit(0)

# ---------------------------------------------------------------------------
# --dir: in-place redaction of an artifact directory (the Playwright report).
# ---------------------------------------------------------------------------

TEXT_SUFFIXES = {
    ".md", ".txt", ".log", ".json", ".jsonl", ".csv", ".yml", ".yaml",
    ".xml", ".har", ".trace", ".network", ".stacks",
    # .svg is text. It is also in STATIC_SUFFIXES, because the reporter ships
    # its own icons -- those are excluded by _is_reporter_asset (report root),
    # so what reaches here is a test-authored SVG attachment under data/.
    ".svg",
}
# Playwright's own report/trace-viewer application. No test data, and the
# shipped report.js carries a base64 blob that the JWT rule would eat.
STATIC_SUFFIXES = {
    ".js", ".css", ".map", ".svg", ".ico", ".ttf", ".woff", ".woff2", ".otf",
    ".webmanifest",
}
REPORT_PAYLOAD = re.compile(
    r'(<template id="playwrightReportBase64">data:application/zip;base64,)'
    r"(.*?)(</template>)",
    re.DOTALL,
)

breaches: list[str] = []
redacted: list[str] = []
unreachable: dict[str, int] = {}


def _is_reporter_asset(rel: str) -> bool:
    """True for Playwright's own report chrome, false for a test attachment.

    The reporter writes its shell and application code at the report ROOT and
    copies every attachment into ``data/``. Skipping by suffix alone treated a
    test-authored ``data/diagram.svg`` as reporter chrome, so it was neither
    scrubbed nor reported.
    """

    return "/" not in rel.replace("\\", "/").strip("/")
scanned = 0


def scrub(text: str, where: str) -> str:
    """Redact, and record a hard-fail rather than let it reach the artifact.

    Stream mode refuses to emit anything at all. In-place there is no such
    option -- the file already exists in the directory about to be uploaded --
    so the offending bytes are replaced and the breach is recorded. The
    process still exits 1 at the end, and the upload step runs `if: always()`,
    so CI goes red AND the artifact is safe.
    """

    if hard_fail.search(text):
        breaches.append(f"{where}: production-secret env name")
        text = hard_fail.sub("<production-secret-env-redacted>", text)
    if pem_block.search(text):
        breaches.append(f"{where}: PEM private key body")
        text = pem_block.sub("<pem-private-key-redacted>", text)
    return redact(text)


def scrub_zip(raw: bytes, where: str) -> tuple[bytes, bool]:
    """Rewrite every UTF-8 entry of a zip; copy the rest byte-for-byte."""

    source = zipfile.ZipFile(io.BytesIO(raw))
    buffer = io.BytesIO()
    changed = False
    with zipfile.ZipFile(buffer, "w") as target_zip:
        for info in source.infolist():
            data = source.read(info)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                target_zip.writestr(info, data)
                continue
            cleaned = scrub(text, f"{where}!{info.filename}")
            if cleaned != text:
                changed = True
            target_zip.writestr(info, cleaned.encode("utf-8"))
    return buffer.getvalue(), changed


root = Path(target)
if not root.is_dir():
    print(f"ERROR: not a directory: {root}", file=sys.stderr)
    raise SystemExit(2)

for path in sorted(root.rglob("*")):
    if path.is_symlink() or not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()

    if suffix in STATIC_SUFFIXES and _is_reporter_asset(rel):
        continue

    if suffix in {".html", ".htm"}:
        raw_html = path.read_text(encoding="utf-8", errors="surrogateescape")
        match = REPORT_PAYLOAD.search(raw_html)
        if match is None:
            # Not a report shell. It is either a viewer shell (trace/index.html,
            # which is reporter application code the replacement list must not
            # touch -- the shipped report.js carries a JWT-shaped blob the `eyJ`
            # rule would eat) or a test-authored .html ATTACHMENT, which is
            # ordinary text and must be scrubbed like any other.
            #
            # This branch used to `continue` for both, so an attachment kept its
            # secret AND was absent from the summary: neither redacted nor named
            # unreachable. Probe: data/attach.html carrying `?token=dtxr_1_...`
            # survived a full --dir pass silently.
            if _is_reporter_asset(rel):
                continue
            scanned += 1
            cleaned = scrub(raw_html, rel)
            if cleaned != raw_html:
                path.write_text(
                    cleaned, encoding="utf-8", errors="surrogateescape", newline=""
                )
                redacted.append(rel)
            continue
        scanned += 1
        payload, changed = scrub_zip(
            base64.b64decode(match.group(2)), f"{rel}#reportBase64"
        )
        if changed:
            rebuilt = (
                raw_html[: match.start(2)]
                + base64.b64encode(payload).decode("ascii")
                + raw_html[match.end(2) :]
            )
            path.write_text(
                rebuilt, encoding="utf-8", errors="surrogateescape", newline=""
            )
            redacted.append(rel)
        continue

    if suffix == ".zip":
        scanned += 1
        payload, changed = scrub_zip(path.read_bytes(), rel)
        if changed:
            path.write_bytes(payload)
            redacted.append(rel)
        continue

    if suffix in TEXT_SUFFIXES:
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        cleaned = scrub(text, rel)
        if cleaned != text:
            path.write_text(
                cleaned, encoding="utf-8", errors="surrogateescape", newline=""
            )
            redacted.append(rel)
        continue

    unreachable[suffix or "(no suffix)"] = unreachable.get(suffix or "(no suffix)", 0) + 1

print(f"redact_evidence: scanned {scanned} redactable file(s) under {root}")
for rel in redacted:
    print(f"  redacted: {rel}")
if unreachable:
    summary = ", ".join(
        f"{count}x {suffix}" for suffix, count in sorted(unreachable.items())
    )
    print(
        "  NOT redactable by text substitution (rendered pixels / opaque "
        f"binaries): {summary}"
    )
if breaches:
    for breach in breaches:
        print(f"ERROR: {breach}", file=sys.stderr)
    print(
        "ERROR: production-secret env name or PEM private key body in "
        "evidence directory",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
