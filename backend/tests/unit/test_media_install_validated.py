from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest

from backend.media.application import InstallValidatedBlob
from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    BlobEpoch,
    BlobReference,
    Sha256Digest,
)
from backend.media.ports.contracts import StorageKey
from backend.media.ports.install import (
    PendingBlobInstall,
    PreparedBlobInstall,
    PreparedMediaOperation,
)
from backend.services.library_blob_storage import FilesystemLibraryBlobStore


class _Journal:
    def __init__(self) -> None:
        self.available = {}
        self.reservations = []
        self.recorded = []
        self.discarded = []
        self.released = []
        self.completed = []
        self._candidates = {}
        self.fail_prepare = False
        self.prepared_operations = []
        # Only the calls that actually granted this caller the staging
        # workflow. prepare_operation absorbed reserve_staging, so this is the
        # modern stand-in for the old `reservations` list: one entry per
        # admitted staging, none when the operation was reused or deferred.
        self.staging_admissions = []
        self.released_claims = []
        # Set to False to make prepare_operation report a concurrent replay
        # instead of granting this caller the staging workflow.
        self.admit_operations = True

    def find_available(self, workspace_id, digest):
        return self.available.get((workspace_id, digest.value))

    def resolve_install_blob(self, *, workspace_id, sha256, proposed_blob_id):
        existing = self.find_available(workspace_id, sha256)
        return (
            existing.reference
            if existing is not None
            else BlobReference(workspace_id, proposed_blob_id, BlobEpoch(1))
        )

    def reserve_staging(self, **kwargs) -> None:
        self.reservations.append(kwargs)

    def prepare_operation(self, **kwargs) -> PreparedMediaOperation:
        """Bind and admit an operation before anything is staged.

        Runs *before* physical staging, so `fail_prepare` (which simulates a
        crash once staging is already accounted) deliberately does not fire
        here — it stays on prepare_install below.
        """

        self.prepared_operations.append(kwargs)
        candidate = kwargs["candidate"]
        existing = self.find_available(candidate.workspace_id, candidate.sha256)
        if existing is not None:
            return PreparedMediaOperation(reused=existing)
        if not (kwargs.get("admit_new_operation", True) and self.admit_operations):
            return PreparedMediaOperation(in_progress=True)
        # Owning the staging workflow requires an exact claim fence.
        self.staging_admissions.append(kwargs)
        return PreparedMediaOperation(
            owns_staging=True,
            claim_fence=kwargs["now"],
        )

    def release_install_claim(self, pending, *, failure_code) -> None:
        self.released_claims.append((pending, failure_code))

    def record_staged(self, staged, *, charge_id, operation_claim) -> None:
        self.recorded.append((staged, charge_id, operation_claim))

    def discard_unaccounted_staging(self, staged, *, deletion) -> None:
        assert deletion.target == staged.target
        assert deletion.storage_key == staged.storage_key
        self.discarded.append((staged, deletion))

    def prepare_install(
        self,
        *,
        candidate: AvailableBlobCandidate,
        staged,
        staging_charge_id,
        blob_charge_id,
        request_id,
        proposed_blob_id,
        proposed_blob_epoch,
        destination,
        publish_generation,
        idempotency_key,
        claim_token,
        operation_claim,
        association_hold,
    ) -> PreparedBlobInstall:
        if self.fail_prepare:
            raise RuntimeError("simulated crash after staging was accounted")
        existing = self.find_available(candidate.workspace_id, candidate.sha256)
        if existing is not None:
            return PreparedBlobInstall(reused=existing)
        pending = PendingBlobInstall(
            request_id=request_id,
            blob=BlobReference(
                candidate.workspace_id,
                proposed_blob_id,
                BlobEpoch(proposed_blob_epoch),
            ),
            inspection_id=uuid4(),
            sha256=candidate.sha256,
            byte_size=candidate.byte_size,
            staged=staged,
            staging_charge_id=staging_charge_id,
            blob_charge_id=blob_charge_id,
            destination=destination,
            publish_generation=publish_generation,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            association_hold=association_hold,
        )
        self._candidates[request_id] = candidate
        return PreparedBlobInstall(pending=pending)

    def release_staging(self, staged, *, charge_id, deletion) -> None:
        self.released.append((staged, charge_id, deletion))

    def complete_install(
        self,
        pending: PendingBlobInstall,
        *,
        publish,
        staging_deletion,
    ) -> AvailableBlobRecord:
        candidate = self._candidates[pending.request_id]
        assert publish.sha256 == pending.sha256
        assert staging_deletion.target == pending.staged.target
        available = AvailableBlobRecord(
            reference=pending.blob,
            sha256=pending.sha256,
            byte_size=pending.byte_size,
            storage_key=pending.destination.value,
            canonical_media_type=candidate.canonical_media_type,
            inspection_id=pending.inspection_id,
        )
        self.available[(pending.blob.workspace_id, pending.sha256.value)] = available
        self.completed.append(available)
        return available


