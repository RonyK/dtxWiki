"""Pre-update backups of dtxWiki state, with manifest, retention, and restore.

The default SQLite backup is an operational checkpoint: the database and the
configured immutable Library blob tree are captured together. The database
contains converter provenance while the tree contains non-rebuildable source
blobs (plus rebuildable derived blobs), so restoring only one side is unsafe.
Legacy uploads remain opt-in through ``include_files=True``.

The SQLite copy uses the stdlib online-backup API (``Connection.backup``), which
produces a single checkpointed file (WAL folded in) — no separate ``-wal``/
``-shm`` to reason about.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy.engine import make_url

from backend.app.data_dir import data_dir as default_data_dir
from backend.app.settings import Settings
from backend.services.ops import db_lock

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 2
DEFAULT_KEEP = 5
BACKUPS_DIRNAME = "backups"

# systemd service account group (matches install.sh + dtxwiki.service); the
# restored JWT key must stay group-readable by it.
SERVICE_GROUP = "dtxwiki"

DB_BACKUP_NAME = "dtxwiki.sqlite"
JWT_BACKUP_NAME = "jwt.pem"
SESSION_SECRET_BACKUP_NAME = "session_secret"
ENV_BACKUP_NAME = "dtxwiki.env"
USER_FILES_DIRNAME = "files"
LIBRARY_CHECKPOINT_DIRNAME = "library"
LIBRARY_CHECKPOINT_PREFIX = f"{USER_FILES_DIRNAME}/{LIBRARY_CHECKPOINT_DIRNAME}"

# Bounds protect backup/restore from an accidentally selected filesystem root,
# a corrupt manifest, and pathological directory trees. They intentionally
# exceed the supported per-upload limits by a wide margin.
MAX_CHECKPOINT_FILES = 1_000_000
MAX_CHECKPOINT_NODES = 2_000_000
MAX_CHECKPOINT_TOTAL_BYTES = 4 * 1024**4
MAX_CHECKPOINT_FILE_BYTES = 8 * 1024**3
MAX_MANIFEST_FILES = MAX_CHECKPOINT_FILES + 16
MAX_MANIFEST_TOTAL_BYTES = MAX_CHECKPOINT_TOTAL_BYTES + 32 * 1024**3
MAX_MANIFEST_FILE_BYTES = 1024**4
MAX_MANIFEST_BYTES = 512 * 1024**2
MAX_MANIFEST_PATH_BYTES = 4096
_COPY_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# The pre-0.1.1 development placeholder. Settings now rejects it at startup; it
# stays here only so a backup can never copy it as if it were a real secret.
_DEV_SESSION_SECRET = "dtxwiki-development-session-secret"


class BackupIntegrityError(RuntimeError):
    """A backed-up file is missing or its sha256 does not match the manifest."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    manifest: dict


@dataclass
class _RestoreSwap:
    """One staged rename in a recoverable multi-target restore cutover."""

    target: Path
    staged: Path | None
    previous: Path
    had_previous: bool = False
    installed: bool = False

    def commit(self) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if _path_exists(self.target):
            os.replace(self.target, self.previous)
            self.had_previous = True
        if self.staged is not None:
            os.replace(self.staged, self.target)
            self.installed = True

    def rollback(self) -> None:
        if self.installed and _path_exists(self.target):
            _remove_path(self.target)
        if self.had_previous and _path_exists(self.previous):
            os.replace(self.previous, self.target)

    def cleanup(self) -> None:
        if _path_exists(self.previous):
            _remove_path(self.previous)
        if self.staged is not None and _path_exists(self.staged):
            _remove_path(self.staged)


def db_path_from_url(database_url: str) -> Path | None:
    """Resolve the on-disk SQLite file path from a SQLAlchemy URL.

    Uses ``make_url`` so the relative (``///``) vs absolute (``////``) and the
    Windows (``///C:/...``) forms are all handled correctly. Returns ``None`` for
    non-SQLite or in-memory URLs.
    """

    if not db_lock.is_sqlite_url(database_url):
        return None
    database = make_url(database_url).database
    if not database or database == ":memory:":
        return None
    return Path(database)


