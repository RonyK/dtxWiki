import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from backend.media.application.install_validated import MediaSourceIntegrityError
from backend.media.domain import (
    AvailableBlobRecord,
    BlobEpoch,
    BlobReference,
    Sha256Digest,
)
from backend.services.library_blob_storage import (
    FilesystemBlobHandle,
    FilesystemLibraryBlobStore,
    LibraryBlobStorageError,
    LibraryBlobTooLargeError,
)
from backend.services.library_formats import FormatRegistry, LibraryFormatError
from backend.services.library_ingest import LibraryUploadAdmissionService


class AsyncUpload:
    def __init__(self, payload: bytes, *, chunk_size: int = 3) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0

    async def read(self, size: int) -> bytes:
        del size
        start = self._offset
        self._offset += self._chunk_size
        return self._payload[start : self._offset]


def _service(tmp_path):
    return _service_for_store(FilesystemLibraryBlobStore(tmp_path / "library"))


def _service_for_store(store, media_installer=None):
    return LibraryUploadAdmissionService(
        store_for=lambda _workspace_id: store,
        format_registry=FormatRegistry(),
        media_installer=media_installer or _FakeMediaInstaller(store),
    )


class _FakeMediaInstaller:
    def __init__(self, store):
        self.store = store

    def execute(self, **kwargs):
        del kwargs
        raise AssertionError("Library admission must use the bounded stream seam")

    def execute_stream(self, **kwargs):
        stream = kwargs["stream"]
        chunks = []
        while chunk := stream.read(3):
            if len(chunk) > 3:
                raise AssertionError("Library installer performed an unbounded read")
            chunks.append(chunk)
        content = b"".join(chunks)
        assert len(content) == kwargs["expected_byte_size"]
        assert hashlib.sha256(content).hexdigest() == kwargs["expected_sha256"]
        workspace_id = kwargs["workspace_id"]
        stored = self.store.put_bytes(
            content,
            workspace_id=workspace_id,
            max_bytes=kwargs["max_bytes"],
        )
        return AvailableBlobRecord(
            reference=BlobReference(workspace_id, uuid4(), BlobEpoch(1)),
            sha256=Sha256Digest(hashlib.sha256(content).hexdigest()),
            byte_size=len(content),
            storage_key=stored.storage_key,
            canonical_media_type=kwargs["canonical_media_type"],
            inspection_id=uuid4(),
        )


class _IntegrityFailureInstaller:
    def execute_stream(self, **kwargs):
        del kwargs
        raise MediaSourceIntegrityError("media source integrity mismatch")


