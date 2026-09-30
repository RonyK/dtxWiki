from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Operator audit artifacts (manual, hand-written sprint closure evidence).
# Test-produced logs live in CI artifacts (see .github/workflows/ci.yml).
EVIDENCE_DIR = ROOT / "internal/audit"
REDACTOR_SH = ROOT / "scripts/redact_evidence.sh"
REDACTOR_PS1 = ROOT / "scripts/redact_evidence.ps1"

# Sprint_5 Phase_0~2 fix-round (XPLAT-3, XPLAT-4, XPLAT-5):
# - XPLAT-4: gate bash tests on `shutil.which("bash")` so Windows CI runners
#   without a bash interpreter skip cleanly instead of failing with
#   WinError 193.
# - XPLAT-3: cover the .ps1 sibling with a parity test that pipes the same
#   input through bash and PowerShell and asserts byte-identical output
#   (after LF normalization). Without this, the .ps1 can rot silently.
# - XPLAT-5: the parity test exercises both scripts on the same input so any
#   pattern drift in either script breaks the assertion.
HAS_BASH = shutil.which("bash") is not None
HAS_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _bash_has_working_python() -> bool:
    """Confirm bash can launch a Python that actually prints --version.

    On Windows dev machines that lack a real Python install, ``python3`` and
    ``python`` resolve to Microsoft Store stubs that exit non-zero. The
    redactor script (XPLAT-2 fix) probes this and refuses to run rather than
    silently emit an unredacted stream — so the test must mirror the same
    probe to avoid false failures on such machines.
    """

    if not HAS_BASH:
        return False
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    "python3 --version >/dev/null 2>&1 "
                    "|| python --version >/dev/null 2>&1"
                ),
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:  # pragma: no cover - defensive
        return False


HAS_BASH_PYTHON = _bash_has_working_python()


def _evidence_text() -> str:
    if not EVIDENCE_DIR.exists():
        return ""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(EVIDENCE_DIR.rglob("*"))
        if path.is_file()
    )


def _run_redactor(input_text: str) -> subprocess.CompletedProcess[str]:
    # On Windows the .sh shebang is not honored by CreateProcess; route the
    # script through bash explicitly so a posix-like interpreter (git-bash,
    # WSL, msys2) runs it. Linux/macOS hand the path off to bash too — they
    # would handle the shebang on their own, but routing through bash keeps
    # the call site uniform.
    cmd: list[str] = ["bash", str(REDACTOR_SH)]
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        # Pin the pipe rather than take `locale.getpreferredencoding()`. The
        # parity sample carries a Korean line on purpose, and a Windows box
        # defaulting to cp1252 cannot even write it to stdin -- the test that
        # exists to prove UTF-8 survives would die before reaching the script.
        encoding="utf-8",
        capture_output=True,
        cwd=ROOT,
        check=False,
    )


def _run_redactor_ps1(input_text: str) -> subprocess.CompletedProcess[str]:
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if ps is None:
        raise RuntimeError("powershell/pwsh not on PATH")
    return subprocess.run(
        [ps, "-NoProfile", "-File", str(REDACTOR_PS1)],
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=ROOT,
        check=False,
    )


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_no_bearer_in_evidence_logs():
    result = _run_redactor("Bearer abcdefgh12345678\n")
    header_result = _run_redactor("Authorization: Bearer abcdefgh12345678\n")

    assert result.returncode == 0, result.stderr
    assert header_result.returncode == 0, header_result.stderr
    assert "Bearer <redacted>" in result.stdout
    assert "Authorization: <redacted>" in header_result.stdout
    assert not re.search(
        r"Bearer (?!resource_metadata\b)[A-Za-z0-9._\-]{8,}",
        _evidence_text(),
    )


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_no_raw_pat_in_evidence_logs():
    result = _run_redactor("token = 'dtxw_abcdefghijklmnopqrstuvwxyz123456'\n")

    assert result.returncode == 0, result.stderr
    assert "dtxw_<redacted>" in result.stdout
    assert not re.search(r"\bdtxw_[A-Za-z0-9._\-]+", _evidence_text())


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_no_jwt_full_in_evidence_logs():
    dummy_jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    result = _run_redactor(f"access_token={dummy_jwt}\n")

    assert result.returncode == 0, result.stderr
    assert "<jwt-redacted>" in result.stdout
    assert not re.search(
        r"eyJ[A-Za-z0-9._\-]{20,}\.[A-Za-z0-9._\-]+\.[A-Za-z0-9._\-]+",
        _evidence_text(),
    )


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_no_session_secret_in_evidence_logs():
    result = _run_redactor("DTXWIKI_SESSION_SECRET=do-not-commit\n")

    assert result.returncode == 1
    assert "production-secret env name" in result.stderr
    assert not re.search(
        r"DTXWIKI_SESSION_SECRET|"
        r"DTXWIKI_OAUTH_JWT_PRIVATE_KEY_(?:PEM|PATH)|"
        r"DTXWIKI_METRICS_BEARER_TOKEN|"
        r"DTXWIKI_SMTP_PASSWORD",
        _evidence_text(),
    )
    assert os.access(REDACTOR_SH, os.X_OK)


