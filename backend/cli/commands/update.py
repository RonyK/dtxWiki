"""`dtxwiki update` / `dtxwiki rollback` commands.

Thin Typer wrappers; the orchestration lives in
:mod:`backend.services.update_service` and :mod:`backend.services.backup_service`.
"""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import nullcontext
from pathlib import Path

import typer

from backend.app.bootstrap import bootstrap_core as bootstrap_app
from backend.app.runtime_resources import RuntimeResources, acquire_paths
from backend.cli import prod_setup
from backend.cli.invocation import invocation_settings, prepared_environment_file
from backend.services import backup_service, update_service
from backend.services.ops import install_method, runtime_lock
from backend.services.ops.maintenance_handoff import (
    HandoffError,
    verify_finalizer_handoff,
)
from backend.utils.redact import redact_url


def _validate_method(value: str) -> str:
    """Reject an unknown ``--method`` as a usage error (exit 2), not the raw
    ``ValueError`` ``detect_install_method`` would otherwise raise."""
    if value != "auto" and value not in install_method.VALID_KINDS:
        allowed = ", ".join(["auto", *sorted(install_method.VALID_KINDS)])
        raise typer.BadParameter(f"must be one of: {allowed}")
    return value


def _load_prod_env(method: str, env_file: str | None) -> Path | None:
    """Source the production env file into ``os.environ`` before ``Settings()``.

    On the systemd tier the CLI is run from an operator shell (``sudo dtxwiki
    update``), NOT by systemd, so the ``EnvironmentFile`` is not in scope and
    ``Settings`` would resolve the *default* (root-home) DB/data dir instead of
    the real production paths. Loading the env file here (and inheriting it into
    the ``--finalize-bootstrap`` subprocess) makes update/rollback operate on the
    live deployment. ``os.environ.setdefault`` keeps any explicit shell override
    authoritative. Returns the env file used (for backup of the env file), or
    ``None`` when not applicable.
    """

    install = install_method.detect_install_method(
        override=None if method == "auto" else method
    )
    if install.kind != "systemd" and env_file is None:
        return None
    path = Path(env_file) if env_file else prod_setup.DEFAULT_ENV_FILE
    for key, value in prod_setup.read_env_file(path).items():
        if key.startswith("DTXWIKI_"):
            os.environ.setdefault(key, value)
    return path


def _legacy_systemd_env_file() -> Path:
    """Recover the env path omitted by older updaters' finalize invocation.

    Ask systemd for the effective unit, including drop-ins, rather than guessing
    the default path or parsing only the bundled unit file.
    """
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "dtxwiki.service",
                "--property=EnvironmentFiles",
                "--value",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = result.stdout.strip()
        annotation = r" \(ignore_errors=(?:yes|no)\)"
        match = re.fullmatch(r"(?P<path>/[^\r\n]+)" + annotation, value)
        if match is not None and len(re.findall(annotation, value)) == 1:
            path = Path(match.group("path"))
            if path.is_absolute() and path.is_file():
                return path
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    typer.echo(
        "systemd env-file discovery failed: expected exactly one existing "
        "absolute EnvironmentFile. Keep services stopped. Re-run "
        "'dtxwiki update --finalize-bootstrap --method systemd "
        "--env-file /path/to/dtxwiki.env', then start services.",
        err=True,
    )
    raise typer.Exit(3)


