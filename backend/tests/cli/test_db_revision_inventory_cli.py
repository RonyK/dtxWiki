from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.cli.commands import db as db_commands
from backend.cli.main import app
from backend.services.ops import db_revision_inventory as inventory
from backend.services.ops import read_only_database


def _db(path: Path, revisions=("bd62193d5cf6",)) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO alembic_version(version_num) VALUES (?)",
        [(revision,) for revision in revisions],
    )
    connection.commit()
    connection.close()


def _fingerprint(path: Path):
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,
        sorted(item.name for item in path.parent.iterdir()),
    )


def test_requires_explicit_target_and_rejects_duplicates(tmp_path: Path):
    runner = CliRunner()
    assert runner.invoke(app, ["db", "revision-inventory", "--json"]).exit_code == 2
    db = tmp_path / "x.sqlite"
    _db(db)
    result = runner.invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--sqlite-file",
            f"same={db}",
            "--sqlite-file",
            f"SAME={db}",
        ],
    )
    assert result.exit_code == 2
    raw_url = runner.invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--sqlite-file",
            "bad=postgresql://user:secret@db.invalid/dtx",
        ],
    )
    assert raw_url.exit_code == 2
    assert "secret" not in raw_url.output


def test_configured_target_does_not_create_default_file(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "new-data-dir"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(data_dir))
    monkeypatch.delenv("DTXWIKI_DATABASE_URL", raising=False)
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--configured", "configured", "--json"],
    )
    assert result.exit_code == 3
    assert not data_dir.exists()


def test_sqlite_probe_does_not_create_sidecars_or_change_bytes_or_mtime(tmp_path: Path):
    db = tmp_path / "space ü.sqlite"
    _db(db)
    before = _fingerprint(db)
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"local={db}", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert _fingerprint(db) == before
    payload = json.loads(result.stdout)
    assert payload["code"] == {
        "alembic_heads": ["ef53a0a7061c"],
        "known_alternate_revisions": [
            "0005_library_multiformat_conversion",
            "0012_graph_view_observations",
        ],
    }
    assert payload["targets"][0]["read_only"]["verified"] is True
    assert payload["targets"][0]["alembic"]["revisions"] == ["bd62193d5cf6"]


@pytest.mark.skipif(os.name == "nt", reason="question mark is not a Windows path char")
def test_sqlite_path_with_url_delimiter_is_not_reparsed(tmp_path: Path):
    database = tmp_path / "literal?query.sqlite"
    _db(database)

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"literal={database}", "--json"],
    )

    assert result.exit_code == 0, result.output
    target = json.loads(result.stdout)["targets"][0]
    assert target["locator"]["path"] == str(database.absolute())
    assert target["alembic"]["revisions"] == ["bd62193d5cf6"]


def test_missing_sqlite_is_not_created(tmp_path: Path):
    db = tmp_path / "missing.sqlite"
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"local={db}", "--json"],
    )
    assert result.exit_code == 3
    assert not db.exists()
    assert json.loads(result.stdout)["targets"][0]["errors"] == ["sqlite_file_missing"]


def test_existing_unversioned_sqlite_is_inconclusive_and_unchanged(tmp_path: Path):
    db = tmp_path / "unversioned.sqlite"
    connection = sqlite3.connect(db)
    connection.close()
    before = _fingerprint(db)

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"empty={db}", "--json"],
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["targets"][0]["consistency"] == "unversioned"
    assert _fingerprint(db) == before


