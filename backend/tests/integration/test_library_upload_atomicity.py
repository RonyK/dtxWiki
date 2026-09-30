from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select

from backend.app.container import assemble
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.library_media.ports import LibraryLogicalQuotaExceededError
from backend.models.base import Base
from backend.models.library import (
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobInspectionRow,
    StoredBlobRow,
)
from backend.models.workspace import Workspace
from backend.services.settings_service import (
    library_format_is_enabled,
    library_upload_limit_bytes,
    normalized_library_policy,
    settings_policy_revision,
)
from backend.spi.conversion import ConversionLimits
from backend.tests.conftest import seed_default_workspace


class _AsyncUpload:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._content) - self._offset
        start = self._offset
        self._offset = min(len(self._content), self._offset + size)
        return self._content[start : self._offset]


def test_concurrent_distinct_digest_quota_race_has_one_atomic_winner(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'library-quota-race.sqlite'}"
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    Base.metadata.create_all(engine)

    first_content = b"# Quota race A\n"
    second_content = b"# Quota race B\n"
    assert len(first_content) == len(second_content)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.flush()
        workspace_id = workspace.id

    settings = Settings(
        database_url=database_url,
        session_secret="library-atomicity-test-secret",
        library_blob_root=str(tmp_path / "library-blobs"),
    )
    services = assemble(
        settings,
        session_factory=session_factory,
    ).get_services()
    with session_factory.begin() as session:
        services.library_logical_quota_factory(session).ensure_capacity(
            workspace_id,
            quota_kind="source",
            additional_bytes=0,
        )
        usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": workspace_id, "quota_kind": "source"},
        )
        assert usage is not None
        usage.limit_bytes = len(first_content)
    with session_factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        policy = normalized_library_policy(workspace.library_policy)
    policy_revision = settings_policy_revision("library", policy)
    upload_limit = library_upload_limit_bytes(
        policy,
        instance_hard_cap_bytes=settings.library_upload_hard_cap_bytes,
    )
    descriptors = services.library_format_registry.descriptors()
    format_limits = {
        descriptor.format_id: library_upload_limit_bytes(
            policy,
            format_id=descriptor.format_id,
            instance_hard_cap_bytes=settings.library_upload_hard_cap_bytes,
        )
        for descriptor in descriptors
    }
    enabled_formats = frozenset(
        descriptor.format_id
        for descriptor in descriptors
        if library_format_is_enabled(policy, descriptor.format_id)
    )
    converter_formats = frozenset(services.library_converter_registry.ready_formats())
    converter_max_bytes = {
        descriptor.format_id: services.library_converter_registry.ready_input_limit(
            descriptor.format_id
        )
        for descriptor in descriptors
    }
    start_capacity_decision = Barrier(2, timeout=10)

    def upload(filename: str, content: bytes):
        actor_id = str(uuid4())
        operation_id = uuid4()
        try:
            with session_factory.begin() as session:
                locked_policy: dict | None = None

                def ensure_source_capacity(
                    detection,
                    digest: str,
                    byte_size: int,
                ) -> None:
                    nonlocal locked_policy
                    start_capacity_decision.wait()
                    locked_policy = (
                        services.library_binary_registration.ensure_source_quota_capacity(
                            session=session,
                            workspace_id=workspace_id,
                            existing_item_id=None,
                            upload_operation_id=operation_id,
                            sha256=digest,
                            byte_size=byte_size,
                            expected_policy_revision=policy_revision,
                        )
                    )

                accepted = asyncio.run(
                    services.library_upload_admission_for_session(
                        session, workspace_id
                    ).admit(
                        _AsyncUpload(content),
                        workspace_id=workspace_id,
                        filename=filename,
                        declared_media_type="text/markdown",
                        max_bytes=upload_limit,
                        enabled_formats=enabled_formats,
                        converter_formats=converter_formats,
                        allow_store_without_converter=policy[
                            "allow_store_without_converter"
                        ],
                        format_max_bytes=format_limits,
                        converter_max_bytes=converter_max_bytes,
                        actor_kind="human",
                        actor_id=actor_id,
                        before_install=ensure_source_capacity,
                    )
                )
                assert locked_policy is not None
                detected_limit = format_limits[accepted.descriptor.format_id]
                converter_limit = converter_max_bytes.get(
                    accepted.descriptor.format_id
                )
                limits = ConversionLimits(
                    max_input_bytes=(
                        min(detected_limit, converter_limit)
                        if converter_limit is not None
                        else detected_limit
                    ),
                    max_output_bytes=settings.library_conversion_max_output_bytes,
                    max_asset_bytes=settings.library_conversion_max_asset_bytes,
                    timeout_seconds=settings.library_conversion_timeout_seconds,
                )
                registration = services.library_binary_registration.register(
                    session=session,
                    workspace_id=workspace_id,
                    accepted=accepted,
                    workspace_policy=locked_policy,
                    limits=limits,
                    actor_kind="human",
                    actor_id=actor_id,
                    upload_operation_id=operation_id,
                )
                return ("ok", registration.version.id, accepted.blob.sha256)
        except LibraryLogicalQuotaExceededError as exc:
            return ("quota", exc.error_code, sha256(content).hexdigest())

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(upload, "race-a.md", first_content),
                executor.submit(upload, "race-b.md", second_content),
            )
            outcomes = [future.result(timeout=20) for future in futures]

        assert sorted(outcome[0] for outcome in outcomes) == ["ok", "quota"]
        rejected = next(outcome for outcome in outcomes if outcome[0] == "quota")
        assert rejected[1] == "media_quota_exceeded"
        rejected_digest = rejected[2]

        count_models = (
            StoredBlobRow,
            BlobIncarnationRow,
            StoredBlobInspectionRow,
            MediaStagingLease,
            MediaInstallRequest,
            MediaOutboxRow,
            LibraryLogicalCharge,
            LibraryItem,
            LibraryItemVersion,
        )
        with session_factory() as session:
            counts = {
                model.__tablename__: session.scalar(
                    select(func.count()).select_from(model)
                )
                for model in count_models
            }
            usage = session.get(
                LibraryLogicalQuotaUsage,
                {"workspace_id": workspace_id, "quota_kind": "source"},
            )
            physical_charge_count = session.scalar(
                select(func.count()).select_from(MediaPhysicalCharge)
            )
        assert usage is not None
        assert usage.charged_bytes == len(first_content)
        assert counts == {
            "stored_blob": 1,
            "blob_incarnation": 1,
            "stored_blob_inspection": 1,
            "media_staging_lease": 1,
            "media_install_request": 1,
            "media_outbox": 2,
            "library_logical_charge": 1,
            "library_item": 1,
            "library_item_version": 1,
        }
        assert physical_charge_count == 2
        stored_files = {
            path.relative_to(Path(settings.library_blob_root)).as_posix()
            for path in Path(settings.library_blob_root).rglob("*")
            if path.is_file()
        }
        assert len(stored_files) == 1
        assert all(rejected_digest not in path for path in stored_files)
    finally:
        engine.dispose()