# ---------------------------------------------------------------------------
# XPLAT-3 / XPLAT-5 — PowerShell sibling coverage + parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not HAS_POWERSHELL,
    reason="pwsh/powershell not on PATH; .sh sibling covered separately",
)
def test_ps1_redacts_bearer_and_hard_fails_on_secret_env():
    """XPLAT-3 positive control + negative control:

    - Positive: ``Bearer <token>`` is replaced with ``Bearer <redacted>``.
    - Negative: ``DTXWIKI_SESSION_SECRET=...`` triggers exit 1 with the
      ``production-secret env name`` keyword pinned (ADR A6).
    """
    bearer_result = _run_redactor_ps1("Bearer abcdefgh12345678\n")
    assert bearer_result.returncode == 0, bearer_result.stderr
    assert "Bearer <redacted>" in bearer_result.stdout

    secret_result = _run_redactor_ps1("DTXWIKI_SESSION_SECRET=do-not-commit\n")
    assert secret_result.returncode == 1
    # Write-Error wraps the message in PowerShell error frames; pin the
    # keyword substring rather than exact stream layout.
    assert "production-secret env name" in secret_result.stderr


@pytest.mark.skipif(
    not (HAS_BASH_PYTHON and HAS_POWERSHELL),
    reason="SMTP secret parity needs both bash+python and pwsh/powershell on PATH",
)
def test_smtp_password_env_hard_fails_in_both_redactors():
    sample = "DTXWIKI_SMTP_PASSWORD=do-not-commit\n"

    sh_result = _run_redactor(sample)
    ps1_result = _run_redactor_ps1(sample)

    assert sh_result.returncode == 1
    assert ps1_result.returncode == 1
    assert "production-secret env name" in sh_result.stderr
    assert "production-secret env name" in ps1_result.stderr
    assert "do-not-commit" not in sh_result.stdout
    assert "do-not-commit" not in ps1_result.stdout


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_sh_hard_fails_on_pem_private_key_body():
    """SEC-3 (bash): PEM private-key bodies must hard-fail (exit 1) with the
    ``PEM private key body`` keyword pinned (ADR A6). A leaked private key
    in evidence is a break-glass incident, not a sanitization scenario.

    Positive control: a benign Bearer line still produces exit 0.
    """
    # Assembled at runtime so the source never holds a PEM private-key block
    # for a secret scanner to trip on; the redactor sees the same bytes.
    pem_input = (
        "-----BEGIN RSA " + "PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj4j5...\n"
        "-----END RSA " + "PRIVATE KEY-----\n"
    )
    pem_result = _run_redactor(pem_input)
    assert pem_result.returncode == 1
    assert "PEM private key body" in pem_result.stderr

    # Positive control — same script, benign input still passes.
    ok_result = _run_redactor("Bearer abcdefgh12345678\n")
    assert ok_result.returncode == 0


@pytest.mark.skipif(
    not HAS_POWERSHELL,
    reason="pwsh/powershell not on PATH; .sh sibling covered separately",
)
def test_ps1_hard_fails_on_pem_private_key_body():
    """SEC-3 (PowerShell): parity with the .sh hard-fail."""
    pem_input = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHQCAQEEIBfp...\n"
        "-----END EC PRIVATE KEY-----\n"
    )
    pem_result = _run_redactor_ps1(pem_input)
    assert pem_result.returncode == 1
    assert "PEM private key body" in pem_result.stderr


