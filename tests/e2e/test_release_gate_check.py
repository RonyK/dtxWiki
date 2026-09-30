from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/release_gate_check.sh"
HEADER = (
    "| client | version | auth path | pat_or_dcr_started_at | registered_at | "
    "first_list_tools_at | elapsed_to_list_tools | list_tools | read_page | "
    "write_agent_page_draft | cross-workspace negative | "
    "restricted-library negative | notes |"
)
SEPARATOR = (
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)
CLI_TEST_FILES = (
    "backend/tests/cli/test_mcp_register_http.py",
    "backend/tests/cli/test_mcp_doctor_http.py",
    "backend/tests/cli/test_users_issue_pat.py",
)


def test_release_gate_check_accepts_complete_evidence(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Release gate check: OK" in result.stdout


def test_release_gate_check_accepts_pytest_warning_suffix(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed, 32 warnings in 12.34s",
        cli_result="52 passed, 3 warnings in 9.39s",
        security_result="7 passed, 1 warning in 1.78s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Release gate check: OK" in result.stdout


def test_release_gate_check_accepts_changed_nonzero_cli_pass_count(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_result="124 passed in 9.39s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr


def test_release_gate_check_rejects_incomplete_cli_manifest(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_test_files=CLI_TEST_FILES[:-1],
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "CLI suite manifest missing" in result.stderr


def test_release_gate_check_rejects_extra_cli_manifest_file(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_test_files=(*CLI_TEST_FILES, "backend/tests/cli/test_cli.py"),
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "CLI suite manifest contains 4 test files" in result.stderr


def test_release_gate_check_rejects_zero_cli_tests(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_result="0 passed in 0.01s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "CLI suite has no nonzero passing result" in result.stderr


def test_release_gate_check_rejects_cli_failure_summary(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_result="52 passed, 1 failed in 9.39s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "CLI suite skip/fail marker" in result.stderr


def test_release_gate_check_rejects_cli_deselection_summary(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        cli_result="52 passed, 17 deselected in 9.39s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "CLI suite skip/fail marker" in result.stderr


def test_release_gate_check_rejects_non_exact_security_pass_count(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        security_result="17 passed in 1.78s",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "security headers suite" in result.stderr


def test_release_gate_check_rejects_remote_skips(tmp_path):
    _write_evidence(tmp_path, remote_result="8 skipped in 0.82s")

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke" in result.stderr


def test_release_gate_check_rejects_remote_fail_summary(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s\n1 failed in 0.01s")

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke skip/fail marker" in result.stderr


def test_release_gate_check_allows_benign_error_word_in_passed_log(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        remote_extra=["negative control observed error_code=restricted_library_denied"],
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr


def test_release_gate_check_rejects_condensed_remote_log(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s", include_nodes=False)

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke PAT list_tools scenario" in result.stderr


def test_release_gate_check_rejects_non_passed_remote_node_status(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        remote_node_status="SKIPPED",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke PAT list_tools scenario" in result.stderr


def test_release_gate_check_rejects_non_exact_remote_pass_count(tmp_path):
    _write_evidence(tmp_path, remote_result="18 passed in 12.34s")

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke 8 scenarios" in result.stderr


def test_release_gate_check_rejects_local_fallback_marker(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        remote_mode="local",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "local fallback smoke marker" in result.stderr


def test_release_gate_check_rejects_missing_remote_client_marker(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        remote_client=None,
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke client marker" in result.stderr


def test_release_gate_check_rejects_missing_remote_teardown_marker(tmp_path):
    _write_evidence(
        tmp_path,
        remote_result="8 passed in 12.34s",
        remote_teardown=None,
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "remote smoke compose teardown marker" in result.stderr


def test_release_gate_check_rejects_placeholder_manual_log(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")
    (tmp_path / "client-smoke-log.md").write_text(
        _valid_client_log().replace("2.1.74", "<version>", 1),
        encoding="utf-8",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "manual client smoke log validator failed" in result.stderr


def test_release_gate_check_summarizes_evidence_blockers(tmp_path):
    _write_evidence(tmp_path, remote_result="8 skipped in 0.82s")
    (tmp_path / "client-smoke-log.md").write_text(
        _valid_client_log().replace("2.1.74", "<version>", 1),
        encoding="utf-8",
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "Remote MCP release blocker summary:" in result.stderr
    assert "Remote smoke evidence is incomplete" in result.stderr
    assert "scripts/release_remote_smoke.sh --preflight" in result.stderr
    assert "Manual external-client evidence is incomplete" in result.stderr
    blocker_index = result.stderr.index("Remote MCP release blocker summary:")
    remote_failure_index = result.stderr.index("FAIL: remote smoke 8 scenarios")
    assert blocker_index < remote_failure_index


def test_release_gate_check_rejects_setup_doc_missing_teardown_marker(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        setup_doc_replacements={"DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok": ""},
    )

    assert result.returncode == 1
    assert "setup doc remote teardown marker" in result.stderr


def test_release_gate_check_rejects_setup_doc_legacy_mcp_transport(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        setup_doc_replacements={
            "Transport: Streamable HTTP only.": "Transport: stdio via dtxwiki-mcp.",
        },
    )

    assert result.returncode == 1
    assert "legacy MCP transport/setup" in result.stderr


def test_release_gate_check_rejects_setup_doc_stale_codex_windows_path(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        setup_doc_replacements={
            "%USERPROFILE%\\.codex\\config.toml": "%APPDATA%\\codex\\config.toml",
        },
    )

    assert result.returncode == 1
    assert "legacy MCP transport/setup" in result.stderr


def test_release_gate_check_requires_claude_user_scope_registration(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        setup_doc_replacements={
            "claude mcp add --scope user --transport http": (
                "claude mcp add --transport http"
            ),
        },
    )

    assert result.returncode == 1
    assert "native Claude CLI user-scope registration command" in result.stderr


def test_release_gate_check_rejects_readme_legacy_mcp_command(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        readme_replacements={
            "See doc/setup/remote-mcp-clients.md.": (
                "Legacy setup: dtxwiki mcp register-remote --remote https://example.test/mcp"
            ),
        },
    )

    assert result.returncode == 1
    assert "README legacy MCP transport/setup" in result.stderr


def test_release_gate_check_rejects_setup_doc_missing_manual_elapsed_contract(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        setup_doc_replacements={
            "elapsed_to_list_tools must be <= 30m.": "",
        },
    )

    assert result.returncode == 1
    assert "setup doc manual elapsed" in result.stderr


def test_release_gate_check_rejects_phase5_overview_missing_client_marker(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        phase5_overview_replacements={"DTXWIKI_REMOTE_CLIENT=<label>": ""},
    )

    assert result.returncode == 1
    assert "Phase_5 overview remote client marker" in result.stderr


def test_release_gate_check_rejects_phase5_overview_missing_manual_kst(tmp_path):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        phase5_overview_replacements={"KST +09:00": "timezone-aware"},
    )

    assert result.returncode == 1
    assert "Phase_5 overview manual KST offset" in result.stderr


def test_release_gate_check_rejects_phase5_overview_missing_external_dev_pc(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        phase5_overview_replacements={"external dev PC": "remote host"},
    )

    assert result.returncode == 1
    assert "Phase_5 overview external dev PC evidence" in result.stderr


def test_release_gate_check_rejects_phase5_overview_stale_codex_windows_path(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        phase5_overview_replacements={
            "%USERPROFILE%\\.codex\\config.toml": "%APPDATA%\\codex\\config.toml",
        },
    )

    assert result.returncode == 1
    assert "stale client config path" in result.stderr


def test_release_gate_check_rejects_sprint5_overview_stale_claude_paths(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        sprint5_overview_replacements={
            "~/.claude.json": "~/.config/claude/",
            "%USERPROFILE%\\.claude.json": "%APPDATA%\\Claude\\",
        },
    )

    assert result.returncode == 1
    assert "stale Sprint 5 overview Phase 5 contract" in result.stderr


def test_release_gate_check_rejects_sprint5_overview_stale_oauth_node(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        sprint5_overview_replacements={
            "test_oauth_full_flow_tool_round_trip": (
                "test_oauth_full_flow_dcr_authorize_token"
            ),
        },
    )

    assert result.returncode == 1
    assert "stale Sprint 5 overview Phase 5 contract" in result.stderr


def test_release_gate_check_rejects_sprint5_overview_stale_remote_command(
    tmp_path,
):
    _write_evidence(tmp_path, remote_result="8 passed in 12.34s")

    result = _run_gate(
        tmp_path,
        sprint5_overview_replacements={
            "scripts/release_remote_smoke.sh": (
                "pytest -m cross_transport_remote "
                "tests/e2e/remote_mcp_smoke.py -v > "
                "evidence/remote-smoke.log"
            ),
            "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok.": "",
        },
    )

    assert result.returncode == 1
    assert "stale Sprint 5 overview Phase 5 contract" in result.stderr


def _run_gate(
    evidence_dir: Path,
    *,
    setup_doc_replacements: dict[str, str] | None = None,
    phase5_overview_replacements: dict[str, str] | None = None,
    sprint5_overview_replacements: dict[str, str] | None = None,
    readme_replacements: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    setup_doc, phase5_overview, sprint5_overview, readme = _write_gate_docs(
        evidence_dir
    )
    if setup_doc_replacements:
        _replace_text(setup_doc, setup_doc_replacements)
    if phase5_overview_replacements:
        _replace_text(phase5_overview, phase5_overview_replacements)
    if sprint5_overview_replacements:
        _replace_text(sprint5_overview, sprint5_overview_replacements)
    if readme_replacements:
        _replace_text(readme, readme_replacements)
    env = os.environ.copy()
    env["DTXWIKI_EVIDENCE_DIR"] = str(evidence_dir)
    env["DTXWIKI_SETUP_DOC"] = str(setup_doc)
    env["DTXWIKI_PHASE5_OVERVIEW"] = str(phase5_overview)
    env["DTXWIKI_SPRINT5_OVERVIEW"] = str(sprint5_overview)
    env["DTXWIKI_README"] = str(readme)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        cwd=evidence_dir,
        text=True,
        capture_output=True,
        env=env,
    )


def _replace_text(path: Path, replacements: dict[str, str]) -> None:
    content = path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        content = content.replace(old, new)
    path.write_text(content, encoding="utf-8")


def _write_gate_docs(root: Path) -> tuple[Path, Path, Path, Path]:
    setup_doc = root / "remote-mcp-clients.md"
    setup_doc.write_text(
        "\n".join(
            [
                "# Remote HTTP MCP Client Setup",
                "",
                "Transport: Streamable HTTP only.",
                "Authentication: PAT bearer or OAuth.",
                "",
                "## 0. HTTP-only Support Boundary",
                "## 1. Prerequisites",
                "## 2. Claude Code",
                "Linux paths ~/.claude.json and ~/.codex/config.toml.",
                "Windows Codex path %USERPROFILE%\\.codex\\config.toml.",
                (
                    "Manual remote evidence includes DTXWIKI_REMOTE_SMOKE_MODE=remote, "
                    "DTXWIKI_REMOTE_CLIENT=codex-oauth-harness, and "
                    "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok."
                ),
                (
                    "Manual client smoke evidence goes in "
                    "client-smoke-log.md; "
                    "elapsed_to_list_tools must be <= 30m."
                ),
                (
                    "Use external dev PC notes and validate with "
                    "validate_client_smoke_log.py."
                ),
                "## 3. Codex CLI",
                "Use dtxwiki mcp register-http and dtxwiki mcp doctor --http.",
                (
                    "claude mcp add --scope user --transport http "
                    "dtxwiki https://example.test/mcp"
                ),
                "## 4. Claude Desktop",
                "## 5. Validate the Connection",
                "",
            ]
        ),
        encoding="utf-8",
    )
    readme = root / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# dtxWiki",
                "",
                "## 원격 서버 배포와 팀 MCP 연결",
                "See doc/setup/remote-mcp-clients.md.",
                "Streamable HTTP endpoint authentication uses PAT or OAuth.",
                "DTXWIKI_WORKSPACE_SLUG is retired.",
                (
                    "dtxwiki mcp register-http "
                    "--url https://example.test/mcp --client codex"
                ),
                "dtxwiki mcp doctor --http https://example.test/mcp",
                "",
            ]
        ),
        encoding="utf-8",
    )
    phase5_overview = root / "Sprint5-Phase5-Overview.md"
    phase5_overview.write_text(
        "\n".join(
            [
                "# Sprint5 Phase5 Overview",
                "",
                "Claude Code Linux path: ~/.claude.json.",
                "Claude Code Windows path: %USERPROFILE%\\.claude.json.",
                "Codex Linux path: ~/.codex/config.toml.",
                "Codex Windows path: %USERPROFILE%\\.codex\\config.toml.",
                (
                    "Remote smoke evidence requires DTXWIKI_REMOTE_SMOKE_MODE=remote, "
                    "DTXWIKI_REMOTE_CLIENT=<label>, and "
                    "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok."
                ),
                (
                    "Manual log fields: pat_or_dcr_started_at and "
                    "elapsed_to_list_tools; full-file placeholder removal; "
                    "KST +09:00; external dev PC evidence."
                ),
                "Manual log validator: validate_client_smoke_log.py.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    sprint5_overview = root / "sprint5-overview.md"
    sprint5_overview.write_text(
        "\n".join(
            [
                "# Sprint5 Overview",
                "",
                "Gate B requires Required client smoke from a remote client path.",
                "localhost single-process tests cannot pass Gate B.",
                "Claude Code Linux path: ~/.claude.json.",
                "Claude Code Windows path: %USERPROFILE%\\.claude.json.",
                "Remote OAuth node: test_oauth_full_flow_tool_round_trip.",
                "Remote smoke wrapper: scripts/release_remote_smoke.sh.",
                "Remote harness marker: DTXWIKI_REMOTE_HARNESS=docker-compose.",
                "Remote transport marker: DTXWIKI_REMOTE_TRANSPORT=streamable.",
                "Remote TLS marker: DTXWIKI_REMOTE_TLS_VERIFY=ca-file.",
                "Remote teardown marker: DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok.",
                (
                    "Manual validator: validate_client_smoke_log.py; "
                    "elapsed_to_list_tools <= 30m."
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return setup_doc, phase5_overview, sprint5_overview, readme


def _write_evidence(
    root: Path,
    *,
    remote_result: str,
    remote_mode: str = "remote",
    remote_client: str | None = "codex-oauth-harness",
    remote_teardown: str | None = "ok",
    include_nodes: bool = True,
    cli_result: str = "52 passed in 9.39s",
    cli_test_files: tuple[str, ...] = CLI_TEST_FILES,
    security_result: str = "7 passed in 1.78s",
    remote_extra: list[str] | None = None,
    remote_node_status: str = "PASSED",
) -> None:
    cli_lines = [
        "DTXWIKI_CLI_TRANSPORT=streamable-http",
        *(f"DTXWIKI_CLI_TEST_FILE={path}" for path in cli_test_files),
        cli_result,
        "",
    ]
    (root / "cli.log").write_text("\n".join(cli_lines), encoding="utf-8")
    (root / "security-headers.log").write_text(
        f"{security_result}\n",
        encoding="utf-8",
    )
    remote_lines = [
        f"DTXWIKI_REMOTE_SMOKE_MODE={remote_mode}",
        "DTXWIKI_REMOTE_HARNESS=docker-compose",
        "DTXWIKI_REMOTE_ORIGIN=https://dtxwiki.test:8443",
        "DTXWIKI_REMOTE_TRANSPORT=streamable",
        "DTXWIKI_REMOTE_TLS_VERIFY=ca-file",
    ]
    if remote_client is not None:
        remote_lines.append(f"DTXWIKI_REMOTE_CLIENT={remote_client}")
    if include_nodes:
        remote_lines.extend(_remote_node_lines(status=remote_node_status))
    if remote_extra:
        remote_lines.extend(remote_extra)
    remote_lines.extend([remote_result, ""])
    if remote_teardown is not None:
        remote_lines.extend([f"DTXWIKI_REMOTE_COMPOSE_TEARDOWN={remote_teardown}", ""])
    (root / "remote-smoke.log").write_text(
        "\n".join(remote_lines),
        encoding="utf-8",
    )
    (root / "client-smoke-log.md").write_text(
        _valid_client_log(),
        encoding="utf-8",
    )


def _valid_client_log() -> str:
    rows = [
        _row("Claude Code", "2.1.74", "PAT bearer (<redacted>)"),
        _row("Claude Code", "2.1.74", "dtxWiki-issued OAuth (<redacted>)"),
        _row("Codex CLI", "codex-cli 0.130.0", "PAT bearer (<redacted>)"),
        _row(
            "Codex CLI",
            "codex-cli 0.130.0",
            "dtxWiki-issued OAuth harness (<redacted>)",
        ),
    ]
    return "\n".join(
        [
            "# Phase 5 Client Smoke Log",
            "",
            "## Required Client/Auth Rows",
            "",
            HEADER,
            SEPARATOR,
            *rows,
            "",
        ]
    )


def _remote_node_lines(*, status: str = "PASSED") -> list[str]:
    names = [
        "test_pat_immediate_connect_list_tools",
        "test_pat_read_page_round_trip",
        "test_pat_write_agent_page_draft_round_trip",
        "test_oauth_full_flow_tool_round_trip",
        "test_oauth_refresh_rotation_and_reuse_denied_via_jti",
        "test_oauth_revoke_invalidates_access_jti",
        "test_cross_workspace_denied_with_keyword",
        "test_restricted_library_denied_with_keyword",
    ]
    return [f"tests/e2e/remote_mcp_smoke.py::{name} {status}" for name in names]


def _row(client: str, version: str, auth_path: str) -> str:
    return (
        f"| {client} | {version} | {auth_path} | "
        "2026-05-18T12:00:00+09:00 | "
        "2026-05-18T12:05:00+09:00 | "
        "2026-05-18T12:09:00+09:00 | "
        "09:00 | "
        "OK count=12 first3=read_page,read,search | "
        "OK slug=phase5-remote-page snippet=Remote smoke | "
        "OK draft_id=<redacted> | "
        '403 keyword="workspace" | '
        '403 keyword="restricted_library_denied" | '
        "external dev PC |"
    )
