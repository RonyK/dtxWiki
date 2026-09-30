from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.services.ops import db_revision_inventory as inventory
from backend.services.ops import read_only_database
from backend.services.ops.filesystem_backup_evidence import (
    FilesystemBackupEvidenceSource,
)


class FixedMigrationGraph:
    def __init__(self, heads=("0002_epic1",)):
        self.heads = heads

    def current_heads(self):
        return self.heads

    def known_revisions(self):
        return frozenset({"0001_initial_release", "0002_epic1"})


def _collect(request, *, probe, now=None, heads=None):
    selected_heads = ("0002_epic1",) if heads is None else heads
    return inventory.collect_inventory(
        request,
        probe=probe,
        migration_graph=FixedMigrationGraph(selected_heads),
        backup_evidence=FilesystemBackupEvidenceSource(),
        command_version="test",
        now=now,
    )


def _marker(state: str = "absent") -> inventory.MarkerSnapshot:
    if state == "complete":
        return inventory.MarkerSnapshot(state="complete", present=("x",), missing=())
    if state == "partial":
        return inventory.MarkerSnapshot(state="partial", present=("x",), missing=("y",))
    return inventory.MarkerSnapshot(state="absent", present=(), missing=("x",))


def _snapshot(
    *,
    revisions=("0001_initial_release",),
    library="absent",
    context_0005="absent",
    context_0006="absent",
    context_0008="absent",
    media_0009="absent",
    public_exposure_0010="absent",
    final_epic1_0002="absent",
):
    return inventory.DatabaseSnapshot(
        dialect="sqlite",
        locator={"dialect": "sqlite", "path": "safe.sqlite"},
        read_only_mechanism="test",
        read_only_verified=True,
        alembic_table_present=True,
        alembic_revisions=revisions,
        library_0005=_marker(library),
        context_0005=_marker(context_0005),
        context_0006=_marker(context_0006),
        context_0008=_marker(context_0008),
        media_0009=_marker(media_0009),
        public_exposure_0010=_marker(public_exposure_0010),
        final_epic1_0002=_marker(final_epic1_0002),
    )


def _final_snapshot(**overrides):
    values = {
        "revisions": ("0002_epic1",),
        "library": "complete",
        "context_0005": "complete",
        "context_0006": "complete",
        "context_0008": "complete",
        "media_0009": "complete",
        "public_exposure_0010": "complete",
        "final_epic1_0002": "complete",
    }
    values.update(overrides)
    return _snapshot(**values)


class FakeProbe:
    def __init__(
        self,
        snapshot,
        *,
        expected_target="sqlite:///not-opened.sqlite",
        expected_checkpoint: bool = False,
    ):
        self.snapshot = snapshot
        self.expected_target = expected_target
        self.expected_checkpoint = expected_checkpoint

    def probe(self, request):
        if self.expected_checkpoint:
            assert isinstance(request, inventory.CheckpointProbeRequest)
            database_target = request.checkpoint.path
            contents = database_target.read_bytes()
            assert request.checkpoint.expected_main.byte_size == len(contents)
            assert request.checkpoint.expected_main.hexdigest == hashlib.sha256(
                contents
            ).hexdigest()
        else:
            assert isinstance(request, inventory.LiveProbeRequest)
            database_target = request.target
        if self.expected_target is not None:
            assert database_target == self.expected_target
        return self.snapshot


def _request():
    return inventory.InventoryRequest(
        live_targets=(
            inventory.LiveTargetRequest(
                name="one",
                source_kind="env",
                source_reference="DB_ONE",
                database_target="sqlite:///not-opened.sqlite",
            ),
        )
    )


def test_clean_inventory_is_deterministic_and_never_approves_start():
    now = datetime(2026, 8, 5, tzinfo=UTC)
    result = _collect(
        _request(), probe=FakeProbe(_snapshot()), now=now
    )
    payload = result.to_dict()
    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["generated_at"] == "2026-08-05T00:00:00Z"
    assert payload["summary"]["implementation_start_evidence_complete"] is False
    assert payload["code"] == {
        "alembic_heads": ["0002_epic1"],
        "known_alternate_revisions": [
            "0005_library_multiformat_conversion",
            "0012_graph_view_observations",
        ],
    }
    assert "not-opened" not in json.dumps(payload)