@pytest.mark.skipif(
    not (HAS_BASH_PYTHON and HAS_POWERSHELL),
    reason="parity test needs both bash+python and pwsh/powershell on PATH",
)
def test_sh_ps1_parity_byte_identical_after_lf_normalization():
    """XPLAT-5 parity: bash and PowerShell siblings MUST emit equivalent
    redacted output for the same input (after LF normalization). Without
    this guard, regex drift in either script would go unnoticed.

    Sprint_5 Phase_2 fix-round-2 (HIGH-2): the original ASCII-only sample
    let the .ps1 UTF-8 encoding bug (XPLAT-NEW-2) slip past parity. The
    sample now includes a Korean evidence line so any future regression
    that drops the .ps1 UTF-8 encoding pin breaks parity directly.
    """
    sample = (
        "Bearer abcdefgh12345678\n"
        "token = 'dtxw_abcdefghijklmnopqrstuvwxyz123456'\n"
        # Phase 5: the sample carried neither a query-parameter secret nor a
        # dtxr_ recovery token, so when the .sh sibling gained rules for both
        # and the .ps1 did not, this guard still reported parity. A test that
        # certifies a parity which no longer holds is worse than no test.
        "GET /reset-password?token=dtxr_1_ONETIME_RECOVERY_TOKEN HTTP/1.1\n"
        # M-2: the fragment form the e2e suite drives now.
        'Navigate to "/reset-password#token=dtxr_1_ONETIME_FRAGMENT_TOKEN"\n'
        "callback?code=oauth-code-value&state=oauth-state-value\n"
        "Authorization: Bearer secret-tok-A1B2C3D4\n"
        "Cookie: session=opaque-cookie-value\n"
        "Mcp-Session-Id: ABCDEFG1234567HIJKLMN89\n"
        '{"access_token": "eyJabcdefghijklmnopqrstuv.payload.sig"}\n'
        "/home/foo/secret\n"
        "C:\\Users\\foo\\secret\n"
        # Sprint_5 Phase_2 fix-round-2 (HIGH-2): non-ASCII context line.
        # The .ps1 redactor must preserve the Korean bytes through UTF-8;
        # the .sh sibling already does via Python text=utf-8 IO.
        "한글 Evidence Bearer abcdefgh12345678\n"
    )
    sh_out = _run_redactor(sample).stdout.replace("\r\n", "\n").replace("\r", "")
    # Sprint_5 Phase_2 fix-round-4 (XPLAT-FR3-2): PowerShell 5.1 (powershell.exe)
    # prepends a UTF-8 BOM to its stdout despite the UTF-8 encoding pin at the
    # top of redact_evidence.ps1; the BOM is a host-driver artifact, not a
    # redactor output. Strip it from the .ps1 side only so the byte-identity
    # assertion holds for ASCII inputs. The matching lstrip pattern is used by
    # the fix-round-3 JSON token tests above (Bot-B7). Korean text host-locale
    # parity remains a separate issue (closure amendment §5.2).
    ps_out = (
        _run_redactor_ps1(sample)
        .stdout.lstrip("﻿")
        .replace("\r\n", "\n")
        .replace("\r", "")
    )
    assert sh_out == ps_out, (
        "redact_evidence.sh and .ps1 diverged on shared input:\n"
        f"--- sh ---\n{sh_out!r}\n--- ps1 ---\n{ps_out!r}"
    )


@pytest.mark.skipif(
    not HAS_POWERSHELL,
    reason="pwsh/powershell not on PATH; .ps1 Windows-path test requires .ps1",
)
def test_ps1_redacts_windows_path_both_forms():
    """Sprint_5 Phase_2 fix-round-2 (XPLAT-NEW-1, Sonnet Xplat
    260517-1123): the .ps1 must redact Windows paths in BOTH literal
    (``C:\\Users\\foo``, common in console evidence) AND JSON-escaped
    (``C:\\\\Users\\\\foo``, common in pytest / SDK exception text).

    Previously the .ps1 redacted only the literal form and the .sh
    redacted only the JSON-escaped form; either-side leak risk depending
    on which redactor was used. Now both scripts cover both forms.

    ADR A6: paired positive controls — same redactor, two input shapes,
    one assertion each. A regression that drops one pattern fails one
    of the two asserts.
    """
    literal_in = "C:\\Users\\foo\\secret\n"
    escaped_in = "C:\\\\Users\\\\foo\\\\secret\n"

    literal_out = _run_redactor_ps1(literal_in).stdout
    escaped_out = _run_redactor_ps1(escaped_in).stdout

    # Both forms collapse to ``~`` (followed by the remaining
    # backslash-prefixed suffix that the regex does not consume).
    assert "C:\\Users\\foo" not in literal_out, (
        f"literal Windows path NOT redacted; out={literal_out!r}"
    )
    assert "~" in literal_out, f"literal path missing ~; out={literal_out!r}"
    assert "C:\\\\Users\\\\foo" not in escaped_out, (
        f"JSON-escaped Windows path NOT redacted; out={escaped_out!r}"
    )
    assert "~" in escaped_out, f"escaped path missing ~; out={escaped_out!r}"


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .sh Windows-path test requires .sh",
)
def test_sh_redacts_windows_path_both_forms():
    """Sprint_5 Phase_2 fix-round-2 (XPLAT-NEW-1) — .sh sibling of
    ``test_ps1_redacts_windows_path_both_forms``. The .sh now matches
    both literal and JSON-escaped Windows paths (previously only the
    JSON-escaped form).

    ADR A6 paired test: this and the .ps1 sibling assert the same
    contract on each redactor independently, so the parity test (which
    needs both interpreters on PATH) is not the only guard against
    Windows-path regex drift.
    """
    literal_in = "C:\\Users\\foo\\secret\n"
    escaped_in = "C:\\\\Users\\\\foo\\\\secret\n"

    literal_out = _run_redactor(literal_in).stdout
    escaped_out = _run_redactor(escaped_in).stdout

    assert "C:\\Users\\foo" not in literal_out, (
        f"literal Windows path NOT redacted; out={literal_out!r}"
    )
    assert "~" in literal_out, f"literal path missing ~; out={literal_out!r}"
    assert "C:\\\\Users\\\\foo" not in escaped_out, (
        f"JSON-escaped Windows path NOT redacted; out={escaped_out!r}"
    )
    assert "~" in escaped_out, f"escaped path missing ~; out={escaped_out!r}"


