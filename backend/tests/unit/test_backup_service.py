"""Round-trip tests for the pre-update backup/restore service."""

import hashlib
import os
import sqlite3
from pathlib import Path

from backend.app.settings import Settings
from backend.services import backup_service


def _make_db(path: Path, rows: int) -> None:
    """Append ``rows`` rows (autoincrement id, no PK collision on repeat calls)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS t "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)"
        )
        conn.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * rows)
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _settings_for(db: Path, jwt: Path, *, library_root: Path | None = None) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{db.as_posix()}",
        oauth_jwt_private_key_path=str(jwt),
        library_blob_root=str(library_root) if library_root else None,
        app_env="development",
    )


def _add_library_blob(db: Path, library_root: Path, content: bytes) -> tuple[Path, str]:
    sha256 = hashlib.sha256(content).hexdigest()
    storage_key = f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"
    blob = library_root / "blobs" / Path(storage_key)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(content)
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS library_blob "
            "(sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, "
            "storage_key TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO library_blob (sha256, byte_size, storage_key) "
            "VALUES (?, ?, ?)",
            (sha256, len(content), storage_key),
        )
        connection.commit()
    finally:
        connection.close()
    return blob, storage_key


def test_db_path_from_url_variants():
    assert backup_service.db_path_from_url(
        "sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"
    ) == Path("/var/lib/dtxwiki/dtxwiki.sqlite")
    assert backup_service.db_path_from_url("sqlite+pysqlite:///rel/x.sqlite") == Path(
        "rel/x.sqlite"
    )
    assert backup_service.db_path_from_url("postgresql+pg8000://h/db") is None
    assert backup_service.db_path_from_url("sqlite+pysqlite:///:memory:") is None


def test_create_backup_captures_db_secrets_and_manifest(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 3)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("KEYDATA", encoding="utf-8")
    (base / "session_secret").write_text("S3CR3T", encoding="utf-8")
    env_file = tmp_path / "dtxwiki.env"
    env_file.write_text("DTXWIKI_APP_ENV=production\n", encoding="utf-8")

    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(
        settings,
        version="0.1.0",
        alembic_revision="abc123",
        install_method="venv",
        data_dir=base,
        env_file=env_file,
    )

    manifest = backup_service.read_manifest(backup_dir)
    assert manifest["dtxwiki_version"] == "0.1.0"
    assert manifest["alembic_revision"] == "abc123"
    assert manifest["install_method"] == "venv"
    assert manifest["includes_db"] is True
    assert manifest["includes_user_files"] is False
    names = {entry["name"] for entry in manifest["files"]}
    assert {"dtxwiki.sqlite", "jwt.pem", "session_secret", "dtxwiki.env"} <= names
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] >= 0
    assert (backup_dir / "dtxwiki.sqlite").exists()


def test_create_backup_normalizes_wal_source_to_standalone_checkpoint(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)")
    writer.execute("INSERT INTO t VALUES (1, 'latest-in-wal')")
    writer.commit()
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("KEYDATA", encoding="utf-8")

    try:
        backup_dir = backup_service.create_backup(
            _settings_for(db, jwt), version="0.1.0", data_dir=base
        )
    finally:
        writer.close()

    checkpoint = backup_dir / backup_service.DB_BACKUP_NAME
    manifest = backup_service.read_manifest(backup_dir)
    assert manifest["database_snapshot_method"] == (
        "sqlite3_backup_delete_journal"
    )
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT v FROM t").fetchone()[0] == "latest-in-wal"
    assert checkpoint.read_bytes()[18:20] == b"\x01\x01"
    assert not Path(f"{checkpoint}-wal").exists()
    assert not Path(f"{checkpoint}-shm").exists()


def test_create_backup_non_sqlite_marks_db_absent(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("KEYDATA", encoding="utf-8")
    settings = Settings(
        database_url="postgresql+pg8000://user:pass@localhost/db",
        oauth_jwt_private_key_path=str(jwt),
        app_env="development",
    )
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    manifest = backup_service.read_manifest(backup_dir)
    assert manifest["includes_db"] is False
    assert manifest["checkpoint_kind"] == "secrets_only"
    assert manifest["checkpoint_complete"] is False
    assert manifest["includes_library_blobs"] is False
    assert not (backup_dir / "dtxwiki.sqlite").exists()
    names = {e["name"] for e in manifest["files"]}
    assert "jwt.pem" in names  # secrets are still captured
    backup_service.verify_backup_integrity(backup_dir)


def test_backup_then_restore_recovers_db_and_secret(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 3)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("ORIGINAL", encoding="utf-8")
    settings = _settings_for(db, jwt)

    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)

    # Mutate live state after the backup.
    _make_db(db, 5)  # now 8 rows
    assert _row_count(db) == 8
    jwt.write_text("CORRUPTED", encoding="utf-8")

    backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert _row_count(db) == 3
    assert jwt.read_text(encoding="utf-8") == "ORIGINAL"


def test_restore_reapplies_jwt_key_ownership(tmp_path, monkeypatch):
    # #2: restore runs as root, so it must re-grant the service-group read perm
    # on the JWT key (root:dtxwiki 0640) or the systemd service crash-loops.
    from backend.cli import prod_setup

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("ORIGINAL", encoding="utf-8")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)

    calls = []
    monkeypatch.setattr(
        prod_setup,
        "set_key_ownership",
        lambda path, group, **k: calls.append((path, group)),
    )
    backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert calls == [(jwt, backup_service.SERVICE_GROUP)]


def test_restore_clears_stale_wal_sidecars(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(settings, data_dir=base)

    # Simulate stale WAL/SHM sidecars left next to the live DB.
    (base / "dtxwiki.sqlite-wal").write_text("stale", encoding="utf-8")
    (base / "dtxwiki.sqlite-shm").write_text("stale", encoding="utf-8")

    backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert not (base / "dtxwiki.sqlite-wal").exists()
    assert not (base / "dtxwiki.sqlite-shm").exists()


def _write_backup(base: Path, version: str, created_utc: str) -> Path:
    # dir names must be filesystem-safe (no ':'); created_utc with ':' lives only
    # inside the manifest (real create_backup uses a colon-free %Y%m%dT%H%M%SZ).
    safe = created_utc.replace(":", "").replace("-", "")
    backup_dir = base / "backups" / f"{version}-{safe}"
    backup_dir.mkdir(parents=True)
    backup_service.write_manifest(
        backup_dir,
        {
            "schema": 1,
            "dtxwiki_version": version,
            "created_utc": created_utc,
            "files": [],
        },
    )
    return backup_dir


def test_list_backups_newest_first(tmp_path):
    base = tmp_path / "data"
    _write_backup(base, "0.1.0", "2026-06-01T00:00:00Z")
    _write_backup(base, "0.2.0", "2026-06-03T00:00:00Z")
    _write_backup(base, "0.1.5", "2026-06-02T00:00:00Z")
    infos = backup_service.list_backups(base)
    versions = [info.manifest["dtxwiki_version"] for info in infos]
    assert versions == ["0.2.0", "0.1.5", "0.1.0"]


def test_prune_keeps_newest_n_plus_per_version(tmp_path):
    base = tmp_path / "data"
    newest = _write_backup(base, "0.3.0", "2026-06-05T00:00:00Z")
    mid = _write_backup(base, "0.3.0", "2026-06-04T00:00:00Z")
    oldest_v3 = _write_backup(base, "0.3.0", "2026-06-03T00:00:00Z")
    older_version = _write_backup(base, "0.2.0", "2026-06-02T00:00:00Z")

    removed = backup_service.prune_backups(base, keep=1)

    # keep=1 keeps the newest (0.3.0@06-05); per-version insurance additionally
    # keeps the latest 0.2.0 backup even though it is beyond keep.
    remaining = {info.path.name for info in backup_service.list_backups(base)}
    assert newest.name in remaining
    assert older_version.name in remaining
    # the two older 0.3.0 backups are beyond keep AND not the per-version-latest.
    removed_names = {p.name for p in removed}
    assert mid.name in removed_names
    assert oldest_v3.name in removed_names


def test_session_secret_captured_from_settings_value(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{db.as_posix()}",
        oauth_jwt_private_key_path=str(jwt),
        session_secret="a-real-production-secret",
        app_env="development",
    )
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    secret_file = backup_dir / "session_secret"
    assert secret_file.exists()
    assert secret_file.read_text(encoding="utf-8") == "a-real-production-secret"
    names = {e["name"] for e in backup_service.read_manifest(backup_dir)["files"]}
    assert "session_secret" in names


def test_dev_session_secret_not_captured(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    settings = _settings_for(db, jwt)  # session_secret unset -> dev fallback
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    assert not (backup_dir / "session_secret").exists()


def test_verify_backup_integrity_detects_tamper(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    backup_dir = backup_service.create_backup(_settings_for(db, jwt), data_dir=base)
    backup_service.verify_backup_integrity(backup_dir)  # clean -> no raise
    (backup_dir / "dtxwiki.sqlite").write_bytes(b"tampered")
    with pytest.raises(backup_service.BackupIntegrityError):
        backup_service.verify_backup_integrity(backup_dir)


def test_restore_refuses_corrupt_backup(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    (backup_dir / "dtxwiki.sqlite").write_bytes(b"x")
    with pytest.raises(backup_service.BackupIntegrityError):
        backup_service.restore_backup(backup_dir, settings, data_dir=base)


def test_manifest_index_url_is_redacted(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    backup_dir = backup_service.create_backup(
        _settings_for(db, jwt),
        data_dir=base,
        index_url="https://user:t0ken@pypi.internal/simple",
    )
    manifest = backup_service.read_manifest(backup_dir)
    assert manifest["index_url"] == "https://user:****@pypi.internal/simple"
    assert "t0ken" not in (backup_dir / "manifest.json").read_text(encoding="utf-8")


def test_manifest_index_url_none_stays_none(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    backup_dir = backup_service.create_backup(_settings_for(db, jwt), data_dir=base)
    assert backup_service.read_manifest(backup_dir)["index_url"] is None


def test_backup_artifacts_are_owner_only(tmp_path):
    # The SQLite copy carries password/token hashes — it must get the same 0600
    # as the sibling secrets, and the backup dirs must be owner-only.
    import sys

    import pytest

    if sys.platform == "win32":
        pytest.skip("POSIX permission bits are a no-op on Windows")
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    backup_dir = backup_service.create_backup(_settings_for(db, jwt), data_dir=base)

    assert ((backup_dir / "dtxwiki.sqlite").stat().st_mode & 0o777) == 0o600
    assert ((backup_dir / "manifest.json").stat().st_mode & 0o777) == 0o600
    assert (backup_dir.stat().st_mode & 0o777) == 0o700
    assert ((base / "backups").stat().st_mode & 0o777) == 0o700


def test_include_files_recorded_in_manifest(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    (base / "uploads").mkdir()
    (base / "uploads" / "a.png").write_bytes(b"img")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(
        settings, data_dir=base, include_files=True
    )
    manifest = backup_service.read_manifest(backup_dir)
    assert manifest["includes_user_files"] is True
    names = {e["name"] for e in manifest["files"]}
    assert "files/uploads/a.png" in names
    backup_service.verify_backup_integrity(backup_dir)  # user-file entries verify


def test_include_files_restored(tmp_path):
    # Codex #44: an include_files backup must be RESTORABLE, not just recorded.
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    (base / "uploads").mkdir()
    (base / "uploads" / "a.png").write_bytes(b"img")
    (base / "library").mkdir()
    (base / "library" / "doc.txt").write_text("hello", encoding="utf-8")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(
        settings, data_dir=base, include_files=True
    )

    # Mutate/delete the live user files after the backup.
    import shutil as _shutil

    _shutil.rmtree(base / "uploads")
    (base / "library" / "doc.txt").write_text("CHANGED", encoding="utf-8")

    backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert (base / "uploads" / "a.png").read_bytes() == b"img"
    assert (base / "library" / "doc.txt").read_text(encoding="utf-8") == "hello"


def test_default_backup_checkpoints_configured_library_and_restores_it(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "external-library"
    blob, storage_key = _add_library_blob(db, library_root, b"source-payload")
    staging = library_root / "staging" / "abandoned.stage"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"ephemeral")
    settings = _settings_for(db, jwt, library_root=library_root)

    first = backup_service.create_backup(settings, data_dir=base)
    second = backup_service.create_backup(settings, data_dir=base)
    first_manifest = backup_service.read_manifest(first)
    second_manifest = backup_service.read_manifest(second)

    assert first_manifest["schema"] == 2
    assert first_manifest["checkpoint_kind"] == "full"
    assert first_manifest["checkpoint_complete"] is True
    assert first_manifest["includes_library_blobs"] is True
    assert first_manifest["includes_user_files"] is False
    entry_name = f"files/library/blobs/{storage_key}"
    assert entry_name in {entry["name"] for entry in first_manifest["files"]}
    assert not (first / "files" / "library" / "staging").exists()
    assert (
        first_manifest["library_checkpoint"]["tree_sha256"]
        == second_manifest["library_checkpoint"]["tree_sha256"]
    )

    blob.write_bytes(b"live-mutated")
    extra = library_root / "blobs" / "orphan"
    extra.write_bytes(b"not-in-checkpoint")
    backup_service.restore_backup(first, settings, data_dir=base)

    assert blob.read_bytes() == b"source-payload"
    assert not extra.exists()
    assert not staging.exists()


def test_create_backup_rejects_db_reference_to_missing_source_blob(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    library_root.mkdir()
    content = b"missing"
    sha256 = hashlib.sha256(content).hexdigest()
    storage_key = f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "CREATE TABLE library_blob "
            "(sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, "
            "storage_key TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO library_blob VALUES (?, ?, ?)",
            (sha256, len(content), storage_key),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backup_service.BackupIntegrityError, match="missing blob"):
        backup_service.create_backup(
            _settings_for(db, jwt, library_root=library_root), data_dir=base
        )

    assert list((base / "backups").iterdir()) == []


def test_create_backup_rejects_context_media_reference_to_missing_blob(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    library_root.mkdir()
    content = b"missing-context-media"
    sha256 = hashlib.sha256(content).hexdigest()
    workspace_id = "00000000-0000-0000-0000-000000000001"
    blob_id = "00000000-0000-0000-0000-000000000002"
    storage_key = f"media/{workspace_id}/{blob_id}/1/{sha256}"
    connection = sqlite3.connect(str(db))
    try:
        connection.executescript(
            "CREATE TABLE stored_blob ("
            "workspace_id TEXT NOT NULL, id TEXT NOT NULL, sha256 TEXT NOT NULL);"
            "CREATE TABLE blob_incarnation ("
            "workspace_id TEXT NOT NULL, blob_id TEXT NOT NULL, "
            "blob_epoch INTEGER NOT NULL, byte_size INTEGER NOT NULL, "
            "storage_key TEXT NOT NULL);"
            "CREATE TABLE context_media_asset ("
            "workspace_id TEXT NOT NULL, blob_id TEXT NOT NULL, "
            "blob_epoch INTEGER NOT NULL);"
        )
        connection.execute(
            "INSERT INTO stored_blob VALUES (?, ?, ?)",
            (workspace_id, blob_id, sha256),
        )
        connection.execute(
            "INSERT INTO blob_incarnation VALUES (?, ?, ?, ?, ?)",
            (workspace_id, blob_id, 1, len(content), storage_key),
        )
        connection.execute(
            "INSERT INTO context_media_asset VALUES (?, ?, ?)",
            (workspace_id, blob_id, 1),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backup_service.BackupIntegrityError, match="missing blob"):
        backup_service.create_backup(
            _settings_for(db, jwt, library_root=library_root), data_dir=base
        )

    assert list((base / "backups").iterdir()) == []


def test_create_backup_rejects_library_artifact_reference_to_missing_blob(tmp_path):
    """A-1: conversion- and preview-stage artifacts share one physical table
    now, so this single fixture (previously a separate
    library_preview_artifact table) covers the invariant for both stages."""
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    library_root.mkdir()
    content = b"missing-library-artifact"
    sha256 = hashlib.sha256(content).hexdigest()
    workspace_id = "00000000-0000-0000-0000-000000000001"
    blob_id = "00000000-0000-0000-0000-000000000002"
    storage_key = f"media/{workspace_id}/{blob_id}/1/{sha256}"
    connection = sqlite3.connect(str(db))
    try:
        connection.executescript(
            "CREATE TABLE stored_blob ("
            "workspace_id TEXT NOT NULL, id TEXT NOT NULL, sha256 TEXT NOT NULL);"
            "CREATE TABLE blob_incarnation ("
            "workspace_id TEXT NOT NULL, blob_id TEXT NOT NULL, "
            "blob_epoch INTEGER NOT NULL, byte_size INTEGER NOT NULL, "
            "storage_key TEXT NOT NULL);"
            "CREATE TABLE library_artifact ("
            "blob_workspace_id TEXT NOT NULL, blob_id TEXT NOT NULL, "
            "blob_epoch INTEGER NOT NULL);"
        )
        connection.execute(
            "INSERT INTO stored_blob VALUES (?, ?, ?)",
            (workspace_id, blob_id, sha256),
        )
        connection.execute(
            "INSERT INTO blob_incarnation VALUES (?, ?, ?, ?, ?)",
            (workspace_id, blob_id, 1, len(content), storage_key),
        )
        connection.execute(
            "INSERT INTO library_artifact VALUES (?, ?, ?)",
            (workspace_id, blob_id, 1),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backup_service.BackupIntegrityError, match="missing blob"):
        backup_service.create_backup(
            _settings_for(db, jwt, library_root=library_root), data_dir=base
        )

    assert list((base / "backups").iterdir()) == []


def test_verify_rejects_manifest_traversal_and_duplicate_paths(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    backup_dir = backup_service.create_backup(_settings_for(db, jwt), data_dir=base)
    manifest = backup_service.read_manifest(backup_dir)
    manifest["files"][0]["name"] = "../outside.sqlite"
    backup_service.write_manifest(backup_dir, manifest)

    with pytest.raises(backup_service.BackupIntegrityError, match="unsafe"):
        backup_service.verify_backup_integrity(backup_dir)

    manifest["files"][0]["name"] = "dtxwiki.sqlite"
    manifest["files"].append(dict(manifest["files"][0]))
    backup_service.write_manifest(backup_dir, manifest)
    with pytest.raises(backup_service.BackupIntegrityError, match="duplicate"):
        backup_service.verify_backup_integrity(backup_dir)


def test_verify_rejects_link_in_library_checkpoint(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    _, storage_key = _add_library_blob(db, library_root, b"payload")
    settings = _settings_for(db, jwt, library_root=library_root)
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    member = backup_dir / "files" / "library" / "blobs" / Path(storage_key)
    external = tmp_path / "external"
    external.write_bytes(b"payload")
    member.unlink()
    try:
        os.symlink(external, member)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(backup_service.BackupIntegrityError, match="link or reparse"):
        backup_service.verify_backup_integrity(backup_dir)


def test_restore_refuses_partial_library_checkpoint_before_cutover(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    blob, _ = _add_library_blob(db, library_root, b"checkpoint")
    settings = _settings_for(db, jwt, library_root=library_root)
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    backed_blob = next((backup_dir / "files" / "library").rglob(blob.name))
    backed_blob.unlink()
    blob.write_bytes(b"live-state")
    _make_db(db, 1)

    with pytest.raises(backup_service.BackupIntegrityError):
        backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert blob.read_bytes() == b"live-state"
    assert _row_count(db) == 2


def test_full_restore_rolls_back_library_when_db_cutover_fails(tmp_path, monkeypatch):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    blob, _ = _add_library_blob(db, library_root, b"checkpoint")
    settings = _settings_for(db, jwt, library_root=library_root)
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    blob.write_bytes(b"live-state")
    _make_db(db, 1)

    real_replace = backup_service.os.replace

    def fail_db_install(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.name.startswith(f".{db.name}.restore-new-")
            and destination_path == db
        ):
            raise OSError("injected DB cutover failure")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_service.os, "replace", fail_db_install)
    with pytest.raises(
        backup_service.BackupIntegrityError, match="prior state recovered"
    ):
        backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert blob.read_bytes() == b"live-state"
    assert _row_count(db) == 2
    assert not list(tmp_path.rglob("*.restore-new-*"))
    assert not list(tmp_path.rglob("*.restore-old-*"))


def test_restore_rejects_manifest_claiming_partial_full_checkpoint(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    settings = _settings_for(db, jwt)
    backup_dir = backup_service.create_backup(settings, data_dir=base)
    manifest = backup_service.read_manifest(backup_dir)
    manifest["checkpoint_complete"] = False
    backup_service.write_manifest(backup_dir, manifest)
    _make_db(db, 1)

    with pytest.raises(backup_service.BackupIntegrityError, match="partial"):
        backup_service.restore_backup(backup_dir, settings, data_dir=base)

    assert _row_count(db) == 2


def test_list_backups_ignores_incomplete_staging_directory(tmp_path):
    base = tmp_path / "data"
    incomplete = base / "backups" / ".0.1.0.incomplete-test"
    incomplete.mkdir(parents=True)
    backup_service.write_manifest(
        incomplete,
        {
            "schema": 1,
            "dtxwiki_version": "0.1.0",
            "created_utc": "2026-08-04T00:00:00Z",
            "files": [],
        },
    )
    assert backup_service.list_backups(base) == []


def test_backup_rejects_library_root_overlapping_backups(tmp_path):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")

    with pytest.raises(backup_service.BackupIntegrityError, match="overlap"):
        backup_service.create_backup(
            _settings_for(db, jwt, library_root=base), data_dir=base
        )


def test_backup_enforces_per_blob_checkpoint_bound(tmp_path, monkeypatch):
    import pytest

    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    _make_db(db, 1)
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("K", encoding="utf-8")
    library_root = tmp_path / "library"
    _add_library_blob(db, library_root, b"too-large")
    monkeypatch.setattr(backup_service, "MAX_CHECKPOINT_FILE_BYTES", 2)

    with pytest.raises(backup_service.BackupIntegrityError, match="size limit"):
        backup_service.create_backup(
            _settings_for(db, jwt, library_root=library_root), data_dir=base
        )

    assert list((base / "backups").iterdir()) == []
