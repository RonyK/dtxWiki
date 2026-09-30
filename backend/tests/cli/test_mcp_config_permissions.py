"""MCP client configs carry bearer tokens: owner-only, backups included."""

from __future__ import annotations

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from backend.cli import main as cli_main
from backend.cli import mcp_http_register as register
from backend.cli.integrations import _common

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits are not portable to Windows"
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_atomic_write_enforces_a_requested_mode_over_a_loose_file(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o644)

    _common.atomic_write_text(target, '{"a": 1}\n', mode=0o600)

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_atomic_write_without_a_mode_still_preserves_operator_files(tmp_path):
    # The Caddyfile written by `dtxwiki setup production` must stay readable
    # by the caddy service.
    target = tmp_path / "Caddyfile"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)

    _common.atomic_write_text(target, "new\n")

    assert _mode(target) == 0o644


def _register_claude(tmp_path, monkeypatch, *, token: str | None):
    target = tmp_path / ".claude.json"
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)
    arguments = [
        "mcp",
        "register-http",
        "--url",
        "https://wiki.example.com/mcp",
        "--client",
        "claude-code",
    ]
    if token is not None:
        arguments += ["--token", token]
    result = CliRunner().invoke(cli_main.app, arguments)
    assert result.exit_code == 0, result.stderr
    return target


def test_registration_tightens_a_world_readable_config_and_its_backup(
    tmp_path, monkeypatch
):
    target = tmp_path / ".claude.json"
    target.write_text('{"mcpServers": {"other": {"url": "x"}}}\n', encoding="utf-8")
    target.chmod(0o644)

    _register_claude(tmp_path, monkeypatch, token="dtxw_secret")

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"]["dtxwiki"]["headers"]["Authorization"] == (
        "Bearer dtxw_secret"
    )
    assert _mode(target) == 0o600
    backups = list(tmp_path.glob(".claude.json.bak.*"))
    assert len(backups) == 1
    assert _mode(backups[0]) == 0o600


def test_registration_tightens_an_existing_loose_backup(tmp_path, monkeypatch):
    target = tmp_path / ".claude.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o600)
    stale_backup = tmp_path / ".claude.json.bak.20260101-0000"
    stale_backup.write_text('{"token": "dtxw_old"}\n', encoding="utf-8")
    stale_backup.chmod(0o644)
    # Backups are named per minute; pin the name so the stale one is reused.
    monkeypatch.setattr(register, "_backup_path", lambda _path: stale_backup)

    _register_claude(tmp_path, monkeypatch, token="dtxw_new")

    assert _mode(stale_backup) == 0o600


def test_new_config_is_created_owner_only(tmp_path, monkeypatch):
    target = _register_claude(tmp_path, monkeypatch, token="dtxw_secret")

    assert _mode(target) == 0o600
