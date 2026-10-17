"""Orchestrate ``dtxwiki update`` across install tiers.

Order (per tier): stop services -> take the runtime lock exclusively -> probe
DB lock -> backup -> pip/pipx upgrade -> finalize-bootstrap (NEW code,
subprocess) -> release the runtime lock -> start services -> healthcheck.

The runtime lock (``.runtime.lock`` in the data directory) is what a running
``dtxwiki serve`` or worker holds in shared mode; an idle server holds no
SQLite lock, so the write probe alone cannot see it. On the systemd tier the
services were just stopped, so the lock is free; on the pipx/venv tiers a
server the operator left running makes the update refuse up front.

Failure policy is **notify-only** (operator choice): on any failure the system is
left as-is and the exact ``dtxwiki rollback --to-backup <dir>`` recovery command
is printed. Nothing is auto-reverted.

The self-upgrade handoff: a running update process holds the OLD code in memory,
so migrations are never run in-process — they run in a fresh subprocess of the
freshly-installed ``dtxwiki`` binary via the hidden ``--finalize-bootstrap`` flag.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from backend.app import bootstrap, versioning
from backend.app import data_dir as data_dir_paths
from backend.app.runtime_resources import LockSet, RuntimeResources
from backend.app.settings import Settings
from backend.services import backup_service, update_check_service
from backend.services.ops import (
    db_lock,
    healthcheck,
    index,
    install_method,
    runtime_lock,
)
from backend.services.ops import service_control as svc
from backend.services.ops.maintenance_handoff import (
    HANDOFF_ENV,
    HandoffError,
    finalizer_handoff,
)
from backend.utils.redact import redact_url

# Documented production env file (systemd tier). Kept here to avoid a
# services -> cli import; mirrors backend.cli.prod_setup.DEFAULT_ENV_FILE.
PROD_ENV_FILE = Path("/etc/dtxwiki/dtxwiki.env")

# Upper bound for a single update step (index fetch / migration). Without it a
# hung mirror or a migration stuck on a lock would wedge the update forever with
# production services already STOPPED. 30 min is generous for a real migration.
_STEP_TIMEOUT_SEC = 1800

# Exceptions a backup create/restore can raise: file I/O (OSError, incl.
# PermissionError/ENOSPC), a corrupt manifest re-read (json.JSONDecodeError, a
# ValueError), the SQLite online-backup API (sqlite3.Error), and the manifest
# sha256 verify (BackupIntegrityError, a RuntimeError not an OSError).
_BACKUP_IO_ERRORS = (
    OSError,
    ValueError,
    sqlite3.Error,
    backup_service.BackupIntegrityError,
)


class UpdateStepError(RuntimeError):
    """A single update step (subprocess) failed."""


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    exit_code: int
    from_version: str
    to_version: str | None
    backup_dir: Path | None
    message: str


def run_check(
    settings: Settings,
    *,
    index_url: str | None = None,
    channel: str | None = None,
    data_dir: Path | None = None,
):
    """Network refresh of the update-available state (for ``update --check``)."""

    target = _settings_with_overrides(settings, index_url, channel)
    return update_check_service.check_for_update(
        target, force=True, allow_network=True, data_dir=data_dir
    )


def run_update(
    settings: Settings,
    *,
    version: str | None = None,
    channel: str | None = None,
    index_url: str | None = None,
    method: str = "auto",
    no_backup: bool = False,
    no_db_backup: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    data_dir: Path | None = None,
    env_file: str | None = None,
    confirm=None,
    echo=print,
) -> UpdateResult:
    current = settings.app_version
    install = install_method.detect_install_method(
        override=None if method == "auto" else method
    )
    resolved_index = index.resolve_index_url(
        flag=index_url, settings_index_url=settings.update_index_url
    )

    if install.kind == "docker":
        return _docker_instructions(settings, version, resolved_index, echo)
    if install.kind == "npm":
        return _npm_update_instructions(settings, version, echo)

    # `update` only moves forward. A pinned older version would downgrade the
    # code and then trip the bootstrap downversion fail-safe with services
    # already stopped — misreported as a migration failure even though the DB
    # was never touched. Refuse up front and point at rollback, which also
    # restores the matching DB backup. Unparseable versions fall through to
    # pip's own resolution error.
    if version is not None and versioning.is_newer(current, version):
        echo(
            f"ERROR: requested version {version} is older than the installed "
            f"{current} — 'dtxwiki update' cannot downgrade."
        )
        echo(
            "  use 'dtxwiki rollback' instead (it reinstalls the old code AND "
            "restores the matching DB backup; 'dtxwiki rollback --list' shows "
            "candidates)."
        )
        return UpdateResult(
            False,
            2,
            current,
            None,
            None,
            f"downgrade to {version} refused: use 'dtxwiki rollback'",
        )

    # Normalize the raw CLI flag through the same validator env/config use, so
    # `--channel PRERELEASE` is not silently treated as stable (the flag bypasses
    # Settings construction). None -> fall back to the configured channel.
    effective_channel = (
        Settings._normalize_update_channel(channel)
        if channel is not None
        else settings.update_channel
    )
    spec = index.package_spec(version)
    install_argv = _install_argv(
        install, spec, resolved_index, pre=(effective_channel == "prerelease")
    )

    non_sqlite_db = _non_sqlite_database(settings.database_url)

    if dry_run:
        _echo_plan(echo, install, install_argv, no_backup, non_sqlite_db)
        return UpdateResult(True, 0, current, version, None, "dry-run")

    # A non-SQLite (e.g. PostgreSQL) DB cannot be snapshotted by create_backup,
    # so an update would otherwise proceed with no DB recovery point while the
    # messaging implies one. Refuse unless the operator explicitly acknowledges.
    if non_sqlite_db and not no_backup:
        if not no_db_backup:
            echo(
                "ERROR: the configured database is not SQLite — the pre-update "
                "backup captures secrets only, NOT the database."
            )
            echo(
                "  take a database snapshot yourself first (e.g. pg_dump), then "
                "re-run with --no-db-backup to acknowledge and proceed."
            )
            return UpdateResult(
                False,
                2,
                current,
                None,
                None,
                "non-SQLite database: pass --no-db-backup to proceed without "
                "a DB snapshot",
            )
        echo(
            "WARNING: non-SQLite database — proceeding with a secrets-only "
            "backup (no DB snapshot; make sure a recent pg_dump or equivalent "
            "exists)."
        )

    if no_backup:
        backup_note = "NO backup will be taken."
    elif non_sqlite_db:
        backup_note = "A secrets-only backup will be taken first (DB NOT included)."
    else:
        backup_note = "A backup will be taken first."
    if (
        not yes
        and confirm is not None
        and not confirm(f"Update dtxWiki ({install.kind}) now? {backup_note}")
    ):
        return UpdateResult(False, 2, current, None, None, "aborted by operator")

    alembic_rev = bootstrap.current_db_alembic_revision(settings)

    # 1. stop writers (systemd) so the SQLite DB is quiescent for backup/migrate.
    if install.services:
        try:
            svc.run_systemctl("stop", install.services)
        except (OSError, subprocess.SubprocessError) as exc:
            return _fail(2, "could not stop services", None, current, echo, exc)

    # 1b. no dtxWiki runtime may run from here until the services restart. The
    #     systemd units are stopped by now; on the pipx/venv tiers a server or
    #     worker the operator left running makes the update refuse here.
    held, refused = _take_runtime_lock(
        data_dir, install, echo, current, "update", settings=settings
    )
    if refused is not None:
        return refused
    assert held is not None

    def restart_old_services() -> None:
        # The new code never ran: give the lock back first, or the restarted
        # services would refuse to start while this process still holds it.
        held.release()
        _start_services(install, echo)

    try:
        # 2. fail fast if a writer still holds the SQLite lock (solo path).
        try:
            db_lock.probe_sqlite_writable(settings.database_url)
        except db_lock.SqliteLockedError as exc:
            restart_old_services()
            return _fail(2, str(exc), None, current, echo, exc)

        # 3. backup. A backup failure is still a pre-DB-change failure: bring
        #    the old code back up and report exit-2 rather than leaving services
        #    stopped.
        backup_dir: Path | None = None
        if not no_backup:
            try:
                backup_dir = backup_service.create_backup(
                    settings,
                    version=current,
                    alembic_revision=alembic_rev,
                    install_method=install.kind,
                    index_url=resolved_index,
                    data_dir=data_dir,
                    env_file=_effective_env_file(install, env_file),
                )
            except _BACKUP_IO_ERRORS as exc:
                restart_old_services()
                return _fail(2, "backup failed", None, current, echo, exc)
            echo(f"backup: {backup_dir}")

        # 4. replace the code (pip/pipx).
        try:
            _run_argv(install_argv, echo=echo)
        except UpdateStepError as exc:
            # No DB change yet -> safe to bring the old version back up.
            restart_old_services()
            return _fail(2, "package install failed", backup_dir, current, echo, exc)

        # 5. finalize-bootstrap: migrations run under the NEW code,
        #    out-of-process.
        if install.dtxwiki_bin is None:
            # Code is upgraded but the new binary cannot be located to run
            # migrations. Never report success on an unmigrated DB.
            if install.services:
                echo("NOTE: services remain STOPPED (migrations did not run).")
            return _fail(
                3,
                "cannot locate the dtxwiki binary to run migrations; run "
                "'dtxwiki bootstrap' manually with the upgraded code, then start "
                "services",
                backup_dir,
                current,
                echo,
                None,
            )
        try:
            # Pass --method so the subprocess sources the same prod env
            # (systemd tier); it also inherits this process's already-loaded
            # DTXWIKI_* environment. No timeout: unlike the network install
            # step, migrations run AFTER the writer-lock probe with services
            # stopped, so they make real local progress — a legitimately long
            # migration on a large DB must not be killed and reported as a
            # half-migrated failure (the _STEP_TIMEOUT_SEC cap stays on the
            # index/install step only). The subprocess takes no runtime lock
            # of its own, so the exclusive one held here does not stop it.
            finalize_argv = [
                str(install.dtxwiki_bin),
                "update",
                "--finalize-bootstrap",
                "--method",
                install.kind,
            ]
            # Inheriting the values is insufficient: finalization also updates
            # the env file (for example, to retain an existing worker unit).
            # Use the same path that was backed up and that the services read.
            effective_env = _effective_env_file(install, env_file)
            if effective_env is not None:
                finalize_argv.extend(["--env-file", str(effective_env)])
            # Confirm the actual guards through a private invocation-scoped
            # challenge. Windows console/venv launchers introduce intermediate
            # PIDs, so direct parent identity cannot establish this handoff.
            with finalizer_handoff(held) as handoff:
                _run_argv(
                    finalize_argv,
                    echo=echo,
                    timeout=None,
                    env={**os.environ, HANDOFF_ENV: handoff},
                )
        except (UpdateStepError, HandoffError) as exc:
            # Post-migration failure: the DB may be partially migrated. Leave
            # services stopped (notify-only) rather than serve a half-migrated
            # DB, and say so.
            if install.services:
                echo(
                    "NOTE: services remain STOPPED to avoid serving a half-migrated DB."
                )
            return _fail(
                3,
                "migration failed after upgrade (DB may be partially migrated)",
                backup_dir,
                current,
                echo,
                exc,
            )
    finally:
        held.release()

    # 6. start services + healthcheck (systemd).
    if install.services:
        try:
            svc.run_systemctl("start", install.services)
        except (OSError, subprocess.SubprocessError) as exc:
            return _fail(1, "service start failed", backup_dir, current, echo, exc)
        base_url = f"http://127.0.0.1:{settings.backend_port}"
        if not healthcheck.poll_health_ready(base_url):
            return _fail(
                1, "post-update healthcheck failed", backup_dir, current, echo, None
            )

    backup_service.prune_backups(data_dir=data_dir, keep=settings.update_keep_backups)
    new_version = _installed_version(install) or version
    echo(f"updated: {current} -> {new_version or '(installed)'}")
    return UpdateResult(True, 0, current, new_version, backup_dir, "updated")


def run_rollback(
    settings: Settings,
    *,
    to_backup: str | None = None,
    method: str = "auto",
    index_url: str | None = None,
    yes: bool = False,
    dry_run: bool = False,
    data_dir: Path | None = None,
    env_file: str | None = None,
    confirm=None,
    echo=print,
) -> UpdateResult:
    current = settings.app_version
    install = install_method.detect_install_method(
        override=None if method == "auto" else method
    )
    if install.kind == "docker":
        # Codex #35: compose reads the full-image var DTXWIKI_IMAGE
        # (image: "${DTXWIKI_IMAGE:-dtxwiki:local}"), NOT DTXWIKI_IMAGE_TAG.
        # Point operators at the variable compose actually consumes so the
        # rollback is not a silent no-op.
        echo(
            "Docker rollback (run on the HOST): set DTXWIKI_IMAGE to the previous "
            "image ref (e.g. registry/dtxwiki:<old-version>), then run"
        )
        echo("  cd infra/prod && docker compose up -d")
        echo("  then restore the data volume from your backup.")
        return UpdateResult(False, 2, current, None, None, "docker: manual rollback")
    if install.kind == "npm":
        return _npm_rollback_instructions(settings, echo)

    if to_backup is not None:
        try:
            backup_dir = backup_service.resolve_backup_dir(Path(to_backup))
        except FileNotFoundError as exc:
            return _fail(2, str(exc), None, current, echo, exc, operation="rollback")
    else:
        infos = backup_service.list_backups(data_dir)
        if not infos:
            echo("no backup available to roll back to.")
            return UpdateResult(False, 2, current, None, None, "no backup")
        backup_dir = infos[0].path

    try:
        manifest = backup_service.read_manifest(backup_dir)
    except (OSError, ValueError) as exc:
        return _fail(
            2,
            f"backup {backup_dir} has an unreadable manifest.json",
            None,
            current,
            echo,
            exc,
            operation="rollback",
        )
    target_version = manifest.get("dtxwiki_version")
    if not target_version:
        return _fail(
            2,
            f"backup {backup_dir} has no recorded version",
            None,
            current,
            echo,
            None,
            operation="rollback",
        )

    # Fail clean BEFORE touching anything if the backup is corrupt.
    try:
        backup_service.verify_backup_integrity(backup_dir)
    except backup_service.BackupIntegrityError as exc:
        return _fail(2, str(exc), backup_dir, current, echo, exc, operation="rollback")

    # Pre-includes_db manifests fall back to the snapshot file's presence; a
    # snapshot only restores onto a SQLite database_url (restore_backup no-ops
    # otherwise), so the messaging must never claim a DB restore that won't run.
    db_in_backup = bool(
        manifest.get(
            "includes_db", (backup_dir / backup_service.DB_BACKUP_NAME).exists()
        )
    )
    db_restorable = (
        db_in_backup
        and backup_service.db_path_from_url(settings.database_url) is not None
    )

    resolved_index = index.resolve_index_url(
        flag=index_url, settings_index_url=settings.update_index_url
    )
    spec = index.package_spec(target_version)
    reinstall_argv = (
        index.build_pipx_install_argv(spec, index_url=resolved_index)
        if install.kind == "pipx"
        else index.build_pip_install_argv(
            list(install.pip_argv),
            spec,
            index_url=resolved_index,
            upgrade=False,
            force_reinstall=True,
        )
    )

    if dry_run:
        echo(
            f"DRY RUN — rollback ({install.kind}) -> {target_version} "
            f"from {backup_dir.name}:"
        )
        if install.services:
            echo(f"  - systemctl stop {' '.join(install.services)}")
        echo("  - " + _redacted_argv(reinstall_argv) + "   (reinstall OLD code FIRST)")
        echo(
            "  - restore DB + secrets from the backup"
            if db_restorable
            else "  - restore secrets from the backup (no DB snapshot to restore)"
        )
        if install.services:
            echo(f"  - systemctl start {' '.join(install.services)} ; /health/ready")
        echo("Re-run without --dry-run to apply.")
        return UpdateResult(True, 0, current, target_version, backup_dir, "dry-run")

    if not db_restorable:
        echo(
            "WARNING: no database snapshot will be restored from this backup — "
            "restore the database yourself if needed (e.g. pg_restore)."
        )
    if (
        not yes
        and confirm is not None
        and not confirm(
            f"Roll back dtxWiki to {target_version} from {backup_dir.name}?"
        )
    ):
        return UpdateResult(False, 2, current, None, backup_dir, "aborted by operator")

    if install.services:
        try:
            svc.run_systemctl("stop", install.services)
        except (OSError, subprocess.SubprocessError) as exc:
            return _fail(
                2,
                "could not stop services",
                backup_dir,
                current,
                echo,
                exc,
                operation="rollback",
            )

    # Restoring a database under a live server or worker is exactly what this
    # lock exists to prevent; see the module docstring.
    held, refused = _take_runtime_lock(
        data_dir, install, echo, current, "rollback", settings=settings
    )
    if refused is not None:
        return refused
    assert held is not None
    try:
        # Reinstall the OLD code FIRST so db_version > code_version is
        # impossible — the downversion fail-safe is never tripped.
        try:
            _run_argv(reinstall_argv, echo=echo)
        except UpdateStepError as exc:
            held.release()
            _start_services(install, echo)
            return _fail(
                2,
                f"could not reinstall dtxwiki=={target_version} "
                "(is that version still in the index?)",
                backup_dir,
                current,
                echo,
                exc,
                operation="rollback",
            )

        # Old code is back; restore DB + secrets. A restore I/O error here can
        # leave a half-copied SQLite file or the still-NEW schema on disk, and
        # the old code started against either would crash-loop on the
        # downversion fail-safe — so leave services STOPPED (same policy as a
        # failed migration) and say so instead of implying they are back up.
        try:
            backup_service.restore_backup(
                backup_dir,
                settings,
                data_dir=data_dir,
                env_file=_effective_env_file(install, env_file),
            )
        except _BACKUP_IO_ERRORS as exc:
            if install.services:
                echo(
                    "NOTE: services remain STOPPED to avoid serving a partially-"
                    "restored DB."
                )
            return _fail(
                3,
                "restore of DB + secrets failed (the DB may be partially restored)",
                backup_dir,
                current,
                echo,
                exc,
                operation="rollback",
            )
    finally:
        held.release()
    if db_restorable:
        echo(f"restored DB + secrets from {backup_dir.name}")
    elif db_in_backup:
        echo(
            f"restored secrets (DB snapshot NOT restored — non-SQLite "
            f"database_url) from {backup_dir.name}"
        )
    else:
        echo(f"restored secrets (DB not included in backup) from {backup_dir.name}")

    if install.services:
        try:
            svc.run_systemctl("start", install.services)
        except (OSError, subprocess.SubprocessError) as exc:
            return _fail(
                1,
                "service start failed",
                backup_dir,
                current,
                echo,
                exc,
                operation="rollback",
            )
        base_url = f"http://127.0.0.1:{settings.backend_port}"
        if not healthcheck.poll_health_ready(base_url):
            return _fail(
                1,
                "post-rollback healthcheck failed",
                backup_dir,
                current,
                echo,
                None,
                operation="rollback",
            )

    echo(f"rolled back: {current} -> {target_version}")
    return UpdateResult(True, 0, current, target_version, backup_dir, "rolled back")


# --- helpers -------------------------------------------------------------


def _take_runtime_lock(
    data_dir: Path | None,
    install,
    echo,
    current: str,
    operation: str,
    *,
    settings: Settings,
) -> tuple[LockSet | None, UpdateResult | None]:
    """Hold the runtime lock exclusively, or restart and report the refusal.

    Nothing has changed yet when this refuses, so services stopped for the
    operation are started again (a no-op on the pipx/venv tiers) and no
    recovery command is suggested.
    """

    try:
        resources = RuntimeResources.for_settings(settings)
        if data_dir is not None:
            resources = replace(resources, root=Path(data_dir).resolve())
        return resources.acquire_activity("exclusive"), None
    except runtime_lock.RuntimeLockHeldError as exc:
        _start_services(install, echo)
        return None, _fail(2, str(exc), None, current, echo, None, operation=operation)
    except (OSError, ValueError) as exc:
        _start_services(install, echo)
        return None, _fail(
            2,
            "could not take the dtxWiki runtime lock",
            None,
            current,
            echo,
            exc,
            operation=operation,
        )


def _non_sqlite_database(database_url: str) -> bool:
    """True when the configured backend is not SQLite (e.g. PostgreSQL).

    Such databases cannot be snapshotted by ``backup_service.create_backup``,
    so update/rollback must surface that instead of implying a DB backup
    happened. In-memory SQLite stays False (nothing to snapshot, but it is
    still the SQLite tier). Unparseable URLs stay False — they fail later with
    their own error rather than a misleading backup warning.
    """

    try:
        return make_url(database_url).get_backend_name() != "sqlite"
    except ArgumentError:
        return False


def _settings_with_overrides(
    settings: Settings, index_url: str | None, channel: str | None
) -> Settings:
    updates: dict = {}
    if index_url is not None:
        updates["update_index_url"] = index_url
    if channel is not None:
        # model_copy(update=) bypasses validators, so normalize here too; else
        # `--check --channel PreRelease` would filter out prereleases.
        updates["update_channel"] = Settings._normalize_update_channel(channel)
    return settings.model_copy(update=updates) if updates else settings


def _effective_env_file(install, env_file: str | None) -> Path | None:
    """Env file to back up/restore. An explicit ``--env-file`` wins on any tier;
    otherwise the systemd default, or ``None`` on non-systemd tiers. Mirrors the
    CLI's ``_load_prod_env`` resolution so a custom env-file deployment's secrets
    are actually included in the rollback safety net."""
    if env_file:
        return Path(env_file)
    return PROD_ENV_FILE if install.kind == "systemd" else None


def _install_argv(
    install, spec: str, resolved_index: str | None, *, pre: bool = False
) -> list[str]:
    if install.kind == "pipx":
        return index.build_pipx_install_argv(spec, index_url=resolved_index, pre=pre)
    return index.build_pip_install_argv(
        list(install.pip_argv), spec, index_url=resolved_index, pre=pre
    )


def _redacted_argv(argv: list[str]) -> str:
    """Join an argv for display with embedded credentials masked — a private
    ``--index-url https://user:token@host/simple`` must never be echoed raw."""
    return " ".join(redact_url(arg) for arg in argv)