def test_exclusive_locked_sqlite_fails_bounded_and_safely(tmp_path: Path, monkeypatch):
    from backend.services.ops import read_only_database

    # The lock below is real and held throughout; only the busy-wait bound the
    # probe would otherwise sit out (5s) is shortened.
    probe_sqlite_path = read_only_database._probe_sqlite_path

    def short_wait(path, **kwargs):
        return probe_sqlite_path(path, **{**kwargs, "timeout_seconds": 0.2})

    monkeypatch.setattr(read_only_database, "_probe_sqlite_path", short_wait)
    db = tmp_path / "locked.sqlite"
    _db(db)
    locker = sqlite3.connect(db)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        result = CliRunner().invoke(
            app,
            [
                "db",
                "revision-inventory",
                "--sqlite-file",
                f"locked={db}",
                "--json",
            ],
        )
    finally:
        locker.rollback()
        locker.close()
    assert result.exit_code == 3
    assert json.loads(result.stdout)["targets"][0]["errors"] == [
        "database_probe_failed"
    ]


def test_multiple_rows_and_partial_markers_are_unsafe(tmp_path: Path):
    multiple = tmp_path / "multiple.sqlite"
    _db(
        multiple, ("0005_audit_workspace_delete", "0005_library_multiformat_conversion")
    )
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"multiple={multiple}", "--json"],
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["targets"][0]["consistency"] == "multiple_heads"

    partial = tmp_path / "partial.sqlite"
    _db(partial)
    connection = sqlite3.connect(partial)
    connection.execute("CREATE TABLE library_run (id TEXT)")
    connection.commit()
    connection.close()
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"partial={partial}", "--json"],
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["targets"][0]["consistency"] == "partial_schema"


def test_old_schema_stamped_current_0002_is_a_nonmutating_conflict(
    tmp_path: Path,
):
    database = tmp_path / "old-0002.sqlite"
    _db(database, ("efcbcf724889",))
    before = _fingerprint(database)

    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--sqlite-file",
            f"old={database}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    target = json.loads(result.stdout)["targets"][0]
    assert target["consistency"] == "stamp_marker_conflict"
    assert target["markers"]["final_epic1_0002"]["state"] == "absent"
    assert _fingerprint(database) == before


def test_query_only_rejects_injected_dml_and_preserves_file(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "reject.sqlite"
    _db(db)
    before = _fingerprint(db)
    original = read_only_database._inspect

    def attempted_write(connection, **kwargs):
        connection.exec_driver_sql("CREATE TABLE forbidden_write (id INTEGER)")
        return original(connection, **kwargs)

    monkeypatch.setattr(read_only_database, "_inspect", attempted_write)
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"local={db}", "--json"],
    )
    assert result.exit_code == 3
    assert _fingerprint(db) == before
    connection = sqlite3.connect(db)
    assert (
        connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='forbidden_write'"
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_wal_visible_stamp_without_sidecar_change(tmp_path: Path):
    db = tmp_path / "wal.sqlite"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)")
    writer.execute("INSERT INTO alembic_version VALUES ('bd62193d5cf6')")
    writer.commit()
    sidecars = [Path(f"{db}-wal"), Path(f"{db}-shm")]
    before_names = sorted(path.name for path in tmp_path.iterdir())
    durable_before = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in (db, sidecars[0])
    }
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"wal={db}", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["targets"][0]["alembic"]["revisions"] == [
        "bd62193d5cf6"
    ]
    # WAL readers necessarily coordinate through the existing -shm mapping;
    # its transient lock bytes or mtime may change. Durable DB/WAL content and
    # the set of sidecar files must not change.
    assert sorted(path.name for path in tmp_path.iterdir()) == before_names
    assert {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in (db, sidecars[0])
    } == durable_before
    writer.close()


