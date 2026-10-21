"""dtxwiki CLI entry point."""

import asyncio
import ipaddress
import json
import logging
import sys
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from backend.app.bootstrap import (
    bootstrap as bootstrap_app,
)
from backend.app.bootstrap import (
    migration_status,
)
from backend.app.data_dir import data_dir, default_sqlite_url
from backend.app.runtime import RuntimeStartupError, serve_runtime
from backend.app.settings import Settings
from backend.app.version import package_version
from backend.cli.commands import admin as admin_commands
from backend.cli.commands import context as context_commands
from backend.cli.commands import db as db_commands
from backend.cli.commands import embedding as embedding_commands
from backend.cli.commands import eval_cmd as eval_commands
from backend.cli.commands import mcp as mcp_commands
from backend.cli.commands import prod as prod_commands
from backend.cli.commands import rerank as rerank_commands
from backend.cli.commands import update as update_commands
from backend.cli.integrations import detect
from backend.scripts.migrate_data import (
    MigrationPrecheckError,
    ProgressEvent,
)
from backend.scripts.migrate_data import (
    migrate_data as run_migrate_data,
)

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Run dtxWiki and manage authenticated HTTP MCP connections.",
)
logger = logging.getLogger("dtxwiki.startup")
_WILDCARD_BIND_HOSTS = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}


def create_app(*args, **kwargs):
    from backend.app.main import create_app as create_fastapi_app

    return create_fastapi_app(*args, **kwargs)