def _run_argv(
    argv: list[str],
    *,
    echo,
    timeout: float = _STEP_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> None:
    echo("$ " + _redacted_argv(argv))
    try:
        kwargs = {"env": env} if env is not None else {}
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateStepError(
            f"command timed out after {timeout:.0f}s: {_redacted_argv(argv)}"
        ) from exc
    except OSError as exc:
        raise UpdateStepError(
            f"could not run command (is {argv[0]!r} installed/on PATH?): {exc}"
        ) from exc
    if result.stdout:
        echo(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr:
            echo(result.stderr.strip())
        raise UpdateStepError(
            f"command failed (exit {result.returncode}): {_redacted_argv(argv)}"
        )


def _start_services(install, echo) -> None:
    if not install.services:
        return
    try:
        svc.run_systemctl("start", install.services)
    except (OSError, subprocess.SubprocessError) as exc:
        echo(f"WARNING: could not restart services: {exc}")


def _installed_version(install) -> str | None:
    if install.dtxwiki_bin is None:
        return None
    try:
        result = subprocess.run(
            [str(install.dtxwiki_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text.split()[-1] if text else None


def _echo_plan(echo, install, install_argv, no_backup, non_sqlite_db) -> None:
    echo(f"DRY RUN — update plan ({install.kind}):")
    if install.services:
        echo(f"  1. systemctl stop {' '.join(install.services)}")
    echo("  - probe SQLite write lock")
    if no_backup:
        echo("  - SKIP backup")
    elif non_sqlite_db:
        echo(
            "  - create pre-update backup (secrets only; DB NOT included — "
            "non-SQLite database)"
        )
    else:
        echo("  - create pre-update backup")
    echo("  - " + _redacted_argv(install_argv))
    echo("  - dtxwiki update --finalize-bootstrap   (migrations, new code)")
    if install.services:
        echo(f"  - systemctl start {' '.join(install.services)}")
        echo("  - poll /health/ready")
    echo("Re-run without --dry-run to apply.")


#: The image compose builds from the checkout when ``DTXWIKI_IMAGE`` is unset.
DOCKER_LOCAL_IMAGE = "dtxwiki:local"


def _docker_image_is_registry_ref(image: str | None) -> bool:
    """Whether ``DTXWIKI_IMAGE`` names an image ``docker compose pull`` can fetch.

    The default stack builds ``dtxwiki:local`` from the checkout and pushes it
    nowhere, so pulling it fails and -- because the documented flow stops the
    services first -- leaves them stopped. Only a namespaced reference
    (``registry.example.com/dtxwiki:0.1.1``, ``ghcr.io/org/dtxwiki:0.1.1``)
    points at a registry.
    """

    if not image:
        return False
    image = image.strip()
    return image != DOCKER_LOCAL_IMAGE and "/" in image


def _image_with_tag(image: str, version: str | None) -> str:
    """``image`` retagged to ``version`` (a registry port is not a tag)."""

    image = image.strip()
    if not version:
        return image
    repository = image.split("@", 1)[0]
    head, _, last = repository.rpartition("/")
    name = last.split(":", 1)[0]
    return f"{head}/{name}:{version}" if head else f"{name}:{version}"


def _docker_instructions(settings, version, resolved_index, echo) -> UpdateResult:
    target_tag = f"v{version}" if version else "vX.Y.Z"
    image = os.environ.get("DTXWIKI_IMAGE")
    echo("Docker install detected — run the update on the HOST, not in-container:")
    if image is not None and _docker_image_is_registry_ref(image):
        target_image = _image_with_tag(image, version)
        echo("  cd infra/prod")
        echo("  # 1. back up the data volume (see DEPLOYMENT.md), then:")
        echo(f"  # 2. set DTXWIKI_IMAGE={target_image} in infra/prod/.env")
        echo("  docker compose pull")
        echo("  docker compose up -d")
    else:
        echo("  # in the dtxWiki checkout the stack is built from:")
        echo(f"  git fetch --tags && git checkout {target_tag}")
        echo("  # back up the data volume (see DEPLOYMENT.md), then:")
        echo("  cd infra/prod")
        if resolved_index:
            echo(
                "  docker compose build "
                f"--build-arg PIP_INDEX_URL={redact_url(resolved_index)}"
            )
        else:
            echo("  docker compose build")
        echo("  docker compose up -d")
    echo(
        "  # the init service runs migrations (dtxwiki bootstrap) before the "
        "app and worker start"
    )
    return UpdateResult(
        False, 2, settings.app_version, version, None, "docker: run on host (see above)"
    )


#: The npm package whose launcher sets ``DTXWIKI_LAUNCHER=npm``.
NPM_PACKAGE = "@dtxsuite/dtxwiki"


def _npm_update_instructions(settings, version, echo) -> UpdateResult:
    target = version or "latest"
    echo(
        f"npm launcher detected — the {NPM_PACKAGE} version decides which "
        "dtxWiki runs, so update the launcher instead:"
    )
    echo("  # 1. stop dtxWiki (server and worker), then back up the data directory:")
    echo(f"  #    {data_dir_paths.data_dir()}")
    echo("  # 2. start the new version; it migrates the database as it starts:")
    echo(f"  npx {NPM_PACKAGE}@{target}")
    echo(
        "  # a global install updates with: "
        f"npm install --global {NPM_PACKAGE}@{target}"
    )
    return UpdateResult(
        False,
        2,
        settings.app_version,
        version,
        None,
        "npm: update the launcher (see above)",
    )


def _npm_rollback_instructions(settings, echo) -> UpdateResult:
    echo(
        f"npm launcher detected — the {NPM_PACKAGE} version decides which "
        "dtxWiki runs, so 'dtxwiki rollback' cannot reinstall an older one:"
    )
    echo("  # 1. stop dtxWiki (server and worker)")
    echo("  # 2. restore the data directory from the copy taken before the update:")
    echo(f"  #    {data_dir_paths.data_dir()}")
    echo("  # 3. start the version that copy belongs to, for example:")
    echo(f"  npx {NPM_PACKAGE}@<version>")
    return UpdateResult(
        False, 2, settings.app_version, None, None, "npm: manual rollback"
    )


def _fail(
    exit_code: int,
    message: str,
    backup_dir: Path | None,
    current: str,
    echo,
    exc: Exception | None,
    *,
    operation: str = "update",
) -> UpdateResult:
    echo(f"ERROR: {operation} failed — {message}.")
    if exc is not None:
        echo(f"  cause: {exc}")
    if backup_dir is not None:
        if operation == "rollback":
            echo(
                "  the system may be in a partially-rolled-back state. To retry "
                "the restore:"
            )
        else:
            echo(
                "  the system may be in a partially-updated state. To revert code + DB:"
            )
        echo(f"    dtxwiki rollback --to-backup {backup_dir}")
    else:
        echo("  no backup was taken; no automatic recovery point available.")
    return UpdateResult(False, exit_code, current, None, backup_dir, message)