def test_wal_probe_pins_one_read_snapshot(tmp_path: Path, monkeypatch):
    db = tmp_path / "wal-snapshot.sqlite"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)")
    writer.execute("INSERT INTO alembic_version VALUES ('bd62193d5cf6')")
    writer.execute("CREATE TABLE snapshot_probe (value INTEGER NOT NULL)")
    writer.execute("INSERT INTO snapshot_probe VALUES (1)")
    writer.commit()
    observed: list[int] = []
    original = read_only_database._inspect

    def inspect_across_commit(connection, **kwargs):
        observed.append(
            connection.exec_driver_sql("SELECT value FROM snapshot_probe").scalar_one()
        )
        writer.execute("UPDATE snapshot_probe SET value = 2")
        writer.commit()
        observed.append(
            connection.exec_driver_sql("SELECT value FROM snapshot_probe").scalar_one()
        )
        return original(connection, **kwargs)

    monkeypatch.setattr(read_only_database, "_inspect", inspect_across_commit)
    try:
        result = CliRunner().invoke(
            app,
            ["db", "revision-inventory", "--sqlite-file", f"wal={db}", "--json"],
        )
    finally:
        writer.close()

    assert result.exit_code == 0, result.output
    assert observed == [1, 1]


def test_closed_wal_database_does_not_create_missing_sidecars(tmp_path: Path):
    database = tmp_path / "closed-wal.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
    )
    connection.execute("INSERT INTO alembic_version VALUES ('bd62193d5cf6')")
    connection.commit()
    connection.close()
    before = sorted(path.name for path in tmp_path.iterdir())
    assert before == [database.name]

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"closed={database}", "--json"],
    )
    assert result.exit_code == 3
    assert json.loads(result.stdout)["targets"][0]["errors"] == [
        "sqlite_wal_sidecars_required"
    ]
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_sqlite_symlink_is_refused_before_resolution(tmp_path: Path):
    database = tmp_path / "target.sqlite"
    link = tmp_path / "link.sqlite"
    _db(database)
    try:
        link.symlink_to(database)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"linked={link}", "--json"],
    )
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["targets"][0]["errors"] == ["sqlite_symlink_refused"]
    assert str(database) not in result.output


