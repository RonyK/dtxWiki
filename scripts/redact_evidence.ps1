# Pipe redactor for Sprint_5 evidence artifacts.
# Reads stdin and writes redacted output to stdout. Hard-fails when production
# secret environment-variable names appear in the evidence stream.
#
# Sprint_5 Phase_2 fix-round-2 (HIGH-2, Codex review 260517-1131 + Sonnet
# Xplat XPLAT-NEW-2): Windows PowerShell 5.1 defaults $OutputEncoding to
# ASCII and [Console]::OutputEncoding / [Console]::InputEncoding to the OEM
# codepage (cp949 on Korean Windows, cp437 on en-US, etc.). Without an
# explicit pin, evidence containing Korean (or any non-ASCII) is mojibake'd
# on a Windows dev PC — the secret is redacted but the surrounding context
# is corrupted. Force BOM-less UTF-8 on every stream so .ps1 emits the same
# bytes as the .sh sibling regardless of host locale.
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = $utf8NoBom
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom

$hardFail = 'DTXWIKI_SESSION_SECRET|DTXWIKI_OAUTH_JWT_PRIVATE_KEY_(PEM|PATH)|DTXWIKI_METRICS_BEARER_TOKEN|DTXWIKI_SMTP_PASSWORD'
$inputText = [Console]::In.ReadToEnd()
if ($inputText -match $hardFail) {
    Write-Error 'production-secret env name in evidence stream'
    exit 1
}

# Sprint_5 Phase_0~2 fix-round (SEC-3): PEM private-key bodies must never
# reach an evidence stream. Detect the BEGIN/END envelope and hard-fail
# rather than redact in place. PowerShell -match defaults to single-line
# semantics so we set the explicit `(?s)` inline modifier for DOTALL.
$pemBlock = '(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----'
if ($inputText -match $pemBlock) {
    Write-Error 'PEM private key body in evidence stream'
    exit 1
}

# Sprint_5 Phase_2 fix-round-2 (XPLAT-NEW-1, Sonnet Xplat 260517-1123):
# the C:\Users path replacement covers BOTH literal (C:\Users\foo) AND
# JSON-escaped (C:\\Users\\foo) forms, matching the .sh sibling so the
# parity test (test_sh_ps1_parity_byte_identical_after_lf_normalization)
# yields byte-identical output for either input form. Order matters:
# match the longer (JSON-escaped, 2-backslash) form first so it isn't
# truncated mid-path by the literal (1-backslash) pattern.
#
# The result goes through [Console]::Out.Write rather than the pipeline: an
# implicitly emitted string picks up a trailing newline the .sh sibling never
# writes, so the two diverged by one byte. That went unnoticed because the
# parity test skips wherever pwsh is absent (all Linux CI) and, where both are
# present, died earlier on a stdin encoding error.
$redacted = $inputText `
    -replace '([?&#](?:access_token|refresh_token|id_token|code_verifier|token|code|state|password|secret)=)[^&\s"''<>\\]+', '$1<redacted>' `
    -replace 'Bearer (?!resource_metadata\b)[A-Za-z0-9._\-]+', 'Bearer <redacted>' `
    -replace '\bdtxw_[A-Za-z0-9._\-]+', 'dtxw_<redacted>' `
    -replace '\bdtxr_[A-Za-z0-9._\-]+', 'dtxr_<redacted>' `
    -replace '(Authorization: )[^\r\n]+', '$1<redacted>' `
    -replace '(Cookie|Set-Cookie): [^\r\n]+', '$1: <redacted>' `
    -replace '(Mcp-Session-Id: )[A-Za-z0-9._\-]+', '$1<redacted>' `
    -replace '("(?:access_token|refresh_token|client_secret|code|code_verifier)": ")[^"]+"', '$1<redacted>"' `
    -replace 'eyJ[A-Za-z0-9._\-]{20,}', '<jwt-redacted>' `
    -replace '/home/[a-zA-Z0-9._\-]+', '~' `
    -replace '/Users/[a-zA-Z0-9._\-]+', '~' `
    -replace 'C:\\\\Users\\\\[a-zA-Z0-9._\-]+', '~' `
    -replace 'C:\\Users\\[a-zA-Z0-9._\-]+', '~'

[Console]::Out.Write($redacted)