def test_partial_markers_and_multiple_rows_are_exit_4():
    partial = _collect(
        _request(), probe=FakeProbe(_snapshot(library="partial"))
    )
    assert partial.exit_code == 4
    assert partial.targets[0].consistency == "partial_schema"

    multiple = _collect(
        _request(),
        probe=FakeProbe(
            _snapshot(
                revisions=(
                    "0005_audit_workspace_delete",
                    "0005_library_multiformat_conversion",
                )
            )
        ),
    )
    assert multiple.exit_code == 4
    assert multiple.targets[0].consistency == "multiple_heads"


def test_final_0002_requires_every_historical_and_final_marker():
    coherent = _collect(_request(), probe=FakeProbe(_final_snapshot()))

    assert coherent.exit_code == 0
    assert coherent.targets[0].consistency == "consistent"
    assert coherent.targets[0].markers is not None
    assert coherent.targets[0].markers["final_epic1_0002"]["state"] == "complete"


def test_old_or_incomplete_database_stamped_0002_is_a_marker_conflict():
    old_shape = _collect(
        _request(),
        probe=FakeProbe(_snapshot(revisions=("0002_epic1",))),
    )
    assert old_shape.exit_code == 4
    assert old_shape.targets[0].consistency == "stamp_marker_conflict"

    incomplete_final_shape = _collect(
        _request(),
        probe=FakeProbe(_final_snapshot(final_epic1_0002="partial")),
    )
    assert incomplete_final_shape.exit_code == 4
    assert (
        incomplete_final_shape.targets[0].consistency
        == "stamp_marker_conflict"
    )


def test_0001_with_any_epic1_schema_evidence_is_not_safe_to_upgrade():
    result = _collect(
        _request(),
        probe=FakeProbe(
            _snapshot(
                revisions=("0001_initial_release",),
                library="complete",
            )
        ),
    )

    assert result.exit_code == 4
    assert result.targets[0].consistency == "stamp_marker_conflict"


@pytest.mark.parametrize(
    "revision",
    (
        "0003_oauth_consent_lifetimes",
        "0004_workspace_member_foundation",
        "0005_library_multiformat_conversion",
        "0005_audit_workspace_delete",
        "0006_review_summary_code",
        "0007_context_orphan_demotion",
        "0008_mcp_access_lifecycle",
        "0009_media_blob_foundation",
        "0010_context_public_exposure",
        "0011_library_preview_lifecycle",
        "0012_context_legacy_cutover",
        "0012_graph_view_observations",
        "0013_context_content_hash_fix",
        "0014_job_failure_notification",
        "0015_consolidated",
    ),
)
def test_removed_revision_stamps_remain_unknown_regardless_of_markers(revision):
    result = _collect(
        _request(),
        probe=FakeProbe(
            _final_snapshot(
                revisions=(revision,),
                final_epic1_0002="partial",
            )
        ),
    )

    assert result.exit_code == 4
    assert result.targets[0].consistency == "unknown_revision"


def test_sqlite_probe_detects_complete_final_epic1_fingerprint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "final-epic1.sqlite"
    table_columns: dict[str, set[str]] = {}
    for marker in inventory.FINAL_EPIC1_0002_MARKERS:
        kind, value = marker.split(":", 1)
        if kind == "table":
            table_columns.setdefault(value, set())
        else:
            table, column = value.split(".", 1)
            table_columns.setdefault(table, set()).add(column)

    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
    )
    connection.execute(
        "INSERT INTO alembic_version VALUES ('0002_epic1')"
    )
    for table, columns in table_columns.items():
        definitions = ", ".join(
            f'"{column}" TEXT' for column in sorted(columns)
        )
        definitions = definitions or '"_marker" TEXT'
        connection.execute(f'CREATE TABLE "{table}" ({definitions})')
    connection.commit()
    connection.close()

    snapshot = read_only_database.probe_read_only(
        f"sqlite+pysqlite:///{database.as_posix()}"
    )

    assert snapshot.final_epic1_0002.state == "complete"
    assert snapshot.final_epic1_0002.missing == ()