def create_backup(
    settings: Settings,
    *,
    version: str | None = None,
    alembic_revision: str | None = None,
    install_method: str | None = None,
    index_url: str | None = None,
    include_files: bool = False,
    data_dir: Path | None = None,
    env_file: Path | None = None,
) -> Path:
    """Snapshot DB + Library blobs + secrets into a timestamped directory.

    A SQLite snapshot always includes the configured Library root, even when it
    is empty. This keeps source blobs and their database provenance in one
    checkpoint. ``include_files`` retains its legacy meaning for the unrelated
    uploads tree. Non-SQLite installations keep the existing secrets-only
    behavior because this service cannot make an external database checkpoint.

    The directory is assembled and verified under an ``.incomplete`` sibling,
    then renamed into the visible backups namespace. Services should be stopped
    first so both the DB and blob tree are quiescent.
    """

    base = (data_dir or default_data_dir()).expanduser().resolve()
    backups_root = base / BACKUPS_DIRNAME
    backups_root.mkdir(parents=True, exist_ok=True)
    _chmod_700(backups_root)

    resolved_version = version or settings.app_version
    final_dir = _allocate_backup_dir(backups_root, resolved_version)
    # Keep the staging segment short enough for Windows MAX_PATH installations;
    # the final directory still carries the human-readable version/timestamp.
    backup_dir = backups_root / f".tmp-{uuid4().hex[:12]}"
    backup_dir.mkdir(parents=False)
    _chmod_700(backup_dir)

    try:
        files: list[dict] = []

        db_path = db_path_from_url(settings.database_url)
        includes_db = db_path is not None and db_path.is_file()
        if includes_db:
            dest = backup_dir / DB_BACKUP_NAME
            _sqlite_backup(db_path, dest)
            files.append(_file_entry(dest))

        library_checkpoint = _omitted_library_checkpoint()
        if includes_db:
            library_root = _library_root(settings, base)
            _validate_library_root_location(library_root, backups_root)
            library_entries, library_checkpoint = _copy_library_checkpoint(
                library_root, backup_dir
            )
            files.extend(library_entries)

        if settings.oauth_jwt_private_key_path:
            jwt_src = Path(settings.oauth_jwt_private_key_path)
            if jwt_src.exists():
                files.append(_copy_secret(jwt_src, backup_dir / JWT_BACKUP_NAME))

        # The session secret lives as the DTXWIKI_SESSION_SECRET *value* (env
        # file), not a standalone file, so capture the resolved value on EVERY
        # tier. A legacy {data_dir}/session_secret file still takes precedence.
        secret_dest = backup_dir / SESSION_SECRET_BACKUP_NAME
        legacy_secret_file = base / SESSION_SECRET_BACKUP_NAME
        if legacy_secret_file.exists():
            files.append(_copy_secret(legacy_secret_file, secret_dest))
        elif settings.session_secret and settings.session_secret != _DEV_SESSION_SECRET:
            secret_dest.write_text(settings.session_secret, encoding="utf-8")
            _chmod_600(secret_dest)
            files.append(_file_entry(secret_dest))

        if env_file is not None and Path(env_file).exists():
            files.append(_copy_secret(Path(env_file), backup_dir / ENV_BACKUP_NAME))

        if include_files:
            files.extend(_copy_user_files(base, backup_dir))

        checkpoint_kind = "full" if includes_db else "secrets_only"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "dtxwiki_version": resolved_version,
            "alembic_revision": alembic_revision,
            "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "install_method": install_method,
            "database_url_redacted": _redact(settings.database_url),
            "index_url": _redact(index_url) if index_url else index_url,
            "checkpoint_kind": checkpoint_kind,
            "checkpoint_complete": checkpoint_kind == "full",
            "database_snapshot_method": (
                "sqlite3_backup_delete_journal" if includes_db else None
            ),
            "files": files,
            "includes_db": includes_db,
            "includes_library_blobs": library_checkpoint["included"],
            "library_checkpoint": library_checkpoint,
            "includes_user_files": include_files,
        }
        write_manifest(backup_dir, manifest)
        verify_backup_integrity(backup_dir)
        if final_dir.exists():
            raise FileExistsError(f"backup destination already exists: {final_dir}")
        backup_dir.rename(final_dir)
        return final_dir
    except BaseException:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise


def restore_backup(
    backup: Path,
    settings: Settings,
    *,
    data_dir: Path | None = None,
    env_file: Path | None = None,
) -> dict:
    """Restore a verified checkpoint from a backup dir (or manifest path).

    Schema-v2 full checkpoints stage the SQLite file and Library tree beside
    their live targets, then cut both over with recoverable rename operations.
    A failed cutover rolls every already-moved target back before returning.
    Schema-v1 backups retain their historical restore behavior.

    Services must be stopped first. Returns the manifest. Rollback restores the
    DB together with reinstalling the matching code version, so the downversion
    fail-safe is never tripped.
    """

    backup_dir = resolve_backup_dir(backup)
    manifest = read_manifest(backup_dir)
    verify_backup_integrity(backup_dir)  # refuse to restore a corrupt snapshot
    base = (data_dir or default_data_dir()).expanduser().resolve()

    db_backup = backup_dir / DB_BACKUP_NAME
    db_path = db_path_from_url(settings.database_url)
    schema = manifest.get("schema", 1)
    if (
        schema >= 2
        and manifest.get("checkpoint_kind") == "full"
        and db_path is not None
    ):
        library_root = _library_root(settings, base)
        _validate_library_root_location(library_root, base / BACKUPS_DIRNAME)
        _restore_full_checkpoint(
            backup_dir,
            manifest,
            db_backup=db_backup,
            db_target=_absolute_path(db_path),
            library_target=library_root,
        )
    elif db_path is not None and db_backup.exists():
        _restore_sqlite(db_backup, db_path)

    jwt_backup = backup_dir / JWT_BACKUP_NAME
    if jwt_backup.exists() and settings.oauth_jwt_private_key_path:
        jwt_dest = Path(settings.oauth_jwt_private_key_path)
        _restore_secret(jwt_backup, jwt_dest)
        # Restore runs as root, so the key would land root:root 0600 and the
        # dtxwiki systemd service (User=dtxwiki) could not read it -> crash loop.
        # Re-grant root:dtxwiki 0640 exactly as `dtxwiki setup` does. No-op when
        # not root (dev/pipx/Windows keep their own perms).
        from backend.cli.prod_setup import set_key_ownership

        set_key_ownership(jwt_dest, SERVICE_GROUP)

    secret_backup = backup_dir / SESSION_SECRET_BACKUP_NAME
    if secret_backup.exists():
        # Codex #34: {data_dir}/session_secret is the path the Docker entrypoint
        # reads to repopulate DTXWIKI_SESSION_SECRET; on systemd the secret comes
        # back via the env-file restore below. (On a bare pipx/venv install with
        # neither mechanism it is inert but harmless.)
        _restore_secret(secret_backup, base / SESSION_SECRET_BACKUP_NAME)

    env_backup = backup_dir / ENV_BACKUP_NAME
    if env_backup.exists() and env_file is not None:
        _restore_secret(env_backup, Path(env_file))

    # Schema-v1 include_files backups contained both uploads and library. In v2,
    # Library is the separately fenced checkpoint and this path restores uploads.
    if manifest.get("includes_user_files"):
        _restore_user_files(backup_dir, base, include_library=schema < 2)

    return manifest


