import json
import re
import shutil
from pathlib import Path

import pytest
import tomlkit
from typer.testing import CliRunner

from backend.catalog.mcp_apps import CatalogRepository
from backend.cli import main as cli_main
from backend.cli import mcp_http_register as register


def test_register_http_claude_code_emits_command(tmp_path, monkeypatch):
    target = tmp_path / ".claude.json"
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "claude-code",
            "--token",
            "dtxw_secret",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "claude mcp add --scope user --transport http dtxwiki" in result.stdout
    assert "Authorization: Bearer <redacted>" in result.stdout
    assert "dtxw_secret" not in result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"]["dtxwiki"]["type"] == "http"
    assert data["mcpServers"]["dtxwiki"]["url"] == "https://wiki.example.com/mcp"
    assert "oauth" not in data["mcpServers"]["dtxwiki"]
    assert data["mcpServers"]["dtxwiki"]["headers"]["Authorization"] == (
        "Bearer dtxw_secret"
    )


def test_register_http_claude_code_creates_backup(tmp_path, monkeypatch):
    target = tmp_path / ".claude.json"
    target.write_text('{"mcpServers": {"other": {"url": "x"}}}\n', encoding="utf-8")
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "claude-code",
        ],
    )

    assert result.exit_code == 0, result.stderr
    backups = list(tmp_path.glob(".claude.json.bak.*"))
    assert len(backups) == 1
    assert re.match(r"\.claude\.json\.bak\.\d{8}-\d{4}", backups[0].name)


