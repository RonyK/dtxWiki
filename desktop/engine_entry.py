"""Frozen local-engine entry point used by the desktop application."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

import uvicorn

from backend.app.bootstrap import bootstrap
from backend.app.data_dir import data_dir
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.cli.prod_setup import generate_jwt_key


def _serve_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args(arguments)


def _read_request() -> dict[str, Any]:
    payload = sys.stdin.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("desktop MCP request is too large")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("desktop MCP request must be a JSON object")
    return value


def _client_inventory() -> dict[str, Any]:
    from backend.cli.integrations.detect import detect_clients

    return {
        "clients": [
            {
                "id": item["name"],
                "detected": item["detected"],
                "configPath": item["config_path"],
            }
            for item in detect_clients()
        ]
    }


def _result_json(result: Any) -> dict[str, Any]:
    value = asdict(result)
    for key in ("config_path", "backup_path"):
        value[key] = str(value[key]) if value[key] is not None else None
    return value


def _registration_auth(request: dict[str, Any], *, token: str | None) -> str:
    raw_auth = request.get("auth")
    if not isinstance(raw_auth, str):
        raise ValueError("desktop MCP auth must be either oauth or pat")
    auth = raw_auth.strip().lower()
    if auth not in {"oauth", "pat"}:
        raise ValueError("desktop MCP auth must be either oauth or pat")
    if auth == "pat" and token is None:
        raise ValueError("PAT registration requires a non-empty access token")
    if auth == "oauth" and token is not None:
        raise ValueError("OAuth registration must not include an access token")
    return auth


def _validate_desktop_auth_transport(
    servers: list[dict[str, Any]],
    *,
    auth: str,
) -> None:
    if auth == "oauth" and any(
        urlsplit(str(server.get("url", ""))).scheme.casefold() == "http"
        for server in servers
    ):
        raise ValueError(
            "OAuth registration requires HTTPS; plaintext loopback HTTP MCP "
            "endpoints require a PAT"
        )


def _register_mcp(request: dict[str, Any]) -> dict[str, Any]:
    from backend.cli.mcp_http_register import (
        local_http_client_ids,
        register_http_server,
    )

    clients = request.get("clients")
    if not isinstance(clients, list) or not clients:
        raise ValueError("select at least one MCP client")
    supported_clients = frozenset(local_http_client_ids())
    normalized_clients = []
    for client in clients:
        normalized = str(client).strip().lower()
        if normalized not in supported_clients:
            raise ValueError(f"unsupported desktop MCP client: {normalized}")
        if normalized not in normalized_clients:
            normalized_clients.append(normalized)

    servers = request.get("servers", [])
    if not isinstance(servers, list):
        raise ValueError("servers must be a list")
    if not servers:
        raise ValueError("select at least one saved server")

    token = request.get("token")
    if token is not None:
        token = str(token).strip() or None
    auth = _registration_auth(request, token=token)
    if auth == "pat" and len(servers) != 1:
        raise ValueError("PAT registration supports exactly one server at a time")

    typed_servers: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("each server must be an object")
        typed_servers.append(server)
    _validate_desktop_auth_transport(typed_servers, auth=auth)

    # Validate every server/client combination before changing any config file.
    for server in typed_servers:
        for client in normalized_clients:
            register_http_server(
                url=str(server.get("url", "")),
                client=client,
                token=token,
                name=str(server.get("mcpName", "")),
                dry_run=True,
                allow_insecure_local=True,
            )

    registrations: list[dict[str, Any]] = []
    configuration_failures: list[dict[str, Any]] = []
    secret_env_names: set[str] = set()
    for server in typed_servers:
        for client in normalized_clients:
            result = register_http_server(
                url=str(server["url"]),
                client=client,
                token=token,
                name=str(server["mcpName"]),
                allow_insecure_local=True,
            )
            registration = _result_json(result)
            registrations.append(registration)
            if registration["wrote_config"]:
                secret_env_names.update(registration.get("secret_env_names", ()))
            else:
                configuration_failures.append(
                    {
                        "client": registration["client"],
                        "config_path": registration["config_path"],
                        "reason": registration["fallback_reason"]
                        or "client configuration was not written",
                    }
                )
    return {
        "registrations": registrations,
        "config_writes_complete": not configuration_failures,
        "configuration_failures": configuration_failures,
        "setup_required": bool(secret_env_names),
        "secret_env_names": sorted(secret_env_names),
    }


def _emit_bridge_result(operation: str) -> None:
    try:
        if operation == "mcp-detect":
            result = _client_inventory()
        else:
            result = _register_mcp(_read_request())
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None


def _watch_parent(server: uvicorn.Server) -> None:
    """Stop when Electron writes ``shutdown`` or its stdin pipe closes."""

    while not server.should_exit:
        line = sys.stdin.readline()
        if not line or line.strip().lower() == "shutdown":
            server.should_exit = True
            return


async def _serve(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("desktop engine only accepts a loopback host")
    oauth_key = data_dir() / "desktop-oauth-jwt.pem"
    generate_jwt_key(oauth_key)
    settings = Settings(
        backend_port=port,
        oauth_jwt_private_key_path=str(oauth_key),
    )
    application = create_app(settings=settings)
    bootstrap(settings, application)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            proxy_headers=False,
        )
    )
    threading.Thread(
        target=_watch_parent,
        args=(server,),
        name="dtxwiki-desktop-parent-watch",
        daemon=True,
    ).start()
    await server.serve()


def main() -> None:
    operation = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if operation in {"mcp-detect", "mcp-register"}:
        _emit_bridge_result(operation)
        return
    arguments = _serve_arguments(sys.argv[1:])
    asyncio.run(_serve(arguments.host, arguments.port))


if __name__ == "__main__":
    main()
