"""HTTP registration overwrite notices, shared by PAT and OAuth setup."""

import json

from typer.testing import CliRunner

from backend.cli import main as cli_main
from backend.cli import mcp_http_register as register

URL = "https://wiki.example.com/mcp"


def test_replacing_a_same_named_registration_is_announced(tmp_path, monkeypatch):
    target = tmp_path / ".claude.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"dtxwiki": {"type": "http", "url": "https://old/mcp"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)

    result = register.register_http_server(url=URL, client="claude-code", token=None)

    assert result.replaced_registration == "https://old/mcp"
    assert "already registers an MCP server named 'dtxwiki'" in result.setup_text
    assert "https://old/mcp" in result.setup_text


def test_the_overwrite_warning_reaches_the_terminal(tmp_path, monkeypatch):
    """DEPLOYMENT.md promises the operator sees this; assert the CLI prints it."""

    target = tmp_path / ".claude.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"dtxwiki": {"type": "http", "url": "https://old/mcp"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        cli_main.app,
        ["mcp", "register-http", "--url", URL, "--client", "claude-code"],
    )

    assert result.exit_code == 0, result.stderr
    assert "already registers an MCP server named 'dtxwiki'" in result.stdout
    assert "https://old/mcp" in result.stdout


def test_registering_an_unused_name_announces_no_replacement(tmp_path, monkeypatch):
    target = tmp_path / ".claude.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"url": "https://old/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)

    result = register.register_http_server(url=URL, client="claude-code", token=None)

    assert result.replaced_registration is None
    assert "already registers" not in result.setup_text


def test_dry_run_warns_before_the_overwrite_rather_than_after(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(
        '[mcp_servers.dtxwiki]\nurl = "https://old/mcp"\n', encoding="utf-8"
    )
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = register.register_http_server(
        url=URL, client="codex", token=None, dry_run=True
    )

    assert result.replaced_registration == "https://old/mcp"
    assert "will replace it" in result.setup_text
    assert 'url = "https://old/mcp"' in target.read_text(encoding="utf-8")