class _BoundedReadStream(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size <= 0 or size > 64 * 1024:
            raise AssertionError("stream installer used an unbounded read")
        return super().read(size)


class _FailingReadStream(_BoundedReadStream):
    def read(self, size: int = -1) -> bytes:
        raise OSError("simulated staged-source read failure")


class _ChangesAfterRewind(_BoundedReadStream):
    def __init__(self, validated: bytes, staged: bytes) -> None:
        super().__init__(validated)
        self._staged = BytesIO(staged)
        self._use_staged = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size <= 0 or size > 64 * 1024:
            raise AssertionError("stream installer used an unbounded read")
        if self._use_staged:
            return self._staged.read(size)
        return BytesIO.read(self, size)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._use_staged = True
        self._staged.seek(offset, whence)
        return BytesIO.seek(self, offset, whence)


def _installer(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "media")
    journal = _Journal()
    installer = InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=journal,
    )
    return installer, store, journal


def _execute_stream(
    installer,
    stream,
    *,
    workspace_id,
    expected_content: bytes,
    idempotency_key: str = "library:stream",
):
    return installer.execute_stream(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="library:test-session",
        idempotency_key=idempotency_key,
        stream=stream,
        expected_sha256=sha256(expected_content).hexdigest(),
        expected_byte_size=len(expected_content),
        max_bytes=len(expected_content) + 100,
        canonical_media_type="text/plain",
        format_id="text",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
    )


def test_stream_install_reads_bounded_chunks_and_reserves_exact_length(tmp_path):
    installer, store, journal = _installer(tmp_path)
    workspace_id = uuid4()
    content = b"bounded-media" * 10_001
    stream = _BoundedReadStream(content)

    available = _execute_stream(
        installer,
        stream,
        workspace_id=workspace_id,
        expected_content=content,
    )

    assert stream.read_sizes
    assert max(stream.read_sizes) == 64 * 1024
    # prepare_operation receives the caller's generous ceiling, so the
    # "reserve exactly what we need" invariant now lives on the accounted
    # staging record rather than on the admission cap.
    assert journal.recorded[0][0].byte_size == len(content)
    assert available.sha256 == Sha256Digest(sha256(content).hexdigest())
    assert available.byte_size == len(content)
    handle = store.open(StorageKey(available.storage_key))
    try:
        assert handle.read(len(content) + 1) == content
    finally:
        handle.close()
    assert not list(store.staging_root.rglob("*.stage"))


@pytest.mark.parametrize(
    "actual_content",
    [
        b"tampered-content",
        b"expected-conten",
        b"expected-content!",
    ],
)
def test_stream_install_rejects_tampered_facts_before_reservation(
    tmp_path,
    actual_content,
):
    installer, store, journal = _installer(tmp_path)
    expected_content = b"expected-content"

    with pytest.raises(ValueError, match="media source integrity mismatch"):
        _execute_stream(
            installer,
            _BoundedReadStream(actual_content),
            workspace_id=uuid4(),
            expected_content=expected_content,
        )

    assert not journal.staging_admissions
    assert not journal.recorded
    assert not journal.discarded
    assert not journal.available
    assert not list(store.staging_root.rglob("*.stage"))
    assert not [path for path in store.blob_root.rglob("*") if path.is_file()]