def test_stream_integrity_failure_uses_typed_library_error_and_cleans_source(
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = _service_for_store(store, _IntegrityFailureInstaller())

    with pytest.raises(LibraryBlobStorageError) as raised:
        asyncio.run(
            service.admit(
                AsyncUpload(b"hello"),
                workspace_id=uuid4(),
                filename="notes.txt",
                declared_media_type="text/plain",
                max_bytes=100,
                enabled_formats=frozenset({"text"}),
                converter_formats=frozenset({"text"}),
                allow_store_without_converter=True,
            )
        )

    assert raised.value.code == "library_blob_staging_integrity"
    assert not list(store.staging_root.rglob("*.stage"))


def test_admit_streams_hashes_detects_and_publishes_text(tmp_path):
    workspace_id = uuid4()
    accepted = asyncio.run(
        _service(tmp_path).admit(
            AsyncUpload(b"# hello\n"),
            workspace_id=workspace_id,
            filename="notes.md",
            declared_media_type="text/markdown",
            max_bytes=100,
            enabled_formats=frozenset({"markdown"}),
            converter_formats=frozenset({"markdown"}),
            allow_store_without_converter=True,
        )
    )

    assert accepted.detection.format_id == "markdown"
    assert accepted.processing_disposition == "queued"
    assert accepted.blob.workspace_id == workspace_id
    assert accepted.blob.byte_size == 8
    assert accepted.blob.storage_key.endswith(accepted.blob.sha256)


class _RejectUnboundedReads:
    def __init__(self, stream) -> None:
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("unbounded staged-file read")
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stream.close()


def test_admit_never_uses_read_bytes_or_an_unbounded_file_read(
    tmp_path,
    monkeypatch,
):
    original_open_binary = FilesystemBlobHandle.open_binary

    def guard_staged_file_after_detection(*_args) -> None:
        def fail_read_bytes(_path):
            raise AssertionError("Path.read_bytes materialized the staged upload")

        def guarded_open_binary(handle):
            return _RejectUnboundedReads(original_open_binary(handle))

        monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
        monkeypatch.setattr(FilesystemBlobHandle, "open_binary", guarded_open_binary)

    accepted = asyncio.run(
        _service(tmp_path).admit(
            AsyncUpload(b"bounded handoff"),
            workspace_id=uuid4(),
            filename="notes.txt",
            declared_media_type="text/plain",
            max_bytes=100,
            enabled_formats=frozenset({"text"}),
            converter_formats=frozenset({"text"}),
            allow_store_without_converter=True,
            before_install=guard_staged_file_after_detection,
        )
    )

    assert accepted.blob.byte_size == len(b"bounded handoff")


def test_admit_preserves_known_format_without_converter(tmp_path):
    accepted = asyncio.run(
        _service(tmp_path).admit(
            AsyncUpload(b"hello"),
            workspace_id=uuid4(),
            filename="notes.txt",
            declared_media_type="text/plain",
            max_bytes=100,
            enabled_formats=frozenset({"text"}),
            converter_formats=frozenset(),
            allow_store_without_converter=True,
        )
    )

    assert accepted.processing_disposition == "stored_unconverted"


def test_converter_input_cap_downgrades_to_stored_unconverted(tmp_path):
    accepted = asyncio.run(
        _service(tmp_path).admit(
            AsyncUpload(b"hello"),
            workspace_id=uuid4(),
            filename="notes.txt",
            declared_media_type="text/plain",
            max_bytes=100,
            enabled_formats=frozenset({"text"}),
            converter_formats=frozenset({"text"}),
            converter_max_bytes={"text": 4},
            allow_store_without_converter=True,
        )
    )

    assert accepted.processing_disposition == "stored_unconverted"


def test_converter_input_cap_fails_closed_when_original_storage_is_disabled(
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = _service_for_store(store)

    with pytest.raises(LibraryFormatError) as raised:
        asyncio.run(
            service.admit(
                AsyncUpload(b"hello"),
                workspace_id=uuid4(),
                filename="notes.txt",
                declared_media_type="text/plain",
                max_bytes=100,
                enabled_formats=frozenset({"text"}),
                converter_formats=frozenset({"text"}),
                converter_max_bytes={"text": 4},
                allow_store_without_converter=False,
            )
        )

    assert raised.value.code == "library_converter_input_too_large"
    assert not list(store.blob_root.rglob("*"))


def test_rejected_format_never_publishes_a_blob(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = _service_for_store(store)

    with pytest.raises(LibraryFormatError) as raised:
        asyncio.run(
            service.admit(
                AsyncUpload(b"hello"),
                workspace_id=uuid4(),
                filename="notes.txt",
                declared_media_type="text/plain",
                max_bytes=100,
                enabled_formats=frozenset(),
                converter_formats=frozenset({"text"}),
                allow_store_without_converter=True,
            )
        )

    assert raised.value.code == "library_format_disabled"
    assert not list(store.blob_root.rglob("*"))


def test_missing_converter_can_fail_closed_without_publishing(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = _service_for_store(store)

    with pytest.raises(LibraryFormatError) as raised:
        asyncio.run(
            service.admit(
                AsyncUpload(b"hello"),
                workspace_id=uuid4(),
                filename="notes.txt",
                declared_media_type="text/plain",
                max_bytes=100,
                enabled_formats=frozenset({"text"}),
                converter_formats=frozenset(),
                allow_store_without_converter=False,
            )
        )

    assert raised.value.code == "library_converter_unavailable"
    assert not list(store.blob_root.rglob("*"))


def test_oversized_upload_cleans_staging(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = _service_for_store(store)

    with pytest.raises(LibraryBlobTooLargeError):
        asyncio.run(
            service.admit(
                AsyncUpload(b"too large", chunk_size=4),
                workspace_id=uuid4(),
                filename="notes.txt",
                declared_media_type="text/plain",
                max_bytes=5,
                enabled_formats=frozenset({"text"}),
                converter_formats=frozenset({"text"}),
                allow_store_without_converter=True,
            )
        )

    assert not list(store.staging_root.rglob("*.stage"))