@pytest.mark.skipif(
    not HAS_POWERSHELL,
    reason="pwsh/powershell not on PATH; Korean encoding test requires .ps1",
)
def test_ps1_preserves_korean_utf8():
    """Sprint_5 Phase_2 fix-round-2 (HIGH-2, Codex 260517-1131):

    PowerShell 5.1 defaults [Console]::InputEncoding / OutputEncoding to
    the OEM codepage (cp949 on Korean Windows). Without the UTF-8 pin at
    the top of scripts/redact_evidence.ps1, Korean evidence is corrupted
    in-flight even though the redactor removes secrets correctly. This
    test pipes Korean text through the .ps1 and asserts the Korean bytes
    survive (byte-equal to UTF-8 ``한글``) AND the Bearer is redacted.

    ADR A6 negative-test rule: paired with the .sh ``test_no_bearer_in_
    evidence_logs`` positive control above — same redaction contract
    expressed on both redactors, but the .ps1 case adds the encoding
    pin verification that the .sh path never needed (Python's text=
    utf-8 IO covers .sh by default).
    """
    sample = "한글 Evidence Bearer abcdefgh12345678\n"
    # subprocess + text=True decodes with locale; force UTF-8 so the
    # check exercises ONLY the .ps1 script, not the subprocess wrapper.
    ps = shutil.which("pwsh") or shutil.which("powershell")
    assert ps is not None  # gated by HAS_POWERSHELL above
    result = subprocess.run(
        [ps, "-NoProfile", "-File", str(REDACTOR_PS1)],
        input=sample.encode("utf-8"),
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # The .ps1 must emit the Korean bytes unchanged (UTF-8 round-trip).
    decoded = result.stdout.decode("utf-8")
    # LF/CRLF tolerant — .ps1 may add a trailing CRLF on some hosts.
    decoded_normalized = decoded.replace("\r\n", "\n").replace("\r", "")
    assert "한글 Evidence" in decoded_normalized, (
        f"Korean characters corrupted by .ps1 (likely missing UTF-8 "
        f"encoding pin); stdout bytes = {result.stdout!r}"
    )
    # Bearer still redacted (positive control on the redaction contract).
    assert "Bearer <redacted>" in decoded_normalized, (
        f"Bearer redaction missing; stdout = {decoded_normalized!r}"
    )


# ---------------------------------------------------------------------------
# Sprint_5 Phase_2 fix-round-3 (Bot-B7) — JSON token redaction produces valid
# JSON. Pre-fix the .sh replacement string was r"\1<redacted>\"" (double-
# quoted raw); Python's re.sub treats \" as an unknown group reference and
# copies it through, so the output became {"access_token": "<redacted>\"}
# (an extra literal backslash before the closing quote) — invalid JSON.
# ---------------------------------------------------------------------------

_JSON_TOKEN_KEYS = (
    "access_token",
    "refresh_token",
    "client_secret",
    "code",
    "code_verifier",
)


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
@pytest.mark.parametrize("key", _JSON_TOKEN_KEYS)
def test_sh_redacts_json_token_produces_valid_json(key):
    """Bot-B7 (sh): piping a single-key JSON object through the .sh redactor
    yields valid JSON whose value is exactly ``<redacted>``.

    ADR A6 positive matrix (5 keys); the negative pair lives in the existing
    ``test_no_session_secret_in_evidence_logs`` hard-fail test.
    """
    payload = json.dumps({key: "secret-value-123"}) + "\n"
    result = _run_redactor(payload)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed[key] == "<redacted>", (
        f"redacted JSON token mis-shaped; stdout = {result.stdout!r}"
    )


@pytest.mark.skipif(
    not HAS_POWERSHELL,
    reason="pwsh/powershell not on PATH; .sh sibling covered separately",
)
@pytest.mark.parametrize("key", _JSON_TOKEN_KEYS)
def test_ps1_redacts_json_token_produces_valid_json(key):
    """Bot-B7 (ps1): parity with the .sh sibling. The .ps1 already used the
    correct PowerShell single-quoted replacement ('$1<redacted>"') so this
    test pins the contract on both redactors and would surface any future
    regression in either.
    """
    payload = json.dumps({key: "secret-value-123"}) + "\n"
    result = _run_redactor_ps1(payload)
    assert result.returncode == 0, result.stderr
    # PowerShell may emit a UTF-8 BOM; strip it before json.loads.
    stdout = result.stdout.lstrip("﻿")
    parsed = json.loads(stdout)
    assert parsed[key] == "<redacted>", (
        f"redacted JSON token mis-shaped; stdout = {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# M-2: recovery links carry their one-time token in the URL fragment
# (`/reset-password#token=`, `/verify-email#token=`), and the e2e suite drives
# that form -- plus one legacy `?token=` link -- so both shapes reach the step
# titles of the published Playwright report. Both must be redacted.
# ---------------------------------------------------------------------------

_RECOVERY_LINK_TOKENS = (
    ("/reset-password", "dtxr_1_M2_RECOVERY_LINK_TOKEN"),
    ("/verify-email", "dtxe_M2_VERIFICATION_LINK_TOKEN"),
)


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
@pytest.mark.parametrize("separator", ["#", "?"], ids=["fragment", "legacy-query"])
@pytest.mark.parametrize(
    ("path", "token"),
    _RECOVERY_LINK_TOKENS,
    ids=["reset-password", "verify-email"],
)
def test_sh_redacts_recovery_link_tokens_in_fragment_and_query(
    separator, path, token
):
    sample = (
        f'Navigate to "{path}{separator}token={token}"\n'
        f"http://127.0.0.1:21997{path}{separator}token={token}\n"
    )

    result = _run_redactor(sample)

    assert result.returncode == 0, result.stderr
    assert token not in result.stdout
    assert f'"{path}{separator}token=<redacted>"' in result.stdout
    assert f"{path}{separator}token=<redacted>\n" in result.stdout


@pytest.mark.skipif(
    not HAS_BASH_PYTHON,
    reason="bash + working python not available; .ps1 sibling covered separately",
)
def test_sh_dir_mode_redacts_a_fragment_token_in_the_report_payload(tmp_path):
    """The HTML reporter base64-inlines every step title into index.html on
    every run, so `page.goto("/reset-password#token=...")` is published by a
    green run. The token is not plaintext in the file; only --dir decodes the
    payload and reaches it."""

    token = _RECOVERY_LINK_TOKENS[0][1]
    title = '{"title":"Navigate to \\"/reset-password#token=' + token + '\\""}'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as payload:
        payload.writestr("step.json", title)
    report = tmp_path / "playwright-report"
    report.mkdir()
    (report / "index.html").write_text(
        '<html><template id="playwrightReportBase64">'
        "data:application/zip;base64,"
        + base64.b64encode(buffer.getvalue()).decode("ascii")
        + "</template></html>",
        encoding="utf-8",
    )
    assert token not in (report / "index.html").read_text(encoding="utf-8")

    result = subprocess.run(
        ["bash", str(REDACTOR_SH), "--dir", str(report)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    match = re.search(
        r"base64,(.*?)</template>",
        (report / "index.html").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None
    published = (
        zipfile.ZipFile(io.BytesIO(base64.b64decode(match.group(1))))
        .read("step.json")
        .decode("utf-8")
    )
    assert token not in published
    # The JSON-escaped terminator survives, so the entry is still valid JSON.
    assert json.loads(published) == {
        "title": 'Navigate to "/reset-password#token=<redacted>"'
    }