def list_backups(data_dir: Path | None = None) -> list[BackupInfo]:
    """Backups newest-first (by manifest created_utc, dir name as fallback)."""

    base = (data_dir or default_data_dir()) / BACKUPS_DIRNAME
    if not base.is_dir():
        return []
    infos: list[BackupInfo] = []
    for child in base.iterdir():
        if child.name.startswith("."):
            continue
        try:
            _require_directory(child, label="backup directory")
        except (OSError, BackupIntegrityError):
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_manifest(child)
        except (OSError, json.JSONDecodeError, BackupIntegrityError):
            continue
        infos.append(BackupInfo(path=child, manifest=manifest))
    infos.sort(
        key=lambda info: (info.manifest.get("created_utc", ""), info.path.name),
        reverse=True,
    )
    return infos


def prune_backups(data_dir: Path | None = None, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Keep the newest ``keep`` backups plus the latest of the newest ``keep``
    distinct versions (rollback insurance).

    The per-version insurance is bounded to the most recent ``keep`` versions so
    retention does not grow without bound as every update bumps the version.
    Returns the directories that were removed.
    """

    keep = max(1, keep)
    infos = list_backups(data_dir)
    survivors: set[Path] = {info.path for info in infos[:keep]}
    seen_versions: list[str] = []
    for info in infos:
        version = info.manifest.get("dtxwiki_version")
        if version is not None and version not in seen_versions:
            seen_versions.append(version)
            # Bound per-version insurance to the newest keep+1 distinct versions
            # (so growth is bounded but the immediately-previous version is always
            # retained for rollback, even when keep=1).
            if len(seen_versions) <= keep + 1:
                survivors.add(info.path)
    removed: list[Path] = []
    for info in infos:
        if info.path not in survivors:
            shutil.rmtree(info.path, ignore_errors=True)
            removed.append(info.path)
    return removed


def write_manifest(backup_dir: Path, manifest: dict) -> None:
    manifest_path = backup_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    _chmod_600(manifest_path)


def read_manifest(backup_dir: Path) -> dict:
    manifest_path = _safe_backup_member(backup_dir, MANIFEST_NAME)
    _require_regular_file(manifest_path, label="backup manifest")
    if manifest_path.stat(follow_symlinks=False).st_size > MAX_MANIFEST_BYTES:
        raise BackupIntegrityError("backup manifest exceeds the size limit")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackupIntegrityError("backup manifest must be a JSON object")
    return value


def verify_backup_integrity(backup: Path) -> None:
    """Raise :class:`BackupIntegrityError` if any recorded file is missing/altered.

    Recomputes each manifest file's sha256 and compares it to the recorded value,
    so a truncated or corrupted backup is refused *before* it is restored.
    """

    backup_dir = resolve_backup_dir(backup)
    manifest = read_manifest(backup_dir)
    schema = manifest.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema not in (1, 2):
        raise BackupIntegrityError(f"unsupported backup manifest schema: {schema!r}")

    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise BackupIntegrityError("backup manifest files must be a list")
    if len(raw_entries) > MAX_MANIFEST_FILES:
        raise BackupIntegrityError("backup manifest exceeds the file-count limit")

    entries: dict[str, dict] = {}
    total_bytes = 0
    for raw_entry in raw_entries:
        entry = _validate_file_entry(raw_entry)
        name = entry["name"]
        if name in entries:
            raise BackupIntegrityError(f"duplicate backup manifest path: {name}")
        entries[name] = entry
        total_bytes += entry["bytes"]
        if total_bytes > MAX_MANIFEST_TOTAL_BYTES:
            raise BackupIntegrityError("backup manifest exceeds the byte limit")

        path = _safe_backup_member(backup_dir, name)
        _require_regular_file(path, label=f"backup file {name}")
        if path.stat(follow_symlinks=False).st_size != entry["bytes"]:
            raise BackupIntegrityError(f"backup file size mismatch: {name}")
        if _sha256(path) != entry["sha256"]:
            raise BackupIntegrityError(f"backup file sha256 mismatch: {name}")

    if schema >= 2:
        _verify_v2_checkpoint(backup_dir, manifest, entries)


# --- internals -----------------------------------------------------------


def _validate_file_entry(raw_entry: object) -> dict:
    if not isinstance(raw_entry, dict):
        raise BackupIntegrityError("backup manifest file entry must be an object")
    name = raw_entry.get("name")
    sha256 = raw_entry.get("sha256")
    byte_size = raw_entry.get("bytes")
    if not isinstance(name, str):
        raise BackupIntegrityError("backup manifest file name must be a string")
    _validate_manifest_name(name)
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or sha256.lower() != sha256
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise BackupIntegrityError(f"invalid backup sha256 for: {name}")
    if (
        not isinstance(byte_size, int)
        or isinstance(byte_size, bool)
        or byte_size < 0
        or byte_size > MAX_MANIFEST_FILE_BYTES
    ):
        raise BackupIntegrityError(f"invalid backup byte size for: {name}")
    return {"name": name, "sha256": sha256, "bytes": byte_size}


def _verify_v2_checkpoint(
    backup_dir: Path, manifest: dict, entries: dict[str, dict]
) -> None:
    kind = manifest.get("checkpoint_kind")
    complete = manifest.get("checkpoint_complete")
    includes_db = manifest.get("includes_db")
    includes_library = manifest.get("includes_library_blobs")
    snapshot_method = manifest.get("database_snapshot_method")
    if kind not in {"full", "secrets_only"}:
        raise BackupIntegrityError("invalid backup checkpoint kind")
    if not isinstance(complete, bool):
        raise BackupIntegrityError("invalid checkpoint completion marker")
    # A short-lived pre-v2 manifest variant omitted includes_db; its actual
    # checkpoint contents remain unambiguous. Keep that rollback path compatible.
    if includes_db is None:
        includes_db = database_recorded = DB_BACKUP_NAME in entries
    else:
        database_recorded = DB_BACKUP_NAME in entries
    if not isinstance(includes_db, bool) or not isinstance(includes_library, bool):
        raise BackupIntegrityError("invalid checkpoint inclusion marker")
    if snapshot_method not in {None, "sqlite3_backup_delete_journal"}:
        raise BackupIntegrityError("invalid database snapshot method")
    library_entries = {
        name: entry
        for name, entry in entries.items()
        if name == LIBRARY_CHECKPOINT_PREFIX
        or name.startswith(f"{LIBRARY_CHECKPOINT_PREFIX}/")
    }
    library = manifest.get("library_checkpoint")
    if not isinstance(library, dict):
        raise BackupIntegrityError("missing Library checkpoint metadata")

    if kind == "full":
        if not complete or not includes_db or not includes_library:
            raise BackupIntegrityError("partial full checkpoint")
        if not database_recorded:
            raise BackupIntegrityError("full checkpoint is missing its database")
        if snapshot_method == "sqlite3_backup_delete_journal":
            _verify_standalone_sqlite_checkpoint(
                _safe_backup_member(backup_dir, DB_BACKUP_NAME)
            )
        _verify_library_checkpoint(backup_dir, library, library_entries)
        _verify_library_database_references(
            _safe_backup_member(backup_dir, DB_BACKUP_NAME), library_entries
        )
    else:
        if (
            complete
            or includes_db
            or includes_library
            or database_recorded
            or snapshot_method is not None
        ):
            raise BackupIntegrityError("inconsistent secrets-only checkpoint")
        if library_entries or library.get("included") is not False:
            raise BackupIntegrityError(
                "secrets-only checkpoint unexpectedly contains Library blobs"
            )

    # A v2 manifest enumerates every regular payload file. Reject unexpected
    # files as well as missing ones so a partial or augmented checkpoint cannot
    # be cut over accidentally. The manifest itself is the sole exception.
    actual_names = {
        relative
        for _, relative, _ in _walk_regular_files(backup_dir)
        if relative != MANIFEST_NAME
    }
    if actual_names != set(entries):
        missing = sorted(set(entries) - actual_names)
        unexpected = sorted(actual_names - set(entries))
        detail = missing[0] if missing else unexpected[0]
        label = "missing" if missing else "unexpected"
        raise BackupIntegrityError(f"{label} checkpoint payload: {detail}")


def _verify_library_checkpoint(
    backup_dir: Path, library: dict, entries: dict[str, dict]
) -> None:
    if library.get("included") is not True:
        raise BackupIntegrityError("full checkpoint omits the Library tree")
    if library.get("tree_prefix") != LIBRARY_CHECKPOINT_PREFIX:
        raise BackupIntegrityError("invalid Library checkpoint prefix")
    file_count = library.get("file_count")
    total_bytes = library.get("total_bytes")
    tree_sha256 = library.get("tree_sha256")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count < 0
        or file_count > MAX_CHECKPOINT_FILES
    ):
        raise BackupIntegrityError("invalid Library checkpoint file count")
    if (
        not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < 0
        or total_bytes > MAX_CHECKPOINT_TOTAL_BYTES
    ):
        raise BackupIntegrityError("invalid Library checkpoint byte count")
    if not _is_sha256(tree_sha256):
        raise BackupIntegrityError("invalid Library checkpoint tree digest")

    relative_entries = _library_relative_entries(entries.values())
    summary = _tree_summary(relative_entries)
    if (
        file_count != summary["file_count"]
        or total_bytes != summary["total_bytes"]
        or tree_sha256 != summary["tree_sha256"]
    ):
        raise BackupIntegrityError("Library checkpoint summary mismatch")

    tree_root = _safe_backup_member(backup_dir, LIBRARY_CHECKPOINT_PREFIX)
    _require_directory(tree_root, label="Library checkpoint tree")
    actual = {relative for _, relative, _ in _walk_regular_files(tree_root)}
    expected = {entry["name"] for entry in relative_entries}
    if actual != expected:
        raise BackupIntegrityError("Library checkpoint tree is partial")


def _verify_library_database_references(
    database: Path, library_entries: dict[str, dict]
) -> None:
    """Ensure every persisted blob provenance row has its immutable payload."""

    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        try:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            version_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(library_item_version)")
            }
            artifact_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(library_artifact)")
            }
            context_asset_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(context_media_asset)")
            }
            shared_blob_schema = {"stored_blob", "blob_incarnation"}.issubset(
                table_names
            )
            reference_selects: list[str] = []
            if {
                "source_blob_workspace_id",
                "source_blob_id",
                "source_blob_epoch",
            }.issubset(version_columns):
                reference_selects.append(
                    "SELECT source_blob_workspace_id, source_blob_id, "
                    "source_blob_epoch FROM library_item_version "
                    "WHERE source_blob_id IS NOT NULL"
                )
            if {"blob_workspace_id", "blob_id", "blob_epoch"}.issubset(
                artifact_columns
            ):
                # A-1: conversion- and preview-stage rows share this one table
                # now, so this single SELECT covers both.
                reference_selects.append(
                    "SELECT blob_workspace_id, blob_id, blob_epoch "
                    "FROM library_artifact WHERE blob_id IS NOT NULL"
                )
            if {"workspace_id", "blob_id", "blob_epoch"}.issubset(
                context_asset_columns
            ):
                reference_selects.append(
                    "SELECT workspace_id, blob_id, blob_epoch "
                    "FROM context_media_asset WHERE blob_id IS NOT NULL"
                )
            if shared_blob_schema and reference_selects:
                rows = connection.execute(
                    "WITH media_references(workspace_id, blob_id, blob_epoch) AS ("
                    + " UNION ".join(reference_selects)
                    + ") "
                    "SELECT DISTINCT stored_blob.sha256, "
                    "blob_incarnation.byte_size, blob_incarnation.storage_key "
                    "FROM media_references "
                    "JOIN blob_incarnation ON "
                    "blob_incarnation.workspace_id = media_references.workspace_id "
                    "AND blob_incarnation.blob_id = media_references.blob_id "
                    "AND blob_incarnation.blob_epoch = "
                    "media_references.blob_epoch "
                    "JOIN stored_blob ON "
                    "stored_blob.workspace_id = media_references.workspace_id "
                    "AND stored_blob.id = media_references.blob_id"
                )
            elif "library_blob" in table_names:
                rows = connection.execute(
                    "SELECT sha256, byte_size, storage_key FROM library_blob"
                )
            else:
                return
            for sha256, byte_size, storage_key in rows:
                if not isinstance(storage_key, str):
                    raise BackupIntegrityError(
                        "Library blob row has an invalid storage key"
                    )
                relative = f"blobs/{storage_key}"
                _validate_manifest_name(relative)
                entry_name = f"{LIBRARY_CHECKPOINT_PREFIX}/{relative}"
                entry = library_entries.get(entry_name)
                if entry is None:
                    raise BackupIntegrityError(
                        f"Library checkpoint is missing blob: {storage_key}"
                    )
                if entry["sha256"] != sha256 or entry["bytes"] != byte_size:
                    raise BackupIntegrityError(
                        f"Library blob provenance mismatch: {storage_key}"
                    )
        finally:
            connection.close()
    except BackupIntegrityError:
        raise
    except sqlite3.Error as exc:
        raise BackupIntegrityError(
            "cannot validate Library provenance in the database checkpoint"
        ) from exc


def _verify_standalone_sqlite_checkpoint(database: Path) -> None:
    try:
        with database.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise BackupIntegrityError("cannot read SQLite checkpoint header") from exc
    if (
        len(header) < 20
        or header[:16] != b"SQLite format 3\x00"
        or header[18:20] != b"\x01\x01"
    ):
        raise BackupIntegrityError(
            "database snapshot is not a standalone DELETE-journal checkpoint"
        )


def _validate_manifest_name(name: str) -> None:
    if not name or len(name.encode("utf-8")) > MAX_MANIFEST_PATH_BYTES:
        raise BackupIntegrityError("backup manifest path is empty or too long")
    if "\\" in name or "\x00" in name:
        raise BackupIntegrityError(f"unsafe backup manifest path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or pure.as_posix() != name:
        raise BackupIntegrityError(f"unsafe backup manifest path: {name!r}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise BackupIntegrityError(f"unsafe backup manifest path: {name!r}")


def _safe_backup_member(backup_dir: Path, name: str) -> Path:
    _validate_manifest_name(name)
    _require_directory(backup_dir, label="backup directory")
    candidate = backup_dir.joinpath(*PurePosixPath(name).parts)
    current = backup_dir
    for part in PurePosixPath(name).parts:
        current /= part
        if _path_exists(current):
            _reject_link_or_reparse(current)
    return candidate


def _copy_library_checkpoint(
    library_root: Path, backup_dir: Path
) -> tuple[list[dict], dict]:
    target = backup_dir / USER_FILES_DIRNAME / LIBRARY_CHECKPOINT_DIRNAME
    if _path_exists(library_root):
        _require_directory(library_root, label="configured Library root")
        entries = _copy_tree_snapshot(
            library_root,
            target,
            manifest_prefix=LIBRARY_CHECKPOINT_PREFIX,
            exclude_top_level={"staging"},
        )
    else:
        target.mkdir(parents=True)
        _chmod_700(target)
        entries = []
    relative_entries = _library_relative_entries(entries)
    checkpoint = {"included": True, "tree_prefix": LIBRARY_CHECKPOINT_PREFIX}
    checkpoint.update(_tree_summary(relative_entries))
    return entries, checkpoint


def _omitted_library_checkpoint() -> dict:
    return {"included": False, "reason": "database_not_included"}


def _library_relative_entries(entries: object) -> list[dict]:
    prefix = f"{LIBRARY_CHECKPOINT_PREFIX}/"
    relative: list[dict] = []
    for raw_entry in entries:
        entry = dict(raw_entry)
        name = entry["name"]
        if not name.startswith(prefix):
            raise BackupIntegrityError("Library entry is outside its tree prefix")
        entry["name"] = name[len(prefix) :]
        _validate_manifest_name(entry["name"])
        relative.append(entry)
    return sorted(relative, key=lambda entry: entry["name"])


def _tree_summary(entries: list[dict]) -> dict:
    ordered = sorted(entries, key=lambda entry: entry["name"])
    encoded = json.dumps(
        ordered,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "file_count": len(ordered),
        "total_bytes": sum(entry["bytes"] for entry in ordered),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _copy_tree_snapshot(
    source: Path,
    target: Path,
    *,
    manifest_prefix: str,
    exclude_top_level: set[str] | None = None,
) -> list[dict]:
    if _path_exists(target):
        raise FileExistsError(f"snapshot target already exists: {target}")
    target.mkdir(parents=True)
    _chmod_700(target)
    walked = _walk_regular_files(source, exclude_top_level=exclude_top_level or set())
    entries: list[dict] = []
    total_bytes = 0
    try:
        for source_path, relative, source_stat in walked:
            destination = target.joinpath(*PurePosixPath(relative).parts)
            sha256, byte_size = _copy_regular_file(
                source_path, destination, expected_stat=source_stat
            )
            total_bytes += byte_size
            if total_bytes > MAX_CHECKPOINT_TOTAL_BYTES:
                raise BackupIntegrityError("checkpoint tree exceeds the byte limit")
            entries.append(
                {
                    "name": f"{manifest_prefix}/{relative}",
                    "sha256": sha256,
                    "bytes": byte_size,
                }
            )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return entries


def _walk_regular_files(
    root: Path, *, exclude_top_level: set[str] | None = None
) -> list[tuple[Path, str, os.stat_result]]:
    _require_directory(root, label="checkpoint tree")
    excluded = exclude_top_level or set()
    stack: list[tuple[Path, PurePosixPath | None]] = [(root, None)]
    result: list[tuple[Path, str, os.stat_result]] = []
    node_count = 0
    while stack:
        directory, relative_dir = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise BackupIntegrityError(
                f"cannot enumerate checkpoint tree: {directory}"
            ) from exc
        for child in children:
            node_count += 1
            if node_count > MAX_CHECKPOINT_NODES:
                raise BackupIntegrityError("checkpoint tree exceeds the node limit")
            child_path = Path(child.path)
            child_stat = child.stat(follow_symlinks=False)
            if child.is_symlink() or _stat_is_reparse(child_stat):
                raise BackupIntegrityError(
                    f"checkpoint tree contains a link or reparse point: {child_path}"
                )
            relative = (
                PurePosixPath(child.name)
                if relative_dir is None
                else relative_dir / child.name
            )
            relative_text = relative.as_posix()
            _validate_manifest_name(relative_text)
            if stat.S_ISDIR(child_stat.st_mode):
                if relative_dir is None and child.name in excluded:
                    continue
                stack.append((child_path, relative))
            elif stat.S_ISREG(child_stat.st_mode):
                if child_stat.st_size > MAX_CHECKPOINT_FILE_BYTES:
                    raise BackupIntegrityError(
                        f"checkpoint file exceeds the size limit: {relative_text}"
                    )
                result.append((child_path, relative_text, child_stat))
                if len(result) > MAX_CHECKPOINT_FILES:
                    raise BackupIntegrityError(
                        "checkpoint tree exceeds the file-count limit"
                    )
            else:
                raise BackupIntegrityError(
                    f"checkpoint tree contains a special file: {child_path}"
                )
    return sorted(result, key=lambda item: item[1])


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_stat: os.stat_result,
    max_bytes: int = MAX_CHECKPOINT_FILE_BYTES,
) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or _stat_is_reparse(opened_stat):
            raise BackupIntegrityError(f"checkpoint source is not regular: {source}")
        if not _same_file_snapshot(expected_stat, opened_stat):
            raise BackupIntegrityError(
                f"checkpoint source changed before copy: {source}"
            )
        with os.fdopen(descriptor, "rb") as source_handle:
            descriptor = -1
            with destination.open("xb") as destination_handle:
                for chunk in iter(lambda: source_handle.read(_COPY_CHUNK_BYTES), b""):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise BackupIntegrityError(
                            f"checkpoint source exceeds the size limit: {source}"
                        )
                    destination_handle.write(chunk)
                    digest.update(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        final_stat = source.stat(follow_symlinks=False)
        if (
            copied != opened_stat.st_size
            or _stat_is_reparse(final_stat)
            or not _same_file_snapshot(opened_stat, final_stat)
        ):
            raise BackupIntegrityError(
                f"checkpoint source changed during copy: {source}"
            )
        _chmod_600(destination)
        return digest.hexdigest(), copied
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    identity_matches = (
        not left.st_ino
        or not right.st_ino
        or (left.st_dev == right.st_dev and left.st_ino == right.st_ino)
    )
    return (
        identity_matches
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value.lower() == value
        and all(character in "0123456789abcdef" for character in value)
    )


def _allocate_backup_dir(backups_root: Path, version: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = backups_root / f"{version}-{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = backups_root / f"{version}-{stamp}-{suffix}"
        suffix += 1
    return candidate


def _sqlite_backup(src: Path, dest: Path) -> None:
    # Codex #39 (solo/pipx tier has no service to stop): rely on SQLite's online
    # backup, which is ITSELF consistency-safe — sqlite3_backup_step restarts the
    # copy if a concurrent writer modifies the source mid-backup, so the snapshot
    # never lags the committed state. busy_timeout bounds any lock wait so a
    # writer that slips in between the lock probe and here makes us wait rather
    # than raise "database is locked". We deliberately do NOT hold a BEGIN
    # IMMEDIATE across the copy: an explicit write transaction on the source
    # conflicts with the backup API (it cannot back up from a connection that is
    # mid-transaction) and would deadlock — the online backup's auto-restart is
    # the correct, lock-free guarantee.
    source = sqlite3.connect(str(src), timeout=5.0)
    try:
        source.execute("PRAGMA busy_timeout=5000")
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
            journal_mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise BackupIntegrityError(
                    "SQLite backup could not be normalized to a standalone checkpoint"
                )
        finally:
            target.close()
    finally:
        source.close()
    # The copy holds password/token hashes — owner-only, like the sibling
    # secrets in the same backup dir.
    _chmod_600(dest)


def _restore_sqlite(src: Path, dest: Path) -> None:
    """Atomically restore a legacy SQLite-only backup with rollback staging."""

    dest = _absolute_path(dest)
    _validate_restore_target(dest, expect_directory=False)
    token = uuid4().hex
    staged = dest.parent / f".{dest.name}.restore-new-{token}"
    expected_stat = src.stat(follow_symlinks=False)
    sha256, byte_size = _copy_regular_file(
        src,
        staged,
        expected_stat=expected_stat,
        max_bytes=MAX_MANIFEST_FILE_BYTES,
    )
    if sha256 != _sha256(src) or byte_size != expected_stat.st_size:
        staged.unlink(missing_ok=True)
        raise BackupIntegrityError("staged SQLite restore failed verification")
    _apply_file_target_identity(staged, dest)
    swaps = [
        _removal_swap(dest.with_name(dest.name + "-wal"), token),
        _removal_swap(dest.with_name(dest.name + "-shm"), token),
        _replacement_swap(dest, staged, token),
    ]
    _commit_restore_swaps(swaps)


def _restore_full_checkpoint(
    backup_dir: Path,
    manifest: dict,
    *,
    db_backup: Path,
    db_target: Path,
    library_target: Path,
) -> None:
    """Stage and recoverably cut over DB + Library as one restore operation."""

    _validate_restore_target(db_target, expect_directory=False)
    _validate_restore_target(library_target, expect_directory=True)
    if _paths_overlap(db_target, library_target):
        raise BackupIntegrityError(
            "SQLite and Library restore targets must not overlap"
        )

    token = uuid4().hex
    staged_db = db_target.parent / f".{db_target.name}.restore-new-{token}"
    staged_library = (
        library_target.parent / f".{library_target.name}.restore-new-{token}"
    )
    db_target.parent.mkdir(parents=True, exist_ok=True)
    library_target.parent.mkdir(parents=True, exist_ok=True)
    library_source = _safe_backup_member(backup_dir, LIBRARY_CHECKPOINT_PREFIX)
    db_entry = next(
        entry for entry in manifest["files"] if entry["name"] == DB_BACKUP_NAME
    )
    library_metadata = manifest["library_checkpoint"]

    try:
        db_stat = db_backup.stat(follow_symlinks=False)
        db_sha256, db_bytes = _copy_regular_file(
            db_backup,
            staged_db,
            expected_stat=db_stat,
            max_bytes=MAX_MANIFEST_FILE_BYTES,
        )
        if db_sha256 != db_entry["sha256"] or db_bytes != db_entry["bytes"]:
            raise BackupIntegrityError("staged SQLite checkpoint mismatch")
        _apply_file_target_identity(staged_db, db_target)

        copied_entries = _copy_tree_snapshot(
            library_source,
            staged_library,
            manifest_prefix=LIBRARY_CHECKPOINT_PREFIX,
        )
        copied_summary = _tree_summary(_library_relative_entries(copied_entries))
        for key in ("file_count", "total_bytes", "tree_sha256"):
            if copied_summary[key] != library_metadata[key]:
                raise BackupIntegrityError(f"staged Library checkpoint {key} mismatch")
        _apply_tree_target_identity(staged_library, library_target)

        swaps = [
            _removal_swap(db_target.with_name(db_target.name + "-wal"), token),
            _removal_swap(db_target.with_name(db_target.name + "-shm"), token),
            _replacement_swap(library_target, staged_library, token),
            _replacement_swap(db_target, staged_db, token),
        ]
        _commit_restore_swaps(swaps)
    except BaseException:
        if _path_exists(staged_db):
            _remove_path(staged_db)
        if _path_exists(staged_library):
            _remove_path(staged_library)
        raise


def _restore_tree_atomic(source: Path, target: Path) -> None:
    target = _absolute_path(target)
    _validate_restore_target(target, expect_directory=True)
    token = uuid4().hex
    staged = target.parent / f".{target.name}.restore-new-{token}"
    _copy_tree_snapshot(source, staged, manifest_prefix="restored")
    _apply_tree_target_identity(staged, target)
    _commit_restore_swaps([_replacement_swap(target, staged, token)])


def _replacement_swap(target: Path, staged: Path, token: str) -> _RestoreSwap:
    previous = target.parent / f".{target.name}.restore-old-{token}"
    return _RestoreSwap(target=target, staged=staged, previous=previous)


def _removal_swap(target: Path, token: str) -> _RestoreSwap:
    previous = target.parent / f".{target.name}.restore-old-{token}"
    return _RestoreSwap(target=target, staged=None, previous=previous)


def _commit_restore_swaps(swaps: list[_RestoreSwap]) -> None:
    committed: list[_RestoreSwap] = []
    try:
        for swap in swaps:
            committed.append(swap)
            swap.commit()
    except BaseException as cutover_error:
        rollback_errors: list[BaseException] = []
        for swap in reversed(committed):
            try:
                swap.rollback()
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        for swap in swaps:
            if swap not in committed:
                try:
                    swap.cleanup()
                except OSError:
                    pass
        if rollback_errors:
            raise BackupIntegrityError(
                "restore cutover failed and rollback could not fully recover"
            ) from rollback_errors[0]
        raise BackupIntegrityError(
            "restore cutover failed; prior state recovered"
        ) from cutover_error
    else:
        for swap in swaps:
            try:
                swap.cleanup()
            except OSError:
                # The live checkpoint is already complete. A leftover uniquely
                # named old/staged path is recoverable operator cleanup, not a
                # reason to roll a valid cutover back.
                pass


def _library_root(settings: Settings, base: Path) -> Path:
    configured = settings.library_blob_root
    return _absolute_path(Path(configured) if configured else base / "library")


def _validate_library_root_location(library_root: Path, backups_root: Path) -> None:
    root = library_root.resolve(strict=False)
    backups = backups_root.resolve(strict=False)
    if _paths_overlap(root, backups):
        raise BackupIntegrityError(
            "configured Library root must not overlap the backups directory"
        )
    if _path_exists(library_root):
        _require_directory(library_root, label="configured Library root")


def _validate_restore_target(target: Path, *, expect_directory: bool) -> None:
    if not target.name or target == target.parent:
        raise BackupIntegrityError("restore target must not be a filesystem root")
    if not _path_exists(target):
        return
    _reject_link_or_reparse(target)
    target_stat = target.stat(follow_symlinks=False)
    mode_matches = (
        stat.S_ISDIR(target_stat.st_mode)
        if expect_directory
        else stat.S_ISREG(target_stat.st_mode)
    )
    if not mode_matches:
        raise BackupIntegrityError(f"unexpected restore target type: {target}")


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    return (
        left_resolved == right_resolved
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _apply_file_target_identity(staged: Path, target: Path) -> None:
    reference = target if _path_exists(target) else target.parent
    reference_stat = reference.stat(follow_symlinks=False)
    mode = stat.S_IMODE(reference_stat.st_mode) if _path_exists(target) else 0o600
    _apply_identity(staged, reference_stat, mode=mode)


def _apply_tree_target_identity(staged: Path, target: Path) -> None:
    """Keep a root-run restore readable by the existing service account."""

    reference = target if _path_exists(target) else target.parent
    reference_stat = reference.stat(follow_symlinks=False)
    root_mode = stat.S_IMODE(reference_stat.st_mode) if _path_exists(target) else 0o700
    _apply_identity(staged, reference_stat, mode=root_mode)

    stack = [staged]
    node_count = 0
    while stack:
        directory = stack.pop()
        for child in os.scandir(directory):
            node_count += 1
            if node_count > MAX_CHECKPOINT_NODES:
                raise BackupIntegrityError("staged restore exceeds the node limit")
            child_path = Path(child.path)
            child_stat = child.stat(follow_symlinks=False)
            if child.is_symlink() or _stat_is_reparse(child_stat):
                raise BackupIntegrityError(
                    f"staged restore contains a link or reparse point: {child_path}"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                _apply_identity(child_path, reference_stat, mode=0o700)
                stack.append(child_path)
            elif stat.S_ISREG(child_stat.st_mode):
                _apply_identity(child_path, reference_stat, mode=0o600)
            else:
                raise BackupIntegrityError(
                    f"staged restore contains a special file: {child_path}"
                )


def _apply_identity(path: Path, reference: os.stat_result, *, mode: int) -> None:
    if sys.platform == "win32":
        return
    path.chmod(mode)
    current = path.stat(follow_symlinks=False)
    desired = (reference.st_uid, reference.st_gid)
    if (current.st_uid, current.st_gid) != desired:
        os.chown(path, *desired, follow_symlinks=False)


def _copy_secret(src: Path, dest: Path) -> dict:
    shutil.copy2(src, dest)
    _chmod_600(dest)
    return _file_entry(dest)


def _restore_secret(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _chmod_600(dest)


def _copy_user_files(base: Path, backup_dir: Path) -> list[dict]:
    """Copy the legacy uploads tree and return its manifest entries."""

    source = base / "uploads"
    if not _path_exists(source):
        return []
    return _copy_tree_snapshot(
        source,
        backup_dir / USER_FILES_DIRNAME / "uploads",
        manifest_prefix=f"{USER_FILES_DIRNAME}/uploads",
    )


def _restore_user_files(backup_dir: Path, base: Path, *, include_library: bool) -> None:
    """Restore opt-in trees using sibling staging rather than delete-then-copy."""

    src_root = backup_dir / USER_FILES_DIRNAME
    names = ("uploads", "library") if include_library else ("uploads",)
    for name in names:
        source = src_root / name
        if source.is_dir():
            _restore_tree_atomic(source, base / name)


def _require_directory(path: Path, *, label: str) -> None:
    if not _path_exists(path):
        raise BackupIntegrityError(f"{label} missing: {path}")
    _reject_link_or_reparse(path)
    path_stat = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(path_stat.st_mode):
        raise BackupIntegrityError(f"{label} is not a directory: {path}")


def _require_regular_file(path: Path, *, label: str) -> None:
    if not _path_exists(path):
        raise BackupIntegrityError(f"{label} missing: {path}")
    _reject_link_or_reparse(path)
    path_stat = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(path_stat.st_mode):
        raise BackupIntegrityError(f"{label} is not a regular file: {path}")


def _reject_link_or_reparse(path: Path) -> None:
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or _stat_is_reparse(path_stat):
        raise BackupIntegrityError(f"link or reparse point is not allowed: {path}")


def _stat_is_reparse(path_stat: os.stat_result) -> bool:
    return bool(getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _remove_path(path: Path) -> None:
    path_stat = path.lstat()
    if _stat_is_reparse(path_stat):
        if stat.S_ISDIR(path_stat.st_mode):
            os.rmdir(path)
        else:
            path.unlink()
        return
    if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _chmod_600(path: Path) -> None:
    if sys.platform != "win32":
        path.chmod(0o600)


def _chmod_700(path: Path) -> None:
    if sys.platform != "win32":
        path.chmod(0o700)


def _file_entry(path: Path) -> dict:
    return {
        "name": path.name,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_backup_dir(backup: Path) -> Path:
    backup = _absolute_path(Path(backup))
    if _path_exists(backup):
        _reject_link_or_reparse(backup)
        backup_stat = backup.stat(follow_symlinks=False)
        if stat.S_ISREG(backup_stat.st_mode) and backup.name == MANIFEST_NAME:
            _require_directory(backup.parent, label="backup directory")
            return backup.parent
        if stat.S_ISDIR(backup_stat.st_mode):
            return backup
    raise FileNotFoundError(f"backup not found: {backup}")


def _redact(url: str) -> str:
    # Codex #47: single source of truth for URL credential masking.
    from backend.utils.redact import redact_url

    return redact_url(url)