def test_unversioned_database_is_inconclusive_exit_4():
    result = _collect(
        _request(), probe=FakeProbe(_snapshot(revisions=()))
    )
    assert result.exit_code == 4
    assert result.targets[0].consistency == "unversioned"


def test_unknown_revision_is_inconclusive_exit_4():
    result = _collect(
        _request(),
        probe=FakeProbe(_snapshot(revisions=("9999_unknown_branch",))),
    )
    assert result.exit_code == 4
    assert result.targets[0].consistency == "unknown_revision"


@pytest.mark.parametrize(
    "category",
    ("category_typo", ["unhashable_category"]),
    ids=("unknown-string", "unhashable-value"),
)
def test_nonconforming_probe_failure_category_fails_closed(category):
    class NonconformingProbe:
        def probe(self, request):
            assert isinstance(request, inventory.LiveProbeRequest)
            assert request.target == "sqlite:///not-opened.sqlite"
            raise inventory.ProbeFailure(
                "adapter_specific_code",
                category=category,  # type: ignore[arg-type]
            )

    result = _collect(_request(), probe=NonconformingProbe())

    assert result.exit_code == 4
    assert result.summary["targets_inconclusive"] == 1
    assert result.targets[0].status == "error"
    assert result.targets[0].consistency == "unknown_revision"
    assert result.targets[0].errors == ("database_probe_contract_violation",)


def test_multiple_code_heads_are_exit_4():
    result = _collect(_request(), probe=FakeProbe(_snapshot()), heads=("left", "right"))
    assert result.exit_code == 4
    assert result.code["alembic_heads"] == ["left", "right"]


def test_manifest_is_positive_evidence_only(tmp_path: Path):
    revision = "0001_initial_release"
    database = tmp_path / "dtxwiki.sqlite"
    database.write_bytes(b"integrity-bound test checkpoint")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": revision,
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
    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FakeProbe(
            _snapshot(revisions=(revision,)),
            expected_target=None,
            expected_checkpoint=True,
        ),
    )
    evidence = result.manifest_evidence[0]
    assert result.exit_code == 0
    assert evidence.integrity_verified is True
    assert evidence.database_checkpoint_included is True
    assert evidence.database_stamp_verified is True
    assert evidence.historical_application == (
        "observed_in_integrity_verified_database_checkpoint"
    )


