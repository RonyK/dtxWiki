import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import engine_entry


def test_desktop_engine_rejects_non_loopback_host():
    with pytest.raises(ValueError, match="loopback"):
        asyncio.run(engine_entry._serve("0.0.0.0", 21080))


def test_desktop_engine_arguments_require_port(monkeypatch):
    arguments = engine_entry._serve_arguments(["--port", "23456"])
    assert arguments.host == "127.0.0.1"
    assert arguments.port == 23456


@pytest.mark.parametrize("frozen", [False, True])
def test_desktop_engine_manages_worker_and_watches_parent(
    monkeypatch, tmp_path, frozen
):
    application = object()
    calls = {}
    threads = []
    monkeypatch.setattr(engine_entry.sys, "frozen", frozen, raising=False)
    # A shell configured for a split systemd deployment must not disable the
    # local Desktop worker.
    monkeypatch.setenv("DTXWIKI_WORKER_MODE", "external")
    monkeypatch.setattr(engine_entry, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(engine_entry, "generate_jwt_key", lambda path: None)
    monkeypatch.setattr(engine_entry, "create_app", lambda **kwargs: application)
    monkeypatch.setattr(
        engine_entry, "bootstrap", lambda *args: calls.update(bootstrapped=args)
    )

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            calls["watcher_started"] = True

    async def serve_runtime(app, settings, **kwargs):
        calls.update(app=app, settings=settings, options=kwargs)
        kwargs["bootstrap"]()
        kwargs["on_server"](server)

    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(engine_entry.threading, "Thread", Thread)
    monkeypatch.setattr(engine_entry, "serve_runtime", serve_runtime)

    asyncio.run(engine_entry._serve("127.0.0.1", 23456))

    assert calls["app"] is application
    assert calls["settings"].worker_mode == "managed"
    assert calls["settings"].backend_port == 23456
    assert calls["bootstrapped"] == (calls["settings"], application)
    assert calls["options"]["worker_command"] == (
        [engine_entry.sys.executable, "worker"] if frozen else None
    )
    assert calls["watcher_started"] is True
    assert threads[0]["target"] is engine_entry._watch_parent
    assert threads[0]["args"] == (server,)


@pytest.mark.parametrize("parent_input", ["shutdown\n", ""])
def test_desktop_parent_shutdown_requests_managed_runtime_exit(
    monkeypatch, parent_input
):
    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(engine_entry.sys, "stdin", io.StringIO(parent_input))

    engine_entry._watch_parent(server)

    assert server.should_exit is True


def test_frozen_worker_operation_does_not_start_another_server(monkeypatch):
    from backend.worker import managed

    calls = []
    monkeypatch.setattr(engine_entry.sys, "argv", ["dtxwiki-engine", "worker"])
    monkeypatch.setattr(managed, "main", lambda: calls.append("worker"))
    monkeypatch.setattr(
        engine_entry, "_serve_arguments", lambda args: pytest.fail("server started")
    )

    engine_entry.main()

    assert calls == ["worker"]


@pytest.mark.parametrize(
    ("operation", "module_name", "argument"),
    [
        ("converter-host", "converter_host", "--converter"),
        ("preview-host", "preview_host", "--provider"),
    ],
)
def test_frozen_provider_operations_preserve_host_arguments(
    monkeypatch, operation, module_name, argument
):
    import importlib

    host = importlib.import_module(f"backend.worker.{module_name}")
    seen = []
    monkeypatch.setattr(host, "main", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(
        engine_entry.sys,
        "argv",
        ["dtxwiki-engine", operation, argument, "allowlisted.id"],
    )

    with pytest.raises(SystemExit) as exited:
        engine_entry.main()

    assert exited.value.code == 7
    assert seen == [[argument, "allowlisted.id"]]


def test_desktop_mcp_inventory_reports_capabilities(monkeypatch):
    from backend.cli.integrations import detect

    monkeypatch.setattr(
        detect,
        "detect_clients",
        lambda: [
            {
                "name": "codex",
                "detected": True,
                "config_path": "C:/Users/test/.codex/config.toml",
            }
        ],
    )
    inventory = engine_entry._client_inventory()
    assert inventory == {
        "clients": [
            {
                "id": "codex",
                "detected": True,
                "configPath": "C:/Users/test/.codex/config.toml",
            }
        ]
    }


def test_desktop_mcp_registers_multiple_saved_servers_after_validation(monkeypatch):
    from backend.cli import mcp_http_register

    calls = []

    def fake_register(**kwargs):
        calls.append(kwargs)
        return mcp_http_register.RegisterHttpResult(
            client=kwargs["client"],
            config_path=Path("config.json"),
            backup_path=None,
            setup_text="redacted setup",
            command=None,
            wrote_config=not kwargs.get("dry_run", False),
        )

    monkeypatch.setattr(mcp_http_register, "register_http_server", fake_register)
    result = engine_entry._register_mcp(
        {
            "auth": "oauth",
            "clients": ["codex", "gemini-cli"],
            "servers": [
                {
                    "url": "https://one.example/mcp",
                    "mcpName": "dtxwiki-one",
                },
                {
                    "url": "https://two.example/mcp",
                    "mcpName": "dtxwiki-two",
                },
            ],
        }
    )

    assert [call.get("dry_run", False) for call in calls] == [
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert len(result["registrations"]) == 4
    assert all(item["wrote_config"] for item in result["registrations"])
    assert result["config_writes_complete"] is True
    assert result["configuration_failures"] == []
    assert all(call["allow_insecure_local"] is True for call in calls)


def test_desktop_mcp_rejects_one_pat_for_multiple_servers():
    with pytest.raises(ValueError, match="exactly one server"):
        engine_entry._register_mcp(
            {
                "auth": "pat",
                "clients": ["codex"],
                "servers": [
                    {"url": "https://one.example/mcp", "mcpName": "one"},
                    {"url": "https://two.example/mcp", "mcpName": "two"},
                ],
                "token": "secret",
            }
        )


def test_desktop_mcp_requires_a_saved_server():
    with pytest.raises(ValueError, match="saved server"):
        engine_entry._register_mcp(
            {
                "auth": "oauth",
                "clients": ["codex"],
                "servers": [],
            }
        )


@pytest.mark.parametrize("client", ["claude-desktop", "cursor"])
def test_desktop_mcp_rejects_clients_without_http_config_support(client):
    with pytest.raises(ValueError, match="unsupported desktop MCP client"):
        engine_entry._register_mcp(
            {
                "auth": "oauth",
                "clients": [client],
                "servers": [{"url": "https://one.example/mcp", "mcpName": "one"}],
            }
        )


def test_desktop_mcp_rejects_tokenless_plaintext_before_preflight(monkeypatch):
    from backend.cli import mcp_http_register

    calls = []
    monkeypatch.setattr(
        mcp_http_register,
        "register_http_server",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(ValueError, match="OAuth registration requires HTTPS"):
        engine_entry._register_mcp(
            {
                "auth": "oauth",
                "clients": ["codex"],
                "servers": [
                    {
                        "url": "http://127.0.0.1:21080/mcp",
                        "mcpName": "dtxwiki-local",
                    }
                ],
            }
        )

    assert calls == []


def test_desktop_mcp_accepts_tokenless_oauth_for_https(monkeypatch):
    from backend.cli import mcp_http_register

    calls = []

    def fake_register(**kwargs):
        calls.append(kwargs)
        return mcp_http_register.RegisterHttpResult(
            client=kwargs["client"],
            config_path=Path("config.toml"),
            backup_path=None,
            setup_text="OAuth setup",
            command=None,
            wrote_config=not kwargs.get("dry_run", False),
        )

    monkeypatch.setattr(mcp_http_register, "register_http_server", fake_register)

    result = engine_entry._register_mcp(
        {
            "auth": "oauth",
            "clients": ["codex"],
            "servers": [
                {
                    "url": "https://wiki.example.com/mcp",
                    "mcpName": "dtxwiki-team",
                }
            ],
        }
    )

    assert len(calls) == 2
    assert result["setup_required"] is False
    assert result["secret_env_names"] == []


@pytest.mark.parametrize(
    ("auth", "token", "message"),
    [
        ("pat", None, "non-empty access token"),
        ("oauth", "secret", "must not include"),
        ("unknown", None, "either oauth or pat"),
    ],
)
def test_desktop_mcp_rejects_inconsistent_auth_payload(auth, token, message):
    with pytest.raises(ValueError, match=message):
        engine_entry._register_mcp(
            {
                "auth": auth,
                "clients": ["codex"],
                "servers": [
                    {
                        "url": "https://wiki.example.com/mcp",
                        "mcpName": "dtxwiki-team",
                    }
                ],
                "token": token,
            }
        )


def test_desktop_mcp_aggregates_required_secret_environment_names(monkeypatch):
    from backend.cli import mcp_http_register

    def fake_register(**kwargs):
        return mcp_http_register.RegisterHttpResult(
            client=kwargs["client"],
            config_path=Path(f"{kwargs['client']}.config"),
            backup_path=None,
            setup_text="PAT setup",
            command=None,
            wrote_config=not kwargs.get("dry_run", False),
            secret_env_names=(
                ("DTXWIKI_MCP_PAT",)
                if kwargs["client"] in {"codex", "gemini-cli"}
                else ()
            ),
        )

    monkeypatch.setattr(mcp_http_register, "register_http_server", fake_register)

    result = engine_entry._register_mcp(
        {
            "auth": "pat",
            "clients": ["codex", "gemini-cli", "claude-code"],
            "servers": [
                {
                    "url": "https://wiki.example.com/mcp",
                    "mcpName": "dtxwiki-team",
                }
            ],
            "token": "dtxw-private-token",
        }
    )

    assert result["setup_required"] is True
    assert result["secret_env_names"] == ["DTXWIKI_MCP_PAT"]
    assert all(
        "dtxw-private-token" not in str(item) for item in result["registrations"]
    )


def test_desktop_mcp_reports_real_codex_and_gemini_pat_setup(
    tmp_path,
    monkeypatch,
):
    from backend.cli import mcp_http_register

    codex_config = tmp_path / "codex.toml"
    gemini_config = tmp_path / "gemini.json"
    monkeypatch.setattr(
        mcp_http_register,
        "codex_http_config_path",
        lambda: codex_config,
    )
    monkeypatch.setattr(
        mcp_http_register,
        "gemini_cli_http_config_path",
        lambda: gemini_config,
    )

    result = engine_entry._register_mcp(
        {
            "auth": "pat",
            "clients": ["codex", "gemini-cli"],
            "servers": [
                {
                    "url": "https://wiki.example.com/mcp",
                    "mcpName": "dtxwiki-team",
                }
            ],
            "token": "dtxw-private-token",
        }
    )

    assert result["setup_required"] is True
    assert result["secret_env_names"] == ["DTXWIKI_MCP_PAT"]
    assert "dtxw-private-token" not in codex_config.read_text(encoding="utf-8")
    assert "dtxw-private-token" not in gemini_config.read_text(encoding="utf-8")
    assert "DTXWIKI_MCP_PAT" in codex_config.read_text(encoding="utf-8")
    assert "DTXWIKI_MCP_PAT" in gemini_config.read_text(encoding="utf-8")


def test_desktop_mcp_reports_config_failure_and_excludes_its_secret_setup(
    monkeypatch,
):
    from backend.cli import mcp_http_register

    def fake_register(**kwargs):
        dry_run = kwargs.get("dry_run", False)
        write_succeeded = not dry_run and kwargs["client"] == "claude-code"
        return mcp_http_register.RegisterHttpResult(
            client=kwargs["client"],
            config_path=Path(f"{kwargs['client']}.config"),
            backup_path=None,
            setup_text="PAT setup",
            command=None,
            wrote_config=write_succeeded,
            fallback_reason=(
                "config patch failed: permission denied"
                if not dry_run and not write_succeeded
                else None
            ),
            secret_env_names=(
                ("DTXWIKI_MCP_PAT",) if kwargs["client"] == "codex" else ()
            ),
        )

    monkeypatch.setattr(mcp_http_register, "register_http_server", fake_register)

    result = engine_entry._register_mcp(
        {
            "auth": "pat",
            "clients": ["claude-code", "codex"],
            "servers": [
                {
                    "url": "https://wiki.example.com/mcp",
                    "mcpName": "dtxwiki-team",
                }
            ],
            "token": "dtxw-private-token",
        }
    )

    assert result["config_writes_complete"] is False
    assert result["configuration_failures"] == [
        {
            "client": "codex",
            "config_path": "codex.config",
            "reason": "config patch failed: permission denied",
        }
    ]
    assert result["setup_required"] is False
    assert result["secret_env_names"] == []