@app.callback()
def root(
    ctx: typer.Context,
    show_version: bool = typer.Option(
        False,
        "--version",
        callback=lambda value: _version_callback(value),
        is_eager=True,
        help="Show version and exit.",
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int | None = typer.Option(
        None,
        help="Bind port. Defaults to DTXWIKI_BACKEND_PORT.",
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    log_level: str = typer.Option("info", help="uvicorn log level."),
    worker_mode: str | None = typer.Option(
        None, help="Background worker mode: managed (default) or external."
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        serve(
            host=host,
            port=port,
            db=db,
            open_browser=open_browser,
            log_level=log_level,
            worker_mode=worker_mode,
        )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int | None = typer.Option(
        None,
        help="Bind port. Defaults to DTXWIKI_BACKEND_PORT.",
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    log_level: str = typer.Option("info", help="uvicorn log level."),
    worker_mode: str | None = typer.Option(
        None, help="Background worker mode: managed (default) or external."
    ),
) -> None:
    run_server(
        host=host,
        port=port,
        db=db,
        open_browser=open_browser,
        log_level=log_level,
        worker_mode=worker_mode,
    )


def run_server(
    *,
    host: str,
    port: int | None,
    db: str | None,
    open_browser: bool,
    log_level: str,
    worker_mode: str | None = None,
) -> None:
    overrides = {}
    if db:
        overrides["database_url"] = db
    if worker_mode is not None:
        if worker_mode not in {"managed", "external"}:
            raise typer.BadParameter(
                "must be managed or external", param_hint="--worker-mode"
            )
        overrides["worker_mode"] = worker_mode
    settings = Settings(**overrides)
    resolved_port = port if port is not None else settings.backend_port
    if settings.app_mode == "solo_local" and not is_loopback_bind_host(host):
        _warn_solo_local_network_bind(host, settings)
    try:
        # Preserve uvicorn.run's automatic uvloop/platform selection. Older
        # supported Uvicorn versions configure the event-loop policy instead.
        loop_config = uvicorn.Config(None, log_level=log_level)
        get_loop_factory = getattr(loop_config, "get_loop_factory", None)
        if get_loop_factory is not None:
            loop_factory = get_loop_factory()
        else:
            loop_config.setup_event_loop()
            loop_factory = None
        asyncio.run(
            serve_runtime(
                None,
                settings,
                host=host,
                port=resolved_port,
                log_level=log_level,
                proxy_headers=settings.behind_proxy,
                forwarded_allow_ips=settings.forwarded_allow_ips,
                on_started=(lambda: webbrowser.open(_browser_url(host, resolved_port)))
                if open_browser
                else None,
                application_factory=lambda effective: create_app(settings=effective),
            ),
            loop_factory=loop_factory,
        )
    except RuntimeStartupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def is_loopback_bind_host(host: str) -> bool:
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _warn_solo_local_network_bind(host: str, settings: Settings) -> None:
    """Warn loudly (not refuse: the container image binds 0.0.0.0 inside)."""

    exposure = (
        "anyone who can reach this port can create an account, "
        if settings.effective_signup_enabled
        else ""
    )
    message = (
        f"WARNING: dtxwiki serve is listening on {host}, which other machines "
        "can reach. DTXWIKI_APP_MODE=solo_local is meant for use on this "
        f"computer only: {exposure}and traffic, passwords and session cookies "
        "included, travels without TLS. For access from other machines run "
        "'dtxwiki setup production' (team mode behind HTTPS). Requests must "
        "also name an allowed host; add LAN names or addresses to "
        "DTXWIKI_ALLOWED_HOSTS."
    )
    typer.echo(message, err=True)
    if logger.hasHandlers():
        # Without a configured handler logging would fall back to stderr and
        # print the same warning a second time.
        logger.warning(message)


def _browser_url(host: str, port: int) -> str:
    # A wildcard bind is not an address a browser can use; the loopback
    # address is, and it is always an allowed host.
    target = _WILDCARD_BIND_HOSTS.get(host.strip(), host.strip())
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"
    return f"http://{target}:{port}"


@app.command(name="bootstrap")
def bootstrap_command(
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    if db:
        settings = Settings(database_url=db, auto_bootstrap=True)
    else:
        settings = Settings(auto_bootstrap=True)
    bootstrap_app(settings)
    typer.echo(f"bootstrapped {_redact(settings.database_url)}")


@app.command()
def reindex(
    workspace: str | None = typer.Option(
        None, help="Workspace slug to reindex; all workspaces if omitted."
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Rebuild symbolic and optional semantic indexes from SQL source-of-truth."""
    from sqlalchemy import select

    from backend.app.container import DatabaseProvider
    from backend.app.db import make_session_factory_from_settings
    from backend.llm.registry import build_embedding_provider
    from backend.models.workspace import Workspace
    from backend.services.indexing import (
        CompositeIndexer,
        DbNativeIndexer,
        Fts5Indexer,
        reindex_workspace,
    )

    settings = Settings(database_url=db) if db else Settings()
    embedder = build_embedding_provider(settings)
    session_factory = make_session_factory_from_settings(settings)
    database_provider = DatabaseProvider(settings, session_factory=session_factory)
    indexers = [Fts5Indexer(database_provider)]
    if embedder is None:
        typer.echo(
            "No embedding provider configured "
            "(DTXWIKI_EMBEDDING_PROVIDER=none); rebuilding FTS5 projections only."
        )
    else:
        from backend.services.vector_store import build_vector_store

        indexers.append(
            DbNativeIndexer(
                embedder=embedder,
                vector_store=build_vector_store(),
                database_provider=database_provider,
            )
        )
    indexer = CompositeIndexer(indexers)
    with session_factory() as session:
        statement = select(Workspace.id, Workspace.slug)
        if workspace:
            statement = statement.where(Workspace.slug == workspace)
        targets = list(session.execute(statement).all())

    totals = {"context_pages": 0, "library_items": 0, "pruned_orphans": 0}
    failures: list[str] = []
    for workspace_id, workspace_slug in targets:
        # One transaction per workspace so progress is durable and a single
        # workspace's failure (e.g. a transient embedding error) does not roll
        # back every other workspace's reindex.
        try:
            counts = reindex_workspace(database_provider, indexer, workspace_id)
            for key in totals:
                totals[key] += counts.get(key, 0)
        except Exception as exc:  # report and continue
            failures.append(workspace_slug)
            typer.echo(f"  ! {workspace_slug}: reindex failed: {exc}")

    typer.echo(
        f"reindexed {totals['context_pages']} context pages and "
        f"{totals['library_items']} library items "
        f"(pruned {totals['pruned_orphans']} orphan chunks"
        + (
            f"; {embedder.dimensions}-dim embeddings)"
            if embedder is not None
            else "; projections only)"
        )
    )
    if failures:
        typer.echo(f"{len(failures)} workspace(s) failed: {', '.join(failures)}")
        raise typer.Exit(code=1)


@app.command()
def info(json_output: bool = typer.Option(False, "--json")) -> None:
    settings = Settings()
    status = migration_status(settings)
    payload = {
        "data_dir": str(data_dir()),
        "database_url": _redact(settings.database_url),
        "default_sqlite_url": default_sqlite_url(),
        "detected_mcp_clients": detect.detect_clients(),
        "version_code": status.code_version,
        "version_db": status.db_version,
        "alembic_head": status.alembic_head,
        "alembic_db": status.alembic_db,
        "migration_status": status.status,
    }
    _print_update_notice(settings)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    for key, value in payload.items():
        typer.echo(f"{key}: {value}")


def _print_update_notice(settings: Settings) -> None:
    """Print a one-line 'update available' notice to stderr (cache-only).

    Kept off stdout so `info --json` stays machine-parseable. The cache is
    refreshed by `dtxwiki update --check`; here we only surface what's cached.
    """
    from backend.services import update_check_service

    info = update_check_service.check_for_update(settings, allow_network=False)
    if info is not None and info.update_available:
        typer.echo(
            f"update available: {info.latest_version} "
            f"(current {info.current_version}). Run 'dtxwiki update'.",
            err=True,
        )


@app.command()
def keygen(
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="PEM file path. Recommended: /etc/dtxwiki/jwt.pem",
        ),
    ],
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing file. Refuses by default to protect existing keys.",
    ),
    key_size: int = typer.Option(
        2048,
        "--key-size",
        min=2048,
        help="RSA key size in bits (>=2048).",
    ),
) -> None:
    """Generate an RSA private key for OAuth JWT signing (PKCS8 PEM)."""
    from backend.cli.prod_setup import generate_jwt_key

    if output.exists() and not force:
        typer.echo(
            f"ERROR: {output} already exists. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(1)

    generate_jwt_key(output, key_size=key_size, force=True)

    typer.echo(f"Wrote RSA-{key_size} PKCS8 PEM to {output}")
    if sys.platform != "win32":
        typer.echo("  chmod: 600 (UNIX permission set)")
    typer.echo(f"  Set DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH={output} in production env.")


@app.command(name="migrate-data")
def migrate_data_command(
    source_url: Annotated[
        str,
        typer.Option("--from", help="Source SQLAlchemy database URL."),
    ],
    target_url: Annotated[
        str,
        typer.Option("--to", help="Target SQLAlchemy database URL."),
    ],
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="Validate and report rows without inserting.",
    ),
    batch_size: int = typer.Option(500, "--batch-size", min=1),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow idempotent resume into a non-empty target.",
    ),
    log_format: str = typer.Option("text", "--log-format", help="text|json"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Copy dtxWiki data between SQLite/PostgreSQL backends."""
    if log_format not in {"text", "json"}:
        raise typer.BadParameter("--log-format must be one of: text, json")
    if not dry_run and not yes:
        if not typer.confirm("Copy data into the target database?"):
            raise typer.Exit(1)

    events: list[ProgressEvent] = []

    def record_progress(event: ProgressEvent) -> None:
        events.append(event)
        if log_format == "text":
            verb = "would copy" if event.dry_run else "copied"
            displayed_rows = event.source_rows if event.dry_run else event.inserted_rows
            typer.echo(
                f"[{event.table_index}/{event.table_count}] {event.table_name}: "
                f"{verb} {displayed_rows}/{event.source_rows} "
                f"(skipped {event.skipped_rows})"
            )

    try:
        report = run_migrate_data(
            source_url,
            target_url,
            dry_run=dry_run,
            batch_size=batch_size,
            force=force,
            on_progress=record_progress,
        )
    except MigrationPrecheckError as exc:
        typer.echo(f"migration precheck failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    if log_format == "json":
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(
            "summary: "
            f"source_rows={report.source_rows}, "
            f"inserted_rows={report.inserted_rows}, "
            f"skipped_rows={report.skipped_rows}, "
            f"dry_run={str(report.dry_run).lower()}"
        )


def _redact(url: str) -> str:
    # Codex #47: also mask ?password=/&token= query creds, not just userinfo.
    from backend.utils.redact import redact_url

    return redact_url(url)


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"dtxwiki {package_version()}")
    raise typer.Exit()


app.add_typer(mcp_commands.mcp_app, name="mcp")
app.add_typer(embedding_commands.embedding_app, name="embedding")
app.add_typer(rerank_commands.rerank_app, name="rerank")
app.add_typer(admin_commands.users_app, name="users")
app.add_typer(admin_commands.workspace_app, name="workspace")
app.add_typer(admin_commands.server_admin_app, name="admin")
app.add_typer(context_commands.context_app, name="context")
app.add_typer(db_commands.db_app, name="db")
app.add_typer(prod_commands.setup_app, name="setup")
app.add_typer(prod_commands.config_app, name="config")
app.command("update")(update_commands.update_command)
app.command("rollback")(update_commands.rollback_command)
app.command("eval")(eval_commands.eval_command)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