def test_removed_context_0008_stamp_is_unknown_even_when_markers_match(
    tmp_path: Path,
):
    database = tmp_path / "context-0008.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL);
        INSERT INTO alembic_version VALUES ('0008_mcp_access_lifecycle');
        CREATE TABLE workspace (id TEXT PRIMARY KEY);
        CREATE TABLE audit_event (
            workspace_id TEXT REFERENCES workspace(id) ON DELETE SET NULL
        );
        CREATE TABLE review_item (decision_summary_code TEXT);
        CREATE TABLE api_tokens (
            catalog_app_id TEXT,
            catalog_app_label_snapshot TEXT,
            catalog_revision TEXT
        );
        CREATE TABLE api_token_workspaces (token_id TEXT, workspace_id TEXT);
        CREATE TABLE oauth_clients (
            catalog_identity_fingerprint TEXT,
            catalog_app_id TEXT,
            catalog_app_label_snapshot TEXT,
            catalog_revision TEXT,
            catalog_association_provenance TEXT
        );
        CREATE TABLE oauth_consents (
            catalog_app_id TEXT,
            catalog_app_label_snapshot TEXT,
            catalog_revision TEXT
        );
        CREATE TABLE oauth_refresh_tokens (rotated_from_id TEXT);
        CREATE TABLE mcp_client (runtime_binding_key TEXT, deleted_at TEXT);
        CREATE TABLE workspace_credential_grant (id TEXT);
        """
    )
    connection.commit()
    connection.close()

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"context={database}", "--json"],
    )
    assert result.exit_code == 4, result.output
    target = json.loads(result.stdout)["targets"][0]
    assert target["markers"]["context_0008"]["state"] == "complete"
    assert target["consistency"] == "unknown_revision"


def test_invalid_database_revision_is_sanitized(tmp_path: Path):
    database = tmp_path / "hostile-revision.sqlite"
    _db(database, ("bad\nrevision\x1b[31m",))
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"hostile={database}"],
    )
    assert result.exit_code == 4
    assert "invalid_alembic_revision" in result.output
    assert "bad\nrevision" not in result.output


def test_backup_manifest_is_bound_to_its_integrity_checked_database(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["integrity_verified"] is True
    assert evidence["database_checkpoint_included"] is True
    assert evidence["database_stamp_verified"] is True
    assert evidence["database_revisions"] == ["bd62193d5cf6"]


def test_manifest_checkpoint_hashes_once_before_inspection(tmp_path: Path, monkeypatch):
    backup = tmp_path / "ordered-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    events: list[str] = []
    original_digest = read_only_database._verify_checkpoint_digest
    original_inspect = read_only_database._inspect

    def recording_digest(*args, **kwargs):
        events.append("digest")
        return original_digest(*args, **kwargs)

    def recording_inspect(*args, **kwargs):
        events.append("inspect")
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(
        read_only_database,
        "_verify_checkpoint_digest",
        recording_digest,
    )
    monkeypatch.setattr(read_only_database, "_inspect", recording_inspect)

    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == ["digest", "inspect"]


def test_manifest_checkpoint_blocks_normal_delete_writer_during_inspection(
    tmp_path: Path, monkeypatch
):
    backup = tmp_path / "concurrent-writer-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = _fingerprint(database)
    original_inspect = read_only_database._inspect
    writer_blocked = False

    def inspect_while_writer_attempts_commit(connection, **kwargs):
        nonlocal writer_blocked
        writer = sqlite3.connect(database, timeout=0.05)
        try:
            writer.execute(
                "UPDATE alembic_version "
                "SET version_num='0004_workspace_member_foundation'"
            )
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.commit()
            writer_blocked = True
            writer.rollback()
        finally:
            writer.close()
        return original_inspect(connection, **kwargs)

    monkeypatch.setattr(
        read_only_database,
        "_inspect",
        inspect_while_writer_attempts_commit,
    )
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert writer_blocked is True
    assert _fingerprint(database) == before
    assert not Path(f"{database}-journal").exists()
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["database_revisions"] == ["bd62193d5cf6"]
    assert evidence["database_stamp_verified"] is True


def test_manifest_checkpoint_digest_failure_is_unsafe_and_sanitized(
    tmp_path: Path, monkeypatch
):
    backup = tmp_path / "unreadable-digest-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def unreadable_digest(*_args, **_kwargs):
        raise inventory.ProbeFailure(
            "sqlite_checkpoint_digest_unverifiable",
            category="checkpoint_binding_unverifiable",
        )

    monkeypatch.setattr(
        read_only_database,
        "_verify_checkpoint_digest",
        unreadable_digest,
    )
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["integrity_verified"] is True
    assert evidence["database_stamp_verified"] is False
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_checkpoint_digest_unverifiable"]
    assert "sha256" not in result.output.lower()


def test_manifest_unsafe_failure_survives_connection_cleanup_failure(
    tmp_path: Path, monkeypatch
):
    backup = tmp_path / "cleanup-failure-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

    class CleanupFailingConnection:
        def exec_driver_sql(self, statement):
            if statement == "PRAGMA query_only":
                return ScalarResult(1)
            if statement == "PRAGMA journal_mode":
                return ScalarResult("delete")
            return ScalarResult(0)

        def close(self):
            raise RuntimeError("secret cleanup detail")

    class Engine:
        def connect(self):
            return CleanupFailingConnection()

        def dispose(self):
            return None

    def mismatching_digest(*_args, **_kwargs):
        raise inventory.ProbeFailure(
            "sqlite_checkpoint_digest_mismatch",
            category="checkpoint_binding_mismatch",
        )

    monkeypatch.setattr(
        read_only_database.sa,
        "create_engine",
        lambda *_a, **_k: Engine(),
    )
    monkeypatch.setattr(
        read_only_database,
        "_verify_checkpoint_digest",
        mismatching_digest,
    )
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_checkpoint_digest_mismatch"]
    assert "secret cleanup detail" not in result.output


def test_live_sqlite_probe_does_not_require_checkpoint_hash(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "live.sqlite"
    _db(database)

    def unexpected_digest(*_args, **_kwargs):
        raise AssertionError("live probes must not hash a checkpoint binding")

    monkeypatch.setattr(
        read_only_database,
        "_verify_checkpoint_digest",
        unexpected_digest,
    )
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"live={database}", "--json"],
    )

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_schema_v1_manifest_checkpoint_rejects_any_adjacent_sidecar(
    tmp_path: Path, suffix: str
):
    backup = tmp_path / f"backup{suffix}"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    assert database.read_bytes()[18:20] == b"\x01\x01"
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"unbound checkpoint state")
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_files = (database, sidecar, manifest)
    before = {
        path: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in evidence_files
    }

    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["integrity_verified"] is True
    assert evidence["database_stamp_verified"] is False
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_wal_snapshot_unverifiable"]
    assert {
        path: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in evidence_files
    } == before


def test_manifest_checkpoint_rechecks_sidecars_inside_read_transaction(
    tmp_path: Path, monkeypatch
):
    backup = tmp_path / "racing-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    injected_journal = Path(f"{database}-journal")
    original = read_only_database._inspect

    def inspect_then_add_sidecar(connection, **kwargs):
        snapshot = original(connection, **kwargs)
        injected_journal.write_bytes(b"appeared during checkpoint probe")
        return snapshot

    monkeypatch.setattr(read_only_database, "_inspect", inspect_then_add_sidecar)
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    assert injected_journal.is_file()
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["integrity_verified"] is True
    assert evidence["database_stamp_verified"] is False
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_wal_snapshot_unverifiable"]


def test_manifest_checkpoint_rechecks_sidecars_when_probe_fails(
    tmp_path: Path, monkeypatch
):
    backup = tmp_path / "failing-racing-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    injected_journal = Path(f"{database}-journal")
    original = read_only_database._inspect

    def inspect_then_fail(connection, **kwargs):
        original(connection, **kwargs)
        injected_journal.write_bytes(b"appeared before a driver failure")
        raise RuntimeError("unsanitized driver detail")

    monkeypatch.setattr(read_only_database, "_inspect", inspect_then_fail)
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 4, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["integrity_verified"] is True
    assert evidence["database_stamp_verified"] is False
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_wal_snapshot_unverifiable"]
    assert "unsanitized driver detail" not in result.output


@pytest.mark.skipif(os.name == "nt", reason="question mark is not a Windows path char")
def test_manifest_directory_with_url_delimiter_probes_verified_file(tmp_path: Path):
    backup = tmp_path / "backup?literal"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database)
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--backup-manifest",
            f"backup={manifest}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["database_stamp_verified"] is True
    assert evidence["database_revisions"] == ["bd62193d5cf6"]


def test_manifest_database_invalid_revision_is_unsafe_and_sanitized(tmp_path: Path):
    backup = tmp_path / "hostile-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    _db(database, ("bad\nrevision\x1b[31m",))
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": None,
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--backup-manifest", f"bad={manifest}", "--json"],
    )

    assert result.exit_code == 4
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["consistency"] == "unknown_revision"
    assert evidence["errors"] == ["manifest_database_invalid_alembic_revision"]
    assert "bad\nrevision" not in result.output


def test_hash_valid_wal_main_only_manifest_is_inconclusive(tmp_path: Path):
    backup = tmp_path / "wal-backup"
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
    )
    connection.execute("INSERT INTO alembic_version VALUES ('bd62193d5cf6')")
    connection.commit()
    connection.close()
    manifest = backup / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "bd62193d5cf6",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": database.stat().st_size,
                        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = sorted(path.name for path in backup.iterdir())

    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--backup-manifest", f"wal={manifest}", "--json"],
    )
    assert result.exit_code == 4, result.output
    evidence = json.loads(result.stdout)["manifest_evidence"][0]
    assert evidence["database_stamp_verified"] is False
    assert evidence["consistency"] == "manifest_snapshot_incomplete"
    assert evidence["errors"] == ["manifest_database_wal_snapshot_unverifiable"]
    assert sorted(path.name for path in backup.iterdir()) == before


@pytest.mark.parametrize("list_sidecars", [False, True])
def test_schema_v1_manifest_cannot_launder_a_wal_only_revision(
    tmp_path: Path, list_sidecars: bool
):
    backup_name = "listed-wal-backup" if list_sidecars else "unlisted-wal-backup"
    backup = tmp_path / backup_name
    backup.mkdir()
    database = backup / "dtxwiki.sqlite"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
        )
        writer.execute("INSERT INTO alembic_version VALUES ('bd62193d5cf6')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        main_sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
        writer.execute("UPDATE alembic_version SET version_num='efcbcf724889'")
        writer.commit()
        assert hashlib.sha256(database.read_bytes()).hexdigest() == main_sha256
        assert database.read_bytes()[18:20] == b"\x02\x02"

        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        assert wal.is_file()
        assert shm.is_file()
        files = [
            {
                "name": "dtxwiki.sqlite",
                "bytes": database.stat().st_size,
                "sha256": main_sha256,
            }
        ]
        if list_sidecars:
            files.extend(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in (wal, shm)
            )
        manifest = backup / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "alembic_revision": "efcbcf724889",
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        evidence_files = (database, wal, shm, manifest)
        before = {
            path: (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in evidence_files
        }
        before_names = sorted(path.name for path in backup.iterdir())

        result = CliRunner().invoke(
            app,
            [
                "db",
                "revision-inventory",
                "--backup-manifest",
                f"wal={manifest}",
                "--json",
            ],
        )

        assert result.exit_code == 4, result.output
        evidence = json.loads(result.stdout)["manifest_evidence"][0]
        assert evidence["status"] == "error"
        assert evidence["integrity_verified"] is True
        assert evidence["database_checkpoint_included"] is True
        assert evidence["database_stamp_verified"] is False
        assert evidence["database_revisions"] == []
        assert evidence["consistency"] == "manifest_snapshot_incomplete"
        assert evidence["errors"] == ["manifest_database_wal_snapshot_unverifiable"]
        assert evidence["historical_application"] == "unknown"
        assert sorted(path.name for path in backup.iterdir()) == before_names
        assert {
            path: (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in evidence_files
        } == before
    finally:
        writer.close()


def test_secret_url_never_appears_and_json_stdout_is_clean(monkeypatch):
    secret = "postgresql+unknown://user:hunter2@db.invalid/dtx?sslpassword=secret2"
    monkeypatch.setenv("INVENTORY_SECRET_DB", secret)
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--target-env",
            "prod=INVENTORY_SECRET_DB",
            "--json",
        ],
    )
    assert result.exit_code == 3
    json.loads(result.stdout)
    combined = result.stdout + (result.stderr or "")
    assert "hunter2" not in combined
    assert "secret2" not in combined
    assert secret not in combined


def test_internal_failure_is_exit_1_and_sanitized(tmp_path: Path, monkeypatch):
    db = tmp_path / "safe.sqlite"
    _db(db)

    def fail(*_args, **_kwargs):
        raise RuntimeError("hunter2 should never be rendered")

    monkeypatch.setattr(db_commands, "collect_inventory", fail)
    result = CliRunner().invoke(
        app,
        ["db", "revision-inventory", "--sqlite-file", f"safe={db}", "--json"],
    )
    assert result.exit_code == 1
    assert "internal_error" in result.stderr
    assert "hunter2" not in result.output


def test_unsafe_exit_4_outweighs_incomplete(tmp_path: Path):
    multiple = tmp_path / "multiple.sqlite"
    _db(multiple, ("one", "two"))
    result = CliRunner().invoke(
        app,
        [
            "db",
            "revision-inventory",
            "--sqlite-file",
            f"unsafe={multiple}",
            "--target-env",
            "missing=MISSING_INVENTORY_ENV",
            "--json",
        ],
    )
    assert result.exit_code == 4
