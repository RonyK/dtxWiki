"""Database revision evidence and explicit compatibility conversion commands."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated

import typer

from backend.app.settings import Settings
from backend.app.version import package_version
from backend.services.ops.alembic_migration_graph import AlembicMigrationGraphSource
from backend.services.ops.db_revision_inventory import (
    InventoryRequest,
    LiveTargetRequest,
    ManifestRequest,
    collect_inventory,
)
from backend.services.ops.filesystem_backup_evidence import (
    FilesystemBackupEvidenceSource,
)
from backend.services.ops.read_only_database import SqlAlchemyRevisionInventoryProbe

db_app = typer.Typer(
    help="Inspect database revisions or explicitly convert legacy revision IDs."
)

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


@db_app.command(name="rename-revisions")
def rename_revisions(
    apply: bool = typer.Option(
        False, "--apply", help="Apply the previewed ID conversion; back up first."
    ),
) -> None:
    """Preview the configured DB's legacy ID conversion without replaying DDL."""

    from backend.services.ops.revision_rename import (
        RevisionRenameError,
        rename_database_revisions,
    )

    try:
        result = rename_database_revisions(Settings().database_url, apply=apply)
    except RevisionRenameError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    except Exception:
        # Driver/configuration exceptions can contain credential-bearing URLs.
        typer.echo("revision_rename_failed: database/configuration error", err=True)
        raise typer.Exit(1) from None
    status = "applied" if result.changed else "unchanged" if apply else "preview"
    typer.echo(f"{status}: {result.old_revision} -> {result.new_revision}")


@db_app.command(name="revision-inventory")
def revision_inventory(
    target_env: Annotated[
        list[str] | None,
        typer.Option(
            "--target-env",
            help=(
                "Repeatable NAME=ENV_VAR target; the URL value is never accepted "
                "in argv."
            ),
        ),
    ] = None,
    sqlite_file: Annotated[
        list[str] | None,
        typer.Option(
            "--sqlite-file", help="Repeatable NAME=PATH existing SQLite file."
        ),
    ] = None,
    backup_manifest: Annotated[
        list[str] | None,
        typer.Option(
            "--backup-manifest",
            help="Repeatable NAME=PATH backup manifest or directory.",
        ),
    ] = None,
    configured: Annotated[
        str | None,
        typer.Option(
            "--configured", help="Inspect Settings().database_url under explicit NAME."
        ),
    ] = None,
    json_output: bool = typer.Option(
        False, "--json", help="Emit stable schema-v1 JSON."
    ),
) -> None:
    """Gather neutral Phase-0 revision evidence; never approves a migration."""

    live: list[LiveTargetRequest] = []
    manifests: list[ManifestRequest] = []
    seen: set[str] = set()

    if configured is not None:
        name = _validate_name(configured)
        _reserve_name(name, seen)
        try:
            settings = Settings()
        except Exception:
            typer.echo(
                "revision inventory failed: invalid_configured_settings", err=True
            )
            raise typer.Exit(1) from None
        live.append(
            LiveTargetRequest(
                name=name,
                source_kind="settings",
                source_reference="DTXWIKI_DATABASE_URL",
                database_target=settings.database_url,
            )
        )

    for raw in target_env or []:
        name, env_name = _split_pair(raw, option="--target-env")
        if not _ENV_NAME.fullmatch(env_name):
            raise typer.BadParameter(
                "environment variable must use A-Z, 0-9, and underscore"
            )
        _reserve_name(name, seen)
        value = os.environ.get(env_name)
        if value is None:
            # A deliberately unsupported URL makes the missing env a per-target
            # incomplete result without ever placing a secret in diagnostics.
            value = "missing-env://"
        live.append(
            LiveTargetRequest(
                name=name,
                source_kind="env",
                source_reference=env_name,
                database_target=value,
            )
        )

    for raw in sqlite_file or []:
        name, path_text = _split_pair(raw, option="--sqlite-file")
        _reject_url_like(path_text, option="--sqlite-file")
        _reserve_name(name, seen)
        path = Path(path_text).expanduser().absolute()
        live.append(
            LiveTargetRequest(
                name=name,
                source_kind="sqlite_file",
                source_reference=str(path),
                database_target=path,
            )
        )

    for raw in backup_manifest or []:
        name, path_text = _split_pair(raw, option="--backup-manifest")
        _reject_url_like(path_text, option="--backup-manifest")
        _reserve_name(name, seen)
        manifests.append(
            ManifestRequest(name=name, path=Path(path_text).expanduser().absolute())
        )

    if not live and not manifests:
        raise typer.BadParameter(
            "at least one explicit --configured, --target-env, --sqlite-file, "
            "or --backup-manifest target is required"
        )

    try:
        result = collect_inventory(
            InventoryRequest(live_targets=tuple(live), manifests=tuple(manifests)),
            probe=SqlAlchemyRevisionInventoryProbe(),
            migration_graph=AlembicMigrationGraphSource(),
            backup_evidence=FilesystemBackupEvidenceSource(),
            command_version=package_version(),
        )
    except Exception:
        typer.echo("revision inventory failed: internal_error", err=True)
        raise typer.Exit(1) from None

    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(
            "Database revision inventory (evidence only; not migration approval)"
        )
        typer.echo(f"code heads: {', '.join(result.code['alembic_heads']) or '<none>'}")
        for target in result.targets:
            revisions = "<unavailable>"
            if target.alembic is not None:
                revisions = ",".join(target.alembic["revisions"]) or "<none>"
            typer.echo(
                f"{target.name}: status={target.status} revisions={revisions} "
                f"consistency={target.consistency or '<unknown>'}"
            )
        for manifest in result.manifest_evidence:
            typer.echo(
                f"{manifest.name}: manifest={manifest.status} "
                f"revision={manifest.alembic_revision or '<unknown>'}"
            )
    for target in result.targets:
        if target.status != "ok" or target.consistency != "consistent":
            codes = ",".join(target.errors) or target.consistency or "unknown"
            typer.echo(f"warning: {target.name}: {codes}", err=True)
    for manifest in result.manifest_evidence:
        if manifest.status != "ok":
            codes = ",".join(manifest.errors) or "manifest_invalid"
            typer.echo(f"warning: {manifest.name}: {codes}", err=True)
    raise typer.Exit(result.exit_code)


def _split_pair(value: str, *, option: str) -> tuple[str, str]:
    if "=" not in value:
        raise typer.BadParameter(f"{option} requires NAME=VALUE")
    name, item = value.split("=", 1)
    name = _validate_name(name)
    if not item:
        raise typer.BadParameter(f"{option} value must not be empty")
    return name, item


def _validate_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise typer.BadParameter(
            "target NAME must start with a letter and contain only letters, "
            "digits, _ or -"
        )
    return value


def _reserve_name(name: str, seen: set[str]) -> None:
    folded = name.casefold()
    if folded in seen:
        raise typer.BadParameter(f"duplicate target name: {name}")
    seen.add(folded)


def _reject_url_like(value: str, *, option: str) -> None:
    if "://" in value:
        raise typer.BadParameter(
            f"{option} accepts a filesystem path, never a database URL"
        )