def test_stream_read_failure_happens_before_reservation(tmp_path):
    installer, store, journal = _installer(tmp_path)
    expected_content = b"expected-content"

    with pytest.raises(ValueError, match="media source integrity mismatch"):
        _execute_stream(
            installer,
            _FailingReadStream(expected_content),
            workspace_id=uuid4(),
            expected_content=expected_content,
        )

    assert not journal.staging_admissions
    assert not journal.discarded
    assert not journal.recorded
    assert not list(store.staging_root.rglob("*.stage"))


def test_stream_change_after_rewind_cannot_reach_storage(tmp_path):
    """A source that mutates after a rewind must not influence what is stored.

    This used to assert a discarded stage: the installer validated the stream,
    rewound it, and re-read it while staging, so a mutating source produced an
    integrity mismatch mid-flight. `_validated_stream_chunks` now reads the
    source exactly once and stages the buffered bytes it validated, so the
    rewind window no longer exists — the install succeeds and the mutated bytes
    are structurally unreachable. Asserting the stored digest keeps that
    guarantee pinned: reintroducing a rewind-and-re-read would stage
    `b"x" * n` here and fail this test.
    """

    installer, store, journal = _installer(tmp_path)
    expected_content = b"expected-content"
    mutated = b"x" * len(expected_content)
    assert mutated != expected_content

    available = _execute_stream(
        installer,
        _ChangesAfterRewind(expected_content, mutated),
        workspace_id=uuid4(),
        expected_content=expected_content,
    )

    assert available.sha256 == Sha256Digest(sha256(expected_content).hexdigest())
    assert available.byte_size == len(expected_content)
    staged, _charge_id, _claim = journal.recorded[0]
    assert staged.sha256 == Sha256Digest(sha256(expected_content).hexdigest())
    assert staged.byte_size == len(expected_content)
    # Nothing had to be thrown away, and no stage file was left behind.
    assert not journal.discarded
    assert not list(store.staging_root.rglob("*.stage"))


def test_bad_retry_preserves_preexisting_accounted_staging(tmp_path):
    installer, store, journal = _installer(tmp_path)
    workspace_id = uuid4()
    content = b"retryable-content"
    journal.fail_prepare = True

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute_stream(
            installer,
            _BoundedReadStream(content),
            workspace_id=workspace_id,
            expected_content=content,
        )
    stage_path = next(store.staging_root.rglob("*.stage"))
    staged_before = stage_path.read_bytes()

    with pytest.raises(ValueError, match="media source integrity mismatch"):
        _execute_stream(
            installer,
            _BoundedReadStream(b"x" * len(content)),
            workspace_id=workspace_id,
            expected_content=content,
        )

    assert stage_path.read_bytes() == staged_before == content
    assert len(journal.staging_admissions) == 1
    assert len(journal.recorded) == 1
    assert not journal.discarded


def test_stream_install_reuse_validates_source_without_new_staging(tmp_path):
    installer, _store, journal = _installer(tmp_path)
    workspace_id = uuid4()
    content = b"idempotent-content"
    first = _execute_stream(
        installer,
        _BoundedReadStream(content),
        workspace_id=workspace_id,
        expected_content=content,
    )

    with pytest.raises(ValueError, match="media source integrity mismatch"):
        _execute_stream(
            installer,
            _BoundedReadStream(b"x" * len(content)),
            workspace_id=workspace_id,
            expected_content=content,
            idempotency_key="library:tampered-replay",
        )
    replay_stream = _BoundedReadStream(content)
    replay = _execute_stream(
        installer,
        replay_stream,
        workspace_id=workspace_id,
        expected_content=content,
    )

    assert replay == first
    assert replay_stream.read_sizes
    assert len(journal.staging_admissions) == 1
    assert len(journal.completed) == 1


def test_bytes_execute_remains_backward_compatible(tmp_path):
    installer, _store, journal = _installer(tmp_path)
    workspace_id = uuid4()
    content = b"existing byte caller"

    available = installer.execute(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="context:test-session",
        idempotency_key="context:bytes",
        content=content,
        max_bytes=100,
        canonical_media_type="image/png",
        format_id="png",
        detector_id="test-detector",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
    )

    assert available.byte_size == len(content)
    # prepare_operation receives the caller's generous ceiling, so the
    # "reserve exactly what we need" invariant now lives on the accounted
    # staging record rather than on the admission cap.
    assert journal.recorded[0][0].byte_size == len(content)