def test_manifest_checkpoint_digest_binding_rejects_same_inode_aba(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "dtxwiki.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"
    )
    connection.execute("INSERT INTO alembic_version VALUES ('0001_initial_release')")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    original_stat = database.stat()

    alternate = tmp_path / "alternate.sqlite"
    alternate.write_bytes(original)
    connection = sqlite3.connect(alternate)
    connection.execute(
        "UPDATE alembic_version SET version_num='0004_workspace_member_foundation'"
    )
    connection.commit()
    connection.close()
    alternate_bytes = original[:100] + alternate.read_bytes()[100:]
    assert len(alternate_bytes) == len(original)
    assert alternate_bytes[:100] == original[:100]
    assert hashlib.sha256(alternate_bytes).digest() != hashlib.sha256(original).digest()

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0004_workspace_member_foundation",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": len(original),
                        "sha256": hashlib.sha256(original).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_source = FilesystemBackupEvidenceSource()
    inspect_called = False
    original_inspect = read_only_database._inspect

    def recording_inspect(*args, **kwargs):
        nonlocal inspect_called
        inspect_called = True
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(read_only_database, "_inspect", recording_inspect)

    class AbaEvidenceSource:
        def verify(self, path):
            evidence = evidence_source.verify(path)
            inode = database.stat().st_ino
            database.write_bytes(alternate_bytes)
            os.utime(
                database,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            assert database.stat().st_ino == inode
            return evidence

        def reverify(self, evidence):
            inode = database.stat().st_ino
            database.write_bytes(original)
            os.utime(
                database,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            assert database.stat().st_ino == inode
            evidence_source.reverify(evidence)

    result = inventory.collect_inventory(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=read_only_database.SqlAlchemyRevisionInventoryProbe(),
        migration_graph=FixedMigrationGraph(),
        backup_evidence=AbaEvidenceSource(),
        command_version="test",
    )

    evidence = result.manifest_evidence[0]
    assert result.exit_code == 4
    assert evidence.integrity_verified is True
    assert evidence.database_stamp_verified is False
    assert evidence.consistency == "manifest_snapshot_incomplete"
    assert evidence.errors == ("manifest_database_checkpoint_digest_mismatch",)
    assert inspect_called is False
    assert hashlib.sha256(database.read_bytes()).digest() == hashlib.sha256(
        original
    ).digest()


def test_manifest_rewrite_during_database_probe_is_unsafe(tmp_path: Path):
    revision = "0005_library_multiformat_conversion"
    database = tmp_path / "dtxwiki.sqlite"
    original = b"integrity-bound test checkpoint"
    database.write_bytes(original)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": revision,
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": len(original),
                        "sha256": hashlib.sha256(original).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class RewritingProbe:
        def probe(self, request):
            assert isinstance(request, inventory.CheckpointProbeRequest)
            assert request.checkpoint.path == database.absolute()
            before = database.stat()
            database.write_bytes(b"x" * len(original))
            after = database.stat()
            assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            return _snapshot(revisions=(revision,), library="complete")

    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=RewritingProbe(),
    )
    evidence = result.manifest_evidence[0]
    assert result.exit_code == 4
    assert evidence.status == "error"
    assert evidence.integrity_verified is False
    assert evidence.database_stamp_verified is False
    assert evidence.consistency == "manifest_changed_during_probe"
    assert evidence.errors == ("manifest_changed_during_probe",)


def test_nonconforming_manifest_probe_failure_category_fails_closed(
    tmp_path: Path,
):
    database = tmp_path / "dtxwiki.sqlite"
    contents = b"integrity-bound test checkpoint"
    database.write_bytes(contents)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0001_initial_release",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class NonconformingProbe:
        def probe(self, request):
            assert isinstance(request, inventory.CheckpointProbeRequest)
            assert request.checkpoint.path == database.absolute()
            raise inventory.ProbeFailure(
                "adapter_specific_code",
                category="category_typo",  # type: ignore[arg-type]
            )

    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=NonconformingProbe(),
    )

    evidence = result.manifest_evidence[0]
    assert result.exit_code == 4
    assert result.summary["targets_inconclusive"] == 1
    assert evidence.status == "error"
    assert evidence.integrity_verified is True
    assert evidence.database_stamp_verified is False
    assert evidence.consistency == "unknown_revision"
    assert evidence.errors == ("manifest_database_probe_contract_violation",)


def test_manifest_is_reverified_after_a_probe_failure(tmp_path: Path):
    database = tmp_path / "dtxwiki.sqlite"
    original = b"integrity-bound test checkpoint"
    database.write_bytes(original)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0001_initial_release",
                "files": [
                    {
                        "name": "dtxwiki.sqlite",
                        "bytes": len(original),
                        "sha256": hashlib.sha256(original).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FailingRewritingProbe:
        def probe(self, request):
            assert isinstance(request, inventory.CheckpointProbeRequest)
            assert request.checkpoint.path == database.absolute()
            database.write_bytes(b"x" * len(original))
            raise inventory.ProbeFailure(
                "sqlite_checkpoint_not_standalone",
                category="snapshot_incomplete",
            )

    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FailingRewritingProbe(),
    )

    evidence = result.manifest_evidence[0]
    assert result.exit_code == 4
    assert evidence.status == "error"
    assert evidence.integrity_verified is False
    assert evidence.database_stamp_verified is False
    assert evidence.consistency == "manifest_changed_during_probe"
    assert evidence.errors == ("manifest_changed_during_probe",)


def test_manifest_claim_without_integrity_is_incomplete(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0005_library_multiformat_conversion",
            }
        ),
        encoding="utf-8",
    )
    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FakeProbe(_snapshot(), expected_target=None),
    )
    assert result.exit_code == 3
    assert result.manifest_evidence[0].integrity_verified is False