def test_register_http_codex_writes_config_toml(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.stderr
    doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    block = doc["mcp_servers"]["dtxwiki"]
    assert block["url"] == "https://wiki.example.com/mcp"
    assert "transport" not in block
    assert "bearer_token_env_var" not in block
    assert "codex mcp add dtxwiki --url https://wiki.example.com/mcp" in (result.stdout)


def test_register_http_accepts_several_named_servers(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://one.example.com/mcp",
            "--name",
            "wiki-one",
            "--url",
            "https://two.example.com/mcp",
            "--name",
            "wiki-two",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.stderr
    doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["wiki-one"]["url"] == "https://one.example.com/mcp"
    assert doc["mcp_servers"]["wiki-two"]["url"] == "https://two.example.com/mcp"
    assert "codex mcp add wiki-one" in result.stdout
    assert "codex mcp add wiki-two" in result.stdout


def test_register_http_rejects_duplicate_normalized_names_before_batch_write(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://one.example.com/mcp",
            "--name",
            "wiki",
            "--url",
            "https://two.example.com/mcp",
            "--name",
            " wiki ",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 1
    assert "--name values must be unique after normalization" in result.stderr
    assert not target.exists()


def test_register_http_codex_creates_backup(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(
        (
            "[mcp_servers.other]\n"
            'url = "https://other/mcp"\n'
            'bearer_token_env_var = "OTHER_PAT"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert list(tmp_path.glob("config.toml.bak.*"))


def test_register_http_codex_does_not_embed_plain_pat(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
            "--token",
            "dtxw_plain_secret",
        ],
    )

    assert result.exit_code == 0, result.stderr
    text = target.read_text(encoding="utf-8")
    assert "dtxw_plain_secret" not in text
    assert 'bearer_token_env_var = "DTXWIKI_MCP_PAT"' in text
    assert "Authorization PAT was not written" in result.stderr


def test_register_http_gemini_writes_http_url_with_environment_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / ".gemini" / "settings.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps(
            {
                "theme": "system",
                "mcpServers": {"other": {"httpUrl": "https://other.example/mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "gemini_cli_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "gemini-cli",
            "--name",
            "team-wiki",
            "--token",
            "dtxw_gemini_secret",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "gemini mcp add --scope user --transport http" in result.stdout
    assert "Authorization: Bearer $DTXWIKI_MCP_PAT" in result.stdout
    assert "dtxw_gemini_secret" not in result.stdout
    assert "Authorization PAT was not written" in result.stderr
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["theme"] == "system"
    assert data["mcpServers"]["other"]["httpUrl"] == ("https://other.example/mcp")
    assert data["mcpServers"]["team-wiki"] == {
        "httpUrl": "https://wiki.example.com/mcp",
        "headers": {
            "Authorization": "Bearer $DTXWIKI_MCP_PAT",
        },
    }
    assert "dtxw_gemini_secret" not in target.read_text(encoding="utf-8")


def test_register_http_gemini_oauth_omits_headers(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(register, "gemini_cli_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "gemini-cli",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["dtxwiki"] == {
        "httpUrl": "https://wiki.example.com/mcp"
    }


def test_register_http_gemini_rejects_underscore_names_before_batch_write(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "settings.json"
    original = '{"mcpServers": {"existing": {}}}\n'
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(register, "gemini_cli_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://one.example.com/mcp",
            "--name",
            "wiki-one",
            "--url",
            "https://two.example.com/mcp",
            "--name",
            "wiki_two",
            "--client",
            "gemini-cli",
        ],
    )

    assert result.exit_code == 1
    assert "must not contain underscores" in result.stderr
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("invalid_name", "message"),
    [
        ("x" * 129, "must contain 1 to 128 characters"),
        ("wiki\nunsafe", "contains unsafe control characters"),
    ],
)
def test_register_http_rejects_late_invalid_names_before_batch_write(
    invalid_name,
    message,
    tmp_path,
    monkeypatch,
):
    target = tmp_path / ".claude.json"
    original = '{"mcpServers": {"existing": {}}}\n'
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(register, "claude_code_http_config_path", lambda: target)
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://one.example.com/mcp",
            "--name",
            "wiki-one",
            "--url",
            "https://two.example.com/mcp",
            "--name",
            invalid_name,
            "--client",
            "claude-code",
        ],
    )

    assert result.exit_code == 1
    assert message in result.stderr
    assert target.read_text(encoding="utf-8") == original


def test_register_http_antigravity_normalizes_null_and_warns_for_plain_pat(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / ".gemini" / "config" / "mcp_config.json"
    target.parent.mkdir(parents=True)
    target.write_text("null\n", encoding="utf-8")
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            "--token",
            "dtxw_antigravity_secret",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert '"serverUrl": "https://wiki.example.com/mcp"' in result.stdout
    assert '"Authorization": "Bearer <redacted>"' in result.stdout
    assert "agy mcp add" not in result.stdout
    assert "dtxw_antigravity_secret" not in result.stdout
    assert "PAT was written to the local client config" in result.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "dtxwiki": {
                "serverUrl": "https://wiki.example.com/mcp",
                "headers": {
                    "Authorization": "Bearer dtxw_antigravity_secret",
                },
            }
        }
    }
    backups = list(target.parent.glob("mcp_config.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "null\n"


def test_register_http_antigravity_reads_pat_from_environment_without_output_leak(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "mcp_config.json"
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)
    monkeypatch.setenv("DTXWIKI_MCP_PAT", "dtxw_environment_secret")

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            "--token-env",
            "DTXWIKI_MCP_PAT",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "dtxw_environment_secret" not in result.stdout
    assert "dtxw_environment_secret" not in result.stderr
    assert (
        json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["dtxwiki"][
            "headers"
        ]["Authorization"]
        == "Bearer dtxw_environment_secret"
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--token", "direct-secret", "--token-env", "DTXWIKI_MCP_PAT"],
            "either --token or --token-env",
        ),
        (["--token-env", "missing"], "must be an uppercase environment name"),
        (["--token-env", "MISSING_PAT"], "is missing or empty"),
    ],
)
def test_register_http_rejects_unsafe_token_environment_inputs(
    tmp_path,
    monkeypatch,
    arguments,
    message,
):
    target = tmp_path / "mcp_config.json"
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)
    monkeypatch.delenv("DTXWIKI_MCP_PAT", raising=False)
    monkeypatch.delenv("MISSING_PAT", raising=False)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            *arguments,
        ],
    )

    assert result.exit_code == 1
    assert message in result.stderr
    assert not target.exists()


def test_register_http_antigravity_preserves_siblings_and_custom_name(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "mcp_config.json"
    target.write_text(
        json.dumps(
            {
                "ui": {"compact": True},
                "mcpServers": {"other": {"serverUrl": "https://other.example/mcp"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            "--name",
            "team-wiki",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert (
        "agy mcp add --type http team-wiki https://wiki.example.com/mcp"
        in result.stdout
    )
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["ui"] == {"compact": True}
    assert data["mcpServers"]["other"] == {"serverUrl": "https://other.example/mcp"}
    assert data["mcpServers"]["team-wiki"] == {
        "serverUrl": "https://wiki.example.com/mcp"
    }

    repeated = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            "--name",
            "team-wiki",
        ],
    )
    assert repeated.exit_code == 0, repeated.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == data


@pytest.mark.parametrize(
    ("original", "message"),
    [
        ('["not", "an", "object"]\n', "config root must be a JSON object"),
        ("{broken\n", "Expecting property name enclosed in double quotes"),
        ('{"mcpServers": []}\n', "mcpServers must be a JSON object"),
    ],
)
def test_register_http_antigravity_rejects_invalid_config_without_overwrite(
    tmp_path,
    monkeypatch,
    original,
    message,
):
    target = tmp_path / "mcp_config.json"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert f"config patch failed: {message}" in result.stderr
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("existing", [None, ""])
def test_register_http_antigravity_creates_missing_or_empty_config(
    tmp_path,
    monkeypatch,
    existing,
):
    target = tmp_path / "config" / "mcp_config.json"
    if existing is not None:
        target.parent.mkdir(parents=True)
        target.write_text(existing, encoding="utf-8")
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "dtxwiki": {"serverUrl": "https://wiki.example.com/mcp"},
        }
    }


def test_register_http_antigravity_exposes_native_oauth_command_and_keeps_writer(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "mcp_config.json"
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = register.register_http_server(
        url="https://wiki.example.com/mcp",
        client="antigravity",
        token=None,
        dry_run=True,
    )

    assert result.command == (
        "agy mcp add --type http dtxwiki https://wiki.example.com/mcp"
    )
    assert result.setup_text.startswith(f"Register the server:\n{result.command}")
    assert "/mcp" in result.setup_text
    assert not target.exists()

    pat = register.register_http_server(
        url="https://wiki.example.com/mcp",
        client="antigravity",
        token="dtxw_test_secret",
        dry_run=True,
    )
    assert pat.command is None
    assert "agy mcp add" not in pat.setup_text
    assert "Bearer <redacted>" in pat.setup_text
    assert "dtxw_test_secret" not in pat.setup_text


def test_register_http_dry_run_reports_hypothetical_backup_without_creating_it(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "mcp_config.json"
    target.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(register, "antigravity_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "antigravity",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "dry-run: would back up" in result.stdout
    assert "\nbackup:" not in result.stdout
    assert not list(tmp_path.glob("mcp_config.json.bak.*"))


def test_register_http_echo_only_when_config_unwritable(monkeypatch):
    def fail_write(**_kwargs):
        raise PermissionError("unwritable")

    monkeypatch.setattr(register, "_write_config", fail_write)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "claude-code",
        ],
    )

    assert result.exit_code == 0
    assert "config patch failed" in result.stderr
    assert "claude mcp add" in result.stdout


def test_register_http_dry_run_no_filesystem_change(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "dry-run" in result.stdout
    assert not target.exists()


def test_register_http_windows_uses_home_dot_codex_path(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "alice"
    monkeypatch.setattr(register.sys, "platform", "win32")
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert (home / ".codex" / "config.toml").is_file()


def test_register_http_prefers_codex_home_on_windows(tmp_path, monkeypatch):
    codex_home = tmp_path / "custom-codex-home"
    fallback_home = tmp_path / "Users" / "alice"
    monkeypatch.setattr(register.sys, "platform", "win32")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HOME", str(fallback_home))
    monkeypatch.setenv("USERPROFILE", str(fallback_home))

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert (codex_home / "config.toml").is_file()
    assert not (fallback_home / ".codex" / "config.toml").exists()


def test_register_http_claude_code_windows_userprofile_path(tmp_path, monkeypatch):
    userprofile = tmp_path / "Users" / "alice"
    monkeypatch.setattr(register.sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setattr(register.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--client",
            "claude-code",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert (userprofile / ".claude.json").is_file()


def test_register_http_rejects_non_https_remote_url(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "http://wiki.example.com/mcp",
            "--client",
            "codex",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "https://" in result.stderr
    assert not target.exists()


def test_register_http_allows_insecure_loopback_only_with_explicit_flag(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    rejected = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "http://127.0.0.1:21080/mcp",
            "--client",
            "codex",
            "--dry-run",
        ],
    )
    assert rejected.exit_code == 1

    rejected_remote = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "http://wiki.example.com/mcp",
            "--client",
            "codex",
            "--dry-run",
            "--allow-insecure-local",
        ],
    )
    assert rejected_remote.exit_code == 1

    rejected_tokenless = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "http://127.0.0.1:21080/mcp",
            "--client",
            "codex",
            "--dry-run",
            "--allow-insecure-local",
        ],
    )
    assert rejected_tokenless.exit_code == 1
    assert "loopback HTTP registration requires a PAT" in rejected_tokenless.stderr

    accepted = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "http://127.0.0.1:21080/mcp",
            "--client",
            "codex",
            "--token",
            "dtxw_secret",
            "--dry-run",
            "--allow-insecure-local",
        ],
    )

    assert accepted.exit_code == 0, accepted.stderr
    assert "dry-run" in accepted.stdout


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.2:21080/mcp",
        "http://localhost.:21080/mcp",
    ],
)
def test_register_http_accepts_shared_loopback_forms(
    url,
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            url,
            "--client",
            "codex",
            "--token",
            "dtxw_secret",
            "--dry-run",
            "--allow-insecure-local",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "dry-run" in result.stdout


def test_register_http_batch_rejects_tokenless_loopback_before_writing(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "https://wiki.example.com/mcp",
            "--name",
            "remote",
            "--url",
            "http://127.0.0.1:21080/mcp",
            "--name",
            "local",
            "--client",
            "codex",
            "--allow-insecure-local",
        ],
    )

    assert result.exit_code == 1
    assert "loopback HTTP registration requires a PAT" in result.stderr
    assert not target.exists()


def test_register_http_normalizes_prefixed_endpoint_and_trailing_slash(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            "  https://wiki.example.com/dtx/mcp/  ",
            "--client",
            "codex",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "https://wiki.example.com/dtx/mcp" in result.stdout
    assert "https://wiki.example.com/dtx/mcp/" not in result.stdout


@pytest.mark.parametrize(
    ("invalid_url", "message", "secret"),
    [
        ("https:///mcp", "with a host", None),
        (
            "https://alice:embedded-secret@wiki.example.com/mcp",
            "embedded credentials",
            "embedded-secret",
        ),
        (
            "https://wiki.example.com/mcp?token=query-secret",
            "query string",
            "query-secret",
        ),
        (
            "https://wiki.example.com/mcp#fragment-secret",
            "fragment",
            "fragment-secret",
        ),
        ("https://wiki.example.com/api/mcp-tools", "exact segment /mcp", None),
        ("https://wiki.example.com:invalid/mcp", "malformed", None),
    ],
)
def test_register_http_rejects_unsafe_endpoint_urls_without_echoing_them(
    invalid_url,
    message,
    secret,
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(register, "codex_http_config_path", lambda: target)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "register-http",
            "--url",
            invalid_url,
            "--client",
            "codex",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert message in result.stderr
    assert invalid_url not in result.stdout
    assert invalid_url not in result.stderr
    if secret is not None:
        assert secret not in result.stdout
        assert secret not in result.stderr
    assert not target.exists()


def test_manual_command_uses_catalog_for_custom_name_and_oauth_sequence():
    claude = register.manual_command(
        url="https://wiki.example.com/mcp",
        client="claude-code",
        token=None,
        name="team-wiki",
    )
    codex = register.manual_command(
        url="https://wiki.example.com/mcp",
        client="codex",
        token=None,
        name="team-wiki",
    )
    antigravity = register.manual_command(
        url="https://wiki.example.com/mcp",
        client="antigravity",
        token=None,
        name="team-wiki",
    )

    assert claude.splitlines() == [
        "claude mcp add --scope user --transport http team-wiki "
        "https://wiki.example.com/mcp",
        "claude mcp login team-wiki",
    ]
    assert "--oauth" not in claude
    assert codex.splitlines() == [
        "codex mcp add team-wiki --url https://wiki.example.com/mcp",
        "codex mcp login team-wiki",
    ]
    # Antigravity signs in from its own UI, so `manual_command` stays
    # shell-only while the full setup text carries the in-app step.
    assert antigravity == (
        "agy mcp add --type http team-wiki https://wiki.example.com/mcp"
    )
    antigravity_setup, _command, _env = register._render_cli_setup(
        url="https://wiki.example.com/mcp",
        client="antigravity",
        token=None,
        name="team-wiki",
    )
    assert "/mcp (type this inside the app)" in antigravity_setup


def test_manual_command_rejects_tokenless_plaintext_loopback():
    with pytest.raises(ValueError, match="loopback HTTP registration requires a PAT"):
        register.manual_command(
            url="http://127.0.0.1:21080/mcp",
            client="codex",
            token=None,
            name="local-wiki",
        )


def _app_command_only_repository(tmp_path) -> CatalogRepository:
    source = Path(register.__file__).resolve().parents[1] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    recipe["artifacts"] = [
        {
            "kind": "copy-mcp-url",
            "step": "register",
            "label": {"en": "Copy MCP URL", "ko": "Copy MCP URL"},
            "value": [{"slot": "mcp-url"}],
        },
        {
            "kind": "app-command",
            "step": "authenticate",
            "value": [
                {"literal": "/mcp auth "},
                {"slot": "server-name"},
            ],
        },
    ]
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    return CatalogRepository(target)


def test_app_command_only_cli_setup_is_not_a_manual_shell_command(tmp_path):
    repository = _app_command_only_repository(tmp_path)

    setup_text, command_text, secret_env_names = register._render_cli_setup(
        url="https://wiki.example.com/mcp",
        client="gemini-cli",
        token=None,
        name="team-wiki",
        repository=repository,
    )

    # The register-step URL must not be dropped from the copy-everything
    # fallback just because it is not a shell command.
    assert setup_text == (
        "Register the server:\n"
        "Copy MCP URL: https://wiki.example.com/mcp\n"
        "Sign in:\n"
        "/mcp auth team-wiki (type this inside the app)\n"
        "Note: no automatic registration was performed; complete the steps "
        "above in the app."
    )
    assert command_text is None
    assert secret_env_names == ()
    with pytest.raises(ValueError, match="does not provide a shell command"):
        register.manual_command(
            url="https://wiki.example.com/mcp",
            client="gemini-cli",
            token=None,
            name="team-wiki",
            repository=repository,
        )


def test_cli_setup_preserves_instruction_before_command_order(tmp_path):
    source = Path(register.__file__).resolve().parents[1] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    recipe = next(
        item for item in payload["recipes"] if item["id"] == "gemini-cli-oauth-r1"
    )
    recipe["artifacts"].insert(
        0,
        {
            "kind": "instruction",
            "step": "register",
            "body": {"en": "Read this first.", "ko": "Read this first."},
        },
    )
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    setup_text, _command_text, _secret_env_names = register._render_cli_setup(
        url="https://wiki.example.com/mcp",
        client="gemini-cli",
        token=None,
        name="team-wiki",
        repository=CatalogRepository(target),
    )

    assert setup_text.index("Read this first.") < setup_text.index("gemini mcp add")


def test_manual_only_notice_depends_on_automatic_registration_artifacts(tmp_path):
    manual_setup, manual_command, _manual_env = register._render_cli_setup(
        url="https://wiki.example.com/mcp",
        client="gemini-cli",
        token=None,
        name="team-wiki",
        repository=_app_command_only_repository(tmp_path),
    )
    automatic_setup, automatic_command, _automatic_env = register._render_cli_setup(
        url="https://wiki.example.com/mcp",
        client="gemini-cli",
        token=None,
        name="team-wiki",
    )

    notice = "Note: no automatic registration was performed"
    assert notice in manual_setup
    assert manual_command is None
    assert notice not in automatic_setup
    assert automatic_command is not None


def test_ordinary_catalog_cli_app_renders_without_production_app_branch(tmp_path):
    source = Path(register.__file__).resolve().parents[1] / "catalog" / "mcp_apps"
    target = tmp_path / "catalog"
    shutil.copytree(source, target)
    catalog_path = target / "catalog.v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    app = json.loads(json.dumps(payload["apps"][1]))
    app.update(
        {
            "id": "synthetic-cli",
            "labels": {"en": "Synthetic CLI", "ko": "Synthetic CLI"},
            "aliases": ["fixture-cli"],
            "featuredRank": None,
        }
    )
    app["variants"] = app["variants"][:1]
    app["variants"][0]["id"] = "synthetic-cli-oauth"
    app["variants"][0]["recipeIds"] = ["synthetic-cli-oauth-r1"]
    recipe = json.loads(
        json.dumps(
            next(
                item
                for item in payload["recipes"]
                if item["id"] == "codex-cli-oauth-r1"
            )
        )
    )
    recipe["id"] = "synthetic-cli-oauth-r1"
    recipe["variantId"] = "synthetic-cli-oauth"
    payload["apps"].append(app)
    payload["recipes"].append(recipe)
    payload["catalogRevision"] = "2026-08-04.2"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    command = register.manual_command(
        url="https://wiki.example.com/mcp",
        client="synthetic-cli",
        token=None,
        name="custom-wiki",
        repository=CatalogRepository(target),
    )

    assert command.splitlines() == [
        "codex mcp add custom-wiki --url https://wiki.example.com/mcp",
        "codex mcp login custom-wiki",
    ]
