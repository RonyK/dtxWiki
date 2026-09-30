"""Filesystem adapter for integrity-bound backup revision evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.services import backup_service
from backend.services.ops.db_revision_inventory import (
    BackupEvidenceFailure,
    DatabaseCheckpointBinding,
    Sha256FileBinding,
    VerifiedManifestEvidence,
    is_revision_id,
)


class FilesystemBackupEvidenceSource:
    def verify(self, path: Path) -> VerifiedManifestEvidence:
        manifest_dir = (
            path.parent
            if path.name == backup_service.MANIFEST_NAME and not path.is_dir()
            else path
        )
        try:
            initial = backup_service.read_manifest(manifest_dir)
            _manifest_fields(initial, manifest_dir=manifest_dir)
            backup_service.verify_backup_integrity(manifest_dir)
            confirmed = backup_service.read_manifest(manifest_dir)
            if confirmed != initial:
                raise backup_service.BackupIntegrityError(
                    "manifest changed during integrity verification"
                )
            schema, revision, database_checkpoint = _manifest_fields(
                confirmed,
                manifest_dir=manifest_dir,
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            backup_service.BackupIntegrityError,
        ) as exc:
            raise BackupEvidenceFailure("manifest_invalid") from exc

        return VerifiedManifestEvidence(
            schema=schema,
            alembic_revision=revision,
            manifest_dir=manifest_dir,
            database_checkpoint=database_checkpoint,
            verification_token=_manifest_token(confirmed),
        )

    def reverify(self, evidence: VerifiedManifestEvidence) -> None:
        try:
            backup_service.verify_backup_integrity(evidence.manifest_dir)
            manifest = backup_service.read_manifest(evidence.manifest_dir)
            _manifest_fields(manifest, manifest_dir=evidence.manifest_dir)
            if _manifest_token(manifest) != evidence.verification_token:
                raise backup_service.BackupIntegrityError(
                    "manifest changed during database probe"
                )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            backup_service.BackupIntegrityError,
        ) as exc:
            raise BackupEvidenceFailure("manifest_changed_during_probe") from exc


def _manifest_fields(
    manifest: dict,
    *,
    manifest_dir: Path,
) -> tuple[int, str | None, DatabaseCheckpointBinding | None]:
    schema = manifest.get("schema")
    revision = manifest.get("alembic_revision")
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema not in (1, 2)
    ):
        raise backup_service.BackupIntegrityError("unsupported schema")
    if revision is not None and not is_revision_id(revision):
        raise backup_service.BackupIntegrityError("invalid revision")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise backup_service.BackupIntegrityError("manifest files must be a list")
    database_entries = [
        entry
        for entry in files
        if isinstance(entry, dict)
        and entry.get("name") == backup_service.DB_BACKUP_NAME
    ]
    if len(database_entries) > 1:
        raise backup_service.BackupIntegrityError("duplicate database checkpoint")
    if not database_entries:
        return schema, revision, None
    database_entry = database_entries[0]
    sha256 = database_entry.get("sha256")
    byte_size = database_entry.get("bytes")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or sha256.lower() != sha256
        or any(character not in "0123456789abcdef" for character in sha256)
        or not isinstance(byte_size, int)
        or isinstance(byte_size, bool)
        or byte_size < 0
    ):
        raise backup_service.BackupIntegrityError("invalid database checkpoint binding")
    return (
        schema,
        revision,
        DatabaseCheckpointBinding(
            path=(manifest_dir / backup_service.DB_BACKUP_NAME).absolute(),
            expected_main=Sha256FileBinding(
                byte_size=byte_size,
                hexdigest=sha256,
            ),
        ),
    )


def _manifest_token(manifest: dict) -> str:
    canonical = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
