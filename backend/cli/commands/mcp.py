"""Authenticated Streamable HTTP MCP client commands."""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer

from backend.catalog.mcp_apps.security import is_safe_secret_env_name
from backend.cli.mcp_doctor import format_text_report, run_http_doctor
from backend.cli.mcp_http_register import register_http_servers

mcp_app = typer.Typer(help="Manage authenticated HTTP MCP connections.")


@mcp_app.command(name="register-http")
def mcp_register_http(
    url: Annotated[
        list[str],
        typer.Option(
            "--url",
            help="Remote MCP URL ending in /mcp; repeat for several servers.",
        ),
    ],
    client: Annotated[
        str,
        typer.Option("--client", help="Active catalog app ID with a CLI recipe."),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Optional PAT; direct use exposes it in this process argv.",
        ),
    ] = None,
    token_env: Annotated[
        str | None,
        typer.Option(
            "--token-env",
            help="Read the PAT from this environment variable instead of argv.",
        ),
    ] = None,
    name: Annotated[
        list[str] | None,
        typer.Option(
            "--name",
            help="MCP server name; repeat once per --url for several servers.",
        ),
    ] = None,
    dry_run: bool = typer.Option(False, "--dry-run"),
    echo_only: bool = typer.Option(False, "--echo-only"),
    allow_insecure_local: bool = typer.Option(
        False,
        "--allow-insecure-local",
        help="Allow HTTP /mcp on loopback or private IPs with a PAT.",
    ),
) -> None:
    """Register a remote HTTP MCP endpoint from the packaged app catalog."""
    try:
        token = _resolve_token(token=token, token_env=token_env)
        results = register_http_servers(
            urls=url,
            client=client,
            token=token,
            names=name,
            dry_run=dry_run,
            echo_only=echo_only,
            allow_insecure_local=allow_insecure_local,
        )
    except ValueError as exc:
        typer.echo(f"register-http failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    for result in results:
        typer.echo(result.setup_text)
        if result.fallback_reason:
            typer.echo(f"fallback: {result.fallback_reason}", err=True)
        if result.backup_path is not None:
            if dry_run:
                typer.echo(f"dry-run: would back up {result.backup_path}")
            else:
                typer.echo(f"backup: {result.backup_path}")
        if result.wrote_config:
            typer.echo(f"updated: {result.config_path}")
        elif dry_run:
            if result.config_path is None:
                typer.echo("dry-run: no local config adapter; rendered setup only")
            else:
                typer.echo(f"dry-run: would update {result.config_path}")
        else:
            location = result.config_path or "no local config adapter"
            typer.echo(f"echo-only: no config file changed ({location})")
        if token and result.secret_env_names:
            env_names = ", ".join(result.secret_env_names)
            typer.echo(
                "Authorization PAT was not written to the client config; export "
                f"{env_names} in your shell or client environment.",
                err=True,
            )
        elif token and result.wrote_config:
            typer.echo(
                "Warning: the Authorization PAT was written to the local client "
                "config. Restrict that file's permissions, rotate the key if it "
                "may have been exposed, and prefer OAuth when available.",
                err=True,
            )


@mcp_app.command(name="doctor")
def mcp_doctor(
    http_url: Annotated[
        str | None,
        typer.Option("--http", help="Remote MCP URL ending in /mcp."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="PAT used for list_tools; direct use exposes it in process argv.",
        ),
    ] = None,
    token_env: Annotated[
        str | None,
        typer.Option(
            "--token-env",
            help="Read the list_tools PAT from this environment variable.",
        ),
    ] = None,
    json_output: bool = typer.Option(False, "--json"),
    timeout_sec: int = typer.Option(10, "--timeout-sec", min=1),
) -> None:
    """Diagnose remote HTTP MCP connectivity."""
    if http_url is None:
        typer.echo("doctor requires --http <url>.", err=True)
        raise typer.Exit(1)
    try:
        token = _resolve_token(token=token, token_env=token_env)
        result = run_http_doctor(
            http_url, token=token, timeout_sec=timeout_sec
        )
    except ValueError as exc:
        typer.echo(f"doctor failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(format_text_report(result))
    raise typer.Exit(result["exit_code"])


def _resolve_token(*, token: str | None, token_env: str | None) -> str | None:
    if token is not None and token_env is not None:
        raise ValueError("pass either --token or --token-env, not both")
    if token_env is None:
        return token
    if not is_safe_secret_env_name(token_env):
        raise ValueError(
            "--token-env must be an uppercase environment name with 2-64 "
            "letters, digits, or underscores"
        )
    resolved = os.environ.get(token_env)
    if not resolved:
        raise ValueError(f"--token-env variable {token_env!r} is missing or empty")
    return resolved
