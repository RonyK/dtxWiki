import asyncio
import hashlib
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

import backend.services.library_blob_storage as blob_storage
from backend.media.domain import BlobEpoch, BlobReference, Sha256Digest
from backend.media.ports.contracts import (
    GenerationFence,
    IdempotencyConflictError,
    NoOverwriteError,
    PhysicalChargeTarget,
    PhysicalDeleteDisposition,
    PhysicalObjectKind,
    PublishDisposition,
)
from backend.services.library_blob_storage import (
    FilesystemLibraryBlobStore,
    LibraryBlobStorageError,
    LibraryBlobTooLargeError,
)


def test_blob_store_hashes_real_bytes_and_commits_to_opaque_relative_key(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    content = b"abc" * 100
    workspace_id = uuid4()

    with store.stage_chunks(
        (content[:101], content[101:]),
        workspace_id=workspace_id,
        max_bytes=len(content),
    ) as staged:
        stored = staged.commit()

    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    assert stored.workspace_id == workspace_id
    assert stored.storage_key == store.storage_key_for_hash(workspace_id, stored.sha256)
    assert stored.storage_key.startswith("w/")
    assert not stored.storage_key.startswith(str(tmp_path))
    with store.open_handle(
        stored.storage_key, byte_size=stored.byte_size
    ).open_binary() as stream:
        assert stream.read() == content


def test_blob_store_deduplicates_same_hash(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()

    first = store.put_bytes(b"same", workspace_id=workspace_id, max_bytes=4)
    second = store.put_bytes(b"same", workspace_id=workspace_id, max_bytes=4)

    assert first.storage_key == second.storage_key
    assert first.created is True
    assert second.created is False


def test_blob_store_scopes_equal_bytes_to_each_workspace(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")

    first = store.put_bytes(b"same", workspace_id=uuid4(), max_bytes=4)
    second = store.put_bytes(b"same", workspace_id=uuid4(), max_bytes=4)

    assert first.sha256 == second.sha256
    assert first.storage_key != second.storage_key
    assert first.created is True
    assert second.created is True


def test_blob_store_implements_shared_media_stage_publish_read_ports(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()
    blob = BlobReference(workspace_id, uuid4(), BlobEpoch(1))
    fence = GenerationFence(blob, 1)
    content = b"shared media"
    target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    staged = store.stage((b"shared ", b"media"), target=target, max_bytes=64)
    destination = store.blob_key(
        blob,
        Sha256Digest(hashlib.sha256(content).hexdigest()),
        publish_generation=1,
    )

    receipt = store.publish_no_overwrite(staged, target=destination, fence=fence)

    assert receipt.disposition is PublishDisposition.PUBLISHED
    handle = store.open(destination)
    try:
        assert handle.byte_size == len(content)
        assert handle.read() == content
    finally:
        handle.close()
    assert (
        store.delete(staged.storage_key, target=target).disposition
        is PhysicalDeleteDisposition.ALREADY_MISSING
    )

    replay_target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    replay = store.stage((content,), target=replay_target, max_bytes=64)
    assert (
        store.publish_no_overwrite(replay, target=destination, fence=fence).disposition
        is PublishDisposition.IDEMPOTENT_REPLAY
    )


def test_shared_media_publish_rejects_collision_and_stale_fence(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()
    blob = BlobReference(workspace_id, uuid4(), BlobEpoch(1))
    content = b"original"
    digest = Sha256Digest(hashlib.sha256(content).hexdigest())
    destination = store.blob_key(blob, digest, publish_generation=1)

    first_target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    first = store.stage((content,), target=first_target, max_bytes=64)
    store.publish_no_overwrite(
        first,
        target=destination,
        fence=GenerationFence(blob, 1),
    )

    different_target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    different = store.stage((b"different",), target=different_target, max_bytes=64)
    with pytest.raises(NoOverwriteError):
        store.publish_no_overwrite(
            different,
            target=destination,
            fence=GenerationFence(blob, 1),
        )

    replay_target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    replay = store.stage((content,), target=replay_target, max_bytes=64)
    with pytest.raises(IdempotencyConflictError, match="fence"):
        store.publish_no_overwrite(
            replay,
            target=destination,
            fence=GenerationFence(blob, 2),
        )


def test_blob_store_concurrent_same_hash_has_one_atomic_winner(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()
    content = b"concurrent immutable content"
    barrier = threading.Barrier(2, timeout=30)

    def publish():
        with store.stage_chunks(
            (content,), workspace_id=workspace_id, max_bytes=len(content)
        ) as staged:
            barrier.wait(timeout=5)
            return staged.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: publish(), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.storage_key for result in results}) == 1
    with store.open_handle(
        results[0].storage_key, byte_size=len(content)
    ).open_binary() as stream:
        assert stream.read() == content


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace regression")
def test_blob_store_accepts_namespace_equivalent_resolved_path(tmp_path, monkeypatch):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()
    digest = hashlib.sha256(b"namespace equivalent").hexdigest()
    storage_key = store.storage_key_for_hash(workspace_id, digest)
    candidate = store.blob_root / Path(storage_key)
    original_resolve = Path.resolve

    def resolve_with_extended_namespace(path, strict=False):
        resolved = original_resolve(path, strict=strict)
        if path == candidate:
            return Path("\\\\?\\" + str(resolved))
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_with_extended_namespace)

    resolved = store.path_for_key(storage_key)

    assert str(resolved).startswith("\\\\?\\")
    assert blob_storage._windows_path_is_within(store.blob_root, resolved)


@pytest.mark.parametrize(
    ("root", "candidate", "expected"),
    [
        (r"C:\library", r"\\?\C:\library\blobs\item", True),
        (r"\\?\C:\library", r"C:\library\blobs\item", True),
        (
            r"\\server\share\library",
            r"\\?\UNC\server\share\library\blobs\item",
            True,
        ),
        (r"C:\library", r"\\?\C:\library-sibling\item", False),
        (
            r"C:\library",
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\library\item",
            False,
        ),
    ],
)
def test_windows_path_containment_only_aliases_equivalent_namespaces(
    root, candidate, expected
):
    assert blob_storage._windows_path_is_within(root, candidate) is expected


def test_blob_store_rejects_same_size_corruption_on_open_and_dedup(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()
    stored = store.put_bytes(b"good", workspace_id=workspace_id, max_bytes=4)
    handle = store.open_handle(stored.storage_key, byte_size=4)

    store.path_for_key(stored.storage_key).write_bytes(b"evil")

    with pytest.raises(LibraryBlobStorageError) as open_error:
        handle.open_binary()
    assert open_error.value.code == "library_blob_integrity"

    with pytest.raises(LibraryBlobStorageError) as dedup_error:
        store.put_bytes(b"good", workspace_id=workspace_id, max_bytes=4)
    assert dedup_error.value.code == "library_blob_collision"
    assert list(store.staging_root.rglob("*.stage")) == []


def test_blob_handle_copy_reports_and_validates_actual_content(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    content = b"copy me exactly"
    stored = store.put_bytes(content, workspace_id=uuid4(), max_bytes=len(content))
    handle = store.open_handle(stored.storage_key, byte_size=stored.byte_size)
    destination = tmp_path / "scratch" / "source.bin"

    copied_size = handle.copy_to(destination)

    assert copied_size == len(content)
    assert destination.read_bytes() == content
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == stored.sha256


def test_blob_handle_copy_leaves_no_output_when_source_was_corrupted(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    stored = store.put_bytes(b"source", workspace_id=uuid4(), max_bytes=6)
    handle = store.open_handle(stored.storage_key, byte_size=stored.byte_size)
    store.path_for_key(stored.storage_key).write_bytes(b"broken")
    destination = tmp_path / "scratch" / "source.bin"

    with pytest.raises(LibraryBlobStorageError):
        handle.copy_to(destination)

    assert not destination.exists()
    assert list(destination.parent.glob("*.copy")) == []


def test_async_upload_offloads_blocking_staging_writes(tmp_path, monkeypatch):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    content = b"first-second"
    write_thread_ids = []
    original_write = blob_storage._write_staging_chunk

    def recording_write(stream, digest, chunk):
        write_thread_ids.append(threading.get_ident())
        original_write(stream, digest, chunk)

    monkeypatch.setattr(blob_storage, "_write_staging_chunk", recording_write)

    class Upload:
        def __init__(self):
            self.chunks = iter((b"first-", b"second", b""))

        async def read(self, _size):
            await asyncio.sleep(0)
            return next(self.chunks)

    async def stage():
        event_loop_thread = threading.get_ident()
        staged = await store.stage_upload(
            Upload(), workspace_id=uuid4(), max_bytes=len(content)
        )
        return event_loop_thread, staged

    event_loop_thread, staged = asyncio.run(stage())
    with staged:
        stored = staged.commit()

    assert write_thread_ids
    assert all(thread_id != event_loop_thread for thread_id in write_thread_ids)
    assert stored.byte_size == len(content)


def test_blob_store_removes_staging_file_on_limit_failure(tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    workspace_id = uuid4()

    with pytest.raises(LibraryBlobTooLargeError):
        store.stage_chunks((b"123", b"45"), workspace_id=workspace_id, max_bytes=4)

    assert list(store.staging_root.rglob("*.stage")) == []


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "sha256/aa/bb/not-a-hash",
        "sha256/aa/bb/" + "a" * 63 + "g",
        "sha256/ff/ff/" + "a" * 64,
        "w/not-a-workspace/aa/" + "a" * 64,
    ],
)
def test_blob_store_rejects_noncanonical_or_traversing_keys(tmp_path, key):
    store = FilesystemLibraryBlobStore(tmp_path / "library")

    with pytest.raises(LibraryBlobStorageError):
        store.path_for_key(key)


def test_blob_store_rejects_a_symlinked_blob_root(tmp_path):
    root = tmp_path / "library"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "blobs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is not permitted on this host")

    with pytest.raises(LibraryBlobStorageError) as error:
        FilesystemLibraryBlobStore(root)

    assert error.value.code == "library_blob_path_unsafe"


def _seed_workspace_bytes(store, workspace_id):
    """Put a file under each of the four workspace-partitioned roots."""

    token = blob_storage._workspace_token(workspace_id)
    seeded = [
        store.blob_root / "w" / token / "ab" / ("a" * 64),
        store.blob_root / "b" / token / uuid4().hex / "1" / "1" / "cd" / ("c" * 64),
        store.staging_root / "media" / workspace_id.hex / "lease.0.stage",
        store.trash_root / workspace_id.hex / uuid4().hex / "1" / "1" / "x.trash",
    ]
    for path in seeded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes")
    return seeded


def test_purge_workspace_removes_every_root_for_one_workspace_only(tmp_path):
    """A workspace purge has to reclaim the bytes, not just the rows.

    Deleting the rows and leaving the files would grow the store forever with
    content nothing can ever reference again -- and the four roots are laid out
    per workspace precisely so this is a bounded operation.
    """

    store = FilesystemLibraryBlobStore(tmp_path / "library")
    doomed = uuid4()
    neighbour = uuid4()
    doomed_paths = _seed_workspace_bytes(store, doomed)
    neighbour_paths = _seed_workspace_bytes(store, neighbour)

    removed = store.purge_workspace(doomed)

    assert len(removed) == 4
    for path in doomed_paths:
        assert not path.exists(), f"{path} survived the purge"
    for path in neighbour_paths:
        assert path.exists(), f"{path} belongs to another workspace"


def test_purge_workspace_is_idempotent_when_nothing_is_stored(tmp_path):
    # The delete path calls this unconditionally, so a workspace that never
    # stored a byte must not turn a completed deletion into an error.
    store = FilesystemLibraryBlobStore(tmp_path / "library")

    assert store.purge_workspace(uuid4()) == []


def test_purge_workspace_refuses_to_follow_a_link_out_of_the_store(tmp_path):
    """The containment guard the read paths use applies to deletion too.

    Without it a symlinked workspace directory would hand rmtree a path outside
    the store -- the one bug in this method that would destroy unrelated data.
    """

    store = FilesystemLibraryBlobStore(tmp_path / "library")
    outside = tmp_path / "not-the-store"
    outside.mkdir(parents=True)
    (outside / "keepme.txt").write_bytes(b"unrelated")

    workspace_id = uuid4()
    token = blob_storage._workspace_token(workspace_id)
    link = store.blob_root / "w" / token
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows refuses symlinks without developer mode or elevation, which
        # would silently skip the one test that proves rmtree cannot escape the
        # store -- on the platform most likely to hit it. A directory junction
        # needs no privilege and is the same reparse point the guard checks.
        if os.name != "nt":
            pytest.skip("symlink creation is not permitted in this environment")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not link.exists():
            pytest.skip("neither a symlink nor a junction could be created")

    with pytest.raises(LibraryBlobStorageError):
        store.purge_workspace(workspace_id)

    assert (outside / "keepme.txt").exists()
