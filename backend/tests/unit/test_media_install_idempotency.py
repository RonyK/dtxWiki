from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

import pytest

from backend.media.application.install_validated import (
    InstallValidatedBlob,
    _operation_fingerprint,
)
from backend.media.domain import (
    AvailableBlobCandidate,
    BlobEpoch,
    BlobReference,
    Sha256Digest,
)
from backend.media.ports.install import (
    MediaInstallInProgressError,
    PreparedBlobInstall,
    PreparedMediaOperation,
)
from backend.services.library_blob_storage import FilesystemLibraryBlobStore

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("30000000-0000-0000-0000-000000000003")


class _InProgressJournal:
    def __init__(self, phase: str) -> None:
        self.phase = phase

    def resolve_install_blob(self, *, workspace_id, sha256, proposed_blob_id):
        del sha256
        return BlobReference(workspace_id, proposed_blob_id, BlobEpoch(1))

    def prepare_operation(self, **_kwargs) -> PreparedMediaOperation:
        if self.phase == "operation":
            return PreparedMediaOperation(in_progress=True)
        return PreparedMediaOperation(
            owns_staging=True,
            claim_fence=datetime(2025, 1, 1, 1, tzinfo=UTC),
        )

    def record_staged(self, _staged, *, charge_id, operation_claim) -> None:
        del charge_id, operation_claim

    def prepare_install(self, **_kwargs) -> PreparedBlobInstall:
        return PreparedBlobInstall(in_progress=True)


def _candidate() -> AvailableBlobCandidate:
    return AvailableBlobCandidate(
        workspace_id=WORKSPACE_ID,
        sha256=Sha256Digest("a" * 64),
        byte_size=12,
        storage_key="b/workspace/blob/1/1/aa/" + "a" * 64,
        canonical_media_type="image/png",
        format_id="png",
        detector_id="signature",
        detector_version="1",
        registry_version="registry-v1",
        validation_schema_version="schema-v1",
        metadata={"animated": False, "width": 10},
    )


def _fingerprint(
    candidate: AvailableBlobCandidate,
    *,
    actor_id=ACTOR_ID,
    session_key="context:session",
    idempotency_key="context:asset",
    request_id=REQUEST_ID,
) -> str:
    return _operation_fingerprint(
        candidate,
        actor_id=actor_id,
        session_key=session_key,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )


def test_media_install_fingerprint_canonicalizes_metadata_key_order() -> None:
    candidate = _candidate()

    reordered = replace(candidate, metadata={"width": 10, "animated": False})

    assert _fingerprint(reordered) == _fingerprint(candidate)


@pytest.mark.parametrize(
    ("candidate_changes", "operation_changes"),
    [
        ({"workspace_id": UUID("10000000-0000-0000-0000-000000000009")}, {}),
        ({"sha256": Sha256Digest("b" * 64)}, {}),
        ({"byte_size": 13}, {}),
        ({"storage_key": "b/other-target"}, {}),
        ({"canonical_media_type": "image/jpeg"}, {}),
        ({"format_id": "jpeg"}, {}),
        ({"detector_id": "magic"}, {}),
        ({"detector_version": "2"}, {}),
        ({"registry_version": "registry-v2"}, {}),
        ({"validation_schema_version": "schema-v2"}, {}),
        ({"metadata": {"animated": True, "width": 10}}, {}),
        ({}, {"actor_id": UUID("20000000-0000-0000-0000-000000000009")}),
        ({}, {"session_key": "context:other-session"}),
        ({}, {"idempotency_key": "context:other-asset"}),
        ({}, {"request_id": UUID("30000000-0000-0000-0000-000000000009")}),
    ],
)
def test_media_install_fingerprint_commits_every_immutable_operation_fact(
    candidate_changes,
    operation_changes,
) -> None:
    candidate = _candidate()

    changed_candidate = replace(candidate, **candidate_changes)

    assert _fingerprint(changed_candidate, **operation_changes) != _fingerprint(
        candidate
    )


@pytest.mark.parametrize("phase", ["operation", "publish_claim"])
def test_media_install_wait_is_bounded_with_a_fixed_domain_clock(
    phase,
    tmp_path,
) -> None:
    store = FilesystemLibraryBlobStore(tmp_path / phase)
    installer = InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=_InProgressJournal(phase),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
        wait_timeout_seconds=0.02,
    )
    started_at = monotonic()

    with pytest.raises(MediaInstallInProgressError):
        installer.execute(
            workspace_id=WORKSPACE_ID,
            actor_id=ACTOR_ID,
            session_key="context:session",
            idempotency_key="context:asset",
            content=b"bounded-wait",
            max_bytes=1024,
            canonical_media_type="image/png",
            format_id="png",
            detector_id="signature",
            detector_version="1",
            registry_version="registry-v1",
            validation_schema_version="schema-v1",
        )

    assert monotonic() - started_at < 1


def test_media_install_in_progress_failure_is_explicitly_retryable() -> None:
    error = MediaInstallInProgressError()

    assert str(error) == "media_install_in_progress"
    assert error.retryable is True
    assert error.outcome_unknown is True