def update_command(
    check: bool = typer.Option(
        False, "--check", help="Report only; exit 10 if an update is available."
    ),
    version: str | None = typer.Option(
        None, "--version", help="Target version to pin (default: latest on channel)."
    ),
    channel: str | None = typer.Option(
        None, "--channel", help="Release channel: stable|prerelease."
    ),
    index_url: str | None = typer.Option(
        None, "--index-url", "--source", help="Package index URL override."
    ),
    method: str = typer.Option(
        "auto",
        "--method",
        help="auto|pipx|venv|systemd|docker|npm.",
        callback=_validate_method,
    ),
    env_file: str | None = typer.Option(
        None,
        "--env-file",
        help="Production env file to source (systemd; default "
        "/etc/dtxwiki/dtxwiki.env).",
    ),
    no_backup: bool = typer.Option(
        False, "--no-backup", help="Skip the pre-update backup (requires --yes)."
    ),
    no_db_backup: bool = typer.Option(
        False,
        "--no-db-backup",
        help="Acknowledge a non-SQLite database is NOT backed up; take your "
        "own snapshot first (e.g. pg_dump).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not prompt for confirmation."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan; change nothing."
    ),
    finalize_bootstrap: bool = typer.Option(False, "--finalize-bootstrap", hidden=True),
) -> None:
    """Update dtxWiki in place: backup -> upgrade -> migrate -> restart."""

    # Internal step: run migrations under the freshly-installed (NEW) code.
    if finalize_bootstrap:
        env_file = env_file or prepared_environment_file()
        install = install_method.detect_install_method(
            override=None if method == "auto" else method
        )
        production_env = None
        if install.kind == "systemd":
            production_env = (
                Path(env_file) if env_file is not None else _legacy_systemd_env_file()
            )
            env_file = str(production_env)
        _load_prod_env(method, env_file)
        if production_env is not None:
            # Older installers already provision a separate worker unit. The
            # old updater invokes this step from the newly installed package,
            # so persist the mode before those unchanged units start again.
            prod_setup.set_env_values(
                production_env,
                {"DTXWIKI_WORKER_MODE": "external"},
            )
            os.environ["DTXWIKI_WORKER_MODE"] = "external"
        bootstrap_overrides = {"auto_bootstrap": True}
        if production_env is not None:
            bootstrap_overrides["worker_mode"] = "external"
        settings = invocation_settings(**bootstrap_overrides)
        resources = RuntimeResources.for_settings(settings)
        # The old updater already holds root EX. It does not know about the
        # DB-side lock, so acquire that lock here without retaking root EX.
        # A new updater confirms its exact root/DB guards without depending on
        # the finalizer interpreter's immediate parent PID.
        database_paths = [
            path
            for path in resources.activity_paths
            if path != resources.root / ".runtime.lock"
        ]
        try:
            inherited_db_guard = verify_finalizer_handoff(resources.activity_paths)
            with (
                nullcontext()
                if inherited_db_guard
                else acquire_paths(database_paths, "exclusive")
            ):
                bootstrap_app(settings)
        except (runtime_lock.RuntimeLockHeldError, OSError, HandoffError) as exc:
            typer.echo(f"finalize-bootstrap refused: {exc}", err=True)
            raise typer.Exit(3) from exc
        typer.echo(f"finalize-bootstrap: migrated {redact_url(settings.database_url)}")
        return

    _load_prod_env(method, env_file)
    settings = invocation_settings()

    if check:
        info = update_service.run_check(settings, index_url=index_url, channel=channel)
        if info is None:
            typer.echo("update check unavailable (disabled or index unreachable).")
            raise typer.Exit(0)
        if info.update_available:
            typer.echo(
                f"update available: {info.latest_version} "
                f"(current {info.current_version}). Run 'dtxwiki update'."
            )
            raise typer.Exit(10)
        typer.echo(f"up to date ({info.current_version}).")
        raise typer.Exit(0)

    if no_backup and not yes:
        typer.echo(
            "--no-backup requires --yes (backups are the rollback safety net).",
            err=True,
        )
        raise typer.Exit(2)

    try:
        result = update_service.run_update(
            settings,
            version=version,
            channel=channel,
            index_url=index_url,
            method=method,
            no_backup=no_backup,
            no_db_backup=no_db_backup,
            yes=yes,
            dry_run=dry_run,
            env_file=env_file,
            confirm=typer.confirm,
            echo=typer.echo,
        )
    except typer.Abort:
        # Non-interactive shells make typer.confirm raise Abort (which Typer maps
        # to exit 1); surface the documented exit 2 'aborted by operator' instead.
        typer.echo("update aborted (no confirmation; pass --yes).", err=True)
        raise typer.Exit(2) from None
    raise typer.Exit(result.exit_code)


def rollback_command(
    to_backup: str | None = typer.Option(
        None, "--to-backup", help="Backup dir or manifest.json (default: latest)."
    ),
    list_backups: bool = typer.Option(
        False, "--list", help="List available backups and exit."
    ),
    method: str = typer.Option(
        "auto",
        "--method",
        help="auto|pipx|venv|systemd|docker|npm.",
        callback=_validate_method,
    ),
    index_url: str | None = typer.Option(
        None, "--index-url", "--source", help="Index to reinstall the old version."
    ),
    env_file: str | None = typer.Option(
        None,
        "--env-file",
        help="Production env file to source (systemd; default "
        "/etc/dtxwiki/dtxwiki.env).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan; change nothing."
    ),
) -> None:
    """Roll back to a previous backup (reinstall old code + restore DB/secrets)."""

    _load_prod_env(method, env_file)
    settings = invocation_settings()

    if list_backups:
        infos = backup_service.list_backups()
        if not infos:
            typer.echo("no backups found.")
            raise typer.Exit(0)
        for info in infos:
            manifest = info.manifest
            typer.echo(
                f"{info.path.name}  "
                f"version={manifest.get('dtxwiki_version')}  "
                f"created={manifest.get('created_utc')}  "
                f"rev={manifest.get('alembic_revision')}"
            )
        raise typer.Exit(0)

    try:
        result = update_service.run_rollback(
            settings,
            to_backup=to_backup,
            method=method,
            index_url=index_url,
            yes=yes,
            dry_run=dry_run,
            env_file=env_file,
            confirm=typer.confirm,
            echo=typer.echo,
        )
    except typer.Abort:
        typer.echo("rollback aborted (no confirmation; pass --yes).", err=True)
        raise typer.Exit(2) from None
    raise typer.Exit(result.exit_code)