def test_integrity_verified_manifest_without_database_is_still_incomplete(
    tmp_path: Path,
):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0005_library_multiformat_conversion",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FakeProbe(_snapshot(), expected_target=None),
    )
    assert result.exit_code == 3
    evidence = result.manifest_evidence[0]
    assert evidence.integrity_verified is True
    assert evidence.database_checkpoint_included is False
    assert evidence.errors == ("manifest_database_checkpoint_missing",)


def test_manifest_revision_must_match_checkpoint_stamp(tmp_path: Path):
    database = tmp_path / "dtxwiki.sqlite"
    database.write_bytes(b"integrity-bound test checkpoint")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "alembic_revision": "0005_library_multiformat_conversion",
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
    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FakeProbe(
            _snapshot(),
            expected_target=None,
            expected_checkpoint=True,
        ),
    )
    assert result.exit_code == 4
    evidence = result.manifest_evidence[0]
    assert evidence.consistency == "manifest_revision_conflict"
    assert evidence.errors == ("manifest_revision_mismatch",)


def test_corrupt_manifest_is_incomplete(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("not-json", encoding="utf-8")
    result = _collect(
        inventory.InventoryRequest(
            manifests=(inventory.ManifestRequest(name="backup", path=manifest),)
        ),
        probe=FakeProbe(_snapshot(), expected_target=None),
    )
    assert result.exit_code == 3
    assert result.manifest_evidence[0].errors == ("manifest_invalid",)


def test_postgres_requests_read_only_and_timeouts_in_startup_packet(monkeypatch):
    captured = {}

    class FailingEngine:
        def connect(self):
            raise RuntimeError("do not render this raw failure")

        def dispose(self):
            captured["disposed"] = True

    def create_engine(*_args, **kwargs):
        captured.update(kwargs)
        return FailingEngine()

    monkeypatch.setattr(read_only_database.sa, "create_engine", create_engine)
    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database.probe_read_only(
            "postgresql+pg8000://user:secret@example.invalid/dtx",
            timeout_seconds=2.5,
        )
    assert raised.value.code == "database_probe_failed"
    options = captured["connect_args"]["startup_params"]["options"]
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=2500" in options
    assert "lock_timeout=2500" in options
    assert captured["disposed"] is True


@pytest.mark.parametrize(
    "probe_request",
    (
        "not-a-revision-probe-request",
        inventory.CheckpointProbeRequest(
            checkpoint="not-a-checkpoint-binding"  # type: ignore[arg-type]
        ),
    ),
    ids=("unknown-request", "invalid-checkpoint-payload"),
)
def test_probe_rejects_invalid_request_before_dispatch(probe_request):
    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database.SqlAlchemyRevisionInventoryProbe().probe(  # type: ignore[arg-type]
            probe_request
        )

    assert raised.value.code == "invalid_probe_request"
    assert raised.value.category == "contract_violation"


def test_invalid_checkpoint_binding_is_rejected_before_engine_creation(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "checkpoint.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    def unexpected_engine(*_args, **_kwargs):
        raise AssertionError("invalid binding must not create a database engine")

    monkeypatch.setattr(read_only_database.sa, "create_engine", unexpected_engine)
    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database.SqlAlchemyRevisionInventoryProbe().probe(
            inventory.CheckpointProbeRequest(
                checkpoint=inventory.DatabaseCheckpointBinding(
                    path=database,
                    expected_main=inventory.Sha256FileBinding(
                        byte_size=database.stat().st_size,
                        hexdigest="not-a-sha256",
                    ),
                )
            )
        )

    assert raised.value.code == "invalid_checkpoint_binding"
    assert raised.value.category == "contract_violation"


def test_checkpoint_digest_io_failure_is_sanitized():
    class FailingHandle:
        def seek(self, _offset):
            raise OSError("secret checkpoint path")

    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database._verify_checkpoint_digest(
            FailingHandle(),  # type: ignore[arg-type]
            inventory.Sha256FileBinding(byte_size=100, hexdigest="0" * 64),
        )

    assert raised.value.code == "sqlite_checkpoint_digest_unverifiable"
    assert raised.value.category == "checkpoint_binding_unverifiable"
    assert "secret" not in str(raised.value)


def test_sqlite_connection_cleanup_only_failure_is_sanitized():
    class CleanupFailingConnection:
        def close(self):
            raise RuntimeError("secret cleanup detail")

    with pytest.raises(inventory.ProbeFailure) as raised:
        with read_only_database._sqlite_probe_connection(
            CleanupFailingConnection()  # type: ignore[arg-type]
        ):
            pass

    assert raised.value.code == "database_probe_failed"
    assert raised.value.category == "unavailable"
    assert "secret" not in str(raised.value)


def test_postgres_first_statement_pins_read_only_repeatable_read(monkeypatch):
    statements: list[str] = []

    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

    class Transaction:
        is_active = True

        def rollback(self):
            self.is_active = False

    class Connection:
        def begin(self):
            return Transaction()

        def exec_driver_sql(self, statement):
            statements.append(statement)
            if statement == "SHOW transaction_read_only":
                return ScalarResult("on")
            if statement == "SHOW transaction_isolation":
                return ScalarResult("repeatable read")
            return ScalarResult(None)

        def close(self):
            return None

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(
        read_only_database.sa, "create_engine", lambda *_a, **_k: Engine()
    )
    monkeypatch.setattr(
        read_only_database,
        "_inspect",
        lambda *_args, **_kwargs: _snapshot(),
    )

    snapshot = read_only_database.probe_read_only(
        "postgresql+pg8000://user:secret@example.invalid/dtx"
    )

    assert snapshot.alembic_revisions == ("0001_initial_release",)
    assert statements[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    assert statements[1:3] == [
        "SHOW transaction_read_only",
        "SHOW transaction_isolation",
    ]


def test_postgres_cleanup_failure_remains_sanitized(monkeypatch):
    class CleanupFailingEngine:
        def connect(self):
            raise RuntimeError("secret connection detail")

        def dispose(self):
            raise RuntimeError("secret cleanup detail")

    monkeypatch.setattr(
        read_only_database.sa,
        "create_engine",
        lambda *_args, **_kwargs: CleanupFailingEngine(),
    )

    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database.probe_read_only(
            "postgresql+pg8000://user:secret@example.invalid/dtx"
        )

    assert raised.value.code == "database_probe_failed"
    assert str(raised.value) == "database_probe_failed"


@pytest.mark.parametrize("stage", ["rollback", "close", "dispose"])
def test_postgres_cleanup_only_failures_are_target_local(stage, monkeypatch):
    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

    class Transaction:
        is_active = True

        def rollback(self):
            if stage == "rollback":
                raise RuntimeError("secret rollback detail")
            self.is_active = False

    class Connection:
        def begin(self):
            return Transaction()

        def exec_driver_sql(self, statement):
            if statement == "SHOW transaction_read_only":
                return ScalarResult("on")
            if statement == "SHOW transaction_isolation":
                return ScalarResult("repeatable read")
            return ScalarResult(None)

        def close(self):
            if stage == "close":
                raise RuntimeError("secret close detail")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            if stage == "dispose":
                raise RuntimeError("secret dispose detail")

    monkeypatch.setattr(
        read_only_database.sa, "create_engine", lambda *_args, **_kwargs: Engine()
    )
    monkeypatch.setattr(
        read_only_database,
        "_inspect",
        lambda *_args, **_kwargs: _snapshot(),
    )

    with pytest.raises(inventory.ProbeFailure) as raised:
        read_only_database.probe_read_only(
            "postgresql+pg8000://user:secret@example.invalid/dtx"
        )

    assert raised.value.code == "database_probe_failed"
    assert str(raised.value) == "database_probe_failed"
